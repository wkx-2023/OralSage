"""Stage exam generation and grading for learning sessions.

The stage exam is a session-level assessment that covers every outline
section in the current lesson.  It intentionally reuses the existing
single-choice assessment pipeline and persists the exam as a JSON resource
under ``data/learning/sessions/{session_id}/stage_exam.json``.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .assessment import assess_answer
from .handout_cache import get_handout_cache
from .memory import LearningMemory
from .profile_memory import build_profile_slice
from .tutor_cache import get_tutor_cache
from .user_model import update_profile_from_assessment


_VALID_CHOICES = {"A", "B", "C", "D", "E"}
_KAG_QUESTION_BANK_PATH = Path("data/kb/kag_bge_m3/questions.jsonl")
_FULL_QUESTION_BANK_PATH = Path("data/question_bank/question_bank.json")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stage_exam_path(
    session_id: str,
    resource_root: str | Path | None = None,
) -> Path:
    root = Path(resource_root) if resource_root else Path("data/learning/sessions")
    return root / session_id / "stage_exam.json"


def load_stage_exam(
    session_id: str,
    resource_root: str | Path | None = None,
) -> dict[str, Any] | None:
    path = stage_exam_path(session_id, resource_root)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _contains_legacy_fallback_question(exam: dict[str, Any]) -> bool:
    questions = exam.get("questions", []) if isinstance(exam, dict) else []
    return any(
        isinstance(question, dict)
        and question.get("source") == "stage_exam.fallback"
        for question in questions
    )


def _write_stage_exam(
    session_id: str,
    exam: dict[str, Any],
    resource_root: str | Path | None = None,
) -> None:
    path = stage_exam_path(session_id, resource_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(exam, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(path))


def public_stage_exam(
    exam: dict[str, Any],
    *,
    include_answers: bool = False,
) -> dict[str, Any]:
    """Return a client-safe copy of a stage exam."""
    result = dict(exam)
    public_questions: list[dict[str, Any]] = []
    for question in exam.get("questions", []) or []:
        if not isinstance(question, dict):
            continue
        item = dict(question)
        if not include_answers:
            item.pop("answer", None)
            item.pop("explanation", None)
        public_questions.append(item)
    result["questions"] = public_questions
    if not include_answers and isinstance(result.get("submission"), dict):
        submission = dict(result["submission"])
        submission.pop("results", None)
        result["submission"] = submission
    return result


def generate_topic_quiz(
    memory: LearningMemory,
    session_id: str,
    *,
    topic: str,
    question_count: int = 5,
    resource_root: str | Path | None = None,
) -> dict[str, Any]:
    """Build a count-controlled quiz from real question-bank records for a topic."""
    session = memory.get_session(session_id)
    normalized_topic = " ".join(str(topic or "").split()).strip()
    if not normalized_topic:
        raise ValueError("A topic is required for a standalone quiz.")

    requested_count = max(1, min(int(question_count or 5), 20))
    question_bank = _load_stage_exam_question_bank()
    rows = _question_bank_kag_rows(question_bank) or _question_bank_full_records(question_bank)
    node = {
        "id": "standalone_topic",
        "title": normalized_topic,
        "difficulty": "basic",
        "kg_node_ids": [],
        "question_ids": [],
    }
    candidates = [
        item
        for item in rows
        if isinstance(item, dict) and _question_bank_item_matches_topic(item, normalized_topic)
    ]
    candidates.sort(key=_question_bank_sort_key)

    questions: list[dict[str, Any]] = []
    used_source_question_ids: set[str] = set()
    for raw in candidates:
        source_question_id = _question_bank_dedupe_id(raw)
        if not source_question_id or source_question_id in used_source_question_ids:
            continue
        question = _normalize_question_bank_question(
            raw,
            node,
            1,
            len(questions) + 1,
            question_bank=question_bank,
            exact_question_id_match=False,
        )
        if not question or not _is_valid_question(question):
            continue
        question["source"] = "question_bank.topic_match"
        question["exam_question_index"] = len(questions) + 1
        questions.append(question)
        used_source_question_ids.add(source_question_id)
        if len(questions) >= requested_count:
            break

    available_count = len(questions)
    warnings: list[str] = []
    if available_count < requested_count:
        warnings.append(
            f"Only {available_count} verified question-bank questions are available for the requested topic."
        )

    exam = {
        "schema_version": "learning.topic_quiz.v1",
        "exam_id": f"topic_quiz_{session_id}",
        "session_id": session_id,
        "user_id": session.get("user_id"),
        "topic": normalized_topic,
        "status": "ready" if questions else "failed",
        "source": {
            "question_sources": _source_counts(questions),
            "question_bank_path": str(_FULL_QUESTION_BANK_PATH),
            "kag_question_bank_path": str(_KAG_QUESTION_BANK_PATH),
        },
        "questions": questions,
        "summary": {
            "question_count": available_count,
            "requested_question_count": requested_count,
            "section_count": 1,
            "submitted": False,
        },
        "warnings": warnings,
        "error": None if questions else "No verified question-bank questions matched the requested topic.",
    }
    if questions:
        _write_stage_exam(session_id, exam, resource_root)
        _record_generated_event(memory, session, exam)
    return exam


def generate_stage_exam(
    memory: LearningMemory,
    session_id: str,
    *,
    resource_root: str | Path | None = None,
    outline_evidence_map: dict[str, Any] | None = None,
    force: bool = False,
    questions_per_section: int = 1,
) -> dict[str, Any]:
    """Build or load the session-level stage exam."""
    existing = load_stage_exam(session_id, resource_root)
    if existing and not force and not _contains_legacy_fallback_question(existing):
        return existing

    session = memory.get_session(session_id)
    outline_record = memory.latest_outline(session_id)
    if not outline_record:
        raise ValueError(f"Session has no outline: {session_id}")

    outline = outline_record.get("outline", {}) or {}
    nodes = [node for node in outline.get("nodes", []) or [] if isinstance(node, dict)]
    if not nodes:
        raise ValueError(f"Session outline has no nodes: {session_id}")

    per_section = max(1, min(int(questions_per_section or 1), 3))
    question_bank = _load_stage_exam_question_bank()
    questions: list[dict[str, Any]] = []
    section_count: dict[str, int] = {}
    warnings: list[str] = []
    used_source_question_ids: set[str] = set()

    for node_index, node in enumerate(nodes, start=1):
        node_id = str(node.get("id") or f"node_{node_index}")
        candidates = _questions_from_question_bank(
            node=node,
            node_index=node_index,
            question_bank=question_bank,
            used_source_question_ids=used_source_question_ids,
        )
        if not candidates:
            candidates = _questions_from_tutor_cache(
                session_id=session_id,
                node=node,
                node_index=node_index,
                resource_root=resource_root,
            )
        section_count[node_id] = 0
        for candidate in candidates:
            if section_count[node_id] >= per_section:
                break
            if not _is_valid_question(candidate):
                continue
            candidate["exam_question_index"] = len(questions) + 1
            questions.append(candidate)
            used_source_question_ids.add(str(candidate.get("source_question_id") or ""))
            section_count[node_id] += 1

        if section_count[node_id] <= 0:
            warnings.append(f"{node_id}: no usable bank or tutor question")

    if not questions:
        raise ValueError("No valid stage exam questions could be generated")

    exam = {
        "schema_version": "learning.stage_exam.v1",
        "exam_id": f"stage_exam_{session_id}",
        "session_id": session_id,
        "user_id": session.get("user_id"),
        "topic": session.get("topic"),
        "status": "ready",
        "created_at": _now(),
        "updated_at": _now(),
        "source": {
            "outline_id": outline_record.get("id"),
            "outline_node_count": len(nodes),
            "questions_per_section": per_section,
            "question_sources": _source_counts(questions),
            "question_bank_path": str(_FULL_QUESTION_BANK_PATH),
            "kag_question_bank_path": str(_KAG_QUESTION_BANK_PATH),
        },
        "questions": questions,
        "summary": {
            "question_count": len(questions),
            "section_count": sum(1 for count in section_count.values() if count > 0),
            "submitted": False,
        },
        "warnings": warnings,
    }
    _write_stage_exam(session_id, exam, resource_root)
    _record_generated_event(memory, session, exam)
    return exam


def submit_stage_exam(
    memory: LearningMemory,
    session_id: str,
    answers: list[dict[str, Any]],
    *,
    resource_root: str | Path | None = None,
    outline_evidence_map: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Grade a submitted stage exam and persist the result."""
    exam = load_stage_exam(session_id, resource_root)
    if not exam:
        exam = generate_stage_exam(
            memory,
            session_id,
            resource_root=resource_root,
            outline_evidence_map=outline_evidence_map,
        )

    session = memory.get_session(session_id)
    user_id = str(session.get("user_id") or "")
    answers_by_id = {
        str(item.get("question_id") or "").strip(): str(item.get("selected_option") or item.get("answer") or "").strip()
        for item in answers
        if isinstance(item, dict)
    }
    if not answers_by_id:
        raise ValueError("No stage exam answers submitted")

    question_by_id = {
        str(question.get("id") or ""): question
        for question in exam.get("questions", []) or []
        if isinstance(question, dict)
    }
    unknown_ids = sorted(qid for qid in answers_by_id if qid not in question_by_id)
    if unknown_ids:
        raise ValueError(f"Unknown stage exam question ids: {', '.join(unknown_ids[:5])}")

    results: list[dict[str, Any]] = []
    section_scores: dict[str, list[float]] = {}
    wrong_kg_node_ids: set[str] = set()
    mastery_updates: list[dict[str, Any]] = []
    profile = build_profile_slice(
        memory,
        user_id=user_id,
        role="assessment",
        context={"session_id": session_id, "surface": "stage_exam"},
    )

    for question_id, selected in answers_by_id.items():
        question = question_by_id[question_id]
        node_id = str(question.get("outline_node_id") or "")
        handout_section = _handout_section(session_id, node_id, resource_root)
        node_evidence = (outline_evidence_map or {}).get(node_id, {})
        assessment = assess_answer(
            question=question,
            user_answer=selected,
            outline_node_id=node_id,
            user_profile=profile,
            handout_section=handout_section,
            node_evidence=node_evidence,
        )
        score = float(assessment.get("score", 0.0) or 0.0)
        section_scores.setdefault(node_id, []).append(score)

        grading = {
            "surface": "stage_exam",
            "stage_exam_id": exam.get("exam_id"),
            "is_correct": assessment.get("is_correct", False),
            "score": score,
            "mastery_delta": assessment.get("mastery_delta", 0.0),
            "mistake_tags": assessment.get("mistake_tags", []),
            "explanation": assessment.get("explanation", ""),
            "option_error_analysis": assessment.get("option_error_analysis"),
            "correct_answer_explanation": assessment.get("correct_answer_explanation"),
            "misconception_summary": assessment.get("misconception_summary"),
            "related_kg_node_ids": assessment.get("related_kg_node_ids", []),
            "fallback_rule_based": assessment.get("fallback_rule_based", True),
            "content_status": assessment.get("content_status", "rule_based"),
            "recommendation": assessment.get("recommendation", {}),
        }
        memory.record_answer(
            session_id=session_id,
            user_id=user_id,
            question_id=question_id,
            outline_node_id=node_id,
            user_answer=selected,
            grading=grading,
        )

        kg_node_ids = _as_str_list(question.get("kg_node_ids", []))
        if not assessment.get("is_correct", False):
            wrong_kg_node_ids.update(kg_node_ids)
            wrong_kg_node_ids.update(_as_str_list(assessment.get("related_kg_node_ids", [])))
            _update_profile_from_wrong_answer(memory, user_id, node_id, assessment, question)

        for kg_id in kg_node_ids or [None]:
            mastery_updates.append(
                memory.upsert_mastery(
                    user_id=user_id,
                    session_id=session_id,
                    concept_name=_concept_name(question),
                    mastery_delta=float(assessment.get("mastery_delta", 0.0) or 0.0),
                    kg_node_id=kg_id,
                    evidence={
                        "surface": "stage_exam",
                        "stage_exam_id": exam.get("exam_id"),
                        "question_id": question_id,
                        "outline_node_id": node_id,
                        "user_answer": selected,
                        "is_correct": assessment.get("is_correct", False),
                        "score": score,
                    },
                    confidence_delta=0.06,
                )
            )

        answer_event_id = memory.record_event(
            session_id=session_id,
            user_id=user_id,
            event_type="answer_submitted",
            outline_node_id=node_id,
            payload={
                "surface": "stage_exam",
                "stage_exam_id": exam.get("exam_id"),
                "question_id": question_id,
                "question_stem": question.get("stem", ""),
                "kg_node_ids": kg_node_ids,
                "user_answer": selected,
                "is_correct": assessment.get("is_correct", False),
                "score": score,
                "mastery_delta": assessment.get("mastery_delta", 0.0),
                "mistake_tags": assessment.get("mistake_tags", []),
                "related_kg_node_ids": assessment.get("related_kg_node_ids", []),
                "misconception_summary": assessment.get("misconception_summary"),
                "remedial_suggestion": (assessment.get("recommendation", {}) or {}).get("remedial_suggestion"),
                "recommendation_action": (assessment.get("recommendation", {}) or {}).get("action"),
                "fallback_rule_based": assessment.get("fallback_rule_based", True),
                "content_status": assessment.get("content_status", "rule_based"),
            },
        )

        reviewed = public_stage_question(question, include_answer=True)
        reviewed.update({
            "selected_option": selected,
            "assessment": assessment,
            "answer_event_id": answer_event_id,
        })
        results.append(reviewed)

    section_score_map = {
        node_id: round(sum(values) / len(values), 4)
        for node_id, values in section_scores.items()
        if values
    }
    overall_score = round(
        sum(item.get("assessment", {}).get("score", 0.0) for item in results) / len(results),
        4,
    )
    weak_section_ids = [
        node_id for node_id, score in section_score_map.items() if score < 0.8
    ]
    submission = {
        "submitted_at": _now(),
        "answer_count": len(answers_by_id),
        "overall_score": overall_score,
        "section_scores": section_score_map,
        "weak_section_ids": weak_section_ids,
        "weak_kg_node_ids": sorted(wrong_kg_node_ids),
        "correct_count": sum(1 for item in results if item.get("assessment", {}).get("is_correct")),
        "question_count": len(results),
        "remediation": _build_remediation(overall_score, weak_section_ids, sorted(wrong_kg_node_ids)),
        "mastery_updates_count": len(mastery_updates),
        "results": results,
    }
    exam["status"] = "submitted"
    exam["updated_at"] = _now()
    exam["submission"] = submission
    exam["summary"] = {
        "question_count": len(exam.get("questions", []) or []),
        "section_count": len(section_score_map),
        "submitted": True,
        "overall_score": overall_score,
        "weak_section_count": len(weak_section_ids),
        "weak_kg_node_count": len(wrong_kg_node_ids),
    }
    _write_stage_exam(session_id, exam, resource_root)
    submission["stage_exam_event_id"] = memory.record_event(
        session_id=session_id,
        user_id=user_id,
        event_type="stage_exam_submitted",
        outline_node_id=None,
        payload={
            "surface": "stage_exam",
            "stage_exam_id": exam.get("exam_id"),
            "overall_score": overall_score,
            "section_scores": section_score_map,
            "weak_section_ids": weak_section_ids,
            "weak_kg_node_ids": sorted(wrong_kg_node_ids),
            "question_count": len(results),
            "correct_count": submission["correct_count"],
            "remediation": submission["remediation"],
        },
    )
    _write_stage_exam(session_id, exam, resource_root)
    return exam


