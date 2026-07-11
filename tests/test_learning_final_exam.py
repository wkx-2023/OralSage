from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from backend.services.learning.final_exam import (
    build_and_save_final_exam,
    final_exam_submission_path,
    submit_final_exam,
)
from backend.services.learning.memory import LearningMemory
from backend.services.learning.orchestrator import LearningOrchestrator


def _write_fixture_catalog(root: Path, *, complete: bool = True) -> tuple[Path, Path]:
    bank_path = root / "question_bank.json"
    kag_dir = root / "kag"
    kag_dir.mkdir(parents=True)
    records = []
    kag_questions = []
    for question_id, kg_id in (
        ("q_kg_1_a", "kg://course/core_1"),
        ("q_kg_1_b", "kg://course/core_1"),
        ("q_kg_2", "kg://course/core_2"),
        ("q_kg_3", "kg://course/core_3"),
    ):
        records.append({
            "id": question_id,
            "subject_name": "牙周病学",
            "chapter_name": "测试章节",
            "stem": f"{question_id} 的题干",
            "options": (
                {"A": "选项一", "B": "选项二", "C": "选项三"}
                if complete or question_id != "q_kg_1_a"
                else {}
            ),
            "answer": "B",
            "explanation": f"{question_id} 的解析",
            "primary_knowledge_point": f"知识点 {kg_id}",
        })
        kag_questions.append({
            "question_id": f"question:{question_id}",
            "source_question_id": question_id,
            "primary_node_id": kg_id,
            "primary_knowledge_point": f"知识点 {kg_id}",
            "match_status": "linked",
            "verification_status": "linked",
        })
    bank_path.write_text(
        json.dumps({"records": records}, ensure_ascii=False),
        encoding="utf-8",
    )
    (kag_dir / "questions.jsonl").write_text(
        "\n".join(
            json.dumps(question, ensure_ascii=False)
            for question in kag_questions
        )
        + "\n",
        encoding="utf-8",
    )
    return bank_path, kag_dir


def _outline() -> dict:
    return {
        "title": "牙周炎课程",
        "nodes": [
            {"id": "node_1", "kg_node_ids": ["kg://course/core_1"]},
            {"id": "node_2", "kg_node_ids": ["kg://course/core_2"]},
            {"id": "node_3", "kg_node_ids": ["kg://course/core_3"]},
        ],
    }


def _summary() -> dict:
    return {
        "events": [{
            "event_type": "answer_submitted",
            "payload": {
                "is_correct": False,
                "kg_node_ids": ["kg://course/core_2"],
            },
        }],
        "mastery": [
            {"kg_node_id": "kg://course/core_1", "mastery_score": 0.85},
            {"kg_node_id": "kg://course/core_2", "mastery_score": 0.2},
            {"kg_node_id": "kg://course/core_3", "mastery_score": 0.7},
        ],
        "learner_memory": {
            "weaknesses": {
                "items": [{"kg_node_ids": ["kg://course/core_3"]}],
            },
        },
        "profile": {},
    }


