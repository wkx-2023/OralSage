from __future__ import annotations

import asyncio
import unittest
from typing import Any
from unittest.mock import MagicMock

from backend.services.router.jobs import RouterJobManager
from backend.services.router.master_router import MasterRouter


class _LearningRouter:
    async def route(self, **_kwargs):
        await asyncio.sleep(0.01)
        return {
            "ok": True,
            "route": {
                "intent": "learning",
                "target": "learning_orchestrator",
                "source": "llm_router",
                "reason": "test",
            },
            "final_result": {
                "session": {"id": "lesson-test", "topic": "牙周炎", "user_id": "u1"},
                "outline": {
                    "title": "牙周炎",
                    "nodes": [{"id": "node-1", "title": "病因与机制", "secret": "drop"}],
                },
                "evidence_pool": {"large": "must not reach chat"},
                "current_outline_node_id": "node-1",
            },
            "next_action": "start_session",
        }


class _LearningOrchestrator:
    def next_step(self, session_id, stream_callback=None):
        if stream_callback is not None:
            stream_callback("这是本节")
            stream_callback("有效讲稿。")
        return {
            "session_id": session_id,
            "outline_node_id": "node-1",
            "teaching_text": "这是本节有效讲稿。",
            "questions": [],
            "content_status": "llm_generated",
            "internal_trace": {"drop": True},
        }

    def wait_for_ppt_resources(self, session_id, *, timeout_seconds):
        return {"session_id": session_id, "status": "pending", "timeout": timeout_seconds}


class _ContinueRouter:
    async def route(self, **_kwargs):
        return {
            "ok": True,
            "route": {
                "intent": "learning",
                "target": "learning_orchestrator",
                "source": "explicit_recommendation",
                "reason": "continue selected recommendation",
            },
            "final_result": {
                "session": {"id": "lesson-existing", "topic": "口腔溃疡", "user_id": "u1"},
                "tutor_step": {
                    "session_id": "lesson-existing",
                    "outline_node_id": "node-2",
                    "teaching_text": "已经推进后的第二节。",
                    "questions": [],
                },
                "lesson_completed": False,
            },
            "next_action": "continue_session",
        }


class _ContinueOrchestrator(_LearningOrchestrator):
    def next_step(self, session_id, stream_callback=None):
        raise AssertionError("continued plan task must not advance the session twice")


class RouterJobManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_learning_job_emits_replayable_compact_result(self):
        manager = RouterJobManager(_LearningRouter(), _LearningOrchestrator())
        created = manager.create(
            message="学习牙周炎",
            user_id="u1",
            history=[],
            context={"user_id": "u1"},
        )
        job_id = created["job_id"]

        for _ in range(100):
            snapshot = manager.snapshot(job_id)
            if snapshot["done"]:
                break
            await asyncio.sleep(0.01)
        else:
            self.fail("router job did not complete")

        self.assertEqual(snapshot["status"], "complete")
        event_types = [event["type"] for event in snapshot["events"]]
        self.assertIn("route_ready", event_types)
        self.assertIn("content_delta", event_types)
        self.assertEqual(event_types[-1], "complete")

        result = snapshot["events"][-1]["data"]
        router_result = result["router_result"]
        self.assertNotIn("evidence_pool", router_result["final_result"])
        self.assertEqual(
            router_result["final_result"]["outline"]["nodes"],
            [{"id": "node-1", "title": "病因与机制"}],
        )
        self.assertEqual(result["learning_step"]["teaching_text"], "这是本节有效讲稿。")
        self.assertNotIn("internal_trace", result["learning_step"])

        last_id = snapshot["events"][-2]["id"]
        replay = manager.snapshot(job_id, after=last_id)
        self.assertEqual(len(replay["events"]), 1)
        self.assertEqual(replay["events"][0]["type"], "complete")

    async def test_continued_recommendation_does_not_advance_twice(self):
        manager = RouterJobManager(_ContinueRouter(), _ContinueOrchestrator())
        created = manager.create(
            message="",
            user_id="u1",
            history=[],
            context={"user_id": "u1", "recommendation_id": "continue:test"},
        )

        for _ in range(100):
            snapshot = manager.snapshot(created["job_id"])
            if snapshot["done"]:
                break
            await asyncio.sleep(0.01)
        else:
            self.fail("router job did not complete")

        self.assertEqual(snapshot["status"], "complete")
        result = snapshot["events"][-1]["data"]
        self.assertEqual(result["learning_step"]["outline_node_id"], "node-2")
        self.assertEqual(result["learning_step"]["teaching_text"], "已经推进后的第二节。")


