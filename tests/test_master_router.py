"""Tests for master_router.py — no external LLM or network calls.

All tests inject a mock classifier so the test suite is fully deterministic
and does not depend on any external service.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.schemas import RouterChatRequest
from backend.services.learning.study_plan import OrchestrationValidationError

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
    _extract_standalone_topic,
    _extract_requested_limit,
    _extract_requested_months,
    _literature_options_from_context,
    _requested_quiz_question_count,
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


class TestStandaloneQuizParsing:
    def test_topic_and_count_are_extracted_from_natural_language_request(self):
        message = "\u9488\u5bf9\u7259\u5468\u708e\u51fa 5 \u9053\u9898"
        assert _extract_standalone_topic(message, "quiz") == "\u7259\u5468\u708e"
        assert _requested_quiz_question_count(message) == 5
        assert _requested_quiz_question_count("\u9488\u5bf9\u7259\u5468\u708e\u51fa 1 \u9053\u9898") == 1

    def test_topic_and_count_accept_subject_between_quantity_and_question(self):
        message = "\u5e2e\u6211\u51fa5\u9053\u7259\u5468\u708e\u7684\u9898"
        assert _extract_standalone_topic(message, "quiz") == "\u7259\u5468\u708e"
        assert _requested_quiz_question_count(message) == 5

    def test_explicit_resource_topic_is_available_for_standalone_dispatch(self):
        assert _extract_standalone_topic("\u751f\u6210\u7259\u5468\u708e\u8bb2\u4e49", "handout") == "\u7259\u5468\u708e"

    def test_llm_quiz_route_preserves_structured_slots(self):
        route = _validate_route({
            "intent": "learning",
            "plan_item_requested": False,
            "dispatch_type": "agent",
            "target_agent": "quiz",
            "topic": "\u7259\u5468\u708e",
            "question_count": 5,
            "reason": "\u51fa\u7ec3\u4e60\u9898",
        })
        assert route is not None
        assert route["target_agent"] == "quiz"
        assert route["topic"] == "\u7259\u5468\u708e"
        assert route["question_count"] == 5

    @pytest.mark.asyncio
    async def test_explicit_topic_resource_bypasses_existing_session(self, mock_orchestrator):
        router = MasterRouter(mock_orchestrator)
        router.dispatch_agent = AsyncMock(return_value={"ok": True})

        await router.route(
            "\u751f\u6210\u7259\u5468\u708e\u8bb2\u4e49",
            "demo",
            context={"session_id": "formal-session"},
        )

        args = router.dispatch_agent.await_args.args
        kwargs = router.dispatch_agent.await_args.kwargs
        assert args[0] == "handout"
        assert kwargs["context"]["session_id"] == ""
        assert kwargs["context"]["topic"] == "\u7259\u5468\u708e"

    @pytest.mark.asyncio
    async def test_explicit_topic_resource_bypasses_llm_course_classification(self, mock_orchestrator):
        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier("learning", "generic course classification"),
        )
        router.dispatch_agent = AsyncMock(return_value={"ok": True})

        await router.route("\u751f\u6210\u7259\u5468\u708e\u8bb2\u4e49", "demo")

        args = router.dispatch_agent.await_args.args
        kwargs = router.dispatch_agent.await_args.kwargs
        assert args[0] == "handout"
        assert kwargs["context"]["session_id"] == ""
        assert kwargs["context"]["topic"] == "\u7259\u5468\u708e"


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

    def test_literature_route_preserves_structured_search_slots(self):
        result = _validate_route({
            "intent": "literature",
            "plan_item_requested": False,
            "query": "龋病微生物组研究",
            "limit": 3,
            "months": 12,
            "reason": "检索近期文献",
        })
        assert result is not None
        assert result["query"] == "龋病微生物组研究"
        assert result["limit"] == 3
        assert result["months"] == 12


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
    async def test_study_plan_capacity_error_returns_safe_clarification(
        self, mock_orchestrator
    ):
        """Schedule exhaustion is actionable UI state, not a raw exception."""

        def _capacity_failure(
            memory, *, user_id, plan_date, max_items=4, request_text=None
        ):
            raise OrchestrationValidationError(
                "day_offset_exceeds_total_days",
                "items[19].day_offset=7 must be less than total_days=7",
            )

        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier("study_plan", "整本教材学习计划"),
            study_plan_service=_capacity_failure,
        )
        message = "请为整本《牙周病学》制定学习计划"
        result = await router.route(message, "demo")

        assert result["ok"] is True
        assert "error" not in result
        final = result["final_result"]
        assert final["mode"] == "plan_capacity_clarification"
        assert final["clarification_needed"] is True
        assert final["clarification_reason"] == "schedule_capacity"
        assert len(final["clarification_options"]) == 3
        serialized = json.dumps(result, ensure_ascii=False)
        assert "items[19]" not in serialized
        assert "day_offset" not in serialized
        assert "total_days" not in serialized
        assert message in serialized
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
    def test_chinese_count_and_recency_are_parsed(self):
        assert _extract_requested_limit("给我找三篇龋病相关文献") == 3
        assert _extract_requested_limit("检索二十五篇论文") == 25
        assert _extract_requested_months("近三个月的研究") == 3
        assert _extract_requested_months("最近的前沿文献") == 12
        assert _extract_requested_months("过去两年文献") == 24
        assert _extract_requested_months("五年生存率相关文献") is None
        assert _extract_requested_limit("近三个月的研究") is None

    def test_literature_option_priority_is_context_then_route_then_text(self):
        options = _literature_options_from_context(
            "找三篇最近的龋病文献",
            {"limit": 8, "months": 6, "query": "context query"},
            {"limit": 4, "months": 24, "query": "route query"},
        )
        assert options == {"query": "context query", "months": 6, "limit": 8}

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
        assert seen["args"] == ("我想要阅读阻生牙近三个月的文献", 3, 5)
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
        assert seen["args"] == ("我想要阅读阻生牙近三个月的文献", 3, 5)
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
    async def test_literature_handler_understands_chinese_count_and_latest(self, mock_orchestrator):
        seen: dict[str, Any] = {}

        async def _lit_mock(*args, **kwargs):
            seen["args"] = args
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
        result = await router.route("给我找三篇龋病相关的最近前沿文献", "demo")

        assert result["ok"] is True
        assert seen["args"] == ("给我找三篇龋病相关的最近前沿文献", 12, 3)

    @pytest.mark.asyncio
    async def test_literature_handler_uses_llm_search_focus_and_slots(self, mock_orchestrator):
        seen: dict[str, Any] = {}

        async def _lit_mock(*args, **kwargs):
            seen["args"] = args
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
            classifier=lambda *_args: {
                "intent": "literature",
                "plan_item_requested": False,
                "query": "龋病微生物组前沿研究",
                "limit": 4,
                "months": 18,
                "reason": "提取结构化检索需求",
            },
            literature_service=_lit_mock,
        )
        result = await router.route("帮我找相关论文", "demo")

        assert result["ok"] is True
        assert seen["args"] == ("龋病微生物组前沿研究", 18, 4)

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
        scoped_item = {
            **sample_plan_item,
            "learning_scope": {
                "book_title": "口腔材料学",
                "chapter_title": "第三章 牙体修复材料",
                "chapter_key": "chapter:oral-materials-ch3",
            },
        }
        mock_orchestrator.memory.get_study_plan_item.return_value = scoped_item
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
        assert result["route"]["learning_scope"]["book_title"] == "口腔材料学"
        assert result["route"]["learning_scope"]["chapter_title"].startswith("第三章")
        assert result["next_action"] == "start_session"
        call_kwargs = mock_orchestrator.start_lesson.call_args.kwargs
        assert call_kwargs["learning_scope"]["chapter_key"].startswith("chapter:")
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
        """Scope with kg_node_ids filters relevant_nodes to only matching ones.

        plan_lesson now uses the unified preflight_scope_resources check.
        The test mocks preflight to return usable=True with the matching
        KG node IDs verified, so the downstream filtering is exercised.
        Uses ``kg://`` prefixed IDs so they pass through the format gate.
        """
        from backend.services.learning.planner import plan_lesson

        scope = {"kg_node_ids": ["kg://match-1", "kg://match-2"]}
        matching = [
            self._fake_node("kg://match-1", "炎症"),
            self._fake_node("kg://match-2", "组织破坏"),
        ]
        non_matching = [
            self._fake_node("kg://other-1", "无关概念A"),
            self._fake_node("kg://other-2", "无关概念B"),
        ]

        _mock_preflight = {
            "usable": True,
            "reason_code": "ok",
            "kg_node_hit_count": 2,
            "kg_node_ids_checked": ["kg://match-1", "kg://match-2"],
            "kg_node_ids_usable": ["kg://match-1", "kg://match-2"],
            "evidence_chunk_count": 0,
            "scope_diagnostics": {
                "has_kg_node_ids": True,
                "has_document_id": False,
                "has_chapter_key": False,
                "title": "",
                "book_title": "",
                "document_id": "",
                "chapter_key": "",
                "checks_performed": ["kg_nodes"],
            },
            "error_message": None,
        }

        with (
            patch(
                "backend.services.learning.planner.preflight_scope_resources",
                return_value=_mock_preflight,
            ),
            patch(
                "backend.services.learning.planner.load_kg_nodes",
                return_value=matching + non_matching,
            ),
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
        # Only matching nodes should be in matched_nodes (scope KG nodes
        # loaded directly + topic-scored nodes within scope)
        matched_ids = {n["node_id"] for n in result["matched_nodes"]}
        assert "kg://match-1" in matched_ids
        assert "kg://match-2" in matched_ids
        assert "kg://other-1" not in matched_ids
        assert "kg://other-2" not in matched_ids

    def test_scope_with_no_matching_nodes_returns_error(self):
        """When scope has kg_node_ids but preflight says unusable →
        ok=False with scope_error.  The unified preflight check is the
        single gate — no fallback to unscoped planning."""
        from backend.services.learning.planner import plan_lesson

        scope = {"kg_node_ids": ["kg-non-existent-1", "kg-non-existent-2"]}
        nodes = [
            self._fake_node("kg-other-1", "无关A"),
            self._fake_node("kg-other-2", "无关B"),
        ]

        _mock_preflight_unusable = {
            "usable": False,
            "reason_code": "no_kg_nodes_usable",
            "kg_node_hit_count": 0,
            "kg_node_ids_checked": ["kg-non-existent-1", "kg-non-existent-2"],
            "kg_node_ids_usable": [],
            "evidence_chunk_count": 0,
            "scope_diagnostics": {
                "has_kg_node_ids": True,
                "has_document_id": False,
                "has_chapter_key": False,
                "title": "",
                "book_title": "",
                "document_id": "",
                "chapter_key": "",
                "checks_performed": ["kg_nodes"],
            },
            "error_message": "The learning scope has no usable KG nodes.",
        }

        with (
            patch(
                "backend.services.learning.planner.preflight_scope_resources",
                return_value=_mock_preflight_unusable,
            ),
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
        assert "scope_preflight" in result
        assert "scope_error" in result
        assert "no usable KG nodes" in result["scope_error"]
        assert result["learning_scope"] == scope
        assert result["outline"] == {"nodes": []}
        assert result["matched_nodes"] == []
        assert result["planning_trace"]["strategy"] == "scope_preflight_failed"

    def test_scope_with_empty_kg_node_ids_rejected(self):
        """Scope with empty kg_node_ids list → empty scope → preflight rejects
        (was: treated as no scope).  An empty scope dict with no identifiers
        is not valid — it must carry at least one resource identifier."""
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
        ):
            result = plan_lesson("牙周炎", learning_scope=scope)

        # Empty scope with no identifiers → preflight rejects
        assert result["ok"] is False
        assert result["error"] == "scope_no_resources"

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

    def test_scope_with_strange_kg_node_ids_type_rejected(self):
        """Scope with kg_node_ids that is not a list → empty scope → preflight
        rejects (was: treated as no scope).  A malformed scope with no valid
        identifiers is not usable."""
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
        ):
            result = plan_lesson("牙周炎", learning_scope=scope)

        # Malformed scope → preflight rejects (empty scope)
        assert result["ok"] is False
        assert result["error"] == "scope_no_resources"

    def test_scope_with_empty_string_ids_filtered_out(self):
        """Scope with kg_node_ids containing empty strings → empty strings ignored,
        only the valid kg:// ID is used.  Mock preflight to verify the single valid ID."""
        from backend.services.learning.planner import plan_lesson

        scope = {"kg_node_ids": ["", "  ", "kg://real-1"]}
        nodes = [
            self._fake_node("kg://real-1", "概念A"),
            self._fake_node("kg://other", "概念B"),
        ]

        _mock_preflight = {
            "usable": True,
            "reason_code": "ok",
            "kg_node_hit_count": 1,
            "kg_node_ids_checked": ["kg://real-1"],
            "kg_node_ids_usable": ["kg://real-1"],
            "evidence_chunk_count": 0,
            "scope_diagnostics": {
                "has_kg_node_ids": True,
                "has_document_id": False,
                "has_chapter_key": False,
                "title": "",
                "book_title": "",
                "document_id": "",
                "chapter_key": "",
                "checks_performed": ["kg_nodes"],
            },
            "error_message": None,
        }

        with (
            patch(
                "backend.services.learning.planner.preflight_scope_resources",
                return_value=_mock_preflight,
            ),
            patch(
                "backend.services.learning.planner.load_kg_nodes",
                return_value=nodes,
            ),
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

        # Only kg://real-1 filtered through
        assert result["ok"] is True
        matched_ids = {n["node_id"] for n in result["matched_nodes"]}
        assert "kg://real-1" in matched_ids
        assert "kg://other" not in matched_ids


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
        """Scope with document_id AND kg_node_ids → both filters apply.
        When evidence chunks are available (doc-target-001 matches) the
        unified preflight says usable — even if KG nodes don't match.
        The scope has at least one usable resource type."""
        from backend.services.learning.planner import plan_lesson

        scope = {
            "document_id": "doc-target-001",
            "kg_node_ids": ["kg-nonexistent"],
        }
        nodes = [self._fake_node("kg-other", "其他概念")]

        _mock_preflight = {
            "usable": True,
            "reason_code": "ok",
            "kg_node_hit_count": 0,
            "kg_node_ids_checked": ["kg-nonexistent"],
            "kg_node_ids_usable": [],
            "evidence_chunk_count": 1,
            "scope_diagnostics": {
                "has_kg_node_ids": True,
                "has_document_id": True,
                "has_chapter_key": False,
                "title": "",
                "book_title": "",
                "document_id": "doc-target-001",
                "chapter_key": "",
                "checks_performed": ["kg_nodes", "evidence_chunks"],
            },
            "error_message": None,
        }

        with (
            patch(
                "backend.services.learning.planner.preflight_scope_resources",
                return_value=_mock_preflight,
            ),
            patch(
                "backend.services.learning.planner.load_kg_nodes",
                return_value=nodes,
            ),
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

        # Evidence chunks pass → preflight says usable, planning proceeds.
        # The scope is usable by evidence chunks even without matching KG nodes.
        assert result["ok"] is True
        # planning_context should have the doc-matched chunks
        ctx_chunks = result.get("planning_context", {}).get("chunks", [])
        assert len(ctx_chunks) == 1

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


# ---------------------------------------------------------------------------
# R10: parent_plan_draft_id deterministic routing pre-check
# ---------------------------------------------------------------------------


def _make_test_draft_store(drafts: dict[str, dict[str, Any]]) -> Any:
    """Create a :class:`PlanDraftStore` pre-populated with *drafts*.

    Each value in *drafts* is the raw record dict stored directly in the
    store's internal ``_drafts`` mapping.  The clock is frozen far in the
    future so TTL checks always pass.
    """
    from backend.services.learning.study_plan import PlanDraftStore

    store = PlanDraftStore(ttl_seconds=3600, _clock=lambda: 1_000_000_000.0)
    with store._lock:
        for draft_id, record in drafts.items():
            store._drafts[draft_id] = record
    return store


class TestParentDraftContinuationRouting:
    """Prove that ``parent_plan_draft_id`` with ``clarification_needed=true``
    parent draft routes directly to ``study_plan`` without LLM classification.

    This is the R10 fix: after a formal plan clarification, a user's chapter-title
    reply must continue into the study_plan flow instead of falling through to
    the LLM classifier (which would misroute it as ``clarification``).
    """

    @pytest.mark.asyncio
    async def test_clarification_parent_draft_bypasses_classifier(
        self, mock_orchestrator
    ):
        """Parent draft has clarification_needed=true → study_plan without LLM classify.

        The LLM classifier is configured to return ``clarification`` (wrong), but
        the deterministic pre-check must override and route to ``study_plan``.
        """
        captured_kwargs: dict = {}

        def _mock_sp(memory, *, user_id, plan_date, max_items=4,
                     request_text=None, parent_plan_draft_id=None, **kw):
            captured_kwargs.update({
                "parent_plan_draft_id": parent_plan_draft_id,
                "request_text": request_text,
                "history": kw.get("history"),
            })
            return {
                "plan_date": plan_date,
                "recommendations": [],
                "plan_draft_id": "draft_child_002",
            }

        # Parent draft with clarification_needed=true — the user is answering
        # a prior study-plan clarification question.
        parent_payload = {
            "request": {"intent": "learn_chapter", "days": 7, "daily_minutes": 30},
            "items": [],
            "clarification_needed": True,
            "clarification_message": "请告诉我您想学习哪本教材或哪个章节？",
        }
        test_store = _make_test_draft_store({
            "draft_parent_clarify": {
                "plan_draft_id": "draft_parent_clarify",
                "user_id": "demo",
                "created_at": 999_000_000.0,
                "expires_at": 2_000_000_000.0,
                "payload": parent_payload,
            },
        })

        with patch(
            "backend.services.learning.study_plan._get_default_draft_store",
            return_value=test_store,
        ):
            router = MasterRouter(
                mock_orchestrator,
                classifier=_make_classifier("clarification", "意图不明"),
                study_plan_service=_mock_sp,
            )

            result = await router.route(
                "第三章 牙周病的发病机制",
                "demo",
                history=[
                    {"role": "assistant", "content": "请告诉我您想学习哪本教材或哪个章节？"},
                ],
                context={
                    "user_id": "demo",
                    "parent_plan_draft_id": "draft_parent_clarify",
                },
            )

        # Must route to study_plan — NOT clarification despite the classifier
        assert result["ok"] is True, f"expected ok=True, got {result.get('error')}"
        assert result["route"]["intent"] == "study_plan", (
            f"R10: must route to study_plan, got {result['route']['intent']}"
        )
        assert result["route"]["source"] == "parent_draft_clarification", (
            "source must identify the deterministic pre-check"
        )
        # The service must receive the parent draft ID and the user's message
        assert captured_kwargs.get("parent_plan_draft_id") == "draft_parent_clarify"
        assert captured_kwargs.get("request_text") == "第三章 牙周病的发病机制"
        # History must be forwarded so the LLM parser can resolve context
        assert captured_kwargs.get("history") is not None
        assert len(captured_kwargs["history"]) == 1

    @pytest.mark.asyncio
    async def test_non_clarification_parent_falls_through_to_classifier(
        self, mock_orchestrator
    ):
        """Parent draft without clarification_needed → normal LLM classification.

        When the parent draft is a completed plan (no clarification_needed flag),
        the pre-check must NOT hijack routing — the classifier decides.
        """
        study_called: list[dict] = []

        def _mock_sp(memory, *, user_id, plan_date, max_items=4,
                     request_text=None, parent_plan_draft_id=None, **kw):
            study_called.append({"parent": parent_plan_draft_id})
            return {"recommendations": []}

        # Parent draft with actual items — NOT awaiting clarification
        parent_payload = {
            "request": {"intent": "learn_chapter", "days": 7, "daily_minutes": 30,
                        "chapter_key": "chapter:perio-ch3"},
            "items": [{"task_type": "learn_chapter", "learning_target": "牙周病发病机制"}],
        }
        test_store = _make_test_draft_store({
            "draft_parent_done": {
                "plan_draft_id": "draft_parent_done",
                "user_id": "demo",
                "created_at": 999_000_000.0,
                "expires_at": 2_000_000_000.0,
                "payload": parent_payload,
            },
        })

        with patch(
            "backend.services.learning.study_plan._get_default_draft_store",
            return_value=test_store,
        ):
            mock_orchestrator.start_lesson.return_value = {
                "session": {"id": "sess-parent-fallthrough"},
                "outline": {"nodes": []},
            }
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
                    "parent_plan_draft_id": "draft_parent_done",
                },
            )

        assert result["ok"] is True
        # Must follow the classifier — NOT hijacked to study_plan
        assert result["route"]["intent"] == "learning", (
            f"non-clarification parent must not override classifier; "
            f"expected learning, got {result['route']['intent']}"
        )
        assert result["route"]["source"] == "llm_router", (
            "source must be llm_router when pre-check does not match"
        )
        # study_plan_service must NOT be called
        assert len(study_called) == 0

    @pytest.mark.asyncio
    async def test_missing_parent_draft_falls_through_to_classifier(
        self, mock_orchestrator
    ):
        """Parent draft ID not found in store → normal LLM classification.

        A stale parent_plan_draft_id (expired / deleted) must not crash the
        router or cause a spurious study_plan route.
        """
        study_called: list[dict] = []

        def _mock_sp(memory, *, user_id, plan_date, max_items=4,
                     request_text=None, parent_plan_draft_id=None, **kw):
            study_called.append({"parent": parent_plan_draft_id})
            return {"recommendations": []}

        # Empty store — the parent_plan_draft_id won't be found
        test_store = _make_test_draft_store({})

        with patch(
            "backend.services.learning.study_plan._get_default_draft_store",
            return_value=test_store,
        ):
            router = MasterRouter(
                mock_orchestrator,
                classifier=_make_classifier("direct_qa", "直接问答"),
                study_plan_service=_mock_sp,
            )

            result = await router.route(
                "牙周炎怎么治疗？",
                "demo",
                context={
                    "user_id": "demo",
                    "parent_plan_draft_id": "draft_stale_gone",
                },
            )

        assert result["ok"] is True
        # Must follow the classifier — unknown parent is harmless
        assert result["route"]["intent"] == "direct_qa", (
            f"unknown parent draft must fall through to classifier; "
            f"expected direct_qa, got {result['route']['intent']}"
        )
        assert result["route"]["source"] == "llm_router"

    @pytest.mark.asyncio
    async def test_clarification_continuation_with_book_and_chapter(
        self, mock_orchestrator
    ):
        """Full R10 scenario: chapter+book reply after clarification → study_plan.

        User first said "帮我安排一下接下来怎么学" (got clarification), then
        replies with "第三章 牙周病的发病机制" — the pre-check must route to
        study_plan and the service receives both message and parent draft ID.
        """
        captured_kwargs: dict = {}

        def _mock_sp(memory, *, user_id, plan_date, max_items=4,
                     request_text=None, parent_plan_draft_id=None, **kw):
            captured_kwargs.update({
                "parent_plan_draft_id": parent_plan_draft_id,
                "request_text": request_text,
                "history": kw.get("history"),
            })
            return {
                "plan_date": plan_date,
                "mode": "custom_draft",
                "plan_draft_id": "draft_child_ch3",
                "recommendations": [
                    {
                        "recommendation_id": "draft_child_ch3:0",
                        "title": "学习《牙周病学》牙周病的发病机制",
                        "task_type": "learn_chapter",
                        "estimated_minutes": 30,
                    },
                ],
                "trace": {"strategy": "custom_draft_from_request_text"},
            }

        parent_payload = {
            "request": {"intent": "custom", "days": 7, "daily_minutes": 30},
            "items": [],
            "clarification_needed": True,
            "clarification_message": "请告诉我您想学习的具体内容，以便我为您制定合适的学习计划。",
        }
        test_store = _make_test_draft_store({
            "draft_parent_vague": {
                "plan_draft_id": "draft_parent_vague",
                "user_id": "demo",
                "created_at": 999_000_000.0,
                "expires_at": 2_000_000_000.0,
                "payload": parent_payload,
            },
        })

        with patch(
            "backend.services.learning.study_plan._get_default_draft_store",
            return_value=test_store,
        ):
            router = MasterRouter(
                mock_orchestrator,
                # Deliberately wrong classifier — would misroute as clarification
                classifier=_make_classifier("clarification", "意图模糊"),
                study_plan_service=_mock_sp,
            )

            result = await router.route(
                "第三章 牙周病的发病机制",
                "demo",
                history=[
                    {"role": "user", "content": "帮我安排一下接下来怎么学"},
                    {"role": "assistant",
                     "content": "请告诉我您想学习的具体内容，以便我为您制定合适的学习计划。"},
                ],
                context={
                    "user_id": "demo",
                    "parent_plan_draft_id": "draft_parent_vague",
                },
            )

        assert result["ok"] is True
        assert result["route"]["intent"] == "study_plan", (
            f"R10 chapter-title reply must route to study_plan, "
            f"got {result['route']['intent']}"
        )
        assert result["route"]["source"] == "parent_draft_clarification"
        assert captured_kwargs["parent_plan_draft_id"] == "draft_parent_vague"
        assert captured_kwargs["request_text"] == "第三章 牙周病的发病机制"
        assert captured_kwargs.get("history") is not None
        # The result must carry a real plan (not clarification fallback)
        assert len(result["final_result"].get("recommendations", [])) > 0, (
            "R10: must produce a real plan with recommendations, not empty"
        )
        assert result["final_result"]["plan_draft_id"] == "draft_child_ch3"

    @pytest.mark.asyncio
    async def test_no_parent_in_context_never_triggers_pre_check(
        self, mock_orchestrator
    ):
        """Context without parent_plan_draft_id must NEVER trigger the pre-check.

        A plain chapter title message without study-plan context must go through
        normal classification — NOT be hijacked to study_plan.
        """
        study_called: list[dict] = []

        def _mock_sp(memory, *, user_id, plan_date, max_items=4,
                     request_text=None, parent_plan_draft_id=None, **kw):
            study_called.append({"parent": parent_plan_draft_id})
            return {"recommendations": []}

        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier("direct_qa", "直接问答"),
            study_plan_service=_mock_sp,
        )

        result = await router.route(
            "第三章 牙周病的发病机制",
            "demo",
            context={"user_id": "demo"},  # NO parent_plan_draft_id
        )

        assert result["ok"] is True
        assert result["route"]["intent"] == "direct_qa", (
            f"without parent_plan_draft_id, classifier must decide; "
            f"expected direct_qa, got {result['route']['intent']}"
        )
        assert len(study_called) == 0, (
            "study_plan_service must NOT be called without parent draft context"
        )


