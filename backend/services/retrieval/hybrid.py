from __future__ import annotations

import math
from collections import Counter, defaultdict

from .chunk_store import load_chunks
from .index_store import bm25_scores_from_index, vector_scores_from_index
from .models import EvidenceChunk
from .text_features import (
    CORE_HINTS,
    GENERIC_ANCHORS,
    MEDICAL_HINTS,
    core_anchors,
    query_anchors,
    tokenize,
)


SOURCE_PRIORITIES = {
    "guideline": 1.35,
    "clinical_norm": 1.35,
    "textbook": 1.18,
    "clinical_case": 1.05,
    "general_reference": 0.92,
}

CONTENT_TYPE_PRIORITIES = {
    "definition": 1.04,
    "etiology": 1.08,
    "pathogenesis": 1.16,
    "pathology": 1.14,
    "clinical_feature": 1.08,
    "diagnosis": 1.12,
    "differential": 1.12,
    "treatment": 1.05,
    "prognosis": 1.04,
    "risk": 1.06,
    "contraindication": 1.08,
}


def _candidate_chunks(
    query: str,
    chunks: tuple[EvidenceChunk, ...],
    source_types: set[str] | None = None,
    limit: int = 900,
) -> list[EvidenceChunk]:
    anchors = query_anchors(query)
    cores = core_anchors(query)
    if not anchors:
        return [chunk for chunk in chunks if not source_types or chunk.source_type in source_types][:limit]

    scored: list[tuple[float, EvidenceChunk]] = []
    for chunk in chunks:
        if source_types and chunk.source_type not in source_types:
            continue
        title = (chunk.title or "").lower()
        text = (chunk.text or "").lower()
        if cores and not any(anchor in title or anchor in text for anchor in cores):
            continue
        score = 0.0
        for anchor in anchors:
            if anchor in GENERIC_ANCHORS:
                weight = 0.25
            elif anchor in cores:
                weight = 3.2
            else:
                weight = 1.0
            if anchor in title:
                score += 3.0 * weight
            if anchor in text:
                score += 1.0 * weight
        if score:
            score *= SOURCE_PRIORITIES.get(chunk.source_type, 1.0)
            scored.append((score, chunk))

    scored.sort(key=lambda item: item[0], reverse=True)
    if scored:
        return [chunk for _, chunk in scored[:limit]]
    return [chunk for chunk in chunks if not source_types or chunk.source_type in source_types][:limit]


def _bm25_scores(query: str, chunks: list[EvidenceChunk]) -> dict[int, float]:
    query_terms = list(dict.fromkeys(tokenize(query)))
    if not query_terms or not chunks:
        return {}

    doc_counts: list[Counter[str]] = []
    df: Counter[str] = Counter()
    for chunk in chunks:
        counts = Counter(tokenize(f"{chunk.title}\n{chunk.text}"))
        doc_counts.append(counts)
        df.update(set(counts))
    avgdl = sum(sum(counts.values()) for counts in doc_counts) / max(len(doc_counts), 1)

    k1 = 1.4
    b = 0.72
    scores: dict[int, float] = defaultdict(float)
    total_docs = len(chunks)

    for idx, term_counts in enumerate(doc_counts):
        if not term_counts:
            continue
        dl = sum(term_counts.values())
        for term in query_terms:
            freq = term_counts.get(term, 0)
            if not freq:
                continue
            idf = math.log(1 + (total_docs - df[term] + 0.5) / (df[term] + 0.5))
            denom = freq + k1 * (1 - b + b * dl / max(avgdl, 1))
            scores[idx] += idf * (freq * (k1 + 1)) / denom

    return dict(scores)


def _keyword_boost(query: str, chunk: EvidenceChunk) -> float:
    haystack = f"{chunk.title}\n{chunk.text}".lower()
    boost = 0.0
    for hint in MEDICAL_HINTS:
        hint_lower = hint.lower()
        if hint_lower in query.lower() and hint_lower in haystack:
            boost += 0.35
    return boost


def _content_type_weight(query: str, chunk: EvidenceChunk) -> float:
    content_types = (chunk.metadata or {}).get("content_type") or (chunk.metadata or {}).get("content_types") or []
    if isinstance(content_types, str):
        content_types = [content_types]
    if not isinstance(content_types, list):
        return 1.0
    query_lower = (query or "").lower()
    wanted: set[str] = set()
    if any(term in query_lower for term in ("机制", "为什么", "病理机制", "形成")):
        wanted.update({"pathogenesis", "etiology", "pathology"})
    if any(term in query_lower for term in ("表现", "症状", "特征")):
        wanted.add("clinical_feature")
    if any(term in query_lower for term in ("鉴别", "区别")):
        wanted.add("differential")
    if any(term in query_lower for term in ("诊断", "检查")):
        wanted.add("diagnosis")
    if any(term in query_lower for term in ("治疗", "处理", "原则")):
        wanted.add("treatment")
    if any(term in query_lower for term in ("风险", "癌变", "并发症")):
        wanted.update({"risk", "prognosis"})
    if wanted and wanted.intersection(str(item) for item in content_types):
        return 1.18
    weight = 1.0
    for item in content_types:
        weight = max(weight, CONTENT_TYPE_PRIORITIES.get(str(item), 1.0))
    return weight


