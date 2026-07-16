from __future__ import annotations

import re
from typing import Any

from .knowledge_search_format import format_knowledge_chunk
from .knowledge_search_retrieval import retrieve_knowledge_search_chunks
from .retrieval.models import EvidenceChunk


MAX_RESULTS = 24
DEFAULT_RESULTS = 24


def _clean_query(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _result_payload(chunk: EvidenceChunk, query: str) -> dict[str, Any]:
    formatted = format_knowledge_chunk(chunk, query)
    return {
        "id": str(chunk.id),
        "title": str(formatted.get("title") or "相关知识片段"),
        "subject": str(formatted.get("subject") or ""),
        "chapter": str(formatted.get("chapter") or ""),
        "preview": str(formatted.get("preview") or ""),
        "detail": str(formatted.get("detail") or ""),
        "blocks": formatted.get("blocks") if isinstance(formatted.get("blocks"), list) else [],
    }


def run_knowledge_search(query: str, top_k: int = DEFAULT_RESULTS) -> dict[str, Any]:
    normalized_query = _clean_query(query)
    if not normalized_query:
        return {
            "ok": False,
            "module": "knowledge_search",
            "message": "query is required",
            "results": [],
        }

    try:
        requested = int(top_k)
    except (TypeError, ValueError):
        requested = DEFAULT_RESULTS
    limit = min(MAX_RESULTS, max(1, requested))

    chunks = retrieve_knowledge_search_chunks(normalized_query, limit)
    results = [_result_payload(chunk, normalized_query) for chunk in chunks[:limit]]
    results = [item for item in results if item["preview"] or item["detail"]]
    return {
        "ok": True,
        "module": "knowledge_search",
        "query": normalized_query,
        "count": len(results),
        "results": results,
    }


__all__ = ["run_knowledge_search"]