class _DirectQARouter:
    """A mock router that returns direct_qa results (no LLM/KAG)."""

    def __init__(self, qa_result: dict[str, Any] | None = None) -> None:
        self._qa_result = qa_result or {
            "answer": "氟斑牙的治疗方法包括树脂渗透、贴面修复和全冠修复等。",
            "resolved_question": "氟斑牙怎么治疗？",
            "conversation_context_used": False,
            "used_evidence": [],
            "evidence_status": "sufficient",
            "should_reinforce": False,
            "confusion_kg_node_ids": [],
            "trace": {"mode": "direct_qa_evidence", "total_seconds": 0.5},
        }

    async def route(self, **_kwargs):
        await asyncio.sleep(0.01)
        return {
            "ok": True,
            "route": {
                "intent": "direct_qa",
                "target": "direct_qa_service",
                "source": "llm_router",
                "reason": "具体口腔医学问题，走直接问答。",
            },
            "final_result": dict(self._qa_result),
            "next_action": "show_direct_answer",
        }


class RouterJobManagerDirectQATests(unittest.IsolatedAsyncioTestCase):
    """Tests that RouterJobManager correctly passes Direct QA final_result.answer.

    These tests prove that the job path (→ snapshot → complete event) preserves
    the service's current answer and does NOT fall back to learning / PPT /
    literature / cached content.
    """

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _unique_answer() -> str:
        """Return a unique answer text so identity can be asserted."""
        return (
            "牙隐裂的典型症状包括咬合痛、冷热刺激痛、定点咀嚼痛，"
            "可通过染色法、透照法和咬诊进行诊断。"
        )

    @staticmethod
    def _mock_orchestrator():
        orch = MagicMock()
        orch.memory = MagicMock()
        return orch

    # ------------------------------------------------------------------
    # positive answer identity through job path
    # ------------------------------------------------------------------

    async def test_direct_qa_job_final_result_answer_is_service_answer(self):
        """Job complete event → router_result.final_result.answer IS the service answer."""
        expected_answer = self._unique_answer()
        qa_router = _DirectQARouter({"answer": expected_answer})
        manager = RouterJobManager(qa_router, self._mock_orchestrator())

        created = manager.create(
            message="牙隐裂怎么诊断？",
            user_id="user-dqa-job-1",
            history=[],
            context={"user_id": "user-dqa-job-1"},
        )
        job_id = created["job_id"]

        for _ in range(100):
            snapshot = manager.snapshot(job_id)
            if snapshot["done"]:
                break
            await asyncio.sleep(0.01)
        else:
            self.fail("router job did not complete")

        self.assertEqual(snapshot["status"], "complete")

        # Extract the answer from the complete event
        complete_event = snapshot["events"][-1]
        self.assertEqual(complete_event["type"], "complete")

        router_result = complete_event["data"]["router_result"]
        self.assertTrue(router_result["ok"])
        self.assertEqual(router_result["route"]["intent"], "direct_qa")
        self.assertEqual(router_result["next_action"], "show_direct_answer")

        final_result = router_result["final_result"]
        # The answer must be the exact service answer
        self.assertEqual(final_result["answer"], expected_answer)
        self.assertIn("牙隐裂", final_result["answer"])
        self.assertIn("咬合痛", final_result["answer"])

        # learning_step must be None for direct_qa (no session → no next_step)
        self.assertIsNone(complete_event["data"]["learning_step"])

    async def test_direct_qa_job_answer_identity_across_two_topics(self):
        """Two structurally different topics → each job preserves its own answer."""
        answer_a = "根管治疗的成功率在规范操作下可达90%以上，关键在于彻底清创和严密充填。"
        answer_b = "牙周基础治疗包括龈上洁治、龈下刮治和根面平整，是牙周炎治疗的第一步。"

        # Topic A
        qa_a = _DirectQARouter({"answer": answer_a})
        manager_a = RouterJobManager(qa_a, self._mock_orchestrator())
        created_a = manager_a.create(
            message="根管治疗的成功率如何？",
            user_id="user-dqa-endo",
            history=[],
            context={"user_id": "user-dqa-endo"},
        )
        for _ in range(100):
            snap_a = manager_a.snapshot(created_a["job_id"])
            if snap_a["done"]:
                break
            await asyncio.sleep(0.01)

        # Topic B
        qa_b = _DirectQARouter({"answer": answer_b})
        manager_b = RouterJobManager(qa_b, self._mock_orchestrator())
        created_b = manager_b.create(
            message="牙周基础治疗包括哪些？",
            user_id="user-dqa-perio",
            history=[],
            context={"user_id": "user-dqa-perio"},
        )
        for _ in range(100):
            snap_b = manager_b.snapshot(created_b["job_id"])
            if snap_b["done"]:
                break
            await asyncio.sleep(0.01)

        ans_a = snap_a["events"][-1]["data"]["router_result"]["final_result"]["answer"]
        ans_b = snap_b["events"][-1]["data"]["router_result"]["final_result"]["answer"]

        self.assertEqual(ans_a, answer_a)
        self.assertEqual(ans_b, answer_b)
        # Must not be the same (no cached/shared answer)
        self.assertNotEqual(ans_a, ans_b)

    # ------------------------------------------------------------------
    # negative — answer must NOT be from learning / PPT / literature / cache
    # ------------------------------------------------------------------

    async def test_direct_qa_job_has_no_session_outline_or_ppt(self):
        """Job result must NOT contain session, outline, PPT, or learning fields."""
        qa_router = _DirectQARouter()
        manager = RouterJobManager(qa_router, self._mock_orchestrator())

        created = manager.create(
            message="龋病的四联因素是什么？",
            user_id="user-dqa-neg-1",
            history=[],
            context={"user_id": "user-dqa-neg-1"},
        )
        for _ in range(100):
            snapshot = manager.snapshot(created["job_id"])
            if snapshot["done"]:
                break
            await asyncio.sleep(0.01)

        complete_data = snapshot["events"][-1]["data"]
        router_result = complete_data["router_result"]
        final_result = router_result["final_result"]

        # Forbidden keys that would indicate learning/PPT/literature fallback
        forbidden = {"session", "session_id", "outline", "ppt", "pptx",
                     "slides", "candidates", "articles", "recommendations"}
        result_keys = set(final_result.keys())
        self.assertTrue(
            result_keys.isdisjoint(forbidden),
            f"final_result contains forbidden keys: {result_keys & forbidden}",
        )

        # learning_step must be None (no tutoring happened)
        self.assertIsNone(complete_data["learning_step"])

    async def test_direct_qa_job_answer_is_not_placeholder_or_cached(self):
        """Answer must NOT be a placeholder/fallback/cached string."""
        expected = self._unique_answer()
        qa_router = _DirectQARouter({"answer": expected})
        manager = RouterJobManager(qa_router, self._mock_orchestrator())

        created = manager.create(
            message="牙隐裂怎么诊断？",
            user_id="user-dqa-neg-2",
            history=[],
            context={"user_id": "user-dqa-neg-2"},
        )
        for _ in range(100):
            snapshot = manager.snapshot(created["job_id"])
            if snapshot["done"]:
                break
            await asyncio.sleep(0.01)

        answer = snapshot["events"][-1]["data"]["router_result"]["final_result"]["answer"]

        # Must NOT be the generic unavailable fallback
        self.assertNotIn("不可用", answer)
        # Must NOT be a learning rebuild placeholder
        self.assertNotIn("旧总控 agent 已停用", answer)
        # Must NOT be an interface-removed stub
        self.assertNotIn("Interface retained", answer)
        # Must be the exact service answer
        self.assertEqual(answer, expected)

    async def test_direct_qa_job_route_ready_event_matches_intent(self):
        """The route_ready event must carry direct_qa route and show_direct_answer."""
        qa_router = _DirectQARouter()
        manager = RouterJobManager(qa_router, self._mock_orchestrator())

        created = manager.create(
            message="口腔扁平苔藓如何治疗？",
            user_id="user-dqa-event",
            history=[],
            context={"user_id": "user-dqa-event"},
        )
        for _ in range(100):
            snapshot = manager.snapshot(created["job_id"])
            if snapshot["done"]:
                break
            await asyncio.sleep(0.01)

        route_events = [e for e in snapshot["events"] if e["type"] == "route_ready"]
        self.assertEqual(len(route_events), 1)

        route_event = route_events[0]
        self.assertEqual(route_event["data"]["route"]["intent"], "direct_qa")
        self.assertEqual(route_event["data"]["next_action"], "show_direct_answer")
        self.assertIsNone(route_event["data"]["session_id"])
        # The route_ready message must reflect direct_qa (not generic fallback)
        self.assertIn("直接问答", route_event["message"])

    async def test_direct_qa_job_with_history_preserves_context(self):
        """Job with conversation history preserves the answer identity."""
        expected_answer = "复发性阿弗他溃疡的病因包括免疫因素、遗传因素、感染因素等。"
        qa_router = _DirectQARouter({"answer": expected_answer})
        manager = RouterJobManager(qa_router, self._mock_orchestrator())

        history = [
            {"role": "user", "content": "口腔溃疡反复发作是什么原因？"},
            {"role": "assistant", "content": "复发性阿弗他溃疡有多种原因。"},
        ]

        created = manager.create(
            message="具体有哪些原因？",
            user_id="user-dqa-hist",
            history=history,
            context={"user_id": "user-dqa-hist"},
        )
        for _ in range(100):
            snapshot = manager.snapshot(created["job_id"])
            if snapshot["done"]:
                break
            await asyncio.sleep(0.01)

        answer = snapshot["events"][-1]["data"]["router_result"]["final_result"]["answer"]
        self.assertEqual(answer, expected_answer)
        # learning_step is None — no session was created
        self.assertIsNone(snapshot["events"][-1]["data"]["learning_step"])


