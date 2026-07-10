from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.services.learning.audio import reconcile_audio_manifest
from backend.services.learning.courseware import build_courseware_resources, export_pptx
from backend.services.learning.handout_cache import (
    get_handout_cache,
    save_handout_cache_failed,
    save_handout_cache_pending,
    save_handout_cache_ready,
)
from backend.services.learning.lecture_script import build_and_save_lecture_script
from backend.services.learning.orchestrator import _resolve_qa_outline_node
from backend.services.learning.orchestrator import LearningOrchestrator
from backend.services.learning.planner import repair_non_content_planner_fields
from backend.services.learning.resource_factory import generate_handout_section
from backend.services.learning.tutor import build_tutor_step_rule


REPO_ROOT = Path(__file__).resolve().parents[1]


def _node_evidence_with_edges(count: int = 17) -> tuple[dict, list[str]]:
    edge_ids = [f"edge_{index:02d}" for index in range(count)]
    return ({
        "outline_node_id": "node_1",
        "outline_node_title": "Mechanism",
        "kg_node_ids": [],
        "kg_nodes": [],
        "kg_edges": [
            {
                "edge_id": edge_id,
                "source_node_id": "kg://source",
                "target_node_id": f"kg://target/{index}",
                "relation": "RELATED_TO",
            }
            for index, edge_id in enumerate(edge_ids)
        ],
        "concepts": [],
        "questions": [],
        "evidence_chunks": [{
            "chunk_id": "chunk:source",
            "title": "Source",
            "source": "textbook",
            "source_type": "textbook",
            "text_excerpt": "Evidence-backed source text for the lesson section.",
        }],
    }, edge_ids)


class HandoutEvidenceTests(unittest.TestCase):
    def test_seventeen_valid_edges_are_all_processed(self) -> None:
        node_evidence, edge_ids = _node_evidence_with_edges()
        plan = {
            "need_more_evidence": False,
            "requested_chunk_ids": ["chunk:source"],
            "requested_kg_node_ids": [],
            "requested_kg_edge_ids": edge_ids,
            "requested_question_ids": [],
            "targeted_search_queries": [],
            "section_outline": ["Point one", "Point two"],
            "evidence_questions": [],
        }
        explanation = "Evidence-backed explanation. " * 12
        section = {
            "outline_node_id": "node_1",
            "title": "Mechanism",
            "section_status": "llm_generated",
            "evidence_status": "sufficient",
            "used_evidence": {
                "kg_node_ids": [],
                "kg_edge_ids": edge_ids,
                "concept_ids": [],
                "question_ids": [],
                "evidence_chunk_ids": ["chunk:source"],
            },
            "content": {
                "overview": "Evidence-backed overview.",
                "key_points": [
                    {"heading": "Point one", "explanation": explanation, "evidence_refs": ["chunk:source"]},
                    {"heading": "Point two", "explanation": explanation, "evidence_refs": ["chunk:source"]},
                ],
                "clinical_example": "",
                "common_confusions": [],
                "transition_to_next": "Continue.",
            },
            "missing_evidence": [],
        }
        responses = [json.dumps(plan), json.dumps(section)]
        with (
            patch("backend.services.llm_client.is_llm_configured", return_value=True),
            patch("backend.services.llm_client.chat_completion", side_effect=responses),
        ):
            result = generate_handout_section(
                outline_node={
                    "id": "node_1",
                    "title": "Mechanism",
                    "learning_objectives": ["Understand mechanism"],
                },
                node_evidence=node_evidence,
                evidence_pool={},
            )

        self.assertEqual(result["section_status"], "llm_generated")
        ledger = result["_trace"]["evidence_ledger"]
        self.assertEqual(ledger["requested_kg_edge_ids"], edge_ids)
        self.assertEqual(ledger["accepted_kg_edge_ids"], edge_ids)
        self.assertEqual(ledger["processed_kg_edge_ids"], edge_ids)
        self.assertEqual(ledger["cited_kg_edge_ids"], edge_ids)


