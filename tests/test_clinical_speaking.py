"""Clinical speaking workflow tests, including the no-fallback contract."""

from __future__ import annotations

import json
import os
import random
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.services.clinical_speaking.agents import (
    MAX_RETRY_RESPONSE_CHARS,
    StrictAgentError,
    _strict_json_call,
    agent_readiness,
    assess_exam_answer,
    build_judge_blueprint,
    normalize_medical_transcript,
    patient_reply,
)
from backend.services.clinical_speaking import agents as clinical_agents
from backend.services.clinical_speaking.case_repository import ClinicalCaseRepository
from backend.services.clinical_speaking.service import (
    MAX_EVIDENCE_ITEMS,
    MAX_EVIDENCE_TOTAL_CHARS,
    ClinicalSpeakingError,
    ClinicalSpeakingService,
)
from backend.services.clinical_speaking.store import ClinicalSpeakingStore
from backend.services.learning.memory import LearningMemory
from backend.services.llm_client import LLMRequestError
from backend.services.xfyun_iat import XfyunIatConfig


CASE = {
    "case_ref": "case_test",
    "diagnosis": "急性冠周炎",
    "diagnosis_aliases": ["急性冠周炎", "智齿冠周炎", "冠周炎"],
    "patient_facts": [
        {"id": "chief_complaint", "label": "主诉", "value": "右下后牙区反复肿痛三天。"},
        {"id": "present_illness", "label": "现病史", "value": "半年前有过类似发作，最近张口不适。"},
    ],
    "clinical_findings": {"exam": "右下第三磨牙部分萌出，冠周软组织红肿。", "aux_exam": ""},
    "department": "口腔颌面外科门诊",
}

PATIENT_DIAGNOSIS_DECISION = {
    "action": "diagnosis_correct",
    "reply": "",
    "supported_fact_ids": [],
    "unknown": False,
    "diagnosis_candidate": "急性冠周炎",
    "trace": {"role": "patient_dialogue_controller", "fallback_used": False},
    "diagnosis_verification": {
        "role": "patient_diagnosis_guard",
        "fallback_used": False,
    },
}


def blueprint() -> dict:
    labels = (
        ("pathogenesis", "病理机制"),
        ("clinical_features", "临床表现与诊断依据"),
        ("differential", "鉴别诊断"),
        ("treatment", "治疗原则"),
        ("risk_followup", "风险与随访"),
    )
    return {
        "questions": [
            {
                "question_id": f"judge_{index}",
                "category": category,
                "label": label,
                "prompt": f"请说明{label}。",
                "max_score": 20,
                "rubric_points": [f"{label}要点一", f"{label}要点二"],
                "evidence_refs": [1],
            }
            for index, (category, label) in enumerate(labels, 1)
        ],
        "trace": {"role": "judge_agent", "fallback_used": False},
    }


class FakeRepository:
    path = Path("fake-cases.jsonl")

    def count(self) -> int:
        return 1

    def diagnoses(self) -> list[str]:
        return ["急性冠周炎"]

    def select(
        self,
        targets: list[str],
        *,
        preferred_diagnosis: str | None = None,
        matched_target: str = "",
    ):
        selected_target = matched_target or (targets[0] if targets else "")
        return dict(CASE), "learner_context" if selected_target else "random", selected_target


class ClinicalSpeakingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.memory = LearningMemory(root / "learning.sqlite3")
        self.store = ClinicalSpeakingStore(root / "clinical.sqlite3")
        evidence = [
            SimpleNamespace(
                source_type="textbook",
                source="牙周病学",
                title=f"证据{index}",
                text=f"急性冠周炎教材证据{index}",
                metadata={"kg_node_id": "kg-pericoronitis"},
            )
            for index in range(1, 5)
        ]
        self.service = ClinicalSpeakingService(
            memory=self.memory,
            repository=FakeRepository(),
            store=self.store,
            retrieval_fn=lambda *args, **kwargs: evidence,
        )

    def tearDown(self) -> None:
        self.memory._conn.close()
        self.temp.cleanup()

    @staticmethod
    def ready_payload() -> dict:
        return {
            "ready": True,
            "checks": {"spark_agents": True, "xfyun_iat": True, "xfyun_avatar": True, "clinical_cases": True},
            "missing": [],
        }

    def start(self) -> dict:
        with patch.object(self.service, "readiness", return_value=self.ready_payload()), patch(
            "backend.services.clinical_speaking.service.build_judge_blueprint",
            return_value=blueprint(),
        ), patch(
            "backend.services.clinical_speaking.service.patient_opening",
            return_value={
                "reply": "医生，我右下后牙肿痛三天了。",
                "supported_fact_ids": ["chief_complaint"],
                "trace": {"role": "patient_agent", "fallback_used": False},
                "grounding": {"role": "patient_grounding_guard", "fallback_used": False},
            },
        ):
            return self.service.start_session("student-1")

    def test_agent_configuration_failure_has_no_fallback(self) -> None:
        with patch.dict(os.environ, {
            "CLINICAL_SPEAKING_LLM_API_KEY": "",
            "CLINICAL_SPEAKING_LLM_BASE_URL": "",
            "CLINICAL_SPEAKING_LLM_MODEL": "",
        }, clear=False):
            result = agent_readiness()
        self.assertFalse(result["configured"])
        self.assertFalse(result["fallback_used"])

    def test_agent_readiness_rejects_wrong_model_configuration(self) -> None:
        with patch.dict(
            os.environ,
            {
                "CLINICAL_SPEAKING_LLM_API_KEY": "key",
                "CLINICAL_SPEAKING_LLM_BASE_URL": "https://example.test/v2",
                "CLINICAL_SPEAKING_LLM_MODEL": "generalv3",
            },
            clear=False,
        ):
            result = agent_readiness()
        self.assertFalse(result["configured"])
        self.assertIn("CLINICAL_SPEAKING_LLM_MODEL=xsparkx2flash", result["missing"])

    def test_agent_readiness_uses_sse_and_retries_empty_stream(self) -> None:
        clinical_env = {
            "CLINICAL_SPEAKING_LLM_API_KEY": "key",
            "CLINICAL_SPEAKING_LLM_BASE_URL": "https://example.test/v2",
            "CLINICAL_SPEAKING_LLM_MODEL": "xsparkx2flash",
        }
        clinical_agents._READINESS_CACHE.clear()
        try:
            with patch.dict(os.environ, clinical_env, clear=False), patch(
                "backend.services.clinical_speaking.agents.chat_completion",
                side_effect=[
                    LLMRequestError("Streaming LLM response contained no text content"),
                    '{"ok": true}',
                ],
            ) as mocked, patch(
                "backend.services.clinical_speaking.agents.classify_llm_error",
                return_value="unknown",
            ), patch("backend.services.clinical_speaking.agents.time.sleep"):
                result = agent_readiness(verify=True)
        finally:
            clinical_agents._READINESS_CACHE.clear()

        self.assertTrue(result["authorized"])
        self.assertEqual(mocked.call_count, 2)
        self.assertTrue(callable(mocked.call_args_list[0].kwargs["stream_callback"]))
        self.assertIsNone(mocked.call_args_list[1].kwargs["stream_callback"])

    def test_readiness_does_not_repeat_short_lived_signed_urls(self) -> None:
        with patch(
            "backend.services.clinical_speaking.service.agent_readiness",
            return_value={"configured": True, "missing": [], "fallback_used": False},
        ), patch(
            "backend.services.clinical_speaking.service.get_public_iat_config",
            return_value={"configured": True, "missing": [], "signed_url": "wss://iat/signed"},
        ), patch(
            "backend.services.clinical_speaking.service.get_public_avatar_config",
            return_value={"configured": True, "missing": [], "signed_url": "wss://avatar/signed"},
        ):
            readiness = self.service.readiness()
        self.assertNotIn("signed_url", str(readiness["providers"]))

    def test_typed_input_skips_speech_terminology_agent(self) -> None:
        with patch(
            "backend.services.clinical_speaking.service.normalize_medical_transcript",
        ) as normalizer:
            result = self.service._prepare_transcript(
                "  我的诊断是急性冠周炎  ",
                ["急性冠周炎"],
                "typed",
            )

        normalizer.assert_not_called()
        self.assertEqual(result["normalized_text"], "我的诊断是急性冠周炎")
        self.assertEqual(result["trace"]["role"], "typed_input")
        self.assertFalse(result["trace"]["fallback_used"])

    def test_speech_input_keeps_terminology_agent(self) -> None:
        normalized = {
            "raw_text": "急性根尖周严",
            "normalized_text": "急性根尖周炎",
            "corrections": [{"from": "严", "to": "炎", "reason": "医学术语纠正"}],
            "trace": {"role": "terminology_agent", "fallback_used": False},
        }
        with patch(
            "backend.services.clinical_speaking.service.normalize_medical_transcript",
            return_value=normalized,
        ) as normalizer:
            result = self.service._prepare_transcript(
                "急性根尖周严",
                ["急性根尖周炎"],
                "speech",
            )

        normalizer.assert_called_once_with("急性根尖周严", ["急性根尖周炎"])
        self.assertEqual(result["normalized_text"], "急性根尖周炎")

    def test_agent_failure_stops_start_without_fake_session(self) -> None:
        with patch.object(self.service, "readiness", return_value=self.ready_payload()), patch(
            "backend.services.clinical_speaking.service.build_judge_blueprint",
            side_effect=StrictAgentError("spark failed", code="spark_request_failed"),
        ):
            with self.assertRaises(ClinicalSpeakingError) as raised:
                self.service.start_session("student-1")
        self.assertEqual(raised.exception.code, "spark_request_failed")
        self.assertEqual(self.store.list_for_user("student-1"), [])

    def test_evidence_context_is_bounded_without_rule_fallback(self) -> None:
        evidence = [
            SimpleNamespace(
                source_type="textbook",
                source="口腔医学教材" * 40,
                title=f"长证据{index}" * 40,
                text=(f"急性冠周炎第{index}条知识库原文。" * 300),
                metadata={
                    "kg_node_id": f"kg-{index}",
                    "large_unused_payload": "x" * 5000,
                },
            )
            for index in range(10)
        ]
        self.service.retrieval_fn = lambda *args, **kwargs: evidence

        private, public = self.service._retrieve_evidence("急性冠周炎")

        self.assertLessEqual(len(private), MAX_EVIDENCE_ITEMS)
        self.assertLessEqual(sum(len(item["text"]) for item in private), MAX_EVIDENCE_TOTAL_CHARS)
        self.assertNotIn("large_unused_payload", str(private))
        self.assertEqual(len(public), len(private))

    def test_evidence_retrieval_covers_all_exam_domains(self) -> None:
        calls: list[str] = []
        evidence = [
            SimpleNamespace(
                id=f"chunk-{index}",
                source_type="textbook",
                source="口腔医学教材",
                title=f"证据{index}",
                text=f"急性冠周炎教材证据{index}",
                metadata={"kg_node_id": "kg-pericoronitis"},
            )
            for index in range(1, 7)
        ]

        def retrieve(query: str, **_: object) -> list[SimpleNamespace]:
            calls.append(query)
            return evidence

        self.service.retrieval_fn = retrieve
        private, _ = self.service._retrieve_evidence("急性冠周炎")

        self.assertEqual(len(calls), 6)
        self.assertEqual(calls[0], "急性冠周炎")
        covered = {domain for item in private for domain in item["domains"]}
        self.assertEqual(
            covered,
            {"pathogenesis", "clinical_features", "differential", "treatment", "risk_followup"},
        )

    def test_domain_terms_take_priority_inside_domain_retrieval(self) -> None:
        pathology = SimpleNamespace(
            id="pathology",
            source_type="textbook",
            source="教材",
            title="急性冠周炎",
            text="急性冠周炎的病理变化和炎症过程。",
            metadata={},
        )
        treatment = SimpleNamespace(
            id="treatment",
            source_type="clinical_norm",
            source="规范",
            title="治疗章节",
            text="急性冠周炎治疗原则包括局部冲洗、引流和感染控制。",
            metadata={},
        )
        extras = [
            SimpleNamespace(
                id=f"extra-{index}",
                source_type="textbook",
                source="教材",
                title=f"急性冠周炎证据{index}",
                text=f"急性冠周炎补充证据{index}。",
                metadata={},
            )
            for index in range(3)
        ]
        self.service.retrieval_fn = lambda *args, **kwargs: [pathology, treatment, *extras]
        evidence, _ = self.service._retrieve_evidence("急性冠周炎")
        treatment_item = next(item for item in evidence if item["title"] == "治疗章节")
        self.assertIn("treatment", treatment_item["domains"])

    def test_token_limit_is_reported_and_never_replaced_with_fake_output(self) -> None:
        with patch(
            "backend.services.clinical_speaking.agents.chat_completion",
            side_effect=LLMRequestError("TokenOverLimitError kernel error;code=1010"),
        ):
            with self.assertRaises(StrictAgentError) as raised:
                _strict_json_call(
                    [{"role": "user", "content": "test"}],
                    lambda data: data,
                )
        self.assertEqual(raised.exception.code, "spark_context_over_limit")

    def test_json_contract_retry_does_not_reinject_unbounded_output(self) -> None:
        responses = ["x" * 10000, '{"ok": true}']
        with patch(
            "backend.services.clinical_speaking.agents.chat_completion",
            side_effect=responses,
        ) as mocked:
            result = _strict_json_call(
                [{"role": "user", "content": "test"}],
                lambda data: data,
            )
        retry_messages = mocked.call_args_list[1].args[0]
        self.assertFalse(any(item["role"] == "assistant" for item in retry_messages))
        retry_message = json.loads(retry_messages[-1]["content"])
        self.assertEqual(len(retry_message["invalid_response_excerpt"]), MAX_RETRY_RESPONSE_CHARS)
        self.assertIn("完整 JSON 根对象", retry_message["instruction"])
        self.assertNotIn("response_format", mocked.call_args_list[0].kwargs)
        self.assertEqual(mocked.call_args_list[0].kwargs["max_tokens"], 4096)
        self.assertEqual(
            mocked.call_args_list[0].kwargs["extra_body"],
            {"search_disable": True},
        )
        self.assertTrue(result["ok"])

    def test_judge_blueprint_retries_with_complete_keyed_contract(self) -> None:
        malformed = {
            "questions": [
                {
                    "category": "pathogenesis",
                    "prompt": "请说明病理机制。",
                    "rubic_points": ["错误拼写一", "错误拼写二"],
                }
            ]
        }
        complete = {
            "questions": {
                category: {
                    "prompt": f"请说明{label}。",
                    "rubric_points": [f"{label}要点一", f"{label}要点二"],
                    "evidence_refs": [1],
                }
                for category, label in (
                    ("pathogenesis", "病理机制"),
                    ("clinical_features", "临床表现与诊断依据"),
                    ("differential", "鉴别诊断"),
                    ("treatment", "治疗原则"),
                    ("risk_followup", "风险与随访"),
                )
            }
        }
        evidence = [
            {
                "index": 1,
                "source": "口腔医学教材",
                "title": "急性冠周炎",
                "text": "急性冠周炎的病因、表现、诊断、鉴别、治疗和随访证据。",
                "metadata": {},
            }
        ]
        with patch(
            "backend.services.clinical_speaking.agents.chat_completion",
            side_effect=[json.dumps(malformed, ensure_ascii=False), json.dumps(complete, ensure_ascii=False)],
        ) as mocked:
            result = build_judge_blueprint("急性冠周炎", evidence)

        self.assertEqual(len(result["questions"]), 5)
        self.assertEqual(result["questions"][0]["evidence_refs"], [1])
        retry_messages = mocked.call_args_list[1].args[0]
        self.assertIn("完整 JSON 根对象", retry_messages[-1]["content"])

    def test_judge_blueprint_repairs_only_invalid_domain_with_agent(self) -> None:
        categories = (
            ("pathogenesis", "病理机制"),
            ("clinical_features", "临床表现与诊断依据"),
            ("differential", "鉴别诊断"),
            ("treatment", "治疗原则"),
            ("risk_followup", "风险与随访"),
        )
        partial = {
            "questions": {
                category: {
                    "prompt": f"请说明{label}。",
                    "rubric_points": [f"{label}要点一", f"{label}要点二"],
                    "evidence_refs": [1],
                }
                for category, label in categories[:-1]
            }
        }
        partial["questions"]["risk_followup"] = {
            "prompt": "一道仅考查风险与随访的口头问题",
            "rubric_points": ["证据直接支持的要点一", "证据直接支持的要点二"],
            "evidence_refs": [1],
        }
        repaired = {
            "prompt": "请说明风险与随访。",
            "rubric_points": ["风险要点", "随访要点"],
            "evidence_refs": [1],
        }
        evidence = [
            {
                "index": 1,
                "source": "口腔医学教材",
                "title": "急性冠周炎",
                "text": "急性冠周炎完整教材证据。",
                "metadata": {},
            }
        ]
        with patch(
            "backend.services.clinical_speaking.agents.chat_completion",
            side_effect=[json.dumps(partial, ensure_ascii=False), json.dumps(repaired, ensure_ascii=False)],
        ) as mocked:
            result = build_judge_blueprint("急性冠周炎", evidence)

        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(len(result["questions"]), 5)
        self.assertEqual(result["trace"]["repaired_domains"], ["risk_followup"])

    def test_judge_blueprint_normalizes_numeric_reference_strings_without_repair(self) -> None:
        complete = {
            "questions": {
                category: {
                    "prompt": f"请结合教材说明{label}的具体内容。",
                    "rubric_points": [f"{label}的医学要点甲", f"{label}的医学要点乙"],
                    "evidence_refs": ["1"],
                }
                for category, label in (
                    ("pathogenesis", "病理机制"),
                    ("clinical_features", "临床表现与诊断依据"),
                    ("differential", "鉴别诊断"),
                    ("treatment", "治疗原则"),
                    ("risk_followup", "风险与随访"),
                )
            }
        }
        evidence = [
            {
                "index": 1,
                "source": "口腔医学教材",
                "title": "急性冠周炎",
                "text": "急性冠周炎完整教材证据。",
                "metadata": {},
            }
        ]
        with patch(
            "backend.services.clinical_speaking.agents.chat_completion",
            return_value=json.dumps(complete, ensure_ascii=False),
        ) as mocked:
            result = build_judge_blueprint("急性冠周炎", evidence)

        self.assertEqual(mocked.call_count, 1)
        self.assertTrue(all(question["evidence_refs"] == [1] for question in result["questions"]))
        self.assertEqual(result["trace"]["repaired_domains"], [])

    def test_transient_agent_error_is_retried_without_fallback(self) -> None:
        with patch(
            "backend.services.clinical_speaking.agents.chat_completion",
            side_effect=[LLMRequestError("temporary network error"), '{"ok": true}'],
        ) as mocked, patch(
            "backend.services.clinical_speaking.agents.classify_llm_error",
            return_value="network",
        ), patch("backend.services.clinical_speaking.agents.time.sleep"):
            result = _strict_json_call(
                [{"role": "user", "content": "test"}],
                lambda data: data,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(mocked.call_count, 2)

    def test_clinical_agent_uses_sse_on_first_request(self) -> None:
        with patch(
            "backend.services.clinical_speaking.agents.chat_completion",
            return_value='{"ok": true}',
        ) as mocked:
            result = _strict_json_call(
                [{"role": "user", "content": "test"}],
                lambda data: data,
                agent_label="患者对话 Agent",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(mocked.call_count, 1)
        self.assertTrue(callable(mocked.call_args.kwargs["stream_callback"]))

    def test_empty_sse_response_is_retried_with_non_stream_transport(self) -> None:
        with patch(
            "backend.services.clinical_speaking.agents.chat_completion",
            side_effect=[
                "",
                '{"ok": true}',
            ],
        ) as mocked, patch("backend.services.clinical_speaking.agents.time.sleep"):
            result = _strict_json_call(
                [{"role": "user", "content": "test"}],
                lambda data: data,
                agent_label="患者对话 Agent",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(mocked.call_count, 2)
        self.assertTrue(callable(mocked.call_args_list[0].kwargs["stream_callback"]))
        self.assertIsNone(mocked.call_args_list[1].kwargs["stream_callback"])

    def test_patient_grounding_failure_is_repaired_by_agent_without_template_reply(self) -> None:
        with patch(
            "backend.services.clinical_speaking.agents._verify_diagnosis_decision",
            return_value={
                "approved": False,
                "reason_code": "not_a_diagnosis_assertion",
                "diagnosis_candidate": "",
                "trace": {"role": "patient_diagnosis_guard", "fallback_used": False},
            },
        ), patch(
            "backend.services.clinical_speaking.agents._strict_json_call",
            side_effect=[
                {
                    "action": "reply",
                    "intent": "question",
                    "diagnosis_candidate": "",
                    "diagnosis_correct": False,
                    "reply": "我还伴有病例中没有记录的发热。",
                    "supported_fact_ids": ["chief_complaint"],
                    "unknown": False,
                    "trace": {"role": "patient_agent", "fallback_used": False},
                    "dialogue_trace": {"role": "patient_dialogue_controller", "fallback_used": False},
                },
                {
                    "grounded": False,
                    "unsupported_claims": ["伴有发热"],
                    "trace": {"role": "patient_grounding_guard", "fallback_used": False},
                },
                {
                    "action": "reply",
                    "intent": "question",
                    "diagnosis_candidate": "",
                    "diagnosis_correct": False,
                    "reply": "我主要就是右下后牙肿痛。",
                    "supported_fact_ids": ["chief_complaint"],
                    "unknown": False,
                    "trace": {"role": "patient_agent", "fallback_used": False},
                    "dialogue_trace": {"role": "patient_dialogue_controller", "fallback_used": False},
                },
                {
                    "grounded": True,
                    "unsupported_claims": [],
                    "trace": {"role": "patient_grounding_guard", "fallback_used": False},
                },
            ],
        ) as mocked:
            result = patient_reply(CASE, [], "还有其他症状吗")
        self.assertEqual(mocked.call_count, 4)
        self.assertEqual(result["action"], "reply")
        self.assertEqual(result["reply"], "我主要就是右下后牙肿痛。")
        self.assertEqual(result["repair_attempts"], 1)
        self.assertFalse(result["trace"]["fallback_used"])

    def test_rejected_diagnosis_handoff_is_repaired_into_patient_dialogue(self) -> None:
        rejected = {
            "approved": False,
            "reason_code": "uncertain_or_question",
            "diagnosis_candidate": "急性冠周炎",
            "trace": {"role": "patient_diagnosis_guard", "fallback_used": False},
        }
        with patch(
            "backend.services.clinical_speaking.agents._verify_diagnosis_decision",
            return_value=rejected,
        ) as verifier, patch(
            "backend.services.clinical_speaking.agents._strict_json_call",
            side_effect=[
                {
                    "action": "diagnosis_correct",
                    "intent": "diagnosis_assertion",
                    "diagnosis_candidate": "急性冠周炎",
                    "diagnosis_correct": True,
                    "reply": "",
                    "supported_fact_ids": [],
                    "unknown": False,
                    "trace": {"role": "patient_agent", "fallback_used": False},
                    "dialogue_trace": {
                        "role": "patient_dialogue_controller",
                        "fallback_used": False,
                    },
                },
                {
                    "action": "reply",
                    "intent": "diagnosis_question",
                    "diagnosis_candidate": "急性冠周炎",
                    "diagnosis_correct": False,
                    "reply": "这个我不太清楚。",
                    "supported_fact_ids": [],
                    "unknown": True,
                    "trace": {"role": "patient_agent", "fallback_used": False},
                    "dialogue_trace": {
                        "role": "patient_dialogue_controller",
                        "fallback_used": False,
                    },
                },
                {
                    "grounded": True,
                    "unsupported_claims": [],
                    "trace": {"role": "patient_grounding_guard", "fallback_used": False},
                },
            ],
        ) as mocked:
            result = patient_reply(CASE, [], "是不是急性冠周炎？")
        self.assertEqual(mocked.call_count, 3)
        self.assertEqual(verifier.call_count, 1)
        self.assertEqual(result["action"], "reply")
        self.assertEqual(result["reply"], "这个我不太清楚。")
        self.assertEqual(result["repair_attempts"], 1)

    def test_patient_unknown_requires_real_boolean(self) -> None:
        invalid = json.dumps(
            {
                "reply": "我不清楚。",
                "supported_fact_ids": [],
                "unknown": "false",
            },
            ensure_ascii=False,
        )
        with patch(
            "backend.services.clinical_speaking.agents.chat_completion",
            return_value=invalid,
        ):
            with self.assertRaises(StrictAgentError) as raised:
                patient_reply(CASE, [], "还有其他症状吗？")
        self.assertEqual(raised.exception.code, "spark_contract_failed")

    def test_patient_reply_cannot_repeat_private_diagnosis(self) -> None:
        leaking = json.dumps(
            {
                "action": "reply",
                "intent": "diagnosis_question",
                "diagnosis_candidate": "急性冠周炎",
                "diagnosis_correct": False,
                "reply": "我不确定自己是不是急性冠周炎。",
                "supported_fact_ids": [],
                "unknown": True,
            },
            ensure_ascii=False,
        )
        with patch(
            "backend.services.clinical_speaking.agents._verify_diagnosis_decision",
            return_value={
                "approved": False,
                "reason_code": "uncertain_or_question",
                "diagnosis_candidate": "急性冠周炎",
                "trace": {"role": "patient_diagnosis_guard", "fallback_used": False},
            },
        ), patch(
            "backend.services.clinical_speaking.agents.chat_completion",
            return_value=leaking,
        ):
            with self.assertRaises(StrictAgentError) as raised:
                patient_reply(CASE, [], "是不是急性冠周炎？")
        self.assertEqual(raised.exception.code, "spark_contract_failed")
        self.assertIn("患者对话 Agent", str(raised.exception))

    def test_patient_diagnosis_gate_runs_in_parallel_with_reply_generation(self) -> None:
        approved = {
            "approved": True,
            "reason_code": "explicit_correct_diagnosis",
            "diagnosis_candidate": "急性冠周炎",
            "trace": {"role": "patient_diagnosis_guard", "fallback_used": False},
        }
        with patch(
            "backend.services.clinical_speaking.agents._verify_diagnosis_decision",
            return_value=approved,
        ), patch(
            "backend.services.clinical_speaking.agents._strict_json_call",
        ) as dialogue_call:
            result = patient_reply(CASE, [], "结合表现，我诊断为急性冠周炎。")

        dialogue_call.assert_called_once()
        self.assertEqual(result["action"], "diagnosis_correct")
        self.assertEqual(result["diagnosis_candidate"], "急性冠周炎")
        self.assertEqual(result["reply"], "")
        self.assertFalse(result["trace"]["fallback_used"])

    def test_terminology_agent_cannot_replace_transcript_with_field_description(self) -> None:
        invalid = json.dumps(
            {
                "normalized_text": "仅纠正明显同音字、近音字和断句，不改写原意",
                "corrections": [],
            },
            ensure_ascii=False,
        )
        with patch(
            "backend.services.clinical_speaking.agents.chat_completion",
            return_value=invalid,
        ):
            with self.assertRaises(StrictAgentError) as raised:
                normalize_medical_transcript("我的初步诊断是根尖周囊肿", ["根尖周囊肿"])
        self.assertEqual(raised.exception.code, "spark_contract_failed")

    def test_judge_assessment_requires_nonempty_evidence_and_array_feedback(self) -> None:
        question = blueprint()["questions"][0]
        evidence = [{"index": 1, "source": "教材", "title": "证据", "text": "医学证据"}]
        invalid = json.dumps(
            {
                "score": 10,
                "critique": "需要改进。",
                "strengths": "表达清楚",
                "missing_points": [],
                "errors": [],
                "evidence_refs": [],
            },
            ensure_ascii=False,
        )
        with patch(
            "backend.services.clinical_speaking.agents.chat_completion",
            return_value=invalid,
        ):
            with self.assertRaises(StrictAgentError) as raised:
                assess_exam_answer("急性冠周炎", question, "回答", evidence)
        self.assertEqual(raised.exception.code, "spark_contract_failed")

    def test_natural_language_diagnosis_is_controlled_by_patient_agent(self) -> None:
        session = self.start()
        normalized = {
            "raw_text": "你这个是急性冠周炎",
            "normalized_text": "你这个是急性冠周炎",
            "corrections": [],
            "trace": {"role": "terminology_agent", "fallback_used": False},
        }
        decision = {
            "action": "diagnosis_correct",
            "reply": "",
            "supported_fact_ids": [],
            "unknown": False,
            "diagnosis_candidate": "急性冠周炎",
            "trace": {"role": "patient_dialogue_controller", "fallback_used": False},
            "diagnosis_verification": {
                "role": "patient_diagnosis_guard",
                "fallback_used": False,
            },
        }
        with patch(
            "backend.services.clinical_speaking.service.normalize_medical_transcript",
            return_value=normalized,
        ), patch(
            "backend.services.clinical_speaking.service.patient_reply",
            return_value=decision,
        ) as patient_mock:
            result = self.service.submit_consultation_turn(
                session["id"], "student-1", "你这个是急性冠周炎"
            )
        patient_mock.assert_called_once()
        self.assertEqual(result["session"]["status"], "diagnosis_confirmation")
        self.assertEqual(result["diagnosis_candidate"]["display_name"], "急性冠周炎")
        self.assertIsNone(result["patient_reply"])

    def test_consultation_uses_patient_agent_and_never_template_fallback(self) -> None:
        session = self.start()
        with patch(
            "backend.services.clinical_speaking.service.normalize_medical_transcript",
            return_value={
                "raw_text": "什么时候开始疼的",
                "normalized_text": "什么时候开始疼的",
                "corrections": [],
                "trace": {"role": "terminology_agent", "fallback_used": False},
            },
        ), patch(
            "backend.services.clinical_speaking.service.patient_reply",
            return_value={
                "action": "reply",
                "reply": "三天前开始疼。",
                "supported_fact_ids": ["chief_complaint"],
                "unknown": False,
                "trace": {"role": "patient_agent", "fallback_used": False},
                "grounding": {"role": "patient_grounding_guard", "fallback_used": False},
            },
        ) as patient_mock:
            result = self.service.submit_consultation_turn(session["id"], "student-1", "什么时候开始疼的")
        patient_mock.assert_called_once()
        self.assertEqual(result["patient_reply"], "三天前开始疼。")
        self.assertFalse(result["fallback_used"])

    def test_consultation_request_id_is_idempotent(self) -> None:
        session = self.start()
        normalized = {
            "raw_text": "什么时候开始疼的？",
            "normalized_text": "什么时候开始疼的？",
            "corrections": [],
            "trace": {"role": "terminology_agent", "fallback_used": False},
        }
        reply = {
            "action": "reply",
            "reply": "三天前开始疼。",
            "supported_fact_ids": ["chief_complaint"],
            "unknown": False,
            "trace": {"role": "patient_agent", "fallback_used": False},
            "grounding": {"role": "patient_grounding_guard", "fallback_used": False},
        }
        with patch(
            "backend.services.clinical_speaking.service.normalize_medical_transcript",
            return_value=normalized,
        ) as normalize_mock, patch(
            "backend.services.clinical_speaking.service.patient_reply",
            return_value=reply,
        ) as patient_mock:
            first = self.service.submit_consultation_turn(
                session["id"], "student-1", "什么时候开始疼的？", "request-turn-0001"
            )
            second = self.service.submit_consultation_turn(
                session["id"], "student-1", "什么时候开始疼的？", "request-turn-0001"
            )
        self.assertEqual(normalize_mock.call_count, 1)
        self.assertEqual(patient_mock.call_count, 1)
        self.assertEqual(first["patient_reply"], second["patient_reply"])
        self.assertEqual(len(self.store.get(session["id"])["turns"]), 3)

    def test_concurrent_duplicate_consultation_is_serialized(self) -> None:
        session = self.start()
        normalized = {
            "raw_text": "什么时候开始疼的？",
            "normalized_text": "什么时候开始疼的？",
            "corrections": [],
            "trace": {"role": "terminology_agent", "fallback_used": False},
        }
        reply = {
            "action": "reply",
            "reply": "三天前开始疼。",
            "supported_fact_ids": ["chief_complaint"],
            "unknown": False,
            "trace": {"role": "patient_agent", "fallback_used": False},
            "grounding": {"role": "patient_grounding_guard", "fallback_used": False},
        }
        with patch(
            "backend.services.clinical_speaking.service.normalize_medical_transcript",
            return_value=normalized,
        ) as normalize_mock, patch(
            "backend.services.clinical_speaking.service.patient_reply",
            return_value=reply,
        ) as patient_mock:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(
                        self.service.submit_consultation_turn,
                        session["id"],
                        "student-1",
                        "什么时候开始疼的？",
                        "request-turn-concurrent-0001",
                    )
                    for _ in range(2)
                ]
                results = [future.result() for future in futures]
        self.assertEqual(normalize_mock.call_count, 1)
        self.assertEqual(patient_mock.call_count, 1)
        self.assertEqual(results[0]["patient_reply"], results[1]["patient_reply"])
        self.assertEqual(len(self.store.get(session["id"])["turns"]), 3)

    def test_start_request_id_is_idempotent(self) -> None:
        with patch.object(self.service, "readiness", return_value=self.ready_payload()), patch(
            "backend.services.clinical_speaking.service.build_judge_blueprint",
            return_value=blueprint(),
        ) as blueprint_mock, patch(
            "backend.services.clinical_speaking.service.patient_opening",
            return_value={
                "reply": "医生，我右下后牙肿痛三天了。",
                "supported_fact_ids": ["chief_complaint"],
                "trace": {"role": "patient_agent", "fallback_used": False},
                "grounding": {"role": "patient_grounding_guard", "fallback_used": False},
            },
        ) as opening_mock:
            first = self.service.start_session("student-1", "request-start-0001")
            second = self.service.start_session("student-1", "request-start-0001")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(blueprint_mock.call_count, 1)
        self.assertEqual(opening_mock.call_count, 1)
        self.assertEqual(len(self.store.list_for_user("student-1")), 1)
        learning_sessions = [
            item
            for item in self.memory.list_sessions_for_user("student-1")
            if item["mode"] == "clinical_speaking"
        ]
        self.assertEqual(len(learning_sessions), 1)
        started_events = self.memory.list_events(
            "student-1",
            session_id=learning_sessions[0]["id"],
            event_types=["clinical_speaking_started"],
        )
        self.assertEqual(len(started_events), 1)

    def test_public_session_does_not_leak_personalized_diagnosis_target(self) -> None:
        session = self.start()
        state = self.store.get(session["id"])
        state["selection"] = {
            "source": "learner_context",
            "matched_target": "急性冠周炎：病理机制",
            "learner_targets": ["急性冠周炎：病理机制"],
            "reason": "该薄弱点与急性冠周炎直接相关",
            "trace": {"fallback_used": False},
        }
        public = self.service.public_session(state)
        self.assertEqual(public["selection"], {"source": "learner_context", "matched": True})
        self.assertNotIn("matched_target", str(public))
        self.assertNotIn("learner_targets", str(public))
        self.assertNotIn("reason", str(public))

    def test_judge_score_updates_profile_and_mastery(self) -> None:
        session = self.start()
        with patch(
            "backend.services.clinical_speaking.service.normalize_medical_transcript",
            return_value={
                "raw_text": "诊断为急性冠周炎",
                "normalized_text": "我的初步诊断是急性冠周炎",
                "corrections": [],
                "trace": {"role": "terminology_agent", "fallback_used": False},
            },
        ), patch(
            "backend.services.clinical_speaking.service.patient_reply",
            return_value=PATIENT_DIAGNOSIS_DECISION,
        ):
            detected = self.service.submit_consultation_turn(session["id"], "student-1", "诊断为急性冠周炎")
        self.assertEqual(detected["session"]["status"], "diagnosis_confirmation")
        confirmed = self.service.confirm_diagnosis(session["id"], "student-1", True)
        self.assertEqual(confirmed["session"]["status"], "oral_exam")

        with patch(
            "backend.services.clinical_speaking.service.normalize_medical_transcript",
            return_value={
                "raw_text": "回答内容",
                "normalized_text": "回答内容",
                "corrections": [],
                "trace": {"role": "terminology_agent", "fallback_used": False},
            },
        ), patch(
            "backend.services.clinical_speaking.service.assess_exam_answer",
            return_value={
                "question_id": "judge_1",
                "category": "pathogenesis",
                "label": "病理机制",
                "score": 8.0,
                "max_score": 20,
                "critique": "病理机制描述不完整。",
                "strengths": [],
                "missing_points": ["缺少感染扩散过程"],
                "errors": [],
                "evidence_refs": [1],
                "trace": {"role": "judge_agent", "fallback_used": False},
            },
        ):
            result = self.service.submit_exam_answer(session["id"], "student-1", "回答内容")

        self.assertEqual(result["assessment"]["score"], 8.0)
        profile = self.memory.get_profile("student-1")
        self.assertIn("急性冠周炎：病理机制", profile["known_weak_points"])
        # Clinical sessions use a separate public id; verify through the linked
        # learning session stored privately instead.
        private = self.store.get(session["id"])
        mastery = self.memory.get_mastery_by_kg_node("student-1", private["learning_session_id"])
        self.assertIn("kg-pericoronitis", mastery)
        self.assertFalse(result["fallback_used"])

    def test_exam_answer_request_id_does_not_advance_twice(self) -> None:
        session = self.start()
        with patch(
            "backend.services.clinical_speaking.service.normalize_medical_transcript",
            return_value={
                "raw_text": "诊断为急性冠周炎",
                "normalized_text": "我的初步诊断是急性冠周炎",
                "corrections": [],
                "trace": {"role": "terminology_agent", "fallback_used": False},
            },
        ), patch(
            "backend.services.clinical_speaking.service.patient_reply",
            return_value=PATIENT_DIAGNOSIS_DECISION,
        ):
            self.service.submit_consultation_turn(session["id"], "student-1", "诊断为急性冠周炎")
        self.service.confirm_diagnosis(session["id"], "student-1", True)

        assessment = {
            "question_id": "judge_1",
            "category": "pathogenesis",
            "label": "病理机制",
            "score": 12.0,
            "max_score": 20,
            "critique": "回答基本正确。",
            "strengths": ["提到感染"],
            "missing_points": ["缺少扩散过程"],
            "errors": [],
            "evidence_refs": [1],
            "trace": {"role": "judge_agent", "fallback_used": False},
        }
        normalized = {
            "raw_text": "回答内容",
            "normalized_text": "回答内容",
            "corrections": [],
            "trace": {"role": "terminology_agent", "fallback_used": False},
        }
        with patch(
            "backend.services.clinical_speaking.service.normalize_medical_transcript",
            return_value=normalized,
        ) as normalize_mock, patch(
            "backend.services.clinical_speaking.service.assess_exam_answer",
            return_value=assessment,
        ) as judge_mock:
            first = self.service.submit_exam_answer(
                session["id"], "student-1", "回答内容", "request-exam-0001"
            )
            second = self.service.submit_exam_answer(
                session["id"], "student-1", "回答内容", "request-exam-0001"
            )

        state = self.store.get(session["id"])
        self.assertEqual(normalize_mock.call_count, 1)
        self.assertEqual(judge_mock.call_count, 1)
        self.assertEqual(state["question_index"], 1)
        self.assertEqual(len(state["assessments"]), 1)
        self.assertEqual(first["assessment"]["question_id"], second["assessment"]["question_id"])
        mastery = self.memory.get_mastery_by_kg_node("student-1", state["learning_session_id"])
        self.assertEqual(next(iter(mastery.values()))["exposure_count"], 1)
        assessed_events = self.memory.list_events(
            "student-1",
            session_id=state["learning_session_id"],
            event_types=["clinical_speaking_answer_assessed"],
        )
        self.assertEqual(len(assessed_events), 1)
        self.assertEqual(assessed_events[0]["payload"]["label"], "病理机制")

    def test_exam_replay_recovers_learning_update_without_reinvoking_agents(self) -> None:
        session = self.start()
        state = self.store.get(session["id"])
        question = state["blueprint"]["questions"][0]
        state["status"] = "oral_exam"
        state["turns"].append(
            {
                "role": "judge",
                "stage": "oral_exam",
                "content": question["prompt"],
                "question_id": question["question_id"],
            }
        )
        self.store.save(state)
        normalized = {
            "raw_text": "answer",
            "normalized_text": "answer",
            "corrections": [],
            "trace": {"role": "terminology_agent", "fallback_used": False},
        }
        assessment = {
            "question_id": "judge_1",
            "category": "pathogenesis",
            "label": question["label"],
            "score": 12.0,
            "max_score": 20,
            "critique": "The answer is partially complete.",
            "strengths": ["One relevant point"],
            "missing_points": ["One missing point"],
            "errors": [],
            "evidence_refs": [1],
            "trace": {"role": "judge_agent", "fallback_used": False},
        }
        with patch(
            "backend.services.clinical_speaking.service.normalize_medical_transcript",
            return_value=normalized,
        ), patch(
            "backend.services.clinical_speaking.service.assess_exam_answer",
            return_value=assessment,
        ), patch.object(
            self.service,
            "_apply_assessment",
            side_effect=RuntimeError("simulated learning-memory interruption"),
        ):
            with self.assertRaises(RuntimeError):
                self.service.submit_exam_answer(
                    session["id"],
                    "student-1",
                    "answer",
                    "request-recovery-0001",
                )

        persisted = self.store.get(session["id"])
        self.assertEqual(persisted["question_index"], 1)
        self.assertEqual(len(persisted["assessments"]), 1)
        with patch(
            "backend.services.clinical_speaking.service.normalize_medical_transcript"
        ) as normalize_mock, patch(
            "backend.services.clinical_speaking.service.assess_exam_answer"
        ) as judge_mock:
            replay = self.service.submit_exam_answer(
                session["id"],
                "student-1",
                "answer",
                "request-recovery-0001",
            )
        normalize_mock.assert_not_called()
        judge_mock.assert_not_called()
        self.assertEqual(replay["assessment"]["question_id"], "judge_1")
        mastery = self.memory.get_mastery_by_kg_node(
            "student-1", persisted["learning_session_id"]
        )
        self.assertEqual(next(iter(mastery.values()))["exposure_count"], 1)

    def test_five_answers_complete_both_clinical_and_learning_sessions(self) -> None:
        session = self.start()
        with patch(
            "backend.services.clinical_speaking.service.normalize_medical_transcript",
            return_value={
                "raw_text": "诊断为急性冠周炎",
                "normalized_text": "我的初步诊断是急性冠周炎",
                "corrections": [],
                "trace": {"role": "terminology_agent", "fallback_used": False},
            },
        ), patch(
            "backend.services.clinical_speaking.service.patient_reply",
            return_value=PATIENT_DIAGNOSIS_DECISION,
        ):
            self.service.submit_consultation_turn(session["id"], "student-1", "诊断为急性冠周炎")
        self.service.confirm_diagnosis(session["id"], "student-1", True)

        assessments = []
        for question in blueprint()["questions"]:
            assessments.append(
                {
                    "question_id": question["question_id"],
                    "category": question["category"],
                    "label": question["label"],
                    "score": 10.0,
                    "max_score": 20,
                    "critique": "需要继续完善。",
                    "strengths": [],
                    "missing_points": ["要点不完整"],
                    "errors": [],
                    "evidence_refs": [1],
                    "trace": {"role": "judge_agent", "fallback_used": False},
                }
            )
        normalized = {
            "raw_text": "回答内容",
            "normalized_text": "回答内容",
            "corrections": [],
            "trace": {"role": "terminology_agent", "fallback_used": False},
        }
        with patch(
            "backend.services.clinical_speaking.service.normalize_medical_transcript",
            return_value=normalized,
        ), patch(
            "backend.services.clinical_speaking.service.assess_exam_answer",
            side_effect=assessments,
        ):
            result = None
            for index in range(5):
                result = self.service.submit_exam_answer(
                    session["id"],
                    "student-1",
                    "回答内容",
                    f"request-exam-{index + 10:04d}",
                )

        self.assertIsNotNone(result)
        self.assertTrue(result["completed"])
        self.assertEqual(result["summary"]["score"], 50.0)
        state = self.store.get(session["id"])
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["question_index"], 5)
        self.assertEqual(len(state["assessments"]), 5)
        learning_session = self.memory.get_session(state["learning_session_id"])
        self.assertEqual(learning_session["status"], "completed")
        completed_events = self.memory.list_events(
            "student-1",
            session_id=state["learning_session_id"],
            event_types=["clinical_speaking_completed"],
        )
        self.assertEqual(len(completed_events), 1)

        self.memory.set_session_status(state["learning_session_id"], "active")
        self.memory._conn.execute(
            "DELETE FROM lesson_events WHERE session_id = ? AND event_type = ?",
            (state["learning_session_id"], "clinical_speaking_completed"),
        )
        self.memory._conn.commit()
        replay = self.service.submit_exam_answer(
            session["id"],
            "student-1",
            "answer",
            "request-exam-0014",
        )
        self.assertTrue(replay["completed"])
        self.assertEqual(
            self.memory.get_session(state["learning_session_id"])["status"],
            "completed",
        )
        completed_events = self.memory.list_events(
            "student-1",
            session_id=state["learning_session_id"],
            event_types=["clinical_speaking_completed"],
        )
        self.assertEqual(len(completed_events), 1)

    def test_validated_blueprint_is_reused_from_persistent_cache(self) -> None:
        evidence = [
            {
                "index": 1,
                "source": "口腔医学教材",
                "title": "急性冠周炎",
                "text": "急性冠周炎完整教材证据。",
                "metadata": {},
                "domains": ["pathogenesis"],
            }
        ]
        with patch(
            "backend.services.clinical_speaking.service.build_judge_blueprint",
            return_value=blueprint(),
        ) as mocked:
            first = self.service._get_or_build_blueprint("急性冠周炎", evidence)
            second = self.service._get_or_build_blueprint("急性冠周炎", evidence)

        self.assertEqual(mocked.call_count, 1)
        self.assertFalse(first["trace"]["cache_hit"])
        self.assertTrue(second["trace"]["cache_hit"])

    def test_invalid_cached_blueprint_is_rebuilt(self) -> None:
        evidence = [
            {
                "index": 1,
                "source": "口腔医学教材",
                "title": "急性冠周炎",
                "text": "急性冠周炎完整教材证据。",
                "metadata": {},
                "domains": ["pathogenesis"],
            }
        ]
        with patch.object(
            self.store,
            "get_blueprint",
            return_value={"questions": [], "trace": {"fallback_used": False}},
        ), patch(
            "backend.services.clinical_speaking.service.build_judge_blueprint",
            return_value=blueprint(),
        ) as builder:
            result = self.service._get_or_build_blueprint("急性冠周炎", evidence)
        self.assertEqual(builder.call_count, 1)
        self.assertEqual(len(result["questions"]), 5)
        self.assertFalse(result["trace"]["cache_hit"])


class CaseRepositoryTests(unittest.TestCase):
    def test_repository_filters_followups_and_direct_identifiers_from_output(self) -> None:
        rows = [
            {
                "case_id": "1",
                "visit_no": "SHOULD-NOT-LEAK",
                "visit_date": "2026-01-01",
                "department": "口腔颌面外科门诊",
                "diagnosis_name": "急性冠周炎",
                "chief_complaint": "右下后牙反复肿痛三天。",
                "present_illness": "三天前开始肿痛，张口时明显不适。",
                "past_history": "否认药物过敏。",
                "exam": "右下第三磨牙冠周组织红肿。",
                "aux_exam": "暂无",
            },
            {
                "case_id": "2",
                "diagnosis_name": "急性冠周炎",
                "chief_complaint": "术后拆线复查。",
                "present_illness": "病史同前，今天复查。",
                "exam": "创口愈合。",
            },
        ]
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "cases.jsonl"
            path.write_text("\n".join(__import__("json").dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")
            repo = ClinicalCaseRepository(path, rng=random.Random(1))
            case, _, _ = repo.select([])
            self.assertEqual(repo.count(), 1)
        self.assertNotIn("visit_no", case)
        self.assertNotIn("SHOULD-NOT-LEAK", str(case))

    def test_repository_excludes_non_disease_labels_from_five_domain_training(self) -> None:
        rows = [
            {
                "case_id": "eligible",
                "diagnosis_name": "牙髓炎",
                "chief_complaint": "右下后牙自发疼痛三天。",
                "present_illness": "夜间疼痛明显并向同侧头面部放散。",
                "exam": "右下磨牙深龋，探诊敏感。",
            },
            {
                "case_id": "finding-only",
                "diagnosis_name": "残冠",
                "chief_complaint": "后牙牙冠缺损要求治疗。",
                "present_illness": "牙冠缺损已经持续半年并影响咀嚼。",
                "exam": "后牙仅剩部分牙冠组织。",
            },
        ]
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "cases.jsonl"
            path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")
            repo = ClinicalCaseRepository(path, rng=random.Random(1))
            self.assertEqual(repo.diagnoses(), ["牙髓炎"])
            self.assertEqual(repo.count(), 1)

    def test_random_selection_chooses_diagnosis_before_case(self) -> None:
        class RecordingRng:
            def __init__(self) -> None:
                self.calls: list[list[object]] = []

            def choice(self, values: list[object]) -> object:
                self.calls.append(list(values))
                return values[-1]

        rows = []
        for index, diagnosis in enumerate(["急性冠周炎", "急性冠周炎", "急性冠周炎", "牙髓炎"], 1):
            rows.append(
                {
                    "case_id": str(index),
                    "diagnosis_name": diagnosis,
                    "chief_complaint": "右下后牙反复疼痛三天。",
                    "present_illness": "三天前开始疼痛，夜间症状更加明显。",
                    "exam": "右下后牙存在明确的口内异常表现。",
                }
            )
        rng = RecordingRng()
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "cases.jsonl"
            path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")
            repo = ClinicalCaseRepository(path, rng=rng)  # type: ignore[arg-type]
            repo.select([])

        self.assertEqual(set(rng.calls[0]), {"急性冠周炎", "牙髓炎"})
        self.assertEqual(len(rng.calls), 2)


class XfyunIatTests(unittest.TestCase):
    def test_browser_final_frame_preserves_buffered_audio_tail(self) -> None:
        script = (
            Path(__file__).resolve().parents[1]
            / "frontend"
            / "js"
            / "clinical-speaking.js"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "iatFrame(recorder.config, 2, finalSamples)",
            script,
            "The final IAT frame must carry PCM buffered after the last 40 ms frame.",
        )

    def test_signed_url_never_exposes_secret(self) -> None:
        config = XfyunIatConfig(
            app_id="app",
            api_key="key",
            api_secret="never-public",
            server_url="wss://iat.example.test/v2/iat",
            language="zh_cn",
            accent="mandarin",
            domain="iat",
            vad_eos=10000,
        )
        payload = config.public_payload()
        self.assertTrue(payload["configured"])
        self.assertNotIn("never-public", str(payload))
        self.assertFalse(payload["fallback_used"])

    def test_avatar_alpha_video_dimensions_are_left_to_the_sdk(self) -> None:
        root = Path(__file__).resolve().parents[1]
        css = (root / "frontend" / "css" / "clinical-speaking.css").read_text(encoding="utf-8")
        script = (root / "frontend" / "js" / "clinical-speaking.js").read_text(encoding="utf-8")

        self.assertNotIn("width: 100% !important", css)
        self.assertNotIn("height: 100% !important", css)
        self.assertIn("alpha: (config.protocol || 'xrtc') === 'xrtc' ? 1 : 0", script)

    def test_composer_is_auto_sizing_and_optimistically_renders_doctor_turn(self) -> None:
        root = Path(__file__).resolve().parents[1]
        css = (root / "frontend" / "css" / "clinical-speaking.css").read_text(encoding="utf-8")
        script = (root / "frontend" / "js" / "clinical-speaking.js").read_text(encoding="utf-8")
        page = (root / "frontend" / "pages" / "clinical-speaking.html").read_text(encoding="utf-8")

        self.assertIn("resize: none", css)
        self.assertNotIn("resize: vertical", css)
        self.assertIn("function resizeTranscriptInput", script)
        self.assertIn("state.optimisticTurn = {", script)
        self.assertIn("你 · 发送中", script)
        self.assertIn('id="doctor-transcript" rows="1"', page)


if __name__ == "__main__":
    unittest.main()
