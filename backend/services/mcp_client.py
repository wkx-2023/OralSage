"""MCP client used by the router for professional knowledge tools."""

from __future__ import annotations

import json
import os
from typing import Any

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client


DEFAULT_MCP_URL = "http://127.0.0.1:8000/mcp/server/"


class MCPToolError(RuntimeError):
    pass


class MCPTransportError(MCPToolError):
    pass


def _tool_error_text(result: Any) -> str:
    parts = [getattr(item, "text", str(item)) for item in (result.content or [])]
    return "\n".join(part for part in parts if part) or "MCP tool call failed"


async def call_knowledge_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Invoke a tool through the MCP Streamable HTTP protocol."""
    endpoint = os.getenv("ORALSAGE_MCP_URL", DEFAULT_MCP_URL).strip() or DEFAULT_MCP_URL
    try:
        async with streamable_http_client(endpoint) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
    except Exception as exc:
        raise MCPTransportError(f"MCP transport unavailable at {endpoint}: {exc}") from exc

    if result.isError:
        raise MCPToolError(_tool_error_text(result))
    if isinstance(result.structuredContent, dict):
        return result.structuredContent

    text = _tool_error_text(result)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MCPToolError(f"MCP tool {tool_name} returned an unstructured response") from exc
    if not isinstance(payload, dict):
        raise MCPToolError(f"MCP tool {tool_name} returned an invalid response")
    return payload


async def run_literature_search_via_mcp(
    query: str,
    months: int | None = None,
    limit: int = 5,
    *,
    include_chat_translations: bool = False,
) -> dict[str, Any]:
    """Run existing literature orchestration with MCP-backed retrieval tools.

    A failed MCP connection falls back to the original direct service so a
    deployment ordering issue cannot take down literature search.
    """
    from .literature.agent import run_literature_search

    transport_error: MCPTransportError | None = None

    async def tracked_tool_call(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        nonlocal transport_error
        try:
            return await call_knowledge_tool(tool_name, arguments)
        except MCPTransportError as exc:
            transport_error = exc
            raise

    try:
        # Validate connectivity before the agent starts collecting partial errors.
        await tracked_tool_call("resolve_oral_terms", {"query": query, "months": months})
        result = await run_literature_search(
            query,
            months,
            limit,
            include_chat_translations=include_chat_translations,
            tool_caller=tracked_tool_call,
        )
        if transport_error is None:
            return result
    except MCPTransportError:
        pass

    # An interrupted MCP session must not change the user-facing behavior of
    # the established literature service.
    return await run_literature_search(
        query,
        months,
        limit,
        include_chat_translations=include_chat_translations,
    )