class MasterRouterDirectQATests(unittest.IsolatedAsyncioTestCase):
    """Tests for MasterRouter direct_qa intent routing.

    These tests exercise MasterRouter directly (not through RouterJobManager)
    using injectable classifier and service mocks.  No LLM or KAG backend is
    called.
    """

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _mock_orchestrator():
        """Return a MagicMock that quacks enough like LearningOrchestrator."""
        orch = MagicMock()
        orch.memory = MagicMock()
        return orch

    @staticmethod
    def _direct_qa_route(**overrides: Any) -> dict[str, Any]:
        """Build a classifier result for the direct_qa intent."""
        return {
            "intent": "direct_qa",
            "plan_item_requested": False,
            "topic": "",
            "reason": "具体口腔医学问题，走直接问答。",
            **overrides,
        }

    @staticmethod
    def _mock_direct_qa_result(**overrides: Any) -> dict[str, Any]:
        """Build a realistic Direct QA service result."""
        return {
            "answer": "牙髓炎的疼痛特点是自发性、阵发性、夜间加重，而根尖周炎为持续性钝痛、咬合痛明显。",
            "resolved_question": "牙髓炎与根尖周炎如何鉴别？",
            "conversation_context_used": False,
            "used_evidence": [
                {"chunk_id": "chunk:001", "source": "textbook", "why_used": "pulpitis pain"},
                {"chunk_id": "chunk:002", "source": "textbook", "why_used": "periapical pain"},
            ],
            "evidence_status": "sufficient",
            "should_reinforce": False,
            "confusion_kg_node_ids": [],
            "trace": {
                "mode": "direct_qa_evidence",
                "uses_llm": True,
                "uses_kag": True,
                "total_seconds": 1.5,
                "user_id": "user-test-1",
            },
            **overrides,
        }

    # ------------------------------------------------------------------
    # direct_qa positive
    # ------------------------------------------------------------------

    async def test_direct_qa_intent_routes_to_service_and_produces_answer(self):
        """Classifier returns direct_qa → service called → answer in final_result."""
        mock_qa_result = self._mock_direct_qa_result()
        direct_qa_service = lambda **kw: mock_qa_result

        router = MasterRouter(
            self._mock_orchestrator(),
            classifier=lambda msg, hist, ctx: self._direct_qa_route(),
            direct_qa_service=direct_qa_service,
        )

        result = await router.route(
            message="牙髓炎与根尖周炎如何鉴别？",
            user_id="user-test-1",
            history=[],
            context={},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["route"]["intent"], "direct_qa")
        self.assertEqual(result["route"]["target"], "direct_qa_service")
        self.assertEqual(result["route"]["source"], "llm_router")
        self.assertEqual(result["next_action"], "show_direct_answer")

        final_result = result["final_result"]
        self.assertIsNotNone(final_result)
        self.assertIn("牙髓炎", final_result["answer"])
        self.assertIn("根尖周炎", final_result["answer"])
        self.assertEqual(final_result, mock_qa_result)

    async def test_direct_qa_no_session_or_outline_in_final_result(self):
        """Direct QA final_result must NOT contain session, outline, or PPT fields."""
        mock_qa_result = self._mock_direct_qa_result()
        direct_qa_service = lambda **kw: mock_qa_result

        router = MasterRouter(
            self._mock_orchestrator(),
            classifier=lambda msg, hist, ctx: self._direct_qa_route(),
            direct_qa_service=direct_qa_service,
        )

        result = await router.route(
            message="什么是龋病的四联因素？",
            user_id="user-test-2",
            history=[],
        )

        final_result = result["final_result"]
        forbidden = {
            "session", "session_id", "outline", "ppt", "pptx", "slides",
        }
        result_keys = set(final_result.keys())
        self.assertTrue(
            result_keys.isdisjoint(forbidden),
            f"final_result contains forbidden keys: {result_keys & forbidden}",
        )

    # ------------------------------------------------------------------
    # session negative — session_id preserves existing learning path
    # ------------------------------------------------------------------

    async def test_session_id_context_preserves_learning_path(self):
        """When session_id is in context, router goes to learning, NOT direct_qa."""
        orch = self._mock_orchestrator()
        orch.continue_to_next_section.return_value = {
            "session_id": "session-existing",
            "outline_node_id": "node-2",
            "teaching_text": "继续学习的内容。",
            "questions": [],
            "lesson_completed": False,
        }

        router = MasterRouter(
            orch,
            # Even if classifier would return direct_qa, session_id takes priority
            classifier=lambda msg, hist, ctx: self._direct_qa_route(),
        )

        result = await router.route(
            message="继续",
            user_id="u1",
            history=[],
            context={"session_id": "session-existing"},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["route"]["intent"], "learning")
        self.assertEqual(result["route"]["source"], "session_context")
        self.assertNotEqual(result["route"]["intent"], "direct_qa")
        # verify continue_to_next_section was called
        orch.continue_to_next_section.assert_called_once()

    # ------------------------------------------------------------------
    # old three intents regression
    # ------------------------------------------------------------------

    async def test_learning_intent_still_routes_correctly(self):
        """learning intent → start_lesson called, session created."""
        orch = self._mock_orchestrator()
        orch.start_lesson.return_value = {
            "session": {"id": "lesson-new", "topic": "牙周炎", "user_id": "u1"},
            "outline": {"title": "牙周炎", "nodes": [{"id": "n1", "title": "病因"}]},
        }

        router = MasterRouter(
            orch,
            classifier=lambda msg, hist, ctx: {
                "intent": "learning",
                "plan_item_requested": False,
                "topic": "牙周炎",
                "reason": "用户想系统学习。",
            },
        )

        result = await router.route(
            message="系统学习牙周炎",
            user_id="u1",
            history=[],
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["route"]["intent"], "learning")
        self.assertEqual(result["route"]["target"], "learning_orchestrator")
        self.assertEqual(result["next_action"], "start_session")
        orch.start_lesson.assert_called_once()

    async def test_study_plan_intent_still_routes_correctly(self):
        """study_plan intent → study_plan_service called."""
        orch = self._mock_orchestrator()
        mock_plan_result = {
            "recommendations": [{"title": "牙周炎基础", "recommendation_id": "rec-1"}],
        }

        router = MasterRouter(
            orch,
            classifier=lambda msg, hist, ctx: {
                "intent": "study_plan",
                "plan_item_requested": False,
                "topic": "",
                "reason": "用户想看学习计划。",
            },
            study_plan_service=lambda memory, user_id, plan_date: mock_plan_result,
        )

        result = await router.route(
            message="今天学什么",
            user_id="u1",
            history=[],
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["route"]["intent"], "study_plan")
        self.assertEqual(result["route"]["target"], "study_plan")
        self.assertEqual(result["next_action"], "show_recommendations")
        self.assertEqual(result["final_result"], mock_plan_result)

    async def test_literature_intent_still_routes_correctly(self):
        """literature intent → literature_service called."""
        orch = self._mock_orchestrator()
        mock_lit_result = {
            "ok": True,
            "results": [{"title": "牙周炎最新研究进展"}],
        }

        async def literature_service(query: str) -> dict[str, Any]:
            return mock_lit_result

        router = MasterRouter(
            orch,
            classifier=lambda msg, hist, ctx: {
                "intent": "literature",
                "plan_item_requested": False,
                "topic": "",
                "reason": "用户想查文献。",
            },
            literature_service=literature_service,
        )

        result = await router.route(
            message="帮我找牙周炎最新文献",
            user_id="u1",
            history=[],
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["route"]["intent"], "literature")
        self.assertEqual(result["route"]["target"], "frontier_literature")
        self.assertEqual(result["next_action"], "show_literature")
        self.assertEqual(result["final_result"], mock_lit_result)

    # ------------------------------------------------------------------
    # final_result.answer consumption
    # ------------------------------------------------------------------

    async def test_final_result_answer_contains_direct_qa_answer(self):
        """Consumer accesses final_result.answer → gets the service's answer text."""
        mock_qa_result = self._mock_direct_qa_result(
            answer="氟斑牙的治疗方法包括树脂渗透、贴面修复和全冠修复等。"
        )
        direct_qa_service = lambda **kw: mock_qa_result

        router = MasterRouter(
            self._mock_orchestrator(),
            classifier=lambda msg, hist, ctx: self._direct_qa_route(),
            direct_qa_service=direct_qa_service,
        )

        result = await router.route(
            message="氟斑牙怎么治疗？",
            user_id="user-test-3",
            history=[],
        )

        # The consumer reads final_result.answer
        consumer_answer = result["final_result"]["answer"]
        self.assertIn("氟斑牙", consumer_answer)
        self.assertIn("树脂渗透", consumer_answer)
        self.assertEqual(consumer_answer, mock_qa_result["answer"])

    # ------------------------------------------------------------------
    # history passed to Direct QA service
    # ------------------------------------------------------------------

    async def test_direct_qa_receives_history_from_router(self):
        """Router normalizes and passes history to the Direct QA service."""
        captured_kwargs: list[dict] = []

        def direct_qa_service(**kw: Any) -> dict[str, Any]:
            captured_kwargs.append(kw)
            return self._mock_direct_qa_result()

        router = MasterRouter(
            self._mock_orchestrator(),
            classifier=lambda msg, hist, ctx: self._direct_qa_route(),
            direct_qa_service=direct_qa_service,
        )

        history = [
            {"role": "user", "content": "牙髓炎如何诊断？"},
            {"role": "assistant", "content": "牙髓炎的诊断方法包括热诊、叩诊和X线检查。"},
        ]

        await router.route(
            message="那根尖周炎呢？",
            user_id="user-test-4",
            history=history,
        )

        self.assertEqual(len(captured_kwargs), 1)
        self.assertEqual(captured_kwargs[0]["message"], "那根尖周炎呢？")
        self.assertEqual(captured_kwargs[0]["user_id"], "user-test-4")
        # history should be normalized (role + content keys only)
        self.assertEqual(len(captured_kwargs[0]["history"]), 2)
        self.assertEqual(
            captured_kwargs[0]["history"][0],
            {"role": "user", "content": "牙髓炎如何诊断？"},
        )

    # ------------------------------------------------------------------
    # service failure graceful degradation
    # ------------------------------------------------------------------

    async def test_direct_qa_service_exception_returns_graceful_error(self):
        """Direct QA service raises → ok=False with fallback answer text."""
        def failing_service(**kw: Any) -> dict[str, Any]:
            raise RuntimeError("KAG backend unreachable")

        router = MasterRouter(
            self._mock_orchestrator(),
            classifier=lambda msg, hist, ctx: self._direct_qa_route(),
            direct_qa_service=failing_service,
        )

        result = await router.route(
            message="牙齿隐裂怎么处理？",
            user_id="user-test-5",
            history=[],
        )

        self.assertFalse(result["ok"])
        self.assertIn("error", result)
        self.assertIn("KAG backend", result["error"])
        # fallback answer text still present
        self.assertIn("answer", result["final_result"])
        self.assertIn("不可用", result["final_result"]["answer"])
        self.assertEqual(result["next_action"], "show_direct_answer")

    # ------------------------------------------------------------------
    # validation — classifier output
    # ------------------------------------------------------------------

    async def test_injected_classifier_direct_qa_is_validated(self):
        """When injected classifier returns direct_qa, _validate_route accepts it."""
        router = MasterRouter(
            self._mock_orchestrator(),
            classifier=lambda msg, hist, ctx: self._direct_qa_route(),
            direct_qa_service=lambda **kw: self._mock_direct_qa_result(),
        )

        result = await router.route(
            message="口腔白斑的恶变率是多少？",
            user_id="user-test-6",
            history=[],
        )

        self.assertTrue(result["ok"])
        route = result["route"]
        self.assertEqual(route["intent"], "direct_qa")
        self.assertEqual(route["target"], "direct_qa_service")
        self.assertEqual(route["source"], "llm_router")
        self.assertEqual(route["plan_item_requested"], False)

    async def test_direct_qa_with_plan_item_requested_true_is_rejected(self):
        """plan_item_requested=true with direct_qa → rejected by validator → clarification."""
        orch = self._mock_orchestrator()

        router = MasterRouter(
            orch,
            classifier=lambda msg, hist, ctx: {
                "intent": "direct_qa",
                "plan_item_requested": True,
                "topic": "",
                "reason": "invalid combo",
            },
        )

        result = await router.route(
            message="牙周炎怎么治疗？",
            user_id="u1",
            history=[],
        )

        # _validate_route rejects plan_item_requested with non-learning intent
        # → falls back to clarification
        self.assertTrue(result["ok"])
        self.assertEqual(result["route"]["intent"], "clarification")
        self.assertEqual(result["next_action"], "select_plan")

    # ------------------------------------------------------------------
    # two structurally different oral topics (no hardcoded KG IDs / indices)
    # ------------------------------------------------------------------

    async def test_topic_endodontics_direct_qa(self):
        """Endodontics question routes to direct_qa (structurally different topic A)."""
        mock_qa_result = self._mock_direct_qa_result(
            answer="根管治疗中工作长度的测定方法包括X线法和根尖定位仪法。"
        )
        direct_qa_service = lambda **kw: mock_qa_result

        router = MasterRouter(
            self._mock_orchestrator(),
            classifier=lambda msg, hist, ctx: self._direct_qa_route(),
            direct_qa_service=direct_qa_service,
        )

        result = await router.route(
            message="根管治疗中如何测定工作长度？",
            user_id="user-endo",
            history=[],
        )

        self.assertEqual(result["route"]["intent"], "direct_qa")
        self.assertIn("根管治疗", result["final_result"]["answer"])

    async def test_topic_periodontics_direct_qa(self):
        """Periodontics question routes to direct_qa (structurally different topic B)."""
        mock_qa_result = self._mock_direct_qa_result(
            answer="牙周炎的Glickman分类包括I-IV度。"
        )
        direct_qa_service = lambda **kw: mock_qa_result

        router = MasterRouter(
            self._mock_orchestrator(),
            classifier=lambda msg, hist, ctx: self._direct_qa_route(),
            direct_qa_service=direct_qa_service,
        )

        result = await router.route(
            message="牙周炎的分类标准是什么？",
            user_id="user-perio",
            history=[],
        )

        self.assertEqual(result["route"]["intent"], "direct_qa")
        self.assertIn("牙周炎", result["final_result"]["answer"])


if __name__ == "__main__":
    unittest.main()
