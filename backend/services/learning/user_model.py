"""User model / profile module for the learning agent.

Provides quick-diagnosis profile creation, default profiles, incremental
profile updates from learning events, and a human-readable profile summary.

All functions are pure — they receive and return dicts.  No database or LLM
calls happen here.
"""

from __future__ import annotations

import copy
from typing import Any

from .schemas import DEFAULT_PROFILE, LearningProfile

# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def default_profile() -> dict[str, Any]:
    """Return a deep copy of :data:`DEFAULT_PROFILE`.

    The deep copy prevents accidental shared-mutation of nested mutable
    values (lists) across callers.
    """
    return copy.deepcopy(DEFAULT_PROFILE)


def diagnose_profile(answers: dict[str, Any] | None = None) -> dict[str, Any]:
    """Normalise quick-diagnosis answers into a validated profile dict.

    Parameters
    ----------
    answers:
        Optional dict that may contain any subset of profile fields.

    Returns
    -------
    dict
        A profile dict that passes ``LearningProfile(**profile)`` validation.
    """
    base = default_profile()

    if not answers:
        return base

    # --- string fields ------------------------------------------------
    for key in ("level", "goal", "preferred_explanation", "pace"):
        val = answers.get(key)
        if isinstance(val, str) and val.strip():
            base[key] = val.strip()

    # --- optional string field ----------------------------------------
    target_exam = answers.get("target_exam")
    if isinstance(target_exam, str) and target_exam.strip():
        base["target_exam"] = target_exam.strip()
    else:
        base["target_exam"] = None

    # --- list fields --------------------------------------------------
    for key in ("known_weak_points", "mistake_patterns"):
        val = answers.get(key)
        if isinstance(val, list):
            base[key] = [str(v) for v in val if v]
        else:
            base[key] = []

    # --- confidence ---------------------------------------------------
    base["confidence"] = 0.65

    # Validate round-trip
    LearningProfile(**base)
    return base


