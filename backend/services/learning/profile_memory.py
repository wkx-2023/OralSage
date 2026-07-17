"""Layered learner memory for the learning agents.

This module implements the first DeepTutor-inspired memory path:

L1 learning event -> LLM signal extraction -> deterministic reducer ->
L2 per-surface summary + L3 learner profile slots -> role-specific slices.

The LLM is allowed to understand and propose signals.  It is not allowed to
write memory directly; reducers validate evidence ids, slots, actions, and
merge shape before persisting anything.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from backend.services.llm_client import chat_completion, is_llm_configured

from .memory import LearningMemory
from .user_model import merge_profile

PROFILE_STATE_SLOT = "profile_state"
SIGNAL_MEMORY_SLOTS = (
    "profile",
    "mastery",
    "weaknesses",
    "preferences",
    "goals",
    "rhythm",
)
MEMORY_SLOTS = SIGNAL_MEMORY_SLOTS + (PROFILE_STATE_SLOT,)
SIGNAL_ACTIONS = ("add", "update", "reinforce", "weaken", "ignore")
STYLE_DIMENSIONS: dict[str, tuple[str, ...]] = {
    "explanation_depth": ("concise", "standard", "mechanism_deep"),
    "entry_mode": ("concept_first", "case_first", "question_first", "practice_first"),
    "interaction_tendency": ("asks_why", "asks_examples", "asks_exam_points", "passive"),
    "pace": ("slow", "standard", "fast"),
}
STYLE_TOKEN_RULES: tuple[tuple[str, str, tuple[str, ...], float], ...] = (
    ("explanation_depth", "mechanism_deep", ("why", "mechanism", "pathway", "causal", "principle", "为什么", "机制", "原理", "因果", "通路", "病理", "rankl", "opg"), 1.0),
    ("explanation_depth", "concise", ("concise", "brief", "short", "direct", "简洁", "要点", "直接", "太长"), 0.9),
    ("entry_mode", "concept_first", ("concept", "definition", "principle", "概念", "定义", "原理", "本质"), 0.75),
    ("entry_mode", "case_first", ("case", "clinical", "example", "病例", "案例", "临床", "举例"), 0.9),
    ("entry_mode", "question_first", ("question", "ask", "follow-up", "提问", "追问", "问题", "疑问"), 0.85),
    ("entry_mode", "practice_first", ("practice", "quiz", "exercise", "wrong answer", "retrieval_enhanced", "active recall", "练习", "做题", "题目", "错题", "答题", "检索增强", "主动回忆"), 0.85),
    ("interaction_tendency", "asks_why", ("why", "mechanism", "causal", "为什么", "机制", "因果", "原理"), 1.0),
    ("interaction_tendency", "asks_examples", ("example", "case", "clinical", "举例", "例子", "病例", "案例", "临床"), 0.9),
    ("interaction_tendency", "asks_exam_points", ("exam", "test", "key point", "考试", "考点", "重点", "怎么记"), 0.9),
    ("interaction_tendency", "passive", ("passive", "被动", "很少提问", "不主动"), 0.7),
    ("pace", "slow", ("slow", "slower", "too fast", "慢", "放慢", "太快", "跟不上"), 1.0),
    ("pace", "fast", ("fast", "faster", "too slow", "快", "加快", "太慢"), 1.0),
    ("pace", "standard", ("standard", "normal pace", "标准节奏", "正常节奏"), 0.5),
)
USER_STYLE_EVENT_TYPES = {
    "user_interruption_question",
    "answer_submitted",
    "resource_opened",
    "reading_completed",
    "review_completed",
    "lesson_finished",
    "study_plan_item_completed",
    "study_plan_item_deferred",
}
LEARNER_STYLE_DEFINITIONS: dict[str, tuple[str, str]] = {
    "exam_oriented_learner": (
        "考试导向型学习者",
        "你更适合围绕考试重点组织学习，并通过练习反馈持续校准。",
    ),
    "practice_driven_learner": (
        "练习驱动型学习者",
        "你更适合边做边学，通过问题、反馈和错题复盘建立理解。",
    ),
    "case_driven_learner": (
        "病例驱动型学习者",
        "你更适合从病例或临床情境进入，再回到概念理解。",
    ),
    "text_depth_learner": (
        "推导深度型学习者",
        "你更偏好机制、原理和推理过程的分步拆解。",
    ),
    "steady_paced_learner": (
        "稳健节奏型学习者",
        "你更适合稳定推进、少量多次、循序渐进的学习节奏。",
    ),
    "visual_structured_learner": (
        "图解结构型学习者",
        "你更适合通过结构清晰、分层明确的框架组织知识。",
    ),
    "concept_then_case_learner": (
        "概念后接病例型学习者",
        "你更适合先理解概念框架，再通过病例或案例验证理解。",
    ),
    "concise_focus_learner": (
        "要点聚焦型学习者",
        "你更适合先获得简洁清晰的要点，再按需要补充细节。",
    ),
}
LEARNER_STYLE_TOKEN_MAP = {
    "exam_oriented_learner": "exam_oriented_learner",
    "practice_driven_learner": "practice_driven_learner",
    "case_driven_learner": "case_driven_learner",
    "text_depth_learner": "text_depth_learner",
    "steady_paced_learner": "steady_paced_learner",
    "visual_structured_learner": "visual_structured_learner",
    "concept_then_case_learner": "concept_then_case_learner",
    "concise_focus_learner": "concise_focus_learner",
    "考试导向型学习者": "exam_oriented_learner",
    "练习驱动型学习者": "practice_driven_learner",
    "检索增强型学习者": "practice_driven_learner",
    "病例驱动型学习者": "case_driven_learner",
    "文本深度型学习者": "text_depth_learner",
    "推导型学习者": "text_depth_learner",
    "推导深度型学习者": "text_depth_learner",
    "稳健节奏型学习者": "steady_paced_learner",
    "视觉结构型学习者": "visual_structured_learner",
    "图解结构型学习者": "visual_structured_learner",
    "概念后接病例型学习者": "concept_then_case_learner",
    "要点型学习者": "concise_focus_learner",
    "要点聚焦型学习者": "concise_focus_learner",
    "retrieval_enhanced": "practice_driven_learner",
    "retrieval_enhanced_learner": "practice_driven_learner",
    "active_recall_learner": "practice_driven_learner",
}
STYLE_DIMENSION_TYPE_MAP = {
    ("explanation_depth", "mechanism_deep"): "text_depth_learner",
    ("explanation_depth", "concise"): "concise_focus_learner",
    ("entry_mode", "concept_first"): "concept_then_case_learner",
    ("entry_mode", "case_first"): "case_driven_learner",
    ("entry_mode", "question_first"): "practice_driven_learner",
    ("entry_mode", "practice_first"): "practice_driven_learner",
    ("interaction_tendency", "asks_why"): "text_depth_learner",
    ("interaction_tendency", "asks_examples"): "case_driven_learner",
    ("interaction_tendency", "asks_exam_points"): "exam_oriented_learner",
    ("pace", "slow"): "steady_paced_learner",
}
MEMORY_ASPECTS: dict[str, tuple[str, ...]] = {
    "profile": (
        "learning_style_tendency",
        "cognitive_style",
        "base_level",
        "general_trait",
    ),
    "mastery": ("concept_mastery",),
    "weaknesses": (
        "weak_concept",
        "misconception",
        "error_cause",
        "repeated_confusion",
    ),
    "preferences": (
        "explanation_preference",
        "modality_preference",
        "interaction_preference",
    ),
    "goals": ("learning_goal", "exam_goal", "clinical_goal"),
    "rhythm": ("pace_preference", "persistence_pattern", "review_need"),
}
SINGLETON_ASPECTS = {
    ("profile", "learning_style_tendency"),
    ("profile", "base_level"),
}
SURFACE_EVENT_TYPES: dict[str, tuple[str, ...]] = {
    "qa": ("user_interruption_question",),
    "quiz": ("answer_submitted",),
    "lecture": ("tutor_exposure", "tutor_step_generated"),
    "planning": ("lesson_started", "progress_updated"),
    "reading": ("resource_opened", "reading_completed"),
    "review": ("review_completed", "lesson_finished"),
    "study_plan": (
        "study_plan_item_created",
        "study_plan_item_completed",
        "study_plan_item_deferred",
    ),
}

_MEMORY_API_KEY_NAMES = (
    "LEARNING_MEMORY_LLM_API_KEY",
    "LEARNING_LLM_API_KEY",
    "QWEN_API_KEY",
    "DASHSCOPE_API_KEY",
)
_MEMORY_BASE_URL_NAMES = (
    "LEARNING_MEMORY_LLM_BASE_URL",
    "LEARNING_LLM_BASE_URL",
    "QWEN_BASE_URL",
    "DASHSCOPE_BASE_URL",
)
_MEMORY_MODEL_NAMES = (
    "LEARNING_MEMORY_LLM_MODEL",
    "LEARNING_LLM_MODEL",
    "QWEN_MODEL",
    "DASHSCOPE_MODEL",
)


def is_learning_memory_llm_configured() -> bool:
    """Return True when an LLM is available for memory signal extraction."""
    return is_llm_configured(*_MEMORY_API_KEY_NAMES)


def update_learner_memory_from_event(
    memory: LearningMemory,
    *,
    user_id: str,
    session_id: str,
    event_id: str,
    surface: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run realtime L3 learner-memory update for one L1 event.

    The update is best-effort.  Failures are returned in the trace and should
    not block the main learning flow.
    """
    event = memory.get_event(event_id, user_id=user_id)
    if event is None:
        return {
            "updated": False,
            "reason": "event_not_found",
            "event_id": event_id,
            "trace": {"mode": "learner_memory_update"},
        }

    recent_events = memory.list_recent_events(user_id, session_id=session_id, limit=8)
    learner_memory = _with_default_slots(memory.get_learner_memory(user_id))
    compact_profile = memory.get_or_create_profile(user_id)

    extraction = extract_event_signals(
        event=event,
        surface=surface,
        recent_events=recent_events,
        learner_memory=learner_memory,
        compact_profile=compact_profile,
        context=context or {},
    )
    reduction = reduce_memory_signals(
        memory,
        user_id=user_id,
        session_id=session_id,
        event_id=event_id,
        surface=surface,
        extraction=extraction,
    )

    return {
        "updated": bool(reduction.get("applied_count", 0)),
        "event_id": event_id,
        "surface": surface,
        "extraction": extraction,
        "reduction": reduction,
        "trace": {
            "mode": "learner_memory_update",
            "extractor_status": extraction.get("trace", {}).get("status"),
            "applied_count": reduction.get("applied_count", 0),
        },
    }


