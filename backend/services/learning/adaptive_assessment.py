"""Adaptive assessment utilities based on Bloom levels and IRT calibration.

The module is intentionally provider-agnostic.  It annotates generated or
selected questions with measurement metadata, then updates item statistics from
submitted answers.  Callers can use the same functions for topic quizzes,
stage exams, comprehensive exams, and later special-practice surfaces.
"""

from __future__ import annotations

import hashlib
import math
from typing import Any


BLOOM_LEVELS = (
    "remember",
    "understand",
    "apply",
    "analyze",
    "evaluate",
    "create",
)

BLOOM_LABELS = {
    "remember": "记忆",
    "understand": "理解",
    "apply": "应用",
    "analyze": "分析",
    "evaluate": "评价",
    "create": "创造",
}


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


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, tuple):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _logit(probability: float) -> float:
    p = _clamp(probability, 1e-4, 1.0 - 1e-4)
    return math.log(p / (1.0 - p))


def _stable_bucket(value: str, size: int) -> int:
    if size <= 1:
        return 0
    digest = hashlib.sha1(value.encode("utf-8", errors="ignore")).hexdigest()
    return int(digest[:8], 16) % size


def _mastery_value(raw: Any) -> float | None:
    if isinstance(raw, dict):
        for key in ("p_mastery", "mastery", "score", "mastery_score"):
            if key in raw:
                return _clamp(_as_float(raw.get(key), 0.5), 0.0, 1.0)
        return None
    if isinstance(raw, (int, float, str)):
        return _clamp(_as_float(raw, 0.5), 0.0, 1.0)
    return None


def mastery_for_question(
    question: dict[str, Any],
    profile_or_mastery: dict[str, Any] | None = None,
    *,
    default: float = 0.5,
) -> float:
    """Estimate mastery for a question from KG-node mastery maps."""
    source = profile_or_mastery if isinstance(profile_or_mastery, dict) else {}
    mastery_map = source.get("mastery_by_kg_node") if isinstance(source.get("mastery_by_kg_node"), dict) else source
    kg_ids = _as_list(question.get("kg_node_ids"))
    values: list[float] = []
    for kg_id in kg_ids:
        value = _mastery_value(mastery_map.get(kg_id)) if isinstance(mastery_map, dict) else None
        if value is not None:
            values.append(value)
    if values:
        return round(sum(values) / len(values), 3)
    return round(_clamp(default, 0.0, 1.0), 3)


def bloom_level_for_mastery(mastery: float, *, salt: str = "") -> str:
    """Map mastery into a Bloom cognitive level band."""
    value = _clamp(mastery, 0.0, 1.0)
    if value < 0.4:
        choices = ("remember", "understand")
    elif value < 0.7:
        choices = ("apply", "analyze")
    else:
        choices = ("evaluate", "create")
    return choices[_stable_bucket(salt or f"{value:.3f}", len(choices))]


def target_difficulty_for_mastery(mastery: float) -> float:
    """Return a target item difficulty in [0, 1], higher means harder."""
    value = _clamp(mastery, 0.0, 1.0)
    if value < 0.4:
        difficulty = 0.28 + (value / 0.4) * 0.18
    elif value < 0.7:
        difficulty = 0.50 + ((value - 0.4) / 0.3) * 0.18
    else:
        difficulty = 0.72 + ((value - 0.7) / 0.3) * 0.16
    return round(_clamp(difficulty, 0.15, 0.95), 3)


def learner_theta_from_mastery(mastery: float) -> float:
    """Map a mastery probability to a compact IRT ability parameter."""
    return round(_clamp(_logit(_clamp(mastery, 0.02, 0.98)), -3.0, 3.0), 3)


def question_item_key(question: dict[str, Any], *, surface: str) -> str:
    """Build a stable item key, preferring reusable question-bank identity."""
    question_bank = question.get("question_bank") if isinstance(question.get("question_bank"), dict) else {}
    source_id = str(question.get("source_question_id") or question_bank.get("question_id") or "").strip()
    if not source_id:
        source_id = str(question.get("source_question_id") or question.get("id") or "").strip()
    digest = hashlib.sha1(source_id.encode("utf-8", errors="ignore")).hexdigest()[:16]
    return f"{surface}:{digest}"


def annotate_question(
    question: dict[str, Any],
    profile_or_mastery: dict[str, Any] | None = None,
    *,
    surface: str,
    sequence_index: int = 1,
) -> dict[str, Any]:
    """Attach Bloom/IRT target metadata to one question."""
    item = dict(question)
    mastery = mastery_for_question(item, profile_or_mastery)
    salt = f"{surface}:{item.get('source_question_id') or item.get('id') or sequence_index}"
    bloom_level = bloom_level_for_mastery(mastery, salt=salt)
    target_difficulty = target_difficulty_for_mastery(mastery)
    theta = learner_theta_from_mastery(mastery)
    item_key = question_item_key(item, surface=surface)
    metadata = {
        "schema_version": "adaptive_assessment.bloom_irt.v1",
        "surface": surface,
        "item_key": item_key,
        "bloom_level": bloom_level,
        "bloom_label": BLOOM_LABELS[bloom_level],
        "target_difficulty": target_difficulty,
        "mastery_snapshot": mastery,
        "learner_theta_snapshot": theta,
        "kg_node_ids": _as_list(item.get("kg_node_ids")),
        "policy": "mastery_to_bloom_to_irt_target",
    }
    item["bloom_level"] = bloom_level
    item["bloom_label"] = BLOOM_LABELS[bloom_level]
    item["target_difficulty"] = target_difficulty
    item["adaptive_assessment"] = metadata
    return item


