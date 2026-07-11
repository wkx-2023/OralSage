"""Personalized final-exam assembly from the linked oral question bank."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .kag_paths import project_root, resolve_kag_dir


EXAM_SCHEMA_VERSION = "learning.final_exam.v1"
ANSWER_KEY_SCHEMA_VERSION = "learning.final_exam_answer_key.v1"
SUBMISSION_SCHEMA_VERSION = "learning.final_exam_submission.v1"
DEFAULT_QUESTION_COUNT = 10

_CATALOG_LOCK = threading.RLock()
_CATALOG_CACHE: dict[tuple[str, int, int, str, int, int], dict[str, Any]] = {}
_SESSION_LOCKS_GUARD = threading.Lock()
_SESSION_LOCKS: dict[str, threading.Lock] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dedupe(values: list[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    return [value]


def _env_question_count() -> int:
    try:
        value = int(os.getenv("LEARNING_FINAL_EXAM_QUESTION_COUNT", "10"))
    except ValueError:
        value = DEFAULT_QUESTION_COUNT
    return max(1, min(value, 50))


def _session_lock(session_id: str) -> threading.Lock:
    with _SESSION_LOCKS_GUARD:
        return _SESSION_LOCKS.setdefault(session_id, threading.Lock())


def candidate_question_bank_paths() -> list[Path]:
    """Return supported full-question-bank locations in priority order."""
    root = project_root()
    configured = str(os.getenv("LEARNING_QUESTION_BANK_PATH") or "").strip()
    paths: list[Path] = []
    if configured:
        configured_path = Path(configured).expanduser()
        paths.append(
            configured_path
            if configured_path.is_absolute()
            else (root / configured_path)
        )
    paths.extend([
        root / "data" / "question_bank_core_linked" / "question_bank_with_kg_core.json",
        root / "data" / "question_bank" / "question_bank.json",
    ])
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path.resolve())
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def resolve_question_bank_path(explicit_path: str | Path | None = None) -> Path | None:
    if explicit_path is not None:
        path = Path(explicit_path).expanduser()
        if not path.is_absolute():
            path = project_root() / path
        return path.resolve() if path.is_file() else None
    for path in candidate_question_bank_paths():
        if path.is_file():
            return path.resolve()
    return None


def _load_records(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".jsonl":
        records: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    records.append(value)
        return records

    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if not isinstance(value, dict):
        return []
    for key in ("records", "questions", "data", "items"):
        records = value.get(key)
        if isinstance(records, list):
            return [item for item in records if isinstance(item, dict)]
    return []


def _load_kag_question_index(kag_dir: Path) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    path = kag_dir / "questions.jsonl"
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            try:
                question = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(question, dict):
                continue
            keys = _dedupe([
                question.get("source_question_id"),
                question.get("question_id"),
            ])
            for key in keys:
                index[key] = question
    return index


def _normalize_options(value: Any) -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    if isinstance(value, dict):
        for key, text in value.items():
            label = str(key or "").strip().upper()
            content = str(text or "").strip()
            if label and content:
                options.append({"key": label, "text": content})
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, dict):
                label = str(
                    item.get("key")
                    or item.get("label")
                    or item.get("id")
                    or chr(65 + index)
                ).strip().upper()
                content = str(
                    item.get("text")
                    or item.get("content")
                    or item.get("value")
                    or ""
                ).strip()
            else:
                raw = str(item or "").strip()
                match = re.match(r"^\s*([A-Z])\s*[.、:：)）]\s*(.+)$", raw, re.I)
                label = match.group(1).upper() if match else chr(65 + index)
                content = match.group(2).strip() if match else raw
            if label and content:
                options.append({"key": label, "text": content})
    options.sort(key=lambda item: item["key"])
    return options


def _normalize_answer(value: Any, valid_labels: set[str]) -> str:
    raw_values = _as_list(value)
    labels: list[str] = []
    for raw in raw_values:
        labels.extend(re.findall(r"[A-Z]", str(raw or "").upper()))
    labels = _dedupe(labels)
    if not labels or any(label not in valid_labels for label in labels):
        return ""
    return "".join(sorted(labels))


def _linked_kg_node_ids(
    record: dict[str, Any],
    kag_question: dict[str, Any] | None,
) -> list[str]:
    values: list[Any] = []
    values.extend(_as_list(record.get("kg_node_ids")))
    values.extend(_as_list(record.get("matched_core_uids")))
    values.append(record.get("primary_node_id"))

    linking = record.get("kg_core_linking") or {}
    if isinstance(linking, dict):
        if linking.get("status") in {None, "matched"}:
            values.append(linking.get("primary_core_uid"))
            values.extend(_as_list(linking.get("matched_core_uids")))

    if isinstance(kag_question, dict):
        values.append(kag_question.get("primary_node_id"))
    return _dedupe(values)


def _normalize_question(
    record: dict[str, Any],
    kag_question: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    question_id = str(
        record.get("id")
        or record.get("question_id")
        or record.get("source_question_id")
        or ""
    ).strip()
    stem = str(record.get("stem") or record.get("question") or "").strip()
    options = _normalize_options(
        record.get("options")
        or record.get("choices")
        or record.get("alternatives")
    )
    valid_labels = {item["key"] for item in options}
    answer = _normalize_answer(record.get("answer"), valid_labels)
    kg_node_ids = _linked_kg_node_ids(record, kag_question)
    if not question_id or not stem:
        return None, "missing_id_or_stem"
    if len(options) < 2:
        return None, "missing_options"
    if not answer:
        return None, "invalid_answer"
    if not kg_node_ids:
        return None, "missing_kg_link"

    linking = record.get("kg_core_linking") or {}
    if not isinstance(linking, dict):
        linking = {}
    match_status = str(
        (kag_question or {}).get("match_status")
        or linking.get("status")
        or record.get("match_status")
        or "linked"
    )
    knowledge_points = _dedupe(
        _as_list(record.get("knowledge_points"))
        + [
            record.get("primary_knowledge_point"),
            (kag_question or {}).get("primary_knowledge_point"),
        ]
    )
    return {
        "id": question_id,
        "type": "multiple_choice" if len(answer) > 1 else "single_choice",
        "stem": stem,
        "options": options,
        "answer": answer,
        "explanation": str(
            record.get("explanation")
            or record.get("analysis")
            or ""
        ).strip(),
        "kg_node_ids": kg_node_ids,
        "knowledge_points": knowledge_points,
        "subject": str(
            record.get("subject_name")
            or record.get("subject")
            or ""
        ).strip(),
        "chapter": str(
            record.get("chapter_name")
            or record.get("chapter")
            or ""
        ).strip(),
        "source_exam_id": str(record.get("source_exam_id") or "").strip(),
        "match_status": match_status,
    }, None


def load_question_catalog(
    *,
    question_bank_path: str | Path | None = None,
    kag_dir: str | Path | None = None,
) -> dict[str, Any]:
    bank_path = resolve_question_bank_path(question_bank_path)
    active_kag_dir = Path(kag_dir) if kag_dir is not None else resolve_kag_dir()
    if bank_path is None:
        return {
            "status": "unavailable",
            "questions": [],
            "question_bank_path": "",
            "kag_dir": str(active_kag_dir),
            "loaded_question_count": 0,
            "usable_question_count": 0,
            "rejected_counts": {"question_bank_not_found": 1},
            "candidate_paths": [str(path) for path in candidate_question_bank_paths()],
        }

    bank_stat = bank_path.stat()
    kag_path = active_kag_dir / "questions.jsonl"
    kag_stat = kag_path.stat()
    cache_key = (
        str(bank_path),
        bank_stat.st_mtime_ns,
        bank_stat.st_size,
        str(active_kag_dir.resolve()),
        kag_stat.st_mtime_ns,
        kag_stat.st_size,
    )
    with _CATALOG_LOCK:
        cached = _CATALOG_CACHE.get(cache_key)
        if cached is not None:
            return cached

    records = _load_records(bank_path)
    kag_index = _load_kag_question_index(active_kag_dir)
    questions: list[dict[str, Any]] = []
    rejected_counts: dict[str, int] = {}
    for record in records:
        record_id = str(
            record.get("id")
            or record.get("question_id")
            or record.get("source_question_id")
            or ""
        ).strip()
        normalized, rejected_reason = _normalize_question(
            record,
            kag_index.get(record_id),
        )
        if normalized is None:
            reason = rejected_reason or "invalid_question"
            rejected_counts[reason] = rejected_counts.get(reason, 0) + 1
            continue
        questions.append(normalized)

    result = {
        "status": "ready" if questions else "unavailable",
        "questions": questions,
        "question_bank_path": str(bank_path),
        "kag_dir": str(active_kag_dir),
        "loaded_question_count": len(records),
        "usable_question_count": len(questions),
        "rejected_counts": rejected_counts,
        "candidate_paths": [str(path) for path in candidate_question_bank_paths()],
    }
    with _CATALOG_LOCK:
        _CATALOG_CACHE.clear()
        _CATALOG_CACHE[cache_key] = result
    return result


def _course_kg_nodes(outline: dict[str, Any]) -> tuple[list[str], dict[str, list[str]]]:
    kg_node_ids: list[str] = []
    outline_by_kg: dict[str, list[str]] = {}
    for node in outline.get("nodes", []) or []:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("id") or "").strip()
        for kg_id in _dedupe(_as_list(node.get("kg_node_ids"))):
            kg_node_ids.append(kg_id)
            outline_by_kg.setdefault(kg_id, []).append(node_id)
    return _dedupe(kg_node_ids), outline_by_kg


def _weakness_signals(
    session_summary: dict[str, Any],
    course_kg_node_ids: list[str],
) -> dict[str, Any]:
    course_set = set(course_kg_node_ids)
    wrong: list[str] = []
    for event in session_summary.get("events", []) or []:
        if not isinstance(event, dict) or event.get("event_type") != "answer_submitted":
            continue
        payload = event.get("payload") or {}
        if isinstance(payload, dict) and payload.get("is_correct") is False:
            wrong.extend(_as_list(payload.get("kg_node_ids")))
            wrong.extend(_as_list(payload.get("related_kg_node_ids")))

    memory_weak: list[str] = []
    learner_memory = session_summary.get("learner_memory") or {}
    weaknesses = learner_memory.get("weaknesses") or {}
    for item in weaknesses.get("items", []) if isinstance(weaknesses, dict) else []:
        if isinstance(item, dict):
            memory_weak.extend(_as_list(item.get("kg_node_ids")))

    profile = session_summary.get("profile") or {}
    memory_weak.extend(_as_list(profile.get("known_weak_points")))

    mastery_scores: dict[str, float] = {}
    for record in session_summary.get("mastery", []) or []:
        if not isinstance(record, dict):
            continue
        kg_id = str(record.get("kg_node_id") or "").strip()
        if kg_id not in course_set:
            continue
        try:
            score = float(record.get("mastery_score", 0.5))
        except (TypeError, ValueError):
            score = 0.5
        if kg_id not in mastery_scores:
            mastery_scores[kg_id] = max(0.0, min(1.0, score))

    wrong_ids = [kg_id for kg_id in _dedupe(wrong) if kg_id in course_set]
    memory_weak_ids = [
        kg_id for kg_id in _dedupe(memory_weak) if kg_id in course_set
    ]
    low_mastery_ids = [
        kg_id
        for kg_id in course_kg_node_ids
        if kg_id in mastery_scores and mastery_scores[kg_id] < 0.6
    ]
    weak_ids = _dedupe(wrong_ids + memory_weak_ids + low_mastery_ids)
    return {
        "wrong_kg_node_ids": wrong_ids,
        "memory_weak_kg_node_ids": memory_weak_ids,
        "low_mastery_kg_node_ids": low_mastery_ids,
        "weak_kg_node_ids": weak_ids,
        "mastery_scores": mastery_scores,
    }


def _stable_order(session_id: str, question_id: str) -> int:
    digest = hashlib.sha256(
        f"{session_id}\0{question_id}".encode("utf-8")
    ).hexdigest()
    return int(digest[:16], 16)


def _selection_reason(
    kg_node_ids: set[str],
    signals: dict[str, Any],
) -> str:
    if kg_node_ids.intersection(signals["wrong_kg_node_ids"]):
        return "previous_wrong_answer"
    if kg_node_ids.intersection(signals["memory_weak_kg_node_ids"]):
        return "learner_memory_weakness"
    if kg_node_ids.intersection(signals["low_mastery_kg_node_ids"]):
        return "low_mastery"
    return "course_coverage"


def _rank_question(
    question: dict[str, Any],
    *,
    session_id: str,
    signals: dict[str, Any],
) -> tuple[int, float, int, int]:
    kg_ids = set(question.get("kg_node_ids", []))
    reason = _selection_reason(kg_ids, signals)
    reason_rank = {
        "previous_wrong_answer": 0,
        "learner_memory_weakness": 1,
        "low_mastery": 2,
        "course_coverage": 3,
    }[reason]
    mastery_scores = signals["mastery_scores"]
    score = min((mastery_scores.get(kg_id, 0.5) for kg_id in kg_ids), default=0.5)
    match_rank = 0 if question.get("match_status") in {"matched", "linked"} else 1
    return (
        reason_rank,
        score,
        match_rank,
        _stable_order(session_id, str(question.get("id") or "")),
    )


def _select_questions(
    *,
    session_id: str,
    catalog_questions: list[dict[str, Any]],
    course_kg_node_ids: list[str],
    outline_by_kg: dict[str, list[str]],
    signals: dict[str, Any],
    question_count: int,
) -> tuple[list[dict[str, Any]], int]:
    course_set = set(course_kg_node_ids)
    candidates = [
        question
        for question in catalog_questions
        if course_set.intersection(question.get("kg_node_ids", []))
    ]
    candidates.sort(
        key=lambda question: _rank_question(
            question,
            session_id=session_id,
            signals=signals,
        )
    )

    wrong = set(signals["wrong_kg_node_ids"])
    memory_weak = set(signals["memory_weak_kg_node_ids"])
    mastery_scores = signals["mastery_scores"]
    target_order = sorted(
        course_kg_node_ids,
        key=lambda kg_id: (
            0 if kg_id in wrong else 1,
            0 if kg_id in memory_weak else 1,
            mastery_scores.get(kg_id, 0.5),
            course_kg_node_ids.index(kg_id),
        ),
    )

    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    for kg_id in target_order:
        if len(selected) >= question_count:
            break
        candidate = next(
            (
                item
                for item in candidates
                if item["id"] not in selected_ids
                and kg_id in item.get("kg_node_ids", [])
            ),
            None,
        )
        if candidate is not None:
            selected.append(candidate)
            selected_ids.add(candidate["id"])

    for candidate in candidates:
        if len(selected) >= question_count:
            break
        if candidate["id"] not in selected_ids:
            selected.append(candidate)
            selected_ids.add(candidate["id"])

    enriched: list[dict[str, Any]] = []
    for question in selected:
        matched_course_ids = [
            kg_id
            for kg_id in course_kg_node_ids
            if kg_id in question.get("kg_node_ids", [])
        ]
        outline_node_ids = _dedupe([
            outline_node_id
            for kg_id in matched_course_ids
            for outline_node_id in outline_by_kg.get(kg_id, [])
        ])
        enriched.append({
            **question,
            "course_kg_node_ids": matched_course_ids,
            "outline_node_ids": outline_node_ids,
            "selection_reason": _selection_reason(
                set(matched_course_ids),
                signals,
            ),
        })
    return enriched, len(candidates)


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
        os.replace(str(tmp_path), str(path))
    finally:
        tmp_path.unlink(missing_ok=True)


def final_exam_path(
    session_id: str,
    resource_root: str | Path | None = None,
) -> Path:
    root = Path(resource_root) if resource_root else project_root() / "data" / "learning" / "sessions"
    return root / session_id / "final_exam" / "exam.v1.json"


def final_exam_answer_key_path(
    session_id: str,
    resource_root: str | Path | None = None,
) -> Path:
    return final_exam_path(session_id, resource_root).with_name("answer_key.v1.json")


def final_exam_submission_path(
    session_id: str,
    resource_root: str | Path | None = None,
) -> Path:
    return final_exam_path(session_id, resource_root).with_name("submission.v1.json")


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def get_final_exam(
    session_id: str,
    resource_root: str | Path | None = None,
) -> dict[str, Any]:
    path = final_exam_path(session_id, resource_root)
    if not path.is_file():
        return {
            "schema_version": EXAM_SCHEMA_VERSION,
            "session_id": session_id,
            "status": "missing",
            "question_count": 0,
            "resource_path": str(path),
        }
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "schema_version": EXAM_SCHEMA_VERSION,
            "session_id": session_id,
            "status": "failed",
            "question_count": 0,
            "resource_path": str(path),
            "error": f"exam_resource_invalid: {exc}",
        }
    if not isinstance(value, dict):
        return {
            "schema_version": EXAM_SCHEMA_VERSION,
            "session_id": session_id,
            "status": "failed",
            "question_count": 0,
            "resource_path": str(path),
            "error": "exam_resource_is_not_an_object",
        }
    return {**value, "resource_path": str(path)}


def _exam_revision(value: dict[str, Any] | None) -> int:
    if not isinstance(value, dict):
        return 0
    try:
        revision = int(value.get("exam_revision", 0))
    except (TypeError, ValueError):
        revision = 0
    return max(0, revision)


def _option_labels(question: dict[str, Any]) -> list[str]:
    labels = [
        str(option.get("key") or "").strip().upper()
        for option in question.get("options", []) or []
        if isinstance(option, dict)
    ]
    return _dedupe([label for label in labels if label])


def _normalize_submission_option(value: Any, valid_labels: list[str]) -> str | None:
    text = str(value or "").strip()
    if not text:
        return ""
    found = _dedupe(re.findall(r"[A-Z]", text.upper()))
    valid = set(valid_labels)
    if not found or any(label not in valid for label in found):
        return None
    label_order = {label: index for index, label in enumerate(valid_labels)}
    return "".join(sorted(found, key=lambda label: label_order.get(label, 999)))


def _canonical_submission_answers(
    answers: list[dict[str, Any]],
    questions_by_id: dict[str, dict[str, Any]],
) -> tuple[dict[str, str] | None, list[dict[str, str]]]:
    submitted: dict[str, str] = {}
    errors: list[dict[str, str]] = []
    for index, item in enumerate(answers or []):
        if not isinstance(item, dict):
            errors.append({
                "code": "invalid_answer_item",
                "message": f"answers[{index}] must be an object",
            })
            continue
        question_id = str(item.get("question_id") or "").strip()
        if not question_id:
            errors.append({
                "code": "missing_question_id",
                "message": f"answers[{index}].question_id is required",
            })
            continue
        if question_id in submitted:
            errors.append({
                "code": "duplicate_question_id",
                "question_id": question_id,
                "message": f"Duplicate answer for question {question_id}",
            })
            continue
        question = questions_by_id.get(question_id)
        if question is None:
            errors.append({
                "code": "unknown_question_id",
                "question_id": question_id,
                "message": f"Unknown question_id {question_id}",
            })
            continue
        normalized = _normalize_submission_option(
            item.get("selected_option", ""),
            _option_labels(question),
        )
        if normalized is None:
            errors.append({
                "code": "invalid_selected_option",
                "question_id": question_id,
                "message": f"Invalid selected_option for question {question_id}",
            })
            continue
        submitted[question_id] = normalized

    if errors:
        return None, errors
    canonical = {question_id: submitted.get(question_id, "") for question_id in questions_by_id}
    return canonical, []


def _answer_key_by_id(answer_key: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in answer_key.get("answers", []) or []:
        if not isinstance(item, dict):
            continue
        question_id = str(item.get("question_id") or "").strip()
        answer = str(item.get("answer") or "").strip().upper()
        if question_id and answer:
            result[question_id] = {
                "answer": answer,
                "explanation": str(item.get("explanation") or "").strip(),
            }
    return result


def _grade_questions(
    *,
    exam: dict[str, Any],
    answer_key: dict[str, Any],
    answers_by_question_id: dict[str, str],
) -> list[dict[str, Any]]:
    answer_map = _answer_key_by_id(answer_key)
    results: list[dict[str, Any]] = []
    for question in exam.get("questions", []) or []:
        if not isinstance(question, dict):
            continue
        question_id = str(question.get("id") or "").strip()
        key = answer_map.get(question_id, {})
        correct_answer = str(key.get("answer") or "").strip().upper()
        selected = answers_by_question_id.get(question_id, "")
        is_unanswered = selected == ""
        is_correct = bool(correct_answer and selected == correct_answer)
        results.append({
            "question_id": question_id,
            "type": question.get("type", "single_choice"),
            "stem": question.get("stem", ""),
            "options": question.get("options", []),
            "selected_option": selected,
            "correct_answer": correct_answer,
            "is_correct": is_correct,
            "is_unanswered": is_unanswered,
            "score": 1.0 if is_correct else 0.0,
            "explanation": key.get("explanation", ""),
            "kg_node_ids": _dedupe(_as_list(question.get("kg_node_ids"))),
            "course_kg_node_ids": _dedupe(_as_list(question.get("course_kg_node_ids"))),
            "outline_node_ids": _dedupe(_as_list(question.get("outline_node_ids"))),
            "knowledge_points": _dedupe(_as_list(question.get("knowledge_points"))),
            "selection_reason": question.get("selection_reason", ""),
            "subject": question.get("subject", ""),
            "chapter": question.get("chapter", ""),
        })
    return results


def _wrong_question_view(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "question_id": result.get("question_id", ""),
        "type": result.get("type", "single_choice"),
        "stem": result.get("stem", ""),
        "options": result.get("options", []),
        "selected_option": result.get("selected_option", ""),
        "correct_answer": result.get("correct_answer", ""),
        "explanation": result.get("explanation", ""),
        "kg_node_ids": result.get("kg_node_ids", []),
        "course_kg_node_ids": result.get("course_kg_node_ids", []),
        "outline_node_ids": result.get("outline_node_ids", []),
        "knowledge_points": result.get("knowledge_points", []),
        "is_unanswered": bool(result.get("is_unanswered")),
    }


def _public_submission_response(
    submission: dict[str, Any],
    *,
    cache_hit: bool,
) -> dict[str, Any]:
    question_results = submission.get("question_results", []) or []
    wrong_questions = [
        _wrong_question_view(result)
        for result in question_results
        if isinstance(result, dict) and not result.get("is_correct")
    ]
    return {
        "schema_version": SUBMISSION_SCHEMA_VERSION,
        "status": "graded",
        "exam_id": submission.get("exam_id", ""),
        "exam_revision": submission.get("exam_revision", 1),
        "session_id": submission.get("session_id", ""),
        "score": submission.get("score", 0.0),
        "correct_count": submission.get("correct_count", 0),
        "wrong_count": submission.get("wrong_count", 0),
        "unanswered_count": submission.get("unanswered_count", 0),
        "question_count": submission.get("question_count", 0),
        "wrong_questions": wrong_questions,
        "submitted_at": submission.get("submitted_at", ""),
        "cache_hit": cache_hit,
        "resource_path": submission.get("resource_path", ""),
        "memory_update": submission.get("memory_update"),
    }


def submit_final_exam(
    *,
    session_id: str,
    answers: list[dict[str, Any]],
    resource_root: str | Path | None = None,
    on_first_submit: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate, grade and persist a final-exam submission."""
    exam_path = final_exam_path(session_id, resource_root)
    answer_key_path = final_exam_answer_key_path(session_id, resource_root)
    submission_path = final_exam_submission_path(session_id, resource_root)

    with _session_lock(session_id):
        exam = get_final_exam(session_id, resource_root)
        if exam.get("status") == "missing":
            return {
                "schema_version": SUBMISSION_SCHEMA_VERSION,
                "status": "exam_not_found",
                "session_id": session_id,
                "error": "final_exam_not_found",
            }
        if exam.get("status") not in {"ready", "partial"}:
            return {
                "schema_version": SUBMISSION_SCHEMA_VERSION,
                "status": "exam_not_ready",
                "session_id": session_id,
                "exam_id": exam.get("exam_id"),
                "error": exam.get("error") or "final_exam_not_ready",
            }
        answer_key = _read_json_object(answer_key_path)
        if not answer_key:
            return {
                "schema_version": SUBMISSION_SCHEMA_VERSION,
                "status": "exam_not_ready",
                "session_id": session_id,
                "exam_id": exam.get("exam_id"),
                "error": "answer_key_not_found_or_invalid",
            }
        if answer_key.get("exam_id") != exam.get("exam_id"):
            return {
                "schema_version": SUBMISSION_SCHEMA_VERSION,
                "status": "exam_not_ready",
                "session_id": session_id,
                "exam_id": exam.get("exam_id"),
                "error": "answer_key_exam_mismatch",
            }

        questions = [
            question
            for question in exam.get("questions", []) or []
            if isinstance(question, dict) and str(question.get("id") or "").strip()
        ]
        questions_by_id = {
            str(question.get("id") or "").strip(): question
            for question in questions
        }
        if not questions_by_id:
            return {
                "schema_version": SUBMISSION_SCHEMA_VERSION,
                "status": "exam_not_ready",
                "session_id": session_id,
                "exam_id": exam.get("exam_id"),
                "error": "final_exam_has_no_questions",
            }

        answers_by_question_id, errors = _canonical_submission_answers(
            answers,
            questions_by_id,
        )
        if errors:
            return {
                "schema_version": SUBMISSION_SCHEMA_VERSION,
                "status": "invalid_submission",
                "session_id": session_id,
                "exam_id": exam.get("exam_id"),
                "errors": errors,
            }
        assert answers_by_question_id is not None

        existing = _read_json_object(submission_path)
        if existing and existing.get("exam_id") == exam.get("exam_id"):
            existing_answers = existing.get("answers_by_question_id", {})
            if existing_answers == answers_by_question_id:
                existing["resource_path"] = str(submission_path)
                return _public_submission_response(existing, cache_hit=True)
            return {
                "schema_version": SUBMISSION_SCHEMA_VERSION,
                "status": "exam_already_submitted",
                "session_id": session_id,
                "exam_id": exam.get("exam_id"),
                "error": "This final exam has already been submitted with different answers.",
            }

        question_results = _grade_questions(
            exam=exam,
            answer_key=answer_key,
            answers_by_question_id=answers_by_question_id,
        )
        correct_count = sum(1 for result in question_results if result["is_correct"])
        unanswered_count = sum(1 for result in question_results if result["is_unanswered"])
        wrong_count = sum(
            1
            for result in question_results
            if not result["is_correct"] and not result["is_unanswered"]
        )
        question_count = len(question_results)
        score = round(correct_count / question_count, 4) if question_count else 0.0
        submitted_at = _now()
        submission = {
            "schema_version": SUBMISSION_SCHEMA_VERSION,
            "exam_id": exam.get("exam_id", ""),
            "exam_revision": exam.get("exam_revision", 1),
            "session_id": session_id,
            "user_id": exam.get("user_id", ""),
            "question_count": question_count,
            "score": score,
            "correct_count": correct_count,
            "wrong_count": wrong_count,
            "unanswered_count": unanswered_count,
            "answers_by_question_id": answers_by_question_id,
            "question_results": question_results,
            "submitted_at": submitted_at,
            "source_exam_path": str(exam_path),
        }

        if on_first_submit is not None:
            try:
                submission["memory_update"] = on_first_submit(submission)
            except Exception as exc:  # noqa: BLE001 - API must report persistence failure
                return {
                    "schema_version": SUBMISSION_SCHEMA_VERSION,
                    "status": "memory_persistence_failed",
                    "session_id": session_id,
                    "exam_id": exam.get("exam_id"),
                    "error": str(exc),
                }

        submission["resource_path"] = str(submission_path)
        _write_json_atomic(submission_path, submission)
        return _public_submission_response(submission, cache_hit=False)


