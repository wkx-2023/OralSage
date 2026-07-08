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


def recommend_study_plan(
    memory: LearningMemory,
    *,
    user_id: str,
    plan_date: str,
    max_items: int = 4,
) -> dict[str, Any]:
    """Return preview recommendations for one study-plan day.

    Recommendations are intentionally deterministic and lightweight so this
    path can stay fast even when the main learning chain is busy.
    """
    target_date = _normalize_recommendation_date(plan_date)
    target = date.fromisoformat(target_date)

    profile = memory.get_or_create_profile(user_id)
    daily_minutes = _profile_daily_budget(profile)
    assessment_preference = str(profile.get("assessment_preference") or "moderate").strip() or "moderate"
    planning_support_need = str(profile.get("planning_support_need") or "normal").strip() or "normal"
    item_limit = _recommended_item_limit(
        requested=max_items,
        daily_minutes=daily_minutes,
        planning_support_need=planning_support_need,
    )
    learner_memory = memory.get_learner_memory(user_id)
    sessions = memory.list_sessions_for_user(user_id, limit=8)
    existing_items = memory.list_study_plan_items_for_day(user_id, target_date)
    existing_titles = {_normalize_title(item.get("title")) for item in existing_items}
    recent_items = memory.list_study_plan_items(
        user_id,
        start_date=(target - timedelta(days=21)).isoformat(),
        end_date=target_date,
    )

    active_session = _find_active_session(memory, sessions)
    recommendations: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    _append_recommendation(
        recommendations,
        seen_keys,
        existing_titles,
        _recommend_continue_lesson(active_session),
    )
    for item in _recommend_weakness_reviews(
        profile=profile,
        learner_memory=learner_memory,
        active_session=active_session,
    ):
        _append_recommendation(recommendations, seen_keys, existing_titles, item)
    for item in _recommend_low_mastery_reviews(memory, user_id=user_id, sessions=sessions):
        _append_recommendation(recommendations, seen_keys, existing_titles, item)
    for item in _recommend_catch_up_items(recent_items, target_date=target_date):
        _append_recommendation(recommendations, seen_keys, existing_titles, item)

    practice_item = _recommend_practice_block(
        memory,
        user_id=user_id,
        sessions=sessions,
        assessment_preference=assessment_preference,
        daily_minutes=daily_minutes,
    )
    _append_recommendation(recommendations, seen_keys, existing_titles, practice_item)

    if not recommendations:
        for item in _fallback_recommendations(
            profile=profile,
            active_session=active_session,
            daily_minutes=daily_minutes,
        ):
            _append_recommendation(recommendations, seen_keys, existing_titles, item)

    recommendations.sort(
        key=lambda item: (-float(item.get("priority", 0.0)), str(item.get("title") or "")),
    )
    recommendations = recommendations[:item_limit]

    return {
        "user_id": user_id,
        "plan_date": target_date,
        "mode": "preview_recommendations",
        "recommendations": recommendations,
        "trace": {
            "strategy": "deterministic_dual_mode_study_plan",
            "existing_day_item_count": len(existing_items),
            "recent_item_count": len(recent_items),
            "session_count": len(sessions),
            "active_session_id": active_session.get("id") if active_session else None,
            "active_session_topic": active_session.get("topic") if active_session else None,
            "recommendation_count": len(recommendations),
            "daily_available_minutes": daily_minutes,
            "assessment_preference": assessment_preference,
            "planning_support_need": planning_support_need,
            "used_slots": [slot for slot in ("weaknesses", "mastery", "rhythm") if slot in learner_memory],
        },
    }


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


def _normalize_recommendation_date(plan_date: str) -> str:
    text = str(plan_date or "").strip()[:10]
    try:
        target = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"Invalid plan date: {plan_date!r}") from exc
    if target < date.today():
        raise ValueError("recommendations only support today or future dates")
    return target.isoformat()


def _normalize_title(value: Any) -> str:
    return " ".join(str(value or "").split()).strip().lower()


def _clean_text(value: Any, *, limit: int = 240) -> str:
    text = " ".join(str(value or "").split()).strip()
    return text[:limit].rstrip() if len(text) > limit else text


def _profile_daily_budget(profile: dict[str, Any]) -> int:
    raw = profile.get("daily_available_minutes", 45)
    try:
        minutes = int(raw)
    except (TypeError, ValueError):
        minutes = 45
    return max(10, min(minutes, 240))


def _recommended_item_limit(
    *,
    requested: int,
    daily_minutes: int,
    planning_support_need: str,
) -> int:
    if daily_minutes <= 30:
        budget_limit = 2
    elif daily_minutes <= 45:
        budget_limit = 3
    elif daily_minutes <= 75:
        budget_limit = 4
    else:
        budget_limit = 5
    if planning_support_need == "high":
        budget_limit = min(budget_limit, 3)
    return max(1, min(int(requested or 4), budget_limit, 6))


