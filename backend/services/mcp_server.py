"""Standards-compliant MCP tools for OralSage professional knowledge."""

from __future__ import annotations

import asyncio
from typing import Any

from mcp.server.fastmcp import FastMCP

from .literature.retrieval import search_elsevier_oral_medicine, search_pubmed_oral_medicine
from .literature.terms import resolve_terms
from .retrieval import explain_relevance, search_knowledge_base


# Mounted by FastAPI at /mcp/server/. FastMCP provides initialize, tools/list,
# and tools/call over the standard Streamable HTTP transport.
oral_sage_mcp = FastMCP(
    "OralSage Professional Knowledge",
    instructions="Professional oral medicine retrieval and literature-search tools.",
    streamable_http_path="/",
)


def _bounded_top_k(value: int) -> int:
    return max(1, min(int(value), 24))


@oral_sage_mcp.tool(
    name="knowledge_base_search",
    description="Search OralSage's local oral medicine knowledge base for supporting evidence.",
)
async def knowledge_base_search(query: str, top_k: int = 8) -> dict[str, Any]:
    query = query.strip()
    if not query:
        return {"ok": False, "tool": "knowledge_base_search", "message": "query is required", "results": []}

    chunks = await asyncio.to_thread(search_knowledge_base, query, top_k=_bounded_top_k(top_k))
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


@oral_sage_mcp.tool(
    name="resolve_oral_terms",
    description="Convert an oral medicine topic into English search terms and a recommended time window.",
)
async def resolve_oral_terms(query: str, months: int | None = None) -> dict[str, Any]:
    query = query.strip()
    return {
        "ok": True,
        "tool": "resolve_oral_terms",
        "query": query,
        "result": resolve_terms(query, months).to_dict(),
    }


@oral_sage_mcp.tool(
    name="search_pubmed_oral_medicine",
    description="Search recent oral medicine literature through PubMed ESearch, ESummary, and EFetch.",
)
async def search_pubmed_oral_medicine_tool(
    query: str,
    months: int | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    articles = await search_pubmed_oral_medicine(query.strip(), months, limit)
    return {
        "ok": True,
        "tool": "search_pubmed_oral_medicine",
        "query": query,
        "count": len(articles),
        "articles": articles,
    }


@oral_sage_mcp.tool(
    name="search_elsevier_oral_medicine",
    description="Search recent oral medicine literature through the Scopus Search API.",
)
async def search_elsevier_oral_medicine_tool(
    query: str,
    months: int | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    articles = await search_elsevier_oral_medicine(query.strip(), months, limit)
    return {
        "ok": True,
        "tool": "search_elsevier_oral_medicine",
        "query": query,
        "count": len(articles),
        "articles": articles,
    }


def mcp_asgi_app():
    """Return the ASGI app mounted by the primary FastAPI service."""
    return oral_sage_mcp.streamable_http_app()
