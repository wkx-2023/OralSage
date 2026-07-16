from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from backend.services.learning.audio import (
    XfyunSmartTTSClient,
    XfyunTTSConfig,
    ensure_handout_audio,
    reconcile_audio_manifest,
)
from backend.services.learning.handout_cache import (
    get_handout_cache,
    save_handout_cache_failed,
    save_handout_cache_pending,
    save_handout_cache_ready,
)
from backend.services.learning.lecture_script import build_tutor_lecture_script
from backend.services.learning.orchestrator import _resolve_qa_outline_node
from backend.services.learning.orchestrator import LearningOrchestrator
from backend.services.learning.memory import LearningMemory
from backend.services.learning.pin_and_paper_planner import plan_pin_and_paper_slide
from backend.services.learning.planner import repair_non_content_planner_fields
from backend.services.learning.ppt_pipeline import (
    _find_browser,
    _source_hash,
    _write_json_atomic,
    export_pin_and_paper_pptx,
    refresh_ppt_resources,
)
from backend.services.learning.resource_factory import generate_handout_section
from backend.services.learning.stage_exam import (
    generate_stage_exam,
    public_stage_exam,
    submit_stage_exam,
)
from backend.services.learning.tutor import build_tutor_step_rule
from backend.services.learning.tutor_cache import save_tutor_cache_ready


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