class OrchestrationStateTests(unittest.TestCase):
    def test_evidence_context_can_be_reactivated_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            first = object.__new__(LearningOrchestrator)
            first.resource_root = Path(temp)
            first._last_evidence_pool = None
            first._last_outline_evidence_map = None
            first._active_evidence_session_id = None
            first._persist_evidence_context(
                "session",
                {"topic": "test"},
                {"node_1": {"kg_edges": [{"edge_id": "edge_1"}]}},
            )

            restarted = object.__new__(LearningOrchestrator)
            restarted.resource_root = Path(temp)
            restarted._last_evidence_pool = None
            restarted._last_outline_evidence_map = None
            restarted._active_evidence_session_id = None
            self.assertTrue(restarted._activate_evidence_context("session"))
            self.assertEqual(
                restarted._last_outline_evidence_map["node_1"]["kg_edges"][0]["edge_id"],
                "edge_1",
            )

    def test_missing_sequence_reason_is_repaired_without_touching_evidence(self) -> None:
        plan = {
            "nodes": [{
                "id": "node_1",
                "title": "Definition",
                "sequence_reason": "",
                "kg_node_ids": ["kg://one"],
            }]
        }
        warnings = repair_non_content_planner_fields(plan)
        self.assertTrue(plan["nodes"][0]["sequence_reason"])
        self.assertEqual(plan["nodes"][0]["kg_node_ids"], ["kg://one"])
        self.assertEqual(len(warnings), 1)

    def test_completed_lesson_question_routes_to_mechanism_node(self) -> None:
        nodes = [
            {"id": "node_1", "title": "定义与疾病本质"},
            {"id": "node_2", "title": "病因与危险因素"},
            {"id": "node_3", "title": "发病机制与组织破坏"},
            {"id": "node_4", "title": "临床表现与检查要点"},
            {"id": "node_5", "title": "诊断与鉴别诊断"},
        ]
        node = _resolve_qa_outline_node("牙周炎的形成机制是什么？", nodes)
        self.assertEqual(node["id"], "node_3")

    def test_failed_tutor_is_truthful(self) -> None:
        step = build_tutor_step_rule(
            {"nodes": [{"id": "node_1", "title": "Mechanism", "kg_node_ids": []}]},
            "node_1",
        )
        self.assertEqual(step["content_status"], "generation_failed")
        self.assertTrue(step["trace"]["fallback_rule_based"])
        self.assertFalse(step["questions"])

    def test_handout_attempt_count_survives_failure_and_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            save_handout_cache_pending("session", "node_1", root, source="background_prefetch")
            save_handout_cache_failed("session", "node_1", "first failure", "background_prefetch", root)
            save_handout_cache_pending("session", "node_1", root, source="cache_miss_sync_generation")
            record = get_handout_cache("session", "node_1", root)
            self.assertEqual(record["attempt_count"], 2)
            self.assertEqual(record["trace"]["source"], "cache_miss_sync_generation")

    def test_continue_does_not_commit_progress_when_generation_fails(self) -> None:
        class Memory:
            current = "node_1"

            def latest_outline(self, _session_id):
                return {"outline": {"nodes": [{"id": "node_1"}, {"id": "node_2"}]}}

            def get_session(self, _session_id):
                return {"id": "session", "user_id": "user", "current_outline_node_id": self.current}

            def set_current_outline_node(self, _session_id, node_id):
                self.current = node_id

            def record_event(self, **_kwargs):
                return "event"

        orchestrator = object.__new__(LearningOrchestrator)
        orchestrator.memory = Memory()
        orchestrator._last_wrong_answer_context = None
        orchestrator._wait_for_outline_completion = lambda *_args, **_kwargs: True
        orchestrator._update_learner_memory_for_event = lambda **_kwargs: {}
        orchestrator.next_step = lambda _session_id: {
            "content_status": "generation_failed",
            "handout_generation_error": "evidence_generation_failed",
            "handout_cache_hit": False,
            "cache_miss_sync_generation": True,
        }

        result = orchestrator.continue_to_next_section("session")
        self.assertFalse(result["advanced"])
        self.assertTrue(result["generation_blocked"])
        self.assertEqual(result["blocked_outline_node_id"], "node_2")
        self.assertEqual(orchestrator.memory.current, "node_1")


