from __future__ import annotations

from unittest.mock import patch

from backend.services.knowledge_search import run_knowledge_search
from backend.services.retrieval.models import EvidenceChunk


def _chunk(
    index: int = 1,
    text: str = "牙周基础治疗包括口腔卫生指导、洁治和根面平整。",
) -> EvidenceChunk:
    return EvidenceChunk(
        id=f"chunk:{index}",
        text=text,
        source="internal",
        source_type="textbook",
        title="牙周病学｜牙周炎｜非手术治疗",
        metadata={
            "subject": "《牙周病学》孟焕新、第5版",
            "chapter": "牙周炎的治疗",
            "heading_path": ["牙周炎", "【治疗】"],
            "source_layer": "textbook_evidence",
            "kag": {"retrieval_trace": {"dense": 3}},
        },
    )


def test_knowledge_search_uses_page_retrieval_and_caps_results() -> None:
    chunks = [_chunk(index) for index in range(1, 25)]

    with patch(
        "backend.services.knowledge_search.retrieve_knowledge_search_chunks",
        return_value=chunks,
    ) as retrieve:
        result = run_knowledge_search("  牙周基础治疗  ", top_k=99)

    retrieve.assert_called_once_with("牙周基础治疗", 24)
    assert result["ok"] is True
    assert result["count"] == 24
    assert len(result["results"]) == 24


def test_knowledge_search_projects_only_public_page_fields() -> None:
    with patch(
        "backend.services.knowledge_search.retrieve_knowledge_search_chunks",
        return_value=[_chunk()],
    ):
        result = run_knowledge_search("牙周炎如何治疗")

    item = result["results"][0]
    assert set(item) == {
        "id",
        "title",
        "subject",
        "chapter",
        "preview",
        "detail",
        "blocks",
    }
    assert item["title"] == "牙周炎 · 非手术治疗"
    assert item["subject"] == "牙周病学"
    assert item["blocks"]
    assert "source" not in item
    assert "metadata" not in item
    assert "trace" not in item
    assert "score" not in item


def test_knowledge_search_returns_structured_reading_blocks() -> None:
    text = """牙周炎

【治疗】

非手术治疗包括以下措施：

1. 口腔卫生指导。

2. 洁治、刮治和根面平整。

3. 必要时进行咬合调整。

【预后】

规范治疗后炎症通常可以得到控制。"""
    with patch(
        "backend.services.knowledge_search.retrieve_knowledge_search_chunks",
        return_value=[_chunk(text=text)],
    ):
        result = run_knowledge_search("牙周炎如何治疗")

    item = result["results"][0]
    assert [block["type"] for block in item["blocks"]] == ["paragraph", "list"]
    assert item["blocks"][-1]["items"] == [
        "口腔卫生指导。",
        "洁治、刮治和根面平整。",
        "必要时进行咬合调整。",
    ]
    assert "预后" not in item["detail"]


def test_knowledge_search_default_requests_twenty_four_results() -> None:
    with patch(
        "backend.services.knowledge_search.retrieve_knowledge_search_chunks",
        return_value=[],
    ) as retrieve:
        result = run_knowledge_search("牙齿遇冷短暂疼痛")

    retrieve.assert_called_once_with("牙齿遇冷短暂疼痛", 24)
    assert result["count"] == 0


def test_blank_query_does_not_run_retrieval() -> None:
    with patch("backend.services.knowledge_search.retrieve_knowledge_search_chunks") as retrieve:
        result = run_knowledge_search("   ")

    retrieve.assert_not_called()
    assert result == {
        "ok": False,
        "module": "knowledge_search",
        "message": "query is required",
        "results": [],
    }


def test_empty_formatted_chunk_is_not_returned() -> None:
    empty = _chunk(text="")
    with patch(
        "backend.services.knowledge_search.retrieve_knowledge_search_chunks",
        return_value=[empty],
    ):
        result = run_knowledge_search("牙周炎")

    assert result["count"] == 0
    assert result["results"] == []