def consolidate_l2(
    memory: LearningMemory,
    *,
    user_id: str,
    session_id: str | None = None,
    surface: str | None = None,
    surfaces: list[str] | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Incrementally summarize L1 events into per-surface L2 summaries."""
    chosen = surfaces or ([surface] if surface else ["qa", "quiz", "lecture", "planning", "study_plan"])
    results: dict[str, Any] = {}
    for item in chosen:
        if not item:
            continue
        results[item] = _consolidate_l2_surface(
            memory,
            user_id=user_id,
            session_id=session_id,
            surface=item,
            limit=limit,
        )
    return {
        "layer": "L2",
        "user_id": user_id,
        "session_id": session_id,
        "results": results,
        "summaries_added": sum(1 for r in results.values() if r.get("summary")),
    }


def consolidate_l3(
    memory: LearningMemory,
    *,
    user_id: str,
    session_id: str | None = None,
    slot: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Incrementally consolidate L2 summaries into L3 learner-memory slots."""
    key = _meta_key("l3", slot or "all", session_id)
    seen = memory.get_consolidation_seen_ids(user_id, "L3", key)
    summaries = memory.list_learning_summaries(
        user_id,
        session_id=session_id,
        limit=limit,
    )
    new_summaries = [s for s in summaries if s.get("id") not in seen]
    if not new_summaries:
        return {
            "layer": "L3",
            "user_id": user_id,
            "session_id": session_id,
            "slot": slot,
            "applied_count": 0,
            "summary_count": 0,
            "no_new_input": True,
        }

    extraction = extract_summary_signals(
        summaries=new_summaries,
        learner_memory=_with_default_slots(memory.get_learner_memory(user_id)),
        compact_profile=memory.get_or_create_profile(user_id),
        target_slot=slot,
    )
    source_event_ids = _source_event_ids_from_summaries(new_summaries)
    fallback_event_id = source_event_ids[0] if source_event_ids else ""
    if not fallback_event_id:
        memory.save_consolidation_seen_ids(
            user_id,
            "L3",
            key,
            seen | {str(s["id"]) for s in new_summaries if s.get("id")},
        )
        return {
            "layer": "L3",
            "user_id": user_id,
            "session_id": session_id,
            "slot": slot,
            "applied_count": 0,
            "summary_count": len(new_summaries),
            "reason": "no_source_event_ids",
        }

    reduction = reduce_memory_signals(
        memory,
        user_id=user_id,
        session_id=session_id or "",
        event_id=fallback_event_id,
        surface="l3_consolidation",
        extraction=extraction,
        append_summary=False,
    )
    memory.save_consolidation_seen_ids(
        user_id,
        "L3",
        key,
        seen | {str(s["id"]) for s in new_summaries if s.get("id")},
    )
    return {
        "layer": "L3",
        "user_id": user_id,
        "session_id": session_id,
        "slot": slot,
        "summary_count": len(new_summaries),
        "extraction": extraction,
        "reduction": reduction,
        "applied_count": reduction.get("applied_count", 0),
    }


def extract_event_signals(
    *,
    event: dict[str, Any],
    surface: str,
    recent_events: list[dict[str, Any]],
    learner_memory: dict[str, Any],
    compact_profile: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    """Use the LLM to extract candidate memory signals from one event."""
    if not is_learning_memory_llm_configured():
        return _rule_event_style_extraction(event, status="fallback_no_llm")

    messages = _build_signal_extractor_messages(
        event=event,
        surface=surface,
        recent_events=recent_events,
        learner_memory=learner_memory,
        compact_profile=compact_profile,
        context=context,
    )
    try:
        raw = chat_completion(
            messages,
            temperature=0.15,
            max_tokens=1400,
            api_key_names=_MEMORY_API_KEY_NAMES,
            base_url_names=_MEMORY_BASE_URL_NAMES,
            model_names=_MEMORY_MODEL_NAMES,
            response_format={"type": "json_object"},
        )
    except Exception as exc:  # noqa: BLE001 - best-effort memory layer
        return _rule_event_style_extraction(
            event,
            status="fallback_exception",
            error=str(exc),
        )

    try:
        parsed = _extract_json(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        return _rule_event_style_extraction(
            event,
            status="fallback_invalid_json",
            error=str(exc),
        )

    signals = _normalize_signals(parsed.get("signals", []), fallback_event_id=event["id"])
    return {
        "should_update": bool(parsed.get("should_update", bool(signals))),
        "signals": signals,
        "trace": {
            "mode": "llm_event_signal_extractor",
            "status": "llm_extracted",
            "uses_llm": True,
            "raw_signal_count": len(parsed.get("signals", []))
            if isinstance(parsed.get("signals", []), list)
            else 0,
            "valid_signal_count": len(signals),
        },
    }


def _rule_event_style_extraction(
    event: dict[str, Any],
    *,
    status: str,
    error: str = "",
) -> dict[str, Any]:
    event_type = str(event.get("event_type") or "")
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    texts: list[str] = []
    if event_type == "user_interruption_question":
        texts.append(str(payload.get("question") or ""))
    for key in ("learning_strategy", "preferred_explanation", "interaction_preference"):
        texts.append(str(payload.get(key) or ""))
    label = _infer_learning_style_label(" ".join(texts))
    signals: list[dict[str, Any]] = []
    if event_type in USER_STYLE_EVENT_TYPES and label:
        signals = _normalize_signals(
            [
                {
                    "slot": "profile",
                    "aspect": "learning_style_tendency",
                    "label": label,
                    "action": "reinforce",
                    "content": _learning_style_content(label),
                    "confidence": 0.46,
                    "evidence_event_ids": [str(event.get("id") or "")],
                    "reason": "explicit user behavior rule fallback",
                }
            ],
            fallback_event_id=str(event.get("id") or ""),
        )
    trace = {
        "mode": "rule_event_style_extractor",
        "status": status,
        "uses_llm": status != "fallback_no_llm",
        "valid_signal_count": len(signals),
    }
    if error:
        trace["error"] = error[:240]
    return {
        "should_update": bool(signals),
        "signals": signals,
        "trace": trace,
    }


def extract_summary_signals(
    *,
    summaries: list[dict[str, Any]],
    learner_memory: dict[str, Any],
    compact_profile: dict[str, Any],
    target_slot: str | None = None,
) -> dict[str, Any]:
    """Extract candidate L3 signals from new L2 summaries."""
    fallback_event_id = (_source_event_ids_from_summaries(summaries) or [""])[0]
    if not is_learning_memory_llm_configured():
        return _rule_summary_signals(
            summaries,
            fallback_event_id=fallback_event_id,
            target_slot=target_slot,
            status="fallback_no_llm",
        )

    messages = _build_l3_consolidation_messages(
        summaries=summaries,
        learner_memory=learner_memory,
        compact_profile=compact_profile,
        target_slot=target_slot,
    )
    try:
        raw = chat_completion(
            messages,
            temperature=0.15,
            max_tokens=1600,
            api_key_names=_MEMORY_API_KEY_NAMES,
            base_url_names=_MEMORY_BASE_URL_NAMES,
            model_names=_MEMORY_MODEL_NAMES,
            response_format={"type": "json_object"},
        )
        parsed = _extract_json(raw)
    except Exception as exc:  # noqa: BLE001 - best-effort consolidation
        result = _rule_summary_signals(
            summaries,
            fallback_event_id=fallback_event_id,
            target_slot=target_slot,
            status="fallback_exception",
        )
        result["trace"]["error"] = str(exc)[:240]
        return result

    signals = _normalize_signals(
        parsed.get("signals", []),
        fallback_event_id=fallback_event_id,
    )
    signals = _ensure_learning_style_signal(
        signals,
        summaries=summaries,
        target_slot=target_slot,
        fallback_event_id=fallback_event_id,
    )
    return {
        "should_update": bool(parsed.get("should_update", bool(signals))),
        "signals": signals,
        "trace": {
            "mode": "llm_l3_consolidator",
            "status": "llm_extracted",
            "uses_llm": True,
            "summary_count": len(summaries),
            "valid_signal_count": len(signals),
        },
    }


def reduce_memory_signals(
    memory: LearningMemory,
    *,
    user_id: str,
    session_id: str,
    event_id: str,
    surface: str,
    extraction: dict[str, Any],
    append_summary: bool = True,
) -> dict[str, Any]:
    """Validate and apply candidate signals to L3 learner memory."""
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    by_slot: dict[str, list[dict[str, Any]]] = {}

    for signal in extraction.get("signals", []):
        if not isinstance(signal, dict):
            continue
        slot = str(signal.get("slot", "")).strip()
        action = str(signal.get("action", "add")).strip()
        if slot not in SIGNAL_MEMORY_SLOTS:
            skipped.append({"reason": "invalid_slot", "slot": slot})
            continue
        if action not in SIGNAL_ACTIONS or action == "ignore":
            skipped.append({"reason": "ignored_or_invalid_action", "slot": slot})
            continue
        evidence_ids = _valid_evidence_ids(
            memory,
            user_id=user_id,
            event_id=event_id,
            evidence_ids=_as_str_list(signal.get("evidence_event_ids")),
        )
        if not evidence_ids:
            skipped.append({"reason": "no_valid_evidence", "slot": slot})
            continue
        aspect = _normalize_aspect(slot, signal.get("aspect"))
        normalized = {
            "slot": slot,
            "action": action,
            "aspect": aspect,
            "label": _normalize_label(
                slot,
                aspect,
                signal.get("label"),
                signal.get("content") or signal.get("text"),
            ),
            "text": _clean_text(signal.get("content") or signal.get("text")),
            "kg_node_ids": _as_str_list(signal.get("kg_node_ids")),
            "confidence": _clamp_float(signal.get("confidence"), 0.0, 1.0, 0.5),
            "evidence_ids": evidence_ids,
            "source_summary_ids": _as_str_list(signal.get("source_summary_ids"))[:20],
            "score_delta": _clamp_float(signal.get("score_delta"), -0.3, 0.3, 0.0),
            "severity_delta": _clamp_float(signal.get("severity_delta"), -0.3, 0.3, 0.0),
            "reason": _clean_text(signal.get("reason"), limit=240),
        }
        if not normalized["text"]:
            skipped.append({"reason": "empty_text", "slot": slot})
            continue
        by_slot.setdefault(slot, []).append(normalized)

    for slot, slot_signals in by_slot.items():
        data = memory.get_learner_memory_slot(user_id, slot)
        items = data.get("items", [])
        if not isinstance(items, list):
            items = []
        for signal in slot_signals:
            entry = _apply_signal_to_items(items, signal)
            applied.append({
                "slot": slot,
                "aspect": entry.get("aspect", ""),
                "entry_id": entry.get("id", ""),
                "action": signal["action"],
                "text": entry.get("text", ""),
            })
        data["items"] = _trim_items(items, slot=slot)
        data["updated_at"] = _now()
        memory.save_learner_memory_slot(user_id, slot, data)

    summary = None
    if applied:
        if append_summary:
            summary = _append_l2_signal_summary(
                memory,
                user_id=user_id,
                session_id=session_id,
                surface=surface,
                event_id=event_id,
                applied=applied,
            )
        profile_state = refresh_profile_state_from_l3(memory, user_id)
        compact_profile = refresh_compact_profile_from_l3(memory, user_id)
    else:
        profile_state = memory.get_learner_memory_slot(user_id, PROFILE_STATE_SLOT)
        if "style" not in profile_state:
            profile_state = _default_profile_state()
        compact_profile = memory.get_or_create_profile(user_id)

    return {
        "applied_count": len(applied),
        "applied": applied,
        "skipped": skipped,
        "learning_summary": summary,
        "profile_state": profile_state,
        "compact_profile": compact_profile,
    }


def refresh_profile_state_from_l3(
    memory: LearningMemory,
    user_id: str,
) -> dict[str, Any]:
    """Recompute the stable L3 profile state from evidence memory slots."""
    l3 = _with_default_slots(memory.get_learner_memory(user_id))
    previous_state = l3.get(PROFILE_STATE_SLOT, {})
    if not isinstance(previous_state, dict):
        previous_state = {}
    compact_profile = memory.get_or_create_profile(user_id)
    style = _build_style_state(l3)
    weakness_summary = _build_weakness_summary(_slot_items(l3, "weaknesses"))
    mastery_summary = _build_mastery_summary(_slot_items(l3, "mastery"))
    assessed_style = _build_assessed_style(compact_profile)
    observed_style = _build_observed_style(
        memory,
        user_id=user_id,
        l3=l3,
        style=style,
    )
    effective_style = _build_effective_style(
        assessed_style,
        observed_style,
        previous_state=previous_state,
    )

    summary, personalization_policy, confidence = _build_profile_state_summary(
        style,
        weakness_summary,
        mastery_summary,
    )
    evidence_ids = _profile_state_evidence_ids(
        style,
        weakness_summary,
        mastery_summary,
    )
    state = {
        "schema_version": 1,
        "style_fusion_version": "v1",
        "slot": PROFILE_STATE_SLOT,
        "style": style,
        "assessed_style": assessed_style,
        "observed_style": observed_style,
        "effective_style": effective_style,
        "weakness_summary": weakness_summary,
        "mastery_summary": mastery_summary,
        "summary": summary,
        "confidence": confidence,
        "personalization_policy": personalization_policy,
        "evidence_ids": evidence_ids[:40],
        "updated_at": str(previous_state.get("updated_at") or ""),
    }
    comparable_keys = tuple(key for key in state if key != "updated_at")
    if previous_state and all(previous_state.get(key) == state.get(key) for key in comparable_keys):
        return previous_state
    state["updated_at"] = _now()
    return memory.save_learner_memory_slot(user_id, PROFILE_STATE_SLOT, state)


def _style_definition(learner_type: str) -> tuple[str, str]:
    return LEARNER_STYLE_DEFINITIONS.get(
        learner_type,
        ("学习风格正在形成", "系统会随后续学习行为继续校准你的学习画像。"),
    )


def _canonical_learner_type(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    direct = LEARNER_STYLE_TOKEN_MAP.get(text)
    if direct:
        return direct
    token = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return LEARNER_STYLE_TOKEN_MAP.get(token, "")


def _empty_style_profile(source: str) -> dict[str, Any]:
    return {
        "type": "",
        "label": "",
        "summary": "",
        "source": source,
        "status": "unobserved",
        "confidence": 0.0,
        "evidence_count": 0,
        "session_count": 0,
        "evidence_ids": [],
        "updated_at": "",
    }


def _build_assessed_style(profile: dict[str, Any]) -> dict[str, Any]:
    learner_type = _canonical_learner_type(profile.get("learner_type"))
    if not learner_type:
        return _empty_style_profile("assessment")
    label, default_summary = _style_definition(learner_type)
    return {
        "type": learner_type,
        "label": label,
        "summary": _clean_text(profile.get("learner_type_summary"), limit=180) or default_summary,
        "source": "assessment",
        "status": "baseline",
        "confidence": round(_clamp_float(profile.get("confidence"), 0.0, 0.95, 0.72), 3),
        "evidence_count": 0,
        "session_count": 0,
        "evidence_ids": [],
        "updated_at": str(profile.get("last_assessed_at") or ""),
    }


def _user_style_events(
    memory: LearningMemory,
    *,
    user_id: str,
    evidence_ids: list[str],
    cache: dict[str, dict[str, Any] | None],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for event_id in _dedupe(evidence_ids)[:40]:
        if event_id not in cache:
            cache[event_id] = memory.get_event(event_id, user_id=user_id)
        event = cache[event_id]
        if event and str(event.get("event_type") or "") in USER_STYLE_EVENT_TYPES:
            events.append(event)
    return events


def _build_observed_style(
    memory: LearningMemory,
    *,
    user_id: str,
    l3: dict[str, Any],
    style: dict[str, Any],
) -> dict[str, Any]:
    event_cache: dict[str, dict[str, Any] | None] = {}
    candidates: dict[str, dict[str, Any]] = {}

    def add_candidate(
        learner_type: str,
        *,
        confidence: float,
        evidence_ids: list[str],
        updated_at: str,
        rank_bonus: float = 0.0,
    ) -> None:
        if learner_type not in LEARNER_STYLE_DEFINITIONS:
            return
        events = _user_style_events(
            memory,
            user_id=user_id,
            evidence_ids=evidence_ids,
            cache=event_cache,
        )
        valid_ids = [str(event.get("id") or "") for event in events if event.get("id")]
        if not valid_ids:
            return
        event_updated_at = max((str(event.get("created_at") or "") for event in events), default="")
        current = candidates.setdefault(
            learner_type,
            {
                "type": learner_type,
                "confidence": 0.0,
                "evidence_ids": [],
                "updated_at": "",
                "rank": 0.0,
            },
        )
        current["evidence_ids"] = _dedupe(current["evidence_ids"] + valid_ids)[:40]
        evidence_count = len(current["evidence_ids"])
        normalized_confidence = _style_confidence(
            _clamp_float(confidence, 0.0, 1.0, 0.0),
            evidence_count,
        )
        current["confidence"] = round(max(float(current["confidence"]), normalized_confidence), 3)
        current["rank"] = max(float(current["rank"]), normalized_confidence + rank_bonus)
        current["updated_at"] = max(str(current["updated_at"]), event_updated_at, str(updated_at or ""))

    for item in _slot_items(l3, "profile"):
        if item.get("aspect") != "learning_style_tendency":
            continue
        learner_type = _canonical_learner_type(item.get("label"))
        if not learner_type:
            learner_type = _canonical_learner_type(_infer_learning_style_label(str(item.get("text") or "")))
        add_candidate(
            learner_type,
            confidence=_clamp_float(item.get("confidence"), 0.0, 1.0, 0.5),
            evidence_ids=_item_evidence_ids(item),
            updated_at=str(item.get("updated_at") or ""),
            rank_bonus=0.12,
        )

    for dimension, data in style.items():
        if not isinstance(data, dict):
            continue
        dominant = str(data.get("dominant") or "")
        learner_type = STYLE_DIMENSION_TYPE_MAP.get((dimension, dominant), "")
        option = (data.get("options") or {}).get(dominant, {})
        if not isinstance(option, dict):
            continue
        add_candidate(
            learner_type,
            confidence=_clamp_float(option.get("score"), 0.0, 1.0, 0.0),
            evidence_ids=_as_str_list(option.get("evidence_ids")),
            updated_at=str(option.get("last_seen_at") or ""),
        )

    if not candidates:
        return {**_empty_style_profile("learning_behavior"), "candidates": []}

    ranked = sorted(candidates.values(), key=lambda item: float(item.get("rank", 0.0)), reverse=True)
    top = ranked[0]
    evidence_ids = _dedupe(_as_str_list(top.get("evidence_ids")))
    evidence_events = _user_style_events(
        memory,
        user_id=user_id,
        evidence_ids=evidence_ids,
        cache=event_cache,
    )
    session_count = len({str(event.get("session_id") or "") for event in evidence_events if event.get("session_id")})
    evidence_count = len(evidence_ids)
    confidence = round(_clamp_float(top.get("confidence"), 0.0, 0.95, 0.0), 3)
    if evidence_count == 1:
        status = "forming"
    elif evidence_count < 4:
        status = "emerging"
    else:
        status = "stable" if confidence >= 0.65 else "emerging"
    if _is_stale_timestamp(str(top.get("updated_at") or "")):
        status = "stale"
    if len(ranked) > 1 and float(top.get("rank", 0.0)) - float(ranked[1].get("rank", 0.0)) < 0.12:
        status = "mixed"

    learner_type = str(top["type"])
    label, summary = _style_definition(learner_type)
    return {
        "type": learner_type,
        "label": label,
        "summary": summary,
        "source": "learning_behavior",
        "status": status,
        "confidence": confidence,
        "evidence_count": evidence_count,
        "session_count": session_count,
        "evidence_ids": evidence_ids[:20],
        "updated_at": str(top.get("updated_at") or ""),
        "candidates": [
            {
                "type": str(item["type"]),
                "label": _style_definition(str(item["type"]))[0],
                "confidence": round(_clamp_float(item.get("confidence"), 0.0, 0.95, 0.0), 3),
                "evidence_count": len(_dedupe(_as_str_list(item.get("evidence_ids")))),
            }
            for item in ranked[:3]
        ],
    }


def _style_transition(
    previous_effective: dict[str, Any],
    observed_style: dict[str, Any],
) -> dict[str, Any]:
    previous = previous_effective.get("transition")
    previous = previous if isinstance(previous, dict) else {}
    candidate_type = str(observed_style.get("type") or "")
    evidence_ids = _dedupe(_as_str_list(observed_style.get("evidence_ids")))
    if previous.get("candidate_type") != candidate_type:
        confirmations = 1
    else:
        previous_ids = set(_as_str_list(previous.get("evidence_ids")))
        confirmations = int(previous.get("confirmations") or 1)
        if any(event_id not in previous_ids for event_id in evidence_ids):
            confirmations += 1
    return {
        "candidate_type": candidate_type,
        "confirmations": min(confirmations, 3),
        "evidence_ids": evidence_ids[:20],
    }


def _effective_style_value(
    style_profile: dict[str, Any],
    *,
    source: str,
    status: str | None = None,
    confidence: float | None = None,
    transition: dict[str, Any] | None = None,
) -> dict[str, Any]:
    value = {
        key: style_profile.get(key, default)
        for key, default in _empty_style_profile(source).items()
    }
    value["source"] = source
    if status:
        value["status"] = status
    if confidence is not None:
        value["confidence"] = round(_clamp_float(confidence, 0.0, 0.95, 0.0), 3)
    if transition:
        value["transition"] = transition
    return value


def _build_effective_style(
    assessed_style: dict[str, Any],
    observed_style: dict[str, Any],
    *,
    previous_state: dict[str, Any],
) -> dict[str, Any]:
    assessed_type = str(assessed_style.get("type") or "")
    observed_type = str(observed_style.get("type") or "")
    previous_effective = previous_state.get("effective_style")
    previous_effective = previous_effective if isinstance(previous_effective, dict) else {}
    previous_type = str(previous_effective.get("type") or "")

    if not assessed_type and not observed_type:
        return _empty_style_profile("none")
    if not observed_type:
        return _effective_style_value(assessed_style, source="assessment")

    observed_stable = (
        observed_style.get("status") == "stable"
        and int(observed_style.get("evidence_count") or 0) >= 4
        and int(observed_style.get("session_count") or 0) >= 2
        and float(observed_style.get("confidence") or 0.0) >= 0.65
    )

    if not assessed_type:
        if previous_type and previous_type != observed_type:
            transition = _style_transition(previous_effective, observed_style)
            if not observed_stable or int(transition["confirmations"]) < 2:
                return _effective_style_value(
                    previous_effective,
                    source=str(previous_effective.get("source") or "learning_behavior"),
                    status="recalibrating",
                    transition=transition,
                )
        return _effective_style_value(observed_style, source="learning_behavior")

    if assessed_type == observed_type:
        assessed_confidence = float(assessed_style.get("confidence") or 0.0)
        observed_confidence = float(observed_style.get("confidence") or 0.0)
        fused_confidence = 1.0 - (1.0 - assessed_confidence) * (1.0 - observed_confidence * 0.7)
        return _effective_style_value(
            observed_style,
            source="fused",
            status="stable" if observed_stable else "calibrated",
            confidence=fused_confidence,
        )

    transition = _style_transition(previous_effective, observed_style)
    if observed_stable and previous_type == observed_type:
        return _effective_style_value(observed_style, source="learning_behavior", status="updated")
    if observed_stable and int(transition["confirmations"]) >= 2:
        return _effective_style_value(observed_style, source="learning_behavior", status="updated")
    if previous_type and previous_type not in {assessed_type, observed_type}:
        return _effective_style_value(
            previous_effective,
            source=str(previous_effective.get("source") or "learning_behavior"),
            status="recalibrating",
            transition=transition,
        )
    return _effective_style_value(
        assessed_style,
        source="assessment",
        status="calibrating",
        transition=transition,
    )


def refresh_compact_profile_from_l3(
    memory: LearningMemory,
    user_id: str,
) -> dict[str, Any]:
    """Refresh the legacy flat user profile from L3 learner memory."""
    existing = memory.get_or_create_profile(user_id)
    l3 = _with_default_slots(memory.get_learner_memory(user_id))
    weak_items = _slot_items(l3, "weaknesses")
    pref_items = _slot_items(l3, "preferences")
    goal_items = _slot_items(l3, "goals")
    rhythm_items = _slot_items(l3, "rhythm")

    weak_points: list[str] = []
    mistake_patterns: list[str] = []
    for item in weak_items[:10]:
        kg_ids = _as_str_list(item.get("kg_node_ids"))
        weak_points.extend(kg_ids or [str(item.get("text", ""))])
        if item.get("text"):
            mistake_patterns.append(str(item["text"]))

    patch: dict[str, Any] = {
        "known_weak_points": weak_points,
        "mistake_patterns": mistake_patterns,
        "confidence": max(float(existing.get("confidence", 0.2)), 0.5 if weak_points or pref_items else 0.3),
    }
    preference = _infer_preferred_explanation(pref_items)
    if preference:
        patch["preferred_explanation"] = preference
    pace = _infer_pace(rhythm_items)
    if pace:
        patch["pace"] = pace
    if goal_items:
        patch["goal"] = str(goal_items[0].get("text", existing.get("goal", "course_learning")))[:80]

    updated = merge_profile(existing, patch)
    return memory.save_profile(user_id, updated)


def _build_style_state(l3: dict[str, Any]) -> dict[str, Any]:
    accumulators = {
        dimension: {
            "options": {
                option: {
                    "raw_score": 0.0,
                    "evidence_ids": [],
                    "signals": [],
                    "last_seen_at": "",
                }
                for option in options
            }
        }
        for dimension, options in STYLE_DIMENSIONS.items()
    }

    for slot in ("profile", "preferences", "goals", "rhythm"):
        for item in _slot_items(l3, slot):
            for dimension, option, strength in _style_signals_from_item(slot, item):
                _add_style_signal(
                    accumulators,
                    dimension=dimension,
                    option=option,
                    item=item,
                    strength=strength,
                )

    result: dict[str, Any] = {}
    for dimension, options in STYLE_DIMENSIONS.items():
        result[dimension] = _finish_style_dimension(
            dimension,
            accumulators[dimension]["options"],
            options,
        )
    return result


def _style_signals_from_item(
    slot: str,
    item: dict[str, Any],
) -> list[tuple[str, str, float]]:
    text = " ".join(
        str(item.get(key, ""))
        for key in ("slot", "aspect", "label", "text")
    ).lower()
    signals: list[tuple[str, str, float]] = []
    for dimension, option, tokens, strength in STYLE_TOKEN_RULES:
        if any(token in text for token in tokens):
            adjusted = strength
            if slot == "profile":
                adjusted *= 1.05
            elif slot == "preferences":
                adjusted *= 1.0
            elif slot == "rhythm" and dimension == "pace":
                adjusted *= 1.2
            elif slot == "goals" and option == "asks_exam_points":
                adjusted *= 1.1
            signals.append((dimension, option, adjusted))
    return signals


def _add_style_signal(
    accumulators: dict[str, Any],
    *,
    dimension: str,
    option: str,
    item: dict[str, Any],
    strength: float,
) -> None:
    if dimension not in accumulators:
        return
    option_state = accumulators[dimension]["options"].get(option)
    if option_state is None:
        return
    evidence_ids = _item_evidence_ids(item)
    evidence_count = max(1, len(evidence_ids))
    weight = _item_weight(item) * strength * (1.0 + 0.35 * min(evidence_count - 1, 5))
    option_state["raw_score"] += weight
    option_state["evidence_ids"] = _dedupe(option_state["evidence_ids"] + evidence_ids)
    signal_text = _clean_text(item.get("label") or item.get("text"), limit=120)
    if signal_text:
        option_state["signals"] = _dedupe(option_state["signals"] + [signal_text])[:5]
    updated_at = str(item.get("updated_at") or "")
    if updated_at and updated_at > str(option_state.get("last_seen_at") or ""):
        option_state["last_seen_at"] = updated_at


def _finish_style_dimension(
    dimension: str,
    raw_options: dict[str, dict[str, Any]],
    option_order: tuple[str, ...],
) -> dict[str, Any]:
    options: dict[str, dict[str, Any]] = {}
    ranked: list[tuple[str, float]] = []
    for option in option_order:
        raw = raw_options.get(option, {})
        evidence_ids = _dedupe(_as_str_list(raw.get("evidence_ids")))
        raw_score = float(raw.get("raw_score", 0.0))
        score = _clamp_float(raw_score / 1.5, 0.0, 1.0, 0.0)
        evidence_count = len(evidence_ids)
        confidence = _style_confidence(score, evidence_count)
        status = _style_status(
            score=score,
            evidence_count=evidence_count,
            last_seen_at=str(raw.get("last_seen_at") or ""),
        )
        options[option] = {
            "score": round(score, 3),
            "confidence": round(confidence, 3),
            "evidence_count": evidence_count,
            "status": status,
            "evidence_ids": evidence_ids[:12],
            "last_seen_at": raw.get("last_seen_at", ""),
            "signals": _as_str_list(raw.get("signals"))[:5],
        }
        ranked.append((option, score))

    if dimension == "pace" and all(score == 0.0 for _, score in ranked):
        options["standard"] = {
            **options.get("standard", {}),
            "score": 0.5,
            "confidence": 0.2,
            "evidence_count": 0,
            "status": "default",
            "evidence_ids": [],
            "last_seen_at": "",
            "signals": [],
        }
        ranked = [(option, float(options[option]["score"])) for option in option_order]

    ranked.sort(key=lambda item: item[1], reverse=True)
    dominant = ranked[0][0] if ranked and ranked[0][1] > 0 else ""
    top_score = ranked[0][1] if ranked else 0.0
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0
    dimension_status = options.get(dominant, {}).get("status", "unobserved") if dominant else "unobserved"
    if dominant and second_score > 0 and top_score - second_score < 0.15:
        dimension_status = "mixed"

    return {
        "dominant": dominant,
        "status": dimension_status,
        "confidence": options.get(dominant, {}).get("confidence", 0.0) if dominant else 0.0,
        "options": options,
    }


def _style_confidence(score: float, evidence_count: int) -> float:
    if evidence_count <= 0:
        return 0.0
    confidence = 0.18 + min(evidence_count, 5) * 0.13 + score * 0.38
    if evidence_count == 1:
        confidence = min(confidence, 0.49)
    return _clamp_float(confidence, 0.0, 0.95, 0.0)


def _style_status(
    *,
    score: float,
    evidence_count: int,
    last_seen_at: str,
) -> str:
    if evidence_count <= 0 or score <= 0:
        return "unobserved"
    if _is_stale_timestamp(last_seen_at):
        return "stale"
    if evidence_count == 1:
        return "observed"
    if evidence_count < 4:
        return "emerging"
    return "stable" if score >= 0.65 else "emerging"


def _build_weakness_summary(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for item in items[:8]:
        severity = _clamp_float(
            item.get("severity", item.get("confidence", 0.5)),
            0.0,
            1.0,
            0.5,
        )
        if severity < 0.25:
            status = "resolved"
        elif severity < 0.55:
            status = "improving"
        else:
            status = "active"
        summary.append({
            "id": item.get("id", ""),
            "label": item.get("label") or item.get("aspect") or "weakness",
            "text": _clean_text(item.get("text"), limit=160),
            "kg_node_ids": _as_str_list(item.get("kg_node_ids"))[:8],
            "severity": round(severity, 3),
            "confidence": _clamp_float(item.get("confidence"), 0.0, 1.0, severity),
            "status": status,
            "evidence_count": len(_item_evidence_ids(item)),
            "evidence_ids": _item_evidence_ids(item)[:12],
            "last_seen_at": item.get("updated_at", ""),
        })
    return summary


def _build_mastery_summary(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for item in items[:12]:
        score = _clamp_float(item.get("score", item.get("confidence", 0.5)), 0.0, 1.0, 0.5)
        if score >= 0.75:
            trend = "mastered"
        elif score >= 0.55:
            trend = "improving"
        else:
            trend = "needs_review"
        summary.append({
            "id": item.get("id", ""),
            "label": item.get("label") or item.get("aspect") or "concept_mastery",
            "text": _clean_text(item.get("text"), limit=140),
            "kg_node_ids": _as_str_list(item.get("kg_node_ids"))[:8],
            "mastery": round(score, 3),
            "confidence": _clamp_float(item.get("confidence"), 0.0, 1.0, score),
            "trend": trend,
            "evidence_count": len(_item_evidence_ids(item)),
            "evidence_ids": _item_evidence_ids(item)[:12],
            "last_seen_at": item.get("updated_at", ""),
        })
    return summary


def _build_profile_state_summary(
    style: dict[str, Any],
    weakness_summary: list[dict[str, Any]],
    mastery_summary: list[dict[str, Any]],
) -> tuple[str, str, float]:
    parts: list[str] = []
    confidence_values: list[float] = []
    for dimension, data in style.items():
        dominant = str(data.get("dominant") or "")
        status = str(data.get("status") or "")
        confidence = _clamp_float(data.get("confidence"), 0.0, 1.0, 0.0)
        if dominant and status not in {"unobserved", "default"}:
            confidence_values.append(confidence)
            parts.append(_style_summary_phrase(dimension, dominant, status))

    active_weaknesses = [w for w in weakness_summary if w.get("status") == "active"]
    if active_weaknesses:
        label = str(active_weaknesses[0].get("label") or active_weaknesses[0].get("text") or "")
        if label:
            parts.append(f"当前主要薄弱点是{label}")
        confidence_values.append(float(active_weaknesses[0].get("confidence", 0.5)))

    if mastery_summary:
        confidence_values.append(float(mastery_summary[0].get("confidence", 0.5)))

    confidence = (
        sum(confidence_values) / len(confidence_values)
        if confidence_values
        else 0.0
    )
    confidence = round(_clamp_float(confidence, 0.0, 1.0, 0.0), 3)
    if confidence >= 0.75:
        policy = "strong"
    elif confidence >= 0.55:
        policy = "moderate"
    else:
        policy = "light"

    if not parts:
        return "暂无足够稳定的画像证据，先采用标准讲解策略。", policy, confidence
    return "；".join(parts[:4]) + "。", policy, confidence


def _style_summary_phrase(dimension: str, option: str, status: str) -> str:
    status_text = {
        "observed": "观察到",
        "emerging": "逐渐显现",
        "stable": "较稳定",
        "mixed": "呈混合倾向",
        "stale": "较早观察到",
    }.get(status, "观察到")
    labels = {
        ("explanation_depth", "mechanism_deep"): "机制深度解释偏好",
        ("explanation_depth", "concise"): "简洁要点式讲解偏好",
        ("explanation_depth", "standard"): "标准深度讲解偏好",
        ("entry_mode", "concept_first"): "概念优先进入方式",
        ("entry_mode", "case_first"): "病例或临床例子优先进入方式",
        ("entry_mode", "question_first"): "问题驱动进入方式",
        ("entry_mode", "practice_first"): "练习驱动进入方式",
        ("interaction_tendency", "asks_why"): "通过追问原因校准理解",
        ("interaction_tendency", "asks_examples"): "偏好用例子校准理解",
        ("interaction_tendency", "asks_exam_points"): "关注考点和记忆策略",
        ("interaction_tendency", "passive"): "交互较被动",
        ("pace", "slow"): "偏慢节奏",
        ("pace", "standard"): "标准节奏",
        ("pace", "fast"): "偏快节奏",
    }
    return f"{status_text}{labels.get((dimension, option), option)}"


def _profile_state_evidence_ids(
    style: dict[str, Any],
    weakness_summary: list[dict[str, Any]],
    mastery_summary: list[dict[str, Any]],
) -> list[str]:
    evidence: list[str] = []
    for dimension in style.values():
        for option in (dimension.get("options") or {}).values():
            if isinstance(option, dict):
                evidence.extend(_as_str_list(option.get("evidence_ids")))
    for item in weakness_summary + mastery_summary:
        evidence.extend(_as_str_list(item.get("evidence_ids")))
    return _dedupe(evidence)


def _item_evidence_ids(item: dict[str, Any]) -> list[str]:
    return _dedupe(
        _as_str_list(item.get("evidence_ids"))
        + _as_str_list(item.get("evidence_event_ids"))
    )


def _item_weight(item: dict[str, Any]) -> float:
    confidence = _clamp_float(item.get("confidence"), 0.0, 1.0, 0.5)
    return confidence * _recency_weight(str(item.get("updated_at") or ""))


def _recency_weight(updated_at: str) -> float:
    parsed = _parse_iso_datetime(updated_at)
    if parsed is None:
        return 0.85
    age_days = max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds() / 86400)
    return _clamp_float(0.5 ** (age_days / 90.0), 0.25, 1.0, 0.85)


def _is_stale_timestamp(updated_at: str) -> bool:
    parsed = _parse_iso_datetime(updated_at)
    if parsed is None:
        return False
    age_days = (datetime.now(timezone.utc) - parsed).total_seconds() / 86400
    return age_days > 120


def _parse_iso_datetime(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def build_profile_slice(
    memory: LearningMemory,
    *,
    user_id: str,
    role: str,
    context: dict[str, Any] | None = None,
    limit: int = 6,
) -> dict[str, Any]:
    """Build a small role-specific learner-memory slice for an agent."""
    context = context or {}
    kg_node_ids = set(_as_str_list(context.get("kg_node_ids")))
    l3 = _with_default_slots(memory.get_learner_memory(user_id))
    compact = memory.get_or_create_profile(user_id)
    profile_state = _compact_profile_state_for_prompt(
        l3.get(PROFILE_STATE_SLOT, {})
    )

    def related(slot: str) -> list[dict[str, Any]]:
        items = _slot_items(l3, slot)
        if kg_node_ids and slot in {"mastery", "weaknesses"}:
            matched = [
                item for item in items
                if kg_node_ids.intersection(_as_str_list(item.get("kg_node_ids")))
            ]
            if matched:
                return matched[:limit]
        return items[:limit]

    role = role.lower().strip()
    base = {
        "role": role,
        "compact_profile": compact,
        "profile_state": profile_state,
        "profile_state_summary": profile_state.get("summary", ""),
        "context": {
            "topic": context.get("topic", ""),
            "outline_node_id": context.get("outline_node_id", ""),
            "kg_node_ids": sorted(kg_node_ids),
        },
    }

    if role == "planner":
        base.update({
            "profile": related("profile"),
            "goals": related("goals"),
            "rhythm": related("rhythm"),
            "weaknesses": related("weaknesses"),
            "mastery": related("mastery"),
            "recent_evidence": _profile_slice_evidence(
                related("goals") + related("rhythm") + related("weaknesses") + related("mastery"),
                limit=3,
            ),
        })
    elif role == "tutor":
        base.update({
            "profile": related("profile"),
            "preferences": related("preferences"),
            "rhythm": related("rhythm"),
            "weaknesses": related("weaknesses"),
            "mastery": related("mastery"),
            "recent_evidence": _profile_slice_evidence(
                related("preferences") + related("rhythm") + related("weaknesses") + related("mastery"),
                limit=3,
            ),
        })
    elif role == "qa":
        base.update({
            "profile": related("profile"),
            "preferences": related("preferences"),
            "rhythm": related("rhythm"),
            "weaknesses": related("weaknesses"),
            "mastery": related("mastery"),
            "recent_evidence": _profile_slice_evidence(
                related("preferences") + related("weaknesses") + related("mastery"),
                limit=3,
            ),
        })
    elif role == "assessment":
        base.update({
            "profile": related("profile"),
            "weaknesses": related("weaknesses"),
            "mastery": related("mastery"),
            "rhythm": related("rhythm"),
            "recent_evidence": _profile_slice_evidence(
                related("weaknesses") + related("mastery") + related("rhythm"),
                limit=3,
            ),
        })
    else:
        base.update({slot: related(slot) for slot in SIGNAL_MEMORY_SLOTS})
        base["recent_evidence"] = _profile_slice_evidence(
            [item for slot in SIGNAL_MEMORY_SLOTS for item in related(slot)],
            limit=3,
        )

    session_id = context.get("session_id")
    if session_id:
        base["recent_summaries"] = memory.list_learning_summaries(
            user_id,
            session_id=str(session_id),
            limit=limit,
        )
    return base


def _consolidate_l2_surface(
    memory: LearningMemory,
    *,
    user_id: str,
    session_id: str | None,
    surface: str,
    limit: int,
) -> dict[str, Any]:
    event_types = list(SURFACE_EVENT_TYPES.get(surface, ()))
    key = _meta_key("l2", surface, session_id)
    seen = memory.get_consolidation_seen_ids(user_id, "L2", key)
    events = memory.list_events(
        user_id,
        session_id=session_id,
        event_types=event_types or None,
        limit=limit,
    )
    events = [event for event in events if _event_matches_surface(event, surface)]
    new_events = [event for event in events if event.get("id") not in seen]
    if not new_events:
        return {
            "surface": surface,
            "summary": None,
            "event_count": 0,
            "no_new_input": True,
        }

    summary_payload = _summarize_events_for_l2(surface, new_events)
    summary = memory.append_learning_summary(
        user_id=user_id,
        session_id=session_id,
        surface=f"{surface}_summary",
        summary=summary_payload,
        source_event_ids=[str(event["id"]) for event in new_events],
    )
    memory.save_consolidation_seen_ids(
        user_id,
        "L2",
        key,
        seen | {str(event["id"]) for event in new_events if event.get("id")},
    )
    return {
        "surface": surface,
        "summary": summary,
        "event_count": len(new_events),
        "trace": summary_payload.get("trace", {}),
    }


def _summarize_events_for_l2(
    surface: str,
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    if is_learning_memory_llm_configured():
        messages = _build_l2_summary_messages(surface, events)
        try:
            raw = chat_completion(
                messages,
                temperature=0.15,
                max_tokens=1400,
                api_key_names=_MEMORY_API_KEY_NAMES,
                base_url_names=_MEMORY_BASE_URL_NAMES,
                model_names=_MEMORY_MODEL_NAMES,
                response_format={"type": "json_object"},
            )
            parsed = _extract_json(raw)
            return _normalize_l2_summary(
                surface,
                events,
                parsed,
                trace_status="llm_summarized",
            )
        except Exception as exc:  # noqa: BLE001 - safe fallback
            summary = _rule_l2_summary(surface, events)
            summary["trace"] = {
                "mode": "l2_surface_summarizer",
                "status": "fallback_exception",
                "uses_llm": True,
                "error": str(exc)[:240],
            }
            return summary
    summary = _rule_l2_summary(surface, events)
    summary["trace"] = {
        "mode": "l2_surface_summarizer",
        "status": "fallback_no_llm",
        "uses_llm": False,
    }
    return summary


def _build_signal_extractor_messages(
    *,
    event: dict[str, Any],
    surface: str,
    recent_events: list[dict[str, Any]],
    learner_memory: dict[str, Any],
    compact_profile: dict[str, Any],
    context: dict[str, Any],
) -> list[dict[str, str]]:
    system = (
        "你是 OralSage 的学习记忆信号抽取器。你的任务是阅读一个学习事件，"
        "判断它是否应该更新用户长期画像。你只输出 JSON，不要写解释性散文。\n\n"
        "原则：\n"
        "1. LLM 只提出候选信号，不直接改画像。\n"
        "2. 不要把普通好奇提问都当成不会；只有事件证据显示困惑、错因、偏好、目标或节奏变化时才更新。\n"
        "3. 每条信号必须引用 evidence_event_ids，优先引用当前事件 id。\n"
        "4. 字段要少，content 用一句话描述稳定可复用的信息。\n\n"
        "5. 如果事件揭示学习风格倾向，将它写入 slot=profile, "
        "aspect=learning_style_tendency，并给出简短 label，例如“概念后接病例型学习者”。\n\n"
        "输出格式：\n"
        "{\n"
        '  "should_update": true,\n'
        '  "signals": [\n'
        "    {\n"
        '      "slot": "profile | mastery | weaknesses | preferences | goals | rhythm",\n'
        '      "aspect": "learning_style_tendency | concept_mastery | misconception | explanation_preference | learning_goal | pace_preference | ...",\n'
        '      "label": "可选的短标签",\n'
        '      "action": "add | update | reinforce | weaken | ignore",\n'
        '      "content": "一句话画像信号",\n'
        '      "kg_node_ids": ["kg://..."],\n'
        '      "confidence": 0.0,\n'
        '      "evidence_event_ids": ["evt_xxx"],\n'
        '      "score_delta": 0.0,\n'
        '      "severity_delta": 0.0,\n'
        '      "reason": "为什么这个事件支持该信号"\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "slot 说明：profile=用户是谁；mastery=知识点掌握度；weaknesses=易错点与错因；"
        "preferences=学习偏好；goals=目标导向；rhythm=学习节奏。\n"
        "常用 aspect：profile.learning_style_tendency；mastery.concept_mastery；"
        "weaknesses.misconception/error_cause/repeated_confusion；"
        "preferences.explanation_preference/modality_preference；"
        "goals.learning_goal/exam_goal；rhythm.pace_preference/review_need。"
    )
    payload = {
        "current_event": _compact_event(event),
        "surface": surface,
        "recent_events": [_compact_event(e) for e in recent_events[-6:]],
        "compact_profile": compact_profile,
        "learner_memory_snapshot": _compact_l3_for_prompt(learner_memory),
        "context": context,
    }
    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False, indent=2)
            + "\n\n请只返回 JSON 对象。",
        },
    ]


def _build_l2_summary_messages(
    surface: str,
    events: list[dict[str, Any]],
) -> list[dict[str, str]]:
    system = (
        "You are OralSage's L2 memory summarizer. Read raw learning events "
        "from one surface and produce a compact, evidence-grounded summary. "
        "Return only JSON. Do not invent facts.\n\n"
        "Output schema:\n"
        "{\n"
        '  "summary": "one concise paragraph",\n'
        '  "key_signals": ["durable learning signal", "..."],\n'
        '  "kg_node_ids": ["kg://..."]\n'
        "}\n"
    )
    payload = {
        "surface": surface,
        "events": [_compact_event(event) for event in events],
    }
    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False, indent=2),
        },
    ]


def _build_l3_consolidation_messages(
    *,
    summaries: list[dict[str, Any]],
    learner_memory: dict[str, Any],
    compact_profile: dict[str, Any],
    target_slot: str | None,
) -> list[dict[str, str]]:
    allowed_event_ids = _source_event_ids_from_summaries(summaries)
    slot_instruction = (
        f"Only emit signals for slot={target_slot}."
        if target_slot in SIGNAL_MEMORY_SLOTS
        else "Emit signals for any relevant slot."
    )
    system = (
        "You are OralSage's L3 learner-memory consolidator. Read new L2 "
        "summaries and propose stable learner-profile signals. Return only "
        "JSON. The reducer will validate and write memory.\n\n"
        f"{slot_instruction}\n\n"
        "Rules:\n"
        "1. Use only evidence_event_ids from the allowed list.\n"
        "2. Prefer stable, repeated, or educationally useful conclusions.\n"
        "3. Do not duplicate existing memory unless the signal reinforces it.\n\n"
        "4. Synthesize the durable learning-style conclusion as "
        "slot=profile, aspect=learning_style_tendency when the summaries "
        "support it. Use label for the short display name, such as "
        "“概念后接病例型学习者”, “病例驱动型学习者”, “图解结构型学习者”, "
        "“练习驱动型学习者”, or “推导型学习者”.\n\n"
        "Output schema:\n"
        "{\n"
        '  "should_update": true,\n'
        '  "signals": [\n'
        "    {\n"
        '      "slot": "profile | mastery | weaknesses | preferences | goals | rhythm",\n'
        '      "aspect": "learning_style_tendency | concept_mastery | misconception | explanation_preference | learning_goal | pace_preference | ...",\n'
        '      "label": "optional short display label",\n'
        '      "action": "add | update | reinforce | weaken | ignore",\n'
        '      "content": "one concise memory claim",\n'
        '      "kg_node_ids": ["kg://..."],\n'
        '      "confidence": 0.0,\n'
        '      "evidence_event_ids": ["evt_xxx"],\n'
        '      "score_delta": 0.0,\n'
        '      "severity_delta": 0.0,\n'
        '      "reason": "why the summaries support this signal"\n'
        "    }\n"
        "  ]\n"
        "}\n"
    )
    payload = {
        "allowed_evidence_event_ids": allowed_event_ids,
        "new_l2_summaries": [_compact_summary(summary) for summary in summaries],
        "compact_profile": compact_profile,
        "learner_memory_snapshot": _compact_l3_for_prompt(learner_memory),
    }
    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False, indent=2),
        },
    ]