def merge_profile(existing: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Merge *patch* into *existing* and return a validated profile dict.

    Rules
    -----
    * Start from :func:`default_profile`.
    * *existing* overrides defaults.
    * *patch* overrides *existing*.
    * ``known_weak_points`` and ``mistake_patterns`` are accumulated across
      all three layers and then deduplicated (preserving insertion order).
    * ``confidence`` is ``max(existing, patch)``, clamped to ``[0, 0.95]``.
    """
    result = default_profile()
    existing = existing or {}
    patch = patch or {}

    # --- accumulate list fields across all layers --------------------
    for key in ("known_weak_points", "mistake_patterns"):
        combined: list[str] = []
        for source in (result, existing, patch):
            vals = source.get(key, [])
            if isinstance(vals, list):
                combined.extend(str(v).strip() for v in vals if str(v).strip())
        result[key] = combined

    # --- scalar fields: patch > existing > default -------------------
    for key in result:
        if key in ("known_weak_points", "mistake_patterns"):
            continue  # already accumulated above
        if key in existing:
            result[key] = existing[key]
        if key in patch:
            result[key] = patch[key]

    # --- deduplicate lists (preserve order) ---------------------------
    for key in ("known_weak_points", "mistake_patterns"):
        vals = result.get(key)
        if isinstance(vals, list):
            seen: set[str] = set()
            deduped: list[str] = []
            for v in vals:
                s = str(v).strip()
                if s and s not in seen:
                    seen.add(s)
                    deduped.append(s)
            result[key] = deduped

    # --- confidence ---------------------------------------------------
    existing_conf = float(existing.get("confidence", 0.2))
    patch_conf = float(patch.get("confidence", 0.2))
    result["confidence"] = min(max(existing_conf, patch_conf), 0.95)

    # Validate
    LearningProfile(**result)
    return result


def update_profile_from_event(
    profile: dict[str, Any],
    event_type: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Update *profile* in-place based on a learning event, return it.

    Parameters
    ----------
    profile:
        The current profile dict (will be normalised but not mutated — a copy
        is returned).
    event_type:
        One of ``user_interruption_question``, ``assessment_completed``,
        ``pace_feedback``, ``explanation_preference``, or any other value
        (which is passed through unchanged).
    payload:
        Event-specific payload dict.
    """
    # Always start from a validated base
    base = _normalise_profile(profile)
    patch: dict[str, Any] = {}

    if event_type == "user_interruption_question":
        confusion_tags: list[str] = _as_str_list(payload.get("confusion_tags"))
        related_concepts: list[str] = _as_str_list(payload.get("related_concepts"))
        merged_weak = list(base.get("known_weak_points", [])) + confusion_tags + related_concepts
        patch["known_weak_points"] = merged_weak
        patch["confidence"] = max(base.get("confidence", 0.2), 0.35)

    elif event_type == "assessment_completed":
        is_correct = bool(payload.get("is_correct", True))
        mistake_type = payload.get("mistake_type")
        affected = _as_str_list(
            payload.get("affected_concepts")
            or payload.get("affected_kg_node_ids")
            or []
        )

        patch["known_weak_points"] = list(base.get("known_weak_points", []))
        if not is_correct:
            if affected:
                patch["known_weak_points"].extend(affected)
            if isinstance(mistake_type, str) and mistake_type.strip():
                patch["mistake_patterns"] = list(
                    base.get("mistake_patterns", [])
                ) + [mistake_type.strip()]
        patch["confidence"] = max(base.get("confidence", 0.2), 0.4)

    elif event_type == "pace_feedback":
        pace = payload.get("pace")
        if isinstance(pace, str) and pace.strip():
            patch["pace"] = pace.strip()

    elif event_type == "explanation_preference":
        pref = payload.get("preferred_explanation")
        if isinstance(pref, str) and pref.strip():
            patch["preferred_explanation"] = pref.strip()

    # For any other event_type, patch remains empty → normalised profile returned.

    return merge_profile(base, patch)


def profile_brief(profile: dict[str, Any]) -> str:
    """Return a one-line Chinese summary of *profile* for terminal display.

    Example::

        基础：本科高年级；目标：期末考试；偏好：病例带入；节奏：标准；薄弱点：暂无
    """
    level = profile.get("level", "unknown")
    goal = profile.get("goal", "unknown")
    pref = profile.get("preferred_explanation", "unknown")
    pace = profile.get("pace", "unknown")
    weak = profile.get("known_weak_points", [])

    weak_str = "、".join(weak) if weak else "暂无"
    return (
        f"基础：{level}；目标：{goal}；偏好：{pref}；节奏：{pace}；薄弱点：{weak_str}"
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def update_profile_from_assessment(
    profile: dict[str, Any],
    assessment: dict[str, Any],
    outline_node_id: str,
    question: dict[str, Any],
) -> dict[str, Any]:
    """Update user profile from an Assessment result (wrong answer).

    Accumulates weakness signals:
    - ``related_kg_node_ids`` → ``known_weak_points``
    - ``misconception_summary`` → ``mistake_patterns``
    - Increases ``confidence`` (we now know more about the user's gaps)

    This is called **only** for wrong answers.  Correct answers require
    no profile update (mastery is updated separately via Memory).

    Parameters
    ----------
    profile:
        The current user profile dict.
    assessment:
        The full assessment dict from :func:`assessment.assess_answer`.
    outline_node_id:
        The current outline node id.
    question:
        The question dict (for fallback kg_node_ids).
    """
    base = _normalise_profile(profile)
    patch: dict[str, Any] = {
        "known_weak_points": list(base.get("known_weak_points", [])),
        "mistake_patterns": list(base.get("mistake_patterns", [])),
    }

    # --- weakness: related KG node ids ---
    related_kg = _as_str_list(assessment.get("related_kg_node_ids", []))
    if not related_kg:
        related_kg = _as_str_list(question.get("kg_node_ids", []))
    if related_kg:
        patch["known_weak_points"].extend(related_kg)

    # --- mistake pattern: misconception summary ---
    misconception = str(assessment.get("misconception_summary") or "").strip()
    if misconception and misconception.lower() != "none":
        patch["mistake_patterns"].append(misconception)

    # --- section context ---
    if outline_node_id:
        patch["known_weak_points"].append(f"section:{outline_node_id}")

    # --- confidence: we learned about user's gaps ---
    patch["confidence"] = max(base.get("confidence", 0.2), 0.45)

    return merge_profile(base, patch)


def update_profile_from_qa(
    profile: dict[str, Any],
    qa_result: dict[str, Any],
    outline_node_id: str,
) -> dict[str, Any]:
    """Update user profile from a QA interaction.

    **Does not** treat all questions as "user doesn't understand".
    Distinguishes between:

    - **Exposed misconception** (``should_reinforce=True`` or non-empty
      ``confusion_kg_node_ids``): the question revealed a gap →
      add to ``known_weak_points``, update ``mistake_patterns``.
    - **Normal expansion question** (``should_reinforce=False`` and
      empty ``confusion_kg_node_ids``): the user is just curious or
      exploring → only record the question topic, no mastery penalty.

    Parameters
    ----------
    profile:
        The current user profile dict.
    qa_result:
        The QA result dict from :func:`qa.answer_question_with_evidence`.
    outline_node_id:
        The current outline node id.
    """
    base = _normalise_profile(profile)
    patch: dict[str, Any] = {
        "known_weak_points": list(base.get("known_weak_points", [])),
        "mistake_patterns": list(base.get("mistake_patterns", [])),
    }

    should_reinforce = bool(qa_result.get("should_reinforce", False))
    confusion_kg_ids = _as_str_list(qa_result.get("confusion_kg_node_ids", []))
    question_text = str(qa_result.get("question", "")).strip()

    # --- Case 1: Exposed misconception ---
    if should_reinforce or confusion_kg_ids:
        if confusion_kg_ids:
            patch["known_weak_points"].extend(confusion_kg_ids)
        if question_text:
            # Record the question topic as a potential weak area (shortened)
            short_topic = question_text[:60]
            patch["mistake_patterns"].append(f"QA疑问:{short_topic}")
        if outline_node_id:
            patch["known_weak_points"].append(f"section:{outline_node_id}")
        patch["confidence"] = max(base.get("confidence", 0.2), 0.4)

    # --- Case 2: Normal expansion question ---
    # Only record the question for awareness, do NOT add to weak_points
    # and do NOT penalize mastery.  The confidence bump is smaller.
    else:
        patch["confidence"] = max(base.get("confidence", 0.2), 0.3)

    return merge_profile(base, patch)


def _normalise_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Ensure *profile* is valid and all keys present."""
    return merge_profile({}, profile)


def _as_str_list(value: Any) -> list[str]:
    """Coerce *value* to a list of non-empty strings."""
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]
