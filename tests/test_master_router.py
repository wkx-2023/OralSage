"""Tests for master_router.py — no external LLM or network calls.

All tests inject a mock classifier so the test suite is fully deterministic
and does not depend on any external service.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.schemas import RouterChatRequest

from backend.services.router.master_router import (
    CANDIDATE_WHITELIST_FIELDS,
    CLASSIFIER_SYSTEM_PROMPT,
    INTENT_TO_NEXT_ACTION,
    INTENT_TO_TARGET,
    PLAN_ITEM_MATCHER_SYSTEM_PROMPT,
    VALID_INTENTS,
    MasterRouter,
    _build_candidate_whitelist,
    _build_classifier_messages,
    _validate_route,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_orchestrator() -> MagicMock:
    """Return a LearningOrchestrator mock with a working memory stub."""
    orch = MagicMock()
    orch.memory = MagicMock()
    orch.memory.get_study_plan_item = MagicMock()
    orch.memory.list_open_study_plan_items = MagicMock(return_value=[])
    orch.start_lesson = MagicMock()
    return orch


@pytest.fixture
def sample_plan_item() -> dict[str, Any]:
    """A valid open plan item (NO hardcoded real-world IDs)."""
    return {
        "id": "pi-test-001",
        "user_id": "demo",
        "plan_date": date.today().isoformat(),
        "title": "Test Topic Alpha",
        "status": "open",
        "linked_course_id": None,
        "linked_course_title": "Linked Course Title Alpha",
        "kg_node_ids_json": "[]",
    }


# ---------------------------------------------------------------------------
# Unit tests — helpers
# ---------------------------------------------------------------------------


class TestValidateRoute:
    """Server-side enum validation."""

    def test_valid_intents(self):
        for intent in VALID_INTENTS:
            result = _validate_route({"intent": intent, "reason": "ok"})
            assert result is not None
            assert result["intent"] == intent
            assert result["source"] == "llm_router"

    def test_invalid_intent_rejected(self):
        assert _validate_route({"intent": "chat", "reason": "x"}) is None
        assert _validate_route({"intent": "", "reason": "x"}) is None

    def test_case_insensitive_intent_accepted(self):
        """Mixed-case intent is normalized via .lower() and accepted."""
        result = _validate_route({"intent": "LEARNING", "reason": "x"})
        assert result is not None
        assert result["intent"] == "learning"

    def test_missing_intent_rejected(self):
        assert _validate_route({"reason": "x"}) is None

    def test_non_dict_rejected(self):
        assert _validate_route("not a dict") is None
        assert _validate_route(None) is None
        assert _validate_route([]) is None

    def test_missing_reason_fills_default(self):
        result = _validate_route({"intent": "learning"})
        assert result is not None
        assert result["reason"] == "无法提供具体理由。"
        assert result["plan_item_requested"] is False

    def test_explicit_plan_item_request_is_preserved(self):
        result = _validate_route(
            {
                "intent": "learning",
                "plan_item_requested": True,
                "reason": "用户明确要求完成已有计划",
            }
        )
        assert result is not None
        assert result["plan_item_requested"] is True

    def test_plan_item_request_must_be_boolean_and_learning_intent(self):
        assert _validate_route(
            {"intent": "learning", "plan_item_requested": "true"}
        ) is None
        assert _validate_route(
            {"intent": "study_plan", "plan_item_requested": True}
        ) is None


class TestBuildClassifierMessages:
    def test_basic_message(self):
        msgs = _build_classifier_messages("hello", None)
        assert msgs[0]["role"] == "system"
        assert CLASSIFIER_SYSTEM_PROMPT in msgs[0]["content"]
        assert msgs[-1]["role"] == "user"
        assert msgs[-1]["content"] == "hello"

    def test_with_history(self):
        history = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
        ]
        msgs = _build_classifier_messages("hello", history)
        # system + 3 history + 1 user = 5
        assert len(msgs) == 5
        assert msgs[1]["content"] == "q1"
        assert msgs[2]["content"] == "a1"
        assert msgs[3]["content"] == "q2"

    def test_history_truncated_to_last_6(self):
        history = [{"role": "user", "content": f"msg{i}"} for i in range(20)]
        msgs = _build_classifier_messages("hello", history)
        # system + up to 6 history + 1 user
        assert len(msgs) <= 8  # system + 6 + 1
        # last history entry should be msg19
        history_contents = [m["content"] for m in msgs[1:-1]]
        assert history_contents[-1] == "msg19"


class TestCandidateWhitelist:
    """Field whitelist for select_plan responses."""

    def test_strips_extra_fields(self):
        items = [
            {"id": "pi-1", "title": "A", "plan_date": "2026-07-12",
             "linked_course_title": "C", "status": "open", "extra": "x",
             "user_id": "demo"},
        ]
        result = _build_candidate_whitelist(items)
        assert len(result) == 1
        assert set(result[0].keys()) == set(CANDIDATE_WHITELIST_FIELDS)
        assert result[0]["plan_item_id"] == "pi-1"
        assert result[0]["title"] == "A"
        # plan_date must NOT appear in the response
        assert "plan_date" not in result[0]

    def test_missing_fields_become_none(self):
        items = [{"id": "pi-min", "title": "Minimal"}]
        result = _build_candidate_whitelist(items)
        assert len(result) == 1
        assert result[0]["plan_item_id"] == "pi-min"
        assert result[0]["linked_course_title"] is None

    def test_empty_list(self):
        assert _build_candidate_whitelist([]) == []

    def test_whitelist_fields_are_strings(self):
        for field in CANDIDATE_WHITELIST_FIELDS:
            assert isinstance(field, str)

    def test_matcher_prompt_is_separate_from_classifier(self):
        """PLAN_ITEM_MATCHER_SYSTEM_PROMPT is NOT the same as CLASSIFIER_SYSTEM_PROMPT."""
        assert PLAN_ITEM_MATCHER_SYSTEM_PROMPT != CLASSIFIER_SYSTEM_PROMPT
        assert "计划匹配器" in PLAN_ITEM_MATCHER_SYSTEM_PROMPT or "plan_item_id" in PLAN_ITEM_MATCHER_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Integration / behaviour tests (async)
# ---------------------------------------------------------------------------


def _make_classifier(
    intent: str,
    reason: str = "test reason",
    *,
    plan_item_requested: bool = False,
):
    """Factory for an injectable classifier that always returns *intent*."""

    def _classify(message, history, context):
        return {
            "intent": intent,
            "target": INTENT_TO_TARGET.get(intent, "none"),
            "plan_item_requested": plan_item_requested,
            "reason": reason,
            "source": "llm_router",
        }

    return _classify


# ---------------------------------------------------------------------------
# Learning intent
# ---------------------------------------------------------------------------


class TestLearningIntent:
    @pytest.mark.asyncio
    async def test_learning_topic_alpha(self, mock_orchestrator):
        """First learning topic — proves no hardcoded sample."""
        mock_orchestrator.start_lesson.return_value = {
            "session": {"id": "sess-alpha"},
            "outline": {"nodes": []},
        }
        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier("learning", "用户想学习"),
        )
        result = await router.route("我想了解口腔溃疡的病因", "demo")

        assert result["ok"] is True
        assert result["route"]["intent"] == "learning"
        assert result["route"]["source"] == "llm_router"
        assert result["next_action"] == "start_session"
        mock_orchestrator.start_lesson.assert_called_once()
        call_kwargs = mock_orchestrator.start_lesson.call_args.kwargs
        assert call_kwargs["user_id"] == "demo"
        assert "口腔溃疡" in call_kwargs["topic"]
        mock_orchestrator.memory.list_open_study_plan_items.assert_not_called()

    @pytest.mark.asyncio
    async def test_learning_topic_beta(self, mock_orchestrator):
        """Second learning topic — different from alpha, proves no hardcoding."""
        mock_orchestrator.start_lesson.return_value = {
            "session": {"id": "sess-beta"},
            "outline": {"nodes": []},
        }
        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier("learning", "用户想学新知识"),
        )
        result = await router.route("我想学习颞下颌关节紊乱的诊断", "demo")

        assert result["ok"] is True
        assert result["route"]["intent"] == "learning"
        mock_orchestrator.start_lesson.assert_called_once()
        call_kwargs = mock_orchestrator.start_lesson.call_args.kwargs
        assert "颞下颌关节紊乱" in call_kwargs["topic"]

    @pytest.mark.asyncio
    async def test_learning_downstream_error_passthrough(self, mock_orchestrator):
        """Downstream failure must be a real error, not faked ok."""
        mock_orchestrator.start_lesson.side_effect = RuntimeError("KAG unavailable")
        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier("learning", "ok"),
        )
        result = await router.route("学点东西", "demo")

        assert result["ok"] is False
        assert "KAG unavailable" in result["error"]
        assert result["final_result"] is None

    @pytest.mark.asyncio
    async def test_existing_session_continue_returns_stage_exam_in_chat(
        self, mock_orchestrator
    ):
        mock_orchestrator.continue_to_next_section.return_value = {
            "lesson_completed": True,
            "stage_exam": {
                "status": "ready",
                "summary": {"question_count": 1, "section_count": 1},
                "questions": [{"id": "q1", "stem": "Stem", "options": ["A. One"]}],
            },
        }
        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier("learning", "should not be used"),
        )

        result = await router.route(
            "继续",
            "demo",
            context={"session_id": "sess-existing"},
        )

        assert result["ok"] is True
        assert result["route"]["source"] == "session_context"
        assert result["next_action"] == "show_stage_exam"
        assert result["final_result"]["stage_exam"]["questions"][0]["stem"] == "Stem"
        mock_orchestrator.continue_to_next_section.assert_called_once_with(
            "sess-existing"
        )
        mock_orchestrator.start_lesson.assert_not_called()

    # -- candidate matching (moved from study_plan to learning) --------------

    @pytest.mark.asyncio
    async def test_single_candidate_routes_to_plan_item(
        self, mock_orchestrator, sample_plan_item
    ):
        """Single open plan item → deterministic dispatch via _handle_plan_item."""
        mock_orchestrator.memory.list_open_study_plan_items.return_value = [
            sample_plan_item
        ]
        mock_orchestrator.memory.get_study_plan_item.return_value = sample_plan_item
        mock_orchestrator.start_lesson.return_value = {
            "session": {"id": "sess-plan"},
            "outline": {"nodes": []},
        }
        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier(
                "learning",
                "用户想学习已有计划任务",
                plan_item_requested=True,
            ),
        )
        result = await router.route("学 Test Topic Alpha", "demo")

        assert result["ok"] is True
        assert result["route"]["source"] == "explicit_plan_item"
        assert result["route"]["intent"] == "learning"
        assert result["next_action"] == "start_session"
        mock_orchestrator.memory.get_study_plan_item.assert_called_once_with(
            "pi-test-001", user_id="demo"
        )
        mock_orchestrator.start_lesson.assert_called_once()
        call_kwargs = mock_orchestrator.start_lesson.call_args.kwargs
        assert call_kwargs["topic"] == "Linked Course Title Alpha"

    @pytest.mark.asyncio
    async def test_multiple_candidates_no_match_returns_select_plan(
        self, mock_orchestrator
    ):
        """Multiple candidates + no LLM match → select_plan with whitelist."""
        items = [
            {"id": "pi-a", "title": "Task A", "plan_date": "2026-07-12",
             "linked_course_title": "Course A", "status": "open", "user_id": "demo",
             "extra_field": "should be stripped"},
            {"id": "pi-b", "title": "Task B", "plan_date": "2026-07-12",
             "linked_course_title": None, "status": "open", "user_id": "demo",
             "internal_note": "secret"},
        ]
        mock_orchestrator.memory.list_open_study_plan_items.return_value = items

        # plan_matcher returns None → no match
        def _no_match(_msg, _candidates):
            return None

        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier(
                "learning",
                "用户想完成已有学习计划",
                plan_item_requested=True,
            ),
            plan_matcher=_no_match,
        )
        result = await router.route("我想学点东西", "demo")

        assert result["ok"] is True
        assert result["route"]["intent"] == "learning"
        assert result["next_action"] == "select_plan"
        assert "candidates" in result["final_result"]
        cands = result["final_result"]["candidates"]
        assert len(cands) == 2

        # Field whitelist: only plan_item_id, title, linked_course_title
        for c in cands:
            for key in c:
                assert key in CANDIDATE_WHITELIST_FIELDS, (
                    f"Field {key!r} not in whitelist {CANDIDATE_WHITELIST_FIELDS}"
                )
            assert "id" not in c
            assert "plan_date" not in c
            assert "extra_field" not in c
            assert "internal_note" not in c
            assert "status" not in c  # internal field, not in whitelist

        # Must NOT start lesson
        mock_orchestrator.start_lesson.assert_not_called()

    @pytest.mark.asyncio
    async def test_multiple_candidates_llm_match_starts_lesson(
        self, mock_orchestrator, sample_plan_item
    ):
        """Multiple candidates + valid LLM match → starts the matched lesson."""
        items = [
            {"id": "pi-x", "title": "Wrong Task", "plan_date": "2026-07-12",
             "linked_course_title": None, "status": "open", "user_id": "demo"},
            sample_plan_item,  # pi-test-001
        ]
        mock_orchestrator.memory.list_open_study_plan_items.return_value = items
        mock_orchestrator.memory.get_study_plan_item.return_value = sample_plan_item
        mock_orchestrator.start_lesson.return_value = {
            "session": {"id": "sess-matched"},
            "outline": {"nodes": []},
        }

        # plan_matcher returns the correct ID
        def _match(_msg, _candidates):
            return "pi-test-001"

        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier(
                "learning",
                "用户想完成已有学习计划",
                plan_item_requested=True,
            ),
            plan_matcher=_match,
        )
        result = await router.route("学 Test Topic Alpha", "demo")

        assert result["ok"] is True
        assert result["next_action"] == "start_session"
        mock_orchestrator.start_lesson.assert_called_once()

    @pytest.mark.asyncio
    async def test_plan_matcher_hallucination_falls_back(
        self, mock_orchestrator
    ):
        """LLM returns ID not in candidate set → hallucination guard → select_plan."""
        items = [
            {"id": "pi-a", "title": "Task A", "plan_date": "2026-07-12",
             "linked_course_title": "Course A", "status": "open", "user_id": "demo"},
            {"id": "pi-b", "title": "Task B", "plan_date": "2026-07-12",
             "linked_course_title": None, "status": "open", "user_id": "demo"},
        ]
        mock_orchestrator.memory.list_open_study_plan_items.return_value = items

        # plan_matcher returns an ID NOT in the candidate set (hallucination)
        def _hallucinate(_msg, _candidates):
            return "pi-evil-not-in-set"

        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier(
                "learning",
                "用户想完成已有学习计划",
                plan_item_requested=True,
            ),
            plan_matcher=_hallucinate,
        )
        result = await router.route("学点东西", "demo")

        assert result["ok"] is True
        assert result["next_action"] == "select_plan"
        cands = result["final_result"]["candidates"]
        assert len(cands) == 2
        # Must NOT start lesson on hallucinated ID
        mock_orchestrator.start_lesson.assert_not_called()

    @pytest.mark.asyncio
    async def test_plan_matcher_exception_falls_back(
        self, mock_orchestrator
    ):
        """plan_matcher raises → fallback to select_plan."""
        items = [
            {"id": "pi-a", "title": "Task A", "plan_date": "2026-07-12",
             "linked_course_title": "Course A", "status": "open", "user_id": "demo"},
            {"id": "pi-b", "title": "Task B", "plan_date": "2026-07-12",
             "linked_course_title": "Course B", "status": "open", "user_id": "demo"},
        ]
        mock_orchestrator.memory.list_open_study_plan_items.return_value = items

        def _crash(_msg, _candidates):
            raise RuntimeError("matcher crash")

        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier(
                "learning",
                "用户想完成已有学习计划",
                plan_item_requested=True,
            ),
            plan_matcher=_crash,
        )
        result = await router.route("学点东西", "demo")

        assert result["ok"] is True
        assert result["next_action"] == "select_plan"
        mock_orchestrator.start_lesson.assert_not_called()

    @pytest.mark.asyncio
    async def test_list_open_failure_falls_back_to_free_study(
        self, mock_orchestrator
    ):
        """list_open_study_plan_items raises → fall back to free study."""
        mock_orchestrator.memory.list_open_study_plan_items.side_effect = (
            RuntimeError("DB unavailable")
        )
        mock_orchestrator.start_lesson.return_value = {
            "session": {"id": "sess-fallback"},
            "outline": {"nodes": []},
        }
        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier(
                "learning",
                "用户想完成已有学习计划",
                plan_item_requested=True,
            ),
        )
        result = await router.route("我想学牙周病", "demo")

        assert result["ok"] is True
        assert result["next_action"] == "start_session"
        mock_orchestrator.start_lesson.assert_called_once()


# ---------------------------------------------------------------------------
# Study plan intent
# ---------------------------------------------------------------------------


class TestStudyPlanIntent:
    @pytest.mark.asyncio
    async def test_study_plan_calls_recommend_service(
        self, mock_orchestrator
    ):
        """study_plan intent → calls recommend_study_plan and returns result."""
        expected = {
            "date": date.today().isoformat(),
            "recommendations": [
                {"type": "continue", "title": "继续学习口腔解剖"},
            ],
        }
        captured_request_text: dict = {}

        def _mock_study_plan(memory, *, user_id, plan_date, max_items=4, request_text=None):
            captured_request_text["value"] = request_text
            return expected

        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier("study_plan", "用户想获取学习计划"),
            study_plan_service=_mock_study_plan,
        )
        message = "帮我制定三天复习《牙周病学》第三章，每天30分钟"
        result = await router.route(message, "demo")

        assert result["ok"] is True
        assert result["route"]["intent"] == "study_plan"
        assert result["next_action"] == "show_recommendations"
        assert result["final_result"] == expected
        assert captured_request_text.get("value") == message
        # Must NOT start a lesson
        mock_orchestrator.start_lesson.assert_not_called()

    @pytest.mark.asyncio
    async def test_study_plan_service_error(
        self, mock_orchestrator
    ):
        """study_plan_service raises → return error with ok=False."""

        def _failing(_memory, *, user_id, plan_date, max_items=4, request_text=None):
            raise RuntimeError("DB unavailable")

        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier("study_plan", "ok"),
            study_plan_service=_failing,
        )
        result = await router.route("今天学什么", "demo")

        assert result["ok"] is False
        assert "DB unavailable" in result["error"] or "学习计划推荐失败" in result["error"]
        assert result["final_result"] is None
        mock_orchestrator.start_lesson.assert_not_called()

    @pytest.mark.asyncio
    async def test_study_plan_request_text_passthrough_second_message(
        self, mock_orchestrator
    ):
        """A second structurally different message also passes through as request_text."""
        captured_request_text: dict = {}

        def _mock_study_plan(memory, *, user_id, plan_date, max_items=4, request_text=None):
            captured_request_text["value"] = request_text
            return {"recommendations": []}

        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier("study_plan", "用户想获取学习计划"),
            study_plan_service=_mock_study_plan,
        )
        message = "本周复习薄弱点"
        result = await router.route(message, "demo")

        assert result["ok"] is True
        assert result["route"]["intent"] == "study_plan"
        assert captured_request_text.get("value") == message

    @pytest.mark.asyncio
    async def test_non_study_plan_does_not_call_study_plan_service(
        self, mock_orchestrator
    ):
        """learning/literature/direct_qa paths must NOT call study_plan_service."""
        mock_orchestrator.start_lesson.return_value = {
            "session": {"id": "sess-learning"},
            "outline": {"nodes": []},
        }
        study_plan_called = []

        def _mock_study_plan(memory, *, user_id, plan_date, max_items=4, request_text=None):
            study_plan_called.append(True)
            return {"recommendations": []}

        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier("learning", "用户想学习"),
            study_plan_service=_mock_study_plan,
        )
        result = await router.route("我想学习口腔溃疡", "demo")

        assert result["ok"] is True
        assert result["route"]["intent"] == "learning"
        assert len(study_plan_called) == 0


# ---------------------------------------------------------------------------
# Literature intent
# ---------------------------------------------------------------------------


class TestLiteratureIntent:
    @pytest.mark.asyncio
    async def test_literature_result_passthrough(self, mock_orchestrator):
        """Literature results must be passed through from the real service."""
        lit_result = {
            "ok": True,
            "module": "frontier_literature",
            "query": "oral cancer biomarkers",
            "terms": {"recommended_keywords": "oral cancer"},
            "articles": [
                {"title": "Novel Biomarker X", "source": "PubMed"}
            ],
            "review": "Recent findings suggest...",
            "sources": ["PubMed"],
            "errors": [],
        }
        mock_lit_service = MagicMock()
        mock_lit_service.return_value = lit_result

        # Must be async-compatible
        async def _lit_mock(*args, **kwargs):
            return lit_result

        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier("literature", "用户需要查文献"),
            literature_service=_lit_mock,
        )
        result = await router.route("检索口腔癌标志物最新文献", "demo")

        assert result["ok"] is True
        assert result["route"]["intent"] == "literature"
        assert result["next_action"] == "show_literature"
        assert result["final_result"]["articles"][0]["title"] == "Novel Biomarker X"
        # Must NOT start a learning session
        mock_orchestrator.start_lesson.assert_not_called()

    @pytest.mark.asyncio
    async def test_literature_fallback_without_llm(self, mock_orchestrator):
        """Obvious literature requests should still work when router LLM is absent."""
        seen: dict[str, Any] = {}

        async def _lit_mock(*args, **kwargs):
            seen["args"] = args
            seen["kwargs"] = kwargs
            return {
                "ok": True,
                "module": "frontier_literature",
                "query": args[0],
                "terms": {"recommended_months": 3},
                "articles": [],
                "review": "",
                "sources": [],
                "errors": [],
            }

        router = MasterRouter(mock_orchestrator, literature_service=_lit_mock)

        with patch("backend.services.router.master_router.is_llm_configured", return_value=False):
            result = await router.route("我想要阅读阻生牙近三个月的文献", "demo")

        assert result["ok"] is True
        assert result["route"]["intent"] == "literature"
        assert result["route"]["source"] == "deterministic_literature_route"
        assert result["next_action"] == "show_literature"
        assert seen["args"] == ("我想要阅读阻生牙近三个月的文献", None, 5)
        assert seen["kwargs"] == {"include_chat_translations": True}
        mock_orchestrator.start_lesson.assert_not_called()

    @pytest.mark.asyncio
    async def test_temporal_text_alone_does_not_trigger_literature_fallback(self, mock_orchestrator):
        """A time window alone is not enough to route into literature mode."""
        router = MasterRouter(mock_orchestrator)

        with patch("backend.services.router.master_router.is_llm_configured", return_value=False):
            result = await router.route("查看近三个月学习计划", "demo")

        assert result["ok"] is True
        assert result["route"]["intent"] == "clarification"
        mock_orchestrator.start_lesson.assert_not_called()

    @pytest.mark.asyncio
    async def test_literature_fallback_overrides_llm_clarification(self, mock_orchestrator):
        """High-confidence literature requests should survive weak LLM clarification."""
        seen: dict[str, Any] = {}

        async def _lit_mock(*args, **kwargs):
            seen["args"] = args
            seen["kwargs"] = kwargs
            return {
                "ok": True,
                "module": "frontier_literature",
                "query": args[0],
                "terms": {"recommended_months": 3},
                "articles": [],
                "review": "",
                "sources": [],
                "errors": [],
            }

        router = MasterRouter(mock_orchestrator, literature_service=_lit_mock)

        with patch("backend.services.router.master_router.is_llm_configured", return_value=True), patch(
            "backend.services.router.master_router.chat_completion",
            return_value='{"intent":"clarification","plan_item_requested":false,"topic":"","reason":"不确定"}',
        ):
            result = await router.route("我想要阅读阻生牙近三个月的文献", "demo")

        assert result["ok"] is True
        assert result["route"]["intent"] == "literature"
        assert result["route"]["source"] == "deterministic_literature_route"
        assert seen["args"] == ("我想要阅读阻生牙近三个月的文献", None, 5)
        assert seen["kwargs"] == {"include_chat_translations": True}

    @pytest.mark.asyncio
    async def test_literature_handler_passes_context_options(self, mock_orchestrator):
        """Chat context months/limit must reach the literature service."""
        seen: dict[str, Any] = {}

        async def _lit_mock(*args, **kwargs):
            seen["args"] = args
            seen["kwargs"] = kwargs
            return {
                "ok": True,
                "module": "frontier_literature",
                "query": args[0],
                "terms": {"recommended_months": args[1]},
                "articles": [],
                "review": "",
                "sources": [],
                "errors": [],
            }

        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier("literature", "用户需要查文献"),
            literature_service=_lit_mock,
        )

        result = await router.route(
            "检索阻生牙文献",
            "demo",
            context={"months": 3, "limit": 8},
        )

        assert result["ok"] is True
        assert seen["args"] == ("检索阻生牙文献", 3, 8)
        assert seen["kwargs"] == {"include_chat_translations": True}

    @pytest.mark.asyncio
    async def test_literature_handler_extracts_limit_from_message(self, mock_orchestrator):
        """A request like '前 7 篇' should not be forced back to the default 5."""
        seen: dict[str, Any] = {}

        async def _lit_mock(*args, **kwargs):
            seen["args"] = args
            seen["kwargs"] = kwargs
            return {
                "ok": True,
                "module": "frontier_literature",
                "query": args[0],
                "terms": {},
                "articles": [],
                "review": "",
                "sources": [],
                "errors": [],
            }

        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier("literature", "用户需要查文献"),
            literature_service=_lit_mock,
        )

        result = await router.route("帮我检索阻生牙前7篇文献", "demo")

        assert result["ok"] is True
        assert seen["args"] == ("帮我检索阻生牙前7篇文献", None, 7)
        assert seen["kwargs"] == {"include_chat_translations": True}

    @pytest.mark.asyncio
    async def test_literature_service_error(self, mock_orchestrator):
        """Literature service failure must be a real error."""

        async def _failing_lit(*args, **kwargs):
            raise RuntimeError("API timeout")

        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier("literature", "ok"),
            literature_service=_failing_lit,
        )
        result = await router.route("查文献", "demo")

        assert result["ok"] is False
        assert "API timeout" in result["error"]


# ---------------------------------------------------------------------------
# Deterministic fallback routing
# ---------------------------------------------------------------------------


class TestDeterministicFallbackRouting:
    @pytest.mark.asyncio
    async def test_explicit_learning_fallback_without_llm(self, mock_orchestrator):
        """Obvious learning requests should still start the learning chain."""
        mock_orchestrator.start_lesson.return_value = {
            "session": {"id": "sess-caries", "topic": "龋病", "user_id": "demo"},
            "outline": {"title": "龋病", "nodes": [{"id": "node-1", "title": "概念"}]},
            "mindmap": {"root": {"name": "龋病", "children": []}},
            "current_outline_node_id": "node-1",
        }
        router = MasterRouter(mock_orchestrator)

        with patch("backend.services.router.master_router.is_llm_configured", return_value=False):
            result = await router.route("我想学习龋病", "demo")

        assert result["ok"] is True
        assert result["route"]["intent"] == "learning"
        assert result["route"]["source"] == "deterministic_learning_route"
        assert result["next_action"] == "start_session"
        assert mock_orchestrator.start_lesson.call_args.kwargs["topic"] == "龋病"

    @pytest.mark.asyncio
    async def test_explicit_learning_fallback_overrides_llm_clarification(self, mock_orchestrator):
        """Weak LLM clarification must not block a clear learning request."""
        mock_orchestrator.start_lesson.return_value = {
            "session": {"id": "sess-caries", "topic": "龋病", "user_id": "demo"},
            "outline": {"title": "龋病", "nodes": [{"id": "node-1", "title": "概念"}]},
            "mindmap": {"root": {"name": "龋病", "children": []}},
            "current_outline_node_id": "node-1",
        }
        router = MasterRouter(mock_orchestrator)

        with patch("backend.services.router.master_router.is_llm_configured", return_value=True), patch(
            "backend.services.router.master_router.chat_completion",
            return_value='{"intent":"clarification","plan_item_requested":false,"topic":"","reason":"不确定"}',
        ):
            result = await router.route("我想学习龋病", "demo")

        assert result["ok"] is True
        assert result["route"]["intent"] == "learning"
        assert result["route"]["source"] == "deterministic_learning_route"
        assert mock_orchestrator.start_lesson.call_args.kwargs["topic"] == "龋病"

    @pytest.mark.asyncio
    async def test_learning_plan_text_does_not_trigger_learning_fallback(self, mock_orchestrator):
        router = MasterRouter(mock_orchestrator)

        with patch("backend.services.router.master_router.is_llm_configured", return_value=False):
            result = await router.route("查看近三个月学习计划", "demo")

        assert result["ok"] is True
        assert result["route"]["intent"] == "clarification"
        mock_orchestrator.start_lesson.assert_not_called()


# ---------------------------------------------------------------------------
# Clarification
# ---------------------------------------------------------------------------


class TestClarification:
    @pytest.mark.asyncio
    async def test_llm_classifies_as_clarification(self, mock_orchestrator):
        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier("clarification", "意图不明"),
        )
        result = await router.route("嗯...", "demo")

        assert result["ok"] is True
        assert result["route"]["intent"] == "clarification"
        assert result["next_action"] == "select_plan"
        # Should not call any downstream service
        mock_orchestrator.start_lesson.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_classifier_output_falls_back_to_clarification(
        self, mock_orchestrator
    ):
        """When the injected classifier returns garbage, fall back."""
        router = MasterRouter(
            mock_orchestrator,
            classifier=lambda m, h, c: {"intent": "chat", "target": "x"},
        )
        result = await router.route("any message", "demo")

        assert result["route"]["intent"] == "clarification"
        assert result["route"]["source"] == "llm_router"
        mock_orchestrator.start_lesson.assert_not_called()

    @pytest.mark.asyncio
    async def test_classifier_raises_falls_back(self, mock_orchestrator):
        """When the injected classifier raises, fall back to clarification."""
        router = MasterRouter(
            mock_orchestrator,
            classifier=lambda m, h, c: (_ for _ in ()).throw(ValueError("boom")),
        )
        result = await router.route("hello", "demo")

        assert result["route"]["intent"] == "clarification"
        mock_orchestrator.start_lesson.assert_not_called()


# ---------------------------------------------------------------------------
# Explicit plan_item_id
# ---------------------------------------------------------------------------


class TestExplicitPlanItem:
    @pytest.mark.asyncio
    async def test_valid_plan_item_starts_lesson(
        self, mock_orchestrator, sample_plan_item
    ):
        """Explicit plan_item_id with linked_course_title starts lesson."""
        mock_orchestrator.memory.get_study_plan_item.return_value = sample_plan_item
        mock_orchestrator.start_lesson.return_value = {
            "session": {"id": "sess-plan"},
            "outline": {"nodes": []},
        }
        router = MasterRouter(mock_orchestrator)

        result = await router.route(
            "ignored message",
            "demo",
            context={"plan_item_id": "pi-test-001", "user_id": "demo"},
        )

        assert result["ok"] is True
        assert result["route"]["source"] == "explicit_plan_item"
        assert result["route"]["intent"] == "learning"
        assert result["next_action"] == "start_session"
        mock_orchestrator.start_lesson.assert_called_once()
        call_kwargs = mock_orchestrator.start_lesson.call_args.kwargs
        # Priority: linked_course_title over title
        assert call_kwargs["topic"] == "Linked Course Title Alpha"

    @pytest.mark.asyncio
    async def test_plan_item_fallback_to_title(
        self, mock_orchestrator
    ):
        """When linked_course_title is empty, fall back to title."""
        item = {
            "id": "pi-test-002",
            "user_id": "demo",
            "plan_date": date.today().isoformat(),
            "title": "Fallback Title Only",
            "status": "open",
            "linked_course_id": None,
            "linked_course_title": None,
            "kg_node_ids_json": "[]",
        }
        mock_orchestrator.memory.get_study_plan_item.return_value = item
        mock_orchestrator.start_lesson.return_value = {
            "session": {"id": "sess-fb"},
        }
        router = MasterRouter(mock_orchestrator)

        result = await router.route(
            "", "demo",
            context={"plan_item_id": "pi-test-002"},
        )

        assert result["ok"] is True
        call_kwargs = mock_orchestrator.start_lesson.call_args.kwargs
        assert call_kwargs["topic"] == "Fallback Title Only"

    @pytest.mark.asyncio
    async def test_non_owner_plan_item_rejected(self, mock_orchestrator):
        """Plan item not belonging to user → rejected (KeyError)."""
        mock_orchestrator.memory.get_study_plan_item.side_effect = KeyError(
            "Study plan item not found: pi-other"
        )
        router = MasterRouter(mock_orchestrator)

        result = await router.route(
            "", "user-b",
            context={"plan_item_id": "pi-other"},
        )

        assert result["ok"] is False
        assert "not found" in result["error"].lower() or "access denied" in result["error"].lower()
        mock_orchestrator.start_lesson.assert_not_called()

    @pytest.mark.asyncio
    async def test_completed_plan_item_rejected(self, mock_orchestrator):
        """Completed plan item must NOT start a lesson."""
        item = {
            "id": "pi-done",
            "user_id": "demo",
            "title": "Completed Task",
            "status": "completed",
            "linked_course_title": "Some Course",
        }
        mock_orchestrator.memory.get_study_plan_item.return_value = item
        router = MasterRouter(mock_orchestrator)

        result = await router.route(
            "", "demo",
            context={"plan_item_id": "pi-done"},
        )

        assert result["ok"] is False
        assert "completed" in result["error"] or "completed" in result["route"]["reason"]
        mock_orchestrator.start_lesson.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_topic_plan_item_rejected(self, mock_orchestrator):
        """Plan item with no resolvable topic → error."""
        item = {
            "id": "pi-empty",
            "user_id": "demo",
            "title": "",
            "status": "open",
            "linked_course_title": None,
        }
        mock_orchestrator.memory.get_study_plan_item.return_value = item
        router = MasterRouter(mock_orchestrator)

        result = await router.route(
            "", "demo",
            context={"plan_item_id": "pi-empty"},
        )

        assert result["ok"] is False
        assert "没有可解析的学习主题" in result["route"]["reason"]
        mock_orchestrator.start_lesson.assert_not_called()


# ---------------------------------------------------------------------------
# Intent constants
# ---------------------------------------------------------------------------


class TestStructuredLearningTasks:
    def test_free_learning_normalizes_conversational_topic(self, mock_orchestrator):
        mock_orchestrator.start_lesson.return_value = {
            "session": {"id": "sess-normalized"},
            "outline": {"nodes": []},
        }
        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier("learning", "用户想学习"),
        )

        result = asyncio.run(router.route("我想学习我想学习口腔溃疡", "demo"))

        assert result["ok"] is True
        assert mock_orchestrator.start_lesson.call_args.kwargs["topic"] == "口腔溃疡"

    def test_weakness_plan_item_uses_focused_learning_target(self, mock_orchestrator):
        mock_orchestrator.memory.get_study_plan_item.return_value = {
            "id": "pi-weakness",
            "user_id": "demo",
            "title": "复习薄弱点：宿主-微生物互作机制理解缺失",
            "status": "open",
            "task_type": "review_weakness",
            "learning_target": "宿主-微生物互作机制理解缺失",
            "linked_course_title": "我想学习口腔溃疡",
        }
        mock_orchestrator.start_lesson.return_value = {
            "session": {"id": "sess-focused"},
            "outline": {"nodes": []},
        }
        router = MasterRouter(mock_orchestrator)

        result = asyncio.run(
            router.route(
                "",
                "demo",
                context={"plan_item_id": "pi-weakness"},
            )
        )

        assert result["ok"] is True
        assert result["route"]["task_type"] == "review_weakness"
        assert mock_orchestrator.start_lesson.call_args.kwargs["topic"] == (
            "口腔溃疡：宿主-微生物互作机制理解缺失"
        )

    def test_continue_task_resumes_linked_session(self, mock_orchestrator):
        """continue_lesson plan item start → calls next_step (current node),
        NOT continue_to_next_section (advances to next node)."""
        mock_orchestrator.memory.get_study_plan_item.return_value = {
            "id": "pi-continue",
            "user_id": "demo",
            "title": "继续学习：口腔溃疡",
            "status": "open",
            "task_type": "continue_lesson",
            "learning_target": "下一节",
            "linked_course_id": "sess-existing",
            "linked_course_title": "口腔溃疡",
        }
        mock_orchestrator.next_step.return_value = {
            "session_id": "sess-existing",
            "outline_node_id": "node-current",
            "teaching_text": "这是当前节点的讲稿内容。",
            "questions": [],
            "content_status": "llm_generated",
        }
        router = MasterRouter(mock_orchestrator)

        result = asyncio.run(
            router.route(
                "",
                "demo",
                context={"plan_item_id": "pi-continue"},
            )
        )

        assert result["ok"] is True
        # Must call next_step (current node), NOT continue_to_next_section
        mock_orchestrator.next_step.assert_called_once_with("sess-existing")
        mock_orchestrator.continue_to_next_section.assert_not_called()
        mock_orchestrator.start_lesson.assert_not_called()
        assert result["route"]["source"] == "explicit_plan_item"
        assert result["next_action"] == "continue_session"
        # The next_step result is wrapped in final_result.tutor_step
        assert result["final_result"]["tutor_step"]["teaching_text"] == "这是当前节点的讲稿内容。"
        assert result["final_result"]["session"]["id"] == "sess-existing"
        assert result["final_result"]["lesson_completed"] is False

    def test_continue_lesson_last_node_still_calls_next_step(self, mock_orchestrator):
        """Even when the current node is the last one, continue_lesson plan
        item start calls next_step (not continue_to_next_section), so
        lesson_completed is NOT caused by advancement."""
        mock_orchestrator.memory.get_study_plan_item.return_value = {
            "id": "pi-continue-last",
            "user_id": "demo",
            "title": "继续学习（最后一节）",
            "status": "open",
            "task_type": "continue_lesson",
            "learning_target": "最后一节",
            "linked_course_id": "sess-last-node",
            "linked_course_title": "牙周炎总复习",
        }
        # next_step on the last node still returns the lecture for that node
        mock_orchestrator.next_step.return_value = {
            "session_id": "sess-last-node",
            "outline_node_id": "node-last",
            "teaching_text": "最后一节的讲稿内容。",
            "questions": [],
            "content_status": "llm_generated",
        }
        router = MasterRouter(mock_orchestrator)

        result = asyncio.run(
            router.route(
                "",
                "demo",
                context={"plan_item_id": "pi-continue-last"},
            )
        )

        assert result["ok"] is True
        # Must call next_step, NOT continue_to_next_section
        mock_orchestrator.next_step.assert_called_once_with("sess-last-node")
        mock_orchestrator.continue_to_next_section.assert_not_called()
        # lesson_completed must be False (not set by next_step)
        assert result["final_result"]["lesson_completed"] is False
        assert result["next_action"] == "continue_session"

    def test_continue_lesson_next_step_error_propagates(self, mock_orchestrator):
        """When next_step fails for a continue_lesson plan item, the error
        is returned clearly — not silently swallowed."""
        mock_orchestrator.memory.get_study_plan_item.return_value = {
            "id": "pi-continue-err",
            "user_id": "demo",
            "title": "继续学习（异常）",
            "status": "open",
            "task_type": "continue_lesson",
            "learning_target": "",
            "linked_course_id": "sess-gone",
            "linked_course_title": "失效课程",
        }
        mock_orchestrator.next_step.side_effect = KeyError(
            "Session not found: sess-gone"
        )
        router = MasterRouter(mock_orchestrator)

        result = asyncio.run(
            router.route(
                "",
                "demo",
                context={"plan_item_id": "pi-continue-err"},
            )
        )

        assert result["ok"] is False
        assert "继续课程失败" in result.get("error", "")
        assert result["final_result"] is None
        mock_orchestrator.next_step.assert_called_once_with("sess-gone")
        mock_orchestrator.continue_to_next_section.assert_not_called()

    def test_continue_lesson_does_not_call_plan_completion(self, mock_orchestrator):
        """continue_lesson plan item start must NOT call
        complete_study_plan_item or any plan update/extension method."""
        mock_orchestrator.memory.get_study_plan_item.return_value = {
            "id": "pi-continue-no-complete",
            "user_id": "demo",
            "title": "继续学习：牙周病学",
            "status": "open",
            "task_type": "continue_lesson",
            "learning_target": "",
            "linked_course_id": "sess-no-complete",
            "linked_course_title": "牙周病学",
        }
        mock_orchestrator.next_step.return_value = {
            "session_id": "sess-no-complete",
            "outline_node_id": "node-1",
            "teaching_text": "讲稿内容。",
            "questions": [],
            "content_status": "llm_generated",
        }
        router = MasterRouter(mock_orchestrator)

        result = asyncio.run(
            router.route(
                "",
                "demo",
                context={"plan_item_id": "pi-continue-no-complete"},
            )
        )

        assert result["ok"] is True
        # Verify no plan-item mutation methods were called
        for forbidden in ("complete_study_plan_item", "update_study_plan_item",
                          "extend_study_plan_item", "mark_plan_item_done"):
            method = getattr(mock_orchestrator.memory, forbidden, None)
            if method is not None:
                method.assert_not_called()

    def test_session_context_continue_still_advances(self, mock_orchestrator):
        """Regular session-context '继续' (with context.session_id) MUST
        still call continue_to_next_section — this is the normal
        interaction path and must not be broken."""
        mock_orchestrator.continue_to_next_section.return_value = {
            "session_id": "sess-reg",
            "outline_node_id": "node-next",
            "teaching_text": "下一节的内容。",
            "questions": [],
            "lesson_completed": False,
        }
        router = MasterRouter(mock_orchestrator)

        result = asyncio.run(
            router.route(
                "继续",
                "demo",
                context={"session_id": "sess-reg"},
            )
        )

        assert result["ok"] is True
        assert result["route"]["source"] == "session_context"
        mock_orchestrator.continue_to_next_section.assert_called_once_with(
            "sess-reg"
        )
        mock_orchestrator.next_step.assert_not_called()

    def test_recommendation_id_is_resolved_server_side(self, mock_orchestrator):
        def _recommend(_memory, *, user_id, plan_date, max_items=4):
            return {
                "plan_date": plan_date,
                "recommendations": [
                    {
                        "recommendation_id": "weakness:host-microbe",
                        "title": "复习薄弱点：宿主-微生物互作机制理解缺失",
                        "task_type": "review_weakness",
                        "learning_target": "宿主-微生物互作机制理解缺失",
                        "linked_course_title": "口腔溃疡",
                    }
                ],
            }

        mock_orchestrator.start_lesson.return_value = {
            "session": {"id": "sess-recommendation"},
            "outline": {"nodes": []},
        }
        router = MasterRouter(mock_orchestrator, study_plan_service=_recommend)

        result = asyncio.run(
            router.route(
                "",
                "demo",
                context={
                    "recommendation_id": "weakness:host-microbe",
                    "plan_date": date.today().isoformat(),
                },
            )
        )

        assert result["ok"] is True
        assert result["route"]["source"] == "explicit_recommendation"
        assert mock_orchestrator.start_lesson.call_args.kwargs["topic"] == (
            "口腔溃疡：宿主-微生物互作机制理解缺失"
        )


class TestIntentConstants:
    def test_all_intents_have_target_mapping(self):
        for intent in VALID_INTENTS:
            assert intent in INTENT_TO_TARGET, f"Missing target for {intent}"

    def test_all_intents_have_next_action_mapping(self):
        for intent in VALID_INTENTS:
            assert intent in INTENT_TO_NEXT_ACTION, f"Missing next_action for {intent}"


# ---------------------------------------------------------------------------
# API-layer contract tests — schema → endpoint → route
# ---------------------------------------------------------------------------


def _build_test_client_and_mock():
    """Build a TestClient app that mirrors the real /api/router/chat endpoint."""
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient as _TC

    router_mock = MagicMock()
    router_mock.route = AsyncMock(return_value={
        "ok": True, "route": {"intent": "clarification"}, "final_result": {},
    })

    app = FastAPI()

    @app.post("/api/router/chat")
    async def router_chat(req: RouterChatRequest):
        result = await router_mock.route(
            message=req.message,
            user_id=req.context.get("user_id", "demo"),
            history=req.history,
            context=req.context,
        )
        return JSONResponse(content=result)

    return _TC(app), router_mock


class TestEndpointContract:
    """Prove the real request→endpoint→route contract with TestClient.

    These tests exercise the exact endpoint logic from main.py
    (RouterChatRequest → extract defaults → MasterRouter.route), with a
    mocked router so no real downstream, external LLM, or network is hit.
    """

    # -- minimum input ---------------------------------------------------------

    def test_minimal_message_only(self):
        """Simple message-only request reaches route with schema defaults."""
        client, mock_router = _build_test_client_and_mock()

        response = client.post("/api/router/chat", json={
            "message": "hello, I want to learn something new",
        })

        assert response.status_code == 200, f"422 detail: {response.json()}"
        mock_router.route.assert_called_once()
        kwargs = mock_router.route.call_args.kwargs
        assert kwargs["message"] == "hello, I want to learn something new"
        assert kwargs["user_id"] == "demo"
        assert kwargs["history"] == []
        assert kwargs["context"] == {"user_id": "demo"}

    def test_different_message_generalized(self):
        """Prove no topic-specific hardcoding — a different message works too."""
        client, mock_router = _build_test_client_and_mock()

        response = client.post("/api/router/chat", json={
            "message": "find the latest dental research for me",
        })

        assert response.status_code == 200, f"422 detail: {response.json()}"
        kwargs = mock_router.route.call_args.kwargs
        assert kwargs["message"] == "find the latest dental research for me"
        assert kwargs["user_id"] == "demo"
        assert kwargs["context"] == {"user_id": "demo"}

    def test_empty_request_body(self):
        """Empty JSON body uses all schema defaults safely."""
        client, mock_router = _build_test_client_and_mock()

        response = client.post("/api/router/chat", json={})

        assert response.status_code == 200, f"422 detail: {response.json()}"
        kwargs = mock_router.route.call_args.kwargs
        assert kwargs["message"] == ""
        assert kwargs["user_id"] == "demo"
        assert kwargs["history"] == []
        assert kwargs["context"] == {"user_id": "demo"}

    # -- explicit context preservation -----------------------------------------

    def test_explicit_user_id_preserved(self):
        """Explicit context.user_id overrides the default."""
        client, mock_router = _build_test_client_and_mock()

        response = client.post("/api/router/chat", json={
            "message": "test",
            "context": {"user_id": "explicit-user-42"},
        })

        assert response.status_code == 200, f"422 detail: {response.json()}"
        kwargs = mock_router.route.call_args.kwargs
        assert kwargs["user_id"] == "explicit-user-42"
        assert kwargs["context"]["user_id"] == "explicit-user-42"

    def test_explicit_session_id_preserved(self):
        """Explicit context.session_id is preserved; user_id falls back to default."""
        client, mock_router = _build_test_client_and_mock()

        response = client.post("/api/router/chat", json={
            "message": "test",
            "context": {"session_id": "sess-abc-123"},
        })

        assert response.status_code == 200, f"422 detail: {response.json()}"
        kwargs = mock_router.route.call_args.kwargs
        assert kwargs["context"]["session_id"] == "sess-abc-123"
        assert kwargs["user_id"] == "demo"

    def test_explicit_plan_item_id_preserved(self):
        """Explicit context.plan_item_id is preserved."""
        client, mock_router = _build_test_client_and_mock()

        response = client.post("/api/router/chat", json={
            "message": "test",
            "context": {"plan_item_id": "pi-test-999"},
        })

        assert response.status_code == 200, f"422 detail: {response.json()}"
        kwargs = mock_router.route.call_args.kwargs
        assert kwargs["context"]["plan_item_id"] == "pi-test-999"
        assert kwargs["user_id"] == "demo"

    def test_full_context_preserved(self):
        """All three context fields passed together are all preserved."""
        client, mock_router = _build_test_client_and_mock()

        response = client.post("/api/router/chat", json={
            "message": "start my task",
            "context": {
                "user_id": "real-user",
                "session_id": "sess-xyz",
                "plan_item_id": "pi-001",
            },
        })

        assert response.status_code == 200, f"422 detail: {response.json()}"
        kwargs = mock_router.route.call_args.kwargs
        assert kwargs["user_id"] == "real-user"
        assert kwargs["context"] == {
            "user_id": "real-user",
            "session_id": "sess-xyz",
            "plan_item_id": "pi-001",
        }

    # -- history preservation --------------------------------------------------

    def test_history_preserved(self):
        """History list is passed through to the router unchanged."""
        client, mock_router = _build_test_client_and_mock()

        history = [
            {"role": "user", "content": "previous question"},
            {"role": "assistant", "content": "previous answer"},
        ]

        response = client.post("/api/router/chat", json={
            "message": "follow up",
            "history": history,
        })

        assert response.status_code == 200, f"422 detail: {response.json()}"
        kwargs = mock_router.route.call_args.kwargs
        assert kwargs["history"] == history
        assert kwargs["context"] == {"user_id": "demo"}

    def test_empty_history_preserved(self):
        """Empty history list is passed through (not replaced)."""
        client, mock_router = _build_test_client_and_mock()

        response = client.post("/api/router/chat", json={
            "message": "test",
            "history": [],
        })

        assert response.status_code == 200, f"422 detail: {response.json()}"
        kwargs = mock_router.route.call_args.kwargs
        assert kwargs["history"] == []

    # -- extra fields allowed --------------------------------------------------

    def test_extra_fields_allowed(self):
        """Extra fields in the request are silently accepted (extra=allow)."""
        client, mock_router = _build_test_client_and_mock()

        response = client.post("/api/router/chat", json={
            "message": "test",
            "unknown_field": "should be ignored",
            "another": 123,
        })

        assert response.status_code == 200, f"422 detail: {response.json()}"
        kwargs = mock_router.route.call_args.kwargs
        assert kwargs["message"] == "test"
        assert kwargs["user_id"] == "demo"


# ---------------------------------------------------------------------------
# learning_scope flow tests — plan item → _execute_learning_task →
# _start_learning → start_lesson
# ---------------------------------------------------------------------------


_SAMPLE_LEARNING_SCOPE: dict[str, Any] = {
    "book_title": "牙周病学",
    "document_id": "doc-periodontology-001",
    "chapter_key": "chapter:abc123",
    "chapter_title": "第三章 牙周组织炎症",
    "kg_node_ids": ["kg-node-001", "kg-node-002"],
}


class TestLearningScopeFlow:
    """Formal ``learning_scope`` from a plan item must be forwarded to
    ``start_lesson``; empty / missing scope must NOT be forwarded."""

    @pytest.mark.asyncio
    async def test_plan_item_with_scope_passes_to_start_lesson(
        self, mock_orchestrator
    ):
        """Item with a non-empty learning_scope → start_lesson receives it."""
        item = {
            "id": "pi-scope-001",
            "user_id": "demo",
            "plan_date": date.today().isoformat(),
            "title": "学习牙周组织炎症",
            "status": "open",
            "linked_course_id": None,
            "linked_course_title": "牙周组织炎症",
            "learning_scope": dict(_SAMPLE_LEARNING_SCOPE),
        }
        mock_orchestrator.memory.get_study_plan_item.return_value = item
        mock_orchestrator.start_lesson.return_value = {
            "session": {"id": "sess-scope"},
            "outline": {"nodes": []},
            "learning_scope": dict(_SAMPLE_LEARNING_SCOPE),
        }
        router = MasterRouter(mock_orchestrator)

        result = await router.route(
            "",
            "demo",
            context={"plan_item_id": "pi-scope-001"},
        )

        assert result["ok"] is True
        assert result["route"]["source"] == "explicit_plan_item"
        # start_lesson was called with learning_scope
        mock_orchestrator.start_lesson.assert_called_once()
        call_kwargs = mock_orchestrator.start_lesson.call_args.kwargs
        assert "learning_scope" in call_kwargs
        assert call_kwargs["learning_scope"] == _SAMPLE_LEARNING_SCOPE

    @pytest.mark.asyncio
    async def test_plan_item_with_empty_scope_not_passed(
        self, mock_orchestrator
    ):
        """Item with ``learning_scope: {}`` → NOT forwarded to start_lesson."""
        item = {
            "id": "pi-empty-scope",
            "user_id": "demo",
            "plan_date": date.today().isoformat(),
            "title": "Fallback Title",
            "status": "open",
            "linked_course_title": None,
            "learning_scope": {},
        }
        mock_orchestrator.memory.get_study_plan_item.return_value = item
        mock_orchestrator.start_lesson.return_value = {
            "session": {"id": "sess-no-scope"},
            "outline": {"nodes": []},
        }
        router = MasterRouter(mock_orchestrator)

        result = await router.route(
            "",
            "demo",
            context={"plan_item_id": "pi-empty-scope"},
        )

        assert result["ok"] is True
        mock_orchestrator.start_lesson.assert_called_once()
        call_kwargs = mock_orchestrator.start_lesson.call_args.kwargs
        assert "learning_scope" not in call_kwargs

    @pytest.mark.asyncio
    async def test_plan_item_missing_scope_not_passed(
        self, mock_orchestrator
    ):
        """Item without ``learning_scope`` key → NOT forwarded."""
        item = {
            "id": "pi-no-scope-key",
            "user_id": "demo",
            "plan_date": date.today().isoformat(),
            "title": "No Scope Key Title",
            "status": "open",
            "linked_course_title": None,
        }
        mock_orchestrator.memory.get_study_plan_item.return_value = item
        mock_orchestrator.start_lesson.return_value = {
            "session": {"id": "sess-no-key"},
            "outline": {"nodes": []},
        }
        router = MasterRouter(mock_orchestrator)

        result = await router.route(
            "",
            "demo",
            context={"plan_item_id": "pi-no-scope-key"},
        )

        assert result["ok"] is True
        mock_orchestrator.start_lesson.assert_called_once()
        call_kwargs = mock_orchestrator.start_lesson.call_args.kwargs
        assert "learning_scope" not in call_kwargs

    @pytest.mark.asyncio
    async def test_free_learning_does_not_pass_scope(
        self, mock_orchestrator
    ):
        """Ordinary free learning (no plan_item_id) → no learning_scope."""
        mock_orchestrator.start_lesson.return_value = {
            "session": {"id": "sess-free"},
            "outline": {"nodes": []},
        }
        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier("learning", "用户想学新知识"),
        )

        result = await router.route("我想学习牙周炎的分类", "demo")

        assert result["ok"] is True
        assert result["route"]["intent"] == "learning"
        mock_orchestrator.start_lesson.assert_called_once()
        call_kwargs = mock_orchestrator.start_lesson.call_args.kwargs
        assert "learning_scope" not in call_kwargs

    @pytest.mark.asyncio
    async def test_scope_from_recommendation_passed(
        self, mock_orchestrator
    ):
        """Recommendation with learning_scope → forwarded via _execute_learning_task."""
        mock_orchestrator.start_lesson.return_value = {
            "session": {"id": "sess-recomm"},
            "outline": {"nodes": []},
            "learning_scope": dict(_SAMPLE_LEARNING_SCOPE),
        }

        # Build a study_plan_service that returns a recommendation with scope
        def _recommend_with_scope(_memory, *, user_id, plan_date, max_items=4):
            return {
                "plan_date": plan_date,
                "recommendations": [
                    {
                        "recommendation_id": "rec-scope-001",
                        "title": "学习牙周组织炎症",
                        "task_type": "learn_chapter",
                        "learning_target": "牙周组织炎症",
                        "linked_course_title": "牙周病学",
                        "learning_scope": dict(_SAMPLE_LEARNING_SCOPE),
                    }
                ],
            }

        router = MasterRouter(
            mock_orchestrator,
            study_plan_service=_recommend_with_scope,
        )

        result = await router.route(
            "",
            "demo",
            context={
                "recommendation_id": "rec-scope-001",
                "plan_date": date.today().isoformat(),
            },
        )

        assert result["ok"] is True
        assert result["route"]["source"] == "explicit_recommendation"
        mock_orchestrator.start_lesson.assert_called_once()
        call_kwargs = mock_orchestrator.start_lesson.call_args.kwargs
        assert "learning_scope" in call_kwargs
        assert call_kwargs["learning_scope"] == _SAMPLE_LEARNING_SCOPE

    @pytest.mark.asyncio
    async def test_scope_with_kg_node_ids_only_passed(
        self, mock_orchestrator
    ):
        """A minimal scope with only kg_node_ids is still non-empty → passed."""
        minimal_scope = {"kg_node_ids": ["kg-solo"]}
        item = {
            "id": "pi-minimal-scope",
            "user_id": "demo",
            "plan_date": date.today().isoformat(),
            "title": "Minimal Scope",
            "status": "open",
            "linked_course_title": "Minimal Course",
            "learning_scope": dict(minimal_scope),
        }
        mock_orchestrator.memory.get_study_plan_item.return_value = item
        mock_orchestrator.start_lesson.return_value = {
            "session": {"id": "sess-minimal"},
            "outline": {"nodes": []},
            "learning_scope": dict(minimal_scope),
        }
        router = MasterRouter(mock_orchestrator)

        result = await router.route(
            "",
            "demo",
            context={"plan_item_id": "pi-minimal-scope"},
        )

        assert result["ok"] is True
        mock_orchestrator.start_lesson.assert_called_once()
        call_kwargs = mock_orchestrator.start_lesson.call_args.kwargs
        assert "learning_scope" in call_kwargs
        assert call_kwargs["learning_scope"] == minimal_scope


# ---------------------------------------------------------------------------
# Planner-level scope filtering tests — plan_lesson with learning_scope
# ---------------------------------------------------------------------------


class TestPlanLessonScopeFiltering:
    """``plan_lesson`` must filter KG nodes when *learning_scope* is provided
    and return ``ok=False`` when the scope yields zero usable resources."""

    def _fake_node(self, node_id: str, name: str, score: float = 0.9) -> dict[str, Any]:
        return {
            "node_id": node_id,
            "canonical_name": name,
            "aliases": [name],
            "node_type": "concept",
            "node_kind": "core",
            "subject": "牙周病学",
            "chapter": "第三章",
            "section": "炎症机制",
            "_score": score,
        }

    def _fake_chunk(self, chunk_id: str, title: str = "test chunk") -> dict[str, Any]:
        return {
            "id": chunk_id,
            "title": title,
            "text_excerpt": "Sample text excerpt for testing.",
            "source": "textbook",
            "source_type": "textbook",
            "path": "doc-001",
            "score": 0.85,
            "metadata": {},
        }

    def test_scope_with_matching_kg_nodes_filters_correctly(self):
        """Scope with kg_node_ids filters relevant_nodes to only matching ones."""
        from backend.services.learning.planner import plan_lesson

        scope = {"kg_node_ids": ["kg-match-1", "kg-match-2"]}
        matching = [
            self._fake_node("kg-match-1", "炎症"),
            self._fake_node("kg-match-2", "组织破坏"),
        ]
        non_matching = [
            self._fake_node("kg-other-1", "无关概念A"),
            self._fake_node("kg-other-2", "无关概念B"),
        ]

        with (
            patch(
                "backend.services.learning.planner.find_relevant_nodes",
                return_value=matching + non_matching,
            ),
            patch(
                "backend.services.learning.planner.collect_planning_context",
                return_value={"ok": True, "chunks": [], "warnings": [], "source": "mock"},
            ),
            patch(
                "backend.services.learning.planner.load_kg_edges_for_nodes",
                return_value=[],
            ),
            patch(
                "backend.services.learning.planner.load_questions_for_nodes",
                return_value=[],
            ),
            patch(
                "backend.services.learning.planner.plan_lesson_initial_agent",
                return_value=(None, None, {"fallback_used": True, "fallback_reason": "test_mock_no_llm"}),
            ),
        ):
            result = plan_lesson("牙周炎", learning_scope=scope)

        # Should succeed — matching nodes exist after filtering
        assert result["ok"] is True
        # Only matching nodes should be in matched_nodes
        matched_ids = {n["node_id"] for n in result["matched_nodes"]}
        assert matched_ids == {"kg-match-1", "kg-match-2"}
        assert "kg-other-1" not in matched_ids
        assert "kg-other-2" not in matched_ids

    def test_scope_with_no_matching_nodes_returns_error(self):
        """When scope has kg_node_ids but none match → ok=False with scope_error."""
        from backend.services.learning.planner import plan_lesson

        scope = {"kg_node_ids": ["kg-non-existent-1", "kg-non-existent-2"]}
        nodes = [
            self._fake_node("kg-other-1", "无关A"),
            self._fake_node("kg-other-2", "无关B"),
        ]

        with (
            patch(
                "backend.services.learning.planner.find_relevant_nodes",
                return_value=nodes,
            ),
            patch(
                "backend.services.learning.planner.collect_planning_context",
                return_value={"ok": True, "chunks": [], "warnings": [], "source": "mock"},
            ),
        ):
            result = plan_lesson("牙周炎", learning_scope=scope)

        assert result["ok"] is False
        assert result["error"] == "scope_no_resources"
        assert "scope_error" in result
        assert "must NOT fall back" in result["scope_error"]
        assert result["learning_scope"] == scope
        assert result["outline"] == {"nodes": []}
        assert result["matched_nodes"] == []
        assert result["planning_trace"]["strategy"] == "scope_filtered_no_match"

    def test_scope_with_empty_kg_node_ids_treated_as_no_scope(self):
        """Scope with empty kg_node_ids list → behaves like no scope (no filter)."""
        from backend.services.learning.planner import plan_lesson

        scope: dict[str, Any] = {"kg_node_ids": []}
        nodes = [
            self._fake_node("kg-any-1", "概念A"),
            self._fake_node("kg-any-2", "概念B"),
        ]

        with (
            patch(
                "backend.services.learning.planner.find_relevant_nodes",
                return_value=nodes,
            ),
            patch(
                "backend.services.learning.planner.collect_planning_context",
                return_value={"ok": True, "chunks": [], "warnings": [], "source": "mock"},
            ),
            patch(
                "backend.services.learning.planner.load_kg_edges_for_nodes",
                return_value=[],
            ),
            patch(
                "backend.services.learning.planner.load_questions_for_nodes",
                return_value=[],
            ),
            patch(
                "backend.services.learning.planner.plan_lesson_initial_agent",
                return_value=(None, None, {"fallback_used": True, "fallback_reason": "test_mock_no_llm"}),
            ),
        ):
            result = plan_lesson("牙周炎", learning_scope=scope)

        # Should succeed — empty kg_node_ids means no filter applied
        assert result["ok"] is True
        assert len(result["matched_nodes"]) == 2

    def test_no_scope_preserves_behaviour(self):
        """Without learning_scope → behaviour unchanged (backward compat)."""
        from backend.services.learning.planner import plan_lesson

        nodes = [
            self._fake_node("kg-any-1", "概念A"),
            self._fake_node("kg-any-2", "概念B"),
        ]

        with (
            patch(
                "backend.services.learning.planner.find_relevant_nodes",
                return_value=nodes,
            ),
            patch(
                "backend.services.learning.planner.collect_planning_context",
                return_value={"ok": True, "chunks": [], "warnings": [], "source": "mock"},
            ),
            patch(
                "backend.services.learning.planner.load_kg_edges_for_nodes",
                return_value=[],
            ),
            patch(
                "backend.services.learning.planner.load_questions_for_nodes",
                return_value=[],
            ),
            patch(
                "backend.services.learning.planner.plan_lesson_initial_agent",
                return_value=(None, None, {"fallback_used": True, "fallback_reason": "test_mock_no_llm"}),
            ),
        ):
            result = plan_lesson("牙周炎")  # no learning_scope

        assert result["ok"] is True
        assert len(result["matched_nodes"]) == 2

    def test_scope_with_strange_kg_node_ids_type_ignored(self):
        """Scope with kg_node_ids that is not a list → treated as no scope."""
        from backend.services.learning.planner import plan_lesson

        scope: dict[str, Any] = {"kg_node_ids": "not-a-list"}
        nodes = [
            self._fake_node("kg-any-1", "概念A"),
        ]

        with (
            patch(
                "backend.services.learning.planner.find_relevant_nodes",
                return_value=nodes,
            ),
            patch(
                "backend.services.learning.planner.collect_planning_context",
                return_value={"ok": True, "chunks": [], "warnings": [], "source": "mock"},
            ),
            patch(
                "backend.services.learning.planner.load_kg_edges_for_nodes",
                return_value=[],
            ),
            patch(
                "backend.services.learning.planner.load_questions_for_nodes",
                return_value=[],
            ),
            patch(
                "backend.services.learning.planner.plan_lesson_initial_agent",
                return_value=(None, None, {"fallback_used": True, "fallback_reason": "test_mock_no_llm"}),
            ),
        ):
            result = plan_lesson("牙周炎", learning_scope=scope)

        # Non-list kg_node_ids → filter not applied
        assert result["ok"] is True
        assert len(result["matched_nodes"]) == 1

    def test_scope_with_empty_string_ids_filtered_out(self):
        """Scope with kg_node_ids containing empty strings → empty strings ignored."""
        from backend.services.learning.planner import plan_lesson

        scope = {"kg_node_ids": ["", "  ", "kg-real-1"]}
        nodes = [
            self._fake_node("kg-real-1", "概念A"),
            self._fake_node("kg-other", "概念B"),
        ]

        with (
            patch(
                "backend.services.learning.planner.find_relevant_nodes",
                return_value=nodes,
            ),
            patch(
                "backend.services.learning.planner.collect_planning_context",
                return_value={"ok": True, "chunks": [], "warnings": [], "source": "mock"},
            ),
            patch(
                "backend.services.learning.planner.load_kg_edges_for_nodes",
                return_value=[],
            ),
            patch(
                "backend.services.learning.planner.load_questions_for_nodes",
                return_value=[],
            ),
            patch(
                "backend.services.learning.planner.plan_lesson_initial_agent",
                return_value=(None, None, {"fallback_used": True, "fallback_reason": "test_mock_no_llm"}),
            ),
        ):
            result = plan_lesson("牙周炎", learning_scope=scope)

        # Only kg-real-1 filtered through
        assert result["ok"] is True
        matched_ids = {n["node_id"] for n in result["matched_nodes"]}
        assert matched_ids == {"kg-real-1"}


# ---------------------------------------------------------------------------
# Orchestrator-level scope error propagation tests
# ---------------------------------------------------------------------------


    def test_scope_with_document_id_filters_chunks(self):
        """Scope with document_id filters planning_context chunks to only that doc."""
        from backend.services.learning.planner import plan_lesson

        scope = {"document_id": "doc-target-001"}
        nodes = [
            self._fake_node("kg-any-1", "概念A"),
            self._fake_node("kg-any-2", "概念B"),
        ]
        matching_chunk = {
            "id": "ch-match",
            "title": "Target Document Chunk",
            "text_excerpt": "From the target document.",
            "source": "textbook",
            "source_type": "textbook",
            "path": "doc-target-001",
            "score": 0.9,
            "metadata": {"document_id": "doc-target-001", "chapter": "第一章"},
        }
        other_chunk = {
            "id": "ch-other",
            "title": "Other Document Chunk",
            "text_excerpt": "From another document.",
            "source": "textbook",
            "source_type": "textbook",
            "path": "doc-other-002",
            "score": 0.8,
            "metadata": {"document_id": "doc-other-002", "chapter": "第二章"},
        }

        with (
            patch(
                "backend.services.learning.planner.find_relevant_nodes",
                return_value=nodes,
            ),
            patch(
                "backend.services.learning.planner.collect_planning_context",
                return_value={
                    "ok": True,
                    "chunks": [matching_chunk, other_chunk],
                    "warnings": [],
                    "source": "mock",
                },
            ),
            patch(
                "backend.services.learning.planner.load_kg_edges_for_nodes",
                return_value=[],
            ),
            patch(
                "backend.services.learning.planner.load_questions_for_nodes",
                return_value=[],
            ),
            patch(
                "backend.services.learning.planner.plan_lesson_initial_agent",
                return_value=(None, None, {"fallback_used": True, "fallback_reason": "test_mock_no_llm"}),
            ),
        ):
            result = plan_lesson("牙周炎", learning_scope=scope)

        assert result["ok"] is True
        # Only the matching chunk should remain
        ctx_chunks = result["planning_context"]["chunks"]
        assert len(ctx_chunks) == 1
        assert ctx_chunks[0]["id"] == "ch-match"
        assert result["planning_context"]["scope_filtered"] is True
        assert result["planning_context"]["scope_document_id"] == "doc-target-001"

    def test_scope_with_chapter_filters_chunks(self):
        """Scope with chapter filters planning_context chunks to matching chapter."""
        from backend.services.learning.planner import plan_lesson

        scope = {"chapter_title": "第三章 牙周组织炎症"}
        nodes = [
            self._fake_node("kg-any-1", "概念A"),
        ]
        matching_chunk = {
            "id": "ch-ch3",
            "title": "Chapter 3 Chunk",
            "text_excerpt": "About chapter 3.",
            "source": "textbook",
            "source_type": "textbook",
            "path": "doc-001",
            "score": 0.9,
            "metadata": {"chapter": "第三章 牙周组织炎症"},
        }
        other_chunk = {
            "id": "ch-ch1",
            "title": "Chapter 1 Chunk",
            "text_excerpt": "About chapter 1.",
            "source": "textbook",
            "source_type": "textbook",
            "path": "doc-001",
            "score": 0.8,
            "metadata": {"chapter": "第一章 基础概念"},
        }

        with (
            patch(
                "backend.services.learning.planner.find_relevant_nodes",
                return_value=nodes,
            ),
            patch(
                "backend.services.learning.planner.collect_planning_context",
                return_value={
                    "ok": True,
                    "chunks": [matching_chunk, other_chunk],
                    "warnings": [],
                    "source": "mock",
                },
            ),
            patch(
                "backend.services.learning.planner.load_kg_edges_for_nodes",
                return_value=[],
            ),
            patch(
                "backend.services.learning.planner.load_questions_for_nodes",
                return_value=[],
            ),
            patch(
                "backend.services.learning.planner.plan_lesson_initial_agent",
                return_value=(None, None, {"fallback_used": True, "fallback_reason": "test_mock_no_llm"}),
            ),
        ):
            result = plan_lesson("牙周炎", learning_scope=scope)

        assert result["ok"] is True
        ctx_chunks = result["planning_context"]["chunks"]
        assert len(ctx_chunks) == 1
        assert ctx_chunks[0]["id"] == "ch-ch3"

    def test_scope_with_document_id_and_chapter_filters_both(self):
        """Scope with document_id AND chapter filters chunks by both criteria."""
        from backend.services.learning.planner import plan_lesson

        scope = {
            "document_id": "doc-target-001",
            "chapter_key": "chapter:abc123",
        }
        nodes = [self._fake_node("kg-any-1", "概念A")]
        good_chunk = {
            "id": "ch-good",
            "title": "Good",
            "text_excerpt": "x",
            "source": "textbook",
            "source_type": "textbook",
            "path": "doc-target-001",
            "score": 0.9,
            "metadata": {"document_id": "doc-target-001", "chapter": "chapter:abc123"},
        }
        wrong_doc = {
            "id": "ch-wrong-doc",
            "title": "Wrong Doc",
            "text_excerpt": "x",
            "source": "textbook",
            "source_type": "textbook",
            "path": "doc-other",
            "score": 0.8,
            "metadata": {"document_id": "doc-other", "chapter": "chapter:abc123"},
        }
        wrong_ch = {
            "id": "ch-wrong-ch",
            "title": "Wrong Chapter",
            "text_excerpt": "x",
            "source": "textbook",
            "source_type": "textbook",
            "path": "doc-target-001",
            "score": 0.7,
            "metadata": {"document_id": "doc-target-001", "chapter": "chapter:xyz"},
        }

        with (
            patch(
                "backend.services.learning.planner.find_relevant_nodes",
                return_value=nodes,
            ),
            patch(
                "backend.services.learning.planner.collect_planning_context",
                return_value={
                    "ok": True,
                    "chunks": [good_chunk, wrong_doc, wrong_ch],
                    "warnings": [],
                    "source": "mock",
                },
            ),
            patch(
                "backend.services.learning.planner.load_kg_edges_for_nodes",
                return_value=[],
            ),
            patch(
                "backend.services.learning.planner.load_questions_for_nodes",
                return_value=[],
            ),
            patch(
                "backend.services.learning.planner.plan_lesson_initial_agent",
                return_value=(None, None, {"fallback_used": True, "fallback_reason": "test_mock_no_llm"}),
            ),
        ):
            result = plan_lesson("牙周炎", learning_scope=scope)

        assert result["ok"] is True
        ctx_chunks = result["planning_context"]["chunks"]
        assert len(ctx_chunks) == 1
        assert ctx_chunks[0]["id"] == "ch-good"

    def test_scope_with_document_id_no_matching_chunks_errors(self):
        """Scope with document_id that matches no chunks → scope error even
        when some KG nodes match the topic (no kg_node_ids in scope)."""
        from backend.services.learning.planner import plan_lesson

        scope = {"document_id": "doc-nonexistent"}
        nodes = [
            self._fake_node("kg-any-1", "概念A"),
            self._fake_node("kg-any-2", "概念B"),
        ]

        with (
            patch(
                "backend.services.learning.planner.find_relevant_nodes",
                return_value=nodes,
            ),
            patch(
                "backend.services.learning.planner.collect_planning_context",
                return_value={
                    "ok": True,
                    "chunks": [
                        {
                            "id": "ch-other",
                            "title": "Other Doc",
                            "text_excerpt": "x",
                            "source": "textbook",
                            "source_type": "textbook",
                            "path": "doc-other",
                            "score": 0.9,
                            "metadata": {"document_id": "doc-other"},
                        },
                    ],
                    "warnings": [],
                    "source": "mock",
                },
            ),
        ):
            result = plan_lesson("牙周炎", learning_scope=scope)

        assert result["ok"] is False
        assert result["error"] == "scope_no_resources"
        assert "no matching evidence chunks" in result["scope_error"]
        assert "document_id='doc-nonexistent'" in result["scope_error"]
        assert result["planning_context"]["chunks"] == []

    def test_scope_with_document_id_and_kg_node_ids_both_filter(self):
        """Scope with document_id AND kg_node_ids → both filters apply;
        matching chunks but non-matching nodes → scope error."""
        from backend.services.learning.planner import plan_lesson

        scope = {
            "document_id": "doc-target-001",
            "kg_node_ids": ["kg-nonexistent"],
        }
        nodes = [self._fake_node("kg-other", "其他概念")]

        with (
            patch(
                "backend.services.learning.planner.find_relevant_nodes",
                return_value=nodes,
            ),
            patch(
                "backend.services.learning.planner.collect_planning_context",
                return_value={
                    "ok": True,
                    "chunks": [
                        {
                            "id": "ch-match",
                            "title": "Match",
                            "text_excerpt": "x",
                            "source": "textbook",
                            "source_type": "textbook",
                            "path": "doc-target-001",
                            "score": 0.9,
                            "metadata": {"document_id": "doc-target-001"},
                        },
                    ],
                    "warnings": [],
                    "source": "mock",
                },
            ),
        ):
            result = plan_lesson("牙周炎", learning_scope=scope)

        assert result["ok"] is False
        assert result["error"] == "scope_no_resources"
        # Chunks were filtered by doc_id (1 match), but nodes are empty
        assert result["matched_nodes"] == []
        # planning_context should still have the doc-matched chunks
        assert len(result["planning_context"]["chunks"]) == 1

    def test_scope_chapter_substring_match(self):
        """Chapter filter uses substring matching — '第三章' should match
        '第三章 牙周组织炎症'."""
        from backend.services.learning.planner import plan_lesson

        scope = {"chapter_title": "第三章"}
        nodes = [self._fake_node("kg-any", "概念")]
        chunk = {
            "id": "ch-1",
            "title": "Ch3",
            "text_excerpt": "x",
            "source": "textbook",
            "source_type": "textbook",
            "path": "doc-001",
            "score": 0.9,
            "metadata": {"chapter": "第三章 牙周组织炎症"},
        }

        with (
            patch(
                "backend.services.learning.planner.find_relevant_nodes",
                return_value=nodes,
            ),
            patch(
                "backend.services.learning.planner.collect_planning_context",
                return_value={
                    "ok": True,
                    "chunks": [chunk],
                    "warnings": [],
                    "source": "mock",
                },
            ),
            patch(
                "backend.services.learning.planner.load_kg_edges_for_nodes",
                return_value=[],
            ),
            patch(
                "backend.services.learning.planner.load_questions_for_nodes",
                return_value=[],
            ),
            patch(
                "backend.services.learning.planner.plan_lesson_initial_agent",
                return_value=(None, None, {"fallback_used": True, "fallback_reason": "test_mock_no_llm"}),
            ),
        ):
            result = plan_lesson("牙周炎", learning_scope=scope)

        # Substring match: "第三章" is in "第三章 牙周组织炎症"
        assert result["ok"] is True
        assert len(result["planning_context"]["chunks"]) == 1


class TestOrchestratorScopeError:
    """``start_lesson`` must raise RuntimeError when plan_lesson returns
    ``ok=False`` with a ``scope_error``."""

    @pytest.mark.asyncio
    async def test_scope_error_propagates_to_router(self, mock_orchestrator):
        """When start_lesson raises RuntimeError with scope_no_resources,
        the router returns ok=False with the error."""
        mock_orchestrator.start_lesson.side_effect = RuntimeError(
            "plan_lesson scope_no_resources for topic '牙周炎': "
            "The learning scope has no usable KG nodes"
        )
        item = {
            "id": "pi-scope-err",
            "user_id": "demo",
            "plan_date": date.today().isoformat(),
            "title": "Bad Scope Item",
            "status": "open",
            "linked_course_title": "牙周炎",
            "learning_scope": {"kg_node_ids": ["no-match-1"]},
        }
        mock_orchestrator.memory.get_study_plan_item.return_value = item

        router = MasterRouter(mock_orchestrator)

        result = await router.route(
            "",
            "demo",
            context={"plan_item_id": "pi-scope-err"},
        )

        assert result["ok"] is False
        assert "scope_no_resources" in str(result.get("error", ""))

    @pytest.mark.asyncio
    async def test_document_id_scope_error_propagates(self, mock_orchestrator):
        """When scope has a document_id with no matching chunks,
        RuntimeError propagates to the router."""
        mock_orchestrator.start_lesson.side_effect = RuntimeError(
            "plan_lesson scope_no_resources for topic '牙周炎': "
            "The learning scope has no matching evidence chunks "
            "for the specified filters (document_id='doc-nonexistent')"
        )
        item = {
            "id": "pi-doc-scope-err",
            "user_id": "demo",
            "plan_date": date.today().isoformat(),
            "title": "Bad Doc Scope",
            "status": "open",
            "linked_course_title": "牙周炎",
            "learning_scope": {"document_id": "doc-nonexistent"},
        }
        mock_orchestrator.memory.get_study_plan_item.return_value = item

        router = MasterRouter(mock_orchestrator)

        result = await router.route(
            "",
            "demo",
            context={"plan_item_id": "pi-doc-scope-err"},
        )

        assert result["ok"] is False
        assert "scope_no_resources" in str(result.get("error", ""))


# ---------------------------------------------------------------------------
# parent_plan_draft_id flow — A → B → C coverage
# ---------------------------------------------------------------------------


class TestStudyPlanParentDraftId:
    """Prove context.parent_plan_draft_id → service kwarg → result chain.

    - A: ``parent_plan_draft_id`` in the request context dict
    - B: the same value appears in the study_plan_service kwargs
    - C: the service produces a result with a plan_draft_id of its own
    """

    # ------------------------------------------------------------------
    # A → B → C positive
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_parent_draft_id_passed_to_service_kwargs(
        self, mock_orchestrator
    ):
        """A context with parent_plan_draft_id → B service receives it as kwarg."""
        captured_kwargs: dict = {}

        def _mock_sp(memory, *, user_id, plan_date, max_items=4,
                     request_text=None, parent_plan_draft_id=None, **kw):
            captured_kwargs["parent_plan_draft_id"] = parent_plan_draft_id
            captured_kwargs["request_text"] = request_text
            return {"plan_date": plan_date, "recommendations": [], "plan_draft_id": "draft_child_001"}

        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier("study_plan", "用户想看学习计划"),
            study_plan_service=_mock_sp,
        )

        result = await router.route(
            "帮我制定三天复习计划",
            "demo",
            context={
                "user_id": "demo",
                "parent_plan_draft_id": "draft_parent_aaa",
            },
        )

        assert result["ok"] is True
        assert result["route"]["intent"] == "study_plan"
        assert captured_kwargs.get("parent_plan_draft_id") == "draft_parent_aaa", (
            "B: service must receive parent_plan_draft_id kwarg from context"
        )
        assert captured_kwargs.get("request_text") == "帮我制定三天复习计划", (
            "existing kwargs must still be passed alongside parent"
        )
        assert result["final_result"]["plan_draft_id"] == "draft_child_001", (
            "C: service result with child draft ID is returned"
        )

    @pytest.mark.asyncio
    async def test_second_parent_draft_id_passed_correctly(
        self, mock_orchestrator
    ):
        """A second structurally different parent draft ID also flows through."""
        captured_kwargs: dict = {}

        def _mock_sp(memory, *, user_id, plan_date, max_items=4,
                     request_text=None, parent_plan_draft_id=None, **kw):
            captured_kwargs["parent_plan_draft_id"] = parent_plan_draft_id
            return {"plan_date": plan_date, "recommendations": [], "plan_draft_id": "draft_child_bbb"}

        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier("study_plan", "用户想看学习计划"),
            study_plan_service=_mock_sp,
        )

        result = await router.route(
            "本周复习薄弱点",
            "demo",
            context={
                "user_id": "demo",
                "parent_plan_draft_id": "draft_parent_zzz",
            },
        )

        assert result["ok"] is True
        assert captured_kwargs["parent_plan_draft_id"] == "draft_parent_zzz", (
            "B: different parent draft ID must also pass through"
        )
        assert result["final_result"]["plan_draft_id"] == "draft_child_bbb"

    # ------------------------------------------------------------------
    # A → B missing — parent_plan_draft_id absent
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_no_parent_in_context_means_none_passed(
        self, mock_orchestrator
    ):
        """When context has no parent_plan_draft_id, service receives None."""
        captured_kwargs: dict = {}

        def _mock_sp(memory, *, user_id, plan_date, max_items=4,
                     request_text=None, parent_plan_draft_id=None, **kw):
            captured_kwargs["parent_plan_draft_id"] = parent_plan_draft_id
            return {"plan_date": plan_date, "recommendations": []}

        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier("study_plan", "用户想看学习计划"),
            study_plan_service=_mock_sp,
        )

        result = await router.route(
            "今天学什么",
            "demo",
            context={"user_id": "demo"},
        )

        assert result["ok"] is True
        assert captured_kwargs.get("parent_plan_draft_id") is None, (
            "no parent in context → service receives None (not a stale value)"
        )

    @pytest.mark.asyncio
    async def test_empty_parent_in_context_treated_as_none(
        self, mock_orchestrator
    ):
        """Empty parent_plan_draft_id in context → treated as None."""
        captured_kwargs: dict = {}

        def _mock_sp(memory, *, user_id, plan_date, max_items=4,
                     request_text=None, parent_plan_draft_id=None, **kw):
            captured_kwargs["parent_plan_draft_id"] = parent_plan_draft_id
            return {"plan_date": plan_date, "recommendations": []}

        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier("study_plan", "ok"),
            study_plan_service=_mock_sp,
        )

        result = await router.route(
            "今天学什么", "demo",
            context={"user_id": "demo", "parent_plan_draft_id": ""},
        )

        assert result["ok"] is True
        assert captured_kwargs.get("parent_plan_draft_id") is None

    @pytest.mark.asyncio
    async def test_whitespace_parent_treated_as_none(
        self, mock_orchestrator
    ):
        """Whitespace-only parent_plan_draft_id → treated as None."""
        captured_kwargs: dict = {}

        def _mock_sp(memory, *, user_id, plan_date, max_items=4,
                     request_text=None, parent_plan_draft_id=None, **kw):
            captured_kwargs["parent_plan_draft_id"] = parent_plan_draft_id
            return {"plan_date": plan_date, "recommendations": []}

        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier("study_plan", "ok"),
            study_plan_service=_mock_sp,
        )

        result = await router.route(
            "今天学什么", "demo",
            context={"user_id": "demo", "parent_plan_draft_id": "   "},
        )

        assert result["ok"] is True
        assert captured_kwargs.get("parent_plan_draft_id") is None

    # ------------------------------------------------------------------
    # Non-study_plan isolation — other intents ignore parent
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_learning_intent_does_not_pass_parent(
        self, mock_orchestrator
    ):
        """Learning intent must not pass parent_plan_draft_id to any service."""
        mock_orchestrator.start_lesson.return_value = {
            "session": {"id": "sess-isolated"},
            "outline": {"nodes": []},
        }
        study_called: list[dict] = []

        def _mock_sp(memory, *, user_id, plan_date, max_items=4,
                     request_text=None, parent_plan_draft_id=None, **kw):
            study_called.append({"parent": parent_plan_draft_id})
            return {"recommendations": []}

        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier("learning", "用户想学习"),
            study_plan_service=_mock_sp,
        )

        result = await router.route(
            "我想学习牙周炎",
            "demo",
            context={
                "user_id": "demo",
                "parent_plan_draft_id": "draft_should_not_be_passed",
            },
        )

        assert result["ok"] is True
        assert result["route"]["intent"] == "learning"
        assert len(study_called) == 0, (
            "study_plan_service must NOT be called at all for learning intent"
        )

    @pytest.mark.asyncio
    async def test_literature_intent_does_not_pass_parent(
        self, mock_orchestrator
    ):
        """Literature intent must not pass parent_plan_draft_id."""
        study_called: list[dict] = []

        def _mock_sp(memory, *, user_id, plan_date, max_items=4,
                     request_text=None, parent_plan_draft_id=None, **kw):
            study_called.append({"parent": parent_plan_draft_id})
            return {"recommendations": []}

        async def _lit_mock(query):
            return {"ok": True, "articles": []}

        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier("literature", "查文献"),
            study_plan_service=_mock_sp,
            literature_service=_lit_mock,
        )

        result = await router.route(
            "检索最新文献",
            "demo",
            context={
                "user_id": "demo",
                "parent_plan_draft_id": "draft_not_for_lit",
            },
        )

        assert result["ok"] is True
        assert result["route"]["intent"] == "literature"
        assert len(study_called) == 0

    @pytest.mark.asyncio
    async def test_direct_qa_intent_does_not_pass_parent(
        self, mock_orchestrator
    ):
        """Direct QA intent must not pass parent_plan_draft_id."""
        study_called: list[dict] = []

        def _mock_sp(memory, *, user_id, plan_date, max_items=4,
                     request_text=None, parent_plan_draft_id=None, **kw):
            study_called.append({"parent": parent_plan_draft_id})
            return {"recommendations": []}

        def _direct_qa(**kw):
            return {"answer": "test answer"}

        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier("direct_qa", "直接问答"),
            study_plan_service=_mock_sp,
            direct_qa_service=_direct_qa,
        )

        result = await router.route(
            "牙周炎怎么治疗？",
            "demo",
            context={
                "user_id": "demo",
                "parent_plan_draft_id": "draft_not_for_qa",
            },
        )

        assert result["ok"] is True
        assert result["route"]["intent"] == "direct_qa"
        assert len(study_called) == 0

    # ------------------------------------------------------------------
    # A → B → error — parent lookup errors propagate
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_service_raises_key_error_returns_false(
        self, mock_orchestrator
    ):
        """When study_plan_service raises because parent is unknown,
        the router returns ok=False, no recommendations, no draft written."""

        def _failing_sp(memory, *, user_id, plan_date, max_items=4,
                        request_text=None, parent_plan_draft_id=None, **kw):
            raise KeyError("Unknown parent plan draft: 'draft_bad'")

        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier("study_plan", "ok"),
            study_plan_service=_failing_sp,
        )

        result = await router.route(
            "今天学什么",
            "demo",
            context={
                "user_id": "demo",
                "parent_plan_draft_id": "draft_bad",
            },
        )

        assert result["ok"] is False, (
            "parent KeyError → ok=False"
        )
        assert result["final_result"] is None, (
            "parent error → no recommendations in final_result"
        )
        assert "draft_bad" in (result.get("error") or ""), (
            "error must reference the failing parent draft ID"
        )

    @pytest.mark.asyncio
    async def test_service_raises_permission_error_returns_false(
        self, mock_orchestrator
    ):
        """Parent draft belonging to another user → ok=False."""

        def _failing_sp(memory, *, user_id, plan_date, max_items=4,
                        request_text=None, parent_plan_draft_id=None, **kw):
            raise PermissionError(
                "Parent plan draft 'draft_other' does not belong to user 'demo'"
            )

        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier("study_plan", "ok"),
            study_plan_service=_failing_sp,
        )

        result = await router.route(
            "今天学什么",
            "demo",
            context={
                "user_id": "demo",
                "parent_plan_draft_id": "draft_other",
            },
        )

        assert result["ok"] is False
        assert result["final_result"] is None
        assert "draft_other" in (result.get("error") or "")

    @pytest.mark.asyncio
    async def test_service_raises_value_error_returns_false(
        self, mock_orchestrator
    ):
        """Expired parent draft → ok=False."""

        def _failing_sp(memory, *, user_id, plan_date, max_items=4,
                        request_text=None, parent_plan_draft_id=None, **kw):
            raise ValueError("Parent plan draft 'draft_expired' has expired")

        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier("study_plan", "ok"),
            study_plan_service=_failing_sp,
        )

        result = await router.route(
            "今天学什么",
            "demo",
            context={
                "user_id": "demo",
                "parent_plan_draft_id": "draft_expired",
            },
        )

        assert result["ok"] is False
        assert result["final_result"] is None
        assert "draft_expired" in (result.get("error") or "")
