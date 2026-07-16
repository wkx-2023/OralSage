from __future__ import annotations

import json
import math
import os
import re
from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .llm_client import DEFAULT_QWEN_BASE_URL, DEFAULT_QWEN_MODEL, is_llm_configured
from .retrieval import EvidenceChunk, rerank_chunks
from .retrieval.kag_singleton import get_kag_solver
from .retrieval.text_features import query_anchors


MAX_QUERY_ROUTES = 3
MAX_POOL_SIZE = 52
MAX_RESULTS = 24

MULTI_CHANNEL_QUOTA = 12
DENSE_QUOTA = 18
SPARSE_QUOTA = 14
PPR_QUOTA = 6

_SPACE_RE = re.compile(r"\s+")
_PUNCTUATION_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+", re.IGNORECASE)

_AUTHORITATIVE_LAYERS = {"clinical_authority", "textbook_evidence"}
_ORAL_METADATA_TERMS = (
    "口腔",
    "牙",
    "齿",
    "龈",
    "牙周",
    "牙髓",
    "根尖",
    "颌",
    "舌",
    "唇",
    "腭",
    "黏膜",
    "唾液",
    "咬合",
    "正畸",
    "种植",
    "义齿",
)
_STRONG_ORAL_BODY_TERMS = (
    "口腔",
    "牙髓",
    "牙周",
    "牙龈",
    "牙本质",
    "根尖",
    "牙槽",
    "拔牙",
    "龋",
    "智齿",
    "义齿",
    "正畸",
    "种植牙",
    "颞下颌",
    "口腔黏膜",
    "颌骨",
)


@dataclass(frozen=True)
class _CandidateRef:
    doc_id: str
    item: dict[str, Any]
    source_layer: str


def _clean_query(value: Any, *, limit: int = 180) -> str:
    query = _SPACE_RE.sub(" ", str(value or "")).strip(" \t\r\n,，。；;：:")
    return query[:limit].strip()


def _rule_normalize_query(query: str) -> str:
    normalized = _clean_query(query)
    replacements = (
        (r"(?:喝|碰到|遇到|遇|接触)(?:凉|冷)(?:水|的东西)?", "冷刺激"),
        (r"(?:酸?痛|疼)(?:一?下)?(?:后)?(?:马上|很快|一会儿)?(?:就)?(?:好|不疼|消失)(?:了)?", "一过性疼痛，刺激去除后立即消失"),
        (r"刺激(?:去掉|移开|撤掉|去除)后?(?:马上|很快)?(?:就)?(?:不疼|好了|消失)(?:了)?", "刺激去除后立即消失"),
        (r"刷牙(?:的?时候|时)?(?:老是|经常|总是)?出血", "刷牙时牙龈出血"),
        (r"牙齿周围(?:的)?沟(?:越来越|变得)?深", "牙周袋加深"),
        (r"(?:磕|撞)(?:掉|脱落)(?:了)?(?:一)?整颗牙", "牙齿全脱出"),
        (r"擦不掉", "不可擦除"),
    )
    for pattern, replacement in replacements:
        normalized = re.sub(pattern, replacement, normalized)
    normalized = re.sub(
        r"(?:请问|我想(?:查|了解)|想(?:查|了解)|可能)?(?:是)?什么情况(?:呢|啊)?$",
        "",
        normalized,
    )
    normalized = re.sub(r"[，,]{2,}", "，", normalized).strip(" ，,。；;")
    return _clean_query(normalized) or _clean_query(query)


def _rule_expanded_queries(original: str, normalized: str) -> list[str]:
    haystack = f"{original} {normalized}"
    expansions: list[str] = []
    if (
        "白" in haystack
        and ("网状" in haystack or "条纹" in haystack)
        and ("口" in haystack or "嘴" in haystack or "黏膜" in haystack)
    ):
        expansions.append("口腔扁平苔藓 典型临床表现 白色网状条纹 Wickham纹 烧灼感")
    if "牙齿全脱出" in normalized:
        expansions.append("恒牙全脱出 牙再植 现场处理 保存介质")
    return expansions[:1]


