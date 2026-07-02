from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any, Iterable

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


DEFAULT_KG_ROOT = PROJECT_ROOT / "data" / "core" / "manual-kg-drafts"
DEFAULT_KB_ROOT = PROJECT_ROOT / "data" / "processed" / "knowledge_base"
DEFAULT_QUESTION_BANK = Path(
    "D:/xwechat_files/wxid_moff5l81dmi122_124f/msg/file/2026-06/question_kg_matches.full.json"
)
DEFAULT_OUT_DIR = PROJECT_ROOT / "data" / "kb" / "canonical"
SOURCE_POLICY_PATH = PROJECT_ROOT / "configs" / "kb" / "source_policy.yaml"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def stable_id(prefix: str, *parts: Any, length: int = 24) -> str:
    payload = "\0".join(str(part or "") for part in parts)
    digest = hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()[:length]
    return f"{prefix}:{digest}"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at {path}:{line_no}: {exc}") from exc
            if isinstance(item, dict):
                yield item


def write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            fh.write("\n")
            count += 1
    return count


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def rel_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def load_source_policy(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Source policy not found: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def authority(policy: dict[str, Any], source_layer: str) -> int:
    try:
        return int(((policy.get("layers") or {}).get(source_layer) or {}).get("authority_level") or 0)
    except (TypeError, ValueError):
        return 0


def source_document_id(source_layer: str, source_type: str, name: str, path: str) -> str:
    return stable_id("source", source_layer, source_type, name, path)


def make_source_record(
    policy: dict[str, Any],
    source_layer: str,
    source_type: str,
    name: str,
    path: str,
    title: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "source_document_id": source_document_id(source_layer, source_type, name, path),
        "source_layer": source_layer,
        "source_type": source_type,
        "name": name,
        "title": title or name,
        "path": path,
        "authority_level": authority(policy, source_layer),
        "verification_status": "source_loaded",
        "metadata": metadata or {},
    }


def draft_files(kg_root: Path) -> list[Path]:
    return sorted(kg_root.rglob("*.kg-draft.json"))


def kg_source_subject(path: Path, kg_root: Path, source_structure: dict[str, Any]) -> str:
    try:
        relative = path.relative_to(kg_root)
        if len(relative.parts) > 1:
            return relative.parts[0]
    except ValueError:
        pass
    return text(source_structure.get("book_title"), "未命名学科")


def kg_node_id(subject: str, draft_id: str, client_ref: str) -> str:
    return f"kg://{subject}/{draft_id}/{client_ref}"


def concept_node_id(subject: str, draft_id: str, client_ref: str) -> str:
    return kg_node_id(subject, draft_id, client_ref)


def normalize_kg(kg_root: Path, policy: dict[str, Any]) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, dict[str, str]],
    dict[str, dict[str, Any]],
]:
    sources: dict[str, dict[str, Any]] = {}
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    draft_ref_maps: dict[str, dict[str, str]] = {}
    draft_meta_by_key: dict[str, dict[str, Any]] = {}
    seen_node_ids: set[str] = set()
    seen_edge_ids: set[str] = set()

    for path in draft_files(kg_root):
        draft = read_json(path)
        source_structure = draft.get("source_structure") if isinstance(draft.get("source_structure"), dict) else {}
        draft_metadata = draft.get("draft_metadata") if isinstance(draft.get("draft_metadata"), dict) else {}
        subject = kg_source_subject(path, kg_root, source_structure)
        book_title = text(source_structure.get("book_title"), subject)
        draft_id = text(draft_metadata.get("draft_id"), path.name.replace(".kg-draft.json", ""))
        document_id = text(source_structure.get("document_id"))
        version_id = text(source_structure.get("version_id"))
        chapter = text(source_structure.get("chapter_title"), "未命名章节")
        section = text(source_structure.get("section_title"))
        draft_rel = rel_path(path)
        source_id = source_document_id("textbook_kg", "kg_draft", book_title, document_id or draft_rel)
        sources[source_id] = {
            "source_document_id": source_id,
            "source_layer": "textbook_kg",
            "source_type": "kg_draft",
            "name": book_title,
            "title": f"{book_title} {chapter}".strip(),
            "path": draft_rel,
            "authority_level": authority(policy, "textbook_kg"),
            "verification_status": "source_loaded",
            "metadata": {
                "document_id": document_id,
                "version_id": version_id,
                "draft_id": draft_id,
                "chapter": chapter,
                "section": section,
                "draft_metadata": draft_metadata,
                "review_summary": draft.get("review_summary") or {},
            },
        }

        draft_key = draft_rel.replace("\\", "/")
        ref_map: dict[str, str] = {}
        draft_ref_maps[draft_key] = ref_map
        draft_meta_by_key[draft_key] = {
            "source_document_id": source_id,
            "subject": subject,
            "book_title": book_title,
            "chapter": chapter,
            "section": section,
            "draft_file": draft_rel,
            "draft_id": draft_id,
            "document_id": document_id,
            "version_id": version_id,
        }

        for core in as_list(draft.get("core_nodes")):
            if not isinstance(core, dict):
                continue
            client_ref = text(core.get("client_ref"))
            node_type = text(core.get("type"), "Core")
            canonical_name = text(core.get("canonical_name"))
            if not client_ref or not canonical_name:
                continue
            core_source = core.get("source_structure") if isinstance(core.get("source_structure"), dict) else {}
            node_chapter = text(core_source.get("chapter_title"), chapter)
            node_section = text(core_source.get("section_title"), section)
            node_id = kg_node_id(subject, draft_id, client_ref)
            ref_map[client_ref] = node_id
            if node_id in seen_node_ids:
                continue
            seen_node_ids.add(node_id)
            nodes.append(
                {
                    "node_id": node_id,
                    "node_kind": "core",
                    "node_type": node_type,
                    "canonical_name": canonical_name,
                    "aliases": [text(item) for item in as_list(core.get("aliases")) if text(item)],
                    "source_layer": "textbook_kg",
                    "source_type": "kg_draft",
                    "subject": subject,
                    "book_title": book_title,
                    "chapter": node_chapter,
                    "section": node_section,
                    "source_document_id": source_id,
                    "draft_file": draft_rel,
                    "source_span_ids": [text(item) for item in as_list(core.get("source_span_ids")) if text(item)],
                    "verification_status": "reviewed_kg_node",
                    "metadata": {
                        "client_ref": client_ref,
                        "draft_id": draft_id,
                        "document_id": document_id,
                        "version_id": version_id,
                        "raw_source_structure": core_source,
                    },
                }
            )

        for support in as_list(draft.get("support_nodes")):
            if not isinstance(support, dict):
                continue
            client_ref = text(support.get("client_ref"))
            node_type = text(support.get("type"), "Support")
            canonical_name = text(support.get("canonical_name"))
            if not client_ref or not canonical_name:
                continue
            node_id = kg_node_id(subject, draft_id, client_ref)
            ref_map[client_ref] = node_id
            if node_id in seen_node_ids:
                continue
            seen_node_ids.add(node_id)
            nodes.append(
                {
                    "node_id": node_id,
                    "node_kind": "support",
                    "node_type": node_type,
                    "canonical_name": canonical_name,
                    "aliases": [text(item) for item in as_list(support.get("aliases")) if text(item)],
                    "source_layer": "textbook_kg",
                    "source_type": "kg_draft",
                    "subject": subject,
                    "book_title": book_title,
                    "chapter": chapter,
                    "section": section,
                    "source_document_id": source_id,
                    "draft_file": draft_rel,
                    "source_span_ids": [text(item) for item in as_list(support.get("source_span_ids")) if text(item)],
                    "verification_status": "reviewed_kg_node",
                    "metadata": {
                        "client_ref": client_ref,
                        "draft_id": draft_id,
                        "document_id": document_id,
                        "version_id": version_id,
                    },
                }
            )

        for concept in as_list(draft.get("concepts")):
            if not isinstance(concept, dict):
                continue
            client_ref = text(concept.get("client_ref"))
            canonical_name = text(concept.get("canonical_name") or concept.get("name"))
            if not client_ref or not canonical_name:
                continue
            node_id = concept_node_id(subject, draft_id, client_ref)
            ref_map[client_ref] = node_id
            if node_id in seen_node_ids:
                continue
            seen_node_ids.add(node_id)
            nodes.append(
                {
                    "node_id": node_id,
                    "node_kind": "concept",
                    "node_type": "Concept",
                    "canonical_name": canonical_name,
                    "aliases": [],
                    "source_layer": "textbook_kg",
                    "source_type": "kg_draft",
                    "subject": subject,
                    "book_title": book_title,
                    "chapter": chapter,
                    "section": section,
                    "source_document_id": source_id,
                    "draft_file": draft_rel,
                    "source_span_ids": [],
                    "verification_status": "reviewed_kg_concept",
                    "metadata": {
                        "client_ref": client_ref,
                        "draft_id": draft_id,
                        "relation": concept.get("relation"),
                        "related_node_refs": as_list(concept.get("related_node_refs")),
                        "reason_not_edge": text(concept.get("reason_not_edge")),
                        "evidence": as_list(concept.get("evidence")),
                    },
                }
            )

        for edge in as_list(draft.get("edges")):
            edge_index = len([key for key in ref_map if key.startswith("edge_")]) + 1
            if not isinstance(edge, dict):
                continue
            subject_ref = text(edge.get("subject_ref"))
            object_ref = text(edge.get("object_ref"))
            source_node_id = ref_map.get(subject_ref)
            target_node_id = ref_map.get(object_ref)
            if not source_node_id or not target_node_id:
                continue
            predicate = text(edge.get("predicate"), "RELATED_TO")
            payload = json.dumps(
                {
                    "source": source_node_id,
                    "target": target_node_id,
                    "predicate": predicate,
                    "evidence": edge.get("evidence"),
                    "draft": draft_rel,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            edge_id = stable_id("edge", payload)
            if edge_id in seen_edge_ids:
                continue
            seen_edge_ids.add(edge_id)
            ref_map[f"edge_{edge_index}:{subject_ref}-{predicate}-{object_ref}"] = edge_id
            ref_map[f"{subject_ref}-{predicate}-{object_ref}"] = edge_id
            edges.append(
                {
                    "edge_id": edge_id,
                    "edge_kind": "kg_relation",
                    "source_node_id": source_node_id,
                    "target_node_id": target_node_id,
                    "predicate": predicate,
                    "support_type": edge.get("support_type"),
                    "importance": edge.get("importance"),
                    "evidence": as_list(edge.get("evidence")),
                    "source_layer": "textbook_kg",
                    "source_document_id": source_id,
                    "draft_file": draft_rel,
                    "verification_status": "reviewed_kg_edge",
                    "metadata": {
                        "subject_ref": subject_ref,
                        "object_ref": object_ref,
                        "document_id": document_id,
                        "version_id": version_id,
                    },
                }
            )

    return list(sources.values()), nodes, edges, draft_ref_maps, draft_meta_by_key


CHUNK_SOURCES = [
    ("oral_textbooks.jsonl", "textbook_evidence", "textbook"),
    ("clinical_norms.jsonl", "clinical_authority", "guideline"),
    ("merck_manual.jsonl", "external_reference", "general_reference"),
]


def normalize_page(value: Any) -> Any:
    if value in ("", None):
        return None
    return value


def chunk_layer_for_source_type(source_type: str) -> str:
    if source_type == "textbook":
        return "textbook_evidence"
    if source_type in {"guideline", "clinical_norm"}:
        return "clinical_authority"
    if source_type == "general_reference":
        return "external_reference"
    return "external_reference"


def normalize_chunks(kb_root: Path, policy: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    sources: dict[str, dict[str, Any]] = {}
    chunks: list[dict[str, Any]] = []
    seen_chunk_ids: set[str] = set()

    for filename, configured_layer, configured_type in CHUNK_SOURCES:
        path = kb_root / filename
        if not path.exists():
            continue
        for record in iter_jsonl(path):
            source_type = text(record.get("source_type"), configured_type)
            source_layer = chunk_layer_for_source_type(source_type) or configured_layer
            source = text(record.get("source"), path.stem)
            record_path = text(record.get("path"))
            source_path = record_path or rel_path(path)
            source_id = source_document_id(source_layer, source_type, source, source_path)
            if source_id not in sources:
                sources[source_id] = make_source_record(
                    policy,
                    source_layer,
                    source_type,
                    source,
                    source_path,
                    title=source,
                    metadata={"jsonl_path": rel_path(path)},
                )
            metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
            metadata = dict(metadata)
            source_chunk_id = text(record.get("id") or record.get("chunk_id"))
            if not source_chunk_id:
                source_chunk_id = stable_id("source_chunk", filename, len(chunks))
            chunk_id = stable_id("chunk", source_layer, source_type, source_chunk_id)
            if chunk_id in seen_chunk_ids:
                continue
            seen_chunk_ids.add(chunk_id)
            page = normalize_page(record.get("page", metadata.get("page")))
            chapter = text(record.get("chapter", metadata.get("chapter")))
            section = text(record.get("section", metadata.get("section")))
            content_type = record.get("content_type", metadata.get("content_type"))
            entities = as_list(record.get("entities", metadata.get("entities")))
            quality_score = record.get("quality_score", metadata.get("quality_score"))
            chunks.append(
                {
                    "chunk_id": chunk_id,
                    "source_chunk_id": source_chunk_id,
                    "source_layer": source_layer,
                    "source_type": source_type,
                    "source_document_id": source_id,
                    "source": source,
                    "title": text(record.get("title")),
                    "text": text(record.get("text")),
                    "path": source_path,
                    "page": page,
                    "chapter": chapter,
                    "section": section,
                    "content_type": content_type,
                    "entities": [item for item in entities if item not in ("", None)],
                    "quality_score": quality_score,
                    "authority_level": authority(policy, source_layer),
                    "verification_status": "source_loaded",
                    "metadata": {
                        **metadata,
                        "jsonl_file": filename,
                        "original_parent_id": record.get("parent_id"),
                    },
                }
            )

    return list(sources.values()), chunks


def question_status(match_status: str) -> str:
    if match_status in {"matched", "weak_match"}:
        return "linked_from_question_bank"
    return "unmatched_from_question_bank"


def question_link_status(match_status: str) -> str:
    if match_status in {"matched", "weak_match"}:
        return "linked"
    return "unmatched"


def kg_file_to_draft_key(kg_file: str) -> str:
    value = kg_file.replace("\\", "/").strip("/")
    if value.startswith("data/core/"):
        return value
    if value.startswith("manual-kg-drafts/"):
        return f"data/core/{value}"
    return value


def link_for_ref(
    question_id: str,
    draft_key: str,
    node_ref: str,
    relation: str,
    match_status: str,
    method: str,
    payload: dict[str, Any],
    draft_ref_maps: dict[str, dict[str, str]],
) -> dict[str, Any] | None:
    target_id = draft_ref_maps.get(draft_key, {}).get(node_ref)
    if not target_id and ":" in node_ref:
        edge_ref_tail = node_ref.split(":", 1)[1]
        target_id = draft_ref_maps.get(draft_key, {}).get(edge_ref_tail)
    if not target_id:
        return None
    confidence = text(payload.get("confidence"))
    score = payload.get("score")
    alignment_id = stable_id("qlink", question_id, target_id, relation, method, node_ref)
    return {
        "alignment_id": alignment_id,
        "from_id": question_id,
        "to_id": target_id,
        "relation": relation,
        "score": score,
        "method_chain": [method],
        "evidence_quote": text(payload.get("evidence_quote")),
        "verification_status": question_link_status(match_status),
        "metadata": {
            "original_match_status": match_status,
            "confidence": confidence,
            "node_ref": node_ref,
            "canonical_name": payload.get("canonical_name"),
            "match_level": payload.get("match_level"),
            "matched_terms": as_list(payload.get("matched_terms")),
            "reason": payload.get("reason"),
            "score_parts": payload.get("score_parts"),
            "evidence_explanation": payload.get("evidence_explanation"),
            "draft_key": draft_key,
        },
    }


def normalize_questions(
    question_bank_path: Path,
    policy: dict[str, Any],
    draft_ref_maps: dict[str, dict[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    if not question_bank_path.exists():
        raise FileNotFoundError(f"Question bank not found: {question_bank_path}")

    data = read_json(question_bank_path)
    records = data.get("records") if isinstance(data, dict) else None
    if not isinstance(records, list):
        raise ValueError(f"Question bank has no records list: {question_bank_path}")

    source_name = text(data.get("source_question_bank"), "question_kg_matches.full.json")
    source_id = source_document_id("assessment", "question_bank", source_name, question_bank_path.as_posix())
    source = make_source_record(
        policy,
        "assessment",
        "question_bank",
        source_name,
        question_bank_path.as_posix(),
        title=source_name,
        metadata={
            "schema_version": data.get("schema_version"),
            "kg_root": data.get("kg_root"),
            "generated_at": data.get("generated_at"),
            "matching_method": data.get("matching_method"),
            "total_questions": data.get("total_questions"),
            "matched_questions": data.get("matched_questions"),
            "weak_match_questions": data.get("weak_match_questions"),
            "unmatched_questions": data.get("unmatched_questions"),
        },
    )

    questions: list[dict[str, Any]] = []
    question_links: list[dict[str, Any]] = []
    seen_links: set[str] = set()
    missing_ref_counts: Counter[str] = Counter()

    for record in records:
        if not isinstance(record, dict):
            continue
        source_question_id = text(record.get("question_id"))
        if not source_question_id:
            continue
        question_id = stable_id("question", source_question_id)
        match_status = text(record.get("match_status"), "unmatched")
        chapter_match = record.get("kg_chapter_match") if isinstance(record.get("kg_chapter_match"), dict) else {}
        kg_file = text(chapter_match.get("kg_file"))
        draft_key = kg_file_to_draft_key(kg_file) if kg_file else ""
        primary = record.get("primary_kg_match") if isinstance(record.get("primary_kg_match"), dict) else {}
        primary_node_ref = text(primary.get("node_ref"))
        primary_node_id = None
        if draft_key and primary_node_ref:
            primary_node_id = draft_ref_maps.get(draft_key, {}).get(primary_node_ref)
            if not primary_node_id:
                missing_ref_counts[f"{draft_key}:{primary_node_ref}"] += 1

        questions.append(
            {
                "question_id": question_id,
                "source_question_id": source_question_id,
                "source_layer": "assessment",
                "source_type": "question_bank",
                "source_document_id": source_id,
                "subject": text(record.get("subject_name")),
                "chapter": text(record.get("chapter_name")),
                "stem": text(record.get("stem")),
                "answer": record.get("answer"),
                "primary_knowledge_point": text(record.get("primary_knowledge_point")),
                "match_status": match_status,
                "provided_primary_kg_node_id": primary_node_id,
                "provided_matches": {
                    "kg_chapter_match": chapter_match,
                    "primary_kg_match": primary,
                    "fine_grained_matches": as_list(record.get("fine_grained_matches")),
                },
                "verification_status": question_status(match_status),
                "metadata": {
                    "local_question_no": record.get("local_question_no"),
                    "source_exam_id": record.get("source_exam_id"),
                    "kg_file": kg_file,
                    "draft_key": draft_key,
                    "primary_node_ref": primary_node_ref,
                    "primary_confidence": primary.get("confidence"),
                    "primary_score": primary.get("score"),
                },
            }
        )

        if match_status not in {"matched", "weak_match"} or not draft_key:
            continue

        if primary_node_ref:
            link = link_for_ref(
                question_id,
                draft_key,
                primary_node_ref,
                "examines_primary",
                match_status,
                "question_bank_primary_match",
                primary,
                draft_ref_maps,
            )
            if link and link["alignment_id"] not in seen_links:
                question_links.append(link)
                seen_links.add(link["alignment_id"])

        for fine in as_list(record.get("fine_grained_matches")):
            if not isinstance(fine, dict):
                continue
            ref = text(fine.get("ref"))
            if ref:
                link = link_for_ref(
                    question_id,
                    draft_key,
                    ref,
                    "examines",
                    match_status,
                    f"question_bank_fine_{text(fine.get('match_level'), 'match')}",
                    fine,
                    draft_ref_maps,
                )
                if link and link["alignment_id"] not in seen_links:
                    question_links.append(link)
                    seen_links.add(link["alignment_id"])
                elif not link:
                    missing_ref_counts[f"{draft_key}:{ref}"] += 1
            for related_ref in as_list(fine.get("related_node_refs")):
                related_ref_text = text(related_ref)
                if not related_ref_text:
                    continue
                link = link_for_ref(
                    question_id,
                    draft_key,
                    related_ref_text,
                    "examines_related",
                    match_status,
                    "question_bank_fine_related_node",
                    fine,
                    draft_ref_maps,
                )
                if link and link["alignment_id"] not in seen_links:
                    question_links.append(link)
                    seen_links.add(link["alignment_id"])
                elif not link:
                    missing_ref_counts[f"{draft_key}:{related_ref_text}"] += 1

    diagnostics = {
        "question_bank_declared_counts": {
            "total_questions": data.get("total_questions"),
            "matched_questions": data.get("matched_questions"),
            "weak_match_questions": data.get("weak_match_questions"),
            "unmatched_questions": data.get("unmatched_questions"),
        },
        "match_status_counts": dict(Counter(item["match_status"] for item in questions)),
        "question_link_count": len(question_links),
        "missing_ref_examples": dict(missing_ref_counts.most_common(25)),
    }
    return [source], questions, question_links, diagnostics


def dedupe_sources(sources: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for source in sources:
        by_id[source["source_document_id"]] = source
    return sorted(by_id.values(), key=lambda item: (item["source_layer"], item["name"], item["path"]))


def build(args: argparse.Namespace) -> dict[str, Any]:
    policy = load_source_policy(args.source_policy)
    kg_sources, nodes, edges, draft_ref_maps, draft_meta_by_key = normalize_kg(args.kg_root, policy)
    chunk_sources, chunks = normalize_chunks(args.knowledge_base, policy)
    question_sources, questions, question_links, question_diagnostics = normalize_questions(
        args.question_bank,
        policy,
        draft_ref_maps,
    )
    sources = dedupe_sources([*kg_sources, *chunk_sources, *question_sources])

    output_paths = {
        "sources": args.out_dir / "sources.jsonl",
        "nodes": args.out_dir / "nodes.jsonl",
        "edges": args.out_dir / "edges.jsonl",
        "chunks": args.out_dir / "chunks.jsonl",
        "questions": args.out_dir / "questions.jsonl",
        "question_links": args.out_dir / "question_links.jsonl",
        "alignments": args.out_dir / "alignments.jsonl",
        "clinical_rules": args.out_dir / "clinical_rules.jsonl",
        "manifest": args.out_dir / "manifest.json",
    }

    counts = {
        "sources": write_jsonl(output_paths["sources"], sources),
        "nodes": write_jsonl(output_paths["nodes"], nodes),
        "edges": write_jsonl(output_paths["edges"], edges),
        "chunks": write_jsonl(output_paths["chunks"], chunks),
        "questions": write_jsonl(output_paths["questions"], questions),
        "question_links": write_jsonl(output_paths["question_links"], question_links),
        "alignments": write_jsonl(output_paths["alignments"], []),
        "clinical_rules": write_jsonl(output_paths["clinical_rules"], []),
    }

    counts["nodes_by_kind"] = dict(Counter(item["node_kind"] for item in nodes))
    counts["chunks_by_layer"] = dict(Counter(item["source_layer"] for item in chunks))
    counts["questions_by_match_status"] = dict(Counter(item["match_status"] for item in questions))
    counts["question_links_by_original_match_status"] = dict(
        Counter((item.get("metadata") or {}).get("original_match_status") for item in question_links)
    )

    kg_declared = {
        "draft_files": len(draft_files(args.kg_root)),
    }
    quality_gates = {
        "schema_files_present": (PROJECT_ROOT / "configs" / "kb" / "schemas").exists(),
        "source_policy_present": args.source_policy.exists(),
        "canonical_raw_only": True,
        "no_model_generated_alignments": True,
        "no_hash_embedding_fallback": True,
        "question_weak_match_treated_as_linked": True,
        "clinical_rules_status": "not_started",
        "kg_chunk_alignment_status": "not_started",
        "index_status": "not_started",
    }

    manifest = {
        "kb_version": args.kb_version,
        "generated_at": now_iso(),
        "stage": "canonical_raw",
        "input_paths": {
            "kg_root": rel_path(args.kg_root),
            "knowledge_base": rel_path(args.knowledge_base),
            "question_bank": args.question_bank.as_posix(),
            "source_policy": rel_path(args.source_policy),
        },
        "outputs": {key: rel_path(path) for key, path in output_paths.items()},
        "counts": counts,
        "diagnostics": {
            "kg": kg_declared,
            "question_bank": question_diagnostics,
            "draft_ref_map_count": len(draft_ref_maps),
            "draft_meta_count": len(draft_meta_by_key),
        },
        "quality_gates": quality_gates,
        "notes": [
            "This stage standardizes existing sources only.",
            "alignments.jsonl and clinical_rules.jsonl are intentionally empty until verified construction stages run.",
            "matched and weak_match question records are both linked when KG refs resolve; original status and confidence are preserved.",
        ],
    }
    write_json(output_paths["manifest"], manifest)
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build canonical raw OralEdu-KB records from existing KG, chunks, and question links.")
    parser.add_argument("--kg-root", type=Path, default=DEFAULT_KG_ROOT)
    parser.add_argument("--knowledge-base", type=Path, default=DEFAULT_KB_ROOT)
    parser.add_argument("--question-bank", type=Path, default=DEFAULT_QUESTION_BANK)
    parser.add_argument("--source-policy", type=Path, default=SOURCE_POLICY_PATH)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--kb-version", default=datetime.now().strftime("%Y%m%d-canonical-raw"))
    return parser.parse_args()


def main() -> None:
    manifest = build(parse_args())
    print(json.dumps({"stage": manifest["stage"], "counts": manifest["counts"], "quality_gates": manifest["quality_gates"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
