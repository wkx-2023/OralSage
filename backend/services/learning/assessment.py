"""Assessment Agent — evaluate a user answer and recommend next action.

Two-layer architecture:

1. **Python rule-based scoring** (deterministic):
   - Extract selected option (A/B/C/D) from free-form user input
   - Compare against correct answer
   - Compute score, is_correct, mastery_delta

2. **LLM deep analysis** (wrong answers only):
   - option_error_analysis: why the selected option is wrong
   - correct_answer_explanation: why the correct answer is right
   - misconception_summary: what misconception this reveals
   - related_kg_node_ids: which KG nodes are involved
   - remedial_suggestion: how to improve

Correct answers use brief rule-based confirmation — no LLM call needed.
Wrong answers **must** go through LLM analysis; if LLM fails,
``fallback_rule_based`` is explicitly marked and the result must not
be presented as a genuine Assessment.

Public API
----------
* ``normalize_answer`` — extract A/B/C/D from free-form user input.
* ``assess_answer`` — main entry point: score + (for wrong) LLM deep analysis.
* ``assess_answer_with_llm`` — LLM-driven wrong-answer deep analysis.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from backend.services.llm_client import chat_completion, is_llm_configured

# LLM config key priority for Assessment
_ASSESSMENT_API_KEY_NAMES = (
    "LEARNING_ASSESSMENT_LLM_API_KEY",
    "LEARNING_LLM_API_KEY",
    "QWEN_API_KEY",
    "DASHSCOPE_API_KEY",
)
_ASSESSMENT_BASE_URL_NAMES = (
    "LEARNING_ASSESSMENT_LLM_BASE_URL",
    "LEARNING_LLM_BASE_URL",
    "QWEN_BASE_URL",
    "DASHSCOPE_BASE_URL",
)
_ASSESSMENT_MODEL_NAMES = (
    "LEARNING_ASSESSMENT_LLM_MODEL",
    "LEARNING_LLM_MODEL",
    "QWEN_MODEL",
    "DASHSCOPE_MODEL",
)

# ============================================================================
# Answer normalisation
# ============================================================================

# Patterns that extract the intended choice letter from common Chinese formats.
_NORMALIZE_PATTERNS = [
    # "选B" / "答案是B" / "选 B" / "答案：B" / "选择B"
    re.compile(r"(?:选|答案|选择)\s*(?:是|：|为)?\s*([A-Ea-e])"),
    # Bare letter at start: "A. xxx" / "A、xxx" / "A xxx"
    re.compile(r"^\s*([A-Ea-e])\s*[.、\s]"),
    # Bare letter anywhere
    re.compile(r"([A-Ea-e])"),
]


def normalize_answer(answer: str) -> str:
    """Extract a normalised A/B/C/D choice letter from a free-form answer.

    Parameters
    ----------
    answer:
        Raw user input, e.g. ``"B"``, ``"B. 理解核心概念"``,
        ``"选B"``, ``"答案是B"``, ``"B、理解核心概念"``.

    Returns
    -------
    str
        Uppercase single letter ``"A"`` … ``"E"``, or the original
        stripped uppercase text if no pattern matches.
    """
    text = answer.strip()

    # Try ordered patterns — first match wins
    for pat in _NORMALIZE_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1).upper()

    # Fallback: just uppercase whatever we have
    return text.upper()


# ============================================================================
# Assessment — main entry point
# ============================================================================


def assess_answer(
    question: dict[str, Any],
    user_answer: str,
    outline_node_id: str | None = None,
    user_profile: dict[str, Any] | None = None,
    handout_section: dict[str, Any] | None = None,
    node_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Grade a single answer and produce a structured assessment.

    Two-layer architecture:
    1. Python deterministic scoring (always)
    2. LLM deep analysis for wrong answers (when LLM configured and
       handout_section available)

    Parameters
    ----------
    question:
        A question dict with at least ``id``, ``type``, ``answer``, ``stem``,
        ``options``.  For single_choice questions ``answer`` should be a
        single letter (A/B/C/D/E).
    user_answer:
        Raw user input.
    outline_node_id:
        Optional override for the outline node.  Falls back to
        ``question["outline_node_id"]``.
    user_profile:
        Optional user profile dict.
    handout_section:
        Optional handout section dict (from Resource Factory).  If provided
        and answer is wrong, enables LLM deep analysis.
    node_evidence:
        Optional node evidence dict from outline_evidence_map.

    Returns
    -------
    dict with keys: is_correct, score, selected_option, correct_option,
    option_error_analysis, correct_answer_explanation, misconception_summary,
    related_kg_node_ids, recommendation, fallback_rule_based, content_status,
    trace, mastery_delta, mistake_tags, explanation.
    """
    question_id = question.get("id", "")
    q_type = question.get("type", "")
    correct_answer = question.get("answer", "")
    node_id = outline_node_id or question.get("outline_node_id", "")

    normalized = normalize_answer(user_answer)
    valid_choices = {"A", "B", "C", "D", "E"}

    # ------------------------------------------------------------------
    # single_choice — Python deterministic scoring
    # ------------------------------------------------------------------
    if q_type == "single_choice":
        is_correct = (normalized == correct_answer.upper())

        if is_correct:
            return _correct_result(
                question_id=question_id,
                node_id=node_id,
                user_answer=user_answer,
                normalized=normalized,
                correct_answer=correct_answer,
                question=question,
            )
        else:
            # --- Wrong answer: try LLM deep analysis ---
            base_result = _incorrect_base(
                question_id=question_id,
                node_id=node_id,
                user_answer=user_answer,
                normalized=normalized,
                correct_answer=correct_answer,
                valid_choices=valid_choices,
            )

            # Attempt LLM deep analysis
            if handout_section and _has_valid_content(handout_section):
                llm_result = assess_answer_with_llm(
                    question=question,
                    user_selected_option=normalized,
                    correct_option=correct_answer.upper(),
                    outline_node_id=node_id,
                    handout_section=handout_section,
                    user_profile=user_profile,
                )
                if llm_result is not None:
                    # Merge LLM analysis into base result
                    return _merge_llm_analysis(base_result, llm_result, question)
                else:
                    # LLM failed — mark fallback explicitly
                    base_result["fallback_rule_based"] = True
                    base_result["content_status"] = "rule_based"
                    base_result["trace"]["llm_attempted"] = True
                    base_result["trace"]["llm_failed"] = True
                    base_result["trace"]["llm_failure_reason"] = "llm_call_or_parse_failed"
                    return base_result
            else:
                # No handout_section available — mark fallback explicitly
                base_result["fallback_rule_based"] = True
                base_result["content_status"] = "rule_based"
                base_result["trace"]["llm_attempted"] = False
                base_result["trace"]["llm_skipped_reason"] = "no_valid_handout_section"
                return base_result

    # ------------------------------------------------------------------
    # unsupported question type
    # ------------------------------------------------------------------
    return _unsupported_result(
        question_id=question_id,
        node_id=node_id,
        user_answer=user_answer,
        normalized=normalized,
        correct_answer=correct_answer,
        q_type=q_type,
    )


