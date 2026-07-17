"""Stable learning skills and deterministic profile-write tools.

This module deliberately does not duplicate the learning agents. A skill
only captures a stable product workflow and delegates its work to the
existing orchestrator and leaf agents.
"""

from __future__ import annotations

from typing import Any

from .profile_memory import (
    refresh_compact_profile_from_l3,
    update_learner_memory_from_event,
)


LEARN_KNOWLEDGE_POINT_SKILL = "learn_knowledge_point"

# Names exposed to the master router/API. This is a whitelist, not an
# open-ended method dispatcher.
LEARNING_AGENT_ALIASES: dict[str, str] = {
    "learn_knowledge_point": LEARN_KNOWLEDGE_POINT_SKILL,
    "learning_skill": LEARN_KNOWLEDGE_POINT_SKILL,
    "learning_orchestrator": LEARN_KNOWLEDGE_POINT_SKILL,
    "planner": "planner",
    "handout": "handout",
    "slides": "slides",
    "ppt": "ppt",
    "tutor": "tutor",
    "lecture_script": "lecture_script",
    "voice": "voice",
    "qa": "qa",
    "quiz": "quiz",
    "assessment": "assessment",
    "continue": "continue",
}


def learning_skill_catalog() -> list[dict[str, Any]]:
    """Return a serializable capability catalog for diagnostics and clients."""
    return [
        {
            "name": LEARN_KNOWLEDGE_POINT_SKILL,
            "kind": "workflow",
            "description": "Create a learning session and schedule the knowledge-point learning chain.",
            "steps": [
                "planner",
                "handout",
                "slides",
                "ppt",
                "tutor",
                "lecture_script",
                "voice",
                "knowledge_profile_write",
            ],
        },
        *[
            {"name": name, "kind": "agent", "description": "Individually dispatchable learning capability."}
            for name in ("planner", "handout", "slides", "ppt", "tutor", "lecture_script", "voice", "qa", "quiz", "assessment", "continue")
        ],
    ]


def write_knowledge_profile_event(
    orchestrator: Any,
    *,
    user_id: str,
    session_id: str,
    event_type: str,
    surface: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist a workflow event and project it into the existing learner profile."""
    payload = {"surface": surface, "source": "learning_skill", **(context or {})}
    try:
        event_id = orchestrator.memory.record_event(
            session_id=session_id,
            user_id=user_id,
            event_type=event_type,
            outline_node_id=payload.get("outline_node_id"),
            payload=payload,
        )
        memory_update = update_learner_memory_from_event(
            orchestrator.memory,
            user_id=user_id,
            session_id=session_id,
            event_id=event_id,
            surface=surface,
            context=context or {},
        )
        profile = refresh_compact_profile_from_l3(orchestrator.memory, user_id)
        return {
            "written": True,
            "event_id": event_id,
            "memory_update": memory_update,
            "profile": profile,
        }
    except Exception as exc:  # profile writes must not invalidate generated learning content
        return {"written": False, "error": f"{type(exc).__name__}: {exc}"}


def run_learn_knowledge_point(
    orchestrator: Any,
    *,
    user_id: str,
    topic: str,
    profile_input: dict[str, Any] | None = None,
    learning_scope: dict[str, Any] | None = None,
    deliver_first_section: bool = False,
    session_mode: str = "course_learning",
) -> dict[str, Any]:
    """Run the stable knowledge-point learning workflow.

    ``start_lesson`` owns planning, first handout generation, slide/PPT
    scheduling and background preparation. ``deliver_first_section`` is opt-in
    because existing clients explicitly request the first Tutor step after a
    session is created; it also schedules the lecture script and voice.
    """
    start_kwargs: dict[str, Any] = {"user_id": user_id, "topic": topic}
    if profile_input:
        start_kwargs["profile_input"] = profile_input
    if learning_scope:
        start_kwargs["learning_scope"] = learning_scope
    if session_mode != "course_learning":
        start_kwargs["session_mode"] = session_mode
        start_kwargs["profile_memory_enabled"] = False

    result = orchestrator.start_lesson(**start_kwargs)
    session = result.get("session") if isinstance(result, dict) else None
    session_id = str((session or {}).get("id") or "")
    if not session_id:
        raise RuntimeError("learn_knowledge_point did not receive a session id")

    trace: list[dict[str, Any]] = [
        {"agent": "planner", "status": "completed"},
        {"agent": "handout", "status": "completed" if result.get("first_handout_section_ready") else "scheduled"},
        {"agent": "slides", "status": (result.get("slides_resource") or {}).get("status", "scheduled")},
        {"agent": "ppt", "status": "scheduled"},
        {"agent": "tutor", "status": "pending"},
        {"agent": "lecture_script", "status": "pending_tutor_step"},
        {"agent": "voice", "status": "pending_tutor_step"},
    ]

    if deliver_first_section:
        tutor_step = orchestrator.next_step(session_id)
        result = {**result, "tutor_step": tutor_step}
        trace[4]["status"] = "completed"
        trace[5]["status"] = "completed"
        trace[6]["status"] = str((tutor_step.get("section_audio_schedule") or {}).get("status") or "scheduled")

    profile_write = (
        _record_standalone_topic_interest(
            orchestrator,
            user_id=user_id,
            session_id=session_id,
            topic=topic,
        )
        if session_mode == "standalone_generation"
        else write_knowledge_profile_event(
            orchestrator,
            user_id=user_id,
            session_id=session_id,
            event_type="learning_skill_started",
            surface="planning",
            context={
                "topic": topic,
                "outline_node_id": result.get("current_outline_node_id"),
                "workflow": LEARN_KNOWLEDGE_POINT_SKILL,
                "deliver_first_section": deliver_first_section,
            },
        )
    )
    return {
        **result,
        "skill": LEARN_KNOWLEDGE_POINT_SKILL,
        "skill_trace": trace,
        "knowledge_profile_write": profile_write,
    }


def _record_standalone_topic_interest(
    orchestrator: Any,
    *,
    user_id: str,
    session_id: str,
    topic: str,
) -> dict[str, Any]:
    """Persist L1 generation activity and a bounded, low-confidence interest."""
    try:
        event_id = orchestrator.memory.record_event(
            session_id=session_id,
            user_id=user_id,
            event_type="standalone_resource_requested",
            outline_node_id=None,
            payload={
                "surface": "standalone_generation",
                "topic": topic,
                "profile_impact": "topic_interest_only",
                "mastery_update_allowed": False,
                "plan_update_allowed": False,
            },
        )
        profile = orchestrator.memory.get_or_create_profile(user_id)
        interests = [item for item in profile.get("topic_interests", []) if isinstance(item, dict)]
        matched = next((item for item in interests if str(item.get("topic") or "") == topic), None)
        count = int(matched.get("request_count") or 0) + 1 if matched else 1
        updated_interest = {
            "topic": topic,
            "request_count": count,
            "confidence": min(0.45, 0.12 + count * 0.06),
            "source": "standalone_generation",
        }
        profile["topic_interests"] = [
            item for item in interests if str(item.get("topic") or "") != topic
        ] + [updated_interest]
        saved = orchestrator.memory.save_profile(user_id, profile)
        return {
            "written": True,
            "event_id": event_id,
            "profile": saved,
            "impact": "topic_interest_only",
            "mastery_updated": False,
            "plan_updated": False,
        }
    except Exception as exc:
        return {"written": False, "error": f"{type(exc).__name__}: {exc}"}
