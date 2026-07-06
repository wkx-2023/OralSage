"""Study-plan service for calendar to-do tracking and memory feedback."""

from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from typing import Any

from .memory import LearningMemory
from .profile_memory import reduce_memory_signals

STUDY_PLAN_SESSION_ID = "study_plan"


def month_bounds(year: int, month: int) -> tuple[str, str, int]:
    """Return ISO start/end dates and day count for a calendar month."""
    if month < 1 or month > 12:
        raise ValueError("month must be between 1 and 12")
    if year < 1900 or year > 2999:
        raise ValueError("year must be between 1900 and 2999")
    days = monthrange(year, month)[1]
    start = date(year, month, 1).isoformat()
    end = date(year, month, days).isoformat()
    return start, end, days


def get_study_plan_month(
    memory: LearningMemory,
    *,
    user_id: str,
    year: int,
    month: int,
) -> dict[str, Any]:
    """Return month-level study-plan data grouped by day."""
    start, end, day_count = month_bounds(year, month)
    items = memory.list_study_plan_items(user_id, start, end)
    days = {
        date(year, month, day).isoformat(): {
            "date": date(year, month, day).isoformat(),
            "items": [],
            "total": 0,
            "completed": 0,
            "pending": 0,
        }
        for day in range(1, day_count + 1)
    }
    for item in items:
        bucket = days.setdefault(
            item["plan_date"],
            {
                "date": item["plan_date"],
                "items": [],
                "total": 0,
                "completed": 0,
                "pending": 0,
            },
        )
        bucket["items"].append(item)
        bucket["total"] += 1
        if item.get("status") == "completed":
            bucket["completed"] += 1
        else:
            bucket["pending"] += 1

    total = len(items)
    completed = sum(1 for item in items if item.get("status") == "completed")
    return {
        "user_id": user_id,
        "year": year,
        "month": month,
        "start_date": start,
        "end_date": end,
        "days": days,
        "items": items,
        "stats": {
            "total": total,
            "completed": completed,
            "pending": total - completed,
        },
    }


def get_study_plan_day(
    memory: LearningMemory,
    *,
    user_id: str,
    plan_date: str,
) -> dict[str, Any]:
    """Return one day's study-plan items."""
    items = memory.list_study_plan_items_for_day(user_id, plan_date)
    completed = sum(1 for item in items if item.get("status") == "completed")
    return {
        "user_id": user_id,
        "date": plan_date,
        "items": items,
        "stats": {
            "total": len(items),
            "completed": completed,
            "pending": len(items) - completed,
        },
    }


