"""Tutor Agent — single-step lecture delivery (LLM-powered).

Reads a handout section and generates natural teaching text with inline
quiz questions.  Does **not** write to the database, advance progress, or
update mastery — it is a pure leaf agent that only outputs structured
signals for the Orchestrator / State Updater to consume.

Public API
----------
* ``find_outline_node`` — locate the current node in an outline.
* ``build_tutor_step`` — produce one round of teaching (LLM with fallback).
* ``build_tutor_step_llm`` — LLM-only tutor step from a handout section.
* ``build_tutor_step_rule`` — rule-based fallback (placeholder).
* ``build_tutor_signals`` — extract structured signals from a tutor step.
* ``build_tutor_feedback`` — generate rule-based feedback from Assessment.
"""

from __future__ import annotations

import json
import re as _re
import time
from pathlib import Path
from typing import Any, Callable


# ============================================================================
# Node lookup
# ============================================================================


def find_outline_node(
    outline: dict[str, Any],
    current_node_id: str | None,
) -> tuple[int, dict[str, Any]]:
    """Locate the current node in *outline*."""
    nodes: list[dict[str, Any]] = outline.get("nodes", [])
    if not nodes:
        raise ValueError("outline has no nodes — cannot find current node")

    if current_node_id is not None:
        for i, node in enumerate(nodes):
            if node.get("id") == current_node_id:
                return i, node

    return 0, nodes[0]


# ============================================================================
# LLM config
# ============================================================================

_TUTOR_API_KEY_NAMES = (
    "LEARNING_TUTOR_LLM_API_KEY",
    "LEARNING_HANDOUT_LLM_API_KEY",
    "LEARNING_RESOURCE_LLM_API_KEY",
    "LEARNING_LLM_API_KEY",
    "QWEN_API_KEY",
    "DASHSCOPE_API_KEY",
)
_TUTOR_BASE_URL_NAMES = (
    "LEARNING_TUTOR_LLM_BASE_URL",
    "LEARNING_HANDOUT_LLM_BASE_URL",
    "LEARNING_RESOURCE_LLM_BASE_URL",
    "LEARNING_LLM_BASE_URL",
    "QWEN_BASE_URL",
    "DASHSCOPE_BASE_URL",
)
_TUTOR_MODEL_NAMES = (
    "LEARNING_TUTOR_LLM_MODEL",
    "LEARNING_HANDOUT_LLM_MODEL",
    "LEARNING_RESOURCE_LLM_MODEL",
    "LEARNING_LLM_MODEL",
    "QWEN_MODEL",
    "DASHSCOPE_MODEL",
)


class _TeachingTextStreamParser:
    """Extract an in-progress teaching_text JSON string from LLM deltas."""

    def __init__(self, callback: Callable[[str], None]) -> None:
        self._callback = callback
        self._buffer = ""
        self._emitted = ""

    def feed(self, delta: str) -> None:
        if not delta:
            return
        self._buffer += delta
        match = _re.search(r'"teaching_text"\s*:\s*"', self._buffer)
        if match is None:
            return
        decoded, _complete = _decode_partial_json_string(self._buffer[match.end():])
        if not decoded.startswith(self._emitted):
            return
        addition = decoded[len(self._emitted):]
        if addition:
            self._emitted = decoded
            self._callback(addition)


def _decode_partial_json_string(value: str) -> tuple[str, bool]:
    output: list[str] = []
    escapes = {
        '"': '"',
        "\\": "\\",
        "/": "/",
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
    }
    index = 0
    while index < len(value):
        char = value[index]
        if char == '"':
            return "".join(output), True
        if char != "\\":
            output.append(char)
            index += 1
            continue
        if index + 1 >= len(value):
            break
        escape = value[index + 1]
        if escape in escapes:
            output.append(escapes[escape])
            index += 2
            continue
        if escape == "u":
            codepoint = value[index + 2:index + 6]
            if len(codepoint) < 4 or not _re.fullmatch(r"[0-9a-fA-F]{4}", codepoint):
                break
            output.append(chr(int(codepoint, 16)))
            index += 6
            continue
        break
    return "".join(output), False


