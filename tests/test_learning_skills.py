from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from backend.services.learning.skills import (
    LEARN_KNOWLEDGE_POINT_SKILL,
    _record_standalone_topic_interest,
    learning_skill_catalog,
    run_learn_knowledge_point,
)
from backend.services.learning.memory import LearningMemory
from backend.services.router.master_router import MasterRouter


class LearningSkillsTests(unittest.TestCase):
    def test_skill_starts_existing_learning_chain_and_returns_trace(self) -> None:
        orchestrator = MagicMock()
        orchestrator.start_lesson.return_value = {
            "session": {"id": "session-1"},
            "current_outline_node_id": "node-1",
            "first_handout_section_ready": True,
            "slides_resource": {"status": "ready"},
        }
        with patch(
            "backend.services.learning.skills.write_knowledge_profile_event",
            return_value={"written": True},
        ) as profile_write:
            result = run_learn_knowledge_point(orchestrator, user_id="user-1", topic="periodontitis")

        self.assertEqual(result["skill"], LEARN_KNOWLEDGE_POINT_SKILL)
        self.assertEqual(result["skill_trace"][0]["agent"], "planner")
        self.assertEqual(result["skill_trace"][1]["status"], "completed")
        orchestrator.start_lesson.assert_called_once_with(user_id="user-1", topic="periodontitis")
        profile_write.assert_called_once()

    def test_skill_can_deliver_first_section_when_requested(self) -> None:
        orchestrator = MagicMock()
        orchestrator.start_lesson.return_value = {
            "session": {"id": "session-2"},
            "current_outline_node_id": "node-1",
            "first_handout_section_ready": True,
            "slides_resource": {"status": "ready"},
        }
        orchestrator.next_step.return_value = {"section_audio_schedule": {"status": "generating"}}
        with patch(
            "backend.services.learning.skills.write_knowledge_profile_event",
            return_value={"written": True},
        ):
            result = run_learn_knowledge_point(
                orchestrator,
                user_id="user-1",
                topic="periodontitis",
                deliver_first_section=True,
            )

        orchestrator.next_step.assert_called_once_with("session-2")
        self.assertEqual(result["skill_trace"][-2]["agent"], "lecture_script")
        self.assertEqual(result["skill_trace"][-1]["status"], "generating")

    def test_master_router_dispatches_registered_tutor_only(self) -> None:
        orchestrator = MagicMock()
        orchestrator.memory.get_session.return_value = {"user_id": "user-1"}
        orchestrator.next_step.return_value = {"outline_node_id": "node-1"}
        router = MasterRouter(orchestrator)
        result = asyncio.run(router.dispatch_agent("tutor", user_id="user-1", context={"session_id": "session-3"}))

        self.assertTrue(result["ok"])
        self.assertEqual(result["route"]["target"], "tutor")
        orchestrator.next_step.assert_called_once_with("session-3")

    def test_session_conversation_can_directly_dispatch_ppt(self) -> None:
        orchestrator = MagicMock()
        orchestrator.memory.get_session.return_value = {"user_id": "user-1"}
        orchestrator.rebuild_ppt_resources.return_value = {"started": True}
        router = MasterRouter(orchestrator)

        result = asyncio.run(
            router.route(
                "\u53ea\u751f\u6210\u5f53\u524d\u8bfe\u7a0b\u7684PPT",
                user_id="user-1",
                context={"session_id": "session-4"},
            )
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["route"]["target"], "ppt")
        orchestrator.rebuild_ppt_resources.assert_called_once_with("session-4")

    def test_single_agent_request_without_session_asks_for_learning_context(self) -> None:
        orchestrator = MagicMock()
        router = MasterRouter(orchestrator)

        result = asyncio.run(router.route("\u53ea\u751f\u6210PPT", user_id="user-1"))

        self.assertTrue(result["ok"])
        self.assertEqual(result["route"]["intent"], "clarification")
        self.assertEqual(result["route"]["requested_target_agent"], "ppt")

    def test_session_conversation_can_request_numbered_quiz(self) -> None:
        orchestrator = MagicMock()
        orchestrator.memory.get_session.return_value = {"user_id": "user-1"}
        orchestrator.ensure_stage_exam_for_interaction.return_value = {
            "status": "ready",
            "questions": [{"stem": "Question 1"}],
        }
        router = MasterRouter(orchestrator)

        result = asyncio.run(
            router.route(
                "\u7ed9\u6211\u51fa5\u9053\u9898",
                user_id="user-1",
                context={"session_id": "session-5"},
            )
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["route"]["target"], "quiz")
        orchestrator.ensure_stage_exam_for_interaction.assert_called_once_with("session-5")

    def test_standalone_generation_records_interest_without_mastery_or_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                session = memory.create_session(
                    "user-1",
                    "periodontitis",
                    mode="standalone_generation",
                )
                result = _record_standalone_topic_interest(
                    SimpleNamespace(memory=memory),
                    user_id="user-1",
                    session_id=session["id"],
                    topic="periodontitis",
                )
                profile = memory.get_or_create_profile("user-1")
                events = memory.list_recent_events("user-1", session_id=session["id"])

                self.assertTrue(result["written"])
                self.assertFalse(result["mastery_updated"])
                self.assertFalse(result["plan_updated"])
                self.assertEqual(profile["topic_interests"][0]["topic"], "periodontitis")
                self.assertEqual(profile["known_weak_points"], [])
                self.assertEqual(events[-1]["event_type"], "standalone_resource_requested")
            finally:
                memory._conn.close()

    def test_ppt_can_bootstrap_a_standalone_generation_context(self) -> None:
        orchestrator = MagicMock()
        orchestrator.memory.get_session.return_value = {"user_id": "user-1"}
        orchestrator.rebuild_ppt_resources.return_value = {"started": True}
        router = MasterRouter(orchestrator)
        bootstrap = {
            "session": {"id": "standalone-1", "mode": "standalone_generation"},
            "knowledge_profile_write": {"impact": "topic_interest_only"},
        }

        with patch(
            "backend.services.router.master_router.run_learn_knowledge_point",
            return_value=bootstrap,
        ) as run_skill:
            result = asyncio.run(
                router.dispatch_agent(
                    "ppt",
                    message="\u751f\u6210\u7259\u5468\u708ePPT",
                    user_id="user-1",
                )
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["final_result"]["standalone_generation"])
        self.assertEqual(result["route"]["topic"], "\u7259\u5468\u708e")
        self.assertEqual(run_skill.call_args.kwargs["session_mode"], "standalone_generation")
        orchestrator.rebuild_ppt_resources.assert_called_once_with("standalone-1")

    def test_catalog_exposes_workflow_and_leaf_agents(self) -> None:
        names = {item["name"] for item in learning_skill_catalog()}
        self.assertIn(LEARN_KNOWLEDGE_POINT_SKILL, names)
        self.assertTrue({"planner", "handout", "tutor", "qa", "assessment"}.issubset(names))


if __name__ == "__main__":
    unittest.main()