def create_study_plan_item(
    memory: LearningMemory,
    *,
    user_id: str,
    plan_date: str,
    title: str,
    notes: str | None = None,
    linked_course_id: str | None = None,
    linked_course_title: str | None = None,
    kg_node_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Create a plan item and record a lightweight plan event."""
    item = memory.create_study_plan_item(
        user_id=user_id,
        plan_date=plan_date,
        title=title,
        notes=notes,
        linked_course_id=linked_course_id,
        linked_course_title=linked_course_title,
        kg_node_ids=kg_node_ids,
    )
    event_id = memory.record_event(
        session_id=STUDY_PLAN_SESSION_ID,
        user_id=user_id,
        event_type="study_plan_item_created",
        outline_node_id=None,
        payload={
            **_event_payload_from_item(item),
            "surface": "study_plan",
            "status": "open",
        },
    )
    return {"item": item, "event_id": event_id}


def update_study_plan_item(
    memory: LearningMemory,
    *,
    item_id: str,
    user_id: str,
    updates: dict[str, Any],
) -> dict[str, Any]:
    """Update editable item fields."""
    return {"item": memory.update_study_plan_item(item_id, user_id=user_id, updates=updates)}


def complete_study_plan_item(
    memory: LearningMemory,
    *,
    item_id: str,
    user_id: str,
    completed: bool = True,
) -> dict[str, Any]:
    """Toggle completion and feed completed items into learner memory."""
    before = memory.get_study_plan_item(item_id, user_id=user_id)
    item = memory.set_study_plan_item_completion(
        item_id,
        user_id=user_id,
        completed=completed,
    )
    event_id: str | None = None
    learner_memory_update: dict[str, Any] | None = None
    if completed and before.get("status") != "completed":
        event_id = memory.record_event(
            session_id=STUDY_PLAN_SESSION_ID,
            user_id=user_id,
            event_type="study_plan_item_completed",
            outline_node_id=None,
            payload={
                **_event_payload_from_item(item),
                "surface": "study_plan",
                "status": "completed",
                "completed_at": item.get("completed_at"),
                "feedback_text": f"用户已完成计划中的：{item['title']}",
            },
        )
        learner_memory_update = _apply_completion_memory(
            memory,
            user_id=user_id,
            event_id=event_id,
            item=item,
        )
    return {
        "item": item,
        "event_id": event_id,
        "learner_memory_update": learner_memory_update,
    }


def defer_study_plan_item(
    memory: LearningMemory,
    *,
    item_id: str,
    user_id: str,
    to_date: str | None = None,
) -> dict[str, Any]:
    """Move one plan item to another day and record the change."""
    before = memory.get_study_plan_item(item_id, user_id=user_id)
    target = to_date
    if not target:
        target = (date.fromisoformat(before["plan_date"]) + timedelta(days=1)).isoformat()
    item = memory.defer_study_plan_item(item_id, user_id=user_id, to_date=target)
    event_id = memory.record_event(
        session_id=STUDY_PLAN_SESSION_ID,
        user_id=user_id,
        event_type="study_plan_item_deferred",
        outline_node_id=None,
        payload={
            **_event_payload_from_item(item),
            "surface": "study_plan",
            "status": "open",
            "deferred_from": before.get("plan_date"),
            "deferred_to": item.get("plan_date"),
        },
    )
    return {"item": item, "event_id": event_id}


def delete_study_plan_item(
    memory: LearningMemory,
    *,
    item_id: str,
    user_id: str,
) -> dict[str, Any]:
    """Delete a study-plan item."""
    return {"item": memory.delete_study_plan_item(item_id, user_id=user_id)}


def _event_payload_from_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "plan_item_id": item.get("id"),
        "plan_date": item.get("plan_date"),
        "title": item.get("title"),
        "linked_course_id": item.get("linked_course_id"),
        "linked_course_title": item.get("linked_course_title"),
        "kg_node_ids": item.get("kg_node_ids", []),
        "deferred_count": item.get("deferred_count", 0),
    }


def _apply_completion_memory(
    memory: LearningMemory,
    *,
    user_id: str,
    event_id: str,
    item: dict[str, Any],
) -> dict[str, Any]:
    title = str(item.get("title") or "").strip()
    extraction = {
        "should_update": True,
        "signals": [
            {
                "slot": "rhythm",
                "action": "reinforce",
                "aspect": "persistence_pattern",
                "label": "计划完成",
                "content": f"用户已完成计划中的：{title}",
                "kg_node_ids": item.get("kg_node_ids", []),
                "confidence": 0.72,
                "evidence_event_ids": [event_id],
                "reason": "study plan checklist completion",
            }
        ],
        "trace": {
            "mode": "deterministic_study_plan_memory",
            "status": "completed_item_recorded",
            "uses_llm": False,
        },
    }
    reduction = reduce_memory_signals(
        memory,
        user_id=user_id,
        session_id=STUDY_PLAN_SESSION_ID,
        event_id=event_id,
        surface="study_plan",
        extraction=extraction,
    )
    return {
        "updated": bool(reduction.get("applied_count", 0)),
        "event_id": event_id,
        "surface": "study_plan",
        "extraction": extraction,
        "reduction": reduction,
        "trace": {
            "mode": "deterministic_study_plan_memory",
            "applied_count": reduction.get("applied_count", 0),
        },
    }