# ============================================================================
# LLM deep analysis (wrong answers only)
# ============================================================================


def assess_answer_with_llm(
    question: dict[str, Any],
    user_selected_option: str,
    correct_option: str,
    outline_node_id: str | None = None,
    handout_section: dict[str, Any] | None = None,
    user_profile: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """LLM-driven deep analysis for a wrong answer.

    Python **only** does deterministic scoring (is_correct, score,
    selected_option, correct_option).  LLM is responsible for:
    - option_error_analysis
    - correct_answer_explanation
    - misconception_summary
    - related_kg_node_ids
    - remedial_suggestion

    Parameters
    ----------
    question:
        The full question dict (stem, options, answer, explanation, kg_node_ids).
    user_selected_option:
        The option letter the user selected (already normalized, uppercase).
    correct_option:
        The correct option letter (uppercase).
    outline_node_id:
        Current outline node id.
    handout_section:
        The handout section dict for context.
    user_profile:
        Optional user profile dict.

    Returns
    -------
    dict | None
        LLM analysis fields on success, ``None`` if LLM is unavailable or fails.
        The caller must **not** treat ``None`` as a valid Assessment —
        ``fallback_rule_based`` must be marked.
    """
    if not is_llm_configured(*_ASSESSMENT_API_KEY_NAMES):
        return None

    t_start = time.time()

    # --- Build prompt ---
    prompt_dir = Path(__file__).resolve().parent / "prompts"
    system_prompt = _load_prompt(prompt_dir, "assessment.md")

    # Build handout section summary (lightweight — headings only)
    handout_summary = _build_handout_summary_for_assessment(handout_section)

    # Build user prompt
    user_prompt = json.dumps({
        "question": {
            "id": question.get("id", ""),
            "stem": question.get("stem", ""),
            "options": question.get("options", []),
            "answer": question.get("answer", ""),
            "explanation": question.get("explanation", ""),
            "kg_node_ids": question.get("kg_node_ids", []),
        },
        "user_selected_option": user_selected_option,
        "correct_option": correct_option,
        "current_outline_node": {
            "id": outline_node_id or "",
        },
        "handout_section_summary": handout_summary,
        "user_profile": user_profile or {},
    }, ensure_ascii=False, indent=2)

    # --- LLM call ---
    try:
        raw = chat_completion(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=2000,
            api_key_names=_ASSESSMENT_API_KEY_NAMES,
            base_url_names=_ASSESSMENT_BASE_URL_NAMES,
            model_names=_ASSESSMENT_MODEL_NAMES,
            response_format={"type": "json_object"},
        )
    except Exception:
        return None

    # --- Parse ---
    parsed = _parse_json_output(raw)
    if parsed is None:
        return None

    # --- Validate required fields ---
    option_error_analysis = str(parsed.get("option_error_analysis", "")).strip()
    correct_answer_explanation = str(parsed.get("correct_answer_explanation", "")).strip()
    misconception_summary = str(parsed.get("misconception_summary", "")).strip()
    remedial_suggestion = str(parsed.get("remedial_suggestion", "")).strip()

    # All four content fields must be non-empty
    if not option_error_analysis or not correct_answer_explanation or not misconception_summary:
        return None

    related_kg_node_ids = _normalize_str_list(parsed.get("related_kg_node_ids", []))
    # Fallback: use question's own kg_node_ids
    if not related_kg_node_ids:
        related_kg_node_ids = _normalize_str_list(question.get("kg_node_ids", []))

    total_seconds = round(time.time() - t_start, 3)

    return {
        "option_error_analysis": option_error_analysis,
        "correct_answer_explanation": correct_answer_explanation,
        "misconception_summary": misconception_summary,
        "related_kg_node_ids": related_kg_node_ids,
        "remedial_suggestion": remedial_suggestion,
        "_llm_seconds": total_seconds,
    }


# ============================================================================
# Result builders
# ============================================================================


def _correct_result(
    question_id: str,
    node_id: str,
    user_answer: str,
    normalized: str,
    correct_answer: str,
    question: dict[str, Any],
) -> dict[str, Any]:
    """Build result for a correct answer.  Brief confirmation, no LLM."""
    question_explanation = question.get("explanation", "")
    related_kg = _normalize_str_list(question.get("kg_node_ids", []))

    return {
        "question_id": question_id,
        "outline_node_id": node_id,
        "user_answer": user_answer,
        "normalized_user_answer": normalized,
        "selected_option": normalized,
        "correct_option": correct_answer.upper(),
        "is_correct": True,
        "score": 1.0,
        "mastery_delta": 0.12,
        "mistake_tags": [],
        "explanation": question_explanation or "回答正确，你对本节核心概念理解到位。",
        "option_error_analysis": None,
        "correct_answer_explanation": question_explanation or None,
        "misconception_summary": None,
        "related_kg_node_ids": related_kg,
        "recommendation": {
            "action": "continue",
            "reason": "用户回答正确，可以继续下一节学习",
            "remedial_suggestion": None,
            "target_outline_node_id": node_id,
        },
        "fallback_rule_based": False,
        "content_status": "rule_based",
        "trace": {
            "mode": "rule_based_assessment",
            "uses_llm": False,
            "supports_subjective_grading": False,
            "llm_attempted": False,
        },
    }


def _incorrect_base(
    question_id: str,
    node_id: str,
    user_answer: str,
    normalized: str,
    correct_answer: str,
    valid_choices: set[str],
) -> dict[str, Any]:
    """Build the base (Python-scored) result for a wrong answer.

    The returned dict has ``fallback_rule_based=False`` as a placeholder
    — the caller must set it to ``True`` if LLM analysis is not available
    or failed.  The ``option_error_analysis`` etc. fields are ``None``
    until LLM analysis succeeds.
    """
    mistake_tags: list[str] = ["选择题错误"]

    # Detect format issues
    if not normalized or normalized not in valid_choices:
        mistake_tags.append("答案格式不清")

    return {
        "question_id": question_id,
        "outline_node_id": node_id,
        "user_answer": user_answer,
        "normalized_user_answer": normalized,
        "selected_option": normalized,
        "correct_option": correct_answer.upper(),
        "is_correct": False,
        "score": 0.0,
        "mastery_delta": -0.08,
        "mistake_tags": mistake_tags,
        "explanation": "回答有误。详见下方错因分析。",
        "option_error_analysis": None,
        "correct_answer_explanation": None,
        "misconception_summary": None,
        "related_kg_node_ids": [],
        "recommendation": {
            "action": "reinforce",
            "reason": "用户回答错误，需要强化本节学习",
            "remedial_suggestion": None,
            "target_outline_node_id": node_id,
        },
        "fallback_rule_based": False,  # caller must override if no LLM
        "content_status": "rule_based",  # caller must override if LLM succeeds
        "trace": {
            "mode": "rule_based_assessment",
            "uses_llm": False,
            "supports_subjective_grading": False,
            "llm_attempted": False,
        },
    }


def _merge_llm_analysis(
    base: dict[str, Any],
    llm: dict[str, Any],
    question: dict[str, Any],
) -> dict[str, Any]:
    """Merge LLM deep analysis fields into the base result."""
    base["option_error_analysis"] = llm["option_error_analysis"]
    base["correct_answer_explanation"] = llm["correct_answer_explanation"]
    base["misconception_summary"] = llm["misconception_summary"]
    base["related_kg_node_ids"] = llm["related_kg_node_ids"]
    base["recommendation"]["remedial_suggestion"] = llm.get("remedial_suggestion")
    base["fallback_rule_based"] = False
    base["content_status"] = "llm_generated"
    base["trace"]["mode"] = "llm_assessment"
    base["trace"]["uses_llm"] = True
    base["trace"]["llm_attempted"] = True
    base["trace"]["llm_succeeded"] = True
    base["trace"]["llm_seconds"] = llm.get("_llm_seconds", 0)
    return base


def _unsupported_result(
    question_id: str,
    node_id: str,
    user_answer: str,
    normalized: str,
    correct_answer: str,
    q_type: str,
) -> dict[str, Any]:
    """Build result for unsupported question types."""
    return {
        "question_id": question_id,
        "outline_node_id": node_id,
        "user_answer": user_answer,
        "normalized_user_answer": normalized,
        "selected_option": normalized,
        "correct_option": correct_answer.upper(),
        "is_correct": False,
        "score": 0.0,
        "mastery_delta": 0.0,
        "mistake_tags": ["暂不支持题型"],
        "explanation": f"当前暂不支持题型「{q_type}」的自动评估。",
        "option_error_analysis": None,
        "correct_answer_explanation": None,
        "misconception_summary": None,
        "related_kg_node_ids": [],
        "recommendation": {
            "action": "reinforce",
            "reason": f"题型 {q_type} 暂不支持自动评分",
            "remedial_suggestion": None,
            "target_outline_node_id": node_id,
        },
        "fallback_rule_based": True,
        "content_status": "rule_based",
        "trace": {
            "mode": "rule_based_assessment",
            "uses_llm": False,
            "supports_subjective_grading": False,
            "llm_attempted": False,
        },
    }


# ============================================================================
# Internal helpers
# ============================================================================


def _has_valid_content(handout_section: dict[str, Any] | None) -> bool:
    """Check if handout_section has valid content for LLM context."""
    if not handout_section or not isinstance(handout_section, dict):
        return False
    if handout_section.get("section_status") != "llm_generated":
        return False
    content = handout_section.get("content", {})
    if not isinstance(content, dict):
        return False
    key_points = content.get("key_points", [])
    return isinstance(key_points, list) and len(key_points) > 0


def _build_handout_summary_for_assessment(
    handout_section: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build a lightweight handout summary for the assessment LLM prompt."""
    if not handout_section:
        return {}
    content = handout_section.get("content", handout_section)
    if not isinstance(content, dict):
        return {"note": "handout content not available"}
    key_points = content.get("key_points", [])
    if not isinstance(key_points, list):
        key_points = []
    return {
        "title": handout_section.get("title", ""),
        "overview": str(content.get("overview", ""))[:800],
        "key_points_headings": [
            kp.get("heading", "") for kp in key_points if isinstance(kp, dict)
        ],
        "key_points_count": len(key_points),
        "has_clinical_example": bool(content.get("clinical_example", "")),
        "has_common_confusions": bool(content.get("common_confusions", [])),
        "common_confusions": [
            {"confusion": cc.get("confusion", ""), "clarification": str(cc.get("clarification", ""))[:400]}
            for cc in content.get("common_confusions", [])
            if isinstance(cc, dict)
        ],
    }


def _load_prompt(prompt_dir: Path, filename: str) -> str:
    """Load a prompt file, or return a minimal fallback."""
    prompt_path = prompt_dir / filename
    if prompt_path.exists():
        return prompt_path.read_text(encoding="utf-8")
    return "你是一名口腔医学学习 Assessment Agent。请基于提供的材料输出严格 JSON 格式的错题分析结果。"


def _parse_json_output(raw: str) -> dict | None:
    """Parse LLM JSON output, handling markdown code fences."""
    text = raw.strip()
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if not fence_match:
        fence_match = re.search(r"```(?:json)?\s*\n?(.*)$", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _normalize_str_list(value: Any) -> list[str]:
    """Normalize a value to a list of non-empty strings."""
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]