# ============================================================================
# Tutor step — LLM-powered
# ============================================================================


def build_tutor_step_llm(
    outline_node: dict[str, Any],
    handout_section_content: dict[str, Any],
    handout_section_title: str,
    kg_node_ids: list[str],
    previous_node_title: str | None = None,
    next_node_title: str | None = None,
    user_mastery_summary: dict[str, Any] | None = None,
    section_position: dict[str, Any] | None = None,
    stream_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Generate a tutor step using LLM, driven by a handout section.

    Parameters
    ----------
    outline_node:
        The current outline node dict (must include id, title,
        learning_objectives, sequence_reason).
    handout_section_content:
        The ``content`` field from a generated handout section dict
        (overview, key_points, clinical_example, common_confusions,
        transition_to_next).
    handout_section_title:
        Title of the handout section being taught.
    kg_node_ids:
        KG node ids associated with this section.
    previous_node_title / next_node_title:
        Titles of adjacent outline nodes (for context).
    user_mastery_summary:
        Optional mastery summary dict keyed by kg_node_id.

    Returns
    -------
    A tutor step dict with keys: ``outline_node_id``, ``teaching_text``,
    ``questions``, ``signals``, ``trace``, ``expected_user_action``,
    ``progress``, ``content_status``, ``content_note``.
    """
    t_start = time.time()

    # --- Check LLM availability ---
    try:
        from backend.services.llm_client import chat_completion, is_llm_configured
    except Exception:
        return _failed_tutor_step(
            outline_node.get("id", ""), "llm_client_import_failed"
        )

    if not is_llm_configured(*_TUTOR_API_KEY_NAMES):
        return _failed_tutor_step(
            outline_node.get("id", ""), "no_llm_api_key_configured"
        )

    _prompt_dir = Path(__file__).resolve().parent / "prompts"

    # --- Guard: handout section must be valid before calling LLM ---
    node_id = outline_node.get("id", "")
    node_title = outline_node.get("title", "")

    _kp = handout_section_content.get("key_points", []) if isinstance(handout_section_content, dict) else []
    _has_kp = isinstance(_kp, list) and len(_kp) > 0
    _has_text = bool(handout_section_content) and isinstance(handout_section_content, dict)

    if not _has_text or not _has_kp:
        return _failed_tutor_step(
            node_id,
            "missing_valid_handout_section: "
            + ("handout_section_content is empty or not a dict"
               if not _has_text else "handout_section_content has no key_points"),
        )

    # --- Build LLM input ---

    # Lightweight mastery summary (avoid flooding the prompt)
    mastery_summary_light: dict[str, dict] = {}
    if user_mastery_summary:
        for kg_id in kg_node_ids:
            if kg_id in user_mastery_summary:
                rec = user_mastery_summary[kg_id]
                mastery_summary_light[kg_id] = {
                    "mastery_score": rec.get("mastery_score", 0.0),
                    "exposure_count": rec.get("exposure_count", 0),
                }

    system_prompt = _load_prompt(_prompt_dir, "tutor.md")

    # Default section_position if not provided
    sp = section_position or {}
    section_pos = {
        "is_first_section": bool(sp.get("is_first_section", False)),
        "is_last_section": bool(sp.get("is_last_section", False)),
        "current_index": int(sp.get("current_index", 1)),
        "total": int(sp.get("total", 1)),
    }

    user_prompt = json.dumps({
        "current_outline_node": {
            "id": node_id,
            "title": node_title,
            "learning_objectives": outline_node.get("learning_objectives", []),
            "sequence_reason": outline_node.get("sequence_reason", ""),
        },
        "handout_section": handout_section_content,
        "previous_node_title": previous_node_title,
        "next_node_title": next_node_title,
        "section_position": section_pos,
        "kg_node_ids": kg_node_ids,
        "user_mastery_summary": mastery_summary_light if mastery_summary_light else None,
    }, ensure_ascii=False, indent=2)

    # --- Call LLM ---
    try:
        parser = _TeachingTextStreamParser(stream_callback) if stream_callback else None
        raw = chat_completion(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.5,
            max_tokens=6000,
            api_key_names=_TUTOR_API_KEY_NAMES,
            base_url_names=_TUTOR_BASE_URL_NAMES,
            model_names=_TUTOR_MODEL_NAMES,
            response_format={"type": "json_object"},
            stream_callback=parser.feed if parser else None,
        )
    except Exception as exc:
        return _failed_tutor_step(node_id, f"llm_call_failed: {str(exc)[:200]}")

    # --- Parse ---
    try:
        parsed = _parse_tutor_output(raw)
    except Exception as exc:
        return _failed_tutor_step(node_id, f"json_parse_failed: {str(exc)[:200]}")

    # --- Normalize ---
    tutor_status = str(parsed.get("tutor_status", "")).strip()
    teaching_text = str(parsed.get("teaching_text", "")).strip()
    if not teaching_text:
        return _failed_tutor_step(node_id, "empty_teaching_text")

    # Normalize questions
    raw_questions = parsed.get("questions", [])
    if not isinstance(raw_questions, list):
        raw_questions = []
    questions: list[dict] = []
    for q in raw_questions:
        if not isinstance(q, dict):
            continue
        questions.append({
            "id": str(q.get("id", f"q_inline_{node_id}")),
            "type": q.get("type", "single_choice"),
            "stem": str(q.get("stem", "")).strip(),
            "options": _normalize_str_list(q.get("options", [])),
            "answer": str(q.get("answer", "")).strip().upper(),
            "explanation": str(q.get("explanation", "")).strip(),
            "kg_node_ids": _normalize_str_list(q.get("kg_node_ids", [])),
        })

    # Normalize signals
    raw_signals = parsed.get("signals", {})
    if not isinstance(raw_signals, dict):
        raw_signals = {}

    raw_used_kg = raw_signals.get("used_kg_node_ids", kg_node_ids)
    used_kg_node_ids = _normalize_str_list(raw_used_kg) if raw_used_kg else list(kg_node_ids)

    used_handout_title = str(
        raw_signals.get("used_handout_section_title", handout_section_title)
    ).strip()

    # Normalize trace
    raw_trace = parsed.get("trace", {})
    if not isinstance(raw_trace, dict):
        raw_trace = {}

    # Normalize quiz scope / timing / user actions
    quiz_scope = str(parsed.get("quiz_scope", "section")).strip()
    if quiz_scope not in ("section",):
        quiz_scope = "section"
    quiz_timing = str(parsed.get("quiz_timing", "after_section")).strip()
    if quiz_timing not in ("after_section",):
        quiz_timing = "after_section"

    raw_available = parsed.get("available_user_actions", None)
    if isinstance(raw_available, list) and raw_available:
        available_user_actions = [str(a).strip() for a in raw_available if str(a).strip()]
    else:
        available_user_actions = ["ask_question", "take_quiz", "continue"]

    llm_expected_action = str(parsed.get("expected_user_action", "")).strip()
    expected_user_action = llm_expected_action if llm_expected_action else "choose_next_action"

    total_seconds = round(time.time() - t_start, 3)

    # --- Validate teaching_text quality (guardrail, no content generation) ---
    validation_errors = validate_tutor_teaching_text(
        teaching_text=teaching_text,
        handout_key_points=handout_section_content.get("key_points", []),
    )
    if validation_errors:
        return _failed_tutor_step(
            node_id,
            f"teaching_text_quality_failed: {'; '.join(validation_errors[:3])}",
        )

    # --- Assemble ---
    signals = {
        "used_kg_node_ids": used_kg_node_ids,
        "used_handout_section_title": used_handout_title,
        "advance_progress": False,  # Tutor NEVER advances progress
    }

    trace = {
        "mode": raw_trace.get("mode", "llm_tutor_agent"),
        "uses_llm": True,
        "uses_handout_section": True,
        "advance_progress": False,
        "tutor_status": tutor_status,
        "total_seconds": total_seconds,
    }

    return {
        "outline_node_id": node_id,
        "teaching_text": teaching_text,
        "questions": questions,
        "quiz_scope": quiz_scope,
        "quiz_timing": quiz_timing,
        "available_user_actions": available_user_actions,
        "expected_user_action": expected_user_action,
        "signals": signals,
        "trace": trace,
        "content_status": "llm_generated",
        "content_note": (
            "Tutor 讲解基于 handout section 生成，由 LLM Agent 自主生成。"
            "题目为当前 section 的可选小测（optional section quiz），用户可自主选择下一步。"
        ),
        "progress": {"current_index": -1, "total": -1},
    }


# ============================================================================
# Tutor step — rule-based fallback
# ============================================================================


def build_tutor_step_from_handout_rule(
    *,
    outline_node: dict[str, Any],
    handout_section_content: dict[str, Any],
    handout_section_title: str,
    kg_node_ids: list[str] | None = None,
    section_position: dict[str, Any] | None = None,
    stream_callback: Callable[[str], None] | None = None,
    error: str = "",
) -> dict[str, Any]:
    """Build a local Tutor step from an already validated handout section."""
    node_id = str(outline_node.get("id") or "")
    title = handout_section_title or str(outline_node.get("title") or "本节内容")
    overview = str(handout_section_content.get("overview") or "").strip()
    key_points = [
        item for item in handout_section_content.get("key_points", [])
        if isinstance(item, dict)
    ]
    confusions = [
        item for item in handout_section_content.get("common_confusions", [])
        if isinstance(item, dict)
    ]
    clinical_example = str(handout_section_content.get("clinical_example") or "").strip()
    transition = str(handout_section_content.get("transition_to_next") or "").strip()

    lines = [f"这一节我们学习「{title}」。"]
    if overview:
        lines.extend(["", overview])
    if key_points:
        lines.append("")
        lines.append("本节可以抓住这些要点：")
        for index, point in enumerate(key_points, 1):
            heading = str(point.get("heading") or f"要点 {index}").strip()
            explanation = str(point.get("explanation") or "").strip()
            lines.append(f"{index}. {heading}。{explanation}")
    if clinical_example:
        lines.extend(["", f"使用提示：{clinical_example}"])
    if confusions:
        lines.append("")
        lines.append("容易混淆的地方：")
        for item in confusions[:3]:
            confusion = str(item.get("confusion") or "").strip()
            clarification = str(item.get("clarification") or "").strip()
            if confusion or clarification:
                lines.append(f"- {confusion}：{clarification}")
    if transition:
        lines.extend(["", transition])
    lines.append("")
    lines.append("这段讲解来自系统已检索到的知识图谱、题库和来源章节整理；如果需要更深入的临床推理，可以继续追问本节细节。")
    teaching_text = "\n".join(line for line in lines if line is not None).strip()
    if stream_callback is not None and teaching_text:
        stream_callback(teaching_text)

    first_heading = str((key_points[0] if key_points else {}).get("heading") or title).strip()
    questions = [{
        "id": f"{node_id}_rule_q1",
        "type": "single_choice",
        "stem": f"学习「{title}」时，最稳妥的做法是？",
        "options": [
            "A. 只背题干关键词",
            f"B. 围绕「{first_heading}」回到证据来源理解",
            "C. 跳过概念直接记答案",
            "D. 不看来源章节",
        ],
        "answer": "B",
        "explanation": "本地兜底讲稿只整理证据来源，学习时应回到 KG 节点、关系和来源章节理解。",
    }]

    current = int((section_position or {}).get("current_index") or -1)
    total = int((section_position or {}).get("total") or -1)
    return {
        "outline_node_id": node_id,
        "teaching_text": teaching_text,
        "questions": questions,
        "quiz_scope": "section",
        "quiz_timing": "after_section",
        "available_user_actions": ["ask_question", "take_quiz", "continue"],
        "expected_user_action": "choose_next_action",
        "signals": {
            "used_kg_node_ids": kg_node_ids or [],
            "used_handout_section_title": title,
            "advance_progress": False,
        },
        "trace": {
            "mode": "fallback_handout_tutor_step",
            "uses_llm": False,
            "uses_handout_section": True,
            "advance_progress": False,
            "fallback_rule_based": True,
            "llm_error": error,
        },
        "content_status": "fallback_rule_based",
        "content_note": "LLM Tutor 不可用；当前讲稿由已生成 handout 证据结构本地整理。",
        "progress": {
            "current_index": current,
            "total": total,
        },
    }


def build_tutor_step_rule(
    outline: dict[str, Any],
    current_node_id: str | None,
    user_profile: dict[str, Any] | None = None,
    mastery: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Rule-based placeholder tutor step (NO handout section, NO LLM)."""
    nodes: list[dict[str, Any]] = outline.get("nodes", [])
    total = len(nodes)

    index, node = find_outline_node(outline, current_node_id)
    node_id = node.get("id", "")
    node_title = node.get("title", "")
    kg_node_ids: list[str] = node.get("kg_node_ids", [])

    teaching_text = (
        f"「{node_title}」的讲义与讲稿生成未完成，当前不能提供可靠的正式讲解。\n\n"
        "系统已记录具体失败原因，请重试本节内容生成后再继续学习。"
    )

    signals = {
        "used_kg_node_ids": kg_node_ids,
        "used_handout_section_title": None,
        "advance_progress": False,
    }

    trace = {
        "mode": "rule_based_placeholder_tutor_step",
        "uses_llm": False,
        "uses_handout_section": False,
        "advance_progress": False,
        "fallback_rule_based": True,
    }

    return {
        "outline_node_id": node_id,
        "teaching_text": teaching_text,
        "questions": [],
        "quiz_scope": "section",
        "quiz_timing": "after_section",
        "available_user_actions": ["ask_question", "take_quiz", "continue"],
        "expected_user_action": "choose_next_action",
        "signals": signals,
        "trace": trace,
        "content_status": "generation_failed",
        "content_note": (
            "正式讲义或讲稿生成失败；当前内容仅报告失败状态，不作为教学内容。"
        ),
        "progress": {
            "current_index": index + 1,
            "total": total,
        },
    }


# ============================================================================
# Tutor step — main entry (LLM with rule-based fallback)
# ============================================================================


def build_tutor_step(
    outline: dict[str, Any] | None = None,
    current_node_id: str | None = None,
    user_profile: dict[str, Any] | None = None,
    mastery: dict[str, Any] | None = None,
    *,
    # --- LLM path parameters ---
    outline_node: dict[str, Any] | None = None,
    handout_section_content: dict[str, Any] | None = None,
    handout_section_title: str = "",
    kg_node_ids: list[str] | None = None,
    previous_node_title: str | None = None,
    next_node_title: str | None = None,
    user_mastery_summary: dict[str, Any] | None = None,
    section_position: dict[str, Any] | None = None,
    stream_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Produce one round of teaching for the current outline node.

    Prefers the LLM path when *handout_section_content* is provided.
    Falls back to the rule-based path when handout content is unavailable
    or LLM is not configured.

    Parameters
    ----------
    outline / current_node_id / user_profile / mastery:
        Rule-based path parameters (kept for backward compatibility).
    outline_node / handout_section_content / ...:
        LLM path parameters.  When *handout_section_content* is provided,
        the LLM path is attempted first.
    """
    # --- LLM path ---
    if handout_section_content is not None and outline_node is not None:
        try:
            result = build_tutor_step_llm(
                outline_node=outline_node,
                handout_section_content=handout_section_content,
                handout_section_title=handout_section_title,
                kg_node_ids=kg_node_ids or [],
                previous_node_title=previous_node_title,
                next_node_title=next_node_title,
                user_mastery_summary=user_mastery_summary,
                section_position=section_position,
                stream_callback=stream_callback,
            )
            if result.get("content_status") != "failed":
                return result
            # Handout provided but Tutor failed — return the failed result directly.
            # Do NOT fallback to rule-based (which teaches from outline title).
            # The only exception: if the failure is an LLM config issue (not a
            # handout quality issue), let it fall through to rule-based if available.
            ts_error = result.get("trace", {}).get("error", "")
            if "missing_valid_handout_section" in ts_error:
                return result
            if ts_error and (
                "no_llm_api_key" in ts_error
                or "llm_client_import" in ts_error
                or "llm_call_failed" in ts_error
                or "Arrearage" in ts_error
                or "HTTP Error 400" in ts_error
            ):
                return build_tutor_step_from_handout_rule(
                    outline_node=outline_node,
                    handout_section_content=handout_section_content,
                    handout_section_title=handout_section_title,
                    kg_node_ids=kg_node_ids or [],
                    section_position=section_position,
                    stream_callback=stream_callback,
                    error=ts_error,
                )
            if ts_error:
                return result
        except Exception:
            pass  # Fall through to rule-based for unexpected errors

    # --- Rule-based fallback ---
    if outline is not None:
        return build_tutor_step_rule(
            outline=outline,
            current_node_id=current_node_id,
            user_profile=user_profile,
            mastery=mastery,
        )

    # --- Nothing to work with ---
    return _failed_tutor_step(
        outline_node.get("id", "") if outline_node else (current_node_id or ""),
        "no_input_provided",
    )


# ============================================================================
# Signals extraction
# ============================================================================


def build_tutor_signals(step: dict[str, Any]) -> dict[str, Any]:
    """Extract structured signals from a tutor step for the State Updater.

    Returns a dict ready to be consumed by
    :meth:`LearningMemory.record_tutor_exposure`.
    """
    signals = step.get("signals", {})
    return {
        "used_kg_node_ids": signals.get("used_kg_node_ids", []),
        "used_handout_section_title": signals.get("used_handout_section_title", ""),
        "advance_progress": signals.get("advance_progress", False),
        "trace": step.get("trace", {}),
    }


# ============================================================================
# Teaching text quality validator (guardrail only — does NOT generate content)
# ============================================================================


def validate_tutor_teaching_text(
    teaching_text: str,
    handout_key_points: list[dict],
) -> list[str]:
    """Validate that *teaching_text* is a real lecture, not a shell.

    Returns a list of error strings (empty = valid).  Never modifies content.
    """
    errors: list[str] = []

    if not teaching_text or not teaching_text.strip():
        errors.append("teaching_text is empty")
        return errors

    text = teaching_text.strip()

    # --- Absolute minimum length ---
    if len(text) < 120:
        errors.append(f"teaching_text too short ({len(text)} chars, min 120)")
        return errors

    # --- If handout has key_points, teaching_text must cover them ---
    if handout_key_points:
        kp_headings = [kp.get("heading", "") for kp in handout_key_points if kp.get("heading")]
        kp_headings = [h for h in kp_headings if len(h) >= 2]

        if kp_headings:
            # Check how many key_point headings are reflected in teaching_text
            covered = 0
            for h in kp_headings:
                # Check if heading keywords appear (split heading into 2-char tokens)
                tokens = [h[i:i+2] for i in range(0, len(h), 2)]
                if any(t in text for t in tokens if len(t) >= 2):
                    covered += 1

            total = len(kp_headings)
            if total >= 2 and covered == 0:
                errors.append(
                    f"teaching_text covers 0/{total} key_point headings — "
                    f"likely only an overview/intro, not a real lecture"
                )
            elif total >= 3 and covered <= 1:
                errors.append(
                    f"teaching_text covers only {covered}/{total} key_point headings — "
                    f"insufficient coverage for a real lecture"
                )

    # --- Reject shell patterns: text that is only intro/预告 without substance ---
    shell_patterns = [
        # Only talks about what the section "will cover"
        "这一节会帮你", "这节课是要帮你", "这一节会带你",
        "你会了解到", "你会学到", "这节课会讲",
        # Only talks about content not being ready
        "讲义内容目前还没有准备好", "暂时没法展开详细的讲解",
        "讲义内容还没有准备好", "讲义完善后",
    ]
    shell_count = sum(1 for p in shell_patterns if p in text)
    # If >60% of the text matches shell patterns (by count), it's a shell
    # For short texts (<200 chars), even 2 shell hits is too many
    if len(text) < 200 and shell_count >= 2:
        errors.append(
            f"teaching_text looks like a shell/intro only ({len(text)} chars, "
            f"{shell_count} shell patterns matched) — expected real lecture content"
        )
    elif len(text) >= 200 and shell_count >= 4:
        errors.append(
            f"teaching_text has too many shell patterns ({shell_count}) — "
            f"likely not a real lecture"
        )

    return errors


# ============================================================================
# Feedback (rule-based — unchanged from MVP)
# ============================================================================


def build_tutor_feedback(
    question: dict[str, Any],
    assessment: dict[str, Any],
) -> dict[str, Any]:
    """Generate rule-based feedback text from an assessment result."""
    is_correct = bool(assessment.get("is_correct", False))
    explanation = assessment.get("explanation", "")
    recommendation: dict[str, Any] = assessment.get("recommendation", {})

    next_action: str
    feedback_text: str

    if is_correct:
        next_action = "continue"
        feedback_text = "回答正确！你对本节核心概念理解到位，可以继续下一节的学习。"
    else:
        next_action = "reinforce"
        detail = explanation or "建议回顾本节内容，重点关注核心概念与证据来源。"
        rec_action = recommendation.get("action", "reinforce")
        if rec_action == "reinforce":
            feedback_text = (
                f"回答有误。{detail}\n\n"
                "建议重新学习本节内容，理解核心概念后再尝试。"
            )
        else:
            feedback_text = f"回答待改进。{detail}"

    return {
        "feedback_text": feedback_text,
        "next_action": next_action,
        "trace": {
            "mode": "rule_based_placeholder_feedback",
            "content_status": "placeholder",
        },
    }


# ============================================================================
# Internal helpers
# ============================================================================


def _load_prompt(prompt_dir: Path, filename: str) -> str:
    """Load a prompt file, or return a fallback."""
    prompt_path = prompt_dir / filename
    if prompt_path.exists():
        return prompt_path.read_text(encoding="utf-8")
    return "你是一名口腔医学讲师。请基于提供的讲义内容生成教学讲解和互动问题。输出严格JSON。"


def _parse_tutor_output(raw: str) -> dict:
    """Parse LLM tutor output, handling JSON fences."""
    text = raw.strip()
    fence_match = _re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, _re.DOTALL)
    if not fence_match:
        fence_match = _re.search(r"```(?:json)?\s*\n?(.*)$", text, _re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError(f"Expected dict, got {type(parsed)}")
    return parsed


def _normalize_str_list(value: Any) -> list[str]:
    """Normalize a value to a list of non-empty strings."""
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def _failed_tutor_step(node_id: str, error: str) -> dict:
    """Build a failed tutor step with all required fields."""
    return {
        "outline_node_id": node_id,
        "teaching_text": "",
        "questions": [],
        "quiz_scope": "section",
        "quiz_timing": "after_section",
        "available_user_actions": ["ask_question", "take_quiz", "continue"],
        "expected_user_action": "choose_next_action",
        "signals": {
            "used_kg_node_ids": [],
            "used_handout_section_title": "",
            "advance_progress": False,
        },
        "trace": {
            "mode": "tutor_failed",
            "uses_llm": False,
            "uses_handout_section": False,
            "advance_progress": False,
            "error": error,
        },
        "content_status": "failed",
        "content_note": f"Tutor 生成失败: {error}",
        "progress": {"current_index": -1, "total": -1},
    }