def _normalize_l2_summary(
    surface: str,
    events: list[dict[str, Any]],
    parsed: dict[str, Any],
    *,
    trace_status: str,
) -> dict[str, Any]:
    summary = _clean_text(parsed.get("summary"), limit=800)
    rule_fallback = _rule_l2_summary(surface, events)
    key_signals = _dedupe(
        _as_str_list(parsed.get("key_signals"))
        + _as_str_list(rule_fallback.get("key_signals"))
    )[:12]
    kg_node_ids = _dedupe(
        _as_str_list(parsed.get("kg_node_ids")) + _kg_node_ids_from_events(events)
    )[:20]
    if not summary:
        summary = rule_fallback["summary"]
    return {
        "surface": surface,
        "summary": summary,
        "key_signals": key_signals,
        "kg_node_ids": kg_node_ids,
        "event_count": len(events),
        "source": "batch_l2",
        "trace": {
            "mode": "l2_surface_summarizer",
            "status": trace_status,
            "uses_llm": trace_status == "llm_summarized",
        },
    }


def _rule_l2_summary(surface: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    key_signals: list[str] = []
    correct = 0
    wrong = 0
    for event in events:
        payload = event.get("payload", {}) or {}
        if payload.get("is_correct") is True:
            correct += 1
        if payload.get("is_correct") is False:
            wrong += 1
        for key in (
            "misconception_summary",
            "question",
            "answer",
            "recommendation_action",
            "handout_section_title",
        ):
            value = _clean_text(payload.get(key), limit=160)
            if value:
                key_signals.append(value)
    key_signals = _dedupe(key_signals)[:12]
    kg_node_ids = _kg_node_ids_from_events(events)
    parts = [f"{surface} surface has {len(events)} new events"]
    if correct or wrong:
        parts.append(f"quiz correctness: {correct} correct, {wrong} wrong")
    if key_signals:
        parts.append("signals: " + "; ".join(key_signals[:3]))
    return {
        "surface": surface,
        "summary": ". ".join(parts),
        "key_signals": key_signals,
        "kg_node_ids": kg_node_ids,
        "event_count": len(events),
        "source": "batch_l2",
        "trace": {
            "mode": "l2_surface_summarizer",
            "status": "rule_summarized",
            "uses_llm": False,
        },
    }


def _rule_summary_signals(
    summaries: list[dict[str, Any]],
    *,
    fallback_event_id: str,
    target_slot: str | None,
    status: str,
) -> dict[str, Any]:
    signals: list[dict[str, Any]] = []
    for summary in summaries:
        payload = summary.get("summary", {}) or {}
        if not isinstance(payload, dict):
            continue
        surface = str(summary.get("surface") or payload.get("surface") or "")
        source_ids = _as_str_list(summary.get("source_event_ids")) or [fallback_event_id]
        kg_node_ids = _as_str_list(payload.get("kg_node_ids"))
        style_signal = _rule_learning_style_signal(
            summary,
            payload,
            source_ids=source_ids,
        )
        if style_signal and (target_slot in (None, "", "profile")):
            signals.append(style_signal)
        for text in _as_str_list(payload.get("key_signals"))[:4]:
            slot = target_slot if target_slot in SIGNAL_MEMORY_SLOTS else _slot_for_surface(surface)
            signals.append({
                "slot": slot,
                "aspect": _default_aspect_for_slot(slot),
                "action": "reinforce",
                "content": text,
                "kg_node_ids": kg_node_ids,
                "confidence": 0.55,
                "evidence_event_ids": source_ids,
                "severity_delta": 0.05 if slot == "weaknesses" else 0.0,
                "score_delta": 0.03 if slot == "mastery" else 0.0,
                "reason": "derived from L2 summary fallback",
            })
    normalized = _normalize_signals(signals, fallback_event_id=fallback_event_id)
    return {
        "should_update": bool(normalized),
        "signals": normalized,
        "trace": {
            "mode": "l3_summary_consolidator",
            "status": status,
            "uses_llm": False,
            "summary_count": len(summaries),
            "valid_signal_count": len(normalized),
        },
    }


def _ensure_learning_style_signal(
    signals: list[dict[str, Any]],
    *,
    summaries: list[dict[str, Any]],
    target_slot: str | None,
    fallback_event_id: str,
) -> list[dict[str, Any]]:
    if target_slot not in (None, "", "profile"):
        return signals
    if any(
        signal.get("slot") == "profile"
        and signal.get("aspect") == "learning_style_tendency"
        for signal in signals
    ):
        return signals
    for summary in summaries:
        payload = summary.get("summary", {}) or {}
        if not isinstance(payload, dict):
            continue
        source_ids = _as_str_list(summary.get("source_event_ids")) or [fallback_event_id]
        style_signal = _rule_learning_style_signal(
            summary,
            payload,
            source_ids=source_ids,
        )
        if style_signal:
            normalized = _normalize_signals([style_signal], fallback_event_id=fallback_event_id)
            return (signals + normalized)[:12]
    return signals


def _rule_learning_style_signal(
    summary: dict[str, Any],
    payload: dict[str, Any],
    *,
    source_ids: list[str],
) -> dict[str, Any] | None:
    text = " ".join(
        [
            str(payload.get("summary", "")),
            " ".join(_as_str_list(payload.get("key_signals"))),
        ]
    )
    label = _infer_learning_style_label(text)
    if not label:
        return None
    return {
        "slot": "profile",
        "aspect": "learning_style_tendency",
        "label": label,
        "action": "reinforce",
        "content": _learning_style_content(label),
        "kg_node_ids": _as_str_list(payload.get("kg_node_ids")),
        "confidence": 0.58,
        "evidence_event_ids": source_ids,
        "source_summary_ids": [str(summary.get("id", ""))] if summary.get("id") else [],
        "reason": "derived from repeated L2 learning-style signals",
    }


def _infer_learning_style_label(text: str) -> str:
    value = text.lower()
    if any(word in value for word in ("图", "表格", "结构", "思维导图", "流程图", "visual", "diagram", "structured")):
        return "图解结构型学习者"
    if any(word in value for word in ("概念后接病例", "先讲概念", "概念", "原理", "机制", "concept", "principle")) and any(
        word in value for word in ("病例", "案例", "临床", "case", "example")
    ):
        return "概念后接病例型学习者"
    if any(word in value for word in ("病例", "案例", "临床", "case", "clinical", "example")):
        return "病例驱动型学习者"
    if any(word in value for word in ("练习", "题目", "答题", "错题", "反复做题", "检索增强", "主动回忆", "practice", "retrieval_enhanced", "active recall")):
        return "练习驱动型学习者"
    if any(word in value for word in ("详细", "推导", "拆解", "一步步", "为什么", "机制", "原理", "detailed", "step", "reasoning", "why", "mechanism", "principle")):
        return "推导型学习者"
    if any(word in value for word in ("简洁", "要点", "直接", "concise")):
        return "要点型学习者"
    return ""


def _learning_style_content(label: str) -> str:
    contents = {
        "图解结构型学习者": "用户更适合通过结构图、流程图或可视化框架组织知识。",
        "概念后接病例型学习者": "用户更适合先理解概念框架，再通过病例或案例验证理解。",
        "病例驱动型学习者": "用户更适合从病例或临床场景进入知识点学习。",
        "练习驱动型学习者": "用户更适合通过做题、反馈和错题复盘推动掌握。",
        "推导型学习者": "用户更适合分步骤拆解机制和推理过程。",
        "要点型学习者": "用户更适合简洁要点式讲解，再补充必要例子。",
    }
    return contents.get(label, label)


def _apply_signal_to_items(items: list[dict[str, Any]], signal: dict[str, Any]) -> dict[str, Any]:
    now = _now()
    existing = _find_similar_item(items, signal)
    slot = signal["slot"]
    if existing is None:
        entry = {
            "id": _new_mem_id(slot),
            "slot": slot,
            "aspect": signal.get("aspect", ""),
            "label": signal.get("label", ""),
            "text": signal["text"][:240],
            "kg_node_ids": signal["kg_node_ids"][:12],
            "confidence": signal["confidence"],
            "evidence_ids": signal["evidence_ids"][:20],
            "source_summary_ids": signal.get("source_summary_ids", [])[:20],
            "updated_at": now,
        }
        if slot == "weaknesses":
            entry["severity"] = _clamp_float(
                signal["confidence"] + signal["severity_delta"],
                0.0,
                1.0,
                signal["confidence"],
            )
        elif slot == "mastery":
            entry["score"] = _clamp_float(
                0.5 + signal["score_delta"],
                0.0,
                1.0,
                0.5,
            )
        items.append(entry)
        return entry

    if signal["action"] == "update" and len(signal["text"]) >= len(str(existing.get("text", ""))):
        existing["text"] = signal["text"][:240]
    if not existing.get("slot"):
        existing["slot"] = slot
    if signal.get("aspect"):
        existing["aspect"] = signal["aspect"]
    if signal.get("label"):
        existing["label"] = signal["label"]
    existing["kg_node_ids"] = _dedupe(_as_str_list(existing.get("kg_node_ids")) + signal["kg_node_ids"])[:12]
    existing["evidence_ids"] = _dedupe(_as_str_list(existing.get("evidence_ids")) + signal["evidence_ids"])[:20]
    existing["source_summary_ids"] = _dedupe(
        _as_str_list(existing.get("source_summary_ids")) + signal.get("source_summary_ids", [])
    )[:20]
    existing["confidence"] = _clamp_float(
        _merge_confidence(
            float(existing.get("confidence", 0.0)),
            signal["confidence"],
            action=signal["action"],
        ),
        0.0,
        1.0,
        signal["confidence"],
    )
    if slot == "weaknesses":
        base = float(existing.get("severity", existing["confidence"]))
        delta = signal["severity_delta"]
        if delta == 0.0:
            delta = -0.08 if signal["action"] == "weaken" else 0.08
        existing["severity"] = _clamp_float(base + delta, 0.0, 1.0, base)
    elif slot == "mastery":
        base = float(existing.get("score", 0.5))
        delta = signal["score_delta"]
        if delta == 0.0:
            delta = -0.05 if signal["action"] == "weaken" else 0.05
        existing["score"] = _clamp_float(base + delta, 0.0, 1.0, base)
    existing["updated_at"] = now
    return existing


def _append_l2_signal_summary(
    memory: LearningMemory,
    *,
    user_id: str,
    session_id: str,
    surface: str,
    event_id: str,
    applied: list[dict[str, Any]],
) -> dict[str, Any]:
    texts = [a["text"] for a in applied if a.get("text")]
    slots = sorted({a["slot"] for a in applied if a.get("slot")})
    summary = {
        "surface": surface,
        "summary": "；".join(texts[:4]),
        "key_signals": texts[:8],
        "slots": slots,
        "source": "realtime_event_signal",
    }
    return memory.append_learning_summary(
        user_id=user_id,
        session_id=session_id,
        surface=surface,
        summary=summary,
        source_event_ids=[event_id],
    )


def _find_similar_item(
    items: list[dict[str, Any]],
    signal: dict[str, Any],
) -> dict[str, Any] | None:
    text = signal["text"]
    kg_ids = set(signal["kg_node_ids"])
    slot = signal.get("slot", "")
    aspect = signal.get("aspect", "")
    if (slot, aspect) in SINGLETON_ASPECTS:
        for item in items:
            if isinstance(item, dict) and item.get("aspect") == aspect:
                return item
    norm = _norm_text(text)
    best: tuple[float, dict[str, Any] | None] = (0.0, None)
    for item in items:
        if not isinstance(item, dict):
            continue
        item_kg = set(_as_str_list(item.get("kg_node_ids")))
        if kg_ids and item_kg and kg_ids.intersection(item_kg):
            return item
        item_norm = _norm_text(item.get("text", ""))
        if norm and item_norm and norm == item_norm:
            return item
        sim = _char_similarity(norm, item_norm)
        if sim > best[0]:
            best = (sim, item)
    return best[1] if best[0] >= 0.72 else None


def _trim_items(items: list[dict[str, Any]], *, slot: str) -> list[dict[str, Any]]:
    key = "severity" if slot == "weaknesses" else "score" if slot == "mastery" else "confidence"
    items.sort(
        key=lambda item: (
            float(item.get(key, item.get("confidence", 0.0))),
            str(item.get("updated_at", "")),
        ),
        reverse=True,
    )
    return items[:80]


def _normalize_label(slot: str, aspect: str, value: Any, content: Any = "") -> str:
    label = _clean_text(value, limit=80)
    if slot == "profile" and aspect == "learning_style_tendency":
        mapped = _learning_style_label_from_token(label)
        if mapped:
            return mapped
        inferred = _infer_learning_style_label(" ".join([label, str(content or "")]))
        if inferred:
            return inferred
    return label


def _learning_style_label_from_token(label: str) -> str:
    token = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    mapping = {
        "concept_first_then_cases": "概念后接病例型学习者",
        "concept_then_case": "概念后接病例型学习者",
        "concept_then_cases": "概念后接病例型学习者",
        "concept_first_case": "概念后接病例型学习者",
        "concept_to_case": "概念后接病例型学习者",
        "case_driven": "病例驱动型学习者",
        "clinical_case_driven": "病例驱动型学习者",
        "case_based": "病例驱动型学习者",
        "visual": "图解结构型学习者",
        "visual_structured": "图解结构型学习者",
        "diagram_structured": "图解结构型学习者",
        "structured_visual": "图解结构型学习者",
        "practice_driven": "练习驱动型学习者",
        "quiz_driven": "练习驱动型学习者",
        "exercise_driven": "练习驱动型学习者",
        "retrieval_enhanced": "练习驱动型学习者",
        "retrieval_enhanced_learner": "练习驱动型学习者",
        "active_recall": "练习驱动型学习者",
        "active_recall_learner": "练习驱动型学习者",
        "reasoning": "推导型学习者",
        "stepwise_reasoning": "推导型学习者",
        "deductive": "推导型学习者",
        "concise": "要点型学习者",
        "key_points": "要点型学习者",
    }
    return mapping.get(token, "")


def _normalize_aspect(slot: str, value: Any) -> str:
    aspect = str(value or "").strip()
    allowed = MEMORY_ASPECTS.get(slot, ())
    if aspect in allowed:
        return aspect
    return _default_aspect_for_slot(slot)


def _default_aspect_for_slot(slot: str) -> str:
    if slot == "profile":
        return "general_trait"
    if slot == "mastery":
        return "concept_mastery"
    if slot == "weaknesses":
        return "misconception"
    if slot == "preferences":
        return "explanation_preference"
    if slot == "goals":
        return "learning_goal"
    if slot == "rhythm":
        return "pace_preference"
    return ""


def _merge_confidence(existing: float, incoming: float, *, action: str) -> float:
    if action == "weaken":
        return max(0.0, existing - max(0.04, incoming * 0.12))
    if action == "update":
        return max(existing * 0.85, incoming)
    gain = max(0.03, incoming * 0.12)
    return existing + (1.0 - existing) * gain


def _valid_evidence_ids(
    memory: LearningMemory,
    *,
    user_id: str,
    event_id: str,
    evidence_ids: list[str],
) -> list[str]:
    ids = evidence_ids or [event_id]
    valid = [eid for eid in _dedupe(ids) if memory.event_exists(user_id, eid)]
    if not valid and memory.event_exists(user_id, event_id):
        return [event_id]
    return valid


def _normalize_signals(value: Any, *, fallback_event_id: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    signals: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        slot = str(item.get("slot", "")).strip()
        action = str(item.get("action", "add")).strip()
        content = _clean_text(item.get("content") or item.get("text"))
        if slot not in SIGNAL_MEMORY_SLOTS or action not in SIGNAL_ACTIONS or not content:
            continue
        aspect = _normalize_aspect(slot, item.get("aspect"))
        item["slot"] = slot
        item["action"] = action
        item["aspect"] = aspect
        item["label"] = _normalize_label(slot, aspect, item.get("label"), content)
        item["content"] = content
        item["evidence_event_ids"] = _as_str_list(item.get("evidence_event_ids")) or [fallback_event_id]
        item["source_summary_ids"] = _as_str_list(item.get("source_summary_ids"))[:20]
        signals.append(item)
    return signals[:12]


def _compact_event(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload", {}) or {}
    if not isinstance(payload, dict):
        payload = {}
    keep_payload = {
        key: payload.get(key)
        for key in (
            "surface",
            "topic",
            "strategy",
            "node_count",
            "resource_count",
            "question",
            "answer",
            "is_correct",
            "score",
            "mastery_delta",
            "question_scope",
            "evidence_status",
            "related_kg_node_ids",
            "confusion_kg_node_ids",
            "should_reinforce",
            "misconception_summary",
            "recommendation_action",
            "content_status",
            "user_answer",
            "question_id",
            "question_stem",
            "kg_node_ids",
            "mistake_tags",
            "remedial_suggestion",
            "handout_section_title",
            "current_outline_node_id_before",
            "current_outline_node_id_after",
            "action",
            "advanced",
            "lesson_completed",
            "plan_item_id",
            "plan_date",
            "title",
            "status",
            "linked_course_title",
            "completed_at",
            "deferred_to",
        )
        if key in payload
    }
    return {
        "id": event.get("id"),
        "event_type": event.get("event_type"),
        "outline_node_id": event.get("outline_node_id"),
        "created_at": event.get("created_at"),
        "payload": keep_payload,
    }


def _compact_summary(summary: dict[str, Any]) -> dict[str, Any]:
    payload = summary.get("summary", {}) or {}
    if not isinstance(payload, dict):
        payload = {}
    return {
        "id": summary.get("id"),
        "surface": summary.get("surface"),
        "source_event_ids": summary.get("source_event_ids", []),
        "summary": payload.get("summary", ""),
        "key_signals": payload.get("key_signals", []),
        "kg_node_ids": payload.get("kg_node_ids", []),
    }


def _compact_l3_for_prompt(learner_memory: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for slot in SIGNAL_MEMORY_SLOTS:
        result[slot] = _slot_items(learner_memory, slot)[:5]
    result[PROFILE_STATE_SLOT] = _compact_profile_state_for_prompt(
        learner_memory.get(PROFILE_STATE_SLOT, {})
    )
    return result


def _compact_profile_state_for_prompt(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or "style" not in value:
        return _default_profile_state()
    style: dict[str, Any] = {}
    for dimension, data in (value.get("style") or {}).items():
        if not isinstance(data, dict):
            continue
        dominant = str(data.get("dominant") or "")
        options = data.get("options") if isinstance(data.get("options"), dict) else {}
        dominant_data = options.get(dominant, {}) if dominant else {}
        style[dimension] = {
            "dominant": dominant,
            "status": data.get("status", ""),
            "confidence": data.get("confidence", 0.0),
            "dominant_option": {
                "score": dominant_data.get("score", 0.0) if isinstance(dominant_data, dict) else 0.0,
                "evidence_count": dominant_data.get("evidence_count", 0) if isinstance(dominant_data, dict) else 0,
                "status": dominant_data.get("status", "") if isinstance(dominant_data, dict) else "",
            },
        }
    return {
        "schema_version": value.get("schema_version", 1),
        "summary": value.get("summary", ""),
        "confidence": value.get("confidence", 0.0),
        "personalization_policy": value.get("personalization_policy", "light"),
        "style": style,
        "weakness_summary": (value.get("weakness_summary") or [])[:3],
        "mastery_summary": (value.get("mastery_summary") or [])[:5],
        "evidence_ids": _as_str_list(value.get("evidence_ids"))[:12],
        "updated_at": value.get("updated_at", ""),
    }


def _default_profile_state() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "style_fusion_version": "v1",
        "slot": PROFILE_STATE_SLOT,
        "style": {},
        "assessed_style": _empty_style_profile("assessment"),
        "observed_style": {**_empty_style_profile("learning_behavior"), "candidates": []},
        "effective_style": _empty_style_profile("none"),
        "weakness_summary": [],
        "mastery_summary": [],
        "summary": "暂无足够稳定的画像证据，先采用标准讲解策略。",
        "confidence": 0.0,
        "personalization_policy": "light",
        "evidence_ids": [],
        "updated_at": "",
    }


def _profile_slice_evidence(
    items: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in sorted(
        [i for i in items if isinstance(i, dict)],
        key=lambda i: str(i.get("updated_at", "")),
        reverse=True,
    ):
        key = str(item.get("id") or item.get("text") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append({
            "slot": item.get("slot", ""),
            "aspect": item.get("aspect", ""),
            "label": item.get("label", ""),
            "text": _clean_text(item.get("text"), limit=160),
            "confidence": item.get("confidence", 0.0),
            "kg_node_ids": _as_str_list(item.get("kg_node_ids"))[:6],
            "evidence_ids": _item_evidence_ids(item)[:8],
            "updated_at": item.get("updated_at", ""),
        })
        if len(deduped) >= limit:
            break
    return deduped


def _event_matches_surface(event: dict[str, Any], surface: str) -> bool:
    payload = event.get("payload", {}) or {}
    if isinstance(payload, dict) and payload.get("surface") == surface:
        return True
    return str(event.get("event_type", "")) in SURFACE_EVENT_TYPES.get(surface, ())


def _kg_node_ids_from_events(events: list[dict[str, Any]]) -> list[str]:
    kg_ids: list[str] = []
    for event in events:
        payload = event.get("payload", {}) or {}
        if not isinstance(payload, dict):
            continue
        kg_ids.extend(_as_str_list(payload.get("kg_node_ids")))
        kg_ids.extend(_as_str_list(payload.get("related_kg_node_ids")))
        kg_ids.extend(_as_str_list(payload.get("confusion_kg_node_ids")))
    return _dedupe(kg_ids)


def _source_event_ids_from_summaries(summaries: list[dict[str, Any]]) -> list[str]:
    event_ids: list[str] = []
    for summary in summaries:
        event_ids.extend(_as_str_list(summary.get("source_event_ids")))
    return _dedupe(event_ids)


def _slot_for_surface(surface: str) -> str:
    if surface.startswith("quiz") or surface.startswith("qa"):
        return "weaknesses"
    if surface.startswith("lecture"):
        return "mastery"
    if surface.startswith("planning"):
        return "goals"
    if surface.startswith("reading"):
        return "preferences"
    if surface.startswith("review"):
        return "rhythm"
    if surface.startswith("study_plan"):
        return "rhythm"
    return "profile"


def _meta_key(layer: str, key: str, session_id: str | None) -> str:
    scope = session_id or "global"
    return f"{scope}:{layer}:{key}"


def _with_default_slots(memory: dict[str, Any]) -> dict[str, Any]:
    result = dict(memory or {})
    for slot in SIGNAL_MEMORY_SLOTS:
        result.setdefault(slot, {"items": []})
    result.setdefault(PROFILE_STATE_SLOT, _default_profile_state())
    return result


def _slot_items(memory: dict[str, Any], slot: str) -> list[dict[str, Any]]:
    data = memory.get(slot, {})
    if not isinstance(data, dict):
        return []
    items = data.get("items", [])
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _infer_preferred_explanation(items: list[dict[str, Any]]) -> str | None:
    text = " ".join(str(item.get("text", "")) for item in items[:5])
    if any(word in text for word in ("病例", "案例", "临床")):
        return "case_first"
    if any(word in text for word in ("图", "表格", "结构")):
        return "concept_then_case"
    if any(word in text for word in ("概念", "原理", "机制")):
        return "concept_then_case"
    if any(word in text for word in ("简洁", "短", "直接", "重点", "考点")):
        return "exam_point_first"
    if any(word in text for word in ("详细", "展开", "一步步")):
        return "concept_then_case"
    return None


def _infer_pace(items: list[dict[str, Any]]) -> str | None:
    text = " ".join(str(item.get("text", "")) for item in items[:5])
    if any(word in text for word in ("慢", "放慢", "太快")):
        return "slow"
    if any(word in text for word in ("快", "加快", "太慢")):
        return "fast"
    return None


def _extract_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1:]
        if text.endswith("```"):
            text = text[:-3]
    parsed = json.loads(text.strip())
    if not isinstance(parsed, dict):
        raise ValueError("expected JSON object")
    return parsed


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def _clean_text(value: Any, limit: int = 240) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _clamp_float(value: Any, low: float, high: float, default: float) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        num = default
    return max(low, min(high, num))


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        item = str(value).strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _norm_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").lower())


def _char_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    return len(sa & sb) / max(1, len(sa | sb))


def _new_mem_id(slot: str) -> str:
    prefix = {
        "profile": "prof",
        "mastery": "mas",
        "weaknesses": "weak",
        "preferences": "pref",
        "goals": "goal",
        "rhythm": "rhy",
    }.get(slot, "mem")
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Weakness evidence overview — unified, LLM-free, testable
# ---------------------------------------------------------------------------


def get_weakness_overview(
    memory: LearningMemory,
    user_id: str,
) -> dict[str, Any]:
    """Return a unified weakness-evidence overview from real learner data.

    This is a **pure, deterministic reader** — no LLM, no writes, no side
    effects.  It consumes only existing :class:`LearningMemory` read APIs
    and returns a structured overview suitable for:

    * Displaying to the user ("what are my weaknesses?")
    * Feeding into the study-plan orchestration context as scope candidates

    Evidence sources (in priority order):

    1. **Learner-memory weakness items** — ``weaknesses`` slot items with
       non-empty ``kg_node_ids``, ``label``, and ``text``.
    2. **Low-mastery scores** — mastery items whose ``score`` is below 0.62
       and which carry ``kg_node_ids``.
    3. **Profile-state summaries** — ``profile_state.weakness_summary`` and
       ``profile_state.mastery_summary`` entries with non-empty
       ``kg_node_ids``.

    Each entry carries a stable ``evidence_source`` marker
    (``"learner_memory_weakness"``, ``"low_mastery"``, or
    ``"profile_state"``) so consumers can trace provenance.

    Returns
    -------
    dict
        Keys:

        * ``weaknesses`` — list of deduplicated weakness entry dicts, each
          with ``label``, ``kg_node_ids``, ``evidence_source``,
          ``severity``, ``text``.
        * ``low_mastery`` — list of deduplicated low-mastery entry dicts,
          each with ``label``, ``kg_node_ids``, ``evidence_source``,
          ``mastery_score``, ``text``.
        * ``has_evidence`` — ``True`` when at least one weakness or
          low-mastery entry with ``kg_node_ids`` was found.
        * ``evidence_unavailable_reason`` — English string explaining why
          no evidence is available (empty when ``has_evidence`` is
          ``True``).
        * ``all_kg_node_ids`` — deduplicated flat list of all KG node IDs
          found across weaknesses and low-mastery entries.
    """
    l3 = _with_default_slots(memory.get_learner_memory(user_id))
    profile_state = l3.get(PROFILE_STATE_SLOT, {})
    if not isinstance(profile_state, dict):
        profile_state = {}

    weakness_entries: list[dict[str, Any]] = []
    low_mastery_entries: list[dict[str, Any]] = []
    seen_kg_ids: set[str] = set()

    # --- 1. Learner-memory weakness items ----------------------------------
    for item in _slot_items(l3, "weaknesses"):
        kg_ids = [
            str(kid).strip()
            for kid in (item.get("kg_node_ids") or [])
            if str(kid).strip()
        ]
        label = str(item.get("label") or item.get("text") or "").strip()
        text = str(item.get("text") or "").strip()
        if not kg_ids or (not label and not text):
            continue
        severity = _clamp_float(
            item.get("severity", item.get("confidence", 0.5)),
            0.0, 1.0, 0.5,
        )
        for kid in kg_ids:
            if kid in seen_kg_ids:
                continue
            seen_kg_ids.add(kid)
            weakness_entries.append({
                "label": label or text[:80],
                "kg_node_ids": [kid],
                "evidence_source": "learner_memory_weakness",
                "severity": round(severity, 3),
                "text": text[:200],
            })

    # --- 2. Low-mastery items from learner memory --------------------------
    for item in _slot_items(l3, "mastery"):
        score = _clamp_float(
            item.get("score", item.get("confidence", 0.5)),
            0.0, 1.0, 0.5,
        )
        if score >= 0.62:
            continue
        kg_ids = [
            str(kid).strip()
            for kid in (item.get("kg_node_ids") or [])
            if str(kid).strip()
        ]
        label = str(item.get("label") or item.get("text") or "").strip()
        text = str(item.get("text") or "").strip()
        if not kg_ids or (not label and not text):
            continue
        for kid in kg_ids:
            if kid in seen_kg_ids:
                continue
            seen_kg_ids.add(kid)
            low_mastery_entries.append({
                "label": label or text[:80],
                "kg_node_ids": [kid],
                "evidence_source": "low_mastery",
                "mastery_score": round(score, 3),
                "text": text[:200],
            })

    # --- 3. Profile-state summaries ----------------------------------------
    for ws in (profile_state.get("weakness_summary") or []):
        if not isinstance(ws, dict):
            continue
        kg_ids = [
            str(kid).strip()
            for kid in (ws.get("kg_node_ids") or [])
            if str(kid).strip()
        ]
        if not kg_ids:
            continue
        label = str(ws.get("label") or ws.get("text") or "").strip()
        if not label:
            continue
        for kid in kg_ids:
            if kid in seen_kg_ids:
                continue
            seen_kg_ids.add(kid)
            weakness_entries.append({
                "label": label,
                "kg_node_ids": [kid],
                "evidence_source": "profile_state",
                "severity": _clamp_float(ws.get("severity"), 0.0, 1.0, 0.5),
                "text": str(ws.get("text") or "")[:200],
            })

    for ms in (profile_state.get("mastery_summary") or []):
        if not isinstance(ms, dict):
            continue
        mastery = _clamp_float(ms.get("mastery"), 0.0, 1.0, 0.5)
        if mastery >= 0.62:
            continue
        kg_ids = [
            str(kid).strip()
            for kid in (ms.get("kg_node_ids") or [])
            if str(kid).strip()
        ]
        if not kg_ids:
            continue
        label = str(ms.get("label") or ms.get("text") or "").strip()
        if not label:
            continue
        for kid in kg_ids:
            if kid in seen_kg_ids:
                continue
            seen_kg_ids.add(kid)
            low_mastery_entries.append({
                "label": label,
                "kg_node_ids": [kid],
                "evidence_source": "profile_state",
                "mastery_score": round(mastery, 3),
                "text": str(ms.get("text") or "")[:200],
            })

    all_kg_node_ids = sorted(seen_kg_ids)
    has_evidence = bool(weakness_entries or low_mastery_entries)

    evidence_unavailable_reason = ""
    if not has_evidence:
        evidence_unavailable_reason = (
            "No weakness or low-mastery evidence with KG node IDs is "
            "available in the learner memory. The system has not yet formed "
            "reliable weakness judgments from your learning activities. "
            "Weakness evidence is built from quiz mistakes, repeated "
            "confusions, and low mastery scores in completed learning "
            "sessions."
        )

    return {
        "weaknesses": weakness_entries,
        "low_mastery": low_mastery_entries,
        "has_evidence": has_evidence,
        "evidence_unavailable_reason": evidence_unavailable_reason,
        "all_kg_node_ids": all_kg_node_ids,
    }
