"""Knowledge tracing models for KG-node cognitive state estimation.

The current production path implements an explainable Bayesian Knowledge
Tracing (BKT) updater.  Each KG node is treated as a skill; answer correctness
is the observation; the output is a posterior mastery probability plus
uncertainty that can be consumed by the existing mastery, review, and student
twin modules.

The module deliberately exposes a DKT-compatible state schema, but does not
train a neural model in the request path.  A future LSTM/Transformer estimator
can write the same state fields without changing downstream callers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import Any


@dataclass(frozen=True)
class BKTParameters:
    """Parameters for one Bayesian Knowledge Tracing skill."""

    p_init: float = 0.30
    p_learn: float = 0.16
    p_guess: float = 0.20
    p_slip: float = 0.10
    p_forget: float = 0.00


DEFAULT_BKT_PARAMETERS = BKTParameters()


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


def _normalize_parameters(value: BKTParameters | dict[str, Any] | None) -> BKTParameters:
    if isinstance(value, BKTParameters):
        return value
    raw = value if isinstance(value, dict) else {}
    return BKTParameters(
        p_init=_clamp(_as_float(raw.get("p_init"), DEFAULT_BKT_PARAMETERS.p_init), 0.01, 0.99),
        p_learn=_clamp(_as_float(raw.get("p_learn"), DEFAULT_BKT_PARAMETERS.p_learn), 0.00, 0.80),
        p_guess=_clamp(_as_float(raw.get("p_guess"), DEFAULT_BKT_PARAMETERS.p_guess), 0.00, 0.49),
        p_slip=_clamp(_as_float(raw.get("p_slip"), DEFAULT_BKT_PARAMETERS.p_slip), 0.00, 0.49),
        p_forget=_clamp(_as_float(raw.get("p_forget"), DEFAULT_BKT_PARAMETERS.p_forget), 0.00, 0.30),
    )


def _normalized_entropy(probability: float) -> float:
    p = _clamp(probability, 1e-9, 1.0 - 1e-9)
    entropy = -(p * math.log(p) + (1.0 - p) * math.log(1.0 - p))
    return _clamp(entropy / math.log(2.0), 0.0, 1.0)


def _confidence_from(probability: float, evidence_count: int) -> float:
    evidence_confidence = 1.0 - math.exp(-max(0, evidence_count) / 4.0)
    certainty = 1.0 - _normalized_entropy(probability)
    return round(_clamp(evidence_confidence * 0.55 + certainty * 0.45, 0.0, 0.99), 3)


def bkt_observe(
    prior_mastery: float,
    *,
    is_correct: bool,
    parameters: BKTParameters | dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply one BKT observation and return posterior/transition details."""
    params = _normalize_parameters(parameters)
    prior = _clamp(float(prior_mastery), 0.0, 1.0)

    if is_correct:
        numerator = prior * (1.0 - params.p_slip)
        denominator = numerator + (1.0 - prior) * params.p_guess
    else:
        numerator = prior * params.p_slip
        denominator = numerator + (1.0 - prior) * (1.0 - params.p_guess)

    posterior = prior if denominator <= 0 else numerator / denominator
    after_learning = (
        posterior * (1.0 - params.p_forget)
        + (1.0 - posterior) * params.p_learn
    )
    uncertainty = _normalized_entropy(after_learning)

    return {
        "prior_mastery": round(prior, 3),
        "posterior_before_learning": round(_clamp(posterior, 0.0, 1.0), 3),
        "p_mastery": round(_clamp(after_learning, 0.0, 1.0), 3),
        "uncertainty": round(uncertainty, 3),
        "parameters": asdict(params),
        "observation": "correct" if is_correct else "incorrect",
        "model": "bkt_kg_node_v1",
    }


def update_bkt_state(
    previous_state: dict[str, Any] | None,
    *,
    kg_node_id: str,
    skill_label: str | None = None,
    is_correct: bool,
    score: float | None = None,
    source: str = "assessment",
    source_ref: str | None = None,
    parameters: BKTParameters | dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Update one KG-node BKT state from an answer observation."""
    previous = previous_state if isinstance(previous_state, dict) else {}
    merged_parameters = parameters
    if merged_parameters is None and isinstance(previous.get("parameters"), dict):
        merged_parameters = previous["parameters"]
    params = _normalize_parameters(merged_parameters)
    prior = _as_float(previous.get("p_mastery"), params.p_init)
    evidence_count = _as_int(previous.get("evidence_count"), 0) + 1
    correct_count = _as_int(previous.get("correct_count"), 0) + (1 if is_correct else 0)
    incorrect_count = _as_int(previous.get("incorrect_count"), 0) + (0 if is_correct else 1)

    observation = bkt_observe(
        prior,
        is_correct=is_correct,
        parameters=params,
    )
    p_mastery = float(observation["p_mastery"])
    uncertainty = float(observation["uncertainty"])
    confidence = _confidence_from(p_mastery, evidence_count)
    score_value = None if score is None else round(_clamp(_as_float(score, 0.0), 0.0, 1.0), 3)

    return {
        "kg_node_id": str(kg_node_id or "").strip(),
        "skill_label": str(skill_label or kg_node_id or "KG skill").strip(),
        "p_mastery": round(p_mastery, 3),
        "uncertainty": round(uncertainty, 3),
        "confidence": confidence,
        "evidence_count": evidence_count,
        "correct_count": correct_count,
        "incorrect_count": incorrect_count,
        "last_observation_correct": bool(is_correct),
        "parameters": observation["parameters"],
        "trace": {
            "model": observation["model"],
            "source": source,
            "source_ref": source_ref,
            "score": score_value,
            "observation": observation["observation"],
            "prior_mastery": observation["prior_mastery"],
            "posterior_before_learning": observation["posterior_before_learning"],
            "p_mastery": observation["p_mastery"],
            "uncertainty": observation["uncertainty"],
            "confidence_formula": "0.55*(1-exp(-evidence_count/4))+0.45*(1-normalized_entropy)",
            "dkt_extension": {
                "status": "reserved_schema",
                "sequence_input": "answer_records ordered by created_at",
                "candidate_models": ["LSTM-DKT", "Transformer-KT"],
            },
        },
    }


def profile_mastery_patch_from_states(
    states: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Build a ``mastery_by_kg_node`` patch from tracing states."""
    patch: dict[str, dict[str, Any]] = {}
    for state in states:
        kg_id = str(state.get("kg_node_id") or "").strip()
        if not kg_id:
            continue
        patch[kg_id] = {
            "score": round(_clamp(_as_float(state.get("p_mastery"), 0.0), 0.0, 1.0), 3),
            "mastery": round(_clamp(_as_float(state.get("p_mastery"), 0.0), 0.0, 1.0), 3),
            "confidence": round(_clamp(_as_float(state.get("confidence"), 0.0), 0.0, 1.0), 3),
            "uncertainty": round(_clamp(_as_float(state.get("uncertainty"), 1.0), 0.0, 1.0), 3),
            "label": str(state.get("skill_label") or kg_id),
            "evidence_count": _as_int(state.get("evidence_count"), 0),
            "model": state.get("trace", {}).get("model", "bkt_kg_node_v1"),
            "updated_at": state.get("updated_at"),
        }
    return patch