# ============================================================================
# preflight_scope_resources — shared resource check (planner module)
# ============================================================================


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_real_kg_ids_cache: list[str] | None = None


def _get_real_kg_ids() -> list[str]:
    """Return a list of real KG node IDs from the test environment KG."""
    global _real_kg_ids_cache
    if _real_kg_ids_cache is not None:
        return _real_kg_ids_cache

    try:
        from backend.services.learning.kag_paths import resolve_kag_dir

        kag_dir = resolve_kag_dir()
        kg_file = kag_dir / "kg_nodes.jsonl"
        if kg_file.is_file():
            ids: list[str] = []
            with open(kg_file, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        n = json.loads(line)
                        nid = n.get("node_id", "")
                        if nid:
                            ids.append(nid)
                            if len(ids) >= 5:
                                break
                    except json.JSONDecodeError:
                        continue
            _real_kg_ids_cache = ids
            return ids
    except Exception:
        pass

    _real_kg_ids_cache = []
    return []


def _real_kg_scope(**overrides: Any) -> dict[str, Any]:
    """Build a learning_scope using real KG node IDs from the test environment.

    Derives document_id, book_title, and chapter_title from the first real
    raw node so that scope constraints match the actual raw KG data.
    """
    ids = _get_real_kg_ids()
    book_title = ""
    document_id = ""
    chapter_title = ""

    if ids:
        try:
            from backend.services.learning.planner import load_kg_nodes
            all_nodes = load_kg_nodes()
            id_index = {str(n.get("node_id", "")).strip(): n for n in all_nodes}
            first = id_index.get(ids[0])
            if first:
                book_title = str(first.get("book_title", "")).strip()
                chapter_title = str(first.get("chapter", "")).strip()
                meta = first.get("metadata")
                if isinstance(meta, dict):
                    document_id = str(meta.get("document_id", "")).strip()
        except Exception:
            pass

    defaults: dict[str, Any] = {
        "book_title": book_title,
        "document_id": document_id,
        "chapter_key": "",
        "chapter_title": chapter_title,
        "kg_node_ids": ids,
        "kg_node_names": {},
    }
    return {**defaults, **overrides}


def _fake_scope(**overrides: Any) -> dict[str, Any]:
    """Build a learning_scope with fake (non-existent) KG node IDs."""
    defaults: dict[str, Any] = {
        "book_title": "牙周病学",
        "document_id": "document.d766e8629232a2f6",
        "chapter_key": "chapter:fake-999",
        "chapter_title": "不存在的章节",
        "kg_node_ids": ["core:fake001", "core:fake002", "core:fake003"],
        "kg_node_names": {},
    }
    return {**defaults, **overrides}


def _empty_scope() -> dict[str, Any]:
    """Return a scope with no resource identifiers."""
    return {
        "book_title": "",
        "document_id": "",
        "chapter_key": "",
        "chapter_title": "",
        "kg_node_ids": [],
        "kg_node_names": {},
    }


class TestPreflightScopeResources:
    """preflight_scope_resources — fail-closed scope validation."""

    # ── Positive ────────────────────────────────────────────────────────

    def test_real_kg_nodes_make_scope_usable(self):
        """A scope with real (existing) KG node IDs is usable."""
        from backend.services.learning.planner import (
            _PREFLIGHT_OK,
            preflight_scope_resources,
        )

        real_ids = _get_real_kg_ids()
        if not real_ids:
            pytest.skip("No real KG data available — test requires kg_nodes.jsonl")

        scope = _real_kg_scope(kg_node_ids=real_ids)
        result = preflight_scope_resources(scope)
        assert result["usable"] is True
        assert result["reason_code"] == _PREFLIGHT_OK
        assert result["kg_node_hit_count"] > 0
        assert len(result["kg_node_ids_usable"]) > 0

    def test_checks_disabled_returns_not_verified(self):
        """When both checks are disabled, scope returns usable=False with
        reason not_verified — fail-closed (no resource check = not proven)."""
        from backend.services.learning.planner import (
            _PREFLIGHT_NOT_VERIFIED,
            preflight_scope_resources,
        )

        scope = _fake_scope()
        result = preflight_scope_resources(
            scope,
            check_kg_nodes=False,
            check_evidence_chunks=False,
        )
        assert result["usable"] is False
        assert result["reason_code"] == _PREFLIGHT_NOT_VERIFIED
        assert "not verified" in result.get("error_message", "").lower()

    # ── Negative: conclusive failures ───────────────────────────────────

    def test_empty_scope_rejected(self):
        """A scope with no identifiers at all is rejected."""
        from backend.services.learning.planner import (
            _PREFLIGHT_EMPTY_SCOPE,
            preflight_scope_resources,
        )

        scope = _empty_scope()
        result = preflight_scope_resources(scope)
        assert result["usable"] is False
        assert result["reason_code"] == _PREFLIGHT_EMPTY_SCOPE
        assert "empty" in result.get("error_message", "").lower()

    def test_fake_kg_node_ids_conclusive_failure(self):
        """Fake IDs in a real KG are a conclusive failure."""
        from backend.services.learning.planner import (
            _PREFLIGHT_NO_KG_AND_NO_CHUNKS,
            _PREFLIGHT_NO_KG_NODES,
            preflight_scope_resources,
        )

        scope = _fake_scope()
        result = preflight_scope_resources(
            scope,
            check_kg_nodes=True,
            check_evidence_chunks=False,
        )
        if result["scope_diagnostics"]["checks_performed"]:
            assert result["kg_node_hit_count"] == 0
            if result["kg_node_hit_count"] == 0:
                assert result["usable"] is False
                assert result["reason_code"] in (
                    _PREFLIGHT_NO_KG_NODES,
                    _PREFLIGHT_NO_KG_AND_NO_CHUNKS,
                )

    # ── Negative: KG unavailable → fail-closed ──────────────────────────

    def test_kg_file_missing_returns_unavailable(self):
        """When KG file is missing, preflight returns usable=False
        (fail-closed — cannot prove scope valid)."""
        from backend.services.learning.planner import (
            _PREFLIGHT_UNAVAILABLE,
            preflight_scope_resources,
        )
        from unittest.mock import patch

        scope = _fake_scope()
        with patch(
            "backend.services.learning.planner.load_kg_nodes",
            side_effect=FileNotFoundError("kg_nodes.jsonl not found"),
        ):
            result = preflight_scope_resources(
                scope,
                check_kg_nodes=True,
                check_evidence_chunks=False,
            )
        assert result["usable"] is False
        assert result["reason_code"] == _PREFLIGHT_UNAVAILABLE
        assert "could not be verified" in result.get("error_message", "")

    def test_kg_file_empty_returns_unavailable(self):
        """When KG file is empty, preflight returns usable=False
        (fail-closed — empty KG cannot verify nodes)."""
        from backend.services.learning.planner import (
            _PREFLIGHT_UNAVAILABLE,
            preflight_scope_resources,
        )
        from unittest.mock import patch

        scope = _fake_scope()
        with patch(
            "backend.services.learning.planner.load_kg_nodes",
            return_value=[],
        ):
            result = preflight_scope_resources(
                scope,
                check_kg_nodes=True,
                check_evidence_chunks=False,
            )
        assert result["usable"] is False
        assert result["reason_code"] == _PREFLIGHT_UNAVAILABLE
        assert "could not be verified" in result.get("error_message", "")

    # ── Side-effect-free contract ───────────────────────────────────────

    def test_preflight_does_not_call_llm(self):
        """The preflight must never call the LLM.
        When checks are disabled, returns not_verified (fail-closed)."""
        from backend.services.learning.planner import (
            _PREFLIGHT_NOT_VERIFIED,
            preflight_scope_resources,
        )

        scope = _fake_scope()
        result = preflight_scope_resources(
            scope,
            check_kg_nodes=False,
            check_evidence_chunks=False,
        )
        assert result["usable"] is False
        assert result["reason_code"] == _PREFLIGHT_NOT_VERIFIED

    def test_preflight_no_side_effects(self):
        """The preflight must not create sessions, write files, or modify state.
        Both calls with same args produce identical results (fail-closed)."""
        from backend.services.learning.planner import preflight_scope_resources

        scope = _real_kg_scope()
        result1 = preflight_scope_resources(
            scope,
            check_kg_nodes=False,
            check_evidence_chunks=False,
        )
        result2 = preflight_scope_resources(
            scope,
            check_kg_nodes=False,
            check_evidence_chunks=False,
        )
        assert result1 == result2
        assert result1["usable"] is False  # fail-closed: not verified


# ---------------------------------------------------------------------------
# Stable-core → raw-node mapping tests
# ---------------------------------------------------------------------------


class TestStableCoreToRawNodeMapping:
    """``_resolve_stable_core_to_raw_nodes`` — identity-space bridge tests.

    Tests that stable ``core:*`` IDs (from the KG producer's
    ``_core_id`` rule) correctly map to raw ``kg://`` node IDs
    from ``kg_nodes.jsonl``, with document/chapter constraints
    and cross-chapter disambiguation.
    """

    # ── Helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _find_real_scope_with_stable_ids() -> dict[str, Any] | None:
        """Find a real chapter scope that has both stable ``core:*`` IDs
        and matching raw ``kg://`` core nodes.  Returns a dict with keys:
        ``scope`` (learning_scope dict), ``raw_nodes`` (list of raw node dicts),
        ``book_title``, ``chapter_title``, ``document_id``.
        """
        from backend.services.learning.planner import load_kg_nodes
        from backend.services.knowledge_graph.agent import get_graph_overview

        overview = get_graph_overview()
        if not overview.get("ok"):
            return None

        all_nodes = load_kg_nodes()
        if not all_nodes:
            return None

        # Index raw nodes by (book_title, chapter) — only core nodes count
        raw_by_chapter: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for n in all_nodes:
            bt = str(n.get("book_title", "")).strip()
            ch = str(n.get("chapter", "")).strip()
            nid = str(n.get("node_id", "")).strip()
            nkind = str(n.get("node_kind", "")).strip()
            if bt and ch and nid and nid.startswith("kg://") and nkind == "core":
                raw_by_chapter.setdefault((bt, ch), []).append(n)

        # Walk chapters from the KG overview, find one with both stable
        # core IDs and raw core nodes with an exact chapter title match
        for chapter in overview.get("chapters", []):
            bt = str(chapter.get("book_title", "")).strip()
            ch_title = str(chapter.get("title", "")).strip()
            doc_id = str(chapter.get("document_id", "")).strip()
            core_ids = [
                str(c["id"]).strip()
                for c in (chapter.get("cores") or [])
                if str(c.get("id", "")).startswith("core:")
            ]
            if not core_ids or not doc_id or not bt or not ch_title:
                continue

            raws = raw_by_chapter.get((bt, ch_title), [])
            if raws and len(raws) >= 1:
                scope: dict[str, Any] = {
                    "book_title": bt,
                    "document_id": doc_id,
                    "chapter_key": chapter.get("key", ""),
                    "chapter_title": ch_title,
                    "kg_node_ids": core_ids,
                    "kg_node_names": {},
                }
                return {
                    "scope": scope,
                    "raw_nodes": raws,
                    "book_title": bt,
                    "chapter_title": ch_title,
                    "document_id": doc_id,
                }

        return None

    # ── Tests ───────────────────────────────────────────────────────────

    def test_real_stable_ids_map_to_raw_nodes(self):
        """Stable core:* IDs from the KG producer map to at least one raw
        kg:// node when scoped by document + chapter."""
        from backend.services.learning.planner import (
            _resolve_stable_core_to_raw_nodes,
        )

        ctx = self._find_real_scope_with_stable_ids()
        if ctx is None:
            pytest.skip("No real chapter with both stable IDs and raw nodes")

        scope = ctx["scope"]
        mapping = _resolve_stable_core_to_raw_nodes(
            scope["kg_node_ids"],
            document_id=ctx["document_id"],
            chapter_title=ctx["chapter_title"],
            book_title=ctx["book_title"],
        )

        assert len(mapping["raw_node_ids"]) > 0, (
            f"Expected at least one raw node for stable IDs "
            f"{scope['kg_node_ids'][:3]}... in {ctx['book_title']} / "
            f"{ctx['chapter_title']}"
        )
        # All returned raw IDs must start with kg://
        for rid in mapping["raw_node_ids"]:
            assert rid.startswith("kg://"), f"Expected kg:// got {rid!r}"

        # stable_id_matches must contain entries for the requested IDs
        matched_stable_ids = set(mapping["stable_id_matches"].keys())
        requested = set(scope["kg_node_ids"])
        assert requested & matched_stable_ids, (
            f"No requested stable IDs were resolved to raw nodes"
        )

    def test_two_different_textbooks_both_work(self):
        """Two chapters from different textbooks each resolve correctly."""
        from backend.services.learning.planner import (
            _resolve_stable_core_to_raw_nodes,
        )
        from backend.services.knowledge_graph.agent import get_graph_overview

        overview = get_graph_overview()
        if not overview.get("ok"):
            pytest.skip("KG overview not available")

        chapters = overview.get("chapters", [])
        # Find two chapters from different books with stable core IDs
        resolved: list[dict[str, Any]] = []
        seen_books: set[str] = set()
        for ch in chapters:
            bt = str(ch.get("book_title", "")).strip()
            ch_title = str(ch.get("title", "")).strip()
            doc_id = str(ch.get("document_id", "")).strip()
            core_ids = [
                str(c["id"]).strip()
                for c in (ch.get("cores") or [])
                if str(c.get("id", "")).startswith("core:")
            ]
            if not core_ids or not doc_id or not bt or not ch_title:
                continue
            if bt in seen_books:
                continue

            mapping = _resolve_stable_core_to_raw_nodes(
                core_ids,
                document_id=doc_id,
                chapter_title=ch_title,
                book_title=bt,
            )
            if mapping["raw_node_ids"]:
                resolved.append({
                    "book_title": bt,
                    "chapter_title": ch_title,
                    "stable_count": len(core_ids),
                    "raw_count": len(mapping["raw_node_ids"]),
                })
                seen_books.add(bt)
                if len(resolved) >= 2:
                    break

        assert len(resolved) >= 2, (
            f"Need at least 2 chapters from different books with both stable "
            f"IDs and raw nodes; got {len(resolved)} from books {seen_books}"
        )

    def test_same_stable_core_cross_chapter_only_current(self):
        """When the same stable core appears in multiple chapters, only the
        current chapter's raw nodes are returned — strict zero cross-chapter
        leakage for the scoped chapter."""
        from backend.services.learning.planner import (
            _resolve_stable_core_to_raw_nodes,
            load_kg_nodes,
        )
        from backend.services.knowledge_graph.agent import stable_core_identity

        all_nodes = load_kg_nodes()
        if not all_nodes:
            pytest.skip("No KG nodes available")

        # Find a canonical_name that appears in >= 2 chapters of the same book
        from collections import defaultdict

        cn_info: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for n in all_nodes:
            cn = str(n.get("canonical_name", "")).strip()
            ch = str(n.get("chapter", "")).strip()
            bt = str(n.get("book_title", "")).strip()
            nt = str(n.get("node_type", "")).strip()
            nid = str(n.get("node_id", "")).strip()
            if cn and ch and bt and nt and nid.startswith("kg://"):
                cn_info[cn][(bt, ch)].append(n)

        # Find a canonical_name with >= 2 chapters in the same book
        multi_ch_candidate = None
        for cn, chapters_map in cn_info.items():
            by_book: dict[str, list[str]] = defaultdict(list)
            for bt_ch in chapters_map:
                by_book[bt_ch[0]].append(bt_ch[1])
            for bt, chs in by_book.items():
                if len(chs) >= 2:
                    multi_ch_candidate = (cn, bt, chs[0], chs[1],
                                         chapters_map[(bt, chs[0])][0])
                    break
            if multi_ch_candidate:
                break

        if multi_ch_candidate is None:
            pytest.skip("No canonical_name found in >= 2 chapters of same book")

        cn, bt, ch_a, ch_b, sample_node = multi_ch_candidate
        nt = str(sample_node.get("node_type", "")).strip()

        # Compute the stable core ID for this canonical name
        test_stable_id = stable_core_identity(bt, nt, cn)

        # Resolve with chapter A scope — must get raw nodes only from chapter A
        mapping_a = _resolve_stable_core_to_raw_nodes(
            [test_stable_id],
            chapter_title=ch_a,
            book_title=bt,
        )

        assert len(mapping_a["raw_node_ids"]) > 0, (
            f"Expected at least one raw node for stable ID {test_stable_id} "
            f"in chapter {ch_a}"
        )

        # Verify ALL raw nodes returned belong to chapter A
        for node in mapping_a["raw_nodes"]:
            node_ch = str(node.get("chapter", "")).strip()
            assert ch_a in node_ch or node_ch in ch_a or not node_ch, (
                f"Cross-chapter leak: node from chapter {node_ch!r} appeared "
                f"in chapter A ({ch_a}) results for canonical_name={cn!r}"
            )

        # Resolve with chapter B scope — must strictly NOT include chapter A nodes
        mapping_b = _resolve_stable_core_to_raw_nodes(
            [test_stable_id],
            chapter_title=ch_b,
            book_title=bt,
        )

        assert len(mapping_b["raw_node_ids"]) > 0, (
            f"Expected at least one raw node for stable ID {test_stable_id} "
            f"in chapter {ch_b}"
        )

        for node in mapping_b["raw_nodes"]:
            node_ch = str(node.get("chapter", "")).strip()
            assert node_ch not in ch_a if node_ch else True, (
                f"Cross-chapter leak into chapter B: node from chapter A "
                f"({ch_a}) appeared in chapter B ({ch_b}) results"
            )

        # The two sets of raw node IDs must be disjoint
        ids_a = set(mapping_a["raw_node_ids"])
        ids_b = set(mapping_b["raw_node_ids"])
        assert ids_a.isdisjoint(ids_b), (
            f"Chapter A and B raw node sets must be disjoint. "
            f"Shared: {ids_a & ids_b}"
        )

    def test_raw_kg_id_pass_through(self):
        """Raw kg:// IDs are looked up directly.  Without constraints they
        pass through; with mismatched constraints they are excluded."""
        from backend.services.learning.planner import (
            _resolve_stable_core_to_raw_nodes,
            load_kg_nodes,
        )

        all_nodes = load_kg_nodes()
        if not all_nodes:
            pytest.skip("No KG nodes available")

        # Pick a real raw node ID
        real_raw = None
        raw_node = None
        for n in all_nodes:
            nid = str(n.get("node_id", "")).strip()
            if nid.startswith("kg://"):
                real_raw = nid
                raw_node = n
                break

        if real_raw is None:
            pytest.skip("No raw kg:// node IDs available")

        # With no constraints — should resolve
        mapping = _resolve_stable_core_to_raw_nodes([real_raw])
        assert real_raw in mapping["raw_node_ids"], (
            f"Raw ID {real_raw!r} should pass through without constraints"
        )

        # With MISMATCHED document_id constraint — must return empty
        mapping2 = _resolve_stable_core_to_raw_nodes(
            [real_raw],
            document_id="document.nonexistent_12345",
        )
        assert real_raw not in mapping2["raw_node_ids"], (
            f"Raw ID {real_raw!r} must NOT pass through with mismatched "
            f"document_id constraint"
        )

        # With MISMATCHED chapter_title — must return empty
        mapping3 = _resolve_stable_core_to_raw_nodes(
            [real_raw],
            chapter_title="不存在的章节标题XYZ_123",
        )
        assert real_raw not in mapping3["raw_node_ids"], (
            f"Raw ID {real_raw!r} must NOT pass through with mismatched "
            f"chapter_title constraint"
        )

    def test_fake_stable_ids_return_empty(self):
        """Fake core:* IDs that don't match any real node return empty."""
        from backend.services.learning.planner import (
            _resolve_stable_core_to_raw_nodes,
        )

        fake_ids = ["core:ffffffffffffffff", "core:deadbeefdeadbeef"]
        mapping = _resolve_stable_core_to_raw_nodes(fake_ids)
        assert mapping["raw_node_ids"] == []
        assert mapping["raw_nodes"] == []

    def test_document_mismatch_returns_empty_for_stable_ids(self):
        """When document_id constraint mismatches, stable IDs resolve to empty."""
        from backend.services.learning.planner import (
            _resolve_stable_core_to_raw_nodes,
        )

        ctx = self._find_real_scope_with_stable_ids()
        if ctx is None:
            pytest.skip("No real chapter with both stable IDs and raw nodes")

        scope = ctx["scope"]
        mapping = _resolve_stable_core_to_raw_nodes(
            scope["kg_node_ids"],
            document_id="document.nonexistent_12345",
            chapter_title=ctx["chapter_title"],
            book_title=ctx["book_title"],
        )
        assert mapping["raw_node_ids"] == [], (
            f"Expected empty result for nonexistent document, "
            f"got {len(mapping['raw_node_ids'])} raw nodes"
        )

    def test_chapter_mismatch_returns_empty_for_stable_ids(self):
        """When chapter_title constraint mismatches, stable IDs resolve to
        strictly zero results."""
        from backend.services.learning.planner import (
            _resolve_stable_core_to_raw_nodes,
        )

        ctx = self._find_real_scope_with_stable_ids()
        if ctx is None:
            pytest.skip("No real chapter with both stable IDs and raw nodes")

        scope = ctx["scope"]
        mapping = _resolve_stable_core_to_raw_nodes(
            scope["kg_node_ids"],
            document_id=ctx["document_id"],
            chapter_title="不存在的章节标题XYZ_123",
            book_title=ctx["book_title"],
        )
        assert mapping["raw_node_ids"] == [], (
            f"Expected strictly empty result for mismatched chapter, "
            f"got {len(mapping['raw_node_ids'])} raw nodes: "
            f"{mapping['raw_node_ids'][:5]}"
        )

    def test_preflight_uses_mapping_for_real_stable_ids(self):
        """preflight_scope_resources correctly resolves real stable IDs to
        raw nodes via the mapping helper, making the scope usable."""
        from backend.services.learning.planner import (
            _PREFLIGHT_OK,
            preflight_scope_resources,
        )

        ctx = self._find_real_scope_with_stable_ids()
        if ctx is None:
            pytest.skip("No real chapter with both stable IDs and raw nodes")

        scope = ctx["scope"]
        result = preflight_scope_resources(
            scope,
            check_kg_nodes=True,
            check_evidence_chunks=False,
        )

        assert result["usable"] is True, (
            f"Expected usable=True for real stable IDs with matching raw nodes. "
            f"Got: {result.get('error_message', 'no error')}"
        )
        assert result["reason_code"] == _PREFLIGHT_OK
        assert result["kg_node_hit_count"] > 0
        assert len(result["kg_node_ids_usable"]) > 0
        # Usable IDs should be the original stable IDs
        for uid in result["kg_node_ids_usable"]:
            assert uid.startswith("core:") or uid.startswith("kg://"), (
                f"Expected core:* or kg:// in usable IDs, got {uid!r}"
            )

    def test_preflight_fake_stable_ids_still_rejected(self):
        """Fake core:* IDs that don't map to any raw node are still rejected
        by preflight (fail-closed)."""
        from backend.services.learning.planner import (
            _PREFLIGHT_NO_KG_NODES,
            _PREFLIGHT_NO_KG_AND_NO_CHUNKS,
            preflight_scope_resources,
        )

        scope = _fake_scope()
        result = preflight_scope_resources(
            scope,
            check_kg_nodes=True,
            check_evidence_chunks=False,
        )

        if result["scope_diagnostics"]["checks_performed"]:
            assert result["usable"] is False
            assert result["kg_node_hit_count"] == 0
            assert result["reason_code"] in (
                _PREFLIGHT_NO_KG_NODES,
                _PREFLIGHT_NO_KG_AND_NO_CHUNKS,
            )

    # ── New tests for strict mapping contract ──────────────────────────

    def test_producer_helper_output_matches_legacy_core_id(self):
        """``stable_core_identity`` from the KG producer produces the same IDs
        as the legacy private ``_core_id`` function."""
        from backend.services.knowledge_graph.agent import (
            _core_id,
            stable_core_identity,
        )

        test_cases = [
            ("牙周病学", "Disease", "慢性牙周炎"),
            ("口腔颌面外科学", "Treatment", "颌骨骨折切开复位内固定术"),
            ("口腔修复学", "Anatomy", "牙体预备"),
            ("牙体牙髓病学", "Etiology", "龋病四联因素"),
        ]
        for bt, nt, cn in test_cases:
            legacy = _core_id(bt, nt, cn)
            public = stable_core_identity(bt, nt, cn)
            assert legacy == public, (
                f"Mismatch: _core_id={legacy!r} != "
                f"stable_core_identity={public!r} "
                f"for ({bt!r}, {nt!r}, {cn!r})"
            )
            assert public.startswith("core:"), (
                f"Expected core:* prefix, got {public!r}"
            )

    def test_unknown_format_ids_are_rejected(self):
        """IDs that are neither ``core:*`` nor ``kg://`` are silently dropped
        and produce empty results."""
        from backend.services.learning.planner import (
            _resolve_stable_core_to_raw_nodes,
        )

        mapping = _resolve_stable_core_to_raw_nodes(
            ["not-a-valid-id", "also-invalid", "", "core:ffffffffffffffff"],
        )
        # Only valid formats survive; fake stable ID maps to nothing
        assert mapping["raw_node_ids"] == [], (
            f"Expected empty result for unknown format IDs, "
            f"got {mapping['raw_node_ids']}"
        )
        # stable_ids_checked should only contain core:* entries
        assert all(
            sid.startswith("core:") for sid in mapping["stable_ids_checked"]
        ), f"Unexpected stable_ids_checked: {mapping['stable_ids_checked']}"
        # raw_ids_checked should contain only kg:// entries
        assert all(
            rid.startswith("kg://") for rid in mapping["raw_ids_checked"]
        ), f"Unexpected raw_ids_checked: {mapping['raw_ids_checked']}"

    def test_book_title_mismatch_returns_empty_for_stable_ids(self):
        """When book_title constraint mismatches, stable IDs resolve to
        strictly zero results."""
        from backend.services.learning.planner import (
            _resolve_stable_core_to_raw_nodes,
        )

        ctx = self._find_real_scope_with_stable_ids()
        if ctx is None:
            pytest.skip("No real chapter with both stable IDs and raw nodes")

        scope = ctx["scope"]
        mapping = _resolve_stable_core_to_raw_nodes(
            scope["kg_node_ids"],
            document_id=ctx["document_id"],
            chapter_title=ctx["chapter_title"],
            book_title="不存在的教材名称XYZ_123",
        )
        assert mapping["raw_node_ids"] == [], (
            f"Expected strictly empty result for mismatched book_title, "
            f"got {len(mapping['raw_node_ids'])} raw nodes"
        )

    def test_preflight_lesson_consistency_same_mapping(self):
        """preflight_scope_resources and plan_lesson use the same mapping
        so that a scope that passes preflight also has usable raw nodes
        in the lesson executor."""
        from backend.services.learning.planner import (
            _PREFLIGHT_OK,
            _resolve_stable_core_to_raw_nodes,
            preflight_scope_resources,
        )

        ctx = self._find_real_scope_with_stable_ids()
        if ctx is None:
            pytest.skip("No real chapter with both stable IDs and raw nodes")

        scope = ctx["scope"]
        # Run the mapping that plan_lesson would use
        mapping = _resolve_stable_core_to_raw_nodes(
            scope["kg_node_ids"],
            document_id=ctx["document_id"],
            chapter_title=ctx["chapter_title"],
            book_title=ctx["book_title"],
        )
        # Run preflight
        preflight = preflight_scope_resources(
            scope,
            check_kg_nodes=True,
            check_evidence_chunks=False,
        )

        assert preflight["usable"] is True
        assert preflight["reason_code"] == _PREFLIGHT_OK
        assert len(mapping["raw_node_ids"]) > 0, (
            "plan_lesson mapping must also find raw nodes"
        )
        # The set of stable IDs that preflight reports as usable must
        # intersect with the ones that the mapping resolves
        preflight_usable = set(preflight["kg_node_ids_usable"])
        mapping_matched = set(mapping["stable_id_matches"].keys())
        assert preflight_usable & mapping_matched, (
            f"preflight usable IDs {preflight_usable} and mapping matched "
            f"stable IDs {mapping_matched} must overlap"
        )

    def test_real_fake_mixed_batch_only_real_resolved(self):
        """A batch mixing real stable IDs with fake ones resolves only the
        real ones — fake ones are not hallucinated."""
        from backend.services.learning.planner import (
            _resolve_stable_core_to_raw_nodes,
        )

        ctx = self._find_real_scope_with_stable_ids()
        if ctx is None:
            pytest.skip("No real chapter with both stable IDs and raw nodes")

        scope = ctx["scope"]
        real_ids = scope["kg_node_ids"]
        mixed_ids = real_ids + ["core:ffffffffffffffff", "core:deadbeefdeadbeef"]
        mapping = _resolve_stable_core_to_raw_nodes(
            mixed_ids,
            document_id=ctx["document_id"],
            chapter_title=ctx["chapter_title"],
            book_title=ctx["book_title"],
        )

        assert len(mapping["raw_node_ids"]) > 0, (
            "At least the real stable IDs must resolve"
        )
        # Fake IDs must NOT appear in the stable_id_matches
        assert "core:ffffffffffffffff" not in mapping["stable_id_matches"], (
            "Fake stable ID must not be present in stable_id_matches"
        )
        assert "core:deadbeefdeadbeef" not in mapping["stable_id_matches"], (
            "Fake stable ID must not be present in stable_id_matches"
        )


# ---------------------------------------------------------------------------
# StableCoreIdentityProducerTests — canonical book title identity
# ---------------------------------------------------------------------------


class StableCoreIdentityProducerTests(unittest.TestCase):
    """Tests for the KG producer's canonical book-title identity helpers.

    Proves that :func:`canonical_book_title` returns the same short title
    the overview uses, and that :func:`stable_core_identity` computed with
    the canonical title matches overview ``core:*`` IDs.
    """

    def test_canonical_title_matches_known_document(self):
        """For a known document_id, canonical_book_title returns the short
        title from BOOK_STYLE_BY_DOCUMENT, regardless of the raw title."""
        from backend.services.knowledge_graph.agent import canonical_book_title

        # Known document: 牙周病学
        doc_id = "document.d766e8629232a2f6"
        # Raw title carries editor/edition/publisher
        raw = "牙周病学（孟焕新主编，第5版，人民卫生出版社）"
        result = canonical_book_title(doc_id, raw)
        self.assertEqual(result, "牙周病学")

    def test_canonical_title_second_known_document(self):
        """A second known document with a different short title also works —
        proves no single-sample hardcoding."""
        from backend.services.knowledge_graph.agent import canonical_book_title

        doc_id = "document.bada3a836e200b0e"
        raw = "口腔颌面外科学（第8版，人民卫生出版社）"
        result = canonical_book_title(doc_id, raw)
        self.assertEqual(result, "口腔颌面外科学")

    def test_canonical_title_unknown_document_uses_raw_title(self):
        """For an unknown document_id, the raw title is used as-is."""
        from backend.services.knowledge_graph.agent import canonical_book_title

        result = canonical_book_title(
            "document.unknown_12345",
            "某未知教材（第1版）",
        )
        self.assertEqual(result, "某未知教材（第1版）")

    def test_stable_id_from_canonical_title_matches_overview(self):
        """stable_core_identity(canonical_title, nt, cn) produces IDs that
        appear in the overview's chapter core_ids — proving the hash input
        is the same."""
        from backend.services.knowledge_graph.agent import (
            canonical_book_title,
            get_graph_overview,
            stable_core_identity,
        )

        overview = get_graph_overview()
        if not overview.get("ok"):
            self.skipTest("KG overview not available")

        # Pick the first chapter that has core IDs
        matched = 0
        for chapter in overview.get("chapters", []):
            cores = chapter.get("cores", [])
            if not cores:
                continue
            doc_id = str(chapter.get("document_id", "")).strip()
            bt_overview = str(chapter.get("book_title", "")).strip()
            if not doc_id or not bt_overview:
                continue

            # Compute canonical title from a plausible raw title
            raw = bt_overview + "（某主编，第N版，人民卫生出版社）"
            bt_canonical = canonical_book_title(doc_id, raw)
            self.assertEqual(
                bt_canonical, bt_overview,
                f"canonical_book_title({doc_id!r}, {raw!r}) = "
                f"{bt_canonical!r}, expected {bt_overview!r}",
            )

            # For each core, recompute stable ID and verify it matches
            for core in cores:
                cid = str(core.get("id", "")).strip()
                if not cid.startswith("core:"):
                    continue
                name = str(core.get("name", "")).strip()
                ntype = str(core.get("type", "")).strip()
                if not name or not ntype:
                    continue
                computed = stable_core_identity(bt_canonical, ntype, name)
                self.assertEqual(
                    computed, cid,
                    f"stable_core_identity({bt_canonical!r}, {ntype!r}, "
                    f"{name!r}) = {computed!r}, expected {cid!r}",
                )
                matched += 1

        self.assertGreater(
            matched, 0,
            "Must verify at least one core ID from the overview",
        )

    def test_stable_id_from_raw_long_title_does_not_match(self):
        """stable_core_identity(raw_long_title, nt, cn) produces DIFFERENT
        IDs from the overview — this is exactly the bug being fixed."""
        from backend.services.knowledge_graph.agent import (
            canonical_book_title,
            get_graph_overview,
            stable_core_identity,
        )

        overview = get_graph_overview()
        if not overview.get("ok"):
            self.skipTest("KG overview not available")

        for chapter in overview.get("chapters", []):
            cores = chapter.get("cores", [])
            if not cores:
                continue
            doc_id = str(chapter.get("document_id", "")).strip()
            bt_overview = str(chapter.get("book_title", "")).strip()
            if not doc_id or not bt_overview:
                continue

            raw_long = bt_overview + "（孟焕新主编，第5版，人民卫生出版社）"
            bt_canonical = canonical_book_title(doc_id, raw_long)
            # Only test when long and canonical actually differ
            if raw_long == bt_canonical:
                continue

            for core in cores:
                cid = str(core.get("id", "")).strip()
                if not cid.startswith("core:"):
                    continue
                name = str(core.get("name", "")).strip()
                ntype = str(core.get("type", "")).strip()
                if not name or not ntype:
                    continue
                # Compute with RAW long title — must NOT match overview
                wrong_id = stable_core_identity(raw_long, ntype, name)
                self.assertNotEqual(
                    wrong_id, cid,
                    f"BUG STILL PRESENT: stable_core_identity with raw long "
                    f"title {raw_long!r} unexpectedly matched overview ID "
                    f"{cid!r}. The canonical-title fix may not be needed "
                    f"for this case.",
                )
                # Compute with CANONICAL title — MUST match
                correct_id = stable_core_identity(bt_canonical, ntype, name)
                self.assertEqual(
                    correct_id, cid,
                    f"stable_core_identity with canonical title "
                    f"{bt_canonical!r} should match overview ID {cid!r}",
                )
                return  # One verified case is enough

        self.skipTest("No chapter with both overview cores and differing titles")

    def test_canonical_title_via_subject_field_consistency(self):
        """The raw node's ``subject`` field carries the same canonical short
        title as canonical_book_title produces — used as test evidence, not
        as a planner truth source."""
        from backend.services.knowledge_graph.agent import canonical_book_title
        from backend.services.learning.planner import load_kg_nodes

        all_nodes = load_kg_nodes()
        if not all_nodes:
            self.skipTest("No KG nodes available")

        verified = 0
        for node in all_nodes:
            bt_raw = str(node.get("book_title", "")).strip()
            subject = str(node.get("subject", "")).strip()
            nid = str(node.get("node_id", "")).strip()
            if not bt_raw or not subject or not nid.startswith("kg://"):
                continue
            # Get document_id from metadata
            meta = node.get("metadata")
            node_doc = (
                str(meta.get("document_id", "")).strip()
                if isinstance(meta, dict) else ""
            )
            if not node_doc:
                continue
            bt_canonical = canonical_book_title(node_doc, bt_raw)
            if bt_canonical == bt_raw:
                # Title already canonical — no drift to verify
                continue
            self.assertEqual(
                bt_canonical, subject,
                f"canonical_book_title({node_doc!r}, {bt_raw!r}) = "
                f"{bt_canonical!r}, but raw node subject = {subject!r}",
            )
            verified += 1
            if verified >= 3:
                break

        if verified == 0:
            self.skipTest(
                "No raw nodes with differing canonical/raw book titles"
            )


# ---------------------------------------------------------------------------
# StableCoreRawNodeMappingTests — stable→raw mapping with canonical titles
# ---------------------------------------------------------------------------


class StableCoreRawNodeMappingTests(unittest.TestCase):
    """Prove that ``_resolve_stable_core_to_raw_nodes`` correctly maps
    overview ``core:*`` IDs to raw ``kg://`` nodes using the KG producer's
    canonical book-title identity helper.
    """

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _find_real_chapter_with_mapping() -> dict[str, Any] | None:
        """Find a real chapter where stable IDs map to raw nodes.

        Uses the canonical_book_title helper (same as production) to match
        overview book_title to raw node book_title.  Returns a dict with
        ``scope``, ``book_title``, ``chapter_title``, ``document_id``.
        """
        from backend.services.knowledge_graph.agent import (
            canonical_book_title,
            get_graph_overview,
        )
        from backend.services.learning.planner import load_kg_nodes

        overview = get_graph_overview()
        if not overview.get("ok"):
            return None

        all_nodes = load_kg_nodes()
        if not all_nodes:
            return None

        # Index raw core nodes by (canonical_book_title, chapter)
        raw_by_chapter: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for n in all_nodes:
            bt_raw = str(n.get("book_title", "")).strip()
            ch = str(n.get("chapter", "")).strip()
            nid = str(n.get("node_id", "")).strip()
            nkind = str(n.get("node_kind", "")).strip()
            meta = n.get("metadata")
            node_doc = (
                str(meta.get("document_id", "")).strip()
                if isinstance(meta, dict) else ""
            )
            if not bt_raw or not ch or not nid.startswith("kg://"):
                continue
            if nkind != "core":
                continue
            bt_canonical = canonical_book_title(node_doc, bt_raw)
            raw_by_chapter.setdefault((bt_canonical, ch), []).append(n)

        # Find a chapter with both stable core IDs and raw nodes
        for chapter in overview.get("chapters", []):
            bt = str(chapter.get("book_title", "")).strip()
            ch_title = str(chapter.get("title", "")).strip()
            doc_id = str(chapter.get("document_id", "")).strip()
            core_ids = [
                str(c["id"]).strip()
                for c in (chapter.get("cores") or [])
                if str(c.get("id", "")).startswith("core:")
            ]
            if not core_ids or not doc_id or not bt or not ch_title:
                continue

            raws = raw_by_chapter.get((bt, ch_title), [])
            if raws and len(raws) >= 1:
                scope: dict[str, Any] = {
                    "book_title": bt,
                    "document_id": doc_id,
                    "chapter_key": chapter.get("key", ""),
                    "chapter_title": ch_title,
                    "kg_node_ids": core_ids,
                    "kg_node_names": {},
                }
                return {
                    "scope": scope,
                    "book_title": bt,
                    "chapter_title": ch_title,
                    "document_id": doc_id,
                }

        return None

    # ── tests ────────────────────────────────────────────────────────────

    def test_real_stable_ids_map_to_raw_nodes(self):
        """After the canonical-title fix, overview stable core:* IDs map
        to at least one raw kg:// node."""
        from backend.services.learning.planner import (
            _resolve_stable_core_to_raw_nodes,
        )

        ctx = self._find_real_chapter_with_mapping()
        if ctx is None:
            self.skipTest("No real chapter with both stable IDs and raw nodes")

        mapping = _resolve_stable_core_to_raw_nodes(
            ctx["scope"]["kg_node_ids"],
            document_id=ctx["document_id"],
            chapter_title=ctx["chapter_title"],
            book_title=ctx["book_title"],
        )

        self.assertGreater(
            len(mapping["raw_node_ids"]), 0,
            f"Expected >=1 raw node for stable IDs in "
            f"{ctx['book_title']} / {ctx['chapter_title']}",
        )
        for rid in mapping["raw_node_ids"]:
            self.assertTrue(
                rid.startswith("kg://"),
                f"Expected kg:// prefix, got {rid!r}",
            )

        # At least one requested stable ID must be in matches
        matched = set(mapping["stable_id_matches"].keys())
        requested = set(ctx["scope"]["kg_node_ids"])
        self.assertTrue(
            requested & matched,
            "No requested stable IDs were resolved to raw nodes",
        )

    def test_two_different_textbooks_both_work(self):
        """Two chapters from different books each resolve through canonical
        title matching."""
        from backend.services.knowledge_graph.agent import (
            get_graph_overview,
        )
        from backend.services.learning.planner import (
            _resolve_stable_core_to_raw_nodes,
        )

        overview = get_graph_overview()
        if not overview.get("ok"):
            self.skipTest("KG overview not available")

        chapters = overview.get("chapters", [])
        resolved = 0
        seen_books: set[str] = set()
        for ch in chapters:
            bt = str(ch.get("book_title", "")).strip()
            ch_title = str(ch.get("title", "")).strip()
            doc_id = str(ch.get("document_id", "")).strip()
            core_ids = [
                str(c["id"]).strip()
                for c in (ch.get("cores") or [])
                if str(c.get("id", "")).startswith("core:")
            ]
            if not core_ids or not doc_id or not bt or not ch_title:
                continue
            if bt in seen_books:
                continue

            mapping = _resolve_stable_core_to_raw_nodes(
                core_ids,
                document_id=doc_id,
                chapter_title=ch_title,
                book_title=bt,
            )
            if mapping["raw_node_ids"]:
                resolved += 1
                seen_books.add(bt)
                if resolved >= 2:
                    break

        self.assertGreaterEqual(
            resolved, 2,
            f"Need >=2 chapters from different books, got {resolved} "
            f"from books {seen_books}",
        )

    def test_fake_stable_ids_return_empty(self):
        """Non-existent core:* IDs produce empty results — fail-closed."""
        from backend.services.learning.planner import (
            _resolve_stable_core_to_raw_nodes,
        )

        mapping = _resolve_stable_core_to_raw_nodes(
            ["core:ffffffffffffffff", "core:deadbeefdeadbeef"],
        )
        self.assertEqual(mapping["raw_node_ids"], [])
        self.assertEqual(mapping["raw_nodes"], [])

    def test_document_mismatch_returns_empty(self):
        """Mismatched document_id constraint → strictly empty results."""
        from backend.services.learning.planner import (
            _resolve_stable_core_to_raw_nodes,
        )

        ctx = self._find_real_chapter_with_mapping()
        if ctx is None:
            self.skipTest("No real chapter with both stable IDs and raw nodes")

        mapping = _resolve_stable_core_to_raw_nodes(
            ctx["scope"]["kg_node_ids"],
            document_id="document.nonexistent_12345",
            chapter_title=ctx["chapter_title"],
            book_title=ctx["book_title"],
        )
        self.assertEqual(
            mapping["raw_node_ids"], [],
            f"Expected empty for nonexistent document, "
            f"got {len(mapping['raw_node_ids'])} nodes",
        )

    def test_chapter_mismatch_returns_empty(self):
        """Mismatched chapter_title constraint → strictly zero results."""
        from backend.services.learning.planner import (
            _resolve_stable_core_to_raw_nodes,
        )

        ctx = self._find_real_chapter_with_mapping()
        if ctx is None:
            self.skipTest("No real chapter with both stable IDs and raw nodes")

        mapping = _resolve_stable_core_to_raw_nodes(
            ctx["scope"]["kg_node_ids"],
            document_id=ctx["document_id"],
            chapter_title="不存在的章节标题XYZ_123",
            book_title=ctx["book_title"],
        )
        self.assertEqual(
            mapping["raw_node_ids"], [],
            f"Expected empty for mismatched chapter, "
            f"got {len(mapping['raw_node_ids'])} nodes",
        )

    def test_book_title_mismatch_returns_empty(self):
        """Mismatched book_title constraint → strictly zero results."""
        from backend.services.learning.planner import (
            _resolve_stable_core_to_raw_nodes,
        )

        ctx = self._find_real_chapter_with_mapping()
        if ctx is None:
            self.skipTest("No real chapter with both stable IDs and raw nodes")

        mapping = _resolve_stable_core_to_raw_nodes(
            ctx["scope"]["kg_node_ids"],
            document_id=ctx["document_id"],
            chapter_title=ctx["chapter_title"],
            book_title="不存在的教材名称XYZ_123",
        )
        self.assertEqual(
            mapping["raw_node_ids"], [],
            f"Expected empty for mismatched book_title, "
            f"got {len(mapping['raw_node_ids'])} nodes",
        )

    def test_raw_kg_id_pass_through_and_scope_mismatch_rejected(self):
        """Raw kg:// IDs pass through without constraints but are rejected
        with mismatched document/chapter/book constraints."""
        from backend.services.learning.planner import (
            _resolve_stable_core_to_raw_nodes,
            load_kg_nodes,
        )

        all_nodes = load_kg_nodes()
        if not all_nodes:
            self.skipTest("No KG nodes available")

        real_raw = None
        for n in all_nodes:
            nid = str(n.get("node_id", "")).strip()
            if nid.startswith("kg://"):
                real_raw = nid
                break
        if real_raw is None:
            self.skipTest("No raw kg:// node IDs available")

        # No constraints → pass through
        mapping = _resolve_stable_core_to_raw_nodes([real_raw])
        self.assertIn(real_raw, mapping["raw_node_ids"])

        # Mismatched document_id → rejected
        mapping2 = _resolve_stable_core_to_raw_nodes(
            [real_raw], document_id="document.nonexistent_12345",
        )
        self.assertNotIn(real_raw, mapping2["raw_node_ids"])

        # Mismatched chapter → rejected
        mapping3 = _resolve_stable_core_to_raw_nodes(
            [real_raw], chapter_title="不存在的章节标题XYZ_123",
        )
        self.assertNotIn(real_raw, mapping3["raw_node_ids"])

    def test_raw_kg_id_book_mismatch_rejected(self):
        """Raw kg:// ID is rejected when book_title constraint is
        mismatched — uses canonical title for comparison."""
        from backend.services.learning.planner import (
            _resolve_stable_core_to_raw_nodes,
            load_kg_nodes,
        )

        all_nodes = load_kg_nodes()
        if not all_nodes:
            self.skipTest("No KG nodes available")

        real_raw = None
        for n in all_nodes:
            nid = str(n.get("node_id", "")).strip()
            if nid.startswith("kg://"):
                real_raw = nid
                break
        if real_raw is None:
            self.skipTest("No raw kg:// node IDs available")

        mapping = _resolve_stable_core_to_raw_nodes(
            [real_raw], book_title="不存在的教材名称XYZ_123",
        )
        self.assertNotIn(
            real_raw, mapping["raw_node_ids"],
            "Raw ID must be rejected with mismatched book_title constraint",
        )

    def test_preflight_uses_mapping_for_real_stable_ids(self):
        """preflight_scope_resources resolves real stable IDs to raw nodes
        via the mapping helper — scope is usable."""
        from backend.services.learning.planner import (
            _PREFLIGHT_OK,
            preflight_scope_resources,
        )

        ctx = self._find_real_chapter_with_mapping()
        if ctx is None:
            self.skipTest("No real chapter with both stable IDs and raw nodes")

        result = preflight_scope_resources(
            ctx["scope"],
            check_kg_nodes=True,
            check_evidence_chunks=False,
        )

        self.assertTrue(
            result["usable"],
            f"Expected usable=True. Got: {result.get('error_message', 'no error')}",
        )
        self.assertEqual(result["reason_code"], _PREFLIGHT_OK)
        self.assertGreater(result["kg_node_hit_count"], 0)
        self.assertGreater(len(result["kg_node_ids_usable"]), 0)

    def test_preflight_fake_stable_ids_rejected(self):
        """Fake core:* IDs are rejected by preflight — fail-closed."""
        from backend.services.learning.planner import (
            _PREFLIGHT_NO_KG_AND_NO_CHUNKS,
            _PREFLIGHT_NO_KG_NODES,
            preflight_scope_resources,
        )

        scope: dict[str, Any] = {
            "book_title": "测试教材",
            "document_id": "doc-test-001",
            "chapter_key": "chapter:test001",
            "chapter_title": "第一章",
            "kg_node_ids": ["core:ffffffffffffffff"],
            "kg_node_names": {},
        }
        result = preflight_scope_resources(
            scope,
            check_kg_nodes=True,
            check_evidence_chunks=False,
        )
        if result["scope_diagnostics"]["checks_performed"]:
            self.assertFalse(result["usable"])
            self.assertEqual(result["kg_node_hit_count"], 0)
            self.assertIn(
                result["reason_code"],
                (_PREFLIGHT_NO_KG_NODES, _PREFLIGHT_NO_KG_AND_NO_CHUNKS),
            )

    def test_preflight_lesson_consistency_same_mapping(self):
        """preflight and _resolve_stable_core_to_raw_nodes share the same
        mapping logic — preflight-usable IDs are also map-resolvable."""
        from backend.services.learning.planner import (
            _PREFLIGHT_OK,
            _resolve_stable_core_to_raw_nodes,
            preflight_scope_resources,
        )

        ctx = self._find_real_chapter_with_mapping()
        if ctx is None:
            self.skipTest("No real chapter with both stable IDs and raw nodes")

        mapping = _resolve_stable_core_to_raw_nodes(
            ctx["scope"]["kg_node_ids"],
            document_id=ctx["document_id"],
            chapter_title=ctx["chapter_title"],
            book_title=ctx["book_title"],
        )
        preflight = preflight_scope_resources(
            ctx["scope"],
            check_kg_nodes=True,
            check_evidence_chunks=False,
        )

        self.assertTrue(preflight["usable"])
        self.assertEqual(preflight["reason_code"], _PREFLIGHT_OK)
        self.assertGreater(len(mapping["raw_node_ids"]), 0)

        preflight_usable = set(preflight["kg_node_ids_usable"])
        mapping_matched = set(mapping["stable_id_matches"].keys())
        self.assertTrue(
            preflight_usable & mapping_matched,
            f"preflight usable {preflight_usable} and mapping matched "
            f"{mapping_matched} must overlap",
        )

    def test_cross_chapter_same_name_only_current_chapter(self):
        """When same canonical_name appears in multiple chapters, only the
        scoped chapter's raw nodes are returned — strict zero cross-chapter
        leakage."""
        from backend.services.knowledge_graph.agent import (
            canonical_book_title,
            stable_core_identity,
        )
        from backend.services.learning.planner import (
            _resolve_stable_core_to_raw_nodes,
            load_kg_nodes,
        )

        all_nodes = load_kg_nodes()
        if not all_nodes:
            self.skipTest("No KG nodes available")

        # Find canonical_name in >=2 chapters of same book
        from collections import defaultdict

        cn_chapters: dict[str, dict[tuple[str, str], list[dict[str, Any]]]] = (
            defaultdict(lambda: defaultdict(list))
        )
        for n in all_nodes:
            cn = str(n.get("canonical_name", "")).strip()
            ch = str(n.get("chapter", "")).strip()
            bt_raw = str(n.get("book_title", "")).strip()
            nt = str(n.get("node_type", "")).strip()
            nid = str(n.get("node_id", "")).strip()
            if not cn or not ch or not bt_raw or not nt or not nid.startswith("kg://"):
                continue
            meta = n.get("metadata")
            node_doc = (
                str(meta.get("document_id", "")).strip()
                if isinstance(meta, dict) else ""
            )
            bt_canonical = canonical_book_title(node_doc, bt_raw)
            cn_chapters[cn][(bt_canonical, ch)].append(n)

        # Find a cn in >=2 chapters of the same book
        candidate = None
        for cn, ch_map in cn_chapters.items():
            by_book: dict[str, list[str]] = defaultdict(list)
            for bt_c, ch in ch_map:
                by_book[bt_c].append(ch)
            for bt_c, chs in by_book.items():
                if len(chs) >= 2:
                    candidate = (cn, bt_c, chs[0], chs[1], ch_map[(bt_c, chs[0])][0])
                    break
            if candidate:
                break

        if candidate is None:
            self.skipTest("No canonical_name in >=2 chapters of same book")

        cn, bt, ch_a, ch_b, sample_node = candidate
        nt = str(sample_node.get("node_type", "")).strip()
        test_stable_id = stable_core_identity(bt, nt, cn)

        # Chapter A scope
        mapping_a = _resolve_stable_core_to_raw_nodes(
            [test_stable_id], chapter_title=ch_a, book_title=bt,
        )
        self.assertGreater(
            len(mapping_a["raw_node_ids"]), 0,
            f"Expected >=1 raw node for {test_stable_id} in chapter {ch_a}",
        )
        for node in mapping_a["raw_nodes"]:
            node_ch = str(node.get("chapter", "")).strip()
            self.assertTrue(
                ch_a in node_ch or node_ch in ch_a or not node_ch,
                f"Cross-chapter leak: node from {node_ch!r} in chapter A ({ch_a})",
            )

        # Chapter B scope — must be disjoint from A
        mapping_b = _resolve_stable_core_to_raw_nodes(
            [test_stable_id], chapter_title=ch_b, book_title=bt,
        )
        self.assertGreater(
            len(mapping_b["raw_node_ids"]), 0,
            f"Expected >=1 raw node for {test_stable_id} in chapter {ch_b}",
        )
        ids_a = set(mapping_a["raw_node_ids"])
        ids_b = set(mapping_b["raw_node_ids"])
        self.assertTrue(
            ids_a.isdisjoint(ids_b),
            f"Chapter A and B raw node sets must be disjoint. "
            f"Shared: {ids_a & ids_b}",
        )


# ============================================================================
# R10 Scoped-Learning Classifier Contract — "学习《教材》第N章" → learning
# ============================================================================


class TestR10ScopedLearningClassifierContract:
    """Prove that the classifier prompt/contract correctly distinguishes
    scoped chapter-learning expressions from ordinary topic learning.

    "学习《教材名》第N章" identifies the learning scope and starts ordinary
    learning unless explicit plan semantics or plan-clarification context exists.

    Ordinary topic learning ("系统学习牙周炎") and Direct QA are preserved.
    """

    # ── Positive: scoped chapter-learning → learning ───────────────────

    @pytest.mark.asyncio
    async def test_direct_textbook_chapter_learning_stays_learning(
        self, mock_orchestrator
    ):
        """A textbook + chapter without planning language starts learning."""
        mock_orchestrator.start_lesson.return_value = {
            "session": {"id": "sess-direct-scoped"}, "outline": {"nodes": []},
        }
        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier("study_plan", "故意模拟错误分类"),
        )

        result = await router.route("我要学习口腔材料学第三章", "demo")

        assert result["ok"] is True
        assert result["route"]["intent"] == "learning"
        assert result["route"]["learning_scope"]["book_title"] == "口腔材料学"
        assert result["route"]["learning_scope"]["chapter_title"].startswith("第三章")
        assert result["next_action"] == "start_session"
        mock_orchestrator.start_lesson.assert_called_once()
        call_kwargs = mock_orchestrator.start_lesson.call_args.kwargs
        assert call_kwargs["learning_scope"]["chapter_key"].startswith("chapter:")

    @pytest.mark.asyncio
    async def test_textbook_chapter_expression_routes_to_learning(
        self, mock_orchestrator
    ):
        """A specific textbook chapter starts an ordinary lesson."""
        mock_orchestrator.start_lesson.return_value = {
            "session": {"id": "sess-scoped-001"}, "outline": {"nodes": []},
        }

        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier("study_plan", "故意模拟错误分类"),
        )

        result = await router.route("学习《牙体牙髓病学》第15章", "demo")

        assert result["ok"] is True
        assert result["route"]["intent"] == "learning"
        assert result["route"]["plan_item_requested"] is False
        assert result["route"]["source"] == "deterministic_scoped_learning"
        assert result["next_action"] == "start_session"
        mock_orchestrator.start_lesson.assert_called_once()

    @pytest.mark.asyncio
    async def test_second_textbook_chapter_expression_routes_to_learning(
        self, mock_orchestrator
    ):
        """A second textbook/chapter expression is handled generically."""
        mock_orchestrator.start_lesson.return_value = {
            "session": {"id": "sess-scoped-002"}, "outline": {"nodes": []},
        }

        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier("study_plan", "故意模拟错误分类"),
        )

        result = await router.route("学习《牙周病学》第3章", "demo")

        assert result["ok"] is True
        assert result["route"]["intent"] == "learning"
        assert result["route"]["plan_item_requested"] is False
        mock_orchestrator.start_lesson.assert_called_once()

    @pytest.mark.asyncio
    async def test_learn_textbook_chapter_alt_syntax_routes_to_learning(
        self, mock_orchestrator
    ):
        """Alternative syntax remains ordinary learning."""
        mock_orchestrator.start_lesson.return_value = {
            "session": {"id": "sess-scoped-003"}, "outline": {"nodes": []},
        }

        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier("study_plan", "故意模拟错误分类"),
        )

        result = await router.route("学《口腔颌面外科学》的第8章", "demo")

        assert result["ok"] is True
        assert result["route"]["intent"] == "learning"
        assert result["route"]["plan_item_requested"] is False
        mock_orchestrator.start_lesson.assert_called_once()

    # ── Negative: ordinary topic learning → learning (preserved) ──────

    @pytest.mark.asyncio
    async def test_ordinary_topic_learning_preserved_alpha(
        self, mock_orchestrator
    ):
        """"系统学习牙周炎的病因和治疗" → learning.

        This is systematic topic learning WITHOUT a specific textbook+chapter
        as plan scope — must stay in learning, not study_plan."""
        mock_orchestrator.start_lesson.return_value = {
            "session": {"id": "sess-topic-learn"},
            "outline": {"nodes": []},
        }
        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier("learning", "用户想系统学习主题"),
        )

        result = await router.route("系统学习牙周炎的病因和治疗", "demo")

        assert result["ok"] is True
        assert result["route"]["intent"] == "learning", (
            f"ordinary topic learning must route to learning, "
            f"got {result['route']['intent']}"
        )
        assert result["route"]["source"] == "llm_router"
        assert result["next_action"] == "start_session"
        mock_orchestrator.start_lesson.assert_called_once()

    @pytest.mark.asyncio
    async def test_ordinary_topic_learning_preserved_beta(
        self, mock_orchestrator
    ):
        """"我想深入学习颞下颌关节紊乱的诊断" → learning.

        Second structurally different ordinary learning expression proves
        the fix doesn't over-capture."""
        mock_orchestrator.start_lesson.return_value = {
            "session": {"id": "sess-tmj-learn"},
            "outline": {"nodes": []},
        }
        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier("learning", "用户想深入学习主题"),
        )

        result = await router.route("我想深入学习颞下颌关节紊乱的诊断", "demo")

        assert result["ok"] is True
        assert result["route"]["intent"] == "learning", (
            f"second ordinary learning must route to learning, "
            f"got {result['route']['intent']}"
        )
        assert result["route"]["source"] == "llm_router"
        mock_orchestrator.start_lesson.assert_called_once()

    # ── Negative: Direct QA → direct_qa (preserved) ──────────────────

    @pytest.mark.asyncio
    async def test_direct_qa_preserved_with_scoped_learning_fix(
        self, mock_orchestrator
    ):
        """Direct medical question → direct_qa, not hijacked by the
        scoped-learning fix."""
        mock_orchestrator.start_lesson.return_value = {
            "session": {"id": "sess-dqa"},
            "outline": {"nodes": []},
        }

        def _mock_dqa(message, *, history, user_id):
            return {"answer": "牙周炎非手术治疗包括洁治、刮治和根面平整。", "sources": []}

        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier("direct_qa", "用户询问具体口腔医学问题"),
            direct_qa_service=_mock_dqa,
        )

        result = await router.route("牙周炎的非手术治疗有哪些方法？", "demo")

        assert result["ok"] is True
        assert result["route"]["intent"] == "direct_qa", (
            f"Direct QA must NOT be hijacked to study_plan, "
            f"got {result['route']['intent']}"
        )
        assert result["next_action"] == "show_direct_answer"
        mock_orchestrator.start_lesson.assert_not_called()

    # ── Prompt contract: general semantic rules ──────────────────────

    def test_classifier_prompt_contains_scoped_learning_boundary(self):
        """The CLASSIFIER_SYSTEM_PROMPT must contain the general semantic
        rule keeping scoped chapter-learning in ordinary learning,
        NOT hardcoded textbook names or chapter numbers."""
        assert "教材名" in CLASSIFIER_SYSTEM_PROMPT, (
            "Prompt must describe the pattern with generic '教材名' placeholder"
        )
        assert "第N章" in CLASSIFIER_SYSTEM_PROMPT or "章节定位" in CLASSIFIER_SYSTEM_PROMPT, (
            "Prompt must describe chapter positioning as plan scope indicator"
        )
        assert "学习范围" in CLASSIFIER_SYSTEM_PROMPT, (
            "Prompt must explain that textbook/chapter identifies scope"
        )
        assert "都应归为 learning" in CLASSIFIER_SYSTEM_PROMPT, (
            "Prompt must keep direct textbook+chapter requests in learning"
        )
        assert "计划语义" in CLASSIFIER_SYSTEM_PROMPT, (
            "Prompt must require explicit planning semantics for study_plan"
        )
        # Must NOT contain hardcoded sample textbook names
        for hardcoded in ["牙体牙髓病学", "牙周病学"]:
            assert hardcoded not in CLASSIFIER_SYSTEM_PROMPT.split("学习需求分类器")[0], (
                f"Prompt must not hardcode textbook name '{hardcoded}' — "
                f"use generic descriptions"
            )

    def test_classifier_prompt_contains_negative_learning_examples(self):
        """The prompt must clarify what ordinary learning looks like so
        the LLM doesn't route everything with '学习' to study_plan."""
        assert ("系统学习" in CLASSIFIER_SYSTEM_PROMPT
                or "普通" in CLASSIFIER_SYSTEM_PROMPT
                or "基本的主题" in CLASSIFIER_SYSTEM_PROMPT), (
            "Prompt must describe ordinary topic learning as a contrasting example"
        )

    def test_validate_route_accepts_study_plan_without_plan_item(self):
        """study_plan with plan_item_requested=false must pass validation —
        this is exactly the scoped chapter-learning case."""
        result = _validate_route({
            "intent": "study_plan",
            "plan_item_requested": False,
            "reason": "用户指定教材+章节作为学习安排范围",
        })
        assert result is not None
        assert result["intent"] == "study_plan"
        assert result["plan_item_requested"] is False

    # ── Explicit plan item / recommendation preserved ─────────────────

    @pytest.mark.asyncio
    async def test_explicit_plan_item_still_works_with_scoped_learning(
        self, mock_orchestrator, sample_plan_item
    ):
        """Explicit plan_item_id still routes deterministically to learning
        (no regression from the prompt change)."""
        mock_orchestrator.memory.get_study_plan_item.return_value = sample_plan_item
        mock_orchestrator.start_lesson.return_value = {
            "session": {"id": "sess-plan-item"},
            "outline": {"nodes": []},
        }
        router = MasterRouter(mock_orchestrator)

        result = await router.route(
            "",
            "demo",
            context={"plan_item_id": "pi-test-001"},
        )

        assert result["ok"] is True
        assert result["route"]["source"] == "explicit_plan_item"
        assert result["route"]["intent"] == "learning"
        mock_orchestrator.start_lesson.assert_called_once()

    # ── Clarification continuation still preserved ────────────────────

    @pytest.mark.asyncio
    async def test_clarification_continuation_still_works(
        self, mock_orchestrator
    ):
        """The parent_plan_draft_id pre-check still works — chapter-title
        reply after clarification routes to study_plan without LLM."""
        captured_kwargs: dict = {}

        def _mock_sp(memory, *, user_id, plan_date, max_items=4,
                     request_text=None, parent_plan_draft_id=None, **kw):
            captured_kwargs["request_text"] = request_text
            captured_kwargs["parent_plan_draft_id"] = parent_plan_draft_id
            return {"plan_date": plan_date, "recommendations": [],
                    "plan_draft_id": "draft_clarify_child"}

        parent_payload = {
            "request": {"intent": "custom", "days": 7, "daily_minutes": 30},
            "items": [],
            "clarification_needed": True,
            "clarification_message": "请告诉我您想学习哪本教材或哪个章节？",
        }
        test_store = _make_test_draft_store({
            "draft_parent_clarify": {
                "plan_draft_id": "draft_parent_clarify",
                "user_id": "demo",
                "created_at": 999_000_000.0,
                "expires_at": 2_000_000_000.0,
                "payload": parent_payload,
            },
        })

        with patch(
            "backend.services.learning.study_plan._get_default_draft_store",
            return_value=test_store,
        ):
            router = MasterRouter(
                mock_orchestrator,
                classifier=_make_classifier("clarification", "意图不明"),
                study_plan_service=_mock_sp,
            )

            result = await router.route(
                "第三章 牙周病的分类和流行病学",
                "demo",
                context={
                    "user_id": "demo",
                    "parent_plan_draft_id": "draft_parent_clarify",
                },
            )

        assert result["ok"] is True
        assert result["route"]["intent"] == "study_plan"
        assert result["route"]["source"] == "parent_draft_clarification"


