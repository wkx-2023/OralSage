from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from backend.services.router.master_router import MasterRouter


class MasterRouterSessionPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_session_question_is_dispatched_to_qa(self):
        orchestrator = MagicMock()
        orchestrator.memory.get_session.return_value = {
            "id": "lesson-1",
            "user_id": "u1",
            "topic": "牙周炎",
        }
        orchestrator.get_policy_state.return_value = {
            "session_id": "lesson-1",
            "user_id": "u1",
            "current_outline_node_id": "node-1",
        }
        orchestrator.execute_policy_action.return_value = {
            "executed": True,
            "result": {"answer": "这是基于本节证据的回答。"},
        }
        decision = {
            "schema_version": "policy.v1",
            "action_type": "agent_call",
            "action": "answer_interruption",
            "target_agent": "qa",
            "payload": {"input_type": "interruption_question", "text": "为什么"},
            "reason": "用户在当前课程中提问。",
            "requires_user_input": False,
            "advance_progress_allowed": False,
            "trace": {"mode": "llm_orchestrator_policy", "uses_llm": True},
        }
        router = MasterRouter(orchestrator)

        with patch(
            "backend.services.router.master_router.decide_next_action_with_llm",
            return_value=decision,
        ):
            result = await router.route(
                "牙周袋为什么会加深？",
                user_id="u1",
                context={"session_id": "lesson-1"},
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["route"]["source"], "learning_session_policy")
        self.assertEqual(result["route"]["target"], "qa")
        self.assertEqual(result["final_result"]["answer"], "这是基于本节证据的回答。")
        execution_input = orchestrator.execute_policy_action.call_args.args[2]
        self.assertEqual(execution_input["type"], "interruption_question")
        self.assertEqual(execution_input["text"], "牙周袋为什么会加深？")

    async def test_existing_session_is_rejected_for_another_user(self):
        orchestrator = MagicMock()
        orchestrator.memory.get_session.return_value = {
            "id": "lesson-1",
            "user_id": "owner",
            "topic": "牙周炎",
        }
        router = MasterRouter(orchestrator)

        result = await router.route(
            "解释一下",
            user_id="other",
            context={"session_id": "lesson-1"},
        )

        self.assertFalse(result["ok"])
        self.assertIn("access denied", result["error"].lower())
        orchestrator.execute_policy_action.assert_not_called()


if __name__ == "__main__":
    unittest.main()