class FinalExamTests(unittest.TestCase):
    def test_unassessed_knowledge_point_is_coverage_not_low_mastery(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bank_path, kag_dir = _write_fixture_catalog(root)
            result = build_and_save_final_exam(
                session_id="unassessed-session",
                user_id="user",
                topic="牙周炎",
                outline={
                    "nodes": [{
                        "id": "node_1",
                        "kg_node_ids": ["kg://course/core_1"],
                    }],
                },
                session_summary={"events": [], "mastery": []},
                resource_root=root / "resources",
                question_bank_path=bank_path,
                kag_dir=kag_dir,
                question_count=1,
            )

            self.assertEqual(
                result["questions"][0]["selection_reason"],
                "course_coverage",
            )
            self.assertEqual(result["coverage"]["low_mastery_kg_node_ids"], [])

    def test_exam_prioritizes_weaknesses_and_covers_course_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bank_path, kag_dir = _write_fixture_catalog(root)
            result = build_and_save_final_exam(
                session_id="session",
                user_id="user",
                topic="牙周炎",
                outline=_outline(),
                session_summary=_summary(),
                resource_root=root / "resources",
                question_bank_path=bank_path,
                kag_dir=kag_dir,
                question_count=3,
            )

            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["question_count"], 3)
            self.assertEqual(result["coverage"]["coverage_ratio"], 1.0)
            self.assertEqual(
                set(result["coverage"]["covered_kg_node_ids"]),
                {
                    "kg://course/core_1",
                    "kg://course/core_2",
                    "kg://course/core_3",
                },
            )
            reasons = {
                question["course_kg_node_ids"][0]: question["selection_reason"]
                for question in result["questions"]
            }
            self.assertEqual(
                reasons["kg://course/core_2"],
                "previous_wrong_answer",
            )
            self.assertEqual(
                reasons["kg://course/core_3"],
                "learner_memory_weakness",
            )
            self.assertEqual(reasons["kg://course/core_1"], "course_coverage")

    def test_student_exam_does_not_expose_answers_and_cache_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            resource_root = root / "resources"
            bank_path, kag_dir = _write_fixture_catalog(root)
            first = build_and_save_final_exam(
                session_id="session",
                user_id="user",
                topic="牙周炎",
                outline=_outline(),
                session_summary=_summary(),
                resource_root=resource_root,
                question_bank_path=bank_path,
                kag_dir=kag_dir,
                question_count=3,
            )
            second = build_and_save_final_exam(
                session_id="session",
                user_id="user",
                topic="牙周炎",
                outline=_outline(),
                session_summary={"events": [], "mastery": []},
                resource_root=resource_root,
                question_bank_path=bank_path,
                kag_dir=kag_dir,
                question_count=3,
            )

            self.assertTrue(second["cache_hit"])
            self.assertEqual(second["exam_id"], first["exam_id"])
            for question in first["questions"]:
                self.assertNotIn("answer", question)
                self.assertNotIn("explanation", question)

            answer_key = json.loads(
                (
                    resource_root
                    / "session"
                    / "final_exam"
                    / "answer_key.v1.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(len(answer_key["answers"]), 3)
            self.assertTrue(all(item["answer"] == "B" for item in answer_key["answers"]))

    def test_incomplete_question_bank_is_reported_without_inventing_options(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bank_path, kag_dir = _write_fixture_catalog(root, complete=False)
            outline = {
                "nodes": [{
                    "id": "node_1",
                    "kg_node_ids": ["kg://course/core_1"],
                }],
            }
            result = build_and_save_final_exam(
                session_id="session",
                user_id="user",
                topic="牙周炎",
                outline=outline,
                session_summary={"events": [], "mastery": []},
                resource_root=root / "resources",
                question_bank_path=bank_path,
                kag_dir=kag_dir,
                question_count=2,
            )

            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["question_count"], 1)
            self.assertEqual(
                result["selection_trace"]["rejected_counts"]["missing_options"],
                1,
            )
            self.assertTrue(result["questions"][0]["options"])

    def test_continue_at_last_node_returns_generated_exam(self) -> None:
        class Memory:
            current = "node_1"

            def latest_outline(self, _session_id):
                return {"outline": {"nodes": [{"id": "node_1"}]}}

            def get_session(self, _session_id):
                return {
                    "id": "session",
                    "user_id": "user",
                    "current_outline_node_id": self.current,
                }

            def set_current_outline_node(self, _session_id, node_id):
                self.current = node_id

            @staticmethod
            def record_event(**_kwargs):
                return "event"

        orchestrator = object.__new__(LearningOrchestrator)
        orchestrator.memory = Memory()
        orchestrator._last_wrong_answer_context = None
        orchestrator._wait_for_outline_completion = lambda *_args, **_kwargs: True
        orchestrator._update_learner_memory_for_event = lambda **_kwargs: {}
        orchestrator.ensure_final_exam = lambda _session_id: {
            "status": "ready",
            "question_count": 3,
        }

        result = orchestrator.continue_to_next_section("session")

        self.assertTrue(result["lesson_completed"])
        self.assertEqual(result["final_exam"]["status"], "ready")
        self.assertEqual(result["final_exam"]["question_count"], 3)

    def test_submit_grades_and_returns_only_wrong_details(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            resource_root = root / "resources"
            bank_path, kag_dir = _write_fixture_catalog(root)
            exam = build_and_save_final_exam(
                session_id="session",
                user_id="user",
                topic="periodontitis",
                outline=_outline(),
                session_summary=_summary(),
                resource_root=resource_root,
                question_bank_path=bank_path,
                kag_dir=kag_dir,
                question_count=3,
            )
            question_ids = [question["id"] for question in exam["questions"]]
            answers = [
                {"question_id": question_ids[0], "selected_option": "B"},
                {"question_id": question_ids[1], "selected_option": "A"},
            ]

            result = submit_final_exam(
                session_id="session",
                answers=answers,
                resource_root=resource_root,
            )

            self.assertEqual(result["status"], "graded")
            self.assertEqual(result["correct_count"], 1)
            self.assertEqual(result["wrong_count"], 1)
            self.assertEqual(result["unanswered_count"], 1)
            self.assertEqual(len(result["wrong_questions"]), 2)
            wrong_ids = {item["question_id"] for item in result["wrong_questions"]}
            self.assertNotIn(question_ids[0], wrong_ids)
            self.assertIn(question_ids[1], wrong_ids)
            self.assertIn(question_ids[2], wrong_ids)
            self.assertNotIn("question_results", result)
            self.assertTrue(all(item["correct_answer"] == "B" for item in result["wrong_questions"]))
            self.assertTrue(all("explanation" in item for item in result["wrong_questions"]))

            submission = json.loads(
                final_exam_submission_path(
                    "session",
                    resource_root,
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(len(submission["question_results"]), 3)
            self.assertTrue(all("correct_answer" in item for item in submission["question_results"]))

    def test_submit_rejects_duplicate_unknown_and_invalid_answers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            resource_root = root / "resources"
            bank_path, kag_dir = _write_fixture_catalog(root)
            exam = build_and_save_final_exam(
                session_id="session",
                user_id="user",
                topic="periodontitis",
                outline=_outline(),
                session_summary=_summary(),
                resource_root=resource_root,
                question_bank_path=bank_path,
                kag_dir=kag_dir,
                question_count=3,
            )
            first_id = exam["questions"][0]["id"]

            duplicate = submit_final_exam(
                session_id="session",
                answers=[
                    {"question_id": first_id, "selected_option": "A"},
                    {"question_id": first_id, "selected_option": "B"},
                ],
                resource_root=resource_root,
            )
            unknown = submit_final_exam(
                session_id="session",
                answers=[{"question_id": "missing", "selected_option": "A"}],
                resource_root=resource_root,
            )
            invalid = submit_final_exam(
                session_id="session",
                answers=[{"question_id": first_id, "selected_option": "Z"}],
                resource_root=resource_root,
            )

            self.assertEqual(duplicate["status"], "invalid_submission")
            self.assertEqual(duplicate["errors"][0]["code"], "duplicate_question_id")
            self.assertEqual(unknown["errors"][0]["code"], "unknown_question_id")
            self.assertEqual(invalid["errors"][0]["code"], "invalid_selected_option")

    def test_submit_is_idempotent_and_changed_second_submit_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            resource_root = root / "resources"
            bank_path, kag_dir = _write_fixture_catalog(root)
            exam = build_and_save_final_exam(
                session_id="session",
                user_id="user",
                topic="periodontitis",
                outline=_outline(),
                session_summary=_summary(),
                resource_root=resource_root,
                question_bank_path=bank_path,
                kag_dir=kag_dir,
                question_count=2,
            )
            answers = [
                {"question_id": question["id"], "selected_option": "B"}
                for question in exam["questions"]
            ]
            callback_count = 0

            def persist(_submission: dict) -> dict:
                nonlocal callback_count
                callback_count += 1
                return {"count": callback_count}

            first = submit_final_exam(
                session_id="session",
                answers=answers,
                resource_root=resource_root,
                on_first_submit=persist,
            )
            second = submit_final_exam(
                session_id="session",
                answers=answers,
                resource_root=resource_root,
                on_first_submit=persist,
            )
            changed = submit_final_exam(
                session_id="session",
                answers=[{**answers[0], "selected_option": "A"}],
                resource_root=resource_root,
                on_first_submit=persist,
            )

            self.assertEqual(first["status"], "graded")
            self.assertFalse(first["cache_hit"])
            self.assertEqual(second["status"], "graded")
            self.assertTrue(second["cache_hit"])
            self.assertEqual(callback_count, 1)
            self.assertEqual(changed["status"], "exam_already_submitted")

    def test_force_rebuild_creates_new_exam_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            resource_root = root / "resources"
            bank_path, kag_dir = _write_fixture_catalog(root)
            first = build_and_save_final_exam(
                session_id="session",
                user_id="user",
                topic="periodontitis",
                outline=_outline(),
                session_summary=_summary(),
                resource_root=resource_root,
                question_bank_path=bank_path,
                kag_dir=kag_dir,
                question_count=3,
            )
            rebuilt = build_and_save_final_exam(
                session_id="session",
                user_id="user",
                topic="periodontitis",
                outline=_outline(),
                session_summary=_summary(),
                resource_root=resource_root,
                question_bank_path=bank_path,
                kag_dir=kag_dir,
                question_count=3,
                force=True,
            )

            self.assertEqual(first["exam_revision"], 1)
            self.assertEqual(rebuilt["exam_revision"], 2)
            self.assertNotEqual(first["exam_id"], rebuilt["exam_id"])

    def test_orchestrator_submit_persists_answer_mastery_profile_and_l3(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            resource_root = root / "resources"
            bank_path, kag_dir = _write_fixture_catalog(root)
            memory = LearningMemory(root / "learning.sqlite3")
            try:
                session = memory.create_session("user", "periodontitis")
                session_id = session["id"]
                memory.save_outline(session_id, _outline(), {}, {})
                memory.set_current_outline_node(session_id, None)
                exam = build_and_save_final_exam(
                    session_id=session_id,
                    user_id="user",
                    topic="periodontitis",
                    outline=_outline(),
                    session_summary=memory.session_summary(session_id),
                    resource_root=resource_root,
                    question_bank_path=bank_path,
                    kag_dir=kag_dir,
                    question_count=3,
                )
                answers = [
                    {
                        "question_id": exam["questions"][0]["id"],
                        "selected_option": "A",
                    },
                    {
                        "question_id": exam["questions"][1]["id"],
                        "selected_option": "B",
                    },
                ]
                orchestrator = LearningOrchestrator(
                    memory=memory,
                    resource_root=resource_root,
                )

                result = orchestrator.submit_final_exam(session_id, answers)
                repeated = orchestrator.submit_final_exam(session_id, answers)

                self.assertEqual(result["status"], "graded")
                self.assertTrue(repeated["cache_hit"])
                answer_records = memory.list_answer_records(session_id)
                self.assertEqual(len(answer_records), 3)
                self.assertTrue(all(record["grading"]["source"] == "final_exam" for record in answer_records))
                summary = memory.session_summary(session_id)
                self.assertGreaterEqual(len(summary["mastery"]), 3)
                profile = memory.get_or_create_profile("user")
                self.assertTrue(profile["known_weak_points"])
                weaknesses = memory.get_learner_memory_slot("user", "weaknesses")
                self.assertTrue(weaknesses.get("items"))
                self.assertGreaterEqual(result["memory_update"]["answer_records_count"], 3)
            finally:
                memory._conn.close()

    def test_submit_api_route_is_registered(self) -> None:
        from backend.main import app

        self.assertTrue(any(
            route.path == "/api/learning/session/{session_id}/final-exam/submit"
            and "POST" in getattr(route, "methods", set())
            for route in app.routes
        ))


if __name__ == "__main__":
    unittest.main()
