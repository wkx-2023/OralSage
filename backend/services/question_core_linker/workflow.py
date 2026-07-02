from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
from typing import Any, NotRequired, TypedDict

from langgraph.graph import END, START, StateGraph

from backend.services.question_bank_builder.exporters import write_json, write_jsonl
from backend.services.question_bank_builder.qwen_client import (
    QwenConfigurationError,
    QwenRequestError,
    QwenSchemaError,
    structured_completion,
)

from .catalog import chapter_candidates_for_subject, compact_core_payload, load_kg_catalog
from .prompts import NO_FALLBACK_SYSTEM, chapter_route_prompt, core_match_prompt, verifier_prompt
from .schemas import (
    ChapterCandidate,
    ChapterRouteDecision,
    CoreMatchLLMDecision,
    CoreVerifierDecision,
    KGCoreLinking,
    KGCoreNode,
    LinkerReviewItem,
    LinkedQuestionBank,
    RouteInfo,
)


class CoreLinkerState(TypedDict):
    question_bank_path: str
    kg_draft_dir: str
    output_dir: str
    review_dir: str
    reports_dir: str
    intermediate_dir: str
    book_name: NotRequired[str | None]
    limit_records: NotRequired[int | None]
    sample_stride: NotRequired[int | None]
    max_workers: NotRequired[int]
    records: NotRequired[list[dict[str, Any]]]
    source_meta: NotRequired[dict[str, Any]]
    kg_catalog: NotRequired[list[dict[str, Any]]]
    linked_records: NotRequired[list[dict[str, Any]]]
    review_required: NotRequired[list[dict[str, Any]]]
    summary: NotRequired[dict[str, Any]]


