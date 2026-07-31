from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.services.learning.knowledge_tracing import (
    bkt_observe,
    profile_mastery_patch_from_states,
    update_bkt_state,
)
from backend.services.learning.memory import LearningMemory
from backend.services.learning.orchestrator import LearningOrchestrator
from backend.services.learning.spaced_repetition import build_spaced_review_schedule


class BKTModelTests(unittest.TestCase):
    def test_correct_observation_increases_mastery_probability(self) -> None:
        result = bkt_observe(0.30, is_correct=True)

        self.assertEqual(result["model"], "bkt_kg_node_v1")
        self.assertGreater(result["p_mastery"], 0.30)
        self.assertLess(result["uncertainty"], 1.0)
        self.assertEqual(result["observation"], "correct")

    def test_incorrect_observation_decreases_high_mastery_probability(self) -> None:
        result = bkt_observe(0.80, is_correct=False)

        self.assertLess(result["p_mastery"], 0.80)
        self.assertEqual(result["observation"], "incorrect")

    def test_profile_patch_preserves_probability_and_uncertainty(self) -> None:
        state = update_bkt_state(
            None,
            kg_node_id="core:perio-pocket",
            skill_label="牙周袋形成机制",
            is_correct=True,
            score=1.0,
            source_ref="q-1",
        )

        patch = profile_mastery_patch_from_states([state])

        self.assertIn("core:perio-pocket", patch)
        item = patch["core:perio-pocket"]
        self.assertEqual(item["model"], "bkt_kg_node_v1")
        self.assertEqual(item["score"], state["p_mastery"])
        self.assertEqual(item["uncertainty"], state["uncertainty"])


class KnowledgeTracingPersistenceTests(unittest.TestCase):
    def test_state_round_trips_through_learning_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                state = update_bkt_state(
                    None,
                    kg_node_id="core:pulpitis-diagnosis",
                    skill_label="牙髓炎诊断",
                    is_correct=False,
                    source="assessment",
                    source_ref="q-2",
                )
                saved = memory.save_knowledge_trace_state(
                    "user-kt",
                    "core:pulpitis-diagnosis",
                    state,
                )
                loaded = memory.get_knowledge_trace_state(
                    "user-kt",
                    "core:pulpitis-diagnosis",
                )

                self.assertIsNotNone(loaded)
                self.assertEqual(saved["p_mastery"], loaded["p_mastery"])
                self.assertEqual(loaded["parameters"]["p_guess"], 0.2)
                self.assertFalse(loaded["last_observation_correct"])
                self.assertEqual(loaded["trace"]["model"], "bkt_kg_node_v1")
            finally:
                memory._conn.close()

    def test_spaced_repetition_can_use_knowledge_tracing_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                state = update_bkt_state(
                    None,
                    kg_node_id="core:low-bkt",
                    skill_label="低掌握 BKT 节点",
                    is_correct=False,
                    source_ref="q-low",
                )
                memory.save_knowledge_trace_state("user-bkt-review", "core:low-bkt", state)

                schedule = build_spaced_review_schedule(
                    memory,
                    user_id="user-bkt-review",
                    target_date="2026-07-30",
                    max_items=3,
                )

                self.assertTrue(schedule)
                self.assertEqual(schedule[0]["kg_node_id"], "core:low-bkt")
                self.assertIn("knowledge_tracing_state", schedule[0]["evidence_sources"])
            finally:
                memory._conn.close()


class KnowledgeTracingOrchestratorTests(unittest.TestCase):
    def test_submit_answer_updates_bkt_state_and_compatible_mastery_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            orchestrator = LearningOrchestrator(memory=memory, resource_root=Path(tmp) / "sessions")
            try:
                user_id = "user-answer-bkt"
                session = memory.create_session(user_id, "牙周炎")
                outline = {
                    "nodes": [
                        {
                            "id": "node-1",
                            "title": "牙周袋形成机制",
                            "kg_node_ids": ["core:perio-pocket"],
                        },
                        {
                            "id": "node-2",
                            "title": "牙槽骨吸收进展",
                            "kg_node_ids": ["core:bone-loss"],
                        },
                    ]
                }
                memory.save_outline(session["id"], outline, {}, {})
                memory.set_current_outline_node(session["id"], "node-1")

                result = orchestrator.submit_answer(
                    session["id"],
                    {
                        "id": "q-perio-pocket",
                        "type": "single_choice",
                        "stem": "牙周袋形成主要与哪项有关？",
                        "answer": "A",
                        "options": ["A. 附着丧失", "B. 釉质发育"],
                        "kg_node_ids": ["core:perio-pocket"],
                    },
                    "A",
                )

                kt_updates = result["knowledge_tracing_updates"]
                self.assertEqual(len(kt_updates), 1)
                self.assertGreater(kt_updates[0]["p_mastery"], 0.30)
                self.assertLess(kt_updates[0]["uncertainty"], 1.0)

                kt_state = memory.get_knowledge_trace_state(user_id, "core:perio-pocket")
                self.assertIsNotNone(kt_state)
                self.assertEqual(kt_state["evidence_count"], 1)
                self.assertTrue(kt_state["last_observation_correct"])

                mastery = memory.get_mastery_by_kg_node(user_id, session["id"])
                self.assertAlmostEqual(
                    mastery["core:perio-pocket"]["mastery_score"],
                    kt_state["p_mastery"],
                    places=3,
                )
                self.assertEqual(
                    mastery["core:perio-pocket"]["evidence"]["knowledge_tracing"]["model"],
                    "bkt_kg_node_v1",
                )

                profile = memory.get_profile(user_id)
                self.assertIsNotNone(profile)
                node_summary = profile["mastery_by_kg_node"]["core:perio-pocket"]
                self.assertEqual(node_summary["score"], kt_state["p_mastery"])
                self.assertEqual(node_summary["uncertainty"], kt_state["uncertainty"])
            finally:
                memory._conn.close()


if __name__ == "__main__":
    unittest.main()
