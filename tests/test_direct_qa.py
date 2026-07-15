"""Tests for Direct QA service.

Covers:
- First question -> KAG + LLM evidence answer (happy path)
- Follow-up with history -> reference resolution, fresh KAG per round
- No history, ambiguous question -> clarification
- No evidence (KAG empty) -> conservative answer
- LLM failure -> unavailable result
- No session, no PPT, no start_lesson / handle_interruption calls
- Evidence validation - only actual KAG chunks in used_evidence
- Output structure integrity
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from backend.services.retrieval.models import EvidenceChunk


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


def _make_evidence_chunk(
    chunk_id: str,
    text: str,
    source: str = "Kou Qiang Yi Xue Jiao Cai",
    source_type: str = "textbook",
    title: str = "",
    score: float = 0.85,
    metadata: dict | None = None,
) -> EvidenceChunk:
    return EvidenceChunk(
        id=chunk_id,
        text=text,
        source=source,
        source_type=source_type,
        title=title or chunk_id,
        score=score,
        metadata=metadata or {},
    )


def _resolution_json(
    *,
    resolved_question: str = "",
    conversation_context_used: bool = False,
    needs_clarification: bool = False,
    clarification_message: str = "",
    search_queries: list | None = None,
) -> str:
    return json.dumps({
        "resolved_question": resolved_question,
        "conversation_context_used": conversation_context_used,
        "needs_clarification": needs_clarification,
        "clarification_message": clarification_message,
        "search_queries": search_queries or [],
    }, ensure_ascii=False)


def _answer_json(
    *,
    answer: str = "",
    evidence_status: str = "sufficient",
    should_reinforce: bool = False,
    confusion_kg_node_ids: list | None = None,
    used_evidence: list | None = None,
) -> str:
    return json.dumps({
        "answer": answer,
        "evidence_status": evidence_status,
        "should_reinforce": should_reinforce,
        "confusion_kg_node_ids": confusion_kg_node_ids or [],
        "used_evidence": used_evidence or [],
    }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class FirstQuestionHappyPathTests(unittest.TestCase):
    """A clear first question without history -> KAG search + evidence answer."""

    def test_first_question_returns_evidence_answer(self) -> None:
        mock_chunks = [
            _make_evidence_chunk("chunk:001", "Pulpitis pain is spontaneous, paroxysmal, nocturnal..."),
            _make_evidence_chunk("chunk:002", "Periapical periodontitis pain is persistent, biting pain..."),
            _make_evidence_chunk("chunk:003", "Key: pulpitis is temperature-sensitive, periapical is percussion-sensitive..."),
        ]

        call_count = [0]

        def mock_llm(messages, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _resolution_json(
                    resolved_question="How to differentiate pulpitis from periapical periodontitis?",
                    conversation_context_used=False,
                    search_queries=[
                        {"query": "pulpitis periapical periodontitis differential diagnosis", "purpose": "differential keys"},
                        {"query": "pulpitis pain characteristics vs periapical", "purpose": "pain comparison"},
                    ],
                )
            else:
                return _answer_json(
                    answer="Differential diagnosis:\n\n1. Pain: pulpitis = spontaneous/nocturnal; periapical = persistent/biting.\n2. Temperature: pulpitis sensitive; periapical not.\n3. Percussion: pulpitis negative; periapical positive.",
                    evidence_status="sufficient",
                    used_evidence=[
                        {"chunk_id": "chunk:001", "source": "textbook", "why_used": "pulpitis pain"},
                        {"chunk_id": "chunk:002", "source": "textbook", "why_used": "periapical pain"},
                        {"chunk_id": "chunk:003", "source": "textbook", "why_used": "differential keys"},
                    ],
                )

        with patch(
            "backend.services.qa.direct_qa.chat_completion", side_effect=mock_llm
        ), patch(
            "backend.services.retrieval.search_knowledge_base", return_value=mock_chunks
        ), patch(
            "backend.services.qa.direct_qa.is_llm_configured", return_value=True
        ):
            from backend.services.qa.direct_qa import answer_direct_question

            result = answer_direct_question(
                message="How to differentiate pulpitis from periapical periodontitis?",
                history=[],
                user_id="user-test-1",
            )

        self.assertIn("pulpitis", result["answer"].lower())
        self.assertIn("periapical", result["answer"].lower())
        self.assertEqual(result["evidence_status"], "sufficient")
        self.assertEqual(len(result["used_evidence"]), 3)
        used_ids = {e["chunk_id"] for e in result["used_evidence"]}
        self.assertTrue(used_ids.issubset({"chunk:001", "chunk:002", "chunk:003"}))

        self.assertEqual(result["trace"]["mode"], "direct_qa_evidence")
        self.assertTrue(result["trace"]["uses_llm"])
        self.assertTrue(result["trace"]["uses_kag"])
        self.assertEqual(result["trace"]["search_result_count"], 3)
        self.assertEqual(result["trace"]["user_id"], "user-test-1")
        self.assertFalse(result["trace"]["history_used"])

        # No session/ppt fields
        self.assertNotIn("session_id", result)
        self.assertNotIn("outline", result)
        self.assertNotIn("ppt", result)

    def test_first_question_with_confusion_detection(self) -> None:
        mock_chunks = [
            _make_evidence_chunk(
                "chunk:010", "Caries etiology includes bacteria, food, host, and time...",
                metadata={"core_node_ids": ["kg-caries-etiology", "kg-caries-pathogenesis"]},
            ),
        ]

        call_count = [0]

        def mock_llm(messages, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _resolution_json(
                    resolved_question="What is the etiology of dental caries?",
                    search_queries=[{"query": "dental caries etiology four factors", "purpose": "caries etiology"}],
                )
            else:
                return _answer_json(
                    answer="Dental caries is caused by the interaction of bacteria, food, host, and time...",
                    evidence_status="sufficient",
                    should_reinforce=True,
                    confusion_kg_node_ids=["kg-caries-etiology", "kg-caries-pathogenesis"],
                    used_evidence=[{"chunk_id": "chunk:010"}],
                )

        with patch(
            "backend.services.qa.direct_qa.chat_completion", side_effect=mock_llm
        ), patch(
            "backend.services.retrieval.search_knowledge_base", return_value=mock_chunks
        ), patch(
            "backend.services.qa.direct_qa.is_llm_configured", return_value=True
        ):
            from backend.services.qa.direct_qa import answer_direct_question

            result = answer_direct_question(
                message="What is the etiology of dental caries?",
                user_id="user-test-2",
            )

        self.assertTrue(result["should_reinforce"])
        self.assertEqual(
            result["confusion_kg_node_ids"],
            ["kg-caries-etiology", "kg-caries-pathogenesis"],
        )


class FollowUpWithHistoryTests(unittest.TestCase):
    """Follow-up questions use history for reference resolution, then re-retrieve KAG."""

    def test_pronoun_reference_resolved_via_history(self) -> None:
        history = [
            {"role": "user", "content": "How to differentiate pulpitis from periapical periodontitis?"},
            {"role": "assistant", "content": "Pulpitis has spontaneous pain; periapical has biting pain..."},
            {"role": "user", "content": "What about the pain characteristics?"},
        ]
        message = "What about the pain characteristics?"

        mock_chunks = [
            _make_evidence_chunk("chunk:100", "Pulpitis pain: spontaneous, paroxysmal, worse at night..."),
            _make_evidence_chunk("chunk:101", "Periapical pain: persistent dull ache, biting pain..."),
        ]

        call_count = [0]

        def mock_llm(messages, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _resolution_json(
                    resolved_question="What are the pain characteristics of pulpitis and periapical periodontitis?",
                    conversation_context_used=True,
                    search_queries=[
                        {"query": "pulpitis pain characteristics", "purpose": "pulpitis pain"},
                        {"query": "periapical periodontitis pain characteristics", "purpose": "periapical pain"},
                    ],
                )
            else:
                return _answer_json(
                    answer="Pulpitis: spontaneous, paroxysmal, worse at night. Periapical: persistent dull ache, biting pain prominent.",
                    evidence_status="sufficient",
                    used_evidence=[
                        {"chunk_id": "chunk:100", "why_used": "pulpitis pain"},
                        {"chunk_id": "chunk:101", "why_used": "periapical pain"},
                    ],
                )

        with patch(
            "backend.services.qa.direct_qa.chat_completion", side_effect=mock_llm
        ), patch(
            "backend.services.retrieval.search_knowledge_base", return_value=mock_chunks
        ), patch(
            "backend.services.qa.direct_qa.is_llm_configured", return_value=True
        ):
            from backend.services.qa.direct_qa import answer_direct_question

            result = answer_direct_question(
                message=message,
                history=history,
                user_id="user-test-3",
            )

        self.assertTrue(result["conversation_context_used"])
        self.assertIn("pain", result["resolved_question"].lower())
        self.assertNotEqual(result["resolved_question"], message)
        self.assertGreaterEqual(len(result["used_evidence"]), 1)
        self.assertEqual(result["trace"]["history_used"], True)
        self.assertEqual(result["trace"]["history_turns"], 3)

    def test_followup_with_two_topics_in_history(self) -> None:
        history = [
            {"role": "user", "content": "What are the classifications of dental caries?"},
            {"role": "assistant", "content": "Caries classified by depth: superficial, medium, deep..."},
            {"role": "user", "content": "What about periodontitis staging?"},
            {"role": "assistant", "content": "Periodontitis stages I-IV..."},
            {"role": "user", "content": "How is deep caries treated?"},
        ]
        message = "How is deep caries treated?"

        mock_chunks = [
            _make_evidence_chunk("chunk:200", "Deep caries treatment: indirect pulp capping, direct pulp capping, root canal..."),
        ]

        call_count = [0]

        def mock_llm(messages, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _resolution_json(
                    resolved_question="What are the treatment methods for deep caries?",
                    conversation_context_used=True,
                    search_queries=[{"query": "deep caries treatment methods", "purpose": "deep caries treatment"}],
                )
            else:
                return _answer_json(
                    answer="Deep caries treatment includes: 1. Indirect pulp capping...",
                    evidence_status="sufficient",
                    used_evidence=[{"chunk_id": "chunk:200"}],
                )

        with patch(
            "backend.services.qa.direct_qa.chat_completion", side_effect=mock_llm
        ), patch(
            "backend.services.retrieval.search_knowledge_base", return_value=mock_chunks
        ), patch(
            "backend.services.qa.direct_qa.is_llm_configured", return_value=True
        ):
            from backend.services.qa.direct_qa import answer_direct_question

            result = answer_direct_question(
                message=message,
                history=history,
                user_id="user-test-4",
            )

        self.assertTrue(result["conversation_context_used"])
        self.assertIn("deep caries", result["resolved_question"].lower())
        self.assertNotIn("periodontitis", result["resolved_question"].lower())

    def test_fresh_kag_search_on_followup(self) -> None:
        history = [
            {"role": "user", "content": "What are the steps of root canal treatment?"},
            {"role": "assistant", "content": "Root canal treatment includes access, preparation, disinfection, obturation."},
            {"role": "user", "content": "What are the precautions for canal preparation?"},
        ]
        message = "What are the precautions for canal preparation?"

        mock_chunks = [
            _make_evidence_chunk("chunk:301", "Canal preparation precautions: working length determination, step-back technique..."),
            _make_evidence_chunk("chunk:302", "Preventing instrument separation during canal preparation..."),
        ]

        call_count = [0]

        def mock_llm(messages, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _resolution_json(
                    resolved_question="What are the precautions for root canal preparation?",
                    conversation_context_used=True,
                    search_queries=[{"query": "root canal preparation precautions", "purpose": "preparation precautions"}],
                )
            else:
                return _answer_json(
                    answer="Canal preparation precautions include working length determination, step-back technique, preventing instrument separation...",
                    evidence_status="sufficient",
                    used_evidence=[
                        {"chunk_id": "chunk:301"},
                        {"chunk_id": "chunk:302"},
                    ],
                )

        with patch(
            "backend.services.qa.direct_qa.chat_completion", side_effect=mock_llm
        ), patch(
            "backend.services.retrieval.search_knowledge_base",
            return_value=mock_chunks,
        ) as mock_kag, patch(
            "backend.services.qa.direct_qa.is_llm_configured", return_value=True
        ):
            from backend.services.qa.direct_qa import answer_direct_question

            result = answer_direct_question(
                message=message,
                history=history,
                user_id="user-test-5",
            )

        mock_kag.assert_called()
        self.assertTrue(result["conversation_context_used"])
        used_ids = {e["chunk_id"] for e in result["used_evidence"]}
        self.assertIn("chunk:301", used_ids)
        self.assertIn("chunk:302", used_ids)


class NoHistoryAmbiguousClarificationTests(unittest.TestCase):
    """Ambiguous question without sufficient history -> clarification requested."""

    def test_vague_question_without_history_requests_clarification(self) -> None:
        def mock_llm(messages, **kwargs):
            return _resolution_json(
                resolved_question="How to handle that?",
                needs_clarification=True,
                clarification_message="Sorry, I am not sure what 'that' refers to. Could you please describe your question in more detail?",
                search_queries=[],
            )

        with patch(
            "backend.services.qa.direct_qa.chat_completion", side_effect=mock_llm
        ), patch(
            "backend.services.qa.direct_qa.is_llm_configured", return_value=True
        ):
            from backend.services.qa.direct_qa import answer_direct_question

            result = answer_direct_question(
                message="How to handle that?",
                history=[],
                user_id="user-test-6",
            )

        self.assertEqual(result["trace"]["mode"], "clarification_needed")
        self.assertFalse(result["conversation_context_used"])
        self.assertEqual(result["evidence_status"], "none")
        self.assertEqual(result["used_evidence"], [])
        self.assertFalse(result["should_reinforce"])
        self.assertIn("not sure", result["answer"].lower())

    def test_vague_with_irrelevant_history_requests_clarification(self) -> None:
        history = [
            {"role": "user", "content": "How is the weather today?"},
            {"role": "assistant", "content": "I am a dental assistant, cannot answer weather questions."},
        ]

        def mock_llm(messages, **kwargs):
            return _resolution_json(
                resolved_question="What to do about this?",
                conversation_context_used=False,
                needs_clarification=True,
                clarification_message="What oral problem are you referring to? Please describe your specific symptoms or questions.",
                search_queries=[],
            )

        with patch(
            "backend.services.qa.direct_qa.chat_completion", side_effect=mock_llm
        ), patch(
            "backend.services.qa.direct_qa.is_llm_configured", return_value=True
        ):
            from backend.services.qa.direct_qa import answer_direct_question

            result = answer_direct_question(
                message="What to do about this?",
                history=history,
                user_id="user-test-7",
            )

        self.assertEqual(result["trace"]["mode"], "clarification_needed")


class NoEvidenceConservativeAnswerTests(unittest.TestCase):
    """KAG returns no results -> conservative answer without fabricated evidence."""

    def test_empty_kag_returns_conservative_answer(self) -> None:
        call_count = [0]

        def mock_llm(messages, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _resolution_json(
                    resolved_question="What is the malignant transformation rate of oral leukoplakia?",
                    search_queries=[{"query": "oral leukoplakia malignant transformation rate", "purpose": "transformation rate data"}],
                )
            else:
                return _answer_json(
                    answer="Regarding the malignant transformation rate of oral leukoplakia, the provided evidence does not contain specific data. Please consult an oral medicine specialist for evaluation.",
                    evidence_status="insufficient",
                    should_reinforce=False,
                    used_evidence=[],
                )

        with patch(
            "backend.services.qa.direct_qa.chat_completion", side_effect=mock_llm
        ), patch(
            "backend.services.retrieval.search_knowledge_base", return_value=[]
        ), patch(
            "backend.services.qa.direct_qa.is_llm_configured", return_value=True
        ):
            from backend.services.qa.direct_qa import answer_direct_question

            result = answer_direct_question(
                message="What is the malignant transformation rate of oral leukoplakia?",
                user_id="user-test-8",
            )

        self.assertEqual(result["evidence_status"], "insufficient")
        self.assertEqual(result["used_evidence"], [])
        self.assertFalse(result["should_reinforce"])
        self.assertEqual(result["trace"]["search_result_count"], 0)

    def test_kag_error_returns_conservative_answer(self) -> None:
        call_count = [0]

        def mock_llm(messages, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _resolution_json(
                    resolved_question="How long do dental implants last?",
                    search_queries=[{"query": "dental implant lifespan", "purpose": "implant longevity"}],
                )
            else:
                return _answer_json(
                    answer="Regarding dental implant lifespan, evidence data cannot be retrieved at this time. Please refer to clinical research literature or consult an implant specialist.",
                    evidence_status="insufficient",
                    should_reinforce=False,
                    used_evidence=[],
                )

        with patch(
            "backend.services.qa.direct_qa.chat_completion", side_effect=mock_llm
        ), patch(
            "backend.services.retrieval.search_knowledge_base",
            side_effect=RuntimeError("KAG backend unavailable"),
        ), patch(
            "backend.services.qa.direct_qa.is_llm_configured", return_value=True
        ):
            from backend.services.qa.direct_qa import answer_direct_question

            result = answer_direct_question(
                message="How long do dental implants last?",
                user_id="user-test-9",
            )

        self.assertIn("evidence_status", result)
        self.assertEqual(result["trace"]["search_result_count"], 0)


class LLMFailureTests(unittest.TestCase):
    """LLM failures -> unavailable result, no persistence, no fake data."""

    def test_resolution_llm_failure_returns_unavailable(self) -> None:
        with patch(
            "backend.services.qa.direct_qa.chat_completion",
            side_effect=RuntimeError("LLM connection timeout"),
        ), patch(
            "backend.services.qa.direct_qa.is_llm_configured", return_value=True
        ):
            from backend.services.qa.direct_qa import answer_direct_question

            result = answer_direct_question(
                message="How to treat periodontitis?",
                user_id="user-test-10",
            )

        self.assertEqual(result["trace"]["mode"], "unavailable")
        self.assertIn("resolution_llm_failed", result["trace"]["detail"])
        self.assertFalse(result["should_reinforce"])
        self.assertEqual(result["evidence_status"], "none")
        self.assertEqual(result["used_evidence"], [])
        self.assertEqual(result["confusion_kg_node_ids"], [])

    def test_answer_llm_failure_returns_unavailable(self) -> None:
        call_count = [0]

        def mock_llm(messages, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _resolution_json(
                    resolved_question="What is the etiology of periodontitis?",
                    search_queries=[{"query": "periodontitis etiology", "purpose": "periodontitis cause"}],
                )
            else:
                raise RuntimeError("LLM answer generation failed")

        mock_chunks = [
            _make_evidence_chunk("chunk:400", "Periodontitis etiology includes plaque biofilm, calculus, systemic factors..."),
        ]

        with patch(
            "backend.services.qa.direct_qa.chat_completion", side_effect=mock_llm
        ), patch(
            "backend.services.retrieval.search_knowledge_base", return_value=mock_chunks
        ), patch(
            "backend.services.qa.direct_qa.is_llm_configured", return_value=True
        ):
            from backend.services.qa.direct_qa import answer_direct_question

            result = answer_direct_question(
                message="What is the etiology of periodontitis?",
                user_id="user-test-11",
            )

        self.assertEqual(result["trace"]["mode"], "unavailable")
        self.assertIn("answer_llm_failed", result["trace"]["detail"])
        self.assertEqual(result["trace"]["search_result_count"], 1)

    def test_no_llm_configured_returns_unavailable(self) -> None:
        with patch(
            "backend.services.qa.direct_qa.is_llm_configured", return_value=False
        ):
            from backend.services.qa.direct_qa import answer_direct_question

            result = answer_direct_question(
                message="How to treat periodontitis?",
                user_id="user-test-12",
            )

        self.assertEqual(result["trace"]["mode"], "unavailable")
        self.assertIn("no_llm", result["trace"]["detail"])

    def test_answer_json_parse_failed_returns_unavailable(self) -> None:
        call_count = [0]

        def mock_llm(messages, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _resolution_json(
                    resolved_question="How to treat dental fluorosis?",
                    search_queries=[{"query": "dental fluorosis treatment", "purpose": "fluorosis treatment"}],
                )
            else:
                return "This is not JSON, just plain text"

        mock_chunks = [
            _make_evidence_chunk("chunk:500", "Dental fluorosis treatment includes resin infiltration, veneer restoration..."),
        ]

        with patch(
            "backend.services.qa.direct_qa.chat_completion", side_effect=mock_llm
        ), patch(
            "backend.services.retrieval.search_knowledge_base", return_value=mock_chunks
        ), patch(
            "backend.services.qa.direct_qa.is_llm_configured", return_value=True
        ):
            from backend.services.qa.direct_qa import answer_direct_question

            result = answer_direct_question(
                message="How to treat dental fluorosis?",
                user_id="user-test-13",
            )

        self.assertEqual(result["trace"]["mode"], "unavailable")
        self.assertIn("json_parse_failed", result["trace"]["detail"])

    def test_empty_answer_returns_unavailable(self) -> None:
        call_count = [0]

        def mock_llm(messages, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _resolution_json(
                    resolved_question="Must wisdom teeth be extracted?",
                    search_queries=[{"query": "wisdom teeth extraction indications", "purpose": "extraction indications"}],
                )
            else:
                return _answer_json(answer="")

        mock_chunks = [
            _make_evidence_chunk("chunk:600", "Wisdom teeth extraction indications: impaction, recurrent pericoronitis..."),
        ]

        with patch(
            "backend.services.qa.direct_qa.chat_completion", side_effect=mock_llm
        ), patch(
            "backend.services.retrieval.search_knowledge_base", return_value=mock_chunks
        ), patch(
            "backend.services.qa.direct_qa.is_llm_configured", return_value=True
        ):
            from backend.services.qa.direct_qa import answer_direct_question

            result = answer_direct_question(
                message="Must wisdom teeth be extracted?",
                user_id="user-test-14",
            )

        self.assertEqual(result["trace"]["mode"], "unavailable")
        self.assertIn("answer_empty", result["trace"]["detail"])


class NoSessionNoPPTProofTests(unittest.TestCase):
    """Verify the Direct QA service never calls session/PPT/orchestrator APIs."""

    def test_result_has_no_session_or_ppt_fields(self) -> None:
        mock_chunks = [
            _make_evidence_chunk("chunk:700", "Recurrent aphthous ulcer treatment includes topical and systemic medications..."),
        ]

        call_count = [0]

        def mock_llm(messages, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _resolution_json(
                    resolved_question="How to treat recurrent aphthous ulcer?",
                    search_queries=[{"query": "recurrent aphthous ulcer treatment", "purpose": "ulcer treatment"}],
                )
            else:
                return _answer_json(
                    answer="Recurrent aphthous ulcer treatment includes...",
                    evidence_status="sufficient",
                    used_evidence=[{"chunk_id": "chunk:700"}],
                )

        with patch(
            "backend.services.qa.direct_qa.chat_completion", side_effect=mock_llm
        ), patch(
            "backend.services.retrieval.search_knowledge_base", return_value=mock_chunks
        ), patch(
            "backend.services.qa.direct_qa.is_llm_configured", return_value=True
        ):
            from backend.services.qa.direct_qa import answer_direct_question

            result = answer_direct_question(
                message="How to treat recurrent aphthous ulcer?",
                user_id="user-test-15",
            )

        forbidden = {
            "session_id", "ppt", "pptx", "outline", "slides",
            "advance_progress", "content_status", "question_scope",
            "related_outline_node_id", "handout_section",
        }
        result_keys = set(result.keys())
        self.assertTrue(
            result_keys.isdisjoint(forbidden),
            f"Result contains forbidden keys: {result_keys & forbidden}",
        )

    def test_service_imports_no_orchestrator_or_session_modules(self) -> None:
        import ast
        from pathlib import Path

        src = Path(__file__).resolve().parents[1] / "backend" / "services" / "qa" / "direct_qa.py"
        source = src.read_text(encoding="utf-8")
        tree = ast.parse(source)

        forbidden_imports = {
            "orchestrator", "start_lesson", "handle_interruption",
            "LearningOrchestrator", "MasterRouter",
        }

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name.lower()
                    for forbidden in forbidden_imports:
                        self.assertNotIn(forbidden.lower(), name,
                            f"Module imports forbidden symbol: {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    module_lower = node.module.lower()
                    for forbidden in forbidden_imports:
                        self.assertNotIn(forbidden.lower(), module_lower,
                            f"Module imports from forbidden module: {node.module}")
                    for alias in node.names:
                        name = alias.name.lower()
                        for forbidden in forbidden_imports:
                            self.assertNotIn(forbidden.lower(), name,
                                f"Module imports forbidden symbol: {alias.name}")

    def test_second_question_fresh_search_not_cached_answer(self) -> None:
        mock_chunks_1 = [
            _make_evidence_chunk("chunk:801", "Periodontal basic treatment includes oral hygiene instruction..."),
        ]
        mock_chunks_2 = [
            _make_evidence_chunk("chunk:802", "Periodontal surgical treatment includes flap surgery..."),
        ]

        chunk_store = {"call": 0}

        def mock_kag(query, **kwargs):
            chunk_store["call"] += 1
            if chunk_store["call"] == 1:
                return mock_chunks_1
            else:
                return mock_chunks_2

        llm_call_count = [0]

        def mock_llm(messages, **kwargs):
            llm_call_count[0] += 1
            if llm_call_count[0] in (1, 3):
                return _resolution_json(
                    resolved_question="Periodontal treatment question",
                    search_queries=[{"query": "periodontal treatment", "purpose": "treatment"}],
                )
            else:
                return _answer_json(
                    answer="Periodontal treatment includes basic and surgical approaches...",
                    evidence_status="sufficient",
                    used_evidence=[{"chunk_id": "chunk:801"}, {"chunk_id": "chunk:802"}],
                )

        with patch(
            "backend.services.qa.direct_qa.chat_completion", side_effect=mock_llm
        ), patch(
            "backend.services.retrieval.search_knowledge_base", side_effect=mock_kag
        ), patch(
            "backend.services.qa.direct_qa.is_llm_configured", return_value=True
        ):
            from backend.services.qa.direct_qa import answer_direct_question

            result1 = answer_direct_question(
                message="What are the basic treatments for periodontitis?",
                user_id="user-test-16",
            )
            result2 = answer_direct_question(
                message="What are the surgical treatments for periodontitis?",
                user_id="user-test-16",
            )

        self.assertGreaterEqual(chunk_store["call"], 2)
        self.assertEqual(result1["trace"]["mode"], "direct_qa_evidence")
        self.assertEqual(result2["trace"]["mode"], "direct_qa_evidence")


class EvidenceValidationTests(unittest.TestCase):
    """used_evidence must only contain chunk_ids from actual KAG results."""

    def test_llm_cited_fake_chunk_ids_are_stripped(self) -> None:
        mock_chunks = [
            _make_evidence_chunk("chunk:real-1", "Actual retrieved evidence..."),
            _make_evidence_chunk("chunk:real-2", "Another actual evidence..."),
        ]

        call_count = [0]

        def mock_llm(messages, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _resolution_json(
                    resolved_question="What are the clinical manifestations of oral lichen planus?",
                    search_queries=[{"query": "oral lichen planus clinical manifestations", "purpose": "manifestations"}],
                )
            else:
                return _answer_json(
                    answer="Oral lichen planus clinical manifestations include...",
                    evidence_status="sufficient",
                    used_evidence=[
                        {"chunk_id": "chunk:real-1", "why_used": "actual evidence"},
                        {"chunk_id": "chunk:fake-999", "why_used": "hallucinated evidence"},
                        {"chunk_id": "chunk:real-2", "why_used": "actual evidence"},
                    ],
                )

        with patch(
            "backend.services.qa.direct_qa.chat_completion", side_effect=mock_llm
        ), patch(
            "backend.services.retrieval.search_knowledge_base", return_value=mock_chunks
        ), patch(
            "backend.services.qa.direct_qa.is_llm_configured", return_value=True
        ):
            from backend.services.qa.direct_qa import answer_direct_question

            result = answer_direct_question(
                message="What are the clinical manifestations of oral lichen planus?",
                user_id="user-test-17",
            )

        used_ids = {e["chunk_id"] for e in result["used_evidence"]}
        self.assertIn("chunk:real-1", used_ids)
        self.assertIn("chunk:real-2", used_ids)
        self.assertNotIn("chunk:fake-999", used_ids)
        self.assertEqual(len(result["used_evidence"]), 2)

    def test_llm_cites_only_fake_chunks_all_stripped(self) -> None:
        mock_chunks = [
            _make_evidence_chunk("chunk:real-only", "The only real evidence..."),
        ]

        call_count = [0]

        def mock_llm(messages, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _resolution_json(
                    resolved_question="What is the etiology of jaw osteomyelitis?",
                    search_queries=[{"query": "jaw osteomyelitis etiology", "purpose": "osteomyelitis cause"}],
                )
            else:
                return _answer_json(
                    answer="Jaw osteomyelitis is divided into suppurative and non-suppurative...",
                    evidence_status="sufficient",
                    used_evidence=[
                        {"chunk_id": "chunk:fake-A", "why_used": "fake A"},
                        {"chunk_id": "chunk:fake-B", "why_used": "fake B"},
                    ],
                )

        with patch(
            "backend.services.qa.direct_qa.chat_completion", side_effect=mock_llm
        ), patch(
            "backend.services.retrieval.search_knowledge_base", return_value=mock_chunks
        ), patch(
            "backend.services.qa.direct_qa.is_llm_configured", return_value=True
        ):
            from backend.services.qa.direct_qa import answer_direct_question

            result = answer_direct_question(
                message="What is the etiology of jaw osteomyelitis?",
                user_id="user-test-18",
            )

        self.assertEqual(result["used_evidence"], [])

    def test_empty_kag_llm_fabricates_citations_all_stripped(self) -> None:
        """KAG returns zero chunks, LLM fabricates used_evidence -> all stripped."""
        call_count = [0]

        def mock_llm(messages, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _resolution_json(
                    resolved_question="What is the prevalence of oral submucous fibrosis?",
                    search_queries=[{"query": "oral submucous fibrosis prevalence", "purpose": "prevalence data"}],
                )
            else:
                return _answer_json(
                    answer="The prevalence of oral submucous fibrosis varies by region...",
                    evidence_status="sufficient",
                    used_evidence=[
                        {"chunk_id": "chunk:fake-osf-1", "why_used": "prevalence stats"},
                        {"chunk_id": "chunk:fake-osf-2", "why_used": "regional data"},
                        {"chunk_id": "chunk:fake-osf-3", "why_used": "risk factors"},
                    ],
                )

        with patch(
            "backend.services.qa.direct_qa.chat_completion", side_effect=mock_llm
        ), patch(
            "backend.services.retrieval.search_knowledge_base", return_value=[]
        ), patch(
            "backend.services.qa.direct_qa.is_llm_configured", return_value=True
        ):
            from backend.services.qa.direct_qa import answer_direct_question

            result = answer_direct_question(
                message="What is the prevalence of oral submucous fibrosis?",
                user_id="user-test-empty-kag",
            )

        # Empty KAG means no evidence is valid — all LLM citations must be stripped
        self.assertEqual(result["used_evidence"], [],
                         "Empty KAG results must reject ALL LLM-fabricated used_evidence")
        self.assertEqual(result["trace"]["search_result_count"], 0)
        # evidence_status should be downgraded from "sufficient" since no evidence exists
        self.assertNotEqual(result["evidence_status"], "sufficient",
                            "evidence_status must not be 'sufficient' when KAG returns no chunks")


class OutputStructureIntegrityTests(unittest.TestCase):
    """Every result dict must contain all required top-level keys."""

    REQUIRED_KEYS = {
        "answer", "resolved_question", "conversation_context_used",
        "used_evidence", "evidence_status", "should_reinforce",
        "confusion_kg_node_ids", "trace",
    }

    def test_success_result_has_all_required_keys(self) -> None:
        mock_chunks = [_make_evidence_chunk("chunk:900", "Tooth defect restoration methods include...")]

        call_count = [0]

        def mock_llm(messages, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _resolution_json(
                    resolved_question="What are the restoration methods for tooth defects?",
                    search_queries=[{"query": "tooth defect restoration", "purpose": "restoration methods"}],
                )
            else:
                return _answer_json(
                    answer="Tooth defect restoration methods include filling, inlay, full crown, etc...",
                    evidence_status="sufficient",
                    used_evidence=[{"chunk_id": "chunk:900"}],
                )

        with patch(
            "backend.services.qa.direct_qa.chat_completion", side_effect=mock_llm
        ), patch(
            "backend.services.retrieval.search_knowledge_base", return_value=mock_chunks
        ), patch(
            "backend.services.qa.direct_qa.is_llm_configured", return_value=True
        ):
            from backend.services.qa.direct_qa import answer_direct_question

            result = answer_direct_question(
                message="What are the restoration methods for tooth defects?",
                user_id="user-test-19",
            )

        for key in self.REQUIRED_KEYS:
            self.assertIn(key, result, f"Missing required key: {key}")

        self.assertIsInstance(result["answer"], str)
        self.assertIsInstance(result["resolved_question"], str)
        self.assertIsInstance(result["conversation_context_used"], bool)
        self.assertIsInstance(result["used_evidence"], list)
        self.assertIsInstance(result["evidence_status"], str)
        self.assertIsInstance(result["should_reinforce"], bool)
        self.assertIsInstance(result["confusion_kg_node_ids"], list)
        self.assertIsInstance(result["trace"], dict)

    def test_unavailable_result_has_all_required_keys(self) -> None:
        with patch(
            "backend.services.qa.direct_qa.is_llm_configured", return_value=False
        ):
            from backend.services.qa.direct_qa import answer_direct_question

            result = answer_direct_question(
                message="Any question",
                user_id="user-test-20",
            )

        for key in self.REQUIRED_KEYS:
            self.assertIn(key, result, f"Missing required key: {key}")

    def test_clarification_result_has_all_required_keys(self) -> None:
        def mock_llm(messages, **kwargs):
            return _resolution_json(
                resolved_question="What is that?",
                needs_clarification=True,
                clarification_message="What are you referring to?",
                search_queries=[],
            )

        with patch(
            "backend.services.qa.direct_qa.chat_completion", side_effect=mock_llm
        ), patch(
            "backend.services.qa.direct_qa.is_llm_configured", return_value=True
        ):
            from backend.services.qa.direct_qa import answer_direct_question

            result = answer_direct_question(
                message="What is that?",
                user_id="user-test-21",
            )

        for key in self.REQUIRED_KEYS:
            self.assertIn(key, result, f"Missing required key: {key}")


class HistoryNormalizationTests(unittest.TestCase):
    """History is normalized to max 8 turns, role+content only."""

    def test_history_trimmed_to_8_turns(self) -> None:
        mock_chunks = [_make_evidence_chunk("chunk:950", "Dental implant indications...")]

        long_history = [
            {"role": "user", "content": f"Question {i}"}
            for i in range(15)
        ]

        captured_history_len = []
        call_count = [0]

        def mock_llm(messages, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                parsed = json.loads(messages[-1]["content"])
                captured_history_len.append(len(parsed.get("history", [])))
                return _resolution_json(
                    resolved_question="Last question?",
                    search_queries=[{"query": "test", "purpose": "test"}],
                )
            else:
                return _answer_json(
                    answer="Answer content...",
                    evidence_status="sufficient",
                    used_evidence=[{"chunk_id": "chunk:950"}],
                )

        with patch(
            "backend.services.qa.direct_qa.chat_completion", side_effect=mock_llm
        ), patch(
            "backend.services.retrieval.search_knowledge_base", return_value=mock_chunks
        ), patch(
            "backend.services.qa.direct_qa.is_llm_configured", return_value=True
        ):
            from backend.services.qa.direct_qa import answer_direct_question

            answer_direct_question(
                message="Last question?",
                history=long_history,
                user_id="user-test-22",
            )

        self.assertEqual(captured_history_len[0], 8)

    def test_invalid_history_entries_filtered(self) -> None:
        mock_chunks = [_make_evidence_chunk("chunk:960", "Test evidence...")]

        messy_history = [
            {"role": "user", "content": "Valid question 1"},
            "not a dict",
            {"role": "bot", "content": "Invalid role"},
            {"role": "user", "content": ""},
            {"role": "assistant", "content": "Valid answer"},
            None,
            {"no_role": "missing"},
        ]

        captured_history_len = []
        call_count = [0]

        def mock_llm(messages, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                parsed = json.loads(messages[-1]["content"])
                captured_history_len.append(len(parsed.get("history", [])))
                return _resolution_json(
                    resolved_question="Valid question 1",
                    search_queries=[{"query": "test", "purpose": "test"}],
                )
            else:
                return _answer_json(
                    answer="Answer",
                    evidence_status="sufficient",
                    used_evidence=[{"chunk_id": "chunk:960"}],
                )

        with patch(
            "backend.services.qa.direct_qa.chat_completion", side_effect=mock_llm
        ), patch(
            "backend.services.retrieval.search_knowledge_base", return_value=mock_chunks
        ), patch(
            "backend.services.qa.direct_qa.is_llm_configured", return_value=True
        ):
            from backend.services.qa.direct_qa import answer_direct_question

            answer_direct_question(
                message="Valid question 1",
                history=messy_history,
                user_id="user-test-23",
            )

        self.assertEqual(captured_history_len[0], 2)


class IsDirectQAAvailableTests(unittest.TestCase):
    """is_direct_qa_available checks LLM + KAG readiness."""

    def test_available_when_both_configured(self) -> None:
        with patch(
            "backend.services.qa.direct_qa.is_llm_configured", return_value=True
        ):
            from backend.services.qa.direct_qa import is_direct_qa_available
            result = is_direct_qa_available()
            self.assertTrue(result)

    def test_unavailable_when_no_llm(self) -> None:
        with patch(
            "backend.services.qa.direct_qa.is_llm_configured", return_value=False
        ):
            from backend.services.qa.direct_qa import is_direct_qa_available
            result = is_direct_qa_available()
            self.assertFalse(result)


class TwoStructuredTopicsTests(unittest.TestCase):
    """At least two structurally different oral topics - not sharing KG ids or array indices."""

    def test_topic_periodontics(self) -> None:
        mock_chunks = [
            _make_evidence_chunk(
                "chunk:perio-1", "Glickman classification of periodontitis...",
                metadata={"core_node_ids": ["kg-perio-glickman"]},
            ),
            _make_evidence_chunk("chunk:perio-2", "Armitage classification of periodontitis..."),
        ]

        call_count = [0]

        def mock_llm(messages, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _resolution_json(
                    resolved_question="What is the classification standard for periodontitis?",
                    search_queries=[{"query": "periodontitis Glickman classification", "purpose": "classification"}],
                )
            else:
                return _answer_json(
                    answer="Periodontitis classification includes Glickman and Armitage systems...",
                    evidence_status="sufficient",
                    should_reinforce=True,
                    confusion_kg_node_ids=["kg-perio-glickman"],
                    used_evidence=[
                        {"chunk_id": "chunk:perio-1"},
                        {"chunk_id": "chunk:perio-2"},
                    ],
                )

        with patch(
            "backend.services.qa.direct_qa.chat_completion", side_effect=mock_llm
        ), patch(
            "backend.services.retrieval.search_knowledge_base", return_value=mock_chunks
        ), patch(
            "backend.services.qa.direct_qa.is_llm_configured", return_value=True
        ):
            from backend.services.qa.direct_qa import answer_direct_question

            result = answer_direct_question(
                message="What is the classification standard for periodontitis?",
                user_id="user-perio-1",
            )

        self.assertIn("periodontitis", result["answer"].lower())
        self.assertTrue(result["should_reinforce"])
        self.assertIn("kg-perio-glickman", result["confusion_kg_node_ids"])

    def test_topic_endodontics(self) -> None:
        mock_chunks = [
            _make_evidence_chunk("chunk:endo-1", "Working length determination methods include radiographic method..."),
            _make_evidence_chunk("chunk:endo-2", "Apex locator principles and usage..."),
        ]

        call_count = [0]

        def mock_llm(messages, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _resolution_json(
                    resolved_question="How to determine working length in root canal treatment?",
                    search_queries=[{"query": "root canal working length determination", "purpose": "working length"}],
                )
            else:
                return _answer_json(
                    answer="Working length determination in root canal treatment includes radiographic method and apex locator method...",
                    evidence_status="sufficient",
                    should_reinforce=False,
                    used_evidence=[
                        {"chunk_id": "chunk:endo-1"},
                        {"chunk_id": "chunk:endo-2"},
                    ],
                )

        with patch(
            "backend.services.qa.direct_qa.chat_completion", side_effect=mock_llm
        ), patch(
            "backend.services.retrieval.search_knowledge_base", return_value=mock_chunks
        ), patch(
            "backend.services.qa.direct_qa.is_llm_configured", return_value=True
        ):
            from backend.services.qa.direct_qa import answer_direct_question

            result = answer_direct_question(
                message="How to determine working length in root canal treatment?",
                user_id="user-endo-1",
            )

        self.assertIn("root canal", result["answer"].lower())
        self.assertFalse(result["should_reinforce"])
        self.assertEqual(result["confusion_kg_node_ids"], [])


class ConfusionKGNodeIdWhitelistTests(unittest.TestCase):
    """confusion_kg_node_ids must only contain IDs from the current round's
    EvidenceChunk.metadata["core_node_ids"] — no fake, cross-round, or
    LLM-hallucinated KG IDs may enter the output."""

    def test_valid_kg_ids_from_metadata_are_kept(self) -> None:
        """KG IDs matching chunk metadata core_node_ids survive filtering."""
        mock_chunks = [
            _make_evidence_chunk(
                "chunk:kg-1", "Evidence about caries etiology...",
                metadata={"core_node_ids": ["kg://caries/etiology", "kg://caries/pathogenesis"]},
            ),
            _make_evidence_chunk(
                "chunk:kg-2", "Evidence about periodontitis classification...",
                metadata={"core_node_ids": ["kg://perio/glickman"]},
            ),
        ]

        call_count = [0]

        def mock_llm(messages, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _resolution_json(
                    resolved_question="What causes dental caries?",
                    search_queries=[{"query": "dental caries etiology", "purpose": "caries cause"}],
                )
            else:
                return _answer_json(
                    answer="Dental caries is caused by bacteria, food, host, and time factors...",
                    evidence_status="sufficient",
                    should_reinforce=True,
                    confusion_kg_node_ids=[
                        "kg://caries/etiology",
                        "kg://caries/pathogenesis",
                        "kg://perio/glickman",
                    ],
                    used_evidence=[{"chunk_id": "chunk:kg-1"}, {"chunk_id": "chunk:kg-2"}],
                )

        with patch(
            "backend.services.qa.direct_qa.chat_completion", side_effect=mock_llm
        ), patch(
            "backend.services.retrieval.search_knowledge_base", return_value=mock_chunks
        ), patch(
            "backend.services.qa.direct_qa.is_llm_configured", return_value=True
        ):
            from backend.services.qa.direct_qa import answer_direct_question

            result = answer_direct_question(
                message="What causes dental caries?",
                user_id="user-kg-whitelist-1",
            )

        self.assertTrue(result["should_reinforce"])
        self.assertEqual(
            set(result["confusion_kg_node_ids"]),
            {"kg://caries/etiology", "kg://caries/pathogenesis", "kg://perio/glickman"},
        )

    def test_fake_kg_ids_not_in_metadata_are_rejected(self) -> None:
        """KG IDs not present in any chunk's core_node_ids are filtered out."""
        mock_chunks = [
            _make_evidence_chunk(
                "chunk:kg-3", "Evidence about pulpitis diagnosis...",
                metadata={"core_node_ids": ["kg://pulpitis/diagnosis"]},
            ),
        ]

        call_count = [0]

        def mock_llm(messages, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _resolution_json(
                    resolved_question="How to diagnose pulpitis?",
                    search_queries=[{"query": "pulpitis diagnosis", "purpose": "pulpitis diagnosis"}],
                )
            else:
                return _answer_json(
                    answer="Pulpitis diagnosis involves thermal testing, percussion, and X-ray...",
                    evidence_status="sufficient",
                    should_reinforce=True,
                    confusion_kg_node_ids=[
                        "kg://pulpitis/diagnosis",         # valid — in metadata
                        "kg://perio/fake-classification",   # fake — not in any chunk
                        "kg://endo/hallucinated",           # fake — not in any chunk
                    ],
                    used_evidence=[{"chunk_id": "chunk:kg-3"}],
                )

        with patch(
            "backend.services.qa.direct_qa.chat_completion", side_effect=mock_llm
        ), patch(
            "backend.services.retrieval.search_knowledge_base", return_value=mock_chunks
        ), patch(
            "backend.services.qa.direct_qa.is_llm_configured", return_value=True
        ):
            from backend.services.qa.direct_qa import answer_direct_question

            result = answer_direct_question(
                message="How to diagnose pulpitis?",
                user_id="user-kg-whitelist-2",
            )

        self.assertTrue(result["should_reinforce"])
        self.assertEqual(result["confusion_kg_node_ids"], ["kg://pulpitis/diagnosis"])
        self.assertNotIn("kg://perio/fake-classification", result["confusion_kg_node_ids"])
        self.assertNotIn("kg://endo/hallucinated", result["confusion_kg_node_ids"])

    def test_empty_metadata_rejects_all_kg_ids(self) -> None:
        """When no chunk has core_node_ids in metadata, all confusion_kg_node_ids are rejected."""
        mock_chunks = [
            _make_evidence_chunk("chunk:kg-4", "General dental anatomy evidence..."),
            _make_evidence_chunk("chunk:kg-5", "More general evidence..."),
        ]

        call_count = [0]

        def mock_llm(messages, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _resolution_json(
                    resolved_question="What is the structure of a tooth?",
                    search_queries=[{"query": "tooth structure anatomy", "purpose": "tooth anatomy"}],
                )
            else:
                return _answer_json(
                    answer="A tooth consists of enamel, dentin, pulp, and cementum...",
                    evidence_status="sufficient",
                    should_reinforce=True,
                    confusion_kg_node_ids=[
                        "kg://anatomy/enamel",
                        "kg://anatomy/dentin",
                    ],
                    used_evidence=[{"chunk_id": "chunk:kg-4"}],
                )

        with patch(
            "backend.services.qa.direct_qa.chat_completion", side_effect=mock_llm
        ), patch(
            "backend.services.retrieval.search_knowledge_base", return_value=mock_chunks
        ), patch(
            "backend.services.qa.direct_qa.is_llm_configured", return_value=True
        ):
            from backend.services.qa.direct_qa import answer_direct_question

            result = answer_direct_question(
                message="What is the structure of a tooth?",
                user_id="user-kg-whitelist-3",
            )

        # should_reinforce=True but no valid KG IDs -> signal preserved, list empty
        self.assertTrue(result["should_reinforce"])
        self.assertEqual(result["confusion_kg_node_ids"], [],
                         "Empty metadata must reject ALL confusion_kg_node_ids")

    def test_multi_chunk_union_allows_corresponding_ids(self) -> None:
        """Multiple chunks with different core_node_ids — union allows each chunk's IDs."""
        mock_chunks = [
            _make_evidence_chunk(
                "chunk:kg-6", "GTR membrane materials...",
                metadata={"core_node_ids": ["kg://perio/gtr/membrane"]},
            ),
            _make_evidence_chunk(
                "chunk:kg-7", "Bone graft materials for GTR...",
                metadata={"core_node_ids": ["kg://perio/gtr/bone-graft"]},
            ),
            _make_evidence_chunk(
                "chunk:kg-8", "GTR surgical technique steps...",
                metadata={"core_node_ids": ["kg://perio/gtr/technique"]},
            ),
        ]

        call_count = [0]

        def mock_llm(messages, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _resolution_json(
                    resolved_question="What materials and techniques are used in GTR?",
                    search_queries=[{"query": "GTR guided tissue regeneration", "purpose": "GTR overview"}],
                )
            else:
                return _answer_json(
                    answer="GTR uses membrane materials, bone grafts, and specific surgical techniques...",
                    evidence_status="sufficient",
                    should_reinforce=True,
                    confusion_kg_node_ids=[
                        "kg://perio/gtr/membrane",
                        "kg://perio/gtr/bone-graft",
                        "kg://perio/gtr/technique",
                    ],
                    used_evidence=[
                        {"chunk_id": "chunk:kg-6"},
                        {"chunk_id": "chunk:kg-7"},
                        {"chunk_id": "chunk:kg-8"},
                    ],
                )

        with patch(
            "backend.services.qa.direct_qa.chat_completion", side_effect=mock_llm
        ), patch(
            "backend.services.retrieval.search_knowledge_base", return_value=mock_chunks
        ), patch(
            "backend.services.qa.direct_qa.is_llm_configured", return_value=True
        ):
            from backend.services.qa.direct_qa import answer_direct_question

            result = answer_direct_question(
                message="What materials and techniques are used in GTR?",
                user_id="user-kg-whitelist-4",
            )

        self.assertTrue(result["should_reinforce"])
        self.assertEqual(
            set(result["confusion_kg_node_ids"]),
            {"kg://perio/gtr/membrane", "kg://perio/gtr/bone-graft", "kg://perio/gtr/technique"},
        )

    def test_mixed_valid_and_invalid_only_valid_kept(self) -> None:
        """LLM returns a mix of valid and invalid IDs — only valid survive filtering."""
        mock_chunks = [
            _make_evidence_chunk(
                "chunk:kg-9", "Candidiasis classification...",
                metadata={"core_node_ids": ["kg://oral-medicine/candidiasis/acute"]},
            ),
            _make_evidence_chunk(
                "chunk:kg-10", "Chronic candidiasis features...",
                metadata={"core_node_ids": ["kg://oral-medicine/candidiasis/chronic"]},
            ),
        ]

        call_count = [0]

        def mock_llm(messages, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _resolution_json(
                    resolved_question="How is oral candidiasis classified?",
                    search_queries=[{"query": "oral candidiasis classification", "purpose": "candidiasis types"}],
                )
            else:
                return _answer_json(
                    answer="Oral candidiasis is classified into acute and chronic forms...",
                    evidence_status="sufficient",
                    should_reinforce=True,
                    confusion_kg_node_ids=[
                        "kg://oral-medicine/candidiasis/acute",     # valid
                        "kg://oral-medicine/candidiasis/chronic",   # valid
                        "kg://oral-medicine/lichen-planus/reticular",  # NOT in any chunk
                        "kg://oral-medicine/leukoplakia/homogeneous",  # NOT in any chunk
                    ],
                    used_evidence=[{"chunk_id": "chunk:kg-9"}, {"chunk_id": "chunk:kg-10"}],
                )

        with patch(
            "backend.services.qa.direct_qa.chat_completion", side_effect=mock_llm
        ), patch(
            "backend.services.retrieval.search_knowledge_base", return_value=mock_chunks
        ), patch(
            "backend.services.qa.direct_qa.is_llm_configured", return_value=True
        ):
            from backend.services.qa.direct_qa import answer_direct_question

            result = answer_direct_question(
                message="How is oral candidiasis classified?",
                user_id="user-kg-whitelist-5",
            )

        self.assertTrue(result["should_reinforce"])
        self.assertEqual(
            set(result["confusion_kg_node_ids"]),
            {"kg://oral-medicine/candidiasis/acute", "kg://oral-medicine/candidiasis/chronic"},
        )
        for fake_id in (
            "kg://oral-medicine/lichen-planus/reticular",
            "kg://oral-medicine/leukoplakia/homogeneous",
        ):
            self.assertNotIn(fake_id, result["confusion_kg_node_ids"],
                             f"Fake cross-topic ID {fake_id} must be rejected")

    def test_llm_returns_empty_list_when_no_core_node_ids_in_chunks(self) -> None:
        """LLM returns KG IDs but chunks have no core_node_ids -> result is empty."""
        mock_chunks = [
            _make_evidence_chunk("chunk:kg-11", "General oral surgery principles..."),
        ]

        call_count = [0]

        def mock_llm(messages, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _resolution_json(
                    resolved_question="What are the principles of oral surgery?",
                    search_queries=[{"query": "oral surgery principles", "purpose": "surgery basics"}],
                )
            else:
                return _answer_json(
                    answer="Oral surgery principles include asepsis, hemostasis, and minimal trauma...",
                    evidence_status="sufficient",
                    should_reinforce=False,
                    confusion_kg_node_ids=["kg://surgery/asepsis"],
                    used_evidence=[{"chunk_id": "chunk:kg-11"}],
                )

        with patch(
            "backend.services.qa.direct_qa.chat_completion", side_effect=mock_llm
        ), patch(
            "backend.services.retrieval.search_knowledge_base", return_value=mock_chunks
        ), patch(
            "backend.services.qa.direct_qa.is_llm_configured", return_value=True
        ):
            from backend.services.qa.direct_qa import answer_direct_question

            result = answer_direct_question(
                message="What are the principles of oral surgery?",
                user_id="user-kg-whitelist-6",
            )

        # LLM returned an ID but chunks have no core_node_ids — must be rejected
        self.assertEqual(result["confusion_kg_node_ids"], [],
                         "Without core_node_ids in chunks, ALL KG IDs must be rejected")

    def test_cross_round_kg_ids_are_rejected(self) -> None:
        """KG IDs from a previous round's chunks must not leak into the current output.
        This simulates a follow-up where the new chunks have different core_node_ids."""
        mock_chunks_round1 = [
            _make_evidence_chunk(
                "chunk:r1-1", "First round evidence about periodontitis...",
                metadata={"core_node_ids": ["kg://perio/classification"]},
            ),
        ]
        mock_chunks_round2 = [
            _make_evidence_chunk(
                "chunk:r2-1", "Second round evidence about endodontics...",
                metadata={"core_node_ids": ["kg://endo/working-length"]},
            ),
        ]

        # Simulate two rounds with different KAG results
        kag_calls = [0]

        def mock_kag(query, **kwargs):
            kag_calls[0] += 1
            if kag_calls[0] == 1:
                return mock_chunks_round1
            else:
                return mock_chunks_round2

        llm_calls = [0]

        def mock_llm(messages, **kwargs):
            llm_calls[0] += 1
            if llm_calls[0] in (1, 3):
                return _resolution_json(
                    resolved_question="A dental question",
                    search_queries=[{"query": "dental query", "purpose": "test"}],
                )
            elif llm_calls[0] == 2:
                # Round 1 answer: LLM returns round-1 KG IDs
                return _answer_json(
                    answer="Periodontitis classification includes...",
                    evidence_status="sufficient",
                    should_reinforce=True,
                    confusion_kg_node_ids=["kg://perio/classification"],
                    used_evidence=[{"chunk_id": "chunk:r1-1"}],
                )
            else:
                # Round 2 answer: LLM hallucinates round-1 KG ID in round 2
                return _answer_json(
                    answer="Working length determination involves...",
                    evidence_status="sufficient",
                    should_reinforce=True,
                    confusion_kg_node_ids=[
                        "kg://endo/working-length",     # valid for round 2
                        "kg://perio/classification",    # CROSS-ROUND — only in round 1 chunks!
                    ],
                    used_evidence=[{"chunk_id": "chunk:r2-1"}],
                )

        with patch(
            "backend.services.qa.direct_qa.chat_completion", side_effect=mock_llm
        ), patch(
            "backend.services.retrieval.search_knowledge_base", side_effect=mock_kag
        ), patch(
            "backend.services.qa.direct_qa.is_llm_configured", return_value=True
        ):
            from backend.services.qa.direct_qa import answer_direct_question

            # Round 1
            result1 = answer_direct_question(
                message="What is the classification of periodontitis?",
                user_id="user-cross-round",
            )
            # Round 2 (follow-up)
            result2 = answer_direct_question(
                message="How to determine working length?",
                user_id="user-cross-round",
            )

        # Round 1: only round-1 KG IDs survive
        self.assertEqual(result1["confusion_kg_node_ids"], ["kg://perio/classification"])

        # Round 2: cross-round KG ID must be rejected
        self.assertEqual(result2["confusion_kg_node_ids"], ["kg://endo/working-length"])
        self.assertNotIn("kg://perio/classification", result2["confusion_kg_node_ids"],
                         "Cross-round KG ID from round 1 must not leak into round 2 output")

    def test_non_list_core_node_ids_safely_ignored(self) -> None:
        """Malformed core_node_ids (not a list) in metadata is safely ignored."""
        mock_chunks = [
            _make_evidence_chunk(
                "chunk:kg-12", "Evidence with malformed metadata...",
                metadata={"core_node_ids": "not-a-list"},
            ),
            _make_evidence_chunk(
                "chunk:kg-13", "Evidence with valid metadata...",
                metadata={"core_node_ids": ["kg://valid/id"]},
            ),
        ]

        call_count = [0]

        def mock_llm(messages, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _resolution_json(
                    resolved_question="A test question",
                    search_queries=[{"query": "test", "purpose": "test"}],
                )
            else:
                return _answer_json(
                    answer="Test answer...",
                    evidence_status="sufficient",
                    should_reinforce=True,
                    confusion_kg_node_ids=["kg://valid/id", "kg://fake/id"],
                    used_evidence=[{"chunk_id": "chunk:kg-13"}],
                )

        with patch(
            "backend.services.qa.direct_qa.chat_completion", side_effect=mock_llm
        ), patch(
            "backend.services.retrieval.search_knowledge_base", return_value=mock_chunks
        ), patch(
            "backend.services.qa.direct_qa.is_llm_configured", return_value=True
        ):
            from backend.services.qa.direct_qa import answer_direct_question

            result = answer_direct_question(
                message="Test question?",
                user_id="user-kg-whitelist-7",
            )

        # Only the valid ID from the well-formed metadata chunk survives
        self.assertEqual(result["confusion_kg_node_ids"], ["kg://valid/id"])


class DirectQAPersistenceIntegrationTests(unittest.TestCase):
    """Direct QA service → persistence adapter wiring.

    Covers:
    - Positive: adapter called when user_id + should_reinforce=True + valid KG IDs
    - No-op: empty user_id → adapter skipped
    - No-op: should_reinforce=False → adapter skipped
    - No-op: no valid KG IDs after whitelist → adapter skipped
    - Exception in adapter → answer preserved, trace marked failed
    - No session / course table pollution
    """

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _chunks_with_kg() -> list:
        return [
            _make_evidence_chunk(
                "chunk:persist-1",
                "Dental caries is caused by bacteria, food, host, and time factors...",
                metadata={"core_node_ids": ["kg-caries-etiology"]},
            ),
        ]

    @staticmethod
    def _chunks_without_kg() -> list:
        return [
            _make_evidence_chunk(
                "chunk:persist-no-kg",
                "General oral health information without KG node mapping...",
            ),
        ]

    @staticmethod
    def _llm_confusion() -> list:
        """LLM that returns should_reinforce=True with valid KG IDs."""
        call_count = [0]

        def mock_llm(messages, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _resolution_json(
                    resolved_question="What causes dental caries?",
                    search_queries=[{"query": "caries etiology", "purpose": "caries cause"}],
                )
            else:
                return _answer_json(
                    answer="Dental caries is caused by the interaction of bacteria, food, host, and time factors...",
                    evidence_status="sufficient",
                    should_reinforce=True,
                    confusion_kg_node_ids=["kg-caries-etiology"],
                    used_evidence=[{"chunk_id": "chunk:persist-1"}],
                )

        return mock_llm

    @staticmethod
    def _llm_normal() -> list:
        """LLM that returns should_reinforce=False (normal expansion question)."""
        call_count = [0]

        def mock_llm(messages, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _resolution_json(
                    resolved_question="What are the types of dental restorations?",
                    search_queries=[{"query": "dental restoration types", "purpose": "restoration types"}],
                )
            else:
                return _answer_json(
                    answer="Dental restorations include fillings, inlays, onlays, crowns, and bridges...",
                    evidence_status="sufficient",
                    should_reinforce=False,
                    confusion_kg_node_ids=[],
                    used_evidence=[{"chunk_id": "chunk:persist-1"}],
                )

        return mock_llm

    @staticmethod
    def _mock_memory():
        """Return a MagicMock that quacks like LearningMemory."""
        from unittest.mock import MagicMock

        mem = MagicMock()
        mem.get_or_create_profile.return_value = {
            "known_weak_points": [],
            "mistake_patterns": [],
            "mastery_by_kg_node": {},
        }
        mem._conn = MagicMock()
        return mem

    # ------------------------------------------------------------------
    # Positive write
    # ------------------------------------------------------------------

    def test_persistence_called_on_confusion(self) -> None:
        """user_id + should_reinforce=True + valid KG IDs → adapter called, answer preserved."""
        mock_persistence_result = {
            "profile": {"known_weak_points": ["kg-caries-etiology"]},
            "mastery_records": [{"kg_node_id": "kg-caries-etiology", "mastery_score": 0.27}],
            "profile_updated": True,
            "confusion_kg_node_ids_processed": ["kg-caries-etiology"],
            "trace": "ok: profile updated, 1 mastery records written for 1 KG node(s)",
        }

        with patch(
            "backend.services.qa.direct_qa.chat_completion",
            side_effect=self._llm_confusion(),
        ), patch(
            "backend.services.retrieval.search_knowledge_base",
            return_value=self._chunks_with_kg(),
        ), patch(
            "backend.services.qa.direct_qa.is_llm_configured", return_value=True
        ), patch(
            "backend.services.qa.direct_qa_persistence.update_profile_from_qa",
            return_value=mock_persistence_result,
        ) as mock_persist, patch(
            "backend.services.learning.memory.LearningMemory",
            return_value=self._mock_memory(),
        ):
            from backend.services.qa.direct_qa import answer_direct_question

            result = answer_direct_question(
                message="What causes dental caries?",
                user_id="user-persist-1",
            )

        # Answer preserved
        self.assertIn("caries", result["answer"].lower())
        self.assertEqual(result["trace"]["mode"], "direct_qa_evidence")
        self.assertTrue(result["should_reinforce"])
        self.assertEqual(result["confusion_kg_node_ids"], ["kg-caries-etiology"])

        # Adapter called exactly once
        mock_persist.assert_called_once()
        _args, kwargs = mock_persist.call_args
        self.assertEqual(kwargs["user_id"], "user-persist-1")
        self.assertTrue(kwargs["qa_result"]["should_reinforce"] is True)
        self.assertEqual(
            kwargs["qa_result"]["confusion_kg_node_ids"],
            ["kg-caries-etiology"],
        )
        self.assertIn("caries", kwargs["qa_result"]["question"])

        # Trace carries persistence outcome
        self.assertIn("persistence", result["trace"])
        self.assertIn("profile updated", result["trace"]["persistence"])

    # ------------------------------------------------------------------
    # No-op: empty user_id
    # ------------------------------------------------------------------

    def test_persistence_skipped_when_user_id_empty(self) -> None:
        """Empty user_id → adapter NOT called, answer still returned."""
        with patch(
            "backend.services.qa.direct_qa.chat_completion",
            side_effect=self._llm_confusion(),
        ), patch(
            "backend.services.retrieval.search_knowledge_base",
            return_value=self._chunks_with_kg(),
        ), patch(
            "backend.services.qa.direct_qa.is_llm_configured", return_value=True
        ), patch(
            "backend.services.qa.direct_qa_persistence.update_profile_from_qa",
        ) as mock_persist:
            from backend.services.qa.direct_qa import answer_direct_question

            result = answer_direct_question(
                message="What causes dental caries?",
                user_id="",
            )

        # Answer still produced
        self.assertIn("caries", result["answer"].lower())
        self.assertEqual(result["trace"]["mode"], "direct_qa_evidence")

        # Adapter must NOT be called
        mock_persist.assert_not_called()

        # No persistence key in trace
        self.assertNotIn("persistence", result["trace"])

    # ------------------------------------------------------------------
    # No-op: should_reinforce=False
    # ------------------------------------------------------------------

    def test_persistence_skipped_when_should_reinforce_false(self) -> None:
        """should_reinforce=False → adapter skipped even with user_id."""
        with patch(
            "backend.services.qa.direct_qa.chat_completion",
            side_effect=self._llm_normal(),
        ), patch(
            "backend.services.retrieval.search_knowledge_base",
            return_value=self._chunks_with_kg(),
        ), patch(
            "backend.services.qa.direct_qa.is_llm_configured", return_value=True
        ), patch(
            "backend.services.qa.direct_qa_persistence.update_profile_from_qa",
        ) as mock_persist:
            from backend.services.qa.direct_qa import answer_direct_question

            result = answer_direct_question(
                message="What are the types of dental restorations?",
                user_id="user-persist-2",
            )

        # Answer produced normally
        self.assertIn("restoration", result["answer"].lower())
        self.assertFalse(result["should_reinforce"])

        # Adapter must NOT be called
        mock_persist.assert_not_called()

    # ------------------------------------------------------------------
    # No-op: no valid KG IDs after whitelist filtering
    # ------------------------------------------------------------------

    def test_persistence_skipped_when_no_valid_kg_ids(self) -> None:
        """Chunks have no core_node_ids → all KG IDs rejected → adapter skipped."""
        call_count = [0]

        def mock_llm(messages, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _resolution_json(
                    resolved_question="What are general oral health principles?",
                    search_queries=[{"query": "oral health", "purpose": "general"}],
                )
            else:
                return _answer_json(
                    answer="Oral health involves regular brushing, flossing, and dental check-ups...",
                    evidence_status="sufficient",
                    should_reinforce=True,
                    confusion_kg_node_ids=["kg-oral-health"],
                    used_evidence=[{"chunk_id": "chunk:persist-no-kg"}],
                )

        with patch(
            "backend.services.qa.direct_qa.chat_completion", side_effect=mock_llm
        ), patch(
            "backend.services.retrieval.search_knowledge_base",
            return_value=self._chunks_without_kg(),
        ), patch(
            "backend.services.qa.direct_qa.is_llm_configured", return_value=True
        ), patch(
            "backend.services.qa.direct_qa_persistence.update_profile_from_qa",
        ) as mock_persist:
            from backend.services.qa.direct_qa import answer_direct_question

            result = answer_direct_question(
                message="What are general oral health principles?",
                user_id="user-persist-3",
            )

        # Answer preserved
        self.assertIn("brushing", result["answer"].lower())
        self.assertTrue(result["should_reinforce"])
        # KG IDs were rejected by whitelist (chunks have no core_node_ids)
        self.assertEqual(result["confusion_kg_node_ids"], [])

        # Adapter must NOT be called — no valid KG IDs after whitelist
        mock_persist.assert_not_called()

    # ------------------------------------------------------------------
    # Exception does not block answer
    # ------------------------------------------------------------------

    def test_persistence_exception_does_not_block_answer(self) -> None:
        """Adapter raises → answer still returned, trace marked failed."""
        with patch(
            "backend.services.qa.direct_qa.chat_completion",
            side_effect=self._llm_confusion(),
        ), patch(
            "backend.services.retrieval.search_knowledge_base",
            return_value=self._chunks_with_kg(),
        ), patch(
            "backend.services.qa.direct_qa.is_llm_configured", return_value=True
        ), patch(
            "backend.services.qa.direct_qa_persistence.update_profile_from_qa",
            side_effect=RuntimeError("DB connection lost"),
        ), patch(
            "backend.services.learning.memory.LearningMemory",
            return_value=self._mock_memory(),
        ):
            from backend.services.qa.direct_qa import answer_direct_question

            result = answer_direct_question(
                message="What causes dental caries?",
                user_id="user-persist-4",
            )

        # Answer MUST be preserved — exception does not propagate
        self.assertIn("caries", result["answer"].lower())
        self.assertEqual(result["trace"]["mode"], "direct_qa_evidence")
        self.assertTrue(result["should_reinforce"])

        # Trace must mark persistence as failed
        self.assertIn("persistence", result["trace"])
        self.assertIn("failed", result["trace"]["persistence"])
        self.assertIn("DB connection lost", result["trace"]["persistence"])

    def test_persistence_import_error_does_not_block_answer(self) -> None:
        """Import of persistence module fails → answer still returned, trace marked."""
        import builtins
        import sys

        from backend.services.qa.direct_qa import answer_direct_question

        # Remove the persistence module from cache so _persist_qa_result
        # triggers a fresh import (which we will make fail).
        saved_persistence = sys.modules.pop(
            "backend.services.qa.direct_qa_persistence", None
        )

        _original_import = builtins.__import__

        def _selective_import(name, *args, **kwargs):
            if name == "backend.services.qa.direct_qa_persistence":
                raise ImportError(f"No module named '{name}'")
            return _original_import(name, *args, **kwargs)

        try:
            with patch(
                "backend.services.qa.direct_qa.chat_completion",
                side_effect=self._llm_confusion(),
            ), patch(
                "backend.services.retrieval.search_knowledge_base",
                return_value=self._chunks_with_kg(),
            ), patch(
                "backend.services.qa.direct_qa.is_llm_configured", return_value=True
            ), patch(
                "builtins.__import__",
                side_effect=_selective_import,
            ):
                result = answer_direct_question(
                    message="What causes dental caries?",
                    user_id="user-persist-5",
                )
        finally:
            # Restore module cache
            if saved_persistence is not None:
                sys.modules[
                    "backend.services.qa.direct_qa_persistence"
                ] = saved_persistence

        # Answer still returned
        self.assertIn("caries", result["answer"].lower())
        self.assertEqual(result["trace"]["mode"], "direct_qa_evidence")

        # Trace marks persistence failed
        self.assertIn("persistence", result["trace"])
        self.assertIn("failed", result["trace"]["persistence"])

    # ------------------------------------------------------------------
    # No session / course table pollution
    # ------------------------------------------------------------------

    def test_no_session_or_course_table_pollution(self) -> None:
        """Direct QA writes must not create learning_sessions, mastery_records, or lesson_events rows."""
        import tempfile
        from pathlib import Path

        from backend.services.learning.memory import LearningMemory

        with tempfile.TemporaryDirectory() as tmp:
            mem = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                mem.get_or_create_profile("user-pollution")

                # Simulate what _persist_qa_result does: write through the adapter
                from backend.services.qa.direct_qa_persistence import \
                    update_profile_from_qa

                profile = mem.get_or_create_profile("user-pollution")
                update_profile_from_qa(
                    memory=mem,
                    user_id="user-pollution",
                    profile=profile,
                    qa_result={
                        "should_reinforce": True,
                        "confusion_kg_node_ids": ["kg-test"],
                        "question": "Test question?",
                        "resolved_question": "Test question?",
                        "used_evidence": [],
                    },
                    source_ref="dqa-test",
                )

                # Course mastery_records must be empty
                course_rows = mem._conn.execute(
                    "SELECT * FROM mastery_records WHERE user_id = ?",
                    ("user-pollution",),
                ).fetchall()
                self.assertEqual(len(course_rows), 0,
                                 "Direct QA must not write to course mastery_records")

                # lesson_events must be empty
                evt_rows = mem._conn.execute(
                    "SELECT * FROM lesson_events WHERE user_id = ?",
                    ("user-pollution",),
                ).fetchall()
                self.assertEqual(len(evt_rows), 0,
                                 "Direct QA must not create lesson_events")

                # No learning session created
                sessions = mem.list_sessions_for_user("user-pollution")
                self.assertEqual(len(sessions), 0,
                                 "Direct QA must not create learning_sessions")

                # Direct QA mastery table SHOULD have the record
                dqa_rows = mem.get_direct_qa_mastery_by_user("user-pollution")
                self.assertEqual(len(dqa_rows), 1)
                self.assertEqual(dqa_rows[0]["kg_node_id"], "kg-test")
                self.assertEqual(dqa_rows[0]["source"], "direct_qa")
            finally:
                mem._conn.close()

    def test_full_chain_writes_only_to_direct_qa_tables(self) -> None:
        """End-to-end: answer_direct_question → adapter → only direct_qa tables touched."""
        import tempfile
        from pathlib import Path

        from backend.services.learning.memory import LearningMemory

        # Use a class-level variable to pass the db path through the mock
        db_path: list[Path] = []

        class _TestLearningMemory(LearningMemory):
            """LearningMemory subclass that records the db path for post-hoc verification."""

            def __init__(self, db_path_arg=None):
                path = db_path_arg or LearningMemory._default_path()
                super().__init__(path)
                if not db_path:
                    db_path.append(path)

            @staticmethod
            def _default_path():
                return LearningMemory._DEFAULT_DB_PATH

        with tempfile.TemporaryDirectory() as tmp:
            test_db = Path(tmp) / "learning.sqlite3"
            mem = _TestLearningMemory(test_db)
            user_id = "user-full-chain"

            with patch(
                "backend.services.qa.direct_qa.chat_completion",
                side_effect=self._llm_confusion(),
            ), patch(
                "backend.services.retrieval.search_knowledge_base",
                return_value=self._chunks_with_kg(),
            ), patch(
                "backend.services.qa.direct_qa.is_llm_configured", return_value=True
            ), patch(
                "backend.services.learning.memory.LearningMemory",
                return_value=mem,
            ):
                from backend.services.qa.direct_qa import answer_direct_question

                result = answer_direct_question(
                    message="What causes dental caries?",
                    user_id=user_id,
                )

            # Answer returned
            self.assertIn("caries", result["answer"].lower())
            self.assertIn("persistence", result["trace"])
            self.assertIn("profile updated", result["trace"]["persistence"])

            # _persist_qa_result closes the connection it created (mem).
            # Re-open a fresh connection to the same file for verification.
            verify_mem = LearningMemory(test_db)
            try:
                dqa_rows = verify_mem.get_direct_qa_mastery_by_user(user_id)
                self.assertEqual(len(dqa_rows), 1)
                self.assertEqual(dqa_rows[0]["kg_node_id"], "kg-caries-etiology")

                # Course tables untouched
                course_rows = verify_mem._conn.execute(
                    "SELECT * FROM mastery_records WHERE user_id = ?",
                    (user_id,),
                ).fetchall()
                self.assertEqual(len(course_rows), 0)

                evt_rows = verify_mem._conn.execute(
                    "SELECT * FROM lesson_events WHERE user_id = ?",
                    (user_id,),
                ).fetchall()
                self.assertEqual(len(evt_rows), 0)

                sessions = verify_mem.list_sessions_for_user(user_id)
                self.assertEqual(len(sessions), 0)
            finally:
                verify_mem._conn.close()


if __name__ == "__main__":
    unittest.main()