def explain_relevance(query: str, chunk: EvidenceChunk) -> str:
    haystack = f"{chunk.title}\n{chunk.text}".lower()
    matched = []
    for anchor in query_anchors(query):
        if anchor in GENERIC_ANCHORS:
            continue
        if anchor in haystack and anchor not in matched:
            matched.append(anchor)
    content_types = (chunk.metadata or {}).get("content_type") or (chunk.metadata or {}).get("content_types")
    suffix = ""
    if content_types:
        suffix = f"；内容标签：{content_types}"
    if matched:
        return "命中核心词：" + "、".join(matched[:6]) + suffix
    if chunk.source_type == "guideline":
        return "来自指南/共识库，可作为规范性依据候选，但需结合原文判断相关性。" + suffix
    if chunk.source_type == "textbook":
        return "来自教材库，可作为基础理论或临床解释依据候选。" + suffix
    return "与检索问题存在词项或语义匹配，需结合上下文复核。" + suffix


def _quality_weight(chunk: EvidenceChunk) -> float:
    metadata = chunk.metadata or {}
    quality = metadata.get("ocr_quality")
    if isinstance(quality, dict):
        try:
            score = float(quality.get("score", 1.0))
        except (TypeError, ValueError):
            score = 1.0
        return max(0.45, min(1.0, score))
    try:
        return max(0.45, min(1.0, float(metadata.get("quality_score", 1.0))))
    except (TypeError, ValueError):
        return 1.0


def _fallback_lexical_search(
    query: str,
    all_chunks: tuple[EvidenceChunk, ...],
    source_types: set[str] | None,
    recall_k: int,
) -> list[tuple[float, EvidenceChunk]]:
    candidates = _candidate_chunks(query, all_chunks, source_types, recall_k * 4)
    if not candidates:
        return []
    scores = _bm25_scores(query, candidates)
    ranked = [(score, candidates[idx]) for idx, score in scores.items()]
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[:recall_k]


def _merge_ranked(
    query: str,
    sparse: list[tuple[float, EvidenceChunk]],
    dense: list[tuple[float, EvidenceChunk]],
    fallback: list[tuple[float, EvidenceChunk]],
    source_types: set[str] | None,
) -> list[EvidenceChunk]:
    combined: dict[str, EvidenceChunk] = {}
    scores: dict[str, float] = defaultdict(float)

    def add(items: list[tuple[float, EvidenceChunk]], channel: str, channel_weight: float) -> None:
        if not items:
            return
        max_score = max(abs(score) for score, _ in items) or 1.0
        for rank, (score, chunk) in enumerate(items):
            if source_types and chunk.source_type not in source_types:
                continue
            combined[chunk.id] = chunk
            rank_bonus = 1.0 / max(rank + 1, 1)
            normalized = max(float(score), 0.0) / max_score
            scores[chunk.id] += channel_weight * normalized + rank_bonus * 0.06
            chunk.metadata = dict(chunk.metadata or {})
            channels = list(chunk.metadata.get("retrieval_channels") or [])
            if channel not in channels:
                channels.append(channel)
            chunk.metadata["retrieval_channels"] = channels

    add(sparse, "bm25_index", 0.54)
    add(dense, "vector_index", 0.42)
    add(fallback, "lexical_fallback", 0.34)

    ranked: list[EvidenceChunk] = []
    for chunk_id, chunk in combined.items():
        priority = SOURCE_PRIORITIES.get(chunk.source_type, 1.0)
        chunk.score = scores[chunk_id] * priority * _quality_weight(chunk) * _content_type_weight(query, chunk)
        chunk.score += _keyword_boost(query, chunk)
        ranked.append(chunk)
    ranked.sort(key=lambda item: item.score, reverse=True)
    return ranked


def _diversify(ranked: list[EvidenceChunk], target: int) -> list[EvidenceChunk]:
    diversified: list[EvidenceChunk] = []
    source_counts: Counter[str] = Counter()
    type_counts: Counter[str] = Counter()
    for chunk in ranked:
        source_key = chunk.source or chunk.source_type
        if source_counts[source_key] >= 2:
            continue
        if chunk.source_type == "general_reference" and type_counts[chunk.source_type] >= 2:
            continue
        if chunk.source_type == "clinical_case" and type_counts[chunk.source_type] >= 2:
            continue
        diversified.append(chunk)
        source_counts[source_key] += 1
        type_counts[chunk.source_type] += 1
        if len(diversified) >= target:
            break

    if len(diversified) < target:
        seen = {chunk.id for chunk in diversified}
        for chunk in ranked:
            if chunk.id not in seen:
                diversified.append(chunk)
                seen.add(chunk.id)
                if len(diversified) >= target:
                    break
    return diversified


def search_knowledge_base(query: str, top_k: int = 8, source_types: set[str] | None = None) -> list[EvidenceChunk]:
    all_chunks = load_chunks()
    if not all_chunks:
        return []
    chunks_by_id = {chunk.id: chunk for chunk in all_chunks}
    target = max(1, min(int(top_k or 8), 50))
    recall_k = max(24, min(120, target * 8))

    sparse = bm25_scores_from_index(query, chunks_by_id, recall_k)
    dense = vector_scores_from_index(query, chunks_by_id, recall_k)
    fallback = []
    if not sparse and not dense:
        fallback = _fallback_lexical_search(query, all_chunks, source_types, recall_k)

    ranked = _merge_ranked(query, sparse, dense, fallback, source_types)
    if not ranked:
        return []
    return _diversify(ranked, target)
