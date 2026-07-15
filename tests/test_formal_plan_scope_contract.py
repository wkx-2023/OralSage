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


def _make_orchestrator(
    memory: LearningMemory,
    *,
    start_lesson_return: dict[str, Any] | None = None,
    continue_return: dict[str, Any] | None = None,
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
