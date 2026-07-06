"""Smoke test for deterministic learner profile-state aggregation.

Run from the project root:

    python scripts/smoke_test_learning_profile_state.py

Expected output ends with ``learning profile_state smoke test passed``.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from backend.services.learning.memory import LearningMemory
from backend.services.learning.profile_memory import (
    build_profile_slice,
    reduce_memory_signals,
)

TEST_DIR = Path("data/learning_profile_state_smoke")
DB_PATH = TEST_DIR / "learning.sqlite3"
USER_ID = "profile_state_demo"


def _signal(
    *,
    slot: str,
    aspect: str,
    content: str,
    event_id: str,
    action: str = "add",
    label: str = "",
    confidence: float = 0.8,
    kg_node_ids: list[str] | None = None,
    severity_delta: float = 0.0,
    score_delta: float = 0.0,
) -> dict:
    return {
        "slot": slot,
        "aspect": aspect,
        "label": label,
        "action": action,
        "content": content,
        "kg_node_ids": kg_node_ids or [],
        "confidence": confidence,
        "evidence_event_ids": [event_id],
        "severity_delta": severity_delta,
        "score_delta": score_delta,
    }


def _apply_signal(
    mem: LearningMemory,
    *,
    session_id: str,
    event_id: str,
    surface: str,
    signal: dict,
) -> dict:
    return reduce_memory_signals(
        mem,
        user_id=USER_ID,
        session_id=session_id,
        event_id=event_id,
        surface=surface,
        extraction={"should_update": True, "signals": [signal]},
    )


def main() -> None:
    if TEST_DIR.exists():
        shutil.rmtree(TEST_DIR)

    mem = LearningMemory(db_path=DB_PATH)
    session = mem.create_session(USER_ID, "periodontitis")
    sid = session["id"]
    mem.set_current_outline_node(sid, "node_1")

    # One mechanism-oriented question is only an observation, not a stable style.
    evt1 = mem.record_event(
        session_id=sid,
        user_id=USER_ID,
        event_type="user_interruption_question",
        outline_node_id="node_1",
        payload={
            "surface": "qa",
            "question": "Why does periodontitis cause alveolar bone resorption?",
            "advance_progress": False,
        },
    )
    _apply_signal(
        mem,
        session_id=sid,
        event_id=evt1,
        surface="qa",
        signal=_signal(
            slot="preferences",
            aspect="explanation_preference",
            label="mechanism depth",
            content="Learner asks why and prefers mechanism/pathway explanations.",
            event_id=evt1,
            confidence=0.82,
        ),
    )
    state = mem.get_learner_memory_slot(USER_ID, "profile_state")
    depth = state["style"]["explanation_depth"]
    assert depth["dominant"] == "mechanism_deep", depth
    assert depth["status"] == "observed", depth
    assert state["personalization_policy"] == "light", state

    # Repeated consistent evidence moves the tendency to emerging.
    for idx in range(2, 4):
        evt = mem.record_event(
            session_id=sid,
            user_id=USER_ID,
            event_type="user_interruption_question",
            outline_node_id="node_1",
            payload={
                "surface": "qa",
                "question": f"Can you explain the causal mechanism again? #{idx}",
                "advance_progress": False,
            },
        )
        _apply_signal(
            mem,
            session_id=sid,
            event_id=evt,
            surface="qa",
            signal=_signal(
                slot="preferences",
                aspect="explanation_preference",
                label="mechanism depth",
                content="Learner repeatedly asks for causal mechanism/pathway explanation.",
                event_id=evt,
                action="reinforce",
                confidence=0.82,
            ),
        )

    state = mem.get_learner_memory_slot(USER_ID, "profile_state")
    depth = state["style"]["explanation_depth"]
    assert depth["dominant"] == "mechanism_deep", depth
    assert depth["status"] == "emerging", depth
    assert depth["options"]["mechanism_deep"]["evidence_count"] >= 3, depth

    # Weaknesses are also stateful: weaken signals lower severity to improving.
    kg_id = "kg://periodontology/periodontitis/core_1"
    wrong_evt = mem.record_event(
        session_id=sid,
        user_id=USER_ID,
        event_type="answer_submitted",
        outline_node_id="node_1",
        payload={
            "surface": "quiz",
            "is_correct": False,
            "kg_node_ids": [kg_id],
            "misconception_summary": "Confuses gingivitis and periodontitis.",
        },
    )
    _apply_signal(
        mem,
        session_id=sid,
        event_id=wrong_evt,
        surface="quiz",
        signal=_signal(
            slot="weaknesses",
            aspect="misconception",
            label="gingivitis vs periodontitis",
            content="Learner confuses gingivitis and periodontitis.",
            event_id=wrong_evt,
            confidence=0.65,
            kg_node_ids=[kg_id],
        ),
    )
    state = mem.get_learner_memory_slot(USER_ID, "profile_state")
    assert state["weakness_summary"][0]["status"] == "active", state["weakness_summary"]

    correct_evt = mem.record_event(
        session_id=sid,
        user_id=USER_ID,
        event_type="answer_submitted",
        outline_node_id="node_1",
        payload={
            "surface": "quiz",
            "is_correct": True,
            "kg_node_ids": [kg_id],
        },
    )
    _apply_signal(
        mem,
        session_id=sid,
        event_id=correct_evt,
        surface="quiz",
        signal=_signal(
            slot="weaknesses",
            aspect="misconception",
            label="gingivitis vs periodontitis",
            content="Learner confuses gingivitis and periodontitis.",
            event_id=correct_evt,
            action="weaken",
            confidence=0.65,
            kg_node_ids=[kg_id],
            severity_delta=-0.3,
        ),
    )
    state = mem.get_learner_memory_slot(USER_ID, "profile_state")
    assert state["weakness_summary"][0]["status"] == "improving", state["weakness_summary"]

    profile_slice = build_profile_slice(
        mem,
        user_id=USER_ID,
        role="tutor",
        context={"session_id": sid, "kg_node_ids": [kg_id]},
    )
    assert profile_slice["profile_state"]["summary"], profile_slice
    assert profile_slice["recent_evidence"], profile_slice

    print("profile_state summary:", state["summary"])
    print("learning profile_state smoke test passed")


if __name__ == "__main__":
    main()
