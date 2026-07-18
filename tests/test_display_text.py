from backend.services.display_text import sanitize_display_text
from unittest.mock import patch

import pytest


def test_internal_evidence_ids_are_removed_from_visible_text() -> None:
    text = (
        "1. 牙髓炎常有自发痛（chunk:endo-001）。\n"
        "2. 叩诊有助于鉴别 [core:abc_123]。\n"
        "来源 document.d766e8629232a2f6"
    )

    cleaned = sanitize_display_text(text)

    assert "chunk:" not in cleaned
    assert "core:" not in cleaned
    assert "document." not in cleaned
    assert "1. 牙髓炎常有自发痛。" in cleaned
    assert "2. 叩诊有助于鉴别。" in cleaned


def test_public_citation_markers_and_markdown_are_preserved() -> None:
    text = "## 核心结论\n\n1. 判断一[依据1]\n2. 判断二[文献2]"
    assert sanitize_display_text(text) == text


@pytest.mark.asyncio
async def test_pathology_answer_is_sanitized_without_touching_citations() -> None:
    from backend.services.pathology.agent import run_pathology_ask

    with patch(
        "backend.services.pathology.agent.normalize_pathology_query",
        return_value=({"local_query_zh": "测试"}, []),
    ), patch(
        "backend.services.pathology.agent.collect_local_evidence",
        return_value=([], {"count": 0}),
    ), patch(
        "backend.services.pathology.agent.is_llm_configured", return_value=True,
    ), patch(
        "backend.services.pathology.agent.chat_completion",
        return_value="## 核心结论\n1. 结论（chunk:path-1）[依据1]",
    ):
        result = await run_pathology_ask("测试问题", allow_web=False)

    assert result["ok"] is True
    assert "chunk:" not in result["answer"]
    assert "[依据1]" in result["answer"]
    assert result["citations"] == []


@pytest.mark.asyncio
async def test_decision_answer_is_sanitized_without_exposing_retrieval_ids() -> None:
    from backend.services.decision.agent import run_decision_assist

    plan = {"combined_query": "测试", "tasks": []}
    with patch(
        "backend.services.decision.agent._missing_fields", return_value=[],
    ), patch(
        "backend.services.decision.agent._plan_retrieval",
        return_value=(plan, "test", []),
    ), patch(
        "backend.services.decision.agent._search_task_evidence",
        return_value=([], []),
    ), patch(
        "backend.services.decision.agent.is_llm_configured", return_value=True,
    ), patch(
        "backend.services.decision.agent.chat_completion",
        return_value="## 三、鉴别诊断\n1. 方向一 core:decision-1 [依据1]",
    ), patch(
        "backend.services.decision.agent.reranker_status",
        return_value={"enabled": False},
    ):
        result = await run_decision_assist("主诉：测试", allow_web=False)

    assert result["ok"] is True
    assert "core:" not in result["answer"]
    assert "[依据1]" in result["answer"]
    assert result["citations"] == []
