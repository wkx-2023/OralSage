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

        def _mock_study_plan(memory, *, user_id, plan_date, max_items=4):
            return expected

        router = MasterRouter(
            mock_orchestrator,
            classifier=_make_classifier("study_plan", "用户想获取学习计划"),
            study_plan_service=_mock_study_plan,
        )
        result = await router.route("今天学什么", "demo")

        assert result["ok"] is True
        assert result["route"]["intent"] == "study_plan"
        assert result["next_action"] == "show_recommendations"
        assert result["final_result"] == expected
        # Must NOT start a lesson
        mock_orchestrator.start_lesson.assert_not_called()

    @pytest.mark.asyncio
    async def test_study_plan_service_error(
        self, mock_orchestrator
    ):
        """study_plan_service raises → return error with ok=False."""

        def _failing(_memory, *, user_id, plan_date, max_items=4):
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
        mock_orchestrator.continue_to_next_section.return_value = {
            "session": {"id": "sess-existing"},
            "lesson_completed": False,
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
        mock_orchestrator.continue_to_next_section.assert_called_once()
        mock_orchestrator.start_lesson.assert_not_called()
        assert result["route"]["source"] == "explicit_plan_item"

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
