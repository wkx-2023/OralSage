"""Tests for LearningMemory study-plan query methods — no external dependencies."""

from __future__ import annotations

import tempfile
import unittest
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from backend.services.learning.memory import LearningMemory
from backend.services.learning.study_plan import _recommend_weakness_reviews


class StudyPlanTaskSemanticsTests(unittest.TestCase):
    def test_weakness_recommendation_separates_course_and_target(self) -> None:
        recommendations = _recommend_weakness_reviews(
            profile={},
            learner_memory={
                "weaknesses": {
                    "items": [
                        {
                            "label": "宿主-微生物互作机制理解缺失",
                            "severity": 0.9,
                            "kg_node_ids": ["kg-host-microbe"],
                        }
                    ]
                }
            },
            active_session={"topic": "我想学习口腔溃疡"},
        )

        self.assertEqual(len(recommendations), 1)
        item = recommendations[0]
        self.assertEqual(item["task_type"], "review_weakness")
        self.assertEqual(item["learning_target"], "宿主-微生物互作机制理解缺失")
        self.assertEqual(item["linked_course_title"], "口腔溃疡")

    def test_existing_database_is_migrated_with_task_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "learning.sqlite3"
            conn = sqlite3.connect(db_path)
            conn.execute(
                """CREATE TABLE study_plan_items (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    plan_date TEXT NOT NULL,
                    title TEXT NOT NULL,
                    notes TEXT,
                    status TEXT NOT NULL DEFAULT 'open',
                    linked_course_id TEXT,
                    linked_course_title TEXT,
                    kg_node_ids_json TEXT NOT NULL,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    deferred_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                )"""
            )
            conn.commit()
            conn.close()

            memory = LearningMemory(db_path)
            try:
                columns = {
                    row["name"]
                    for row in memory._conn.execute("PRAGMA table_info(study_plan_items)").fetchall()
                }
                self.assertIn("task_type", columns)
                self.assertIn("learning_target", columns)
            finally:
                memory._conn.close()

    def test_task_fields_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                item = memory.create_study_plan_item(
                    user_id="u1",
                    plan_date=date.today().isoformat(),
                    title="复习薄弱点：牙周组织炎症",
                    task_type="review_weakness",
                    learning_target="牙周组织炎症",
                )
                self.assertEqual(item["task_type"], "review_weakness")
                self.assertEqual(item["learning_target"], "牙周组织炎症")
            finally:
                memory._conn.close()


class ListOpenStudyPlanItemsTests(unittest.TestCase):
    """Coverage for LearningMemory.list_open_study_plan_items."""

    def test_empty_when_no_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = LearningMemory(root / "learning.sqlite3")
            try:
                result = memory.list_open_study_plan_items("u1")
                self.assertEqual(result, [])
            finally:
                memory._conn.close()

    def test_returns_only_open_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = LearningMemory(root / "learning.sqlite3")
            try:
                today = date.today().isoformat()
                memory.create_study_plan_item(
                    user_id="u1", plan_date=today, title="Open Alpha"
                )
                memory.create_study_plan_item(
                    user_id="u1", plan_date=today, title="Open Beta"
                )
                memory.create_study_plan_item(
                    user_id="u1", plan_date=today, title="Completed Gamma"
                )
                # Mark the last one completed
                items = memory.list_study_plan_items("u1", today, today)
                for item in items:
                    if item["title"] == "Completed Gamma":
                        memory.set_study_plan_item_completion(
                            item["id"], user_id="u1", completed=True
                        )
                        break

                result = memory.list_open_study_plan_items("u1")
                titles = [item["title"] for item in result]
                self.assertEqual(len(result), 2)
                self.assertIn("Open Alpha", titles)
                self.assertIn("Open Beta", titles)
                self.assertNotIn("Completed Gamma", titles)
            finally:
                memory._conn.close()

    def test_excludes_other_users(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = LearningMemory(root / "learning.sqlite3")
            try:
                today = date.today().isoformat()
                memory.create_study_plan_item(
                    user_id="u1", plan_date=today, title="U1 Item"
                )
                memory.create_study_plan_item(
                    user_id="u2", plan_date=today, title="U2 Item"
                )

                result = memory.list_open_study_plan_items("u1")
                titles = [item["title"] for item in result]
                self.assertEqual(len(result), 1)
                self.assertIn("U1 Item", titles)
                self.assertNotIn("U2 Item", titles)
            finally:
                memory._conn.close()

    def test_respects_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = LearningMemory(root / "learning.sqlite3")
            try:
                today = date.today().isoformat()
                for idx in range(5):
                    memory.create_study_plan_item(
                        user_id="u1",
                        plan_date=today,
                        title=f"Item {idx:02d}",
                    )

                full = memory.list_open_study_plan_items("u1")
                self.assertEqual(len(full), 5)

                limited = memory.list_open_study_plan_items("u1", limit=2)
                self.assertEqual(len(limited), 2)
            finally:
                memory._conn.close()

    def test_sort_order_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = LearningMemory(root / "learning.sqlite3")
            try:
                today = date.today().isoformat()
                yesterday = (date.today() - timedelta(days=1)).isoformat()
                tomorrow = (date.today() + timedelta(days=1)).isoformat()

                # Items across different dates to verify plan_date ordering
                memory.create_study_plan_item(
                    user_id="u1", plan_date=today, title="Today Item"
                )
                memory.create_study_plan_item(
                    user_id="u1", plan_date=yesterday, title="Yesterday Item"
                )
                memory.create_study_plan_item(
                    user_id="u1", plan_date=tomorrow, title="Tomorrow Item"
                )

                result = memory.list_open_study_plan_items("u1")
                titles = [item["title"] for item in result]
                # Expect: yesterday, today, tomorrow (plan_date ASC)
                self.assertEqual(titles, ["Yesterday Item", "Today Item", "Tomorrow Item"])
            finally:
                memory._conn.close()

    def test_includes_past_today_and_future_open_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = LearningMemory(root / "learning.sqlite3")
            try:
                past = (date.today() - timedelta(days=3)).isoformat()
                today = date.today().isoformat()
                future = (date.today() + timedelta(days=3)).isoformat()

                memory.create_study_plan_item(
                    user_id="u1", plan_date=past, title="Past Open"
                )
                memory.create_study_plan_item(
                    user_id="u1", plan_date=today, title="Today Open"
                )
                memory.create_study_plan_item(
                    user_id="u1", plan_date=future, title="Future Open"
                )

                result = memory.list_open_study_plan_items("u1")
                titles = [item["title"] for item in result]
                self.assertEqual(len(result), 3)
                self.assertIn("Past Open", titles)
                self.assertIn("Today Open", titles)
                self.assertIn("Future Open", titles)
            finally:
                memory._conn.close()


if __name__ == "__main__":
    unittest.main()