class StageExamTests(unittest.TestCase):
    def test_stage_exam_prefers_question_bank_by_outline_question_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = LearningMemory(root / "learning.sqlite3")
            try:
                session = memory.create_session("u1", "牙周炎")
                session_id = session["id"]
                outline = {
                    "title": "牙周炎",
                    "nodes": [
                        {
                            "id": "node_1",
                            "title": "慢性牙周炎",
                            "difficulty": "basic",
                            "kg_node_ids": ["kg://periodontitis/core_1"],
                            "question_ids": ["question:q1"],
                        }
                    ],
                }
                memory.save_outline(session_id, outline, {"nodes": []}, {"source": "test"})
                bank = [
                    {
                        "question_id": "question:q1",
                        "source_question_id": "source-q1",
                        "source_exam_id": "exam-1",
                        "stem": "慢性牙周炎临床表现不包括( )",
                        "answer": "E",
                        "primary_node_id": "kg://periodontitis/core_1",
                        "primary_knowledge_point": "慢性牙周炎的临床附着丧失表现",
                        "verification_status": "linked",
                        "match_status": "weak_match",
                        "authority_level": 70,
                    },
                    {
                        "question_id": "question:q2",
                        "stem": "侵袭性牙周炎主要病因( )",
                        "answer": "B",
                        "primary_node_id": "kg://periodontitis/core_2",
                        "primary_knowledge_point": "侵袭性牙周炎的多因素病因机制",
                    },
                    {
                        "question_id": "question:q3",
                        "stem": "牙周炎危险因素包括( )",
                        "answer": "C",
                        "primary_node_id": "kg://periodontitis/core_3",
                        "primary_knowledge_point": "吸烟对牙周组织的影响",
                    },
                    {
                        "question_id": "question:q4",
                        "stem": "牙周炎全身促进因素包括( )",
                        "answer": "D",
                        "primary_node_id": "kg://periodontitis/core_4",
                        "primary_knowledge_point": "糖尿病对牙周组织破坏程度的影响",
                    },
                ]

                full_record = {
                    "id": "source-q1",
                    "stem": "Full bank stem",
                    "options": {
                        "A": "Option A",
                        "B": "Option B",
                        "C": "Option C",
                        "D": "Option D",
                        "E": "Option E",
                    },
                    "answer": "E",
                    "explanation": "Full bank explanation",
                    "primary_knowledge_point": "Full bank knowledge point",
                    "subject": "subject",
                    "chapter": "chapter",
                }
                bank = {
                    "kag_rows": bank,
                    "full_records": [full_record],
                    "full_by_id": {"source-q1": full_record},
                }

                with patch("backend.services.learning.stage_exam._load_stage_exam_question_bank", return_value=bank):
                    exam = generate_stage_exam(memory, session_id, resource_root=root, force=True)

                self.assertEqual(exam["source"]["question_sources"]["question_bank.full_options"], 1)
                self.assertEqual(exam["warnings"], [])
                question = exam["questions"][0]
                self.assertEqual(question["source"], "question_bank.full_options")
                self.assertEqual(question["source_question_id"], "source-q1")
                self.assertEqual(question["stem"], "Full bank stem")
                self.assertEqual(question["options"], [
                    "A. Option A",
                    "B. Option B",
                    "C. Option C",
                    "D. Option D",
                    "E. Option E",
                ])
                self.assertEqual(question["answer"], "E")
                self.assertEqual(question["explanation"], "Full bank explanation")
                submitted = submit_stage_exam(
                    memory,
                    session_id,
                    [{"question_id": question["id"], "selected_option": "E"}],
                    resource_root=root,
                )
                self.assertEqual(submitted["submission"]["overall_score"], 1.0)
            finally:
                memory._conn.close()

    def test_generate_and_submit_stage_exam(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = LearningMemory(root / "learning.sqlite3")
            try:
                session = memory.create_session("u1", "牙周炎")
                session_id = session["id"]
                outline = {
                    "title": "牙周炎",
                    "nodes": [
                        {
                            "id": "node_1",
                            "title": "分类",
                            "difficulty": "basic",
                            "kg_node_ids": ["kg:class"],
                        },
                        {
                            "id": "node_2",
                            "title": "病因",
                            "difficulty": "basic",
                            "kg_node_ids": ["kg:cause"],
                        },
                    ],
                }
                memory.save_outline(session_id, outline, {"nodes": []}, {"source": "test"})
                save_tutor_cache_ready(
                    session_id,
                    "node_1",
                    {
                        "outline_node_id": "node_1",
                        "questions": [
                            {
                                "id": "q1",
                                "type": "single_choice",
                                "stem": "牙周炎分类学习的重点是什么？",
                                "options": ["A. 只记名称", "B. 理解分类标准", "C. 跳过", "D. 只背答案"],
                                "answer": "B",
                                "explanation": "需要理解分类标准。",
                                "kg_node_ids": ["kg:class"],
                            }
                        ],
                    },
                    source="test",
                    resource_root=root,
                )

                with patch(
                    "backend.services.learning.stage_exam._load_stage_exam_question_bank",
                    return_value={"kag_rows": [], "full_records": [], "full_by_id": {}},
                ):
                    exam = generate_stage_exam(memory, session_id, resource_root=root)

                self.assertEqual(exam["status"], "ready")
                self.assertEqual(len(exam["questions"]), 2)
                self.assertEqual(exam["source"]["question_sources"]["tutor_step.questions"], 1)
                self.assertEqual(exam["source"]["question_sources"]["stage_exam.fallback"], 1)
                self.assertNotIn("answer", public_stage_exam(exam)["questions"][0])

                submitted = submit_stage_exam(
                    memory,
                    session_id,
                    [
                        {"question_id": exam["questions"][0]["id"], "selected_option": "B"},
                        {"question_id": exam["questions"][1]["id"], "selected_option": "A"},
                    ],
                    resource_root=root,
                )

                self.assertEqual(submitted["status"], "submitted")
                self.assertEqual(submitted["submission"]["overall_score"], 0.5)
                self.assertIn("node_2", submitted["submission"]["weak_section_ids"])
                self.assertIn("kg:cause", submitted["submission"]["weak_kg_node_ids"])
                self.assertEqual(len(memory.list_answer_records(session_id)), 2)
            finally:
                memory._conn.close()


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


class LectureScriptTests(unittest.TestCase):
    def test_tutor_lecture_script_uses_teaching_text_as_source(self) -> None:
        script = build_tutor_lecture_script(
            session_id="session",
            outline_node_id="node_1",
            title="Section",
            tutor_step={
                "teaching_text": "Tutor paragraph one.\n\nTutor paragraph two.",
            },
        )

        self.assertEqual(script["status"], "ready")
        self.assertEqual(script["source"], "tutor_step.teaching_text")
        self.assertEqual(script["source_tutor_hash"], script["source_handout_hash"])
        self.assertEqual(
            [slide["text"] for slide in script["slides"]],
            ["Tutor paragraph one.", "Tutor paragraph two."],
        )
        self.assertTrue(
            all(
                slide["source_path"] == "tutor_step.teaching_text"
                for slide in script["slides"]
            )
        )


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

    def test_handout_fan_out_waits_for_tutor_text_before_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            orchestrator = LearningOrchestrator(memory=object(), resource_root=Path(temp))
            events: list[str] = []

            with (
                patch(
                    "backend.services.learning.orchestrator.build_and_save_lecture_script",
                ) as build_script,
                patch("backend.services.learning.orchestrator.ensure_handout_audio") as synthesize,
                patch.object(
                    orchestrator,
                    "_schedule_ppt_refresh",
                    side_effect=lambda *_args: events.append("ppt_scheduled") or {"started": True},
                ),
            ):
                started_at = time.perf_counter()
                result = orchestrator._fan_out_handout_resources(
                    session_id="session",
                    outline={"nodes": [{"id": "node_1"}]},
                    outline_node_id="node_1",
                    handout_section={"title": "Section", "content": {"overview": "Text"}},
                    source="test",
                )
                elapsed = time.perf_counter() - started_at
                self.assertLess(elapsed, 0.5)
                self.assertIn("ppt_scheduled", events)
                self.assertFalse(result["audio"]["started"])
                self.assertEqual(result["audio"]["status"], "waiting_for_tutor_step")
                build_script.assert_not_called()
                synthesize.assert_not_called()

    def test_failed_audio_is_not_retried_on_cache_hit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            orchestrator = LearningOrchestrator(memory=object(), resource_root=Path(temp))
            script = {
                "status": "ready",
                "source_handout_hash": "same-hash",
                "slides": [{"slide_id": "slide_001", "text": "Narration"}],
            }
            with (
                patch(
                    "backend.services.learning.orchestrator.build_and_save_lecture_script",
                    return_value=script,
                ),
                patch(
                    "backend.services.learning.orchestrator.get_section_audio",
                    return_value={
                        "status": "failed",
                        "source_handout_hash": "same-hash",
                    },
                ),
                patch(
                    "backend.services.learning.orchestrator.ensure_handout_audio",
                ) as synthesize,
            ):
                result = orchestrator._ensure_handout_section_audio(
                    session_id="session",
                    outline_node_id="node_1",
                    handout_section={"title": "Section"},
                    source="tutor_cache_hit",
                )

            self.assertTrue(result["retry_deferred"])
            synthesize.assert_not_called()
            self.assertEqual(orchestrator._audio_generation_threads, {})

    def test_audio_generation_respects_configured_concurrency(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(
                os.environ,
                {"LEARNING_AUDIO_MAX_CONCURRENCY": "1"},
            ):
                orchestrator = LearningOrchestrator(
                    memory=object(),
                    resource_root=Path(temp),
                )
            first_started = threading.Event()
            second_started = threading.Event()
            release_first = threading.Event()

            def blocked_audio(**kwargs):
                node_id = kwargs["outline_node_id"]
                if node_id == "node_1":
                    first_started.set()
                    release_first.wait(5)
                else:
                    second_started.set()
                return {"status": "ready"}

            with (
                patch(
                    "backend.services.learning.orchestrator.ensure_handout_audio",
                    side_effect=blocked_audio,
                ),
                patch(
                    "backend.services.learning.orchestrator.get_section_audio",
                    return_value=None,
                ),
            ):
                orchestrator._schedule_audio_generation(
                    session_id="session",
                    outline_node_id="node_1",
                    handout_section={"title": "node_1"},
                    source="test",
                    lecture_script={
                        "source_handout_hash": "hash-node_1",
                        "slides": [{"slide_id": "slide_001", "text": "Text"}],
                    },
                )
                self.assertTrue(first_started.wait(1))
                orchestrator._schedule_audio_generation(
                    session_id="session",
                    outline_node_id="node_2",
                    handout_section={"title": "node_2"},
                    source="test",
                    lecture_script={
                        "source_handout_hash": "hash-node_2",
                        "slides": [{"slide_id": "slide_001", "text": "Text"}],
                    },
                )
                self.assertFalse(second_started.wait(0.1))
                release_first.set()
                self.assertTrue(second_started.wait(1))

                for thread in list(orchestrator._audio_generation_threads.values()):
                    thread.join(2)

            self.assertEqual(orchestrator._audio_generation_threads, {})

    def test_wait_for_background_resources_reports_timeout_then_completion(self) -> None:
        class Memory:
            @staticmethod
            def latest_outline(_session_id):
                return {"outline": {"nodes": [{"id": "node_1"}]}}

        with tempfile.TemporaryDirectory() as temp:
            orchestrator = LearningOrchestrator(
                memory=Memory(),
                resource_root=Path(temp),
            )
            worker_started = threading.Event()
            release_worker = threading.Event()

            def blocked_audio(**_kwargs):
                worker_started.set()
                release_worker.wait(5)
                return {"status": "ready"}

            with (
                patch(
                    "backend.services.learning.orchestrator.ensure_handout_audio",
                    side_effect=blocked_audio,
                ),
                patch(
                    "backend.services.learning.orchestrator.get_section_audio",
                    return_value=None,
                ),
            ):
                orchestrator._schedule_audio_generation(
                    session_id="session",
                    outline_node_id="node_1",
                    handout_section={"title": "Section"},
                    source="test",
                    lecture_script={
                        "source_handout_hash": "hash-one",
                        "slides": [{"slide_id": "slide_001", "text": "Text"}],
                    },
                )
                self.assertTrue(worker_started.wait(1))

                timed_out = orchestrator.wait_for_background_resources(
                    "session",
                    timeout_seconds=0.01,
                )
                self.assertTrue(timed_out["timed_out"])
                self.assertFalse(timed_out["completed"])
                self.assertTrue(
                    any(name.startswith("audio-generation-") for name in timed_out["active_threads"])
                )

                release_worker.set()
                completed = orchestrator.wait_for_background_resources(
                    "session",
                    timeout_seconds=2,
                )

            self.assertTrue(completed["completed"])
            self.assertFalse(completed["timed_out"])
            self.assertEqual(completed["active_threads"], [])


class PptPipelineTests(unittest.TestCase):
    def _handout(self, node_id: str) -> dict:
        return {
            "outline_node_id": node_id,
            "title": f"Section {node_id}",
            "section_status": "llm_generated",
            "content": {
                "overview": f"Evidence-backed overview for {node_id}.",
                "key_points": [
                    {"heading": "Point one", "explanation": "Traceable explanation one.", "evidence_refs": ["chunk:one"]},
                    {"heading": "Point two", "explanation": "Traceable explanation two.", "evidence_refs": ["chunk:two"]},
                ],
                "clinical_example": "",
                "common_confusions": [],
            },
        }

    def test_invented_three_card_type_is_corrected_with_complete_schema(self) -> None:
        source_slide = {
            "slide_id": "slide_node_2",
            "outline_node_id": "node_2",
            "title": "Risk factors",
            "content": {
                "key_points": [
                    {"heading": "One", "explanation": "First"},
                    {"heading": "Two", "explanation": "Second"},
                    {"heading": "Three", "explanation": "Third"},
                ],
            },
            "blocks": [
                {"block_id": "block_1", "body": "First"},
                {"block_id": "block_2", "body": "Second"},
                {"block_id": "block_3", "body": "Third"},
            ],
        }
        first_plan = {
            "source_slide_id": "slide_node_2",
            "source_outline_node_id": "node_2",
            "page_type": "notes_3up",
            "rationale": "Three parallel points.",
            "slots": {"headline": "Risk factors", "cards": []},
            "content_warnings": [],
        }
        corrected_plan = {
            "source_slide_id": "slide_node_2",
            "source_outline_node_id": "node_2",
            "page_type": "notes",
            "rationale": "Three parallel points.",
            "slots": {
                "headline": "Risk factors",
                "subtitle": "",
                "cards": [
                    {"label": str(index), "title": title, "body": body, "scribble": "", "source_block_ids": [f"block_{index}"]}
                    for index, (title, body) in enumerate(
                        (("One", "First"), ("Two", "Second"), ("Three", "Third")),
                        start=1,
                    )
                ],
                "footer_left": "",
                "footer_right": "",
            },
            "content_warnings": [],
        }
        prompts: list[str] = []

        def llm_client(prompt: str) -> str:
            prompts.append(prompt)
            return json.dumps(first_plan if len(prompts) == 1 else corrected_plan)

        result = plan_pin_and_paper_slide(source_slide, llm_client=llm_client)

        self.assertEqual(result["slide_plan"]["page_type"], "notes")
        self.assertEqual(len(result["slide_plan"]["slots"]["cards"]), 3)
        self.assertEqual(result["llm_calls"], 2)
        self.assertIn("YOUR PREVIOUS FULL PAGE PLAN", prompts[1])
        self.assertIn('The corrected page_type MUST be "notes".', prompts[1])
        self.assertIn("Do not return an empty slots object", prompts[1])

    def test_partial_plan_uses_formal_slides_and_reuses_node_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            session_id = "session"
            outline = {
                "title": "Course",
                "nodes": [
                    {"id": "node_1", "title": "One"},
                    {"id": "node_2", "title": "Two"},
                    {"id": "node_3", "title": "Three"},
                ],
            }
            for node_id in ("node_1", "node_2"):
                save_handout_cache_ready(session_id, node_id, self._handout(node_id), "test", root)

            def planned_deck(deck, **_kwargs):
                return {
                    "slides": [
                        {
                            "source_slide_id": slide["slide_id"],
                            "source_outline_node_id": slide["outline_node_id"],
                            "page_type": "notes_2up",
                            "rationale": "Two source points.",
                            "slots": {
                                "headline": slide["title"],
                                "subtitle": "",
                                "cards": [
                                    {"label": "01", "title": "One", "body": "Body one", "scribble": ""},
                                    {"label": "02", "title": "Two", "body": "Body two", "scribble": ""},
                                ],
                                "footer_left": "OralSage",
                                "footer_right": "",
                            },
                            "content_warnings": [],
                        }
                        for slide in deck["slides"]
                    ],
                    "warnings": [],
                    "_llm_calls": len(deck["slides"]),
                }

            with patch(
                "backend.services.learning.ppt_pipeline.plan_deck_concurrent",
                side_effect=planned_deck,
            ) as planner:
                result = refresh_ppt_resources(
                    session_id=session_id,
                    outline=outline,
                    resource_root=root,
                    llm_client=lambda _prompt: "{}",
                    export_pptx=False,
                )
                second = refresh_ppt_resources(
                    session_id=session_id,
                    outline=outline,
                    resource_root=root,
                    llm_client=lambda _prompt: "{}",
                    export_pptx=False,
                )

            plan = result["pin_and_paper_plan"]
            self.assertEqual(plan["status"], "partial")
            self.assertEqual(plan["ready_node_ids"], ["node_1", "node_2"])
            self.assertEqual(plan["missing_node_ids"], ["node_3"])
            self.assertEqual(plan["content_slide_count"], 2)
            self.assertEqual(len(result["slides"]["resource_json"]["slides"]), 2)
            self.assertEqual(second["pin_and_paper_plan"]["content_slide_count"], 2)
            self.assertEqual(planner.call_count, 1)

    def test_partial_plan_defers_expensive_pptx_export(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plan = {
                "status": "partial",
                "plan_path": str(root / "session" / "slides" / "pin_and_paper_plan.v1.json"),
                "expected_node_ids": ["node_1", "node_2"],
                "ready_node_ids": ["node_1"],
            }
            slides = {
                "status": "ok",
                "resource_json": {"schema_version": "slides.v1", "slides": [{"slide_id": "slide_node_1"}]},
            }
            with (
                patch(
                    "backend.services.learning.ppt_pipeline.generate_slides_resource_from_session_cache",
                    return_value=slides,
                ),
                patch(
                    "backend.services.learning.ppt_pipeline.build_and_save_pin_and_paper_plan",
                    return_value=plan,
                ),
                patch(
                    "backend.services.learning.ppt_pipeline.export_pin_and_paper_pptx",
                ) as export,
            ):
                result = refresh_ppt_resources(
                    session_id="session",
                    outline={"nodes": [{"id": "node_1"}, {"id": "node_2"}]},
                    resource_root=root,
                )

            export.assert_not_called()
            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["pptx"]["status"], "deferred")
            self.assertEqual(result["pptx"]["ready_section_count"], 1)

    def test_failed_concurrent_plan_does_not_overwrite_ready_node_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            session_id = "session"
            outline = {"title": "Course", "nodes": [{"id": "node_1", "title": "One"}]}
            save_handout_cache_ready(session_id, "node_1", self._handout("node_1"), "test", root)
            valid_plan = {
                "source_slide_id": "slide_node_1",
                "source_outline_node_id": "node_1",
                "page_type": "notes_2up",
                "rationale": "Two source points.",
                "slots": {
                    "headline": "One",
                    "subtitle": "",
                    "cards": [
                        {"label": "1", "title": "One", "body": "First", "scribble": ""},
                        {"label": "2", "title": "Two", "body": "Second", "scribble": ""},
                    ],
                    "footer_left": "",
                    "footer_right": "",
                },
                "content_warnings": [],
            }

            def stale_failed_result(deck, **_kwargs):
                slide = deck["slides"][0]
                cache_path = root / session_id / "slides" / "pin_and_paper" / "node_1.json"
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps({
                    "schema_version": "learning.pin_and_paper_node_plan.v1",
                    "session_id": session_id,
                    "outline_node_id": "node_1",
                    "source_slide_hash": _source_hash(slide),
                    "status": "ready",
                    "slide_plan": valid_plan,
                    "warnings": [],
                    "error": None,
                }), encoding="utf-8")
                return {
                    "slides": [{
                        **valid_plan,
                        "page_type": "notes_3up",
                    }],
                    "warnings": ["stale invalid result"],
                    "_llm_calls": 1,
                }

            with patch(
                "backend.services.learning.ppt_pipeline.plan_deck_concurrent",
                side_effect=stale_failed_result,
            ):
                result = refresh_ppt_resources(
                    session_id=session_id,
                    outline=outline,
                    resource_root=root,
                    llm_client=lambda _prompt: "{}",
                    export_pptx=False,
                )

            self.assertEqual(result["pin_and_paper_plan"]["status"], "ready")
            cache = json.loads(
                (root / session_id / "slides" / "pin_and_paper" / "node_1.json").read_text(encoding="utf-8")
            )
            self.assertEqual(cache["status"], "ready")
            self.assertEqual(cache["slide_plan"]["page_type"], "notes_2up")

    def test_concurrent_json_cache_writes_remain_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "cache.json"
            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(
                    lambda index: _write_json_atomic(path, {"writer": index}),
                    range(64),
                ))

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn(payload["writer"], range(64))
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_failed_rebuild_preserves_last_good_pptx(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            session_id = "session"
            session_dir = root / session_id
            session_dir.mkdir(parents=True)
            output_path = session_dir / "lesson.pptx"
            output_path.write_bytes(b"previous valid pptx")
            plan_path = session_dir / "slides" / "pin_and_paper_plan.v1.json"
            plan_path.parent.mkdir(parents=True)
            plan_path.write_text("{}", encoding="utf-8")
            fake_exporter = root / "tools" / "export.mjs"
            fake_exporter.parent.mkdir(parents=True)
            fake_exporter.write_text("", encoding="utf-8")
            (fake_exporter.parent / "node_modules" / "pptxgenjs").mkdir(parents=True)
            plan = {
                "status": "ready",
                "expected_node_ids": ["node_1"],
                "ready_node_ids": ["node_1"],
                "missing_node_ids": [],
                "content_slide_count": 1,
                "slides": [{
                    "source_outline_node_id": "node_1",
                    "page_type": "quote",
                    "slots": {"headline": "Definition"},
                }],
            }

            with (
                patch("backend.services.learning.ppt_pipeline._EXPORTER_PATH", fake_exporter),
                patch("backend.services.learning.ppt_pipeline.shutil.which", return_value="node"),
                patch(
                    "backend.services.learning.ppt_pipeline._render_plan_to_pngs",
                    side_effect=RuntimeError("render failed"),
                ),
            ):
                status = export_pin_and_paper_pptx(
                    session_id=session_id,
                    plan=plan,
                    plan_path=plan_path,
                    resource_root=root,
                )

            self.assertFalse(status["ok"])
            self.assertEqual(output_path.read_bytes(), b"previous valid pptx")

    @unittest.skipUnless(
        _find_browser() is not None
        and shutil.which("node")
        and (REPO_ROOT / "tools" / "ppt_export" / "node_modules" / "pptxgenjs").exists(),
        "Edge/Chrome and PptxGenJS are required",
    )
    def test_html_renderer_exports_distinct_pptx_pages(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            session_id = "session"
            plan = {
                "schema_version": "pin_and_paper_slide_plan.v1",
                "template_slug": "pin-and-paper",
                "deck_title": "Course",
                "session_id": session_id,
                "status": "ready",
                "expected_node_ids": ["node_1"],
                "ready_node_ids": ["node_1"],
                "missing_node_ids": [],
                "content_slide_count": 1,
                "slides": [
                    {
                        "source_slide_id": "cover",
                        "source_outline_node_id": "",
                        "page_type": "cover",
                        "rationale": "Cover",
                        "slots": {
                            "eyebrow": "ORALSAGE", "title": "Course",
                            "subtitle_line1": "Line one", "subtitle_line2": "Line two",
                            "presenter": "OralSage", "presenter_role": "Learning assistant",
                            "date_label": "2026.07.10", "handwritten_note": "Start here",
                        },
                        "content_warnings": [],
                    },
                    {
                        "source_slide_id": "slide_node_1",
                        "source_outline_node_id": "node_1",
                        "page_type": "quote",
                        "rationale": "Definition",
                        "slots": {
                            "headline": "Definition", "quote_text": "Evidence-backed definition.",
                            "attribution_name": "Source", "attribution_detail": "Textbook",
                            "footer_left": "OralSage", "footer_right": "01",
                        },
                        "content_warnings": [],
                    },
                ],
            }
            plan_path = root / session_id / "slides" / "pin_and_paper_plan.v1.json"
            plan_path.parent.mkdir(parents=True)
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            status = export_pin_and_paper_pptx(
                session_id=session_id,
                plan=plan,
                plan_path=plan_path,
                resource_root=root,
            )

            self.assertTrue(status["ok"])
            self.assertEqual(status["validation"]["slide_count"], 2)
            with zipfile.ZipFile(status["pptx_path"]) as archive:
                images = [
                    archive.read(name)
                    for name in sorted(archive.namelist())
                    if name.startswith("ppt/media/") and name.endswith(".png")
                ]
            self.assertEqual(len(images), 2)
            self.assertNotEqual(
                hashlib.sha256(images[0]).hexdigest(),
                hashlib.sha256(images[1]).hexdigest(),
            )


class XfyunLongTextTTSClientTests(unittest.TestCase):
    def _config(self) -> XfyunTTSConfig:
        return XfyunTTSConfig(
            create_url="https://api-dx.xf-yun.com/v1/private/dts_create",
            query_url="https://api-dx.xf-yun.com/v1/private/dts_query",
            app_id="app-id",
            api_key="api-key",
            api_secret="api-secret",
            api_password=None,
            voice="x4_mingge",
            language="zh",
            encoding="lame",
            sample_rate=16000,
            channels=1,
            bit_depth=16,
            speed=50,
            volume=50,
            pitch=50,
            read_punctuation=0,
            pinyin_enabled=0,
            timeout_seconds=30,
            poll_interval_seconds=0.2,
        )

    def test_synthesize_creates_queries_and_downloads_long_text_audio(self) -> None:
        class Response:
            def __init__(self, body: bytes) -> None:
                self.body = body

            def read(self) -> bytes:
                return self.body

            def close(self) -> None:
                pass

        requests: list[object] = []
        audio_url = "https://download.example/audio.mp3"

        def fake_urlopen(request, timeout):
            requests.append(request)
            if len(requests) == 1:
                return Response(
                    json.dumps({
                        "header": {
                            "code": 0,
                            "message": "success",
                            "task_id": "task-1",
                        },
                        "payload": None,
                    }).encode("utf-8")
                )
            if len(requests) == 2:
                return Response(
                    json.dumps({
                        "header": {
                            "code": 0,
                            "message": "success",
                            "task_id": "task-1",
                            "task_status": "5",
                        },
                        "payload": {
                            "audio": {
                                "audio": base64.b64encode(
                                    audio_url.encode("utf-8")
                                ).decode("utf-8"),
                            },
                        },
                    }).encode("utf-8")
                )
            self.assertEqual(request, audio_url)
            self.assertEqual(timeout, 30)
            return Response(b"mp3-bytes")

        client = XfyunSmartTTSClient(self._config())
        with patch("backend.services.learning.audio.urlopen", side_effect=fake_urlopen):
            audio = client.synthesize(" 第一段讲稿。\n")

        self.assertEqual(audio, b"mp3-bytes")
        self.assertEqual(len(requests), 3)

        create_request = requests[0]
        self.assertIn("/v1/private/dts_create?", create_request.full_url)
        self.assertIn("authorization=", create_request.full_url)
        create_body = json.loads(create_request.data.decode("utf-8"))
        self.assertEqual(create_body["header"]["app_id"], "app-id")
        self.assertIn("dts", create_body["parameter"])
        self.assertNotIn("tts", create_body["parameter"])
        self.assertEqual(create_body["parameter"]["dts"]["vcn"], "x4_mingge")
        self.assertEqual(create_body["parameter"]["dts"]["audio"]["encoding"], "lame")
        self.assertEqual(
            base64.b64decode(create_body["payload"]["text"]["text"]).decode("utf-8"),
            "第一段讲稿。",
        )

        query_request = requests[1]
        self.assertIn("/v1/private/dts_query?", query_request.full_url)
        query_body = json.loads(query_request.data.decode("utf-8"))
        self.assertEqual(query_body["header"]["task_id"], "task-1")

    def test_from_env_maps_legacy_super_smart_values_to_long_text_defaults(self) -> None:
        with patch.dict(
            os.environ,
            {
                "XF_TTS_URL": "wss://cbm01.cn-huabei-1.xf-yun.com/v1/private/mcd9m97e6",
                "XF_TTS_APP_ID": "app-id",
                "XF_TTS_API_KEY": "api-key",
                "XF_TTS_API_SECRET": "api-secret",
                "XF_TTS_VCN": "x5_lingxiaoxuan_flow",
            },
            clear=False,
        ):
            config = XfyunTTSConfig.from_env()

        self.assertEqual(config.create_url, "https://api-dx.xf-yun.com/v1/private/dts_create")
        self.assertEqual(config.query_url, "https://api-dx.xf-yun.com/v1/private/dts_query")
        self.assertEqual(config.voice, "x4_mingge")
        self.assertTrue(config.configured())


class AudioManifestTests(unittest.TestCase):
    def test_manifest_is_generating_while_tts_is_running(self) -> None:
        class FakeConfig:
            encoding = "lame"

            @staticmethod
            def configured():
                return True

            @staticmethod
            def provider_meta():
                return {"name": "fake-tts"}

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            synth_started = threading.Event()
            release_synth = threading.Event()
            result: dict = {}
            script = {
                "schema_version": "learning.lecture_script.v1",
                "session_id": "session",
                "outline_node_id": "node_1",
                "status": "ready",
                "source_handout_hash": "source-hash",
                "script_path": str(root / "session" / "lecture_scripts" / "node_1.json"),
                "slides": [{
                    "slide_id": "slide_001",
                    "text": "Persisted narration",
                    "text_hash": "text-hash",
                    "text_chars": 19,
                }],
            }

            def synthesize(_text):
                synth_started.set()
                release_synth.wait(5)
                raise RuntimeError("test synthesis stopped")

            def run_audio():
                result.update(ensure_handout_audio(
                    session_id="session",
                    outline_node_id="node_1",
                    handout_section={"title": "Section"},
                    source="test",
                    resource_root=root,
                    lecture_script=script,
                ))

            with (
                patch(
                    "backend.services.learning.audio.XfyunTTSConfig.from_env",
                    return_value=FakeConfig(),
                ),
                patch(
                    "backend.services.learning.audio.XfyunSmartTTSClient.synthesize",
                    side_effect=synthesize,
                ),
            ):
                worker = threading.Thread(target=run_audio)
                worker.start()
                self.assertTrue(synth_started.wait(1))
                manifest = json.loads(
                    (root / "session" / "audio_manifest.json").read_text(encoding="utf-8")
                )
                self.assertEqual(manifest["status"], "generating")
                self.assertEqual(manifest["sections"][0]["status"], "generating")
                self.assertEqual(manifest["sections"][0]["slides"][0]["status"], "pending")
                reconciled = reconcile_audio_manifest(
                    session_id="session",
                    outline_nodes=[{"id": "node_1"}],
                    resource_root=root,
                )
                self.assertEqual(reconciled["status"], "generating")
                self.assertEqual(reconciled["generating_section_ids"], ["node_1"])
                release_synth.set()
                worker.join(2)

            self.assertFalse(worker.is_alive())
            self.assertEqual(result["status"], "failed")

    def test_audio_uses_the_persisted_lecture_script(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = {
                "schema_version": "learning.lecture_script.v1",
                "session_id": "session",
                "outline_node_id": "node_1",
                "status": "ready",
                "source_handout_hash": "source-hash",
                "script_path": str(root / "session" / "lecture_scripts" / "node_1.json"),
                "slides": [{
                    "slide_id": "slide_001",
                    "text": "Persisted narration",
                    "text_hash": "text-hash",
                    "text_chars": 19,
                }],
            }
            with (
                patch.dict(os.environ, {"LEARNING_AUDIO_TTS_ENABLED": "false"}),
                patch(
                    "backend.services.learning.audio.build_and_save_lecture_script",
                    side_effect=AssertionError("lecture script must not be rebuilt"),
                ),
            ):
                result = ensure_handout_audio(
                    session_id="session",
                    outline_node_id="node_1",
                    handout_section={"title": "Section"},
                    source="test",
                    resource_root=root,
                    lecture_script=script,
                )

            self.assertEqual(result["status"], "skipped")
            self.assertEqual(result["source_handout_hash"], "source-hash")
            self.assertEqual(result["lecture_script_path"], script["script_path"])

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