def _planner_timeout_seconds() -> float:
    raw = os.getenv("KNOWLEDGE_SEARCH_PLANNER_TIMEOUT_SECONDS")
    if not raw:
        return 4.0
    try:
        return max(1.0, min(float(raw), 8.0))
    except ValueError:
        return 4.0


def _first_env(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def _planner_messages(query: str, rule_query: str) -> list[dict[str, str]]:
    system = (
        "你是口腔医学知识库的查询规划器，只负责把用户问题改写成检索语句，不回答问题，"
        "不提供诊断、治疗建议或解释。保留部位、诱因、持续时间、刺激撤除后的变化、否定症状和检索意图。"
        "最多给出2个附加查询。只输出严格JSON，不要Markdown。"
    )
    payload = {
        "original_query": query,
        "rule_normalized_query": rule_query,
        "required_schema": {
            "normalized_query": "一条临床术语规范、但不直接下诊断结论的检索语句",
            "expanded_queries": ["最多一条互补检索语句"],
        },
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def _json_object(raw: str) -> dict[str, Any]:
    value = str(raw or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\s*```$", "", value)
    start = value.find("{")
    end = value.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("query planner did not return a JSON object")
    parsed = json.loads(value[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("query planner JSON must be an object")
    return parsed


def _planner_completion(messages: list[dict[str, str]]) -> str:
    api_key = _first_env(
        "KNOWLEDGE_SEARCH_LLM_API_KEY",
        "DECISION_LLM_API_KEY",
        "QWEN_API_KEY",
        "DASHSCOPE_API_KEY",
    )
    if not api_key:
        raise RuntimeError("knowledge search planner is not configured")
    base_url = _first_env(
        "KNOWLEDGE_SEARCH_LLM_BASE_URL",
        "DECISION_LLM_BASE_URL",
        "QWEN_BASE_URL",
        "DASHSCOPE_BASE_URL",
    ) or DEFAULT_QWEN_BASE_URL
    model = _first_env(
        "KNOWLEDGE_SEARCH_LLM_MODEL",
        "DECISION_LLM_MODEL",
        "QWEN_MODEL",
        "DASHSCOPE_MODEL",
    ) or DEFAULT_QWEN_MODEL
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.0,
        "max_tokens": 240,
        "response_format": {"type": "json_object"},
    }
    request = Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "OralPath-Rebuild/0.1",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=_planner_timeout_seconds()) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
        return str(data["choices"][0]["message"]["content"]).strip()
    except (HTTPError, URLError, TimeoutError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"knowledge search planner request failed: {exc}") from exc


def _llm_planned_queries(query: str, rule_query: str) -> list[str]:
    raw = _planner_completion(_planner_messages(query, rule_query))
    payload = _json_object(raw)
    values: list[Any] = [payload.get("normalized_query")]
    expanded = payload.get("expanded_queries")
    if isinstance(expanded, list):
        values.extend(expanded)
    return [_clean_query(value) for value in values if _clean_query(value)]


def _should_use_llm_planner(original: str, rule_query: str) -> bool:
    mode = str(os.getenv("KNOWLEDGE_SEARCH_LLM_PLANNER_MODE") or "auto").strip().lower()
    if mode in {"0", "false", "off", "disabled", "never"}:
        return False
    if mode in {"1", "true", "on", "enabled", "always"}:
        return True
    if rule_query != original:
        return False
    anchors = query_anchors(original)
    if anchors:
        return False
    return len(original) >= 12


@lru_cache(maxsize=256)
def _planned_queries_cached(query: str) -> tuple[str, ...]:
    original = _clean_query(query)
    if not original:
        return ()
    rule_query = _rule_normalize_query(original)

    llm_queries: list[str] = []
    if _should_use_llm_planner(original, rule_query) and is_llm_configured(
        "KNOWLEDGE_SEARCH_LLM_API_KEY",
        "DECISION_LLM_API_KEY",
        "QWEN_API_KEY",
        "DASHSCOPE_API_KEY",
    ):
        try:
            llm_queries = _llm_planned_queries(original, rule_query)
        except Exception:
            pass

    rule_expansions = _rule_expanded_queries(original, rule_query)
    rule_queries = rule_expansions or [rule_query]
    ordered = [original, *llm_queries, *rule_queries]
    unique: list[str] = []
    for value in ordered:
        normalized = _clean_query(value)
        if len(normalized) < 2 or normalized in unique:
            continue
        unique.append(normalized)
        if len(unique) >= MAX_QUERY_ROUTES:
            break
    return tuple(unique)


def reset_knowledge_search_plan_cache() -> None:
    _planned_queries_cached.cache_clear()


def reset_knowledge_search_retrieval_cache() -> None:
    _retrieve_cached.cache_clear()


def _doc_id(item: dict[str, Any]) -> str:
    return str(item.get("doc_id") or item.get("chunk_id") or "").strip()


def _row_for(solver: Any, doc_id: str) -> dict[str, Any]:
    chunks = getattr(solver, "chunks", {})
    row = chunks.get(doc_id, {}) if isinstance(chunks, dict) else {}
    return row if isinstance(row, dict) else {}


def _metadata_for(row: dict[str, Any]) -> dict[str, Any]:
    metadata = row.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _source_layer(item: dict[str, Any], row: dict[str, Any]) -> str:
    metadata = _metadata_for(row)
    return str(item.get("source_layer") or row.get("source_layer") or metadata.get("source_layer") or "").strip()


def _is_obviously_nonoral_external(item: dict[str, Any], row: dict[str, Any]) -> bool:
    if _source_layer(item, row) != "external_reference":
        return False
    metadata = _metadata_for(row)
    headings = row.get("heading_path") or metadata.get("heading_path") or []
    if not isinstance(headings, list):
        headings = [headings]
    metadata_text = " ".join(
        str(value or "")
        for value in (
            row.get("subject") or metadata.get("subject"),
            row.get("chapter") or metadata.get("chapter"),
            row.get("source_title") or metadata.get("source_title"),
            " ".join(str(value) for value in headings),
        )
    )
    if any(term in metadata_text for term in _ORAL_METADATA_TERMS):
        return False

    body = str(row.get("text") or "")[:1200]
    strong_hits = {term for term in _STRONG_ORAL_BODY_TERMS if term in body}
    return not strong_hits


def _is_low_utility_row(row: dict[str, Any]) -> bool:
    metadata = _metadata_for(row)
    headings = row.get("heading_path") or metadata.get("heading_path") or []
    if not isinstance(headings, list):
        headings = [headings]
    last_heading = _clean_query(headings[-1] if headings else "")
    if last_heading in {"参考文献", "主要参考文献", "目的和要求", "学习目标"}:
        return True
    opening = _SPACE_RE.sub(" ", str(row.get("text") or "")).strip()[:320]
    return bool(
        re.match(r"^(?:【)?(?:目的和要求|学习目标)(?:】)?", opening)
        or re.match(r"^(?:本章)?(?:内容)?(?:应)?(?:重点)?掌握[：:]", opening)
        or opening.startswith(">>提要")
    )


def _quality_allowed_ids(pack: dict[str, Any]) -> set[str]:
    gated = pack.get("gated_candidates")
    if not isinstance(gated, list):
        return set()
    return {_doc_id(item) for item in gated if isinstance(item, dict) and _doc_id(item)}


def _merged_by_id(pack: dict[str, Any]) -> dict[str, dict[str, Any]]:
    candidates = pack.get("candidates")
    if not isinstance(candidates, list):
        return {}
    return {
        _doc_id(item): item
        for item in candidates
        if isinstance(item, dict) and _doc_id(item)
    }


def _channel_entries(
    items: Any,
    *,
    allowed_ids: set[str],
    merged: dict[str, dict[str, Any]],
    solver: Any,
) -> list[_CandidateRef]:
    if not isinstance(items, list):
        return []
    entries: list[_CandidateRef] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        doc_id = _doc_id(item)
        if not doc_id or doc_id not in allowed_ids:
            continue
        merged_item = merged.get(doc_id, item)
        row = _row_for(solver, doc_id)
        if not str(row.get("text") or "").strip():
            continue
        if _is_low_utility_row(row):
            continue
        if _is_obviously_nonoral_external(merged_item, row):
            continue
        entries.append(
            _CandidateRef(
                doc_id=doc_id,
                item=merged_item,
                source_layer=_source_layer(merged_item, row),
            )
        )
    return entries


def _round_robin(groups: Sequence[Sequence[_CandidateRef]]) -> list[_CandidateRef]:
    output: list[_CandidateRef] = []
    width = max((len(group) for group in groups), default=0)
    for index in range(width):
        for group in groups:
            if index < len(group):
                output.append(group[index])
    return output


def _prefer_authoritative(entries: Sequence[_CandidateRef]) -> list[_CandidateRef]:
    authoritative = [entry for entry in entries if entry.source_layer in _AUTHORITATIVE_LAYERS]
    supplemental = [entry for entry in entries if entry.source_layer not in _AUTHORITATIVE_LAYERS]
    output: list[_CandidateRef] = []
    while authoritative or supplemental:
        for _ in range(3):
            if authoritative:
                output.append(authoritative.pop(0))
        if supplemental:
            output.append(supplemental.pop(0))
        if not authoritative:
            output.extend(supplemental)
            break
    return output


def _multi_channel_entries(pack: dict[str, Any], solver: Any) -> list[_CandidateRef]:
    allowed = _quality_allowed_ids(pack)
    merged = _merged_by_id(pack)
    rank_maps: dict[str, dict[str, int]] = {}
    for key, stage in (("dense", "dense_bge_m3"), ("sparse", "bm25"), ("ppr", "graph_ppr"), ("aliases", "alias")):
        values = pack.get(key)
        if isinstance(values, list):
            rank_maps[stage] = {
                _doc_id(item): rank
                for rank, item in enumerate(values, 1)
                if isinstance(item, dict) and _doc_id(item)
            }

    entries = _channel_entries(
        list(merged.values()),
        allowed_ids=allowed,
        merged=merged,
        solver=solver,
    )
    entries = [entry for entry in entries if len(set(entry.item.get("matched_by") or [])) >= 2]

    def score(entry: _CandidateRef) -> tuple[float, int]:
        matched = set(entry.item.get("matched_by") or [])
        fusion = sum(
            1.0 / (60 + rank_maps.get(stage, {}).get(entry.doc_id, 10_000))
            for stage in matched
        )
        authority_bonus = 0.004 if entry.source_layer in _AUTHORITATIVE_LAYERS else 0.0
        return fusion + authority_bonus, len(matched)

    return sorted(entries, key=score, reverse=True)


def _route_buckets(solver: Any, queries: Sequence[str]) -> dict[str, list[list[_CandidateRef]]]:
    buckets: dict[str, list[list[_CandidateRef]]] = {
        "multi": [],
        "dense": [],
        "sparse": [],
        "ppr": [],
        "all": [],
    }
    for query in queries:
        pack = solver.retrieve_candidates(
            query,
            dense_k=40,
            sparse_k=40,
            alias_k=12,
            ppr_k=50,
            doc_types={"chunk"},
        )
        if not isinstance(pack, dict):
            continue
        allowed = _quality_allowed_ids(pack)
        merged = _merged_by_id(pack)
        buckets["multi"].append(_multi_channel_entries(pack, solver))
        for bucket, pack_key in (("dense", "dense"), ("sparse", "sparse"), ("ppr", "ppr"), ("all", "candidates")):
            buckets[bucket].append(
                _channel_entries(
                    pack.get(pack_key),
                    allowed_ids=allowed,
                    merged=merged,
                    solver=solver,
                )
            )
    return buckets


def _text_fingerprint(text: str) -> str:
    return _PUNCTUATION_RE.sub("", str(text or "").lower())


def _span_fingerprint(row: dict[str, Any]) -> tuple[str, ...]:
    metadata = _metadata_for(row)
    spans = row.get("source_span_ids") or metadata.get("source_span_ids") or []
    if not isinstance(spans, list):
        return ()
    return tuple(sorted(str(value) for value in spans if str(value).strip()))


def _add_bucket(
    pool: list[_CandidateRef],
    entries: Iterable[_CandidateRef],
    *,
    quota: int,
    solver: Any,
    seen_ids: set[str],
    seen_texts: set[str],
    seen_spans: set[tuple[str, ...]],
) -> None:
    added = 0
    for entry in entries:
        if added >= quota or len(pool) >= MAX_POOL_SIZE:
            break
        if entry.doc_id in seen_ids:
            continue
        row = _row_for(solver, entry.doc_id)
        text_key = _text_fingerprint(str(row.get("text") or ""))
        span_key = _span_fingerprint(row)
        if text_key and text_key in seen_texts:
            continue
        if span_key and span_key in seen_spans:
            continue
        seen_ids.add(entry.doc_id)
        if text_key:
            seen_texts.add(text_key)
        if span_key:
            seen_spans.add(span_key)
        pool.append(entry)
        added += 1


def _balanced_pool(solver: Any, queries: Sequence[str]) -> list[_CandidateRef]:
    buckets = _route_buckets(solver, queries)
    pool: list[_CandidateRef] = []
    seen_ids: set[str] = set()
    seen_texts: set[str] = set()
    seen_spans: set[tuple[str, ...]] = set()

    selections = (
        ("multi", MULTI_CHANNEL_QUOTA),
        ("dense", DENSE_QUOTA),
        ("sparse", SPARSE_QUOTA),
        ("ppr", PPR_QUOTA),
    )
    for name, quota in selections:
        entries = _prefer_authoritative(_round_robin(buckets[name]))
        _add_bucket(
            pool,
            entries,
            quota=quota,
            solver=solver,
            seen_ids=seen_ids,
            seen_texts=seen_texts,
            seen_spans=seen_spans,
        )

    filler = _prefer_authoritative(_round_robin(buckets["all"]))
    _add_bucket(
        pool,
        filler,
        quota=MAX_POOL_SIZE - len(pool),
        solver=solver,
        seen_ids=seen_ids,
        seen_texts=seen_texts,
        seen_spans=seen_spans,
    )
    return pool


def _clean_label(value: Any, *, limit: int = 90) -> str:
    label = _SPACE_RE.sub(" ", str(value or "")).strip()
    label = re.sub(r"^[（(]已压缩[）)]\s*", "", label)
    label = re.sub(r"\s*[（(]?OCR[）)]?\s*(?:\.pdf)?$", "", label, flags=re.IGNORECASE)
    label = re.sub(r"\s*｜\s*高清全彩.*$", "", label)
    return label[:limit].strip(" -/｜")


def _candidate_to_chunk(entry: _CandidateRef, solver: Any) -> EvidenceChunk:
    row = _row_for(solver, entry.doc_id)
    metadata = _metadata_for(row)
    subject = _clean_label(row.get("subject") or metadata.get("subject"))
    chapter = _clean_label(row.get("chapter") or metadata.get("chapter"))
    headings = row.get("heading_path") or metadata.get("heading_path") or []
    if not isinstance(headings, list):
        headings = [headings]
    heading_path = [_clean_label(value) for value in headings]
    heading_path = [value for value in heading_path if value]

    title_parts: list[str] = []
    for value in (subject, chapter, *heading_path[-2:]):
        if value and value not in title_parts:
            title_parts.append(value)
    reranker_title = "｜".join(title_parts) or _clean_label(row.get("source_title")) or entry.doc_id

    chunk_metadata: dict[str, Any] = {
        "subject": subject,
        "chapter": chapter,
        "heading_path": heading_path,
        "source_layer": entry.source_layer,
    }
    for key in ("chunk_role", "source_span_ids", "core_node_ids", "quality_flags", "page_start", "page_end"):
        value = row.get(key) if row.get(key) not in (None, "", []) else metadata.get(key)
        if value not in (None, "", []):
            chunk_metadata[key] = value

    text = str(row.get("text") or "").strip()
    source = subject or _clean_label(row.get("source_title")) or entry.source_layer or "KAG"
    source_type = str(row.get("source_type") or metadata.get("source_type") or "kag_chunk")
    path = str(row.get("source_title") or row.get("document_id") or "")
    matched_count = len(set(entry.item.get("matched_by") or []))
    return EvidenceChunk(
        id=entry.doc_id,
        text=text,
        source=source,
        source_type=source_type,
        title=reranker_title,
        path=path,
        score=min(1.0, 0.15 * matched_count + (0.1 if entry.source_layer in _AUTHORITATIVE_LAYERS else 0.0)),
        metadata=chunk_metadata,
    )


def _minimum_keep(limit: int, available: int) -> int:
    desired = max(4, min(8, math.ceil(limit / 3)))
    return min(limit, available, desired)


def _intent_mode(query: str) -> str:
    if any(term in query for term in ("什么情况", "表现", "症状", "鉴别", "特征")):
        return "clinical"
    if any(term in query for term in ("为什么", "机制", "原因", "病因")):
        return "mechanism"
    if any(term in query for term in ("怎么处理", "怎样处理", "如何治疗", "治疗", "术前", "用药")):
        return "treatment"
    return "general"


def _specific_query_terms(queries: Sequence[str]) -> list[str]:
    generic = {
        "什么情况",
        "典型临床表现",
        "临床表现",
        "诊断线索",
        "鉴别诊断",
        "怎么处理",
        "怎样处理",
    }
    values: list[str] = []
    for query in queries:
        values.extend(query_anchors(query))
        values.extend(re.split(r"[\s，,；;。]+", query))
    terms: list[str] = []
    for value in values:
        term = _clean_query(value).lower()
        if not 2 <= len(term) <= 16 or term in generic or term in terms:
            continue
        terms.append(term)
    return terms[:16]


def _intent_calibrated_rank(
    ranked: list[EvidenceChunk],
    original: str,
    queries: Sequence[str],
) -> list[EvidenceChunk]:
    mode = _intent_mode(original)
    terms = _specific_query_terms(queries)
    calibrated: list[tuple[float, int, EvidenceChunk]] = []
    for index, chunk in enumerate(ranked):
        try:
            base = float(chunk.metadata.get("rerank_score"))
        except (TypeError, ValueError):
            base = float(chunk.score or 0.0)
        headings = chunk.metadata.get("heading_path") or []
        if not isinstance(headings, list):
            headings = [headings]
        labels = " ".join(
            str(value or "")
            for value in (
                chunk.title,
                chunk.metadata.get("subject"),
                chunk.metadata.get("chapter"),
                " ".join(str(value) for value in headings),
            )
        )
        opening = _SPACE_RE.sub(" ", chunk.text or "").strip()[:420]
        haystack = f"{labels}\n{chunk.text}".lower()
        matched_terms = sum(1 for term in terms if term in haystack)
        label_matches = sum(1 for term in terms if term in labels.lower())
        adjustment = min(0.14, matched_terms * 0.035) + min(0.08, label_matches * 0.025)

        if chunk.metadata.get("source_layer") in _AUTHORITATIVE_LAYERS:
            adjustment += 0.02
        if mode == "clinical":
            if any(term in labels for term in ("临床表现", "诊断", "鉴别", "症状", "特征")):
                adjustment += 0.10
            if any(term in labels for term in ("病因", "发病机制", "治疗", "疾病管理")):
                adjustment -= 0.08
            if any(term in opening[:160] for term in ("感染因素", "病因和发病机制", "发病机制")):
                adjustment -= 0.06
        elif mode == "mechanism":
            if any(term in labels for term in ("病因", "机制", "病理", "发生发展")):
                adjustment += 0.10
            if any(term in labels for term in ("治疗", "操作步骤", "疾病管理")):
                adjustment -= 0.05
        elif mode == "treatment":
            if any(term in labels for term in ("治疗", "处理", "操作", "管理", "注意事项", "围手术期")):
                adjustment += 0.10
            if any(term in labels for term in ("病因", "流行病学")):
                adjustment -= 0.05

        if not chunk.metadata.get("subject") and not any(
            term in f"{labels} {opening}" for term in _ORAL_METADATA_TERMS
        ):
            adjustment -= 0.05
        calibrated.append((base + adjustment, -index, chunk))
    calibrated.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [chunk for _, _, chunk in calibrated]


def _dynamic_select(ranked: list[EvidenceChunk], rerank_source: str, limit: int) -> list[EvidenceChunk]:
    ranked = ranked[:limit]
    if rerank_source != "qwen3-rerank" or not ranked:
        return ranked

    scores: list[float] = []
    for chunk in ranked:
        try:
            scores.append(float(chunk.metadata.get("rerank_score")))
        except (TypeError, ValueError):
            return ranked
    top_score = max(scores)
    relative_floor = max(0.12, min(0.68, top_score * 0.55))
    relevant_count = sum(score >= relative_floor for score in scores)
    keep = max(_minimum_keep(limit, len(ranked)), relevant_count)
    return ranked[: min(limit, keep)]


def _rerank_query(original: str, queries: Sequence[str]) -> str:
    suffix = "避免教学目标、目录、参考文献或只顺带提及关键词的段落"
    intent = f"优先返回直接对应用户检索意图的口腔医学段落，{suffix}"
    if any(term in original for term in ("什么情况", "表现", "症状", "鉴别", "特征")):
        intent = f"优先返回直接描述与用户症状相符的典型临床表现或诊断线索，{suffix}"
    elif any(term in original for term in ("为什么", "机制", "原因", "病因")):
        intent = f"优先返回直接解释病因或发生机制的口腔医学段落，{suffix}"
    elif any(term in original for term in ("怎么处理", "怎样处理", "如何治疗", "治疗", "术前", "用药")):
        intent = f"优先返回直接说明处理原则、治疗方法或注意事项的口腔医学段落，{suffix}"
    planned = "；".join(queries[:MAX_QUERY_ROUTES])
    return f"{intent}。用户检索：{original}。检索表达：{planned}"[:480]


def _clone_chunk(chunk: EvidenceChunk) -> EvidenceChunk:
    return EvidenceChunk(
        id=chunk.id,
        text=chunk.text,
        source=chunk.source,
        source_type=chunk.source_type,
        title=chunk.title,
        path=chunk.path,
        score=chunk.score,
        metadata=deepcopy(chunk.metadata),
    )


@lru_cache(maxsize=128)
def _retrieve_cached(normalized: str, limit: int) -> tuple[EvidenceChunk, ...]:
    queries = _planned_queries_cached(normalized) or (normalized,)
    solver = get_kag_solver()
    refs = _balanced_pool(solver, queries[:MAX_QUERY_ROUTES])
    chunks = [_candidate_to_chunk(entry, solver) for entry in refs]
    if not chunks:
        return ()

    rerank_query = _rerank_query(normalized, queries)
    try:
        ranked, rerank_source = rerank_chunks(rerank_query, chunks, min(limit, len(chunks)))
    except Exception:
        selected = chunks[:limit]
    else:
        ranked = _intent_calibrated_rank(ranked, normalized, queries)
        selected = _dynamic_select(ranked, rerank_source, limit)
    return tuple(_clone_chunk(chunk) for chunk in selected)


def retrieve_knowledge_search_chunks(query: str, top_k: int = MAX_RESULTS) -> list[EvidenceChunk]:
    """Return page-specific semantic retrieval results without changing shared KAG behavior."""

    normalized = _clean_query(query)
    if not normalized:
        return []
    try:
        requested = int(top_k)
    except (TypeError, ValueError):
        requested = MAX_RESULTS
    limit = min(MAX_RESULTS, max(1, requested))
    return [_clone_chunk(chunk) for chunk in _retrieve_cached(normalized, limit)]


__all__ = [
    "retrieve_knowledge_search_chunks",
    "reset_knowledge_search_plan_cache",
    "reset_knowledge_search_retrieval_cache",
]
