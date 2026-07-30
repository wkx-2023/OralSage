from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from backend.services.learning.memory import LearningMemory
from backend.services.learning.spaced_repetition import (
    build_spaced_review_schedule,
    sm2_next_state,
)
from backend.services.learning.study_plan import (
    create_study_plan_item,
    recommend_study_plan,
)


class SM2StateTests(unittest.TestCase):
    def test_failed_review_resets_repetition_and_returns_tomorrow(self) -> None:
        state = sm2_next_state(
            quality=2,
            repetition_count=4,
            ease_factor=2.5,
            previous_interval_days=16,
        )

        self.assertEqual(state["quality"], 2)
        self.assertEqual(state["repetition_count"], 0)
        self.assertEqual(state["interval_days"], 1)
        self.assertGreaterEqual(state["ease_factor"], 1.3)

    def test_successful_mature_review_expands_interval(self) -> None:
        state = sm2_next_state(
            quality=5,
            repetition_count=2,
            ease_factor=2.5,
            previous_interval_days=6,
        )

        self.assertEqual(state["quality"], 5)
        self.assertEqual(state["repetition_count"], 3)
        self.assertGreater(state["ease_factor"], 2.5)
        self.assertGreater(state["interval_days"], 6)


class SpacedReviewScheduleTests(unittest.TestCase):
    def test_builds_due_kg_node_review_from_weakness_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                user_id = "spaced-user"
                kg_node_id = "core:test-weakness"
                memory.save_learner_memory_slot(
                    user_id,
                    "weaknesses",
                    {
                        "items": [
                            {
                                "label": "periodontitis differential",
                                "kg_node_ids": [kg_node_id],
                                "severity": 0.9,
                                "confidence": 0.8,
                                "updated_at": (
                                    date.today() - timedelta(days=3)
                                ).isoformat(),
                            }
                        ]
                    },
                )

                schedule = build_spaced_review_schedule(
                    memory,
                    user_id=user_id,
                    target_date=date.today(),
                    max_items=3,
                )

                self.assertTrue(schedule)
                first = schedule[0]
                self.assertEqual(first["kg_node_id"], kg_node_id)
                self.assertGreater(first["priority"], 0)
                self.assertGreaterEqual(first["overdue_days"], 1)
                self.assertEqual(first["trace"]["algorithm"], "sm2_kg_node_v1")
                self.assertIn("learner_memory_weakness", first["evidence_sources"])
            finally:
                memory._conn.close()

    def test_study_plan_recommends_review_section_with_sm2_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                user_id = "plan-spaced-user"
                kg_node_id = "core:test-plan-weakness"
                memory.save_learner_memory_slot(
                    user_id,
                    "weaknesses",
                    {
                        "items": [
                            {
                                "label": "root canal indications",
                                "kg_node_ids": [kg_node_id],
                                "severity": 0.85,
                                "confidence": 0.75,
                                "updated_at": (
                                    date.today() - timedelta(days=4)
                                ).isoformat(),
                            }
                        ]
                    },
                )

                result = recommend_study_plan(
                    memory,
                    user_id=user_id,
                    plan_date=date.today().isoformat(),
                    max_items=4,
                )

                review = next(
                    item
                    for item in result["recommendations"]
                    if item.get("category") == "spaced_review"
                )
                self.assertEqual(review["task_type"], "review_section")
                self.assertEqual(review["kg_node_ids"], [kg_node_id])
                self.assertEqual(
                    review["learning_scope"]["scope_type"],
                    "spaced_review_kg_node",
                )
                self.assertEqual(
                    review["learning_scope"]["spaced_repetition"]["algorithm"],
                    "sm2_kg_node_v1",
                )
                self.assertEqual(result["trace"]["spaced_review_count"], 1)

                created = create_study_plan_item(
                    memory,
                    user_id=user_id,
                    plan_date=date.today().isoformat(),
                    title=review["title"],
                    notes=review.get("notes"),
                    task_type=review["task_type"],
                    learning_target=review.get("learning_target"),
                    kg_node_ids=review.get("kg_node_ids"),
                    learning_scope=review.get("learning_scope"),
                )["item"]
                self.assertEqual(created["task_type"], "review_section")
                self.assertEqual(created["kg_node_ids"], [kg_node_id])
                self.assertEqual(
                    created["learning_scope"]["scope_type"],
                    "spaced_review_kg_node",
                )
            finally:
                memory._conn.close()


if __name__ == "__main__":
    unittest.main()
