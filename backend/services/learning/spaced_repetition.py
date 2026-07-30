"""Spaced-repetition scheduler for KG-node review planning.

The scheduler is intentionally deterministic and data-light: it estimates an
SM-2 state from the learner's existing mastery / weakness signals, then ranks
review candidates by forgetting risk.  The output is a neutral data structure
that the study-plan service can turn into ``review_section`` plan items.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Any

from .memory import LearningMemory


_LOW_MASTERY_THRESHOLD = 0.68
_MAX_INTERVAL_DAYS = 60
_MIN_EASE_FACTOR = 1.3
_DEFAULT_EASE_FACTOR = 2.5


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _as_float(value: Any, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(result) or math.isinf(result):
        return default
    return result


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def _normalize_kg_ids(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def sm2_quality_from_mastery(
    mastery_score: float,
    *,
    weakness: bool = False,
) -> int:
    """Map a 0-1 mastery score to an SM-2 quality grade."""
    score = _clamp(float(mastery_score), 0.0, 1.0)
    if weakness:
        score -= 0.08
    if score < 0.35:
        return 2
    if score < 0.55:
        return 3
    if score < 0.78:
        return 4
    return 5


def sm2_next_state(
    *,
    quality: int,
    repetition_count: int,
    ease_factor: float = _DEFAULT_EASE_FACTOR,
    previous_interval_days: int | None = None,
) -> dict[str, Any]:
    """Return the next SM-2 state for a review event.

    This follows the classic SM-2 ease-factor update and uses a bounded
    interval to avoid unrealistic schedules when we only have sparse learning
    history.
    """
    q = max(0, min(int(quality), 5))
    ef = max(_MIN_EASE_FACTOR, float(ease_factor))
    ef = ef + (0.1 - (5 - q) * (0.08 + (5 - q) * 0.02))
    ef = max(_MIN_EASE_FACTOR, ef)

    current_rep = max(0, int(repetition_count))
    if q < 3:
        next_rep = 0
        interval = 1
    else:
        next_rep = current_rep + 1
        if next_rep == 1:
            interval = 1
        elif next_rep == 2:
            interval = 6
        else:
            prior = max(1, int(previous_interval_days or 6))
            interval = round(prior * ef)

    return {
        "quality": q,
        "repetition_count": next_rep,
        "ease_factor": round(ef, 3),
        "interval_days": max(1, min(int(interval), _MAX_INTERVAL_DAYS)),
    }


def _estimated_ease_factor(mastery_score: float, difficulty: float) -> float:
    score = _clamp(mastery_score, 0.0, 1.0)
    diff = _clamp(difficulty, 0.0, 1.0)
    return round(_clamp(1.45 + score * 1.25 - diff * 0.25, _MIN_EASE_FACTOR, 2.8), 3)


def _estimated_previous_interval(exposure_count: int, ease_factor: float) -> int:
    exposure = max(0, int(exposure_count))
    if exposure <= 1:
        return 1
    if exposure == 2:
        return 6
    return max(6, min(round(6 * (ease_factor ** max(0, exposure - 2))), _MAX_INTERVAL_DAYS))


def _merge_candidate(
    candidates: dict[str, dict[str, Any]],
    *,
    kg_node_id: str,
    label: str,
    mastery_score: float | None = None,
    confidence: float | None = None,
    exposure_count: int | None = None,
    severity: float | None = None,
    last_seen: Any = None,
    evidence_source: str,
    linked_course_id: str | None = None,
    linked_course_title: str | None = None,
) -> None:
    kg_id = str(kg_node_id or "").strip()
    if not kg_id:
        return
    existing = candidates.setdefault(
        kg_id,
        {
            "kg_node_id": kg_id,
            "label": "",
            "mastery_scores": [],
            "confidence_values": [],
            "exposure_count": 0,
            "severity": 0.0,
            "last_seen_dates": [],
            "evidence_sources": [],
            "linked_course_id": None,
            "linked_course_title": None,
        },
    )
    clean_label = " ".join(str(label or "").split()).strip()
    if clean_label and (not existing["label"] or len(clean_label) < len(existing["label"])):
        existing["label"] = clean_label
    if mastery_score is not None:
        existing["mastery_scores"].append(_clamp(_as_float(mastery_score, 0.5), 0.0, 1.0))
    if confidence is not None:
        existing["confidence_values"].append(_clamp(_as_float(confidence, 0.5), 0.0, 1.0))
    if exposure_count is not None:
        existing["exposure_count"] = max(existing["exposure_count"], max(0, int(exposure_count)))
    if severity is not None:
        existing["severity"] = max(existing["severity"], _clamp(_as_float(severity, 0.0), 0.0, 1.0))
    seen_date = _parse_date(last_seen)
    if seen_date is not None:
        existing["last_seen_dates"].append(seen_date)
    if evidence_source and evidence_source not in existing["evidence_sources"]:
        existing["evidence_sources"].append(evidence_source)
    if linked_course_id and not existing["linked_course_id"]:
        existing["linked_course_id"] = linked_course_id
    if linked_course_title and not existing["linked_course_title"]:
        existing["linked_course_title"] = linked_course_title


def _memory_slot_items(learner_memory: dict[str, Any], slot: str) -> list[dict[str, Any]]:
    data = learner_memory.get(slot)
    if not isinstance(data, dict):
        return []
    return [item for item in data.get("items", []) if isinstance(item, dict)]


def _collect_review_candidates(
    memory: LearningMemory,
    *,
    user_id: str,
    learner_memory: dict[str, Any],
    sessions: list[dict[str, Any]],
    profile: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}

    for item in _memory_slot_items(learner_memory, "weaknesses"):
        severity = _as_float(item.get("severity", item.get("confidence", 0.65)), 0.65)
        label = str(item.get("label") or item.get("text") or "薄弱知识点")
        for kg_id in _normalize_kg_ids(item.get("kg_node_ids")):
            _merge_candidate(
                candidates,
                kg_node_id=kg_id,
                label=label,
                mastery_score=0.5 - severity * 0.25,
                confidence=item.get("confidence"),
                severity=severity,
                last_seen=item.get("updated_at"),
                evidence_source="learner_memory_weakness",
            )

    for item in _memory_slot_items(learner_memory, "mastery"):
        score = _as_float(item.get("score", item.get("mastery")), 0.5)
        if score > _LOW_MASTERY_THRESHOLD:
            continue
        label = str(item.get("label") or item.get("text") or "低掌握知识点")
        for kg_id in _normalize_kg_ids(item.get("kg_node_ids")):
            _merge_candidate(
                candidates,
                kg_node_id=kg_id,
                label=label,
                mastery_score=score,
                confidence=item.get("confidence"),
                severity=max(0.0, 1.0 - score),
                last_seen=item.get("updated_at"),
                evidence_source="learner_memory_mastery",
            )

    for kg_id, raw in (profile.get("mastery_by_kg_node") or {}).items():
        if isinstance(raw, dict):
            score = _as_float(raw.get("score", raw.get("mastery")), 0.5)
            label = str(raw.get("label") or kg_id)
            last_seen = raw.get("updated_at") or raw.get("last_reviewed_at")
        else:
            score = _as_float(raw, 0.5)
            label = str(kg_id)
            last_seen = None
        if score <= _LOW_MASTERY_THRESHOLD:
            _merge_candidate(
                candidates,
                kg_node_id=str(kg_id),
                label=label,
                mastery_score=score,
                severity=max(0.0, 1.0 - score),
                last_seen=last_seen,
                evidence_source="profile_mastery_by_kg_node",
            )

    for row in memory.list_knowledge_trace_states(user_id, limit=200):
        kg_id = str(row.get("kg_node_id") or "").strip()
        if not kg_id:
            continue
        score = _as_float(row.get("p_mastery"), 0.5)
        uncertainty = _as_float(row.get("uncertainty"), 0.5)
        if score > _LOW_MASTERY_THRESHOLD and uncertainty < 0.35 and kg_id not in candidates:
            continue
        _merge_candidate(
            candidates,
            kg_node_id=kg_id,
            label=str(row.get("skill_label") or kg_id),
            mastery_score=score,
            confidence=row.get("confidence"),
            exposure_count=_as_int(row.get("evidence_count"), 0),
            severity=max(0.0, (1.0 - score) * 0.72 + uncertainty * 0.28),
            last_seen=row.get("updated_at"),
            evidence_source="knowledge_tracing_state",
        )

    for session in sessions:
        session_id = str(session.get("id") or "").strip()
        if not session_id:
            continue
        topic = str(session.get("topic") or "").strip()
        for row in memory.get_mastery_by_kg_node(user_id, session_id).values():
            kg_id = str(row.get("kg_node_id") or "").strip()
            if not kg_id or kg_id == "__no_kg_node__":
                continue
            score = _as_float(row.get("mastery_score"), 0.5)
            if score > 0.78 and kg_id not in candidates:
                continue
            _merge_candidate(
                candidates,
                kg_node_id=kg_id,
                label=str(row.get("concept_name") or kg_id),
                mastery_score=score,
                confidence=row.get("confidence"),
                exposure_count=_as_int(row.get("exposure_count"), 0),
                severity=max(0.0, 1.0 - score),
                last_seen=row.get("updated_at"),
                evidence_source="course_mastery_record",
                linked_course_id=session_id,
                linked_course_title=topic,
            )

    for row in memory.get_direct_qa_mastery_by_user(user_id):
        kg_id = str(row.get("kg_node_id") or "").strip()
        if not kg_id:
            continue
        score = _as_float(row.get("mastery_score"), 0.5)
        if score > _LOW_MASTERY_THRESHOLD and kg_id not in candidates:
            continue
        _merge_candidate(
            candidates,
            kg_node_id=kg_id,
            label=str(row.get("concept_name") or kg_id),
            mastery_score=score,
            confidence=row.get("confidence"),
            severity=max(0.0, 1.0 - score),
            last_seen=row.get("updated_at"),
            evidence_source="direct_qa_mastery_record",
        )

    return candidates


def build_spaced_review_schedule(
    memory: LearningMemory,
    *,
    user_id: str,
    target_date: str | date,
    learner_memory: dict[str, Any] | None = None,
    sessions: list[dict[str, Any]] | None = None,
    profile: dict[str, Any] | None = None,
    max_items: int = 3,
) -> list[dict[str, Any]]:
    """Return ranked KG-node review candidates due by *target_date*."""
    if isinstance(target_date, date):
        target = target_date
    else:
        target = date.fromisoformat(str(target_date)[:10])
    learner_memory = learner_memory if isinstance(learner_memory, dict) else memory.get_learner_memory(user_id)
    sessions = sessions if isinstance(sessions, list) else memory.list_sessions_for_user(user_id, limit=8)
    profile = profile if isinstance(profile, dict) else memory.get_or_create_profile(user_id)

    raw_candidates = _collect_review_candidates(
        memory,
        user_id=user_id,
        learner_memory=learner_memory,
        sessions=sessions,
        profile=profile,
    )

    schedule: list[dict[str, Any]] = []
    for candidate in raw_candidates.values():
        scores = candidate["mastery_scores"]
        mastery = min(scores) if scores else 0.45
        confidence_values = candidate["confidence_values"]
        confidence = max(confidence_values) if confidence_values else 0.45
        severity = _clamp(candidate["severity"], 0.0, 1.0)
        difficulty = _clamp((1.0 - mastery) * 0.7 + severity * 0.3, 0.0, 1.0)
        ease_factor = _estimated_ease_factor(mastery, difficulty)
        exposure_count = max(0, int(candidate["exposure_count"]))
        previous_interval = _estimated_previous_interval(exposure_count, ease_factor)
        weakness = any("weakness" in src for src in candidate["evidence_sources"])
        quality = sm2_quality_from_mastery(mastery, weakness=weakness)
        sm2_state = sm2_next_state(
            quality=quality,
            repetition_count=exposure_count,
            ease_factor=ease_factor,
            previous_interval_days=previous_interval,
        )

        last_seen = max(candidate["last_seen_dates"]) if candidate["last_seen_dates"] else target
        elapsed_days = max(0, (target - last_seen).days)
        interval_days = int(sm2_state["interval_days"])
        next_review = last_seen + timedelta(days=interval_days)
        overdue_days = max(0, (target - next_review).days)
        stability_days = max(1.0, interval_days * (0.85 + confidence * 0.3))
        retention = round(math.exp(-elapsed_days / stability_days), 3)
        overdue_factor = 1.0 + min(2.0, overdue_days / max(1, interval_days))
        difficulty_factor = 0.85 + difficulty * 0.65
        weakness_weight = 1.18 if weakness else 1.0
        raw_priority = (1.0 - mastery) * overdue_factor * difficulty_factor * weakness_weight
        if next_review <= target:
            raw_priority += 0.18
        priority = _clamp(0.28 + raw_priority / 2.8, 0.0, 0.98)

        # Keep the candidate if it is already due, nearly due, or clearly weak.
        if next_review > target and mastery > 0.55 and not weakness:
            continue

        label = str(candidate["label"] or candidate["kg_node_id"]).strip()
        schedule.append({
            "kg_node_id": candidate["kg_node_id"],
            "label": label,
            "mastery_score": round(mastery, 3),
            "confidence": round(confidence, 3),
            "difficulty": round(difficulty, 3),
            "ease_factor": sm2_state["ease_factor"],
            "quality": sm2_state["quality"],
            "repetition_count": sm2_state["repetition_count"],
            "interval_days": interval_days,
            "last_reviewed_at": last_seen.isoformat(),
            "next_review_date": next_review.isoformat(),
            "days_since_review": elapsed_days,
            "overdue_days": overdue_days,
            "retention_estimate": retention,
            "priority": round(priority, 3),
            "evidence_sources": candidate["evidence_sources"],
            "linked_course_id": candidate["linked_course_id"],
            "linked_course_title": candidate["linked_course_title"],
            "trace": {
                "algorithm": "sm2_kg_node_v1",
                "priority_formula": "(1-mastery) * overdue_factor * difficulty_factor * weakness_weight",
                "overdue_factor": round(overdue_factor, 3),
                "difficulty_factor": round(difficulty_factor, 3),
                "weakness_weight": weakness_weight,
            },
        })

    schedule.sort(
        key=lambda item: (
            -float(item["priority"]),
            str(item["next_review_date"]),
            str(item["label"]),
        )
    )
    return schedule[: max(1, min(int(max_items), 8))]
