import asyncio
from datetime import date
from unittest.mock import MagicMock, patch

from backend.services.literature.retrieval import _scopus_entry_is_in_window, _scopus_year_filter
from backend.services.mcp_server import oral_sage_mcp
from backend.services.router.master_router import MasterRouter


def test_scopus_uses_supported_year_range_and_precise_local_filter() -> None:
    start = date(2025, 7, 31)
    end = date(2026, 7, 31)

    assert _scopus_year_filter(start, end) == "2025-2026"
    assert _scopus_entry_is_in_window("2025-07-30", start) is False
    assert _scopus_entry_is_in_window("2025-07-31", start) is True
    assert _scopus_entry_is_in_window("2026-01-01", start) is True


def test_mcp_server_registers_discoverable_knowledge_tools() -> None:
    tools = asyncio.run(oral_sage_mcp.list_tools())
    assert {tool.name for tool in tools} == {
        "knowledge_base_search",
        "resolve_oral_terms",
        "search_pubmed_oral_medicine",
        "search_elsevier_oral_medicine",
    }


def test_router_uses_mcp_literature_service_by_default() -> None:
    with patch("backend.services.router.master_router.run_literature_search_via_mcp") as mcp_service:
        router = MasterRouter(MagicMock())

    assert router._literature_service is mcp_service