def _slot_items(learner_memory: dict[str, Any], slot: str) -> list[dict[str, Any]]:
    slot_data = learner_memory.get(slot, {})
    if not isinstance(slot_data, dict):
        return []
    items = slot_data.get("items", [])
    return [item for item in items if isinstance(item, dict)]


def _recommendation(
    *,
    recommendation_id: str,
    title: str,
    category: str,
    reason: str,
    priority: float,
    estimated_minutes: int,
    linked_course_id: str | None = None,
    linked_course_title: str | None = None,
    kg_node_ids: list[str] | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    clean_title = _clean_text(title, limit=180)
    clean_reason = _clean_text(reason, limit=220)
    clean_notes = _clean_text(notes or f"系统推荐：{clean_reason}", limit=800)
    kg_ids = [str(item).strip() for item in (kg_node_ids or []) if str(item).strip()]
    return {
        "recommendation_id": recommendation_id,
        "title": clean_title,
        "category": category,
        "reason": clean_reason,
        "priority": round(float(priority), 3),
        "estimated_minutes": max(5, min(int(estimated_minutes), 90)),
        "linked_course_id": str(linked_course_id or "").strip() or None,
        "linked_course_title": _clean_text(linked_course_title, limit=180) or None,
        "kg_node_ids": kg_ids[:12],
        "notes": clean_notes,
    }


def _append_recommendation(
    recommendations: list[dict[str, Any]],
    seen_keys: set[str],
    existing_titles: set[str],
    item: dict[str, Any] | None,
) -> None:
    if not item:
        return
    title_key = _normalize_title(item.get("title"))
    if not title_key or title_key in existing_titles:
        return
    dedupe_key = item.get("recommendation_id") or title_key
    if dedupe_key in seen_keys:
        return
    seen_keys.add(str(dedupe_key))
    recommendations.append(item)


def _find_active_session(
    memory: LearningMemory,
    sessions: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for session in sessions:
        if session.get("current_outline_node_id"):
            outline_record = memory.latest_outline(session["id"])
            if outline_record:
                return {
                    **session,
                    "_outline": outline_record.get("outline", {}),
                }
    return None


def _find_outline_node(
    outline: dict[str, Any],
    node_id: str,
) -> dict[str, Any] | None:
    for node in outline.get("nodes", []) if isinstance(outline, dict) else []:
        if isinstance(node, dict) and node.get("id") == node_id:
            return node
    return None


def _recommend_continue_lesson(active_session: dict[str, Any] | None) -> dict[str, Any] | None:
    if not active_session:
        return None
    current_node_id = str(active_session.get("current_outline_node_id") or "").strip()
    outline = active_session.get("_outline", {})
    node = _find_outline_node(outline, current_node_id)
    if not current_node_id or not node:
        return None
    topic = str(active_session.get("topic") or "当前课程").strip()
    node_title = str(node.get("title") or current_node_id).strip()
    return _recommendation(
        recommendation_id=f"continue:{active_session['id']}:{current_node_id}",
        title=f"继续学习：{topic} - {node_title}",
        category="continue_lesson",
        reason="最近有一节课程尚未完成，优先继续当前进度最容易形成连续学习节奏。",
        priority=1.0,
        estimated_minutes=int(node.get("estimated_minutes") or 18),
        linked_course_id=active_session.get("id"),
        linked_course_title=topic,
        kg_node_ids=node.get("kg_node_ids") or [],
    )


def _recommend_weakness_reviews(
    *,
    profile: dict[str, Any],
    learner_memory: dict[str, Any],
    active_session: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    topic = active_session.get("topic") if active_session else None

    weakness_items = sorted(
        _slot_items(learner_memory, "weaknesses"),
        key=lambda item: (
            -float(item.get("severity", item.get("confidence", 0.0)) or 0.0),
            str(item.get("updated_at") or ""),
        ),
    )
    for item in weakness_items[:2]:
        label = _clean_text(item.get("label") or item.get("text"), limit=72)
        if not label:
            continue
        results.append(
            _recommendation(
                recommendation_id=f"weakness:{label}",
                title=f"复习薄弱点：{label}",
                category="review_weakness",
                reason="近期问答或测验中反复出现这个薄弱点，建议优先安排针对性回顾。",
                priority=0.92,
                estimated_minutes=15,
                linked_course_title=topic,
                kg_node_ids=item.get("kg_node_ids") or [],
            )
        )

    if results:
        return results

    weak_points = [str(item).strip() for item in profile.get("known_weak_points", []) if str(item).strip()]
    for label in weak_points[:2]:
        results.append(
            _recommendation(
                recommendation_id=f"profile-weakness:{label}",
                title=f"复习薄弱点：{label}",
                category="review_weakness",
                reason="画像里已经记录这是当前薄弱知识点，适合放进下一次计划里巩固。",
                priority=0.84,
                estimated_minutes=15,
                linked_course_title=topic,
            )
        )
    return results


def _recommend_low_mastery_reviews(
    memory: LearningMemory,
    *,
    user_id: str,
    sessions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen_labels: set[str] = set()
    for session in sessions[:4]:
        mastery_map = memory.get_mastery_by_kg_node(user_id, session["id"])
        rows = sorted(
            mastery_map.values(),
            key=lambda row: float(row.get("mastery_score", 0.0)),
        )
        for row in rows:
            mastery_score = float(row.get("mastery_score", 0.0))
            if mastery_score >= 0.62:
                continue
            label = _clean_text(row.get("concept_name") or row.get("kg_node_id"), limit=72)
            if not label:
                continue
            label_key = label.lower()
            if label_key in seen_labels:
                continue
            seen_labels.add(label_key)
            results.append(
                _recommendation(
                    recommendation_id=f"mastery:{session['id']}:{row.get('kg_node_id') or label}",
                    title=f"回顾掌握较弱的知识点：{label}",
                    category="review_mastery",
                    reason=f"最近学习记录显示这个知识点掌握度偏低（约 {round(mastery_score * 100)}%），适合再回看一次。",
                    priority=0.78 - mastery_score * 0.15,
                    estimated_minutes=12,
                    linked_course_id=session.get("id"),
                    linked_course_title=session.get("topic"),
                    kg_node_ids=[row.get("kg_node_id")] if row.get("kg_node_id") else [],
                )
            )
            if len(results) >= 2:
                return results
    return results


def _recommend_catch_up_items(
    recent_items: list[dict[str, Any]],
    *,
    target_date: str,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    pending = [
        item
        for item in recent_items
        if item.get("status") != "completed" and str(item.get("plan_date") or "") < target_date
    ]
    pending.sort(
        key=lambda item: (
            -int(item.get("deferred_count", 0) or 0),
            str(item.get("plan_date") or ""),
        ),
        reverse=True,
    )
    for item in pending[:1]:
        previous_date = str(item.get("plan_date") or "")
        results.append(
            _recommendation(
                recommendation_id=f"catchup:{item.get('id')}",
                title=str(item.get("title") or "补上之前未完成的计划"),
                category="catch_up",
                reason=f"{previous_date} 的计划还没有完成，建议优先补上，避免待办持续积压。",
                priority=0.74 + min(int(item.get("deferred_count", 0) or 0), 3) * 0.02,
                estimated_minutes=12,
                linked_course_id=item.get("linked_course_id"),
                linked_course_title=item.get("linked_course_title"),
                kg_node_ids=item.get("kg_node_ids") or [],
                notes=item.get("notes") or "",
            )
        )
    return results


def _recommend_practice_block(
    memory: LearningMemory,
    *,
    user_id: str,
    sessions: list[dict[str, Any]],
    assessment_preference: str,
    daily_minutes: int,
) -> dict[str, Any] | None:
    total_wrong = 0
    focus_topic = ""
    for session in sessions[:3]:
        answers = memory.list_answer_records(session["id"])
        wrong_count = sum(1 for item in answers if not bool(item.get("is_correct")))
        if wrong_count <= 0:
            continue
        total_wrong += wrong_count
        focus_topic = str(session.get("topic") or "").strip()
        break
    if total_wrong <= 0 and assessment_preference != "frequent":
        return None
    if assessment_preference == "light" and total_wrong < 2:
        return None
    return _recommendation(
        recommendation_id=f"practice:{focus_topic or user_id}",
        title=f"完成一组{focus_topic or '当前主题'}错题回顾练习",
        category="practice",
        reason=f"最近练习里还有 {total_wrong} 道错题相关信号，安排一组回顾练习能更快巩固薄弱环节。",
        priority=0.7,
        estimated_minutes=15,
        linked_course_title=focus_topic or None,
    )


def _fallback_recommendations(
    *,
    profile: dict[str, Any],
    active_session: dict[str, Any] | None,
    daily_minutes: int,
) -> list[dict[str, Any]]:
    topic = active_session.get("topic") if active_session else None
    goal = str(profile.get("goal") or "course_learning")
    planning_support_need = str(profile.get("planning_support_need") or "normal")
    return [
        _recommendation(
            recommendation_id="fallback:review",
            title=f"整理{topic or '本周'}的 3 个关键知识点",
            category="review_summary",
            reason="当前还没有足够强的个性化信号时，先做轻量总结最稳妥。",
            priority=0.48,
            estimated_minutes=min(12, max(8, daily_minutes // 3)),
            linked_course_title=topic,
        ),
        _recommendation(
            recommendation_id="fallback:practice",
            title="安排一次 10 分钟自测回顾",
            category="practice",
            reason=f"画像目标当前偏向 {goal}，保留一个短时自测任务有助于形成稳定节奏。",
            priority=0.44,
            estimated_minutes=min(10, max(6, daily_minutes // 5)),
            linked_course_title=topic,
        ),
    ]