def annotate_questions(
    questions: list[dict[str, Any]],
    profile_or_mastery: dict[str, Any] | None = None,
    *,
    surface: str,
) -> list[dict[str, Any]]:
    return [
        annotate_question(question, profile_or_mastery, surface=surface, sequence_index=index)
        for index, question in enumerate(questions, 1)
        if isinstance(question, dict)
    ]


def theta_from_results(results: list[dict[str, Any]]) -> float:
    ratios: list[float] = []
    for item in results:
        max_score = _as_float(item.get("max_score"), 1.0)
        if max_score <= 0:
            continue
        ratios.append(_clamp(_as_float(item.get("score"), 0.0) / max_score, 0.0, 1.0))
    if not ratios:
        return 0.0
    return round(_clamp(_logit(sum(ratios) / len(ratios)), -3.0, 3.0), 3)


def build_adaptive_submission_report(
    *,
    results: list[dict[str, Any]],
    question_by_id: dict[str, dict[str, Any]],
    item_stats: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a compact report for result pages and downstream planning."""
    theta = theta_from_results(results)
    bloom_buckets: dict[str, dict[str, Any]] = {}
    for result in results:
        question = question_by_id.get(str(result.get("question_id") or ""), {})
        bloom = str(question.get("bloom_level") or question.get("adaptive_assessment", {}).get("bloom_level") or "unknown")
        max_score = _as_float(result.get("max_score"), 1.0)
        ratio = 0.0 if max_score <= 0 else _clamp(_as_float(result.get("score"), 0.0) / max_score, 0.0, 1.0)
        bucket = bloom_buckets.setdefault(
            bloom,
            {
                "bloom_level": bloom,
                "bloom_label": BLOOM_LABELS.get(bloom, bloom),
                "question_count": 0,
                "score_sum": 0.0,
            },
        )
        bucket["question_count"] += 1
        bucket["score_sum"] += ratio

    by_bloom = []
    for bloom in BLOOM_LEVELS:
        if bloom not in bloom_buckets:
            continue
        bucket = bloom_buckets[bloom]
        count = max(1, int(bucket["question_count"]))
        by_bloom.append({
            "bloom_level": bloom,
            "bloom_label": bucket["bloom_label"],
            "question_count": bucket["question_count"],
            "score_rate": round(bucket["score_sum"] / count, 3),
        })

    calibrated_items = []
    for stat in item_stats or []:
        calibrated_items.append({
            "item_key": stat.get("item_key"),
            "bloom_level": stat.get("bloom_level"),
            "bloom_label": BLOOM_LABELS.get(str(stat.get("bloom_level") or ""), stat.get("bloom_level")),
            "difficulty_b": stat.get("difficulty_b"),
            "discrimination_a": stat.get("discrimination_a"),
            "correct_rate": stat.get("correct_rate"),
            "sample_count": stat.get("exposure_count"),
            "quality_status": stat.get("quality_status"),
        })

    return {
        "schema_version": "adaptive_assessment.submission_report.v1",
        "model": "bloom_irt_1pl_2pl_proxy",
        "learner_theta": theta,
        "bloom_performance": by_bloom,
        "calibrated_items": calibrated_items,
    }


def calibrate_submission_items(
    memory: Any,
    *,
    user_id: str,
    surface: str,
    questions: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Persist IRT item statistics when the memory layer supports it."""
    if not hasattr(memory, "update_assessment_item_stat"):
        return []
    question_by_id = {str(item.get("id") or ""): item for item in questions if isinstance(item, dict)}
    theta = theta_from_results(results)
    saved: list[dict[str, Any]] = []
    for result in results:
        question = question_by_id.get(str(result.get("question_id") or ""))
        if not question:
            continue
        max_score = _as_float(result.get("max_score"), 1.0)
        score_ratio = 0.0 if max_score <= 0 else _clamp(_as_float(result.get("score"), 0.0) / max_score, 0.0, 1.0)
        is_correct = bool(result.get("is_correct")) or score_ratio >= 0.6
        metadata = question.get("adaptive_assessment") if isinstance(question.get("adaptive_assessment"), dict) else {}
        stat = memory.update_assessment_item_stat(
            item_key=str(metadata.get("item_key") or question_item_key(question, surface=surface)),
            surface=surface,
            question=question,
            user_id=user_id,
            is_correct=is_correct,
            score_ratio=score_ratio,
            learner_theta=theta,
        )
        saved.append(stat)
    return saved