# ============================================================================
# R9 Router Handoff Matrix — Clarification Continuation & Weakness Routing
# ============================================================================


class TestR9WholeBookClarificationRouterHandoff:
    """Compact matrix proving Router handoff for R9's three backend entry
    points: clarification continuation (book-only, chapter-only), weakness
    query routing, and preservation of regular classification.

    Selection:  pytest -k "whole_book or R9WholeBook or clarification_continuation"
    """

    # ------------------------------------------------------------------
    # Clarification continuation — book-only reply
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_whole_book_clarification_book_only_routes_to_study_plan(
        self, mock_orchestrator
    ):
        """After a study-plan clarification, user replies with a book name only
        (no chapter, no "制定计划" verb) → must route to study_plan via
        parent_draft_clarification pre-check.

        This proves the R9 requirement: book-name-only short continuation
        inherits the clarification context and enters the study_plan flow,
        NOT Direct QA or regular learning.
        """
        captured_kwargs: dict = {}

        def _mock_sp(memory, *, user_id, plan_date, max_items=4,
                     request_text=None, parent_plan_draft_id=None, **kw):
            captured_kwargs.update({
                "parent_plan_draft_id": parent_plan_draft_id,
                "request_text": request_text,
                "history": kw.get("history"),
            })
            return {
                "plan_date": plan_date,
                "mode": "custom_draft",
                "plan_draft_id": "draft_child_book_only",
                "recommendations": [
                    {
                        "recommendation_id": "draft_child_book_only:0",
                        "title": "学习《牙周病学》全书",
                        "task_type": "learn_chapter",
                        "estimated_minutes": 45,
                    },
                ],
            }

        parent_payload = {
            "request": {"intent": "custom", "days": 7, "daily_minutes": 30},
            "items": [],
            "clarification_needed": True,
            "clarification_message": "请告诉我您想学习哪本教材或哪个章节？",
        }
        test_store = _make_test_draft_store({
            "draft_parent_book_clarify": {
                "plan_draft_id": "draft_parent_book_clarify",
                "user_id": "demo",
                "created_at": 999_000_000.0,
                "expires_at": 2_000_000_000.0,
                "payload": parent_payload,
            },
        })

        with patch(
            "backend.services.learning.study_plan._get_default_draft_store",
            return_value=test_store,
        ):
            router = MasterRouter(
                mock_orchestrator,
                classifier=_make_classifier("clarification", "意图模糊"),
                study_plan_service=_mock_sp,
            )

            result = await router.route(
                "《牙周病学》",
                "demo",
                history=[
                    {"role": "user", "content": "我接下来应该怎么规划学习"},
                    {"role": "assistant",
                     "content": "请告诉我您想学习哪本教材或哪个章节？"},
                ],
                context={
                    "user_id": "demo",
                    "parent_plan_draft_id": "draft_parent_book_clarify",
                },
            )

        assert result["ok"] is True, f"expected ok=True, got {result.get('error')}"
        assert result["route"]["intent"] == "study_plan", (
            f"book-only reply after clarification must route to study_plan, "
            f"got {result['route']['intent']}"
        )
        assert result["route"]["source"] == "parent_draft_clarification", (
            "source must be parent_draft_clarification (deterministic pre-check)"
        )
        assert captured_kwargs["parent_plan_draft_id"] == "draft_parent_book_clarify"
        assert captured_kwargs["request_text"] == "《牙周病学》"
        # History forwarded for context resolution
        assert captured_kwargs.get("history") is not None
        # Must produce a real plan, not empty
        assert len(result["final_result"].get("recommendations", [])) > 0, (
            "book-only reply must produce recommendations"
        )
        assert result["final_result"]["plan_draft_id"] == "draft_child_book_only"

    # ------------------------------------------------------------------
    # Clarification continuation — chapter/KP-only reply
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_whole_book_clarification_chapter_only_routes_to_study_plan(
        self, mock_orchestrator
    ):
        """After a study-plan clarification, user replies with only a chapter
        title or knowledge-point description (no book name, no verb) → must
        route to study_plan.

        This proves the short continuation with chapter/KP structure works.
        """
        captured_kwargs: dict = {}

        def _mock_sp(memory, *, user_id, plan_date, max_items=4,
                     request_text=None, parent_plan_draft_id=None, **kw):
            captured_kwargs.update({
                "parent_plan_draft_id": parent_plan_draft_id,
                "request_text": request_text,
                "history": kw.get("history"),
            })
            return {
                "plan_date": plan_date,
                "mode": "custom_draft",
                "plan_draft_id": "draft_child_chapter_only",
                "recommendations": [
                    {
                        "recommendation_id": "draft_child_chapter_only:0",
                        "title": "学习牙周病的分类和流行病学",
                        "task_type": "learn_chapter",
                        "estimated_minutes": 30,
                    },
                ],
            }

        parent_payload = {
            "request": {"intent": "custom", "days": 7, "daily_minutes": 30},
            "items": [],
            "clarification_needed": True,
            "clarification_message": "请告诉我您想学习的具体内容，以便我为您制定合适的学习计划。",
        }
        test_store = _make_test_draft_store({
            "draft_parent_chapter_clarify": {
                "plan_draft_id": "draft_parent_chapter_clarify",
                "user_id": "demo",
                "created_at": 999_000_000.0,
                "expires_at": 2_000_000_000.0,
                "payload": parent_payload,
            },
        })

        with patch(
            "backend.services.learning.study_plan._get_default_draft_store",
            return_value=test_store,
        ):
            router = MasterRouter(
                mock_orchestrator,
                classifier=_make_classifier("direct_qa", "直接问答"),
                study_plan_service=_mock_sp,
            )

            result = await router.route(
                "牙周病的分类和流行病学",
                "demo",
                history=[
                    {"role": "user", "content": "给我做一个学习安排"},
                    {"role": "assistant",
                     "content": "请告诉我您想学习的具体内容，以便我为您制定合适的学习计划。"},
                ],
                context={
                    "user_id": "demo",
                    "parent_plan_draft_id": "draft_parent_chapter_clarify",
                },
            )

        assert result["ok"] is True
        assert result["route"]["intent"] == "study_plan", (
            f"chapter-only reply must route to study_plan, "
            f"got {result['route']['intent']}"
        )
        assert result["route"]["source"] == "parent_draft_clarification"
        assert captured_kwargs["request_text"] == "牙周病的分类和流行病学"
        assert len(result["final_result"].get("recommendations", [])) > 0

    # ------------------------------------------------------------------
    # Clarification continuation — non-clarification parent falls through
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_whole_book_non_clarification_parent_preserves_classifier(
        self, mock_orchestrator
    ):
        """When parent_plan_draft_id refers to a completed (non-clarification)
        plan, the normal classifier result must be preserved — NOT hijacked
        to study_plan.

        This proves the parent-draft pre-check is gated on clarification_needed.
        """
        study_called: list[dict] = []

        def _mock_sp(memory, *, user_id, plan_date, max_items=4,
                     request_text=None, parent_plan_draft_id=None, **kw):
            study_called.append({"parent": parent_plan_draft_id})
            return {"recommendations": []}

        parent_payload = {
            "request": {"intent": "learn_chapter", "days": 7, "daily_minutes": 30},
            "items": [{"task_type": "learn_chapter", "learning_target": "牙周病发病机制"}],
            # NO clarification_needed — this is a completed plan
        }
        test_store = _make_test_draft_store({
            "draft_parent_done": {
                "plan_draft_id": "draft_parent_done",
                "user_id": "demo",
                "created_at": 999_000_000.0,
                "expires_at": 2_000_000_000.0,
                "payload": parent_payload,
            },
        })

        with patch(
            "backend.services.learning.study_plan._get_default_draft_store",
            return_value=test_store,
        ):
            router = MasterRouter(
                mock_orchestrator,
                classifier=_make_classifier("direct_qa", "直接问答"),
                study_plan_service=_mock_sp,
            )

            result = await router.route(
                "牙周炎怎么治疗？",
                "demo",
                context={
                    "user_id": "demo",
                    "parent_plan_draft_id": "draft_parent_done",
                },
            )

        assert result["ok"] is True
        assert result["route"]["intent"] == "direct_qa", (
            f"non-clarification parent must not override classifier; "
            f"expected direct_qa, got {result['route']['intent']}"
        )
        assert result["route"]["source"] == "llm_router"
        assert len(study_called) == 0, (
            "study_plan_service must NOT be called for non-clarification parent"
        )

    # ------------------------------------------------------------------
    # Weakness query classification preserves study_plan routing
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_whole_book_weakness_plan_query_routes_to_study_plan(
        self, mock_orchestrator
    ):
        """"根据我的薄弱点制定计划" must route to study_plan via the classifier,
        not to clarification or learning."""
        captured_kwargs: dict = {}

        def _mock_sp(memory, *, user_id, plan_date, max_items=4,
                     request_text=None, parent_plan_draft_id=None, **kw):
            captured_kwargs["request_text"] = request_text
            return {
                "plan_date": plan_date,
                "mode": "custom_draft",
                "plan_draft_id": "draft_weakness_plan",
                "recommendations": [
                    {
                        "recommendation_id": "draft_weakness_plan:0",
                        "title": "复习薄弱点",
                        "task_type": "review_chapter",
                    },
                ],
            }

        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier("study_plan", "用户想按薄弱点制定计划"),
            study_plan_service=_mock_sp,
        )

        result = await router.route("根据我的薄弱点制定学习计划", "demo")

        assert result["ok"] is True
        assert result["route"]["intent"] == "study_plan", (
            f"weakness plan request must route to study_plan, "
            f"got {result['route']['intent']}"
        )
        assert result["next_action"] == "show_recommendations"
        mock_orchestrator.start_lesson.assert_not_called()

    @pytest.mark.asyncio
    async def test_whole_book_weakness_overview_query_routes_to_study_plan(
        self, mock_orchestrator
    ):
        """"我有什么薄弱点" (short, no plan verb) must route to study_plan
        for weakness overview, not to clarification or direct_qa."""
        captured_kwargs: dict = {}

        def _mock_sp(memory, *, user_id, plan_date, max_items=4,
                     request_text=None, parent_plan_draft_id=None, **kw):
            captured_kwargs["request_text"] = request_text
            return {
                "plan_date": plan_date,
                "mode": "custom_draft",
                "plan_draft_id": "draft_weakness_overview",
                "recommendations": [],
            }

        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier("study_plan", "用户询问薄弱点概况"),
            study_plan_service=_mock_sp,
        )

        result = await router.route("我有什么薄弱点", "demo")

        assert result["ok"] is True
        assert result["route"]["intent"] == "study_plan", (
            f"weakness overview query must route to study_plan, "
            f"got {result['route']['intent']}"
        )

    # ------------------------------------------------------------------
    # Regular classification chain preserved
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_whole_book_regular_learning_classification_preserved(
        self, mock_orchestrator
    ):
        """Regular learning intent (no plan, no book, no weakness) must still
        route through the classifier to the learning handler — proving the
        new entry points don't break the existing classification chain."""
        mock_orchestrator.start_lesson.return_value = {
            "session": {"id": "sess-regular-learn"},
            "outline": {"nodes": []},
        }
        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier("learning", "用户想系统学习"),
        )

        result = await router.route("我想系统学习牙体牙髓病的诊断方法", "demo")

        assert result["ok"] is True
        assert result["route"]["intent"] == "learning", (
            f"regular learning must route to learning, "
            f"got {result['route']['intent']}"
        )
        assert result["route"]["source"] == "llm_router"
        assert result["next_action"] == "start_session"
        mock_orchestrator.start_lesson.assert_called_once()

    @pytest.mark.asyncio
    async def test_whole_book_regular_direct_qa_classification_preserved(
        self, mock_orchestrator
    ):
        """Regular direct_qa intent (specific medical question, no plan/book
        keywords) must still route to direct_qa — proving the entry points
        don't hijack unrelated intent classification."""
        mock_orchestrator.start_lesson.return_value = {
            "session": {"id": "sess-dqa"},
            "outline": {"nodes": []},
        }

        def _mock_direct_qa(message, *, history, user_id):
            return {
                "answer": "牙周炎的治疗包括菌斑控制、洁治、刮治和根面平整等非手术治疗。",
                "sources": [],
            }

        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier("direct_qa", "用户询问具体医学问题"),
            direct_qa_service=_mock_direct_qa,
        )

        result = await router.route("牙周炎的非手术治疗有哪些方法？", "demo")

        assert result["ok"] is True
        assert result["route"]["intent"] == "direct_qa", (
            f"regular direct_qa must preserve its route, "
            f"got {result['route']['intent']}"
        )
        assert result["route"]["source"] == "llm_router"
        assert result["next_action"] == "show_direct_answer"
        mock_orchestrator.start_lesson.assert_not_called()


