from __future__ import annotations

from typing import Any

from backend.services.literature.retrieval import search_elsevier_oral_medicine, search_pubmed_oral_medicine
from backend.services.literature.terms import resolve_terms
from backend.services.retrieval import explain_relevance, search_knowledge_base


def _query(req: Any) -> str:
    return str(req.query or req.question or req.task or req.message or req.text or "")


async def invoke_local_mcp_tool(tool_name: str, req: Any) -> dict[str, Any]:
    normalized = tool_name.strip().lower().replace("-", "_")

    if normalized == "knowledge_base_search":
        query = _query(req)
        top_k = int(req.top_k or req.limit or 8)
        chunks = search_knowledge_base(query, top_k=top_k)
        results = []
        for chunk in chunks:
            item = chunk.to_dict()
            item["relevance"] = explain_relevance(query, chunk)
            results.append(item)
        return {
            "ok": True,
            "tool": "knowledge_base_search",
            "query": query,
            "count": len(results),
            "results": results,
        }

    if normalized in {"resolve_oral_terms", "oral_term_resolver", "resolve_terms"}:
        query = _query(req)
        return {
            "ok": True,
            "tool": "resolve_oral_terms",
            "query": query,
            "result": resolve_terms(query, req.months).to_dict(),
        }

    if normalized == "search_pubmed_oral_medicine":
        query = _query(req)
        articles = await search_pubmed_oral_medicine(query, req.months, req.limit or req.top_k or 5)
        return {
            "ok": True,
            "tool": "search_pubmed_oral_medicine",
            "query": query,
            "count": len(articles),
            "articles": articles,
        }

    if normalized == "search_elsevier_oral_medicine":
        query = _query(req)
        api_key = req.data.get("api_key") if isinstance(req.data, dict) else None
        institution_token = req.data.get("institution_token") if isinstance(req.data, dict) else None
        articles = await search_elsevier_oral_medicine(
            query,
            req.months,
            req.limit or req.top_k or 5,
            api_key=api_key,
            institution_token=institution_token,
        )
        return {
            "ok": True,
            "tool": "search_elsevier_oral_medicine",
            "query": query,
            "count": len(articles),
            "articles": articles,
        }

    return {
        "ok": False,
        "tool": tool_name,
        "message": "Unsupported local MCP tool",
        "supported_tools": [
            "knowledge_base_search",
            "resolve_oral_terms",
            "search_pubmed_oral_medicine",
            "search_elsevier_oral_medicine",
        ],
    }