def _cache_key(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _cache_path(intermediate_dir: str, stage: str, key: str) -> Path:
    return Path(intermediate_dir) / "llm_outputs" / f"{stage}_{key}.json"


def _load_cached(path: Path, model_type):
    if not path.exists():
        return None
    return model_type.model_validate(json.loads(path.read_text(encoding="utf-8")))


def _cached_structured_completion(
    *,
    cache_path: Path,
    user_prompt: str,
    response_model,
    max_tokens: int,
):
    cached = _load_cached(cache_path, response_model)
    if cached is not None:
        return cached
    result = structured_completion(
        system_prompt=NO_FALLBACK_SYSTEM,
        user_prompt=user_prompt,
        response_model=response_model,
        temperature=0.0,
        max_tokens=max_tokens,
    )
    write_json(cache_path, result)
    return result


def _catalog_from_state(state: CoreLinkerState) -> list[KGCoreNode]:
    return [KGCoreNode.model_validate(row) for row in state.get("kg_catalog", [])]


def _review_item(
    record: dict[str, Any],
    status: str,
    reason: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item = LinkerReviewItem(
        question_id=str(record.get("id") or ""),
        subject_name=str(record.get("subject_name") or ""),
        chapter_name=str(record.get("chapter_name") or ""),
        status=status,  # type: ignore[arg-type]
        reason=reason,
        details=details or {},
    )
    return item.model_dump(mode="json")


def _chapter_candidate_payload(candidates: list[ChapterCandidate]) -> list[dict[str, Any]]:
    return [candidate.model_dump(mode="json") for candidate in candidates]


def _exact_chapter_candidate(
    candidates: list[ChapterCandidate],
    source_chapter_name: str,
) -> ChapterCandidate | None:
    for candidate in candidates:
        if candidate.kg_chapter_title == source_chapter_name:
            return candidate
    return None


def _route_failure_link(
    record: dict[str, Any],
    *,
    status: str,
    route: RouteInfo,
    rationale: str,
) -> KGCoreLinking:
    return KGCoreLinking(
        status=status,  # type: ignore[arg-type]
        route=route,
        rationale=rationale,
    )


def _resolve_chapter(
    *,
    record: dict[str, Any],
    candidates: list[ChapterCandidate],
    intermediate_dir: str,
) -> tuple[RouteInfo, ChapterCandidate | None, dict[str, Any] | None]:
    source_subject_name = str(record.get("subject_name") or "")
    source_chapter_name = str(record.get("chapter_name") or "")
    exact = _exact_chapter_candidate(candidates, source_chapter_name)
    if exact is not None:
        route = RouteInfo(
            subject_status="exact_subject",
            chapter_status="exact",
            source_subject_name=source_subject_name,
            source_chapter_name=source_chapter_name,
            kg_subject_name=exact.kg_subject_name,
            kg_chapter_title=exact.kg_chapter_title,
            draft_id=exact.draft_id,
            draft_file=exact.draft_file,
            reason="source chapter title exactly matches KG chapter title in the same subject",
            confidence=1.0,
        )
        return route, exact, None

    prompt = chapter_route_prompt(
        source_subject_name=source_subject_name,
        source_chapter_name=source_chapter_name,
        candidates=_chapter_candidate_payload(candidates),
    )
    try:
        decision = _cached_structured_completion(
            cache_path=_cache_path(
                intermediate_dir,
                "chapter_route",
                _cache_key(
                    {
                        "question_id": record.get("id"),
                        "subject": source_subject_name,
                        "chapter": source_chapter_name,
                        "candidate_drafts": [candidate.draft_id for candidate in candidates],
                    }
                ),
            ),
            user_prompt=prompt,
            response_model=ChapterRouteDecision,
            max_tokens=2000,
        )
    except (QwenConfigurationError, QwenRequestError, QwenSchemaError) as exc:
        route = RouteInfo(
            subject_status="exact_subject",
            chapter_status="llm_error",
            source_subject_name=source_subject_name,
            source_chapter_name=source_chapter_name,
            kg_subject_name=source_subject_name,
            reason=str(exc),
        )
        return route, None, _review_item(record, "llm_error", str(exc), {"stage": "chapter_route"})

    if decision.decision == "resolved":
        selected = next((candidate for candidate in candidates if candidate.draft_id == decision.draft_id), None)
        if selected is None:
            route = RouteInfo(
                subject_status="exact_subject",
                chapter_status="chapter_unresolved",
                source_subject_name=source_subject_name,
                source_chapter_name=source_chapter_name,
                kg_subject_name=source_subject_name,
                reason="Qwen selected a draft_id that is not in candidate chapters",
                confidence=decision.confidence,
            )
            return route, None, _review_item(
                record,
                "chapter_unresolved",
                route.reason,
                {"decision": decision.model_dump(mode="json")},
            )
        route = RouteInfo(
            subject_status="exact_subject",
            chapter_status="resolved_by_llm",
            source_subject_name=source_subject_name,
            source_chapter_name=source_chapter_name,
            kg_subject_name=selected.kg_subject_name,
            kg_chapter_title=selected.kg_chapter_title,
            draft_id=selected.draft_id,
            draft_file=selected.draft_file,
            reason=decision.reason,
            confidence=decision.confidence,
        )
        return route, selected, None

    status = "chapter_ambiguous" if decision.decision == "ambiguous" else "chapter_unresolved"
    route = RouteInfo(
        subject_status="exact_subject",
        chapter_status=status,  # type: ignore[arg-type]
        source_subject_name=source_subject_name,
        source_chapter_name=source_chapter_name,
        kg_subject_name=source_subject_name,
        reason=decision.reason,
        confidence=decision.confidence,
    )
    return route, None, _review_item(record, status, decision.reason, {"decision": decision.model_dump(mode="json")})


def _build_link_from_match(
    *,
    route: RouteInfo,
    match: CoreMatchLLMDecision,
    selected_core: KGCoreNode,
    verifier: CoreVerifierDecision | None,
    status: str,
) -> KGCoreLinking:
    return KGCoreLinking(
        status=status,  # type: ignore[arg-type]
        primary_core_uid=selected_core.kg_core_uid,
        primary_core_name=selected_core.canonical_name,
        primary_core_type=selected_core.type,
        kg_subject_name=selected_core.kg_subject_name,
        kg_chapter_title=selected_core.chapter_title,
        kg_section_title=selected_core.section_title,
        match_type=match.match_type,
        confidence=match.confidence,
        route=route,
        bridge_nodes=match.bridge_nodes,
        matched_knowledge_node=match.matched_node,
        matched_core_uids=match.matched_core_uids or [selected_core.kg_core_uid],
        rationale=match.rationale,
        verifier=verifier,
        question_evidence_terms=match.question_evidence_terms,
        kg_evidence_terms=match.kg_evidence_terms,
        rejected_candidates=match.rejected_candidates,
    )


def _matched_node_exists(match: CoreMatchLLMDecision, selected_core: KGCoreNode) -> tuple[bool, str]:
    node = match.matched_node
    if node is None:
        return False, "match decision did not include matched_node"
    if node.parent_core_uid != selected_core.kg_core_uid:
        return False, "matched_node.parent_core_uid does not equal primary_core_uid"
    if node.node_kind == "core_node":
        if node.client_ref != selected_core.client_ref:
            return False, "matched core_node client_ref does not equal selected core client_ref"
        if node.parent_link_strength != "direct":
            return False, "core_node match must use direct parent_link_strength"
        return True, "matched core_node is valid"
    if node.node_kind == "support_node":
        support_refs = {support.client_ref for support in selected_core.support_nodes}
        if node.client_ref not in support_refs:
            return False, "matched support_node is not listed under selected core"
        if node.parent_link_strength not in {"edge", "strong_related"}:
            return False, "support_node match requires edge or strong_related parent_link_strength"
        return True, "matched support_node is valid"
    if node.node_kind == "concept_node":
        concept_refs = {concept.client_ref for concept in selected_core.concept_nodes}
        if node.client_ref not in concept_refs:
            return False, "matched concept_node is not listed under selected core"
        if node.parent_link_strength == "invalid":
            return False, "concept_node parent_link_strength is invalid"
        return True, "matched concept_node is present under selected core"
    return False, "unsupported matched_node.node_kind"


def _link_one_record(
    *,
    record: dict[str, Any],
    catalog: list[KGCoreNode],
    subject_chapters: dict[str, list[ChapterCandidate]],
    intermediate_dir: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    linked = dict(record)
    source_subject_name = str(record.get("subject_name") or "")
    source_chapter_name = str(record.get("chapter_name") or "")

    if source_subject_name not in subject_chapters:
        route = RouteInfo(
            subject_status="missing_kg_subject",
            chapter_status="skipped_no_kg_subject",
            source_subject_name=source_subject_name,
            source_chapter_name=source_chapter_name,
            reason="source subject_name has no same-name directory in KG manual drafts",
        )
        link = _route_failure_link(
            record,
            status="skipped_no_kg_subject",
            route=route,
            rationale="Skipped because the workflow is configured not to match subjects without same-name KG subject coverage.",
        )
        linked["kg_core_linking"] = link.model_dump(mode="json")
        return linked, _review_item(record, "skipped_no_kg_subject", link.rationale)

    route, chapter, route_review = _resolve_chapter(
        record=record,
        candidates=subject_chapters[source_subject_name],
        intermediate_dir=intermediate_dir,
    )
    if chapter is None:
        status = route.chapter_status
        link = _route_failure_link(
            record,
            status=status,
            route=route,
            rationale=route.reason,
        )
        linked["kg_core_linking"] = link.model_dump(mode="json")
        return linked, route_review or _review_item(record, status, route.reason)

    candidate_cores = [
        core
        for core in catalog
        if core.kg_subject_name == source_subject_name and core.draft_id == chapter.draft_id
    ]
    if not candidate_cores:
        link = _route_failure_link(
            record,
            status="no_core_match",
            route=route,
            rationale="Resolved chapter has no KG core candidates.",
        )
        linked["kg_core_linking"] = link.model_dump(mode="json")
        return linked, _review_item(record, "no_core_match", link.rationale)

    candidate_payload = compact_core_payload(candidate_cores)
    prompt = core_match_prompt(record=record, candidate_cores=candidate_payload)
    try:
        match = _cached_structured_completion(
            cache_path=_cache_path(
                intermediate_dir,
                "node_match",
                _cache_key(
                    {
                        "schema_version": "v3-support-parent-match",
                        "question_id": record.get("id"),
                        "route": route.model_dump(mode="json"),
                        "candidate_core_uids": [core.kg_core_uid for core in candidate_cores],
                    }
                ),
            ),
            user_prompt=prompt,
            response_model=CoreMatchLLMDecision,
            max_tokens=6000,
        )
    except (QwenConfigurationError, QwenRequestError, QwenSchemaError) as exc:
        route.chapter_status = "llm_error"
        link = _route_failure_link(
            record,
            status="llm_error",
            route=route,
            rationale=str(exc),
        )
        linked["kg_core_linking"] = link.model_dump(mode="json")
        return linked, _review_item(record, "llm_error", str(exc), {"stage": "node_match"})

    if match.decision != "match":
        status = "core_ambiguous" if match.decision == "ambiguous" else "no_core_match"
        link = KGCoreLinking(
            status=status,  # type: ignore[arg-type]
            route=route,
            confidence=match.confidence,
            rationale=match.rationale,
            matched_core_uids=match.matched_core_uids,
            bridge_nodes=match.bridge_nodes,
            matched_knowledge_node=match.matched_node,
            question_evidence_terms=match.question_evidence_terms,
            kg_evidence_terms=match.kg_evidence_terms,
            rejected_candidates=match.rejected_candidates,
        )
        linked["kg_core_linking"] = link.model_dump(mode="json")
        return linked, _review_item(record, status, match.rationale, {"match": match.model_dump(mode="json")})

    selected_core = next((core for core in candidate_cores if core.kg_core_uid == match.primary_core_uid), None)
    if selected_core is None:
        link = KGCoreLinking(
            status="review_required",
            route=route,
            confidence=match.confidence,
            rationale="Qwen selected primary_core_uid outside candidate cores.",
            matched_core_uids=match.matched_core_uids,
            bridge_nodes=match.bridge_nodes,
            matched_knowledge_node=match.matched_node,
            question_evidence_terms=match.question_evidence_terms,
            kg_evidence_terms=match.kg_evidence_terms,
            rejected_candidates=match.rejected_candidates,
        )
        linked["kg_core_linking"] = link.model_dump(mode="json")
        return linked, _review_item(
            record,
            "review_required",
            link.rationale,
            {"match": match.model_dump(mode="json")},
        )

    node_valid, node_reason = _matched_node_exists(match, selected_core)
    if not node_valid:
        link = _build_link_from_match(
            route=route,
            match=match,
            selected_core=selected_core,
            verifier=None,
            status="review_required",
        )
        link.rationale = node_reason
        linked["kg_core_linking"] = link.model_dump(mode="json")
        return linked, _review_item(
            record,
            "review_required",
            node_reason,
            {"match": match.model_dump(mode="json")},
        )

    provisional_link = _build_link_from_match(
        route=route,
        match=match,
        selected_core=selected_core,
        verifier=None,
        status="matched",
    )
    verifier_prompt_text = verifier_prompt(
        record=record,
        kg_core_linking=provisional_link.model_dump(mode="json"),
        selected_core=compact_core_payload([selected_core])[0],
    )
    try:
        verifier = _cached_structured_completion(
            cache_path=_cache_path(
                intermediate_dir,
                "node_verify",
                _cache_key(
                    {
                        "schema_version": "v3-support-parent-verify",
                        "question_id": record.get("id"),
                        "primary_core_uid": selected_core.kg_core_uid,
                        "match": match.model_dump(mode="json"),
                    }
                ),
            ),
            user_prompt=verifier_prompt_text,
            response_model=CoreVerifierDecision,
            max_tokens=2500,
        )
    except (QwenConfigurationError, QwenRequestError, QwenSchemaError) as exc:
        link = _build_link_from_match(
            route=route,
            match=match,
            selected_core=selected_core,
            verifier=None,
            status="llm_error",
        )
        link.rationale = str(exc)
        linked["kg_core_linking"] = link.model_dump(mode="json")
        return linked, _review_item(record, "llm_error", str(exc), {"stage": "node_verify"})

    final_status = "matched" if verifier.decision == "accept" else "review_required"
    link = _build_link_from_match(
        route=route,
        match=match,
        selected_core=selected_core,
        verifier=verifier,
        status=final_status,
    )
    linked["kg_core_linking"] = link.model_dump(mode="json")
    review = None
    if final_status != "matched":
        review = _review_item(
            record,
            "review_required",
            "; ".join(verifier.reasons) or "Verifier did not accept the match.",
            {"verifier": verifier.model_dump(mode="json")},
        )
    return linked, review


def load_inputs(state: CoreLinkerState) -> CoreLinkerState:
    question_bank_path = Path(state["question_bank_path"])
    data = json.loads(question_bank_path.read_text(encoding="utf-8"))
    records = list(data.get("records") or [])
    book_name = state.get("book_name")
    if book_name:
        records = [record for record in records if record.get("subject_name") == book_name]
    sample_stride = state.get("sample_stride")
    if sample_stride is not None and int(sample_stride) > 1:
        stride = int(sample_stride)
        records = [record for index, record in enumerate(records) if index % stride == 0]
    limit = state.get("limit_records")
    if limit is not None:
        records = records[: max(0, int(limit))]
    return {
        "records": records,
        "source_meta": {
            "source": data.get("source"),
            "generated_at": data.get("generated_at"),
            "input_total_questions": data.get("total_questions"),
            "question_bank_path": str(question_bank_path),
            "book_name": book_name,
            "sample_stride": sample_stride,
        },
    }


def build_catalog(state: CoreLinkerState) -> CoreLinkerState:
    catalog = load_kg_catalog(Path(state["kg_draft_dir"]))
    return {"kg_catalog": [core.model_dump(mode="json") for core in catalog]}


def link_records(state: CoreLinkerState) -> CoreLinkerState:
    catalog = _catalog_from_state(state)
    records = state.get("records", [])
    subject_chapters = {
        subject: chapter_candidates_for_subject(catalog, subject)
        for subject in sorted({core.kg_subject_name for core in catalog})
    }

    max_workers = max(1, int(state.get("max_workers", 1) or 1))

    def worker(record: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
        return _link_one_record(
            record=record,
            catalog=catalog,
            subject_chapters=subject_chapters,
            intermediate_dir=state["intermediate_dir"],
        )

    if max_workers == 1 or len(records) <= 1:
        results = [worker(record) for record in records]
    else:
        results = [None] * len(records)  # type: ignore[var-annotated]
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {executor.submit(worker, record): index for index, record in enumerate(records)}
            for future in as_completed(future_map):
                results[future_map[future]] = future.result()

    linked_records: list[dict[str, Any]] = []
    review_required: list[dict[str, Any]] = []
    for linked, review in results:
        linked_records.append(linked)
        if review is not None:
            review_required.append(review)
    return {"linked_records": linked_records, "review_required": review_required}


def export_outputs(state: CoreLinkerState) -> CoreLinkerState:
    output_dir = Path(state["output_dir"])
    review_dir = Path(state["review_dir"])
    reports_dir = Path(state["reports_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    review_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    records = state.get("linked_records", [])
    meta = state.get("source_meta", {})
    bank = LinkedQuestionBank(
        source=meta.get("source"),
        total_questions=len(records),
        records=records,
    )
    write_json(output_dir / "question_bank_with_kg_core.json", bank)
    write_jsonl(output_dir / "question_bank_with_kg_core.jsonl", records)

    by_core: dict[str, dict[str, Any]] = {}
    by_status: Counter[str] = Counter()
    by_subject: Counter[str] = Counter()
    for record in records:
        link = record.get("kg_core_linking") or {}
        status = str(link.get("status") or "missing")
        by_status[status] += 1
        by_subject[str(record.get("subject_name") or "")] += 1
        core_uid = link.get("primary_core_uid")
        if status == "matched" and core_uid:
            bucket = by_core.setdefault(
                str(core_uid),
                {
                    "primary_core_uid": core_uid,
                    "primary_core_name": link.get("primary_core_name"),
                    "primary_core_type": link.get("primary_core_type"),
                    "kg_subject_name": link.get("kg_subject_name"),
                    "kg_chapter_title": link.get("kg_chapter_title"),
                    "question_ids": [],
                    "count": 0,
                },
            )
            bucket["question_ids"].append(record.get("id"))
            bucket["count"] += 1
    write_json(output_dir / "index_by_kg_core.json", by_core)
    write_jsonl(review_dir / "core_linking_review_required.jsonl", state.get("review_required", []))

    summary = {
        "input_question_bank": meta.get("question_bank_path"),
        "input_total_questions": meta.get("input_total_questions"),
        "processed_questions": len(records),
        "kg_core_count": len(state.get("kg_catalog", [])),
        "status_counts": dict(sorted(by_status.items())),
        "subject_counts": dict(sorted(by_subject.items())),
        "review_required": len(state.get("review_required", [])),
        "outputs": {
            "question_bank_with_kg_core": str(output_dir / "question_bank_with_kg_core.json"),
            "jsonl": str(output_dir / "question_bank_with_kg_core.jsonl"),
            "index_by_kg_core": str(output_dir / "index_by_kg_core.json"),
            "review_required": str(review_dir / "core_linking_review_required.jsonl"),
        },
    }
    write_json(reports_dir / "summary.json", summary)
    return {"summary": summary}


def build_core_linker_graph():
    graph = StateGraph(CoreLinkerState)
    graph.add_node("load_inputs", load_inputs)
    graph.add_node("build_catalog", build_catalog)
    graph.add_node("link_records", link_records)
    graph.add_node("export_outputs", export_outputs)

    graph.add_edge(START, "load_inputs")
    graph.add_edge("load_inputs", "build_catalog")
    graph.add_edge("build_catalog", "link_records")
    graph.add_edge("link_records", "export_outputs")
    graph.add_edge("export_outputs", END)
    return graph.compile(name="question_core_linker_graph")


def run_linking(
    *,
    question_bank_path: Path,
    kg_draft_dir: Path,
    output_dir: Path,
    review_dir: Path,
    reports_dir: Path,
    intermediate_dir: Path,
    book_name: str | None = None,
    limit_records: int | None = None,
    sample_stride: int | None = None,
    max_workers: int = 1,
) -> CoreLinkerState:
    graph = build_core_linker_graph()
    initial_state: CoreLinkerState = {
        "question_bank_path": str(question_bank_path),
        "kg_draft_dir": str(kg_draft_dir),
        "output_dir": str(output_dir),
        "review_dir": str(review_dir),
        "reports_dir": str(reports_dir),
        "intermediate_dir": str(intermediate_dir),
        "book_name": book_name,
        "limit_records": limit_records,
        "sample_stride": sample_stride,
        "max_workers": max_workers,
    }
    return graph.invoke(initial_state)
