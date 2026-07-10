from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.services.learning.audio import reconcile_audio_manifest
from backend.services.learning.handout_cache import (
    get_handout_cache,
    save_handout_cache_failed,
    save_handout_cache_pending,
    save_handout_cache_ready,
)
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

    def test_refresh_slides_resource_uses_formal_session_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            session_id = "session"
            node_id = "node_1"
            handout = {
                "outline_node_id": node_id,
                "title": "Definition",
                "section_status": "llm_generated",
                "content": {
                    "overview": "Evidence-backed overview.",
                    "key_points": [{
                        "heading": "Core concept",
                        "explanation": "A traceable explanation from the handout.",
                        "evidence_refs": ["chunk:source"],
                    }],
                    "clinical_example": "",
                    "common_confusions": [],
                },
            }
            save_handout_cache_ready(session_id, node_id, handout, "test", root)
            orchestrator = object.__new__(LearningOrchestrator)
            orchestrator.resource_root = root

            result = orchestrator._refresh_slides_resource(
                session_id,
                {"title": "Course", "nodes": [{"id": node_id, "title": "Definition"}]},
            )

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["resource_json"]["schema_version"], "slides.v1")
            self.assertEqual(result["resource_json"]["slides"][0]["title"], "Definition")
            self.assertEqual(orchestrator._slides_resource_status(session_id)["status"], "ready")


class AudioManifestTests(unittest.TestCase):
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

if __name__ == "__main__":
    unittest.main()
