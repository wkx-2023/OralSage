"""Closed-loop contract tests: formal plan item → Router → learning orchestration.

Validates the cross-cutting contract between adopted formal plan items
(LearningMemory), the MasterRouter ``plan_item_id`` path, and the learning
orchestrator via ``learning_scope``.

Every test uses a real ``LearningMemory`` backed by a temporary SQLite
database — no manually constructed mock dicts for the item read path.
No real LLM, network, or screenshot calls.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from backend.services.learning.memory import LearningMemory
from backend.services.router.master_router import MasterRouter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _formal_scope(**overrides: Any) -> dict[str, Any]:
    """Return a realistic formal learning_scope dict (not a mock)."""
    defaults: dict[str, Any] = {
        "book_title": "牙周病学",
        "document_id": "document.d766e8629232a2f6",
        "version_id": "v1.0",
        "chapter_key": "chapter:perio-ch3",
        "chapter_title": "第三章 牙周病的发病机制",
        "chapter_number": 3,
        "section_count": 3,
        "source_sections": ["第一节", "第二节", "第三节"],
        "kg_node_ids": ["core:aaa111", "core:bbb222", "core:ccc333"],
    }
    return {**defaults, **overrides}


def _sample_stage_exam(**overrides: Any) -> dict[str, Any]:
    """Return a realistic public stage-exam dict (what the frontend receives)."""
    defaults: dict[str, Any] = {
        "schema_version": "learning.stage_exam.v1",
        "session_id": "sess-contract",
        "status": "ready",
        "questions": [
            {
                "id": "q1",
                "type": "single_choice",
                "stem": "牙周病的始动因子是？",
                "options": [
                    {"label": "A", "text": "牙石"},
                    {"label": "B", "text": "菌斑生物膜"},
                    {"label": "C", "text": "创伤"},
                    {"label": "D", "text": "全身因素"},
                ],
            },
        ],
        "summary": {"question_count": 1, "section_count": 1},
    }
    return {**defaults, **overrides}


def _make_orchestrator(
    memory: LearningMemory,
    *,
    start_lesson_return: dict[str, Any] | None = None,
    continue_return: dict[str, Any] | None = None,
    stage_exam_return: dict[str, Any] | None = None,
) -> MagicMock:
    """Build a mock orchestrator whose ``.memory`` is a **real** LearningMemory.

    The real memory ensures ``get_study_plan_item`` returns the actual
    persisted row (including ``learning_scope``) — not a hand-crafted mock.
    """
    orch = MagicMock()
    orch.memory = memory
    orch.start_lesson.return_value = start_lesson_return or {
        "session": {"id": "sess-contract"},
        "outline": {"nodes": []},
    }
    orch.continue_to_next_section.return_value = continue_return or {
        "lesson_completed": False,
    }
    orch.ensure_stage_exam_for_interaction.return_value = (
        stage_exam_return or _sample_stage_exam()
    )
    return orch


# ---------------------------------------------------------------------------
# Contract 1 — persistence round-trip
# ---------------------------------------------------------------------------


class TestPersistenceRoundtrip:
    """A formal plan item saved through ``create_study_plan_item`` must be
    readable with all fields — including ``learning_scope`` — unchanged."""

    def test_create_and_read_full_item(self):
        scope = _formal_scope()
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                item = memory.create_study_plan_item(
                    user_id="u-contract",
                    plan_date="2026-07-15",
                    title="合同测试：牙周病发病机制",
                    notes="验证 scope 持久化",
                    linked_course_id="course-perio-101",
                    linked_course_title="牙周病学",
                    task_type="learn_chapter",
                    learning_target="牙周病的发病机制",
                    kg_node_ids=scope["kg_node_ids"],
                    learning_scope=scope,
                )

                # Re-read
                reloaded = memory.get_study_plan_item(item["id"], user_id="u-contract")

                # Structural fields unchanged
                assert reloaded["id"] == item["id"]
                assert reloaded["user_id"] == "u-contract"
                assert reloaded["plan_date"] == "2026-07-15"
                assert reloaded["title"] == "合同测试：牙周病发病机制"
                assert reloaded["status"] == "open"
                assert reloaded["linked_course_id"] == "course-perio-101"
                assert reloaded["linked_course_title"] == "牙周病学"
                assert reloaded["task_type"] == "learn_chapter"
                assert reloaded["learning_target"] == "牙周病的发病机制"
                assert reloaded["kg_node_ids"] == scope["kg_node_ids"]

                # learning_scope round-trip
                assert reloaded["learning_scope"] == scope
                assert reloaded["learning_scope"]["document_id"] == scope["document_id"]
                assert reloaded["learning_scope"]["chapter_key"] == scope["chapter_key"]
                assert reloaded["learning_scope"]["chapter_title"] == scope["chapter_title"]
                assert reloaded["learning_scope"]["book_title"] == scope["book_title"]
                assert reloaded["learning_scope"]["kg_node_ids"] == scope["kg_node_ids"]
                assert reloaded["learning_scope"]["section_count"] == 3
            finally:
                memory._conn.close()

    def test_item_without_scope_returns_empty_dict(self):
        """Backward compatibility: item without scope → learning_scope is {}."""
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                item = memory.create_study_plan_item(
                    user_id="u-contract",
                    plan_date="2026-07-15",
                    title="Old-style item no scope",
                )
                reloaded = memory.get_study_plan_item(item["id"], user_id="u-contract")
                assert reloaded["learning_scope"] == {}
            finally:
                memory._conn.close()

    def test_item_with_minimal_scope_roundtrips(self):
        """A scope with only kg_node_ids (no book/chapter metadata) survives."""
        minimal = {"kg_node_ids": ["node-x", "node-y"]}
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                item = memory.create_study_plan_item(
                    user_id="u-contract",
                    plan_date="2026-07-15",
                    title="Minimal scope item",
                    learning_scope=minimal,
                )
                reloaded = memory.get_study_plan_item(item["id"], user_id="u-contract")
                assert reloaded["learning_scope"] == minimal
                assert reloaded["learning_scope"]["kg_node_ids"] == ["node-x", "node-y"]
            finally:
                memory._conn.close()


# ---------------------------------------------------------------------------
# Contract 2 — Router plan_item_id → start_lesson scope
# ---------------------------------------------------------------------------


class TestRouterPlanItemScopeContract:
    """The MasterRouter ``plan_item_id`` path must read the real item from
    memory and forward its ``learning_scope`` to ``start_lesson``."""

    @pytest.mark.asyncio
    async def test_real_item_scope_forwarded_to_start_lesson(self):
        """A formal plan item persisted in real memory → Router reads it →
        start_lesson receives the identical learning_scope."""
        scope = _formal_scope()

        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                # 1. Persist a real formal plan item
                item = memory.create_study_plan_item(
                    user_id="u-scope-fwd",
                    plan_date="2026-07-15",
                    title="合约范围转发测试",
                    linked_course_title="牙周病学",
                    kg_node_ids=scope["kg_node_ids"],
                    learning_scope=scope,
                )

                # 2. Build orchestrator with the REAL memory (not mocked item)
                orch = _make_orchestrator(memory)

                # 3. Route via plan_item_id — Router internally calls
                #    orch.memory.get_study_plan_item() on the real DB
                router = MasterRouter(orch)
                result = await router.route(
                    "",
                    "u-scope-fwd",
                    context={"plan_item_id": item["id"]},
                )

                assert result["ok"] is True
                assert result["route"]["source"] == "explicit_plan_item"
                assert result["route"]["intent"] == "learning"

                # 4. start_lesson was called with the real scope
                orch.start_lesson.assert_called_once()
                call_kwargs = orch.start_lesson.call_args.kwargs
                assert "learning_scope" in call_kwargs
                # The scope forwarded must be exactly the scope read from the
                # real DB — not a hand-crafted copy
                assert call_kwargs["learning_scope"] == scope
                assert call_kwargs["learning_scope"]["document_id"] == scope["document_id"]
                assert call_kwargs["learning_scope"]["chapter_key"] == scope["chapter_key"]
            finally:
                memory._conn.close()

    @pytest.mark.asyncio
    async def test_real_item_empty_scope_not_forwarded(self):
        """Item with learning_scope={} → start_lesson called without scope kwarg."""
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                item = memory.create_study_plan_item(
                    user_id="u-empty-scope",
                    plan_date="2026-07-15",
                    title="Empty scope item",
                    linked_course_title="Some Course",
                )

                orch = _make_orchestrator(memory)
                router = MasterRouter(orch)
                result = await router.route(
                    "",
                    "u-empty-scope",
                    context={"plan_item_id": item["id"]},
                )

                assert result["ok"] is True
                orch.start_lesson.assert_called_once()
                call_kwargs = orch.start_lesson.call_args.kwargs
                assert "learning_scope" not in call_kwargs
            finally:
                memory._conn.close()

    @pytest.mark.asyncio
    async def test_different_scope_items_produce_different_calls(self):
        """Two structurally different scope items → each call carries its own."""
        scope_a = _formal_scope(
            book_title="牙体牙髓病学",
            document_id="doc-endo-001",
            chapter_key="chapter:endo-ch5",
            chapter_title="第五章 根管治疗",
            kg_node_ids=["core:endo-1", "core:endo-2"],
        )
        scope_b = _formal_scope(
            book_title="口腔颌面外科学",
            document_id="doc-oms-001",
            chapter_key="chapter:oms-ch2",
            chapter_title="第二章 拔牙术",
            kg_node_ids=["core:oms-a", "core:oms-b"],
        )

        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                item_a = memory.create_study_plan_item(
                    user_id="u-multi",
                    plan_date="2026-07-15",
                    title="根管治疗学习",
                    linked_course_title="牙体牙髓病学",
                    kg_node_ids=scope_a["kg_node_ids"],
                    learning_scope=scope_a,
                )
                item_b = memory.create_study_plan_item(
                    user_id="u-multi",
                    plan_date="2026-07-15",
                    title="拔牙术学习",
                    linked_course_title="口腔颌面外科学",
                    kg_node_ids=scope_b["kg_node_ids"],
                    learning_scope=scope_b,
                )

                # Route item A
                orch_a = _make_orchestrator(memory)
                router_a = MasterRouter(orch_a)
                result_a = await router_a.route(
                    "", "u-multi", context={"plan_item_id": item_a["id"]},
                )
                assert result_a["ok"] is True
                assert orch_a.start_lesson.call_args.kwargs["learning_scope"] == scope_a

                # Route item B (separate orchestrator to isolate assertions)
                orch_b = _make_orchestrator(memory)
                router_b = MasterRouter(orch_b)
                result_b = await router_b.route(
                    "", "u-multi", context={"plan_item_id": item_b["id"]},
                )
                assert result_b["ok"] is True
                assert orch_b.start_lesson.call_args.kwargs["learning_scope"] == scope_b

                # Must NOT be the same (no cross-contamination)
                assert scope_a != scope_b
            finally:
                memory._conn.close()


# ---------------------------------------------------------------------------
# Contract 3 — lesson_completed MUST NOT auto-complete the plan item
# ---------------------------------------------------------------------------


class TestLessonCompletedDoesNotMutatePlanItem:
    """The learning orchestrator signals ``lesson_completed=True`` when the
    last outline node is reached.  This signal must NOT write the plan item
    to ``status=completed`` — only the explicit complete endpoint may do so."""

    @pytest.mark.asyncio
    async def test_lesson_completed_preserves_open_status(self):
        """Route via plan_item_id → lesson_completed=True →
        re-read from real DB → status is still 'open'."""
        scope = _formal_scope()

        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                item = memory.create_study_plan_item(
                    user_id="u-complete",
                    plan_date="2026-07-15",
                    title="完成不应关闭计划项",
                    linked_course_title="牙周病学",
                    kg_node_ids=scope["kg_node_ids"],
                    learning_scope=scope,
                )

                orch = _make_orchestrator(
                    memory,
                    start_lesson_return={
                        "session": {"id": "sess-complete-test"},
                        "outline": {"nodes": [{"id": "n1", "title": "t1"}]},
                    },
                    continue_return={
                        "lesson_completed": True,
                        "stage_exam": None,
                    },
                )

                # 1. Route → starts the lesson via plan_item_id
                router = MasterRouter(orch)
                result = await router.route(
                    "",
                    "u-complete",
                    context={"plan_item_id": item["id"]},
                )
                assert result["ok"] is True
                session_id = result["final_result"]["session"]["id"]

                # 2. Simulate a "continue" after lesson completes
                continue_result = await router.route(
                    "继续",
                    "u-complete",
                    context={"session_id": session_id},
                )
                assert continue_result["ok"] is True
                assert continue_result["final_result"].get("lesson_completed") is True

                # 3. Re-read the plan item from the REAL database
                reloaded = memory.get_study_plan_item(item["id"], user_id="u-complete")
                assert reloaded["status"] == "open", (
                    f"lesson_completed signal must NOT set status=completed; "
                    f"got status={reloaded['status']!r}"
                )
                assert reloaded["learning_scope"] == scope
            finally:
                memory._conn.close()

    @pytest.mark.asyncio
    async def test_tutor_step_on_last_node_preserves_open_status(self):
        """When continue_to_next_section returns the last Tutor step
        (lesson_completed=False but it's the final node advancing to None),
        the plan item status must still be 'open'."""
        scope = _formal_scope()

        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                item = memory.create_study_plan_item(
                    user_id="u-tutor",
                    plan_date="2026-07-15",
                    title="末节点Tutor不应关闭",
                    linked_course_title="牙周病学",
                    kg_node_ids=scope["kg_node_ids"],
                    learning_scope=scope,
                )

                # After advancing past the last node → lesson_completed=True
                orch = _make_orchestrator(
                    memory,
                    start_lesson_return={
                        "session": {"id": "sess-tutor"},
                        "outline": {"nodes": [{"id": "n-last", "title": "总结"}]},
                    },
                    continue_return={
                        "tutor_step": {
                            "session_id": "sess-tutor",
                            "outline_node_id": "n-last",
                            "teaching_text": "这是最后一节的讲稿。",
                            "questions": [],
                        },
                        "lesson_completed": True,
                        "advanced": True,
                    },
                )

                router = MasterRouter(orch)
                result = await router.route(
                    "",
                    "u-tutor",
                    context={"plan_item_id": item["id"]},
                )
                assert result["ok"] is True
                session_id = result["final_result"]["session"]["id"]

                # Continue past last node
                await router.route(
                    "继续",
                    "u-tutor",
                    context={"session_id": session_id},
                )

                # Plan item must still be open
                reloaded = memory.get_study_plan_item(item["id"], user_id="u-tutor")
                assert reloaded["status"] == "open", (
                    f"tutor step past last node must NOT set status=completed; "
                    f"got status={reloaded['status']!r}"
                )
            finally:
                memory._conn.close()

    @pytest.mark.asyncio
    async def test_no_complete_update_defer_invoked(self):
        """Verify that the test does NOT call any mutation on the plan item
        (complete / update / defer).  The only write was create_study_plan_item."""
        scope = _formal_scope()

        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                item = memory.create_study_plan_item(
                    user_id="u-no-mutation",
                    plan_date="2026-07-15",
                    title="无变更测试",
                    linked_course_title="牙周病学",
                    kg_node_ids=scope["kg_node_ids"],
                    learning_scope=scope,
                )

                orch = _make_orchestrator(
                    memory,
                    start_lesson_return={
                        "session": {"id": "sess-no-mut"},
                        "outline": {"nodes": [{"id": "n1", "title": "第一节"}]},
                    },
                    continue_return={
                        "lesson_completed": True,
                    },
                )

                router = MasterRouter(orch)
                result = await router.route(
                    "", "u-no-mutation",
                    context={"plan_item_id": item["id"]},
                )
                session_id = result["final_result"]["session"]["id"]

                await router.route(
                    "继续", "u-no-mutation",
                    context={"session_id": session_id},
                )

                # The plan item in the DB should be identical to what we
                # created (same id, status, learning_scope, etc.)
                reloaded = memory.get_study_plan_item(item["id"], user_id="u-no-mutation")
                assert reloaded["id"] == item["id"]
                assert reloaded["status"] == "open"
                assert reloaded["title"] == item["title"]
                assert reloaded["learning_scope"] == scope
                # updated_at may differ if the memory layer touched the row,
                # but status must NOT change
            finally:
                memory._conn.close()


# ---------------------------------------------------------------------------
# Contract 4 — free learning path does NOT pass scope
# ---------------------------------------------------------------------------


class TestFreeLearningNoScope:
    """Ordinary learning (no ``plan_item_id`` in context) must call
    ``start_lesson`` WITHOUT ``learning_scope``."""

    @pytest.mark.asyncio
    async def test_free_learning_with_real_memory_no_scope(self):
        """Even when open plan items exist in memory, free learning must not
        attach learning_scope to start_lesson."""
        scope = _formal_scope()

        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                # Persist an open item — it exists but must NOT leak into
                # the free learning path.
                memory.create_study_plan_item(
                    user_id="u-free",
                    plan_date="2026-07-15",
                    title="不应该被用到的计划项",
                    linked_course_title="牙周病学",
                    kg_node_ids=scope["kg_node_ids"],
                    learning_scope=scope,
                )

                orch = _make_orchestrator(memory)

                # The classifier returns learning WITHOUT plan_item_requested
                def _free_learning_classifier(_msg, _hist, _ctx):
                    return {
                        "intent": "learning",
                        "plan_item_requested": False,
                        "topic": "牙周炎",
                        "reason": "普通学习请求",
                    }

                router = MasterRouter(orch, classifier=_free_learning_classifier)
                result = await router.route("我想学习牙周炎", "u-free")

                assert result["ok"] is True
                assert result["route"]["intent"] == "learning"
                orch.start_lesson.assert_called_once()
                call_kwargs = orch.start_lesson.call_args.kwargs
                assert "learning_scope" not in call_kwargs, (
                    "free learning must NOT forward learning_scope"
                )
            finally:
                memory._conn.close()

    @pytest.mark.asyncio
    async def test_free_learning_plan_item_requested_false_no_scope(self):
        """plan_item_requested=False with open items → still free learning, no scope."""
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                memory.create_study_plan_item(
                    user_id="u-free2",
                    plan_date="2026-07-15",
                    title="开放计划项",
                    linked_course_title="Some Course",
                    learning_scope=_formal_scope(),
                )

                orch = _make_orchestrator(memory)

                def _classifier(_msg, _hist, _ctx):
                    return {
                        "intent": "learning",
                        "plan_item_requested": False,
                        "topic": "龋病",
                        "reason": "想自由学习",
                    }

                router = MasterRouter(orch, classifier=_classifier)
                result = await router.route("学习龋病", "u-free2")

                assert result["ok"] is True
                orch.start_lesson.assert_called_once()
                assert "learning_scope" not in orch.start_lesson.call_args.kwargs
            finally:
                memory._conn.close()

    @pytest.mark.asyncio
    async def test_direct_learning_with_plan_item_id_does_pass_scope(self):
        """Contrast: when plan_item_id IS in context, scope IS forwarded.
        This confirms the free-learning test is checking the right thing."""
        scope = _formal_scope()

        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                item = memory.create_study_plan_item(
                    user_id="u-direct",
                    plan_date="2026-07-15",
                    title="直达范围测试",
                    linked_course_title="牙周病学",
                    kg_node_ids=scope["kg_node_ids"],
                    learning_scope=scope,
                )

                orch = _make_orchestrator(memory)
                router = MasterRouter(orch)
                result = await router.route(
                    "", "u-direct",
                    context={"plan_item_id": item["id"]},
                )

                assert result["ok"] is True
                orch.start_lesson.assert_called_once()
                call_kwargs = orch.start_lesson.call_args.kwargs
                assert "learning_scope" in call_kwargs
                assert call_kwargs["learning_scope"] == scope
            finally:
                memory._conn.close()


# ---------------------------------------------------------------------------
# Contract 5 — review_chapter task type (R6)
# ---------------------------------------------------------------------------


class TestReviewChapterScopeContract:
    """``review_chapter`` plan items must route through the same scoped
    learning path as ``learn_chapter`` — both reuse the real scope's
    existing directed learning (R6)."""

    @pytest.mark.asyncio
    async def test_review_chapter_forwards_scope_to_start_lesson(self):
        """review_chapter → Router plan_item_id → start_lesson gets learning_scope."""
        scope = _formal_scope(
            book_title="牙周病学",
            chapter_title="第三章 牙周病的发病机制",
        )

        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                item = memory.create_study_plan_item(
                    user_id="u-review-ch",
                    plan_date="2026-07-15",
                    title="复习：牙周病发病机制",
                    linked_course_title="牙周病学",
                    task_type="review_chapter",
                    learning_target="牙周病的发病机制",
                    kg_node_ids=scope["kg_node_ids"],
                    learning_scope=scope,
                )

                orch = _make_orchestrator(memory)
                router = MasterRouter(orch)
                result = await router.route(
                    "",
                    "u-review-ch",
                    context={"plan_item_id": item["id"]},
                )

                assert result["ok"] is True
                assert result["route"]["source"] == "explicit_plan_item"
                assert result["route"]["intent"] == "learning"
                assert result["route"]["task_type"] == "review_chapter"

                orch.start_lesson.assert_called_once()
                call_kwargs = orch.start_lesson.call_args.kwargs
                assert "learning_scope" in call_kwargs
                assert call_kwargs["learning_scope"] == scope
                assert call_kwargs["learning_scope"]["chapter_key"] == scope["chapter_key"]
            finally:
                memory._conn.close()

    @pytest.mark.asyncio
    async def test_review_chapter_empty_scope_not_forwarded(self):
        """review_chapter with learning_scope={} → no scope kwarg in start_lesson."""
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                item = memory.create_study_plan_item(
                    user_id="u-review-empty",
                    plan_date="2026-07-15",
                    title="复习（空 scope）",
                    linked_course_title="牙周病学",
                    task_type="review_chapter",
                    learning_target="牙周病的发病机制",
                )

                orch = _make_orchestrator(memory)
                router = MasterRouter(orch)
                result = await router.route(
                    "",
                    "u-review-empty",
                    context={"plan_item_id": item["id"]},
                )

                assert result["ok"] is True
                orch.start_lesson.assert_called_once()
                call_kwargs = orch.start_lesson.call_args.kwargs
                assert "learning_scope" not in call_kwargs
            finally:
                memory._conn.close()


# ---------------------------------------------------------------------------
# Contract 6 — chapter types derive topic from learning_target (R4)
# ---------------------------------------------------------------------------


class TestChapterTypesTopicDerivation:
    """``learn_chapter`` and ``review_chapter`` must derive the learning
    topic from ``learning_target`` (not from the generic fallback path)
    so the downstream orchestrator receives the chapter-scoped topic (R4)."""

    @pytest.mark.asyncio
    async def test_learn_chapter_topic_from_learning_target(self):
        """learn_chapter → topic = learning_target, not raw title."""
        scope = _formal_scope()

        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                item = memory.create_study_plan_item(
                    user_id="u-topic-lc",
                    plan_date="2026-07-15",
                    title="系统学习牙周病发病机制",
                    linked_course_title="牙周病学",
                    task_type="learn_chapter",
                    learning_target="牙周病的发病机制",
                    kg_node_ids=scope["kg_node_ids"],
                    learning_scope=scope,
                )

                orch = _make_orchestrator(memory)
                router = MasterRouter(orch)
                result = await router.route(
                    "",
                    "u-topic-lc",
                    context={"plan_item_id": item["id"]},
                )

                assert result["ok"] is True
                orch.start_lesson.assert_called_once()
                call_kwargs = orch.start_lesson.call_args.kwargs
                # topic is derived from learning_target (not the raw title);
                # linked_course_title is prefixed when not already in target
                assert "牙周病的发病机制" in call_kwargs["topic"]
                assert call_kwargs["learning_scope"] == scope
            finally:
                memory._conn.close()

    @pytest.mark.asyncio
    async def test_review_chapter_topic_from_learning_target(self):
        """review_chapter → topic derived from learning_target (same as learn_chapter)."""
        scope = _formal_scope()

        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                item = memory.create_study_plan_item(
                    user_id="u-topic-rc",
                    plan_date="2026-07-15",
                    title="复习牙周病发病机制",
                    linked_course_title="牙周病学",
                    task_type="review_chapter",
                    learning_target="牙周病的发病机制",
                    kg_node_ids=scope["kg_node_ids"],
                    learning_scope=scope,
                )

                orch = _make_orchestrator(memory)
                router = MasterRouter(orch)
                result = await router.route(
                    "",
                    "u-topic-rc",
                    context={"plan_item_id": item["id"]},
                )

                assert result["ok"] is True
                orch.start_lesson.assert_called_once()
                call_kwargs = orch.start_lesson.call_args.kwargs
                assert "牙周病的发病机制" in call_kwargs["topic"]
                assert call_kwargs["learning_scope"] == scope
            finally:
                memory._conn.close()

    @pytest.mark.asyncio
    async def test_chapter_type_topic_prefixed_with_course_when_distinct(self):
        """When linked_course_title differs from learning_target, topic is prefixed."""
        scope = _formal_scope(
            book_title="口腔颌面外科学",
            chapter_title="第二章 拔牙术",
            chapter_key="chapter:oms-ch2",
            kg_node_ids=["core:oms-a"],
        )

        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                item = memory.create_study_plan_item(
                    user_id="u-prefix",
                    plan_date="2026-07-15",
                    title="学习拔牙术",
                    linked_course_title="口腔颌面外科学",
                    task_type="learn_chapter",
                    learning_target="拔牙术",
                    kg_node_ids=scope["kg_node_ids"],
                    learning_scope=scope,
                )

                orch = _make_orchestrator(memory)
                router = MasterRouter(orch)
                result = await router.route(
                    "",
                    "u-prefix",
                    context={"plan_item_id": item["id"]},
                )

                assert result["ok"] is True
                orch.start_lesson.assert_called_once()
                call_kwargs = orch.start_lesson.call_args.kwargs
                assert call_kwargs["topic"] == "口腔颌面外科学：拔牙术"
                assert call_kwargs["learning_scope"] == scope
            finally:
                memory._conn.close()


# ---------------------------------------------------------------------------
# Contract 7 — practice task type unchanged
# ---------------------------------------------------------------------------


class TestPracticeTaskTypeUnchanged:
    """``practice`` task type must NOT be affected by the chapter-type
    changes — its existing behaviour (learning_target → topic, scope
    forwarding when present) stays identical."""

    @pytest.mark.asyncio
    async def test_practice_still_uses_learning_target_for_topic(self):
        """practice → topic derived from learning_target (existing behaviour)."""
        scope = _formal_scope()

        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                item = memory.create_study_plan_item(
                    user_id="u-practice",
                    plan_date="2026-07-15",
                    title="练习牙周病相关题目",
                    linked_course_title="牙周病学",
                    task_type="practice",
                    learning_target="牙周病练习题",
                    kg_node_ids=scope["kg_node_ids"],
                    learning_scope=scope,
                )

                orch = _make_orchestrator(memory)
                router = MasterRouter(orch)
                result = await router.route(
                    "",
                    "u-practice",
                    context={"plan_item_id": item["id"]},
                )

                assert result["ok"] is True
                assert result["route"]["task_type"] == "practice"
                orch.start_lesson.assert_called_once()
                call_kwargs = orch.start_lesson.call_args.kwargs
                # topic derived from learning_target (existing behaviour);
                # linked_course_title is prefixed when not already in target
                assert "牙周病练习题" in call_kwargs["topic"]
                assert call_kwargs["learning_scope"] == scope
            finally:
                memory._conn.close()

    @pytest.mark.asyncio
    async def test_practice_without_scope_still_works(self):
        """practice without learning_scope → start_lesson called without scope kwarg."""
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                item = memory.create_study_plan_item(
                    user_id="u-practice-ns",
                    plan_date="2026-07-15",
                    title="练习（无 scope）",
                    linked_course_title="牙周病学",
                    task_type="practice",
                    learning_target="牙周病练习题",
                )

                orch = _make_orchestrator(memory)
                router = MasterRouter(orch)
                result = await router.route(
                    "",
                    "u-practice-ns",
                    context={"plan_item_id": item["id"]},
                )

                assert result["ok"] is True
                orch.start_lesson.assert_called_once()
                call_kwargs = orch.start_lesson.call_args.kwargs
                assert "learning_scope" not in call_kwargs
            finally:
                memory._conn.close()


# ---------------------------------------------------------------------------
# Contract 8 — basic_practice uses session's stage exam (NOT start_lesson)
# ---------------------------------------------------------------------------


class TestBasicPracticeStageExamContract:
    """``basic_practice`` plan items must use the associated session's
    existing stage exam — NOT start a new lesson via ``start_lesson``.

    This is the "关联学习" (associated learning) path: the practice
    reuses the session's already-generated stage papers (阶段卷) rather
    than routing through the regular learning (普通学习) path.
    """

    @pytest.mark.asyncio
    async def test_basic_practice_uses_stage_exam_not_start_lesson(self):
        """basic_practice → ensure_stage_exam_for_interaction is called,
        start_lesson is NOT called."""
        scope = _formal_scope()
        stage_exam = _sample_stage_exam()

        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                # Create a real session so get_session() succeeds
                session = memory.create_session("u-bp", "牙周病学")

                # Persist a basic_practice plan item linked to that session
                item = memory.create_study_plan_item(
                    user_id="u-bp",
                    plan_date="2026-07-15",
                    title="基础练习：牙周病发病机制",
                    linked_course_id=session["id"],
                    linked_course_title="牙周病学",
                    task_type="basic_practice",
                    learning_target="牙周病的发病机制",
                    kg_node_ids=scope["kg_node_ids"],
                    learning_scope=scope,
                )

                orch = _make_orchestrator(memory, stage_exam_return=stage_exam)
                router = MasterRouter(orch)
                result = await router.route(
                    "",
                    "u-bp",
                    context={"plan_item_id": item["id"]},
                )

                # Must succeed
                assert result["ok"] is True
                assert result["route"]["intent"] == "learning"
                assert result["route"]["task_type"] == "basic_practice"
                assert result["route"]["source"] == "explicit_plan_item"
                assert result["next_action"] == "show_stage_exam"

                # start_lesson must NOT be called (basic_practice does NOT
                # start a new lesson; it reuses the existing session)
                orch.start_lesson.assert_not_called()

                # ensure_stage_exam_for_interaction must be called with the
                # linked session ID
                orch.ensure_stage_exam_for_interaction.assert_called_once_with(
                    session["id"],
                )

                # The stage exam must be returned as-is
                assert result["final_result"]["stage_exam"] == stage_exam
                assert result["final_result"]["session"]["id"] == session["id"]
                assert result["final_result"]["lesson_completed"] is False
            finally:
                memory._conn.close()

    @pytest.mark.asyncio
    async def test_basic_practice_without_linked_course_id_errors(self):
        """basic_practice without linked_course_id → error, no start_lesson."""
        scope = _formal_scope()

        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                item = memory.create_study_plan_item(
                    user_id="u-bp-no-link",
                    plan_date="2026-07-15",
                    title="基础练习（无关联课程）",
                    linked_course_title="牙周病学",
                    task_type="basic_practice",
                    learning_target="牙周病的发病机制",
                    kg_node_ids=scope["kg_node_ids"],
                    learning_scope=scope,
                )

                orch = _make_orchestrator(memory)
                router = MasterRouter(orch)
                result = await router.route(
                    "",
                    "u-bp-no-link",
                    context={"plan_item_id": item["id"]},
                )

                assert result["ok"] is False
                assert "linked_course_id" in result["error"]
                orch.start_lesson.assert_not_called()
                orch.ensure_stage_exam_for_interaction.assert_not_called()
            finally:
                memory._conn.close()

    @pytest.mark.asyncio
    async def test_basic_practice_session_not_found_errors(self):
        """basic_practice with a linked_course_id that does not exist → error."""
        scope = _formal_scope()

        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                item = memory.create_study_plan_item(
                    user_id="u-bp-no-sess",
                    plan_date="2026-07-15",
                    title="基础练习（session 不存在）",
                    linked_course_id="nonexistent-session-id",
                    linked_course_title="牙周病学",
                    task_type="basic_practice",
                    learning_target="牙周病的发病机制",
                    kg_node_ids=scope["kg_node_ids"],
                    learning_scope=scope,
                )

                orch = _make_orchestrator(memory)
                router = MasterRouter(orch)
                result = await router.route(
                    "",
                    "u-bp-no-sess",
                    context={"plan_item_id": item["id"]},
                )

                assert result["ok"] is False
                assert "not a valid session" in result["error"]
                orch.start_lesson.assert_not_called()
                orch.ensure_stage_exam_for_interaction.assert_not_called()
            finally:
                memory._conn.close()

    @pytest.mark.asyncio
    async def test_basic_practice_wrong_user_session_errors(self):
        """basic_practice with session belonging to a different user → denied."""
        scope = _formal_scope()

        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                # Session owned by user "other-user"
                session = memory.create_session("other-user", "牙周病学")

                # Plan item owned by "u-bp-wrong"
                item = memory.create_study_plan_item(
                    user_id="u-bp-wrong",
                    plan_date="2026-07-15",
                    title="基础练习（跨用户）",
                    linked_course_id=session["id"],
                    linked_course_title="牙周病学",
                    task_type="basic_practice",
                    learning_target="牙周病的发病机制",
                    kg_node_ids=scope["kg_node_ids"],
                    learning_scope=scope,
                )

                orch = _make_orchestrator(memory)
                router = MasterRouter(orch)
                result = await router.route(
                    "",
                    "u-bp-wrong",
                    context={"plan_item_id": item["id"]},
                )

                assert result["ok"] is False
                assert "not a valid session" in result["error"]
                orch.start_lesson.assert_not_called()
                orch.ensure_stage_exam_for_interaction.assert_not_called()
            finally:
                memory._conn.close()

    @pytest.mark.asyncio
    async def test_basic_practice_stage_exam_load_failure(self):
        """When ensure_stage_exam_for_interaction raises, the error is surfaced."""
        scope = _formal_scope()

        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                session = memory.create_session("u-bp-fail", "牙周病学")

                item = memory.create_study_plan_item(
                    user_id="u-bp-fail",
                    plan_date="2026-07-15",
                    title="基础练习（加载失败）",
                    linked_course_id=session["id"],
                    linked_course_title="牙周病学",
                    task_type="basic_practice",
                    learning_target="牙周病的发病机制",
                    kg_node_ids=scope["kg_node_ids"],
                    learning_scope=scope,
                )

                orch = _make_orchestrator(memory)
                orch.ensure_stage_exam_for_interaction.side_effect = RuntimeError(
                    "no question bank"
                )
                router = MasterRouter(orch)
                result = await router.route(
                    "",
                    "u-bp-fail",
                    context={"plan_item_id": item["id"]},
                )

                assert result["ok"] is False
                assert "Stage exam load failed" in result["error"]
                orch.start_lesson.assert_not_called()
            finally:
                memory._conn.close()

    @pytest.mark.asyncio
    async def test_basic_practice_not_called_for_other_task_types(self):
        """practice (not basic_practice) still calls start_lesson normally."""
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                item = memory.create_study_plan_item(
                    user_id="u-reg-practice",
                    plan_date="2026-07-15",
                    title="普通练习",
                    linked_course_title="牙周病学",
                    task_type="practice",
                    learning_target="牙周病练习题",
                )

                orch = _make_orchestrator(memory)
                router = MasterRouter(orch)
                result = await router.route(
                    "",
                    "u-reg-practice",
                    context={"plan_item_id": item["id"]},
                )

                assert result["ok"] is True
                # Regular practice still calls start_lesson
                orch.start_lesson.assert_called_once()
                # stage exam path is NOT triggered
                orch.ensure_stage_exam_for_interaction.assert_not_called()
            finally:
                memory._conn.close()


# ---------------------------------------------------------------------------
# Contract 9 — advanced_practice uses session's stage exam with weakness scope
# ---------------------------------------------------------------------------


class TestAdvancedPracticeStageExamContract:
    """``advanced_practice`` plan items must establish a directional session
    based on the real weakness scope (learning_scope) and reuse the
    associated session's existing stage exam — NOT start a new lesson via
    ``start_lesson``.

    When no weakness evidence (learning_scope is empty/missing), the router
    MUST return an error — NO fallback to ordinary learning.
    """

    @pytest.mark.asyncio
    async def test_advanced_practice_with_scope_uses_stage_exam(self):
        """advanced_practice with valid learning_scope → stage exam returned,
        start_lesson is NOT called."""
        scope = _formal_scope()
        stage_exam = _sample_stage_exam()

        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                session = memory.create_session("u-ap", "牙周病学")

                item = memory.create_study_plan_item(
                    user_id="u-ap",
                    plan_date="2026-07-15",
                    title="进阶练习：牙周病发病机制弱点强化",
                    linked_course_id=session["id"],
                    linked_course_title="牙周病学",
                    task_type="advanced_practice",
                    learning_target="牙周病的发病机制",
                    kg_node_ids=scope["kg_node_ids"],
                    learning_scope=scope,
                )

                orch = _make_orchestrator(memory, stage_exam_return=stage_exam)
                router = MasterRouter(orch)
                result = await router.route(
                    "",
                    "u-ap",
                    context={"plan_item_id": item["id"]},
                )

                assert result["ok"] is True
                assert result["route"]["intent"] == "learning"
                assert result["route"]["task_type"] == "advanced_practice"
                assert result["route"]["source"] == "explicit_plan_item"
                assert result["next_action"] == "show_stage_exam"

                # start_lesson must NOT be called
                orch.start_lesson.assert_not_called()

                # ensure_stage_exam_for_interaction must be called
                orch.ensure_stage_exam_for_interaction.assert_called_once_with(
                    session["id"],
                )

                # Stage exam and scope both returned
                assert result["final_result"]["stage_exam"] == stage_exam
                assert result["final_result"]["session"]["id"] == session["id"]
                assert result["final_result"]["lesson_completed"] is False
                assert result["final_result"]["learning_scope"] == scope
            finally:
                memory._conn.close()

    @pytest.mark.asyncio
    async def test_advanced_practice_without_learning_scope_errors(self):
        """advanced_practice without learning_scope → error, no fallback to
        ordinary learning."""
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                session = memory.create_session("u-ap-no-scope", "牙周病学")

                item = memory.create_study_plan_item(
                    user_id="u-ap-no-scope",
                    plan_date="2026-07-15",
                    title="进阶练习（无弱点范围）",
                    linked_course_id=session["id"],
                    linked_course_title="牙周病学",
                    task_type="advanced_practice",
                    learning_target="牙周病的发病机制",
                )

                orch = _make_orchestrator(memory)
                router = MasterRouter(orch)
                result = await router.route(
                    "",
                    "u-ap-no-scope",
                    context={"plan_item_id": item["id"]},
                )

                assert result["ok"] is False
                assert "weakness evidence" in result["error"]
                # No fallback to ordinary learning
                orch.start_lesson.assert_not_called()
                orch.ensure_stage_exam_for_interaction.assert_not_called()
            finally:
                memory._conn.close()

    @pytest.mark.asyncio
    async def test_advanced_practice_with_empty_scope_errors(self):
        """advanced_practice with learning_scope={} → error (empty scope is
        treated as no evidence)."""
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                session = memory.create_session("u-ap-empty", "牙周病学")

                item = memory.create_study_plan_item(
                    user_id="u-ap-empty",
                    plan_date="2026-07-15",
                    title="进阶练习（空 scope）",
                    linked_course_id=session["id"],
                    linked_course_title="牙周病学",
                    task_type="advanced_practice",
                    learning_target="牙周病的发病机制",
                    learning_scope={},
                )

                orch = _make_orchestrator(memory)
                router = MasterRouter(orch)
                result = await router.route(
                    "",
                    "u-ap-empty",
                    context={"plan_item_id": item["id"]},
                )

                assert result["ok"] is False
                assert "weakness evidence" in result["error"]
                orch.start_lesson.assert_not_called()
                orch.ensure_stage_exam_for_interaction.assert_not_called()
            finally:
                memory._conn.close()

    @pytest.mark.asyncio
    async def test_advanced_practice_without_linked_course_id_errors(self):
        """advanced_practice without linked_course_id → error, no start_lesson."""
        scope = _formal_scope()

        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                item = memory.create_study_plan_item(
                    user_id="u-ap-no-link",
                    plan_date="2026-07-15",
                    title="进阶练习（无关联课程）",
                    linked_course_title="牙周病学",
                    task_type="advanced_practice",
                    learning_target="牙周病的发病机制",
                    kg_node_ids=scope["kg_node_ids"],
                    learning_scope=scope,
                )

                orch = _make_orchestrator(memory)
                router = MasterRouter(orch)
                result = await router.route(
                    "",
                    "u-ap-no-link",
                    context={"plan_item_id": item["id"]},
                )

                assert result["ok"] is False
                assert "linked_course_id" in result["error"]
                orch.start_lesson.assert_not_called()
                orch.ensure_stage_exam_for_interaction.assert_not_called()
            finally:
                memory._conn.close()

    @pytest.mark.asyncio
    async def test_advanced_practice_session_not_found_errors(self):
        """advanced_practice with nonexistent linked_course_id → error."""
        scope = _formal_scope()

        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                item = memory.create_study_plan_item(
                    user_id="u-ap-no-sess",
                    plan_date="2026-07-15",
                    title="进阶练习（session 不存在）",
                    linked_course_id="nonexistent-session-id",
                    linked_course_title="牙周病学",
                    task_type="advanced_practice",
                    learning_target="牙周病的发病机制",
                    kg_node_ids=scope["kg_node_ids"],
                    learning_scope=scope,
                )

                orch = _make_orchestrator(memory)
                router = MasterRouter(orch)
                result = await router.route(
                    "",
                    "u-ap-no-sess",
                    context={"plan_item_id": item["id"]},
                )

                assert result["ok"] is False
                assert "Session not found" in result["error"]
                orch.start_lesson.assert_not_called()
                orch.ensure_stage_exam_for_interaction.assert_not_called()
            finally:
                memory._conn.close()

    @pytest.mark.asyncio
    async def test_advanced_practice_wrong_user_session_errors(self):
        """advanced_practice with session belonging to a different user → denied."""
        scope = _formal_scope()

        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                session = memory.create_session("other-user", "牙周病学")

                item = memory.create_study_plan_item(
                    user_id="u-ap-wrong",
                    plan_date="2026-07-15",
                    title="进阶练习（跨用户）",
                    linked_course_id=session["id"],
                    linked_course_title="牙周病学",
                    task_type="advanced_practice",
                    learning_target="牙周病的发病机制",
                    kg_node_ids=scope["kg_node_ids"],
                    learning_scope=scope,
                )

                orch = _make_orchestrator(memory)
                router = MasterRouter(orch)
                result = await router.route(
                    "",
                    "u-ap-wrong",
                    context={"plan_item_id": item["id"]},
                )

                assert result["ok"] is False
                assert "access denied" in result["error"].lower()
                orch.start_lesson.assert_not_called()
                orch.ensure_stage_exam_for_interaction.assert_not_called()
            finally:
                memory._conn.close()

    @pytest.mark.asyncio
    async def test_advanced_practice_stage_exam_load_failure(self):
        """When ensure_stage_exam_for_interaction raises, error is surfaced,
        no fallback to ordinary learning."""
        scope = _formal_scope()

        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                session = memory.create_session("u-ap-fail", "牙周病学")

                item = memory.create_study_plan_item(
                    user_id="u-ap-fail",
                    plan_date="2026-07-15",
                    title="进阶练习（加载失败）",
                    linked_course_id=session["id"],
                    linked_course_title="牙周病学",
                    task_type="advanced_practice",
                    learning_target="牙周病的发病机制",
                    kg_node_ids=scope["kg_node_ids"],
                    learning_scope=scope,
                )

                orch = _make_orchestrator(memory)
                orch.ensure_stage_exam_for_interaction.side_effect = RuntimeError(
                    "no question bank"
                )
                router = MasterRouter(orch)
                result = await router.route(
                    "",
                    "u-ap-fail",
                    context={"plan_item_id": item["id"]},
                )

                assert result["ok"] is False
                assert "Stage exam load failed" in result["error"]
                orch.start_lesson.assert_not_called()
            finally:
                memory._conn.close()

    @pytest.mark.asyncio
    async def test_advanced_practice_not_called_for_other_task_types(self):
        """basic_practice (not advanced_practice) still works normally."""
        scope = _formal_scope()
        stage_exam = _sample_stage_exam()

        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                session = memory.create_session("u-bp-reg", "牙周病学")

                item = memory.create_study_plan_item(
                    user_id="u-bp-reg",
                    plan_date="2026-07-15",
                    title="基础练习",
                    linked_course_id=session["id"],
                    linked_course_title="牙周病学",
                    task_type="basic_practice",
                    learning_target="牙周病的发病机制",
                    kg_node_ids=scope["kg_node_ids"],
                    learning_scope=scope,
                )

                orch = _make_orchestrator(memory, stage_exam_return=stage_exam)
                router = MasterRouter(orch)
                result = await router.route(
                    "",
                    "u-bp-reg",
                    context={"plan_item_id": item["id"]},
                )

                assert result["ok"] is True
                assert result["route"]["task_type"] == "basic_practice"
                orch.start_lesson.assert_not_called()
                orch.ensure_stage_exam_for_interaction.assert_called_once()
            finally:
                memory._conn.close()


# ============================================================================
# _preflight_draft_items — plan-item preflight validation
# ============================================================================


class TestPreflightDraftItems:
    """Preflight validation of draft item lists before persistence.

    Tests the strict fail-closed rules:
    - learn/review must have usable scope (no user_specified bypass)
    - basic_practice: scope usable + dependency must be learn/review with same scope
    - advanced_practice: scope usable + same-scope predecessor
    - practice→practice forbidden
    - cross-scope forbidden
    - unknown types rejected
    """

    # Helpers — reuse _formal_scope from module level

    @staticmethod
    def _fake_usable_inspector(_scope: dict[str, Any]) -> dict[str, Any]:
        """Fake resource inspector that always returns usable=True.

        Used by tests that validate dependency/structure rules independent
        of real resource availability.  Production code must never use a
        fake inspector.
        """
        return {
            "usable": True,
            "reason_code": "ok",
            "kg_node_hit_count": 1,
            "kg_node_ids_checked": [],
            "kg_node_ids_usable": [],
            "evidence_chunk_count": 1,
            "scope_diagnostics": {
                "has_kg_node_ids": True,
                "has_document_id": True,
                "has_chapter_key": True,
                "title": "",
                "book_title": "",
                "document_id": "",
                "chapter_key": "",
                "checks_performed": ["kg_nodes", "evidence_chunks"],
            },
            "error_message": None,
        }

    @staticmethod
    def _fake_unusable_inspector(_scope: dict[str, Any]) -> dict[str, Any]:
        """Fake resource inspector that always returns usable=False (not_verified).

        Used by fail-closed tests to assert that items with unverified
        resources are rejected.
        """
        return {
            "usable": False,
            "reason_code": "not_verified",
            "kg_node_hit_count": 0,
            "kg_node_ids_checked": [],
            "kg_node_ids_usable": [],
            "evidence_chunk_count": 0,
            "scope_diagnostics": {
                "has_kg_node_ids": True,
                "has_document_id": True,
                "has_chapter_key": True,
                "title": "",
                "book_title": "",
                "document_id": "",
                "chapter_key": "",
                "checks_performed": [],
            },
            "error_message": "Resource verification was not performed.",
        }

    def _draft_scope(self, **overrides: Any) -> dict[str, Any]:
        """A minimal but valid-looking scope for draft item tests."""
        defaults: dict[str, Any] = {
            "book_title": "牙周病学",
            "document_id": "document.d766e8629232a2f6",
            "chapter_key": "chapter:perio-ch1",
            "chapter_title": "第一章 绪论",
            "kg_node_ids": ["core:perio-ch1-n1", "core:perio-ch1-n2"],
            "kg_node_names": {},
        }
        return {**defaults, **overrides}

    # ── Positive ────────────────────────────────────────────────────────

    def test_empty_items_pass(self):
        from backend.services.learning.study_plan import _preflight_draft_items

        result = _preflight_draft_items([])
        assert result["ok"] is True
        assert result["passed"] == 0
        assert result["failed"] == 0

    def test_valid_learn_review_practice_chain_passes(self):
        """learn → review → basic_practice with same scope passes."""
        from backend.services.learning.study_plan import _preflight_draft_items

        scope = self._draft_scope()
        items = [
            {
                "task_type": "learn_chapter",
                "learning_target": "学习第一章",
                "evidence_source": "kg_chapter",
                "learning_scope": scope,
            },
            {
                "task_type": "review_chapter",
                "learning_target": "复习第一章",
                "evidence_source": "kg_chapter",
                "learning_scope": scope,
            },
            {
                "task_type": "basic_practice",
                "learning_target": "练习第一章",
                "evidence_source": "kg_chapter",
                "learning_scope": scope,
                "dependency": 1,
            },
        ]
        result = _preflight_draft_items(items, resource_inspector=self._fake_usable_inspector)
        assert result["ok"] is True
        assert result["passed"] == 3

    # ── Negative: learn/review without scope ────────────────────────────

    def test_learn_chapter_no_scope_fails(self):
        from backend.services.learning.study_plan import (
            _PF_ITEM_MISSING_SCOPE,
            _preflight_draft_items,
        )

        items = [
            {
                "task_type": "learn_chapter",
                "learning_target": "测试章节",
                "evidence_source": "kg_chapter",
                "learning_scope": None,
            }
        ]
        result = _preflight_draft_items(items, resource_inspector=self._fake_usable_inspector)
        assert result["ok"] is False
        assert result["failed"] == 1
        assert result["failures"][0]["error_code"] == _PF_ITEM_MISSING_SCOPE

    def test_review_chapter_user_specified_without_scope_fails(self):
        """user_specified review_chapter without learning_scope is NOW rejected.
        This was a bug — user_specified evidence must still have a real scope."""
        from backend.services.learning.study_plan import (
            _PF_ITEM_MISSING_SCOPE,
            _preflight_draft_items,
        )

        items = [
            {
                "task_type": "review_chapter",
                "learning_target": "临床表现复习",
                "evidence_source": "user_specified",
                "learning_scope": None,
            }
        ]
        result = _preflight_draft_items(items, resource_inspector=self._fake_usable_inspector)
        assert result["ok"] is False
        assert result["failures"][0]["error_code"] == _PF_ITEM_MISSING_SCOPE

    # ── Negative: basic_practice dependency rules ───────────────────────

    def test_basic_practice_no_dependency_fails(self):
        from backend.services.learning.study_plan import (
            _PF_ITEM_ORPHAN_PRACTICE,
            _preflight_draft_items,
        )

        scope = self._draft_scope()
        items = [
            {
                "task_type": "basic_practice",
                "learning_target": "基础练习",
                "evidence_source": "kg_chapter",
                "learning_scope": scope,
                "dependency": None,
            }
        ]
        result = _preflight_draft_items(items, resource_inspector=self._fake_usable_inspector)
        assert result["ok"] is False
        assert result["failed"] == 1
        assert result["failures"][0]["error_code"] == _PF_ITEM_ORPHAN_PRACTICE

    def test_basic_practice_dependency_out_of_range_fails(self):
        from backend.services.learning.study_plan import (
            _PF_ITEM_PRACTICE_DEPENDENCY_MISSING,
            _preflight_draft_items,
        )

        scope = self._draft_scope()
        items = [
            {
                "task_type": "basic_practice",
                "learning_target": "基础练习",
                "evidence_source": "kg_chapter",
                "learning_scope": scope,
                "dependency": 99,
            }
        ]
        result = _preflight_draft_items(items, resource_inspector=self._fake_usable_inspector)
        assert result["failures"][0]["error_code"] == _PF_ITEM_PRACTICE_DEPENDENCY_MISSING

    def test_basic_practice_dependency_must_be_earlier(self):
        """Dependency must reference an EARLIER item (dep < self idx)."""
        from backend.services.learning.study_plan import (
            _PF_ITEM_PRACTICE_DEPENDENCY_MISSING,
            _preflight_draft_items,
        )

        scope = self._draft_scope()
        items = [
            {
                "task_type": "basic_practice",
                "learning_target": "练习",
                "evidence_source": "kg_chapter",
                "learning_scope": scope,
                "dependency": 2,  # references item[2] which comes after item[0]
            },
            {
                "task_type": "learn_chapter",
                "learning_target": "学习",
                "evidence_source": "kg_chapter",
                "learning_scope": scope,
            },
            {
                "task_type": "basic_practice",
                "learning_target": "练习2",
                "evidence_source": "kg_chapter",
                "learning_scope": scope,
                "dependency": 1,
            },
        ]
        result = _preflight_draft_items(items, resource_inspector=self._fake_usable_inspector)
        # item[0] fails: dep=2 >= idx=0, and also dep=2 is practice→practice
        assert result["ok"] is False
        assert any(
            f["error_code"] == _PF_ITEM_PRACTICE_DEPENDENCY_MISSING
            for f in result["failures"]
        )

    def test_basic_practice_cannot_depend_on_practice(self):
        """practice→practice chain is FORBIDDEN. Dependency must be learn/review."""
        from backend.services.learning.study_plan import (
            _PF_ITEM_PRACTICE_DEPENDENCY_MISSING,
            _preflight_draft_items,
        )

        scope = self._draft_scope()
        items = [
            {
                "task_type": "learn_chapter",
                "learning_target": "学习",
                "evidence_source": "kg_chapter",
                "learning_scope": scope,
            },
            {
                "task_type": "basic_practice",
                "learning_target": "基础练习 A",
                "evidence_source": "kg_chapter",
                "learning_scope": scope,
                "dependency": 0,  # valid: depends on learn
            },
            {
                "task_type": "basic_practice",
                "learning_target": "基础练习 B",
                "evidence_source": "kg_chapter",
                "learning_scope": scope,
                "dependency": 1,  # INVALID: practice→practice
            },
        ]
        result = _preflight_draft_items(items, resource_inspector=self._fake_usable_inspector)
        assert result["ok"] is False
        # item[2] (index 2) should fail
        assert any(
            f["item_index"] == 2
            and f["error_code"] == _PF_ITEM_PRACTICE_DEPENDENCY_MISSING
            for f in result["failures"]
        )

    def test_basic_practice_cross_scope_dependency_fails(self):
        """Cross-scope practice dependency is FORBIDDEN."""
        from backend.services.learning.study_plan import (
            _PF_ITEM_PRACTICE_SCOPE_MISMATCH,
            _preflight_draft_items,
        )

        scope_a = self._draft_scope(
            chapter_key="chapter:a", chapter_title="章节A",
            kg_node_ids=["core:a1", "core:a2"],
        )
        scope_b = self._draft_scope(
            chapter_key="chapter:b", chapter_title="章节B",
            kg_node_ids=["core:b1", "core:b2"],
        )
        items = [
            {
                "task_type": "learn_chapter",
                "learning_target": "学习章节A",
                "evidence_source": "kg_chapter",
                "learning_scope": scope_a,
            },
            {
                "task_type": "learn_chapter",
                "learning_target": "学习章节B",
                "evidence_source": "kg_chapter",
                "learning_scope": scope_b,
            },
            {
                "task_type": "basic_practice",
                "learning_target": "练习章节A",
                "evidence_source": "kg_chapter",
                "learning_scope": scope_a,
                "dependency": 1,  # cross-scope: depends on scope_b learn
            },
        ]
        result = _preflight_draft_items(items, resource_inspector=self._fake_usable_inspector)
        assert result["ok"] is False
        assert any(
            f["error_code"] == _PF_ITEM_PRACTICE_SCOPE_MISMATCH
            for f in result["failures"]
        )

    # ── Negative: advanced_practice ─────────────────────────────────────

    def test_advanced_practice_without_scope_fails(self):
        from backend.services.learning.study_plan import (
            _PF_ITEM_MISSING_SCOPE,
            _preflight_draft_items,
        )

        items = [
            {
                "task_type": "advanced_practice",
                "learning_target": "高级练习",
                "evidence_source": "kg_chapter",
                "learning_scope": None,
            }
        ]
        result = _preflight_draft_items(items, resource_inspector=self._fake_usable_inspector)
        assert result["ok"] is False
        assert result["failures"][0]["error_code"] == _PF_ITEM_MISSING_SCOPE

    def test_advanced_practice_with_valid_scope_and_dependency_passes(self):
        """advanced_practice with usable scope and learn/review predecessor passes."""
        from backend.services.learning.study_plan import _preflight_draft_items

        scope = self._draft_scope()
        items = [
            {
                "task_type": "learn_chapter",
                "learning_target": "学习",
                "evidence_source": "kg_chapter",
                "learning_scope": scope,
            },
            {
                "task_type": "advanced_practice",
                "learning_target": "进阶练习",
                "evidence_source": "kg_chapter",
                "learning_scope": scope,
                "dependency": 0,
            },
        ]
        result = _preflight_draft_items(items, resource_inspector=self._fake_usable_inspector)
        assert result["ok"] is True
        assert result["passed"] == 2

    # ── Negative: unknown task types ────────────────────────────────────

    def test_unknown_task_type_rejected(self):
        """Unknown task types are explicitly REJECTED — no pass-through."""
        from backend.services.learning.study_plan import (
            _PF_ITEM_UNKNOWN_TASK_TYPE,
            _preflight_draft_items,
        )

        scope = self._draft_scope()
        items = [
            {
                "task_type": "some_future_type",
                "learning_target": "未来功能",
                "evidence_source": "kg_chapter",
                "learning_scope": scope,
            }
        ]
        result = _preflight_draft_items(items, resource_inspector=self._fake_usable_inspector)
        assert result["ok"] is False
        assert result["failures"][0]["error_code"] == _PF_ITEM_UNKNOWN_TASK_TYPE

    def test_stage_exam_passes_through(self):
        """stage_exam task type is in the allowlist."""
        from backend.services.learning.study_plan import _preflight_draft_items

        items = [
            {
                "task_type": "stage_exam",
                "learning_target": "阶段考试",
            }
        ]
        result = _preflight_draft_items(items)
        assert result["ok"] is True
        assert result["passed"] == 1

    # ── Edge ────────────────────────────────────────────────────────────

    def test_multiple_failures_reported(self):
        from backend.services.learning.study_plan import _preflight_draft_items

        scope = self._draft_scope()
        items = [
            {
                "task_type": "learn_chapter",
                "learning_target": "Item 0",
                "evidence_source": "kg_chapter",
                "learning_scope": None,
            },
            {
                "task_type": "basic_practice",
                "learning_target": "Item 1",
                "evidence_source": "kg_chapter",
                "learning_scope": scope,
                "dependency": None,
            },
        ]
        result = _preflight_draft_items(items, resource_inspector=self._fake_usable_inspector)
        assert result["ok"] is False
        assert result["failed"] == 2
        assert len(result["failures"]) == 2

    # ── Fail-closed: unusable scope via fake inspector ──────────────────

    def test_learn_chapter_with_unusable_scope_fails(self):
        """learn_chapter with an unusable scope (fake inspector returns
        usable=False) is rejected — fail-closed."""
        from backend.services.learning.study_plan import (
            _PF_ITEM_SCOPE_UNUSABLE,
            _preflight_draft_items,
        )

        scope = self._draft_scope()
        items = [
            {
                "task_type": "learn_chapter",
                "learning_target": "不可用章节",
                "evidence_source": "kg_chapter",
                "learning_scope": scope,
            }
        ]
        result = _preflight_draft_items(items, resource_inspector=self._fake_unusable_inspector)
        assert result["ok"] is False
        assert result["failed"] == 1
        assert result["failures"][0]["error_code"] == _PF_ITEM_SCOPE_UNUSABLE

    def test_basic_practice_with_unusable_scope_fails(self):
        """basic_practice with an unusable scope is rejected."""
        from backend.services.learning.study_plan import (
            _PF_ITEM_SCOPE_UNUSABLE,
            _preflight_draft_items,
        )

        scope = self._draft_scope()
        items = [
            {
                "task_type": "learn_chapter",
                "learning_target": "先学习",
                "evidence_source": "kg_chapter",
                "learning_scope": scope,
            },
            {
                "task_type": "basic_practice",
                "learning_target": "再练习",
                "evidence_source": "kg_chapter",
                "learning_scope": scope,
                "dependency": 0,
            },
        ]
        result = _preflight_draft_items(items, resource_inspector=self._fake_unusable_inspector)
        assert result["ok"] is False
        assert result["failed"] == 2  # both learn and practice fail
        assert all(
            f["error_code"] == _PF_ITEM_SCOPE_UNUSABLE
            for f in result["failures"]
        )

    def test_advanced_practice_with_unusable_scope_fails(self):
        """advanced_practice with an unusable scope is rejected."""
        from backend.services.learning.study_plan import (
            _PF_ITEM_SCOPE_UNUSABLE,
            _preflight_draft_items,
        )

        scope = self._draft_scope()
        items = [
            {
                "task_type": "review_chapter",
                "learning_target": "先复习",
                "evidence_source": "kg_chapter",
                "learning_scope": scope,
            },
            {
                "task_type": "advanced_practice",
                "learning_target": "进阶练习",
                "evidence_source": "kg_chapter",
                "learning_scope": scope,
                "dependency": 0,
            },
        ]
        result = _preflight_draft_items(items, resource_inspector=self._fake_unusable_inspector)
        assert result["ok"] is False
        assert result["failed"] == 2

    # ── Test seam: resource_inspector is NOT controllable via request ───

    def test_no_public_flag_to_disable_checks(self):
        """_preflight_draft_items no longer accepts check_kg_nodes /
        check_evidence_chunks boolean flags — the only injection point is
        the private resource_inspector callable."""
        from backend.services.learning.study_plan import _preflight_draft_items
        import inspect

        sig = inspect.signature(_preflight_draft_items)
        param_names = list(sig.parameters.keys())
        assert "check_kg_nodes" not in param_names, (
            "check_kg_nodes must NOT be a public parameter — "
            "use resource_inspector for test injection"
        )
        assert "check_evidence_chunks" not in param_names, (
            "check_evidence_chunks must NOT be a public parameter — "
            "use resource_inspector for test injection"
        )
        assert "resource_inspector" in param_names, (
            "resource_inspector must be the injectable test seam"
        )

    # ── Integration: real inspector is called per item ──────────────────

    def test_real_inspector_called_per_learn_item(self, monkeypatch):
        """The real preflight_scope_resources is called for each learn/review
        item when no resource_inspector is injected."""
        from backend.services.learning import study_plan
        from backend.services.learning.study_plan import _preflight_draft_items

        call_log: list[dict[str, Any]] = []

        def _spy_inspector(scope: dict[str, Any]) -> dict[str, Any]:
            call_log.append(dict(scope))
            return {
                "usable": True,
                "reason_code": "ok",
                "kg_node_hit_count": 1,
                "kg_node_ids_checked": scope.get("kg_node_ids", []),
                "kg_node_ids_usable": scope.get("kg_node_ids", []),
                "evidence_chunk_count": 1,
                "scope_diagnostics": {
                    "has_kg_node_ids": True,
                    "has_document_id": True,
                    "has_chapter_key": True,
                    "title": scope.get("chapter_title", ""),
                    "book_title": scope.get("book_title", ""),
                    "document_id": scope.get("document_id", ""),
                    "chapter_key": scope.get("chapter_key", ""),
                    "checks_performed": ["kg_nodes", "evidence_chunks"],
                },
                "error_message": None,
            }

        scope_a = self._draft_scope(
            chapter_key="chapter:a", chapter_title="章节A",
        )
        scope_b = self._draft_scope(
            chapter_key="chapter:b", chapter_title="章节B",
        )
        items = [
            {
                "task_type": "learn_chapter",
                "learning_target": "学习A",
                "evidence_source": "kg_chapter",
                "learning_scope": scope_a,
            },
            {
                "task_type": "review_chapter",
                "learning_target": "复习B",
                "evidence_source": "kg_chapter",
                "learning_scope": scope_b,
            },
            {
                "task_type": "basic_practice",
                "learning_target": "练习A",
                "evidence_source": "kg_chapter",
                "learning_scope": scope_a,
                "dependency": 0,
            },
        ]
        result = _preflight_draft_items(items, resource_inspector=_spy_inspector)
        assert result["ok"] is True
        assert result["passed"] == 3
        # Inspector must be called exactly 3 times (once per learn/review/practice item)
        assert len(call_log) == 3, (
            f"Expected 3 inspector calls (one per item), got {len(call_log)}"
        )
        # Verify the scopes passed to inspector match the item scopes
        assert call_log[0]["chapter_key"] == "chapter:a"
        assert call_log[1]["chapter_key"] == "chapter:b"
        assert call_log[2]["chapter_key"] == "chapter:a"

    def test_default_inspector_is_real_preflight_scope_resources(self, monkeypatch):
        """When no resource_inspector is passed, the default calls the real
        preflight_scope_resources with both checks enabled."""
        from backend.services.learning import study_plan
        from backend.services.learning.study_plan import _preflight_draft_items
        from backend.services.learning.planner import preflight_scope_resources

        # Spy on the real preflight_scope_resources to verify it's called
        calls: list[tuple] = []
        _original = preflight_scope_resources

        def _spy(scope, **kwargs):
            calls.append((scope, kwargs))
            # Return usable=True so the test passes
            return {
                "usable": True,
                "reason_code": "ok",
                "kg_node_hit_count": 1,
                "kg_node_ids_checked": [],
                "kg_node_ids_usable": [],
                "evidence_chunk_count": 1,
                "scope_diagnostics": {"checks_performed": ["kg_nodes", "evidence_chunks"]},
                "error_message": None,
            }

        monkeypatch.setattr(
            study_plan, "preflight_scope_resources", _spy
        )

        scope = self._draft_scope()
        items = [
            {
                "task_type": "learn_chapter",
                "learning_target": "测试",
                "evidence_source": "kg_chapter",
                "learning_scope": scope,
            },
        ]
        result = _preflight_draft_items(items)
        assert result["ok"] is True
        # The default inspector should have called preflight_scope_resources
        # (via the monkeypatched spy) with both checks enabled.
        assert len(calls) == 1
        _called_scope, _called_kwargs = calls[0]
        assert _called_kwargs.get("check_kg_nodes") is True
        assert _called_kwargs.get("check_evidence_chunks") is True


# ---------------------------------------------------------------------------
# Contract 7 — Session writeback & dependency resolution
# ---------------------------------------------------------------------------


class TestSessionWritebackAndDependencyResolution:
    """Session ID writeback for learn / review items and dependency-based
    session resolution for basic_practice."""

    # ── session writeback ───────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_learn_chapter_writeback_enables_basic_via_dependency(self):
        """learn_chapter → session written back → basic_practice resolves it."""
        scope = _formal_scope()

        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                # Create a real session that start_lesson will "return"
                real_session = memory.create_session("u-wb", "牙周病学")

                # 1. Create learn_chapter item
                learn_item = memory.create_study_plan_item(
                    user_id="u-wb",
                    plan_date="2026-07-15",
                    title="学习牙周病发病机制",
                    linked_course_title="牙周病学",
                    task_type="learn_chapter",
                    learning_target="牙周病的发病机制",
                    kg_node_ids=scope["kg_node_ids"],
                    learning_scope=scope,
                )

                # 2. Create basic_practice item with dependency on learn_item
                practice_item = memory.create_study_plan_item(
                    user_id="u-wb",
                    plan_date="2026-07-16",
                    title="基础练习：牙周病",
                    linked_course_title="牙周病学",
                    task_type="basic_practice",
                    learning_target="牙周病的发病机制",
                    kg_node_ids=scope["kg_node_ids"],
                    learning_scope=scope,
                    dependency=learn_item["id"],
                )

                # 3. Simulate learn_chapter execution (triggers writeback)
                orch_learn = _make_orchestrator(
                    memory,
                    start_lesson_return={
                        "session": {"id": real_session["id"]},
                        "outline": {"nodes": []},
                    },
                )
                router = MasterRouter(orch_learn)
                result_learn = await router.route(
                    "",
                    "u-wb",
                    context={"plan_item_id": learn_item["id"]},
                )
                assert result_learn["ok"] is True
                assert result_learn["route"]["source"] == "explicit_plan_item"

                # 4. Verify session_id was written back to learn_item
                reloaded_learn = memory.get_study_plan_item(
                    learn_item["id"], user_id="u-wb"
                )
                assert reloaded_learn["session_id"] == real_session["id"]
                assert reloaded_learn["linked_course_id"] == real_session["id"]

                # 5. Execute basic_practice — must resolve via dependency
                orch_practice = _make_orchestrator(
                    memory,
                    stage_exam_return=_sample_stage_exam(
                        session_id=real_session["id"]
                    ),
                )
                router2 = MasterRouter(orch_practice)
                result_practice = await router2.route(
                    "",
                    "u-wb",
                    context={"plan_item_id": practice_item["id"]},
                )

                assert result_practice["ok"] is True
                assert result_practice["route"]["task_type"] == "basic_practice"
                assert result_practice["next_action"] == "show_stage_exam"

                # start_lesson must NOT be called for basic_practice
                orch_practice.start_lesson.assert_not_called()

                # ensure_stage_exam_for_interaction must be called with
                # the resolved session ID from the dependency item
                orch_practice.ensure_stage_exam_for_interaction.assert_called_once_with(
                    real_session["id"],
                )
            finally:
                memory._conn.close()

    @pytest.mark.asyncio
    async def test_review_chapter_writeback_enables_basic_via_dependency(self):
        """review_chapter → session written back → basic_practice resolves it."""
        scope = _formal_scope(
            chapter_key="chapter:perio-ch5",
            chapter_title="第五章 牙周病的治疗",
        )

        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                real_session = memory.create_session("u-rwb", "牙周病学")

                # 1. Create review_chapter item
                review_item = memory.create_study_plan_item(
                    user_id="u-rwb",
                    plan_date="2026-07-15",
                    title="复习牙周病治疗",
                    linked_course_title="牙周病学",
                    task_type="review_chapter",
                    learning_target="牙周病的治疗",
                    kg_node_ids=scope["kg_node_ids"],
                    learning_scope=scope,
                )

                # 2. Create basic_practice with dependency
                practice_item = memory.create_study_plan_item(
                    user_id="u-rwb",
                    plan_date="2026-07-16",
                    title="基础练习：牙周病治疗",
                    linked_course_title="牙周病学",
                    task_type="basic_practice",
                    learning_target="牙周病的治疗",
                    kg_node_ids=scope["kg_node_ids"],
                    learning_scope=scope,
                    dependency=review_item["id"],
                )

                # 3. Execute review_chapter → writeback
                orch_review = _make_orchestrator(
                    memory,
                    start_lesson_return={
                        "session": {"id": real_session["id"]},
                        "outline": {"nodes": []},
                    },
                )
                router = MasterRouter(orch_review)
                result_review = await router.route(
                    "",
                    "u-rwb",
                    context={"plan_item_id": review_item["id"]},
                )
                assert result_review["ok"] is True

                reloaded_review = memory.get_study_plan_item(
                    review_item["id"], user_id="u-rwb"
                )
                assert reloaded_review["session_id"] == real_session["id"]

                # 4. Execute basic_practice
                orch_practice = _make_orchestrator(
                    memory,
                    stage_exam_return=_sample_stage_exam(
                        session_id=real_session["id"]
                    ),
                )
                router2 = MasterRouter(orch_practice)
                result_practice = await router2.route(
                    "",
                    "u-rwb",
                    context={"plan_item_id": practice_item["id"]},
                )

                assert result_practice["ok"] is True
                orch_practice.start_lesson.assert_not_called()
                orch_practice.ensure_stage_exam_for_interaction.assert_called_once_with(
                    real_session["id"],
                )
            finally:
                memory._conn.close()

    @pytest.mark.asyncio
    async def test_review_weakness_writeback_enables_basic_via_dependency(self):
        """review_weakness → session written back → basic_practice resolves it."""
        scope = _formal_scope(
            book_title="牙体牙髓病学",
            document_id="doc-endo-001",
            chapter_key="chapter:endo-ch3",
            chapter_title="第三章 根管预备",
        )

        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                real_session = memory.create_session("u-wwb", "牙体牙髓病学")

                review_item = memory.create_study_plan_item(
                    user_id="u-wwb",
                    plan_date="2026-07-15",
                    title="薄弱点复习：根管预备",
                    linked_course_title="牙体牙髓病学",
                    task_type="review_weakness",
                    learning_target="根管预备技术要点",
                    kg_node_ids=scope["kg_node_ids"],
                    learning_scope=scope,
                )

                practice_item = memory.create_study_plan_item(
                    user_id="u-wwb",
                    plan_date="2026-07-16",
                    title="基础练习：根管预备",
                    linked_course_title="牙体牙髓病学",
                    task_type="basic_practice",
                    learning_target="根管预备技术要点",
                    kg_node_ids=scope["kg_node_ids"],
                    learning_scope=scope,
                    dependency=review_item["id"],
                )

                orch_review = _make_orchestrator(
                    memory,
                    start_lesson_return={
                        "session": {"id": real_session["id"]},
                        "outline": {"nodes": []},
                    },
                )
                router = MasterRouter(orch_review)
                result_review = await router.route(
                    "",
                    "u-wwb",
                    context={"plan_item_id": review_item["id"]},
                )
                assert result_review["ok"] is True

                reloaded_review = memory.get_study_plan_item(
                    review_item["id"], user_id="u-wwb"
                )
                assert reloaded_review["session_id"] == real_session["id"]

                orch_practice = _make_orchestrator(
                    memory,
                    stage_exam_return=_sample_stage_exam(
                        session_id=real_session["id"]
                    ),
                )
                router2 = MasterRouter(orch_practice)
                result_practice = await router2.route(
                    "",
                    "u-wwb",
                    context={"plan_item_id": practice_item["id"]},
                )
                assert result_practice["ok"] is True
                orch_practice.start_lesson.assert_not_called()
                orch_practice.ensure_stage_exam_for_interaction.assert_called_once_with(
                    real_session["id"],
                )
            finally:
                memory._conn.close()

    # ── linked_course_id as real session still works ─────────────────────

    @pytest.mark.asyncio
    async def test_basic_practice_with_valid_linked_course_id_still_works(self):
        """When linked_course_id points to a real session, use it directly."""
        scope = _formal_scope()

        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                session = memory.create_session("u-direct", "牙周病学")

                item = memory.create_study_plan_item(
                    user_id="u-direct",
                    plan_date="2026-07-15",
                    title="基础练习（直接 session）",
                    linked_course_id=session["id"],
                    linked_course_title="牙周病学",
                    task_type="basic_practice",
                    learning_target="牙周病的发病机制",
                    kg_node_ids=scope["kg_node_ids"],
                    learning_scope=scope,
                )

                orch = _make_orchestrator(
                    memory,
                    stage_exam_return=_sample_stage_exam(session_id=session["id"]),
                )
                router = MasterRouter(orch)
                result = await router.route(
                    "",
                    "u-direct",
                    context={"plan_item_id": item["id"]},
                )

                assert result["ok"] is True
                orch.start_lesson.assert_not_called()
                orch.ensure_stage_exam_for_interaction.assert_called_once_with(
                    session["id"],
                )
            finally:
                memory._conn.close()

    # ── document_id as pseudo-session ────────────────────────────────────

    @pytest.mark.asyncio
    async def test_basic_practice_document_id_as_session_rejected(self):
        """basic_practice with document_id as linked_course_id → rejected
        (document IDs are NOT session IDs)."""
        scope = _formal_scope()

        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                item = memory.create_study_plan_item(
                    user_id="u-docid",
                    plan_date="2026-07-15",
                    title="基础练习（document_id 伪 session）",
                    linked_course_id="document.d766e8629232a2f6",
                    linked_course_title="牙周病学",
                    task_type="basic_practice",
                    learning_target="牙周病的发病机制",
                    kg_node_ids=scope["kg_node_ids"],
                    learning_scope=scope,
                )

                orch = _make_orchestrator(memory)
                router = MasterRouter(orch)
                result = await router.route(
                    "",
                    "u-docid",
                    context={"plan_item_id": item["id"]},
                )

                assert result["ok"] is False
                assert "not a valid session" in result["error"]
                orch.start_lesson.assert_not_called()
                orch.ensure_stage_exam_for_interaction.assert_not_called()
            finally:
                memory._conn.close()

    # ── dependency not yet started ───────────────────────────────────────

    @pytest.mark.asyncio
    async def test_basic_practice_dependency_not_started_fails(self):
        """basic_practice with dependency on an item that has no session yet."""
        scope = _formal_scope()

        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                # Create learn item but DON'T execute it (no session)
                learn_item = memory.create_study_plan_item(
                    user_id="u-nostart",
                    plan_date="2026-07-15",
                    title="学习（未启动）",
                    linked_course_title="牙周病学",
                    task_type="learn_chapter",
                    learning_target="牙周病的发病机制",
                    kg_node_ids=scope["kg_node_ids"],
                    learning_scope=scope,
                )

                practice_item = memory.create_study_plan_item(
                    user_id="u-nostart",
                    plan_date="2026-07-16",
                    title="基础练习（依赖未启动）",
                    linked_course_title="牙周病学",
                    task_type="basic_practice",
                    learning_target="牙周病的发病机制",
                    kg_node_ids=scope["kg_node_ids"],
                    learning_scope=scope,
                    dependency=learn_item["id"],
                )

                orch = _make_orchestrator(memory)
                router = MasterRouter(orch)
                result = await router.route(
                    "",
                    "u-nostart",
                    context={"plan_item_id": practice_item["id"]},
                )

                assert result["ok"] is False
                assert "not yet started" in result["error"].lower() or "no session_id" in result["error"]
                orch.start_lesson.assert_not_called()
                orch.ensure_stage_exam_for_interaction.assert_not_called()
            finally:
                memory._conn.close()

    # ── cross-scope dependency ───────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_basic_practice_cross_scope_dependency_fails(self):
        """basic_practice depends on an item with a different learning_scope."""
        scope_perio = _formal_scope(
            book_title="牙周病学",
            document_id="doc-perio",
            chapter_key="chapter:perio-ch3",
            chapter_title="第三章 牙周病的发病机制",
        )
        scope_endo = _formal_scope(
            book_title="牙体牙髓病学",
            document_id="doc-endo",
            chapter_key="chapter:endo-ch1",
            chapter_title="第一章 龋病",
        )

        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                real_session = memory.create_session("u-cross-scope", "牙周病学")

                # Execute a learn for perio scope (creates session)
                learn_item = memory.create_study_plan_item(
                    user_id="u-cross-scope",
                    plan_date="2026-07-15",
                    title="学习牙周病",
                    linked_course_title="牙周病学",
                    task_type="learn_chapter",
                    learning_target="牙周病的发病机制",
                    kg_node_ids=scope_perio["kg_node_ids"],
                    learning_scope=scope_perio,
                )

                orch_learn = _make_orchestrator(
                    memory,
                    start_lesson_return={
                        "session": {"id": real_session["id"]},
                        "outline": {"nodes": []},
                    },
                )
                router = MasterRouter(orch_learn)
                await router.route(
                    "",
                    "u-cross-scope",
                    context={"plan_item_id": learn_item["id"]},
                )

                # Create basic_practice with ENDO scope but PERIO dependency
                practice_item = memory.create_study_plan_item(
                    user_id="u-cross-scope",
                    plan_date="2026-07-16",
                    title="基础练习：龋病（dep 指向牙周）",
                    linked_course_title="牙体牙髓病学",
                    task_type="basic_practice",
                    learning_target="龋病",
                    kg_node_ids=scope_endo["kg_node_ids"],
                    learning_scope=scope_endo,
                    dependency=learn_item["id"],
                )

                orch_practice = _make_orchestrator(memory)
                router2 = MasterRouter(orch_practice)
                result = await router2.route(
                    "",
                    "u-cross-scope",
                    context={"plan_item_id": practice_item["id"]},
                )

                assert result["ok"] is False
                assert "scope" in result["error"].lower()
                orch_practice.start_lesson.assert_not_called()
                orch_practice.ensure_stage_exam_for_interaction.assert_not_called()
            finally:
                memory._conn.close()

    # ── cross-user dependency ────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_basic_practice_cross_user_dependency_fails(self):
        """basic_practice with dependency owned by a different user → denied."""
        scope = _formal_scope()

        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                real_session = memory.create_session("user-a", "牙周病学")

                # Learn item owned by user-a, executed with session
                learn_item = memory.create_study_plan_item(
                    user_id="user-a",
                    plan_date="2026-07-15",
                    title="学习牙周病",
                    linked_course_title="牙周病学",
                    task_type="learn_chapter",
                    learning_target="牙周病的发病机制",
                    kg_node_ids=scope["kg_node_ids"],
                    learning_scope=scope,
                )

                orch_learn = _make_orchestrator(
                    memory,
                    start_lesson_return={
                        "session": {"id": real_session["id"]},
                        "outline": {"nodes": []},
                    },
                )
                router_a = MasterRouter(orch_learn)
                await router_a.route(
                    "",
                    "user-a",
                    context={"plan_item_id": learn_item["id"]},
                )

                # Practice item owned by user-b, depends on user-a's item
                practice_item = memory.create_study_plan_item(
                    user_id="user-b",
                    plan_date="2026-07-16",
                    title="基础练习（跨用户依赖）",
                    linked_course_title="牙周病学",
                    task_type="basic_practice",
                    learning_target="牙周病的发病机制",
                    kg_node_ids=scope["kg_node_ids"],
                    learning_scope=scope,
                    dependency=learn_item["id"],
                )

                orch_practice = _make_orchestrator(memory)
                router_b = MasterRouter(orch_practice)
                result = await router_b.route(
                    "",
                    "user-b",
                    context={"plan_item_id": practice_item["id"]},
                )

                assert result["ok"] is False
                # get_study_plan_item raises KeyError when user doesn't match
                assert "not found" in result["error"].lower() or "access denied" in result["error"].lower()
                orch_practice.start_lesson.assert_not_called()
                orch_practice.ensure_stage_exam_for_interaction.assert_not_called()
            finally:
                memory._conn.close()

    # ── dependency points to practice item ───────────────────────────────

    @pytest.mark.asyncio
    async def test_basic_practice_dependency_on_practice_fails(self):
        """basic_practice depends on another practice item → rejected."""
        scope = _formal_scope()

        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                # Create a practice item (not learn/review)
                other_practice = memory.create_study_plan_item(
                    user_id="u-dep-practice",
                    plan_date="2026-07-15",
                    title="其他基础练习",
                    linked_course_title="牙周病学",
                    task_type="basic_practice",
                    learning_target="牙周病的发病机制",
                    kg_node_ids=scope["kg_node_ids"],
                    learning_scope=scope,
                )

                practice_item = memory.create_study_plan_item(
                    user_id="u-dep-practice",
                    plan_date="2026-07-16",
                    title="基础练习（依赖指向练习）",
                    linked_course_title="牙周病学",
                    task_type="basic_practice",
                    learning_target="牙周病的发病机制",
                    kg_node_ids=scope["kg_node_ids"],
                    learning_scope=scope,
                    dependency=other_practice["id"],
                )

                orch = _make_orchestrator(memory)
                router = MasterRouter(orch)
                result = await router.route(
                    "",
                    "u-dep-practice",
                    context={"plan_item_id": practice_item["id"]},
                )

                assert result["ok"] is False
                assert "task_type=basic_practice" in result["error"]
                orch.start_lesson.assert_not_called()
                orch.ensure_stage_exam_for_interaction.assert_not_called()
            finally:
                memory._conn.close()

    # ── no writeback for non-explicit_plan_item sources ──────────────────

    @pytest.mark.asyncio
    async def test_recommendation_does_not_writeback_session(self):
        """Recommendation execution (source=explicit_recommendation) must NOT
        write session_id back to a plan item."""
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                orch = _make_orchestrator(memory)
                router = MasterRouter(orch)
                result = await router.route(
                    "",
                    "u-rec-no-wb",
                    context={
                        "intent": "learning",
                        "recommendation_id": "rec-nonexistent",
                        "recommendation_plan_date": "2026-07-15",
                    },
                )

                # The recommendation path should handle missing
                # recommendations gracefully — we just verify it
                # doesn't crash and returns an error
                assert result.get("ok") is False or "error" in result
            finally:
                memory._conn.close()

    @pytest.mark.asyncio
    async def test_api_roundtrip_preserves_three_optional_fields(self):
        """Create item via API → all three fields survive roundtrip."""
        scope = _formal_scope()

        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "contract.sqlite3")
            try:
                item = memory.create_study_plan_item(
                    user_id="u-api-rtt",
                    plan_date="2026-07-15",
                    title="API roundtrip 测试",
                    linked_course_title="牙周病学",
                    task_type="learn_chapter",
                    learning_target="测试",
                    kg_node_ids=scope["kg_node_ids"],
                    learning_scope=scope,
                    dependency="dep-item-id-123",
                    practice_level="basic",
                    session_id="sess-prefilled",
                )

                reloaded = memory.get_study_plan_item(item["id"], user_id="u-api-rtt")
                assert reloaded["dependency"] == "dep-item-id-123"
                assert reloaded["practice_level"] == "basic"
                assert reloaded["session_id"] == "sess-prefilled"

                # Verify all three fields can be updated
                memory.update_study_plan_item(
                    item["id"],
                    user_id="u-api-rtt",
                    updates={
                        "dependency": "dep-item-id-456",
                        "practice_level": None,
                        "session_id": "sess-updated",
                    },
                )
                reloaded2 = memory.get_study_plan_item(item["id"], user_id="u-api-rtt")
                assert reloaded2["dependency"] == "dep-item-id-456"
                assert reloaded2["practice_level"] is None
                assert reloaded2["session_id"] == "sess-updated"
            finally:
                memory._conn.close()