def build_and_save_final_exam(
    *,
    session_id: str,
    user_id: str,
    topic: str,
    outline: dict[str, Any],
    session_summary: dict[str, Any],
    resource_root: str | Path | None = None,
    question_bank_path: str | Path | None = None,
    kag_dir: str | Path | None = None,
    question_count: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Build one deterministic, weakness-aware exam and persist it."""
    target_count = max(1, min(int(question_count or _env_question_count()), 50))
    path = final_exam_path(session_id, resource_root)
    answer_key_path = final_exam_answer_key_path(session_id, resource_root)

    with _session_lock(session_id):
        existing = get_final_exam(session_id, resource_root)
        if not force and existing.get("status") in {"ready", "partial"}:
            return {**existing, "cache_hit": True}
        exam_revision = (
            _exam_revision(existing) + 1
            if force and _exam_revision(existing) > 0
            else max(1, _exam_revision(existing) or 1)
        )

        course_kg_node_ids, outline_by_kg = _course_kg_nodes(outline)
        signals = _weakness_signals(session_summary, course_kg_node_ids)
        try:
            catalog = load_question_catalog(
                question_bank_path=question_bank_path,
                kag_dir=kag_dir,
            )
        except Exception as exc:
            catalog = {
                "status": "unavailable",
                "questions": [],
                "question_bank_path": "",
                "kag_dir": str(kag_dir or ""),
                "loaded_question_count": 0,
                "usable_question_count": 0,
                "rejected_counts": {"catalog_load_failed": 1},
                "error": str(exc),
            }

        selected, matched_candidate_count = _select_questions(
            session_id=session_id,
            catalog_questions=catalog.get("questions", []),
            course_kg_node_ids=course_kg_node_ids,
            outline_by_kg=outline_by_kg,
            signals=signals,
            question_count=target_count,
        )
        covered_kg_node_ids = _dedupe([
            kg_id
            for question in selected
            for kg_id in question.get("course_kg_node_ids", [])
        ])
        uncovered_kg_node_ids = [
            kg_id for kg_id in course_kg_node_ids if kg_id not in covered_kg_node_ids
        ]

        if len(selected) >= target_count:
            status = "ready"
            error = ""
        elif selected:
            status = "partial"
            error = "insufficient_matched_questions"
        elif not course_kg_node_ids:
            status = "unavailable"
            error = "outline_has_no_kg_node_ids"
        elif catalog.get("status") != "ready":
            status = "unavailable"
            error = catalog.get("error") or "complete_question_bank_not_available"
        else:
            status = "unavailable"
            error = "no_questions_match_course_kg_nodes"

        exam_id = "exam_" + hashlib.sha256(
            f"{session_id}\0{exam_revision}\0".encode("utf-8")
            + "\0".join(question["id"] for question in selected).encode("utf-8")
        ).hexdigest()[:16]
        student_questions = [
            {
                key: value
                for key, value in question.items()
                if key not in {"answer", "explanation"}
            }
            for question in selected
        ]
        exam = {
            "schema_version": EXAM_SCHEMA_VERSION,
            "exam_id": exam_id,
            "exam_revision": exam_revision,
            "session_id": session_id,
            "user_id": user_id,
            "title": f"{topic}综合试卷" if topic else "课程综合试卷",
            "status": status,
            "question_count": len(student_questions),
            "target_question_count": target_count,
            "questions": student_questions,
            "coverage": {
                "course_kg_node_ids": course_kg_node_ids,
                "covered_kg_node_ids": covered_kg_node_ids,
                "uncovered_kg_node_ids": uncovered_kg_node_ids,
                "coverage_ratio": (
                    round(len(covered_kg_node_ids) / len(course_kg_node_ids), 4)
                    if course_kg_node_ids
                    else 0.0
                ),
                "wrong_kg_node_ids": signals["wrong_kg_node_ids"],
                "memory_weak_kg_node_ids": signals["memory_weak_kg_node_ids"],
                "low_mastery_kg_node_ids": signals["low_mastery_kg_node_ids"],
                "weak_kg_node_ids": signals["weak_kg_node_ids"],
            },
            "selection_trace": {
                "mode": "linked_question_bank_weakness_first",
                "question_bank_path": catalog.get("question_bank_path", ""),
                "kag_dir": catalog.get("kag_dir", ""),
                "loaded_question_count": catalog.get("loaded_question_count", 0),
                "usable_question_count": catalog.get("usable_question_count", 0),
                "matched_candidate_count": matched_candidate_count,
                "rejected_counts": catalog.get("rejected_counts", {}),
                "deterministic": True,
            },
            "error": error or None,
            "created_at": _now(),
        }
        answer_key = {
            "schema_version": ANSWER_KEY_SCHEMA_VERSION,
            "exam_id": exam_id,
            "exam_revision": exam_revision,
            "session_id": session_id,
            "answers": [
                {
                    "question_id": question["id"],
                    "answer": question["answer"],
                    "explanation": question["explanation"],
                }
                for question in selected
            ],
            "created_at": exam["created_at"],
        }
        _write_json_atomic(answer_key_path, answer_key)
        _write_json_atomic(path, exam)
        return {
            **exam,
            "resource_path": str(path),
            "cache_hit": False,
        }
