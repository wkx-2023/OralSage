"""Characterization tests for assessment and behavior profile fusion."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from backend.services.learning.memory import LearningMemory
from backend.services.learning.profile_memory import (
    refresh_profile_state_from_l3,
    update_learner_memory_from_event,
)


class LearningProfileFusionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.memory = LearningMemory(Path(self._tmp.name) / "learning.sqlite3")
        self.user_id = "profile-fusion-user"

    def tearDown(self) -> None:
        self.memory._conn.close()
        self._tmp.cleanup()

    def _record_events(self, counts: tuple[int, ...], event_type: str = "user_interruption_question") -> list[str]:
        event_ids: list[str] = []
        for session_index, count in enumerate(counts):
            session = self.memory.create_session(self.user_id, f"topic-{session_index}")
            for event_index in range(count):
                event_ids.append(
                    self.memory.record_event(
                        session_id=session["id"],
                        user_id=self.user_id,
                        event_type=event_type,
                        outline_node_id=None,
                        payload={"question": f"question-{session_index}-{event_index}"},
                    )
                )
        return event_ids

    def _save_style_signal(self, label: str, evidence_ids: list[str]) -> None:
        self.memory.save_learner_memory_slot(
            self.user_id,
            "profile",
            {
                "items": [
                    {
                        "id": "mem_profile_style",
                        "slot": "profile",
                        "aspect": "learning_style_tendency",
                        "label": label,
                        "text": f"用户呈现稳定的{label}倾向。",
                        "confidence": 0.82,
                        "evidence_ids": evidence_ids,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                ]
            },
        )

    def _save_assessment(self, learner_type: str, summary: str) -> None:
        profile = self.memory.get_or_create_profile(self.user_id)
        profile.update(
            {
                "learner_type": learner_type,
                "learner_type_summary": summary,
                "assessment_source": "learning_onboarding_v1:v1",
                "last_assessed_at": datetime.now(timezone.utc).isoformat(),
                "confidence": 0.72,
            }
        )
        self.memory.save_profile(self.user_id, profile)

    def test_behavior_builds_profile_without_assessment(self) -> None:
        evidence_ids = self._record_events((2,))
        self._save_style_signal("练习驱动型学习者", evidence_ids)

        state = refresh_profile_state_from_l3(self.memory, self.user_id)

        self.assertEqual(state["assessed_style"]["type"], "")
        self.assertEqual(state["observed_style"]["type"], "practice_driven_learner")
        self.assertEqual(state["observed_style"]["status"], "emerging")
        self.assertEqual(state["effective_style"]["type"], "practice_driven_learner")
        self.assertEqual(state["effective_style"]["source"], "learning_behavior")

    def test_historical_retrieval_label_is_normalized(self) -> None:
        evidence_ids = self._record_events((1,), event_type="answer_submitted")
        self._save_style_signal("检索增强型学习者", evidence_ids)

        state = refresh_profile_state_from_l3(self.memory, self.user_id)

        self.assertEqual(state["observed_style"]["type"], "practice_driven_learner")
        self.assertEqual(state["observed_style"]["status"], "forming")

    def test_assessment_is_immediate_baseline(self) -> None:
        self._save_assessment("case_driven_learner", "测评显示更适合从病例进入。")

        state = refresh_profile_state_from_l3(self.memory, self.user_id)

        self.assertEqual(state["assessed_style"]["type"], "case_driven_learner")
        self.assertEqual(state["effective_style"]["type"], "case_driven_learner")
        self.assertEqual(state["effective_style"]["source"], "assessment")

    def test_matching_behavior_fuses_with_assessment(self) -> None:
        self._save_assessment("practice_driven_learner", "测评显示更适合边做边学。")
        evidence_ids = self._record_events((2, 2))
        self._save_style_signal("练习驱动型学习者", evidence_ids)

        state = refresh_profile_state_from_l3(self.memory, self.user_id)

        self.assertEqual(state["effective_style"]["type"], "practice_driven_learner")
        self.assertEqual(state["effective_style"]["source"], "fused")
        self.assertGreater(state["effective_style"]["confidence"], 0.72)

    def test_stable_behavior_updates_effective_style_without_overwriting_assessment(self) -> None:
        self._save_assessment("case_driven_learner", "测评显示更适合从病例进入。")
        evidence_ids = self._record_events((2, 2))
        self._save_style_signal("练习驱动型学习者", evidence_ids)

        first = refresh_profile_state_from_l3(self.memory, self.user_id)
        repeated = refresh_profile_state_from_l3(self.memory, self.user_id)

        self.assertEqual(first["effective_style"]["type"], "case_driven_learner")
        self.assertEqual(first["effective_style"]["status"], "calibrating")
        self.assertEqual(repeated["effective_style"]["transition"]["confirmations"], 1)
        self.assertEqual(repeated["updated_at"], first["updated_at"])

        session = self.memory.list_sessions_for_user(self.user_id, limit=1)[0]
        evidence_ids.append(
            self.memory.record_event(
                session_id=session["id"],
                user_id=self.user_id,
                event_type="answer_submitted",
                outline_node_id=None,
                payload={"score": 0.8},
            )
        )
        self._save_style_signal("练习驱动型学习者", evidence_ids)
        updated = refresh_profile_state_from_l3(self.memory, self.user_id)

        self.assertEqual(updated["effective_style"]["type"], "practice_driven_learner")
        self.assertEqual(updated["effective_style"]["source"], "learning_behavior")
        self.assertEqual(self.memory.get_profile(self.user_id)["learner_type"], "case_driven_learner")

    def test_system_generated_events_do_not_define_learning_style(self) -> None:
        evidence_ids = self._record_events((4,), event_type="tutor_step_generated")
        self._save_style_signal("图解结构型学习者", evidence_ids)

        state = refresh_profile_state_from_l3(self.memory, self.user_id)

        self.assertEqual(state["observed_style"]["type"], "")
        self.assertEqual(state["effective_style"]["type"], "")

    def test_realtime_rule_fallback_works_without_llm(self) -> None:
        session = self.memory.create_session(self.user_id, "fallback-topic")
        event_id = self.memory.record_event(
            session_id=session["id"],
            user_id=self.user_id,
            event_type="user_interruption_question",
            outline_node_id=None,
            payload={"question": "为什么会出现这个机制？请详细解释原理。"},
        )

        with patch(
            "backend.services.learning.profile_memory.is_learning_memory_llm_configured",
            return_value=False,
        ):
            result = update_learner_memory_from_event(
                self.memory,
                user_id=self.user_id,
                session_id=session["id"],
                event_id=event_id,
                surface="qa",
            )

        self.assertTrue(result["updated"])
        self.assertEqual(result["trace"]["extractor_status"], "fallback_no_llm")
        state = result["reduction"]["profile_state"]
        self.assertEqual(state["observed_style"]["type"], "text_depth_learner")
        self.assertEqual(state["effective_style"]["source"], "learning_behavior")


if __name__ == "__main__":
    unittest.main()