class CoursewareTests(unittest.TestCase):
    def _content_fixture(self) -> dict:
        return json.loads(
            (REPO_ROOT / "scripts" / "fixtures" / "handout_section_fixture.json").read_text(
                encoding="utf-8"
            )
        )

    def test_audio_manifest_is_partial_when_only_three_of_five_sections_exist(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            session_dir = root / "session"
            session_dir.mkdir(parents=True)
            sections = [
                {"outline_node_id": f"node_{index}", "status": "ready", "slide_count": 1, "audio_count": 1}
                for index in (1, 2, 4)
            ]
            (session_dir / "audio_manifest.json").write_text(
                json.dumps({"session_id": "session", "status": "ready", "sections": sections}),
                encoding="utf-8",
            )
            outline_nodes = [{"id": f"node_{index}"} for index in range(1, 6)]
            result = reconcile_audio_manifest(
                session_id="session",
                outline_nodes=outline_nodes,
                resource_root=root,
            )
            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["ready_section_count"], 3)
            self.assertEqual(result["missing_section_ids"], ["node_3", "node_5"])

    def test_partial_deck_removes_stale_pptx(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            session_dir = root / "session"
            slides_dir = session_dir / "slides"
            slides_dir.mkdir(parents=True)
            slides_path = slides_dir / "slides.v1.json"
            slides_path.write_text("{}", encoding="utf-8")
            stale_pptx = session_dir / "lesson.pptx"
            stale_pptx.write_bytes(b"stale")

            status = export_pptx(
                deck={
                    "session_id": "session",
                    "status": "partial",
                    "expected_section_count": 2,
                    "ready_section_count": 1,
                    "missing_node_ids": ["node_2"],
                    "slide_count": 1,
                },
                slides_path=slides_path,
                resource_root=root,
            )

            self.assertEqual(status["status"], "partial")
            self.assertFalse(stale_pptx.exists())

    @unittest.skipUnless(
        shutil.which("node")
        and (REPO_ROOT / "tools" / "ppt_export" / "node_modules" / "pptxgenjs").exists(),
        "PptxGenJS runtime is not installed",
    )
    def test_complete_scripts_export_a_valid_pptx(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            session_id = "session"
            content = self._content_fixture()
            outline = {"title": "Course", "nodes": []}
            audio_sections = []
            for index in range(1, 3):
                node_id = f"node_{index}"
                title = f"Section {index}"
                handout = {
                    "outline_node_id": node_id,
                    "title": title,
                    "section_status": "llm_generated",
                    "content": content,
                }
                save_handout_cache_ready(session_id, node_id, handout, "test", root)
                script = build_and_save_lecture_script(
                    session_id=session_id,
                    outline_node_id=node_id,
                    handout_section=handout,
                    resource_root=root,
                )
                outline["nodes"].append({"id": node_id, "title": title})
                audio_sections.append({
                    "outline_node_id": node_id,
                    "status": "ready",
                    "slide_count": script["slide_count"],
                    "audio_count": script["slide_count"],
                    "slides": [
                        {
                            "slide_id": item["slide_id"],
                            "status": "ready",
                            "audio_file": f"audio_files/{node_id}/{item['slide_id']}.mp3",
                            "text_hash": item["text_hash"],
                        }
                        for item in script["slides"]
                    ],
                })
            session_dir = root / session_id
            (session_dir / "audio_manifest.json").write_text(
                json.dumps({"session_id": session_id, "status": "ready", "sections": audio_sections}),
                encoding="utf-8",
            )

            result = build_courseware_resources(
                session_id=session_id,
                outline=outline,
                resource_root=root,
            )
            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["slides"]["status"], "ready")
            self.assertTrue(result["pptx_status"]["ok"])
            self.assertTrue(Path(result["pptx_status"]["pptx_path"]).exists())
            self.assertEqual(
                result["pptx_status"]["validation"]["slide_count"],
                result["slides"]["slide_count"] + 1,
            )
            self.assertGreaterEqual(
                result["pptx_status"]["validation"]["notes_count"],
                result["slides"]["slide_count"],
            )
            self.assertTrue(Path(result["pin_and_paper_plan_path"]).exists())


if __name__ == "__main__":
    unittest.main()
