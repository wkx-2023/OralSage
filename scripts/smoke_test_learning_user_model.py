"""Smoke test for the user-model / profile module.

Run from the project root::

    python scripts/smoke_test_learning_user_model.py

Expected output ends with ``learning user model smoke test passed``.
"""

import sys
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from backend.services.learning.user_model import (
    default_profile,
    diagnose_profile,
    merge_profile,
    profile_brief,
    update_profile_from_event,
)


def main() -> None:
    # ---- 1. diagnose with answers ----
    profile = diagnose_profile({
        "level": "本科高年级",
        "goal": "期末考试",
        "preferred_explanation": "病例带入",
        "pace": "标准",
    })

    print("初始画像:")
    print(profile_brief(profile))
    print()

    assert profile["level"] == "本科高年级", f"level mismatch: {profile['level']}"
    assert profile["goal"] == "期末考试", f"goal mismatch: {profile['goal']}"
    assert (
        profile["preferred_explanation"] == "病例带入"
    ), f"preferred_explanation mismatch: {profile['preferred_explanation']}"
    assert profile["confidence"] >= 0.65, f"confidence too low: {profile['confidence']}"

    # ---- 2. interruption question ----
    profile = update_profile_from_event(
        profile,
        event_type="user_interruption_question",
        payload={
            "confusion_tags": ["机制链条不完整"],
            "related_concepts": ["牙槽骨吸收"],
        },
    )

    print("中途提问后:")
    print(profile_brief(profile))
    print()

    assert "机制链条不完整" in profile["known_weak_points"], (
        f"missing confusion_tag: {profile['known_weak_points']}"
    )
    assert "牙槽骨吸收" in profile["known_weak_points"], (
        f"missing related_concept: {profile['known_weak_points']}"
    )
    assert profile["confidence"] >= 0.35, f"confidence too low after qa: {profile['confidence']}"

    # ---- 3. assessment completed (wrong answer) ----
    profile = update_profile_from_event(
        profile,
        event_type="assessment_completed",
        payload={
            "is_correct": False,
            "mistake_type": "概念混淆",
            "affected_concepts": ["牙龈炎与牙周炎区别"],
        },
    )

    print("答错题后:")
    print(profile_brief(profile))
    print()

    assert "概念混淆" in profile["mistake_patterns"], (
        f"missing mistake_type: {profile['mistake_patterns']}"
    )
    assert "牙龈炎与牙周炎区别" in profile["known_weak_points"], (
        f"missing affected_concept: {profile['known_weak_points']}"
    )
    assert profile["confidence"] >= 0.4, (
        f"confidence too low after assessment: {profile['confidence']}"
    )

    # ---- 4. pace_feedback ----
    profile = update_profile_from_event(
        profile,
        event_type="pace_feedback",
        payload={"pace": "慢一点"},
    )
    assert profile["pace"] == "慢一点", f"pace not updated: {profile['pace']}"

    # ---- 5. explanation_preference ----
    profile = update_profile_from_event(
        profile,
        event_type="explanation_preference",
        payload={"preferred_explanation": "先病例后概念"},
    )
    assert (
        profile["preferred_explanation"] == "先病例后概念"
    ), f"explanation not updated: {profile['preferred_explanation']}"

    # ---- 6. unknown event_type ----
    profile2 = update_profile_from_event(
        profile,
        event_type="some_future_event_type",
        payload={"x": 1},
    )
    assert profile2 == profile, "unknown event_type should return normalised profile"

    # ---- 7. default_profile deep-copy ----
    d1 = default_profile()
    d2 = default_profile()
    d1["known_weak_points"].append("test")
    assert d2["known_weak_points"] == [], (
        "default_profile() must return deep copies — lists should not be shared"
    )

    # ---- 8. merge_profile dedup ----
    merged = merge_profile(
        {"known_weak_points": ["A", "B"], "confidence": 0.3},
        {"known_weak_points": ["B", "C"], "confidence": 0.5},
    )
    assert merged["known_weak_points"] == ["A", "B", "C"], (
        f"dedup failed: {merged['known_weak_points']}"
    )
    assert merged["confidence"] == 0.5, f"confidence should be max: {merged['confidence']}"

    # ---- final ----
    print("最终画像:")
    print(profile_brief(profile))
    print()

    print("learning user model smoke test passed")


if __name__ == "__main__":
    main()