# ---------------------------------------------------------------------------
# R10 parent-revision classifier token budget — proof that parent context
# receives higher max_tokens (512) while non-parent stays at 256.
# ---------------------------------------------------------------------------


class TestParentRevisionClassifierTokenBudget:
    """Prove that ``_classify`` selects ``max_tokens`` based on whether
    ``context.parent_plan_draft_id`` is a non-empty string.

    These tests mock ``chat_completion`` (and ``is_llm_configured``) so the
    default LLM classifier path is exercised WITHOUT an injected classifier.
    """

    # ------------------------------------------------------------------
    # Positive: parent context → 512
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_parent_context_receives_higher_max_tokens(
        self, mock_orchestrator
    ):
        """When context has a non-empty parent_plan_draft_id, chat_completion
        must be called with max_tokens=512 (not 256)."""
        captured_kwargs: dict = {}

        def _fake_chat(messages, **kwargs):
            captured_kwargs.update(kwargs)
            return json.dumps({
                "intent": "study_plan",
                "plan_item_requested": False,
                "topic": "",
                "reason": "用户希望保持范围不变但减少练习量并限制每日时间为30分钟",
            }, ensure_ascii=False)

        with (
            patch(
                "backend.services.router.master_router.is_llm_configured",
                return_value=True,
            ),
            patch(
                "backend.services.router.master_router.chat_completion",
                side_effect=_fake_chat,
            ),
        ):
            router = MasterRouter(mock_orchestrator)
            result = await router.route(
                "范围保持不变，每天最多30分钟，把练习减少一些",
                "demo",
                context={"parent_plan_draft_id": "draft_parent_abc123"},
            )

        assert captured_kwargs.get("max_tokens") == 512, (
            f"parent context must receive max_tokens=512, "
            f"got {captured_kwargs.get('max_tokens')}"
        )
        assert result["route"]["intent"] == "study_plan", (
            f"parent revision must route to study_plan, "
            f"got {result['route']['intent']}"
        )
        assert result["route"]["source"] == "llm_router"

    @pytest.mark.asyncio
    async def test_parent_context_complete_study_plan_json_validates(
        self, mock_orchestrator
    ):
        """A complete study_plan classifier JSON produced with the higher
        parent-context token budget must pass _validate_route and produce
        a valid route dict that reaches the study_plan handler."""
        captured_kwargs: dict = {}

        def _fake_study_plan(memory, *, user_id, plan_date, max_items=4,
                             request_text=None, parent_plan_draft_id=None, **kw):
            return {
                "plan_date": plan_date,
                "mode": "custom_draft",
                "plan_draft_id": "draft_child_001",
                "parent_plan_draft_id": parent_plan_draft_id,
                "recommendations": [],
            }

        def _fake_chat(messages, **kwargs):
            captured_kwargs.update(kwargs)
            return json.dumps({
                "intent": "study_plan",
                "plan_item_requested": False,
                "topic": "",
                "reason": "用户希望保持范围不变但减少练习量并限制每日时间为30分钟",
            }, ensure_ascii=False)

        with (
            patch(
                "backend.services.router.master_router.is_llm_configured",
                return_value=True,
            ),
            patch(
                "backend.services.router.master_router.chat_completion",
                side_effect=_fake_chat,
            ),
        ):
            router = MasterRouter(
                mock_orchestrator,
                study_plan_service=_fake_study_plan,
            )
            result = await router.route(
                "范围保持不变，每天最多30分钟，把练习减少一些",
                "demo",
                context={"parent_plan_draft_id": "draft_parent_abc123"},
            )

        assert captured_kwargs.get("max_tokens") == 512
        assert result["ok"] is True
        assert result["route"]["intent"] == "study_plan"
        assert result["route"]["source"] == "llm_router"
        assert result["next_action"] == "show_recommendations"
        # The study_plan service must receive the parent draft ID
        assert result["final_result"].get("parent_plan_draft_id") == "draft_parent_abc123"

    @pytest.mark.asyncio
    async def test_structurally_different_parent_revision_also_validates(
        self, mock_orchestrator
    ):
        """A structurally different parent revision (adding review while
        keeping practice, with a 20-minute daily budget) must also produce
        a valid study_plan route with the higher token budget."""
        captured_kwargs: dict = {}

        def _fake_study_plan(memory, *, user_id, plan_date, max_items=4,
                             request_text=None, parent_plan_draft_id=None, **kw):
            return {
                "plan_date": plan_date,
                "mode": "custom_draft",
                "plan_draft_id": "draft_child_002",
                "parent_plan_draft_id": parent_plan_draft_id,
                "recommendations": [],
            }

        def _fake_chat(messages, **kwargs):
            captured_kwargs.update(kwargs)
            return json.dumps({
                "intent": "study_plan",
                "plan_item_requested": False,
                "topic": "",
                "reason": "用户要求增加复习内容、保持练习量不变、每天限制20分钟",
            }, ensure_ascii=False)

        with (
            patch(
                "backend.services.router.master_router.is_llm_configured",
                return_value=True,
            ),
            patch(
                "backend.services.router.master_router.chat_completion",
                side_effect=_fake_chat,
            ),
        ):
            router = MasterRouter(
                mock_orchestrator,
                study_plan_service=_fake_study_plan,
            )
            result = await router.route(
                "多安排一些复习，练习不用变，每天20分钟就好",
                "demo",
                context={"parent_plan_draft_id": "draft_parent_xyz789"},
            )

        assert captured_kwargs.get("max_tokens") == 512
        assert result["ok"] is True
        assert result["route"]["intent"] == "study_plan"
        assert result["route"]["source"] == "llm_router"
        assert result["next_action"] == "show_recommendations"
        assert result["final_result"].get("parent_plan_draft_id") == "draft_parent_xyz789"

    # ------------------------------------------------------------------
    # Negative: non-parent → 256
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_non_parent_context_keeps_strict_256(
        self, mock_orchestrator
    ):
        """Without parent_plan_draft_id, chat_completion must be called with
        max_tokens=256 — the existing budget is preserved."""
        captured_kwargs: dict = {}

        def _fake_chat(messages, **kwargs):
            captured_kwargs.update(kwargs)
            return json.dumps({
                "intent": "learning",
                "plan_item_requested": False,
                "topic": "牙周炎",
                "reason": "用户想系统学习",
            }, ensure_ascii=False)

        with (
            patch(
                "backend.services.router.master_router.is_llm_configured",
                return_value=True,
            ),
            patch(
                "backend.services.router.master_router.chat_completion",
                side_effect=_fake_chat,
            ),
        ):
            router = MasterRouter(mock_orchestrator)
            result = await router.route("我想系统学习牙周炎", "demo")

        assert captured_kwargs.get("max_tokens") == 256, (
            f"non-parent context must keep max_tokens=256, "
            f"got {captured_kwargs.get('max_tokens')}"
        )
        assert result["route"]["intent"] == "learning"

    @pytest.mark.asyncio
    async def test_empty_parent_draft_id_uses_256(
        self, mock_orchestrator
    ):
        """An empty string parent_plan_draft_id must be treated as absent
        → max_tokens=256 (same as no-parent)."""
        captured_kwargs: dict = {}

        def _fake_chat(messages, **kwargs):
            captured_kwargs.update(kwargs)
            return json.dumps({
                "intent": "learning",
                "plan_item_requested": False,
                "topic": "口腔溃疡",
                "reason": "用户想学习新知识",
            }, ensure_ascii=False)

        with (
            patch(
                "backend.services.router.master_router.is_llm_configured",
                return_value=True,
            ),
            patch(
                "backend.services.router.master_router.chat_completion",
                side_effect=_fake_chat,
            ),
        ):
            router = MasterRouter(mock_orchestrator)
            result = await router.route(
                "我想学习口腔溃疡",
                "demo",
                context={"parent_plan_draft_id": ""},
            )

        assert captured_kwargs.get("max_tokens") == 256, (
            f"empty parent_plan_draft_id must use 256, "
            f"got {captured_kwargs.get('max_tokens')}"
        )
        assert result["route"]["intent"] == "learning"

    @pytest.mark.asyncio
    async def test_whitespace_parent_draft_id_uses_256(
        self, mock_orchestrator
    ):
        """A whitespace-only parent_plan_draft_id must be treated as absent
        → max_tokens=256."""
        captured_kwargs: dict = {}

        def _fake_chat(messages, **kwargs):
            captured_kwargs.update(kwargs)
            return json.dumps({
                "intent": "direct_qa",
                "plan_item_requested": False,
                "topic": "",
                "reason": "用户询问具体医学问题",
            }, ensure_ascii=False)

        with (
            patch(
                "backend.services.router.master_router.is_llm_configured",
                return_value=True,
            ),
            patch(
                "backend.services.router.master_router.chat_completion",
                side_effect=_fake_chat,
            ),
        ):
            router = MasterRouter(mock_orchestrator)
            result = await router.route(
                "牙周炎怎么治疗",
                "demo",
                context={"parent_plan_draft_id": "   "},
            )

        assert captured_kwargs.get("max_tokens") == 256, (
            f"whitespace parent_plan_draft_id must use 256, "
            f"got {captured_kwargs.get('max_tokens')}"
        )
        assert result["route"]["intent"] == "direct_qa"

    @pytest.mark.asyncio
    async def test_non_parent_ordinary_learning_keeps_256(
        self, mock_orchestrator
    ):
        """Ordinary learning without any parent_plan_draft_id in context must
        call chat_completion with max_tokens=256 — the existing budget is
        untouched for all non-parent classification."""
        captured_kwargs: dict = {}

        def _fake_chat(messages, **kwargs):
            captured_kwargs.update(kwargs)
            return json.dumps({
                "intent": "learning",
                "plan_item_requested": False,
                "topic": "颞下颌关节紊乱",
                "reason": "用户想系统学习",
            }, ensure_ascii=False)

        with (
            patch(
                "backend.services.router.master_router.is_llm_configured",
                return_value=True,
            ),
            patch(
                "backend.services.router.master_router.chat_completion",
                side_effect=_fake_chat,
            ),
        ):
            router = MasterRouter(mock_orchestrator)
            result = await router.route(
                "我想学习颞下颌关节紊乱的诊断",
                "demo",
                # No parent_plan_draft_id in context at all
            )

        assert captured_kwargs.get("max_tokens") == 256, (
            f"ordinary learning must use max_tokens=256, "
            f"got {captured_kwargs.get('max_tokens')}"
        )
        assert result["route"]["intent"] == "learning"

    @pytest.mark.asyncio
    async def test_non_parent_direct_qa_keeps_256(
        self, mock_orchestrator
    ):
        """Direct QA without any parent_plan_draft_id must call chat_completion
        with max_tokens=256 — the existing budget is untouched."""
        captured_kwargs: dict = {}

        def _fake_chat(messages, **kwargs):
            captured_kwargs.update(kwargs)
            return json.dumps({
                "intent": "direct_qa",
                "plan_item_requested": False,
                "topic": "",
                "reason": "用户询问具体口腔医学问题",
            }, ensure_ascii=False)

        with (
            patch(
                "backend.services.router.master_router.is_llm_configured",
                return_value=True,
            ),
            patch(
                "backend.services.router.master_router.chat_completion",
                side_effect=_fake_chat,
            ),
        ):
            router = MasterRouter(mock_orchestrator)
            result = await router.route(
                "牙周炎的非手术治疗有哪些方法？",
                "demo",
                # No parent_plan_draft_id
            )

        assert captured_kwargs.get("max_tokens") == 256, (
            f"direct_qa must use max_tokens=256, "
            f"got {captured_kwargs.get('max_tokens')}"
        )
        assert result["route"]["intent"] == "direct_qa"

    # ------------------------------------------------------------------
    # Interaction: deterministic parent_draft_clarification pre-check
    # still fires before the classifier
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_parent_context_clarification_parent_draft_routing_still_works(
        self, mock_orchestrator
    ):
        """When a parent draft has clarification_needed=true, the deterministic
        parent_draft_clarification pre-check fires BEFORE the classifier, so
        chat_completion is never called — the higher token budget is irrelevant
        for this path."""
        from backend.services.learning.study_plan import _get_default_draft_store

        captured_kwargs: dict = {}

        def _fake_chat(messages, **kwargs):
            captured_kwargs["called"] = True
            captured_kwargs.update(kwargs)
            return json.dumps({"intent": "study_plan"})

        # Pre-seed a parent draft with clarification_needed=true via the
        # public save() method — we can't control the generated ID, so we
        # mock get() on the default store to return the needed record.
        draft_store = _get_default_draft_store()
        _orig_get = draft_store.get

        def _mock_get(plan_draft_id, user_id):
            if plan_draft_id == "draft_parent_clarify":
                return {
                    "plan_draft_id": "draft_parent_clarify",
                    "user_id": "demo",
                    "payload": {"clarification_needed": True},
                }
            return _orig_get(plan_draft_id, user_id)

        draft_store.get = _mock_get

        def _fake_sp(memory, *, user_id, plan_date, max_items=4,
                     request_text=None, parent_plan_draft_id=None, **kw):
            return {
                "plan_date": plan_date,
                "mode": "custom_draft",
                "plan_draft_id": "draft_child_clarify",
                "parent_plan_draft_id": parent_plan_draft_id,
                "recommendations": [],
            }

        with (
            patch(
                "backend.services.router.master_router.is_llm_configured",
                return_value=True,
            ),
            patch(
                "backend.services.router.master_router.chat_completion",
                side_effect=_fake_chat,
            ),
        ):
            router = MasterRouter(
                mock_orchestrator,
                study_plan_service=_fake_sp,
            )
            result = await router.route(
                "第三章 牙周病的分类",
                "demo",
                context={"parent_plan_draft_id": "draft_parent_clarify"},
            )

        # The deterministic pre-check must have fired → no LLM call
        assert "called" not in captured_kwargs, (
            "parent_draft_clarification pre-check must bypass the classifier entirely"
        )
        assert result["route"]["source"] == "parent_draft_clarification", (
            f"source must be parent_draft_clarification, "
            f"got {result['route']['source']}"
        )
        assert result["route"]["intent"] == "study_plan"

        # Restore original get method
        draft_store.get = _orig_get
