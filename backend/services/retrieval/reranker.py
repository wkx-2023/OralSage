from __future__ import annotations

import json
import math
import os
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from backend.services.env_loader import load_project_env

from .models import EvidenceChunk


load_project_env()

DEFAULT_QWEN_RERANK_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-api/v1/reranks"
DEFAULT_QWEN_RERANK_MODEL = "qwen3-rerank"
MAX_DOCUMENT_CHARS = 1800


def _env(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def _qwen_api_key() -> str | None:
    return _env("QWEN_RERANK_API_KEY", "RERANKER_API_KEY", "QWEN_API_KEY", "DASHSCOPE_API_KEY")


def _qwen_endpoint() -> str:
    raw = _env("QWEN_RERANK_BASE_URL", "QWEN_RERANK_ENDPOINT", "RERANKER_BASE_URL") or DEFAULT_QWEN_RERANK_ENDPOINT
    raw = raw.rstrip("/")
    if raw.endswith("/reranks"):
        return raw
    return f"{raw}/reranks"


def _qwen_model() -> str:
    return _env("QWEN_RERANK_MODEL", "RERANKER_MODEL") or DEFAULT_QWEN_RERANK_MODEL


def _timeout_seconds() -> int:
    raw = _env("QWEN_RERANK_TIMEOUT_SECONDS", "RERANKER_TIMEOUT_SECONDS")
    if not raw:
        return 45
    try:
        return max(10, min(int(raw), 120))
    except ValueError:
        return 45


def reranker_status() -> dict[str, Any]:
    return {
        "enabled": bool(_qwen_api_key()),
        "provider": "qwen",
        "model": _qwen_model(),
        "endpoint": _qwen_endpoint(),
    }


def _tokens(text: str) -> list[str]:
    text = (text or "").lower()
    tokens = re.findall(r"[a-z0-9][a-z0-9+\-]{1,}", text)
    for segment in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(segment) <= 2:
            tokens.append(segment)
        else:
            tokens.extend(segment[i : i + 2] for i in range(len(segment) - 1))
            tokens.extend(segment[i : i + 3] for i in range(len(segment) - 2))
    return [token for token in tokens if token.strip()]


def _fallback_score(query: str, chunk: EvidenceChunk, rank: int) -> float:
    query_terms = set(_tokens(query))
    doc_terms = set(_tokens(f"{chunk.title}\n{chunk.text}"))
    if not query_terms or not doc_terms:
        overlap = 0.0
    else:
        overlap = len(query_terms & doc_terms) / math.sqrt(len(query_terms) * len(doc_terms))
    return overlap + (1.0 / max(rank + 1, 1)) * 0.15 + float(chunk.score or 0.0) * 0.03


def _fallback_rerank(query: str, chunks: list[EvidenceChunk], top_k: int, reason: str | None = None) -> tuple[list[EvidenceChunk], str]:
    scored = [
        (_fallback_score(query, chunk, rank), chunk)
        for rank, chunk in enumerate(chunks)
    ]
    scored.sort(key=lambda item: item[0], reverse=True)
    for score, chunk in scored:
        chunk.metadata = dict(chunk.metadata or {})
        chunk.metadata["rerank_score"] = round(float(score), 4)
        chunk.metadata["rerank_source"] = "lexical_fallback"
        if reason:
            chunk.metadata["rerank_error"] = reason
        chunk.score = max(float(chunk.score or 0.0), float(score))
    return [chunk for _, chunk in scored[:top_k]], "lexical_fallback"


def _document_text(chunk: EvidenceChunk) -> str:
    title = f"{chunk.source} {chunk.title}".strip()
    text = f"{title}\n{chunk.text}".strip()
    return text[:MAX_DOCUMENT_CHARS]


def _parse_qwen_results(data: dict[str, Any]) -> list[dict[str, Any]]:
    results = data.get("results")
    if isinstance(results, list):
        return [item for item in results if isinstance(item, dict)]
    output = data.get("output")
    if isinstance(output, dict) and isinstance(output.get("results"), list):
        return [item for item in output["results"] if isinstance(item, dict)]
    data_field = data.get("data")
    if isinstance(data_field, dict) and isinstance(data_field.get("results"), list):
        return [item for item in data_field["results"] if isinstance(item, dict)]
    return []


def _qwen_rerank(query: str, chunks: list[EvidenceChunk], top_k: int) -> tuple[list[EvidenceChunk], str]:
    api_key = _qwen_api_key()
    if not api_key:
        return _fallback_rerank(query, chunks, top_k, "Qwen rerank API key is not configured")

    documents = [_document_text(chunk) for chunk in chunks]
    payload = {
        "model": _qwen_model(),
        "query": query,
        "documents": documents,
        "top_n": min(max(int(top_k or 1), 1), len(documents)),
        "return_documents": False,
    }
    request = Request(
        _qwen_endpoint(),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "OralPath-Rebuild/0.1",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=_timeout_seconds()) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
    except (HTTPError, URLError, TimeoutError) as exc:
        return _fallback_rerank(query, chunks, top_k, str(exc))

    results = _parse_qwen_results(data)
    if not results:
        return _fallback_rerank(query, chunks, top_k, f"Unexpected Qwen rerank response shape: {data}")

    scored: list[tuple[float, EvidenceChunk]] = []
    for item in results:
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        if index < 0 or index >= len(chunks):
            continue
        try:
            score = float(item.get("relevance_score", item.get("score", 0.0)))
        except (TypeError, ValueError):
            score = 0.0
        chunk = chunks[index]
        chunk.metadata = dict(chunk.metadata or {})
        chunk.metadata["rerank_score"] = round(score, 4)
        chunk.metadata["rerank_source"] = "qwen3-rerank"
        chunk.score = score
        scored.append((score, chunk))

    if not scored:
        return _fallback_rerank(query, chunks, top_k, "Qwen rerank response did not contain usable indexes")

    scored.sort(key=lambda item: item[0], reverse=True)
    return [chunk for _, chunk in scored[:top_k]], "qwen3-rerank"


def rerank_chunks(query: str, chunks: list[EvidenceChunk], top_k: int) -> tuple[list[EvidenceChunk], str]:
    if not chunks:
        return [], "none"
    return _qwen_rerank(query, chunks, top_k)