def public_stage_question(
    question: dict[str, Any],
    *,
    include_answer: bool = False,
) -> dict[str, Any]:
    item = dict(question)
    if not include_answer:
        item.pop("answer", None)
        item.pop("explanation", None)
    return item


def _load_question_bank(path: Path = _KAG_QUESTION_BANK_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    rows.append(item)
    except Exception:
        return []
    return rows


def _load_full_question_bank(path: Path = _FULL_QUESTION_BANK_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    records = payload.get("records", []) if isinstance(payload, dict) else []
    return [item for item in records if isinstance(item, dict)]


def _load_stage_exam_question_bank() -> dict[str, Any]:
    kag_rows = _load_question_bank(_KAG_QUESTION_BANK_PATH)
    full_records = _load_full_question_bank(_FULL_QUESTION_BANK_PATH)
    full_by_id = {
        str(item.get("id") or "").strip(): item
        for item in full_records
        if str(item.get("id") or "").strip()
    }
    return {
        "kag_rows": kag_rows,
        "full_records": full_records,
        "full_by_id": full_by_id,
    }


def _questions_from_question_bank(
    *,
    node: dict[str, Any],
    node_index: int,
    question_bank: dict[str, Any] | list[dict[str, Any]],
    used_source_question_ids: set[str],
) -> list[dict[str, Any]]:
    if not question_bank:
        return []

    kag_rows = _question_bank_kag_rows(question_bank)
    full_records = _question_bank_full_records(question_bank)
    rows = kag_rows or full_records
    if not rows:
        return []

    node_id = str(node.get("id") or f"node_{node_index}")
    target_question_ids = _as_str_list(node.get("question_ids") or [])
    target_question_id_set = set(target_question_ids)
    kg_node_ids = set(_as_str_list(node.get("kg_node_ids") or []))

    by_id: dict[str, dict[str, Any]] = {}
    for item in rows:
        for key in (
            str(item.get("question_id") or "").strip(),
            str(item.get("source_question_id") or "").strip(),
            str(item.get("id") or "").strip(),
        ):
            if key and key not in by_id:
                by_id[key] = item
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()

    for qid in target_question_ids:
        item = by_id.get(qid)
        dedupe_id = _question_bank_dedupe_id(item)
        if (
            item
            and dedupe_id not in used_source_question_ids
            and _question_bank_item_matches_node_kg(item, kg_node_ids)
        ):
            ordered.append(item)
            seen.add(dedupe_id)

    primary_matches = [
        item
        for item in rows
        if _question_bank_dedupe_id(item) not in seen
        and _question_bank_dedupe_id(item) not in used_source_question_ids
        and str(item.get("primary_node_id") or "") in kg_node_ids
    ]
    primary_matches.sort(key=_question_bank_sort_key)
    ordered.extend(primary_matches)
    seen.update(_question_bank_dedupe_id(item) for item in primary_matches)

    lexical_matches = [
        item
        for item in rows
        if _question_bank_dedupe_id(item) not in seen
        and _question_bank_dedupe_id(item) not in used_source_question_ids
        and _question_bank_item_matches_node_kg(item, kg_node_ids)
        and _question_bank_item_matches_node_text(item, node)
    ]
    lexical_matches.sort(key=_question_bank_sort_key)
    ordered.extend(lexical_matches)

    result: list[dict[str, Any]] = []
    for index, raw in enumerate(ordered, start=1):
        question = _normalize_question_bank_question(
            raw,
            node,
            node_index,
            index,
            question_bank=question_bank,
            exact_question_id_match=bool(
                {
                    str(raw.get("question_id") or "").strip(),
                    str(raw.get("source_question_id") or "").strip(),
                    str(raw.get("id") or "").strip(),
                }
                & target_question_id_set
            ),
        )
        if question:
            result.append(question)
    return result


def _question_bank_kag_rows(question_bank: dict[str, Any] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(question_bank, list):
        return question_bank
    rows = question_bank.get("kag_rows", [])
    return rows if isinstance(rows, list) else []


def _question_bank_full_records(question_bank: dict[str, Any] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(question_bank, list):
        return []
    rows = question_bank.get("full_records", [])
    return rows if isinstance(rows, list) else []


def _question_bank_full_by_id(question_bank: dict[str, Any] | list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if not isinstance(question_bank, dict):
        return {}
    mapping = question_bank.get("full_by_id", {})
    return mapping if isinstance(mapping, dict) else {}


def _question_bank_dedupe_id(item: dict[str, Any]) -> str:
    return str(
        item.get("source_question_id")
        or item.get("question_id")
        or item.get("id")
        or ""
    ).strip()


def _question_bank_sort_key(item: dict[str, Any]) -> tuple[int, int, int, str]:
    verification = 0 if item.get("verification_status") == "linked" else 1
    match = 0 if item.get("match_status") in {"exact_match", "strong_match"} else 1
    authority = -int(item.get("authority_level") or 0)
    return (verification, match, authority, str(item.get("question_id") or ""))


def _question_bank_item_matches_node_kg(
    item: dict[str, Any],
    kg_node_ids: set[str],
) -> bool:
    """Keep stage-exam questions bound to the outline node's KG identity."""
    if not kg_node_ids:
        return True
    primary_node_id = str(item.get("primary_node_id") or "").strip()
    return bool(primary_node_id) and primary_node_id in kg_node_ids


def _question_bank_item_matches_topic(item: dict[str, Any], topic: str) -> bool:
    if item.get("verification_status") != "linked":
        return False
    normalized_topic = "".join(str(topic or "").lower().split())
    if not normalized_topic:
        return False
    haystack = "".join(
        "".join(str(item.get(key) or "").lower().split())
        for key in ("chapter", "primary_knowledge_point", "stem", "subject")
    )
    return normalized_topic in haystack


def _question_bank_item_matches_node_text(item: dict[str, Any], node: dict[str, Any]) -> bool:
    terms = _node_text_terms(node)
    if not terms:
        return False
    haystack = "\n".join(
        str(item.get(key) or "")
        for key in ("stem", "primary_knowledge_point", "chapter", "subject")
    )
    return any(term in haystack for term in terms)


def _node_text_terms(node: dict[str, Any]) -> list[str]:
    raw_terms = [
        str(node.get("title") or "").strip(),
        *[
            str(item).strip()
            for item in node.get("learning_objectives", []) or []
            if str(item).strip()
        ],
    ]
    terms: list[str] = []
    for raw in raw_terms:
        if not raw:
            continue
        terms.append(raw)
        for token in re.split(r"[\s，,、/／（）()：:；;]+", raw):
            token = token.strip()
            if len(token) >= 4:
                terms.append(token)
    result: list[str] = []
    seen: set[str] = set()
    for term in terms:
        if term and term not in seen:
            seen.add(term)
            result.append(term)
    return result


def _normalize_question_bank_question(
    raw: dict[str, Any],
    node: dict[str, Any],
    node_index: int,
    question_index: int,
    *,
    question_bank: dict[str, Any] | list[dict[str, Any]],
    exact_question_id_match: bool,
) -> dict[str, Any] | None:
    node_id = str(node.get("id") or f"node_{node_index}")
    full_by_id = _question_bank_full_by_id(question_bank)
    full_record = _full_question_for_kag_row(raw, full_by_id)
    content = full_record or raw
    stem = str(content.get("stem") or raw.get("stem") or "").strip()
    source_question_id = str(
        content.get("id")
        or raw.get("source_question_id")
        or raw.get("question_id")
        or f"bank_{question_index}"
    )
    raw_answer = str(content.get("answer") or raw.get("answer") or "").strip().upper()
    raw_options = _normalize_options(content.get("options", []))

    if stem and raw_answer in _VALID_CHOICES and len(raw_options) >= 2:
        return {
            "id": f"stage_{_safe_id(node_id)}_{question_index}_{_safe_id(source_question_id)}",
            "source_question_id": source_question_id,
            "source": "question_bank.full_options" if full_record else "question_bank.options",
            "type": "single_choice",
            "outline_node_id": node_id,
            "outline_node_title": str(node.get("title") or ""),
            "stem": stem,
            "options": raw_options,
            "answer": raw_answer,
            "explanation": _question_bank_explanation(raw, full_record),
            "difficulty": str(node.get("difficulty") or "basic"),
            "kg_node_ids": _as_str_list(node.get("kg_node_ids") or [raw.get("primary_node_id")]),
            "question_bank": _question_bank_meta(raw, exact_question_id_match, full_record),
        }

    knowledge_point = str(raw.get("primary_knowledge_point") or "").strip()
    if not stem or not knowledge_point:
        return None

    options, answer = _synthesized_bank_options(
        correct=knowledge_point,
        node=node,
        seed=source_question_id,
        question_bank=question_bank,
    )
    if len(options) < 4:
        return None

    return {
        "id": f"stage_{_safe_id(node_id)}_{question_index}_{_safe_id(source_question_id)}",
        "source_question_id": source_question_id,
        "source": "question_bank.synthesized_options",
        "type": "single_choice",
        "outline_node_id": node_id,
        "outline_node_title": str(node.get("title") or ""),
        "stem": stem,
        "options": options,
        "answer": answer,
        "explanation": _question_bank_explanation(raw),
        "difficulty": str(node.get("difficulty") or "basic"),
        "kg_node_ids": _as_str_list(node.get("kg_node_ids") or [raw.get("primary_node_id")]),
        "question_bank": _question_bank_meta(raw, exact_question_id_match),
    }


def _full_question_for_kag_row(
    raw: dict[str, Any],
    full_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    for key in (
        str(raw.get("source_question_id") or "").strip(),
        str(raw.get("id") or "").strip(),
        str(raw.get("question_id") or "").strip(),
    ):
        if key and isinstance(full_by_id.get(key), dict):
            return full_by_id[key]
    return None


def _question_bank_meta(
    raw: dict[str, Any],
    exact_question_id_match: bool,
    full_record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "question_id": raw.get("question_id"),
        "source_question_id": raw.get("source_question_id"),
        "full_question_id": full_record.get("id") if full_record else None,
        "source_exam_id": raw.get("source_exam_id"),
        "primary_node_id": raw.get("primary_node_id"),
        "primary_knowledge_point": raw.get("primary_knowledge_point"),
        "full_primary_knowledge_point": full_record.get("primary_knowledge_point") if full_record else None,
        "subject": (full_record or raw).get("subject"),
        "chapter": (full_record or raw).get("chapter"),
        "match_status": raw.get("match_status"),
        "verification_status": raw.get("verification_status"),
        "authority_level": raw.get("authority_level"),
        "exact_question_id_match": exact_question_id_match,
    }


def _question_bank_explanation(
    raw: dict[str, Any],
    full_record: dict[str, Any] | None = None,
) -> str:
    if full_record:
        explanation = str(full_record.get("explanation") or "").strip()
        if explanation:
            return explanation
    knowledge_point = str(raw.get("primary_knowledge_point") or "").strip()
    source_exam = str(raw.get("source_exam_id") or "").strip()
    parts = []
    if knowledge_point:
        parts.append(f"题库关联知识点：{knowledge_point}")
    if source_exam:
        parts.append(f"来源试题：{source_exam}")
    return "；".join(parts)


def _synthesized_bank_options(
    *,
    correct: str,
    node: dict[str, Any],
    seed: str,
    question_bank: dict[str, Any] | list[dict[str, Any]],
) -> tuple[list[str], str]:
    correct_text = _strip_option_prefix(_clip(correct, 72))
    distractors = _question_bank_distractors(correct_text, node, question_bank)
    if len(distractors) < 3:
        distractors.extend(_generic_distractors(node, correct_text))
    unique_distractors: list[str] = []
    seen = {correct_text}
    for item in distractors:
        text = _strip_option_prefix(_clip(item, 72))
        if not text or text in seen:
            continue
        seen.add(text)
        unique_distractors.append(text)
        if len(unique_distractors) >= 3:
            break
    if len(unique_distractors) < 3:
        return [], ""

    choices = unique_distractors[:3]
    correct_index = sum(ord(ch) for ch in seed) % 4
    choices.insert(correct_index, correct_text)
    letters = ["A", "B", "C", "D"]
    options = [f"{letter}. {text}" for letter, text in zip(letters, choices)]
    return options, letters[correct_index]


def _question_bank_distractors(
    correct: str,
    node: dict[str, Any],
    question_bank: dict[str, Any] | list[dict[str, Any]],
) -> list[str]:
    kg_node_ids = set(_as_str_list(node.get("kg_node_ids") or []))
    node_question_ids = set(_as_str_list(node.get("question_ids") or []))
    same_node: list[str] = []
    same_outline: list[str] = []
    global_items: list[str] = []

    rows = _question_bank_kag_rows(question_bank) or _question_bank_full_records(question_bank)
    for item in rows:
        kp = str(item.get("primary_knowledge_point") or "").strip()
        if not kp or _strip_option_prefix(kp) == correct:
            continue
        qid = str(item.get("question_id") or "")
        primary_node_id = str(item.get("primary_node_id") or "")
        if primary_node_id in kg_node_ids:
            same_node.append(kp)
        elif qid in node_question_ids:
            same_outline.append(kp)
        else:
            global_items.append(kp)
    return same_node + same_outline + global_items


def _generic_distractors(node: dict[str, Any], correct: str) -> list[str]:
    title = str(node.get("title") or "").strip() or "本节内容"
    return [
        f"只记忆「{title}」的名称而不理解机制",
        f"把「{title}」与无关疾病表现混同",
        f"忽略「{title}」对应的临床判断依据",
        f"只依据单一症状判断「{title}」",
    ]


def _questions_from_tutor_cache(
    *,
    session_id: str,
    node: dict[str, Any],
    node_index: int,
    resource_root: str | Path | None,
) -> list[dict[str, Any]]:
    node_id = str(node.get("id") or f"node_{node_index}")
    record = get_tutor_cache(session_id, node_id, resource_root)
    step = record.get("tutor_step", {}) if record.get("status") == "ready" else {}
    raw_questions = step.get("questions", []) if isinstance(step, dict) else []
    questions: list[dict[str, Any]] = []
    for q_index, raw in enumerate(raw_questions or [], start=1):
        if not isinstance(raw, dict):
            continue
        question = _normalize_question(raw, node, node_index, q_index, source="tutor_step.questions")
        if question:
            questions.append(question)
    return questions


def _normalize_question(
    raw: dict[str, Any],
    node: dict[str, Any],
    node_index: int,
    question_index: int,
    *,
    source: str,
) -> dict[str, Any] | None:
    node_id = str(node.get("id") or f"node_{node_index}")
    stem = str(raw.get("stem") or "").strip()
    answer = str(raw.get("answer") or "").strip().upper()
    options = _normalize_options(raw.get("options", []))
    if not stem or answer not in _VALID_CHOICES or len(options) < 2:
        return None
    original_id = str(raw.get("id") or raw.get("question_id") or f"q{question_index}")
    return {
        "id": f"stage_{_safe_id(node_id)}_{question_index}_{_safe_id(original_id)}",
        "source_question_id": original_id,
        "source": source,
        "type": str(raw.get("type") or "single_choice"),
        "outline_node_id": node_id,
        "outline_node_title": str(node.get("title") or ""),
        "stem": stem,
        "options": options,
        "answer": answer,
        "explanation": str(raw.get("explanation") or "").strip(),
        "difficulty": str(raw.get("difficulty") or node.get("difficulty") or "basic"),
        "kg_node_ids": _as_str_list(raw.get("kg_node_ids") or node.get("kg_node_ids") or []),
    }


def _fallback_question(
    session_id: str,
    node: dict[str, Any],
    node_index: int,
    resource_root: str | Path | None,
) -> dict[str, Any]:
    node_id = str(node.get("id") or f"node_{node_index}")
    title = str(node.get("title") or f"第 {node_index} 节").strip()
    key_point = _first_key_point(session_id, node_id, resource_root)
    correct_text = key_point or f"理解「{title}」的核心概念、证据来源和临床意义"
    return {
        "id": f"stage_{_safe_id(node_id)}_1_fallback",
        "source_question_id": "",
        "source": "stage_exam.fallback",
        "type": "single_choice",
        "outline_node_id": node_id,
        "outline_node_title": title,
        "stem": f"关于「{title}」，下列哪项最符合本节的学习重点？",
        "options": [
            "A. 只记忆名词，不理解概念之间的关系",
            f"B. {correct_text}",
            "C. 跳过基础知识，直接套用治疗方案",
            "D. 只关注题目答案，不回到证据和知识点",
        ],
        "answer": "B",
        "explanation": f"本题用于检查是否把握「{title}」的核心学习目标。",
        "difficulty": str(node.get("difficulty") or "basic"),
        "kg_node_ids": _as_str_list(node.get("kg_node_ids") or []),
    }


def _first_key_point(
    session_id: str,
    node_id: str,
    resource_root: str | Path | None,
) -> str:
    handout = _handout_section(session_id, node_id, resource_root)
    content = handout.get("content", {}) if isinstance(handout, dict) else {}
    points = content.get("key_points", []) if isinstance(content, dict) else []
    for point in points or []:
        if not isinstance(point, dict):
            continue
        heading = str(point.get("heading") or "").strip()
        explanation = str(point.get("explanation") or "").strip()
        text = "：".join(part for part in [heading, _clip(explanation, 56)] if part)
        if text:
            return text
    return ""


def _handout_section(
    session_id: str,
    node_id: str,
    resource_root: str | Path | None,
) -> dict[str, Any]:
    if not node_id:
        return {}
    record = get_handout_cache(session_id, node_id, resource_root)
    if record.get("status") != "ready":
        return {}
    section = record.get("handout_section", {})
    return section if isinstance(section, dict) else {}


def _normalize_options(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, dict):
        options: list[str] = []
        seen: set[str] = set()
        for letter in ("A", "B", "C", "D", "E"):
            value = raw.get(letter) if letter in raw else raw.get(letter.lower())
            if str(value or "").strip():
                options.append(f"{letter}. {value}")
                seen.add(letter)
        for key, value in raw.items():
            letter = str(key).strip().upper()
            if letter in seen or not str(key).strip() or not str(value).strip():
                continue
            options.append(f"{str(key).strip()}. {value}")
        return options
    return []


def _strip_option_prefix(text: str) -> str:
    return re.sub(r"^\s*[A-Ea-e]\s*[\.\、\)]\s*", "", str(text or "").strip())


def _is_valid_question(question: dict[str, Any]) -> bool:
    return (
        isinstance(question, dict)
        and str(question.get("id") or "").strip()
        and str(question.get("stem") or "").strip()
        and str(question.get("answer") or "").strip().upper() in _VALID_CHOICES
        and isinstance(question.get("options"), list)
        and len(question.get("options") or []) >= 2
    )


def _source_counts(questions: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for question in questions:
        key = str(question.get("source") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return counts


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _safe_id(value: str) -> str:
    text = str(value or "").strip() or "item"
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text)
    return text.strip("_") or "item"


def _clip(text: str, limit: int) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    return value[: max(1, limit - 1)] + "..."


def _concept_name(question: dict[str, Any]) -> str:
    return str(question.get("outline_node_title") or question.get("outline_node_id") or "stage_exam").strip()


def _update_profile_from_wrong_answer(
    memory: LearningMemory,
    user_id: str,
    node_id: str,
    assessment: dict[str, Any],
    question: dict[str, Any],
) -> None:
    try:
        before = memory.get_or_create_profile(user_id)
        after = update_profile_from_assessment(
            profile=before,
            assessment=assessment,
            outline_node_id=node_id,
            question=question,
        )
        memory.save_profile(user_id, after)
    except Exception:
        return


def _build_remediation(
    overall_score: float,
    weak_section_ids: list[str],
    weak_kg_node_ids: list[str],
) -> dict[str, Any]:
    if overall_score < 0.6:
        action = "review_section_plus_section_quiz"
        message = "阶段卷低于 60%，建议先复习薄弱小节，再做对应小测。"
    elif overall_score < 0.8:
        action = "review_section"
        message = "阶段卷在 60%-80% 之间，建议复习薄弱小节后继续推进。"
    else:
        action = "continue"
        message = "阶段卷达到 80% 以上，可以正常进入后续学习。"
    return {
        "action": action,
        "message": message,
        "weak_section_ids": weak_section_ids,
        "weak_kg_node_ids": weak_kg_node_ids,
    }


def _record_generated_event(
    memory: LearningMemory,
    session: dict[str, Any],
    exam: dict[str, Any],
) -> None:
    try:
        memory.record_event(
            session_id=str(session.get("id") or exam.get("session_id")),
            user_id=str(session.get("user_id") or exam.get("user_id")),
            event_type="stage_exam_generated",
            outline_node_id=None,
            payload={
                "surface": "stage_exam",
                "stage_exam_id": exam.get("exam_id"),
                "question_count": exam.get("summary", {}).get("question_count", 0),
                "section_count": exam.get("summary", {}).get("section_count", 0),
                "question_sources": exam.get("source", {}).get("question_sources", {}),
            },
        )
    except Exception:
        return
