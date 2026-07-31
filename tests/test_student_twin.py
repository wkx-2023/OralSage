from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from backend.main import app, learning_orchestrator
from backend.services.learning.memory import LearningMemory
from backend.services.learning.student_twin import build_student_twin_simulation


class StudentTwinSimulationTests(unittest.TestCase):
    def _memory_with_weakness(self, tmp: str) -> LearningMemory:
        memory = LearningMemory(Path(tmp) / "learning.sqlite3")
        memory.save_learner_memory_slot(
            "twin-user",
            "weaknesses",
            {
                "items": [
                    {
                        "label": "periodontitis differential diagnosis",
                        "kg_node_ids": ["core:periodontitis-diff"],
                        "severity": 0.9,
                        "confidence": 0.82,
                        "updated_at": (date.today() - timedelta(days=4)).isoformat(),
                    },
                    {
                        "label": "alveolar bone resorption mechanism",
                        "kg_node_ids": ["core:bone-resorption"],
                        "severity": 0.78,
                        "confidence": 0.76,
                        "updated_at": (date.today() - timedelta(days=5)).isoformat(),
                    },
                ]
            },
        )
        return memory

    def test_builds_bottlenecks_paths_and_plan_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = self._memory_with_weakness(tmp)
            try:
                result = build_student_twin_simulation(
                    memory,
                    user_id="twin-user",
                    plan_date=date.today(),
                    available_minutes=20,
                    max_bottlenecks=4,
                )

                self.assertEqual(result["agent"], "StudentTwinAgent")
                self.assertEqual(result["schema_version"], "student_twin.simulation.v1")
                self.assertGreaterEqual(result["state"]["bottleneck_count"], 2)
                self.assertTrue(result["bottlenecks"])
                bottleneck_labels = {item["label"] for item in result["bottlenecks"]}
                self.assertIn("牙周炎鉴别诊断", bottleneck_labels)
                self.assertIn("牙槽骨吸收机制", bottleneck_labels)
                self.assertTrue(result["paths"])
                self.assertEqual(result["paths"][0]["path_id"], result["recommended_path_id"])
                self.assertTrue(result["paths"][0]["recommended"])
                self.assertNotIn("核心薄弱点", result["paths"][0]["steps"][0]["learning_target"])
                self.assertIn("kg_node_names", result["paths"][0]["steps"][0])
                continue_path = next(
                    path for path in result["paths"]
                    if path["path_id"] == "continue_new_lesson"
                )
                self.assertNotEqual(
                    continue_path["steps"][0]["learning_target"],
                    result["bottlenecks"][0]["label"],
                )
                self.assertTrue(continue_path["steps"][0].get("monitoring_kg_node"))
                self.assertTrue(result["recommended_plan_items"])
                self.assertEqual(
                    result["recommended_plan_items"][0]["category"],
                    "student_twin",
                )
                self.assertEqual(
                    result["recommended_plan_items"][0]["learning_scope"]["scope_type"],
                    "student_twin_simulation",
                )
                self.assertIn("kg_node_names", result["recommended_plan_items"][0]["learning_scope"])
                self.assertIn("dashboard", result)
                self.assertTrue(result["dashboard"]["path_simulation"]["paths"])
                self.assertTrue(result["dashboard"]["mastery_forecast"]["series"])
                self.assertTrue(result["dashboard"]["cognitive_map"]["nodes"])
                self.assertTrue(result["dashboard"]["optimal_strategy"]["steps"])
                self.assertIn("path_score_formula", result["trace"])
            finally:
                memory._conn.close()

    def test_student_twin_api_returns_simulation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = self._memory_with_weakness(tmp)
            original_memory = learning_orchestrator.memory
            learning_orchestrator.memory = memory
            try:
                client = TestClient(app)
                resp = client.get(
                    "/api/learning/student-twin/twin-user",
                    params={
                        "plan_date": date.today().isoformat(),
                        "available_minutes": 20,
                    },
                )

                self.assertEqual(resp.status_code, 200)
                body = resp.json()
                self.assertTrue(body["ok"])
                self.assertEqual(body["action"], "student_twin")
                self.assertEqual(body["result"]["agent"], "StudentTwinAgent")
                self.assertTrue(body["result"]["bottlenecks"])
                self.assertTrue(body["result"]["dashboard"]["cognitive_map"]["nodes"])
            finally:
                learning_orchestrator.memory = original_memory
                memory._conn.close()

    def test_cold_start_uses_concrete_kg_concepts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                result = build_student_twin_simulation(
                    memory,
                    user_id="empty-user",
                    plan_date=date.today(),
                    available_minutes=20,
                    max_bottlenecks=4,
                )

                self.assertGreaterEqual(result["state"]["bottleneck_count"], 3)
                labels = [item["label"] for item in result["bottlenecks"]]
                self.assertTrue(any("牙" in label or "龋" in label for label in labels))
                for path in result["paths"]:
                    for step in path["steps"]:
                        self.assertNotIn("核心薄弱点", step["learning_target"])
                        self.assertNotIn("当前课程进度", step["learning_target"])
                        self.assertTrue(step.get("kg_node_names"))
                continue_path = next(
                    path for path in result["paths"]
                    if path["path_id"] == "continue_new_lesson"
                )
                self.assertNotEqual(
                    continue_path["steps"][0]["learning_target"],
                    result["bottlenecks"][0]["label"],
                )
            finally:
                memory._conn.close()


if __name__ == "__main__":
    unittest.main()
