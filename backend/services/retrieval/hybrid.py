from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

from .chunk_store import load_chunks
from .models import EvidenceChunk


SOURCE_PRIORITIES = {
    "guideline": 1.35,
    "clinical_norm": 1.35,
    "textbook": 1.18,
    "clinical_case": 1.05,
    "general_reference": 0.92,
}

MEDICAL_HINTS = (
    "阻生", "第三磨牙", "智齿", "拔除", "根管", "牙髓", "根尖", "牙周", "龋", "牙槽骨",
    "CBCT", "影像", "叩诊", "探诊", "冷热", "松动", "肿胀", "疼痛", "种植", "修复",
    "黏膜", "颞下颌", "正畸", "儿童", "乳牙", "恒牙", "下牙槽神经",
)


def _tokenize(text: str) -> list[str]:
    text = (text or "").lower()
    latin = re.findall(r"[a-z0-9][a-z0-9+\-]{1,}", text)
    chinese = re.findall(r"[\u4e00-\u9fff]+", text)
    tokens: list[str] = []
    tokens.extend(latin)
    for segment in chinese:
        if len(segment) <= 2:
            tokens.append(segment)
            continue
        tokens.extend(segment[i : i + 2] for i in range(len(segment) - 1))
        tokens.extend(segment[i : i + 3] for i in range(len(segment) - 2))
    for hint in MEDICAL_HINTS:
        if hint.lower() in text:
            tokens.append(hint.lower())
    return [token for token in tokens if token.strip()]


def _query_anchors(query: str) -> list[str]:
    query_lower = (query or "").lower()
    anchors: list[str] = []
    for hint in MEDICAL_HINTS:
        if hint.lower() in query_lower:
            anchors.append(hint.lower())
    anchors.extend(re.findall(r"[a-z0-9][a-z0-9+\-]{2,}", query_lower))
    anchors.extend(term for term in re.findall(r"[\u4e00-\u9fff]{2,}", query_lower) if len(term) <= 8)
    return list(dict.fromkeys(anchors))[:24]


def _candidate_chunks(
    query: str,
    chunks: tuple[EvidenceChunk, ...],
    source_types: set[str] | None = None,
    limit: int = 900,
) -> list[EvidenceChunk]:
    anchors = _query_anchors(query)
    if not anchors:
        return list(chunks[:limit])

    scored: list[tuple[float, EvidenceChunk]] = []
    for chunk in chunks:
        if source_types and chunk.source_type not in source_types:
            continue
        title = (chunk.title or "").lower()
        text = (chunk.text or "").lower()
        score = 0.0
        for anchor in anchors:
            if anchor in title:
                score += 3.0
            if anchor in text:
                score += 1.0
        if score:
            score *= SOURCE_PRIORITIES.get(chunk.source_type, 1.0)
            scored.append((score, chunk))

    scored.sort(key=lambda item: item[0], reverse=True)
    if scored:
        return [chunk for _, chunk in scored[:limit]]
    return [chunk for chunk in chunks if not source_types or chunk.source_type in source_types][:limit]


def _bm25_scores(query: str, chunks: list[EvidenceChunk]) -> dict[int, float]:
    query_terms = list(dict.fromkeys(_tokenize(query)))
    if not query_terms or not chunks:
        return {}

    doc_counts: list[Counter[str]] = []
    df: Counter[str] = Counter()
    for chunk in chunks:
        counts = Counter(_tokenize(f"{chunk.title}\n{chunk.text}"))
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


def _quality_weight(chunk: EvidenceChunk) -> float:
    quality = (chunk.metadata or {}).get("ocr_quality")
    if not isinstance(quality, dict):
        return 1.0
    try:
        score = float(quality.get("score", 1.0))
    except (TypeError, ValueError):
        return 1.0
    return max(0.45, min(1.0, score))


def search_knowledge_base(query: str, top_k: int = 8, source_types: set[str] | None = None) -> list[EvidenceChunk]:
    all_chunks = load_chunks()
    candidates = _candidate_chunks(query, all_chunks, source_types)
    if not candidates:
        return []

    scores = _bm25_scores(query, candidates)
    ranked: list[EvidenceChunk] = []
    for idx, score in scores.items():
        chunk = candidates[idx]
        priority = SOURCE_PRIORITIES.get(chunk.source_type, 1.0) * _quality_weight(chunk)
        chunk.score = score * priority + _keyword_boost(query, chunk)
        ranked.append(chunk)

    ranked.sort(key=lambda item: item.score, reverse=True)
    return ranked[: max(1, min(int(top_k or 8), 20))]
