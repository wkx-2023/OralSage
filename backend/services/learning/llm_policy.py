"""LLM Policy Agent (dry-run) — reads learning state + user input, outputs a
structured decision dict that passes schema validation.

This module does **not** execute actions, write to the database, or modify
any agent state.  It is purely a decision layer that can replace the default
rule-based ``decide_next_action`` when an LLM is available.

When no LLM is configured, or when the LLM response is invalid, the module
falls back to the provided fallback policy (typically the rule-based one).
"""

from __future__ import annotations

import json
from typing import Any, Callable

from backend.services.llm_client import chat_completion, is_llm_configured

# ---------------------------------------------------------------------------
# Valid action vocabulary
# ---------------------------------------------------------------------------

_VALID_ACTIONS = frozenset({
    "teach_current_node",
    "answer_interruption",
    "assess_answer",
    "continue_to_next_section",
    "unknown_input",
    "lesson_completed",
})

_VALID_TARGET_AGENTS = frozenset({
    "tutor",
    "qa",
    "assessment",
    "orchestrator",
    "none",
})

_VALID_ACTION_TYPES = frozenset({
    "agent_call",
    "terminal_state",
    "clarification",
})

# ---------------------------------------------------------------------------
# Configuration check
# ---------------------------------------------------------------------------


def is_learning_policy_llm_configured() -> bool:
    """Return True if an LLM API key is available for the learning policy agent."""
    return is_llm_configured(
        "LEARNING_POLICY_LLM_API_KEY",
        "QWEN_API_KEY",
        "DASHSCOPE_API_KEY",
    )


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------


def build_policy_prompt(
    state: dict[str, Any],
    user_input: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Build the messages list for the LLM policy decision call.

    The system prompt instructs the model to output **only** JSON (no
    markdown fences) with the ``policy.v1`` decision schema.
    """
    # Detect raw_text mode
    is_raw_text = (
        user_input is not None
        and isinstance(user_input, dict)
        and user_input.get("type") == "raw_text"
    )

    if is_raw_text:
        raw_text = user_input.get("raw_text", "")
        latest_question = user_input.get("latest_question")

        system_prompt = (
            "You are a learning orchestrator policy agent for a dental/medical "
            "education platform called OralSage.\n\n"
            "Your ONLY job is to decide which action the system should take next "
            "based on the user's natural-language input. You MUST NOT generate "
            "teaching content, explanations, or any other text.\n\n"
            "## Available actions\n\n"
            "| action | target_agent | when to use |\n"
            "|---|---|---|\n"
            "| teach_current_node | tutor | user says 继续/下一步/continue/讲下去/next — teach current node |\n"
            "| answer_interruption | qa | user asks a question (为什么/怎么/区别/不懂/什么意思/解释) — do NOT advance progress |\n"
            "| assess_answer | assessment | user submits an answer (A/B/C/D, 选B, 答案是B, 我选B etc.) |\n"
            "| continue_to_next_section | orchestrator | explicit continue / next-section button — advance to next outline node and then teach it |\n"
            "| unknown_input | orchestrator | learning preference/control (换种讲法/考试重点/简单点/详细点/太快了) or unclear intent |\n"
            "| lesson_completed | none | no current outline node and no user input — session is done |\n\n"
            "## Output format\n\n"
            "Return EXACTLY one JSON object with these fields:\n"
            '  "schema_version": "policy.v1"\n'
            '  "action_type": "agent_call" (or "clarification" for unknown_input, "terminal_state" for lesson_completed)\n'
            '  "action": one of the actions above\n'
            '  "target_agent": matching agent from the table\n'
            '  "payload": object with these sub-fields:\n'
            '    - for assess_answer: {{"input_type":"answer_submission","answer":"B"}}\n'
            '    - for answer_interruption: {{"input_type":"interruption_question","text":"<original question>"}}\n'
            '    - for teach_current_node: {{}}\n'
            '    - for continue_to_next_section: {{"input_type":"continue"}}\n'
            '    - for unknown_input: {{"input_type":"learning_preference_or_control","raw_text":"<original input>"}}\n'
            '    - for lesson_completed: {{}}\n'
            '  "reason": short Chinese explanation (1 sentence)\n'
            '  "requires_user_input": true/false\n'
            '  "advance_progress_allowed": true/false (MUST be false for answer_interruption!)\n'
            '  "trace": {{"mode":"llm_orchestrator_policy","uses_llm":true}}\n\n'
            "DO NOT wrap the JSON in markdown code fences. DO NOT add any text "
            "before or after the JSON object."
        )

        user_prompt_parts = [
            "## Current policy state\n",
            json.dumps(state, ensure_ascii=False, indent=2),
            "\n## User raw_text input\n\n",
            raw_text,
        ]
        if latest_question:
            user_prompt_parts.extend([
                "\n\n## Latest question (for answer context)\n\n",
                json.dumps(latest_question, ensure_ascii=False, indent=2),
            ])
        user_prompt_parts.append(
            "\n\nClassify the user's intent and return ONLY the JSON decision object."
        )

        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "".join(user_prompt_parts)},
        ]

    # --- Standard (non-raw_text) prompt -----------------------------------
    system_prompt = (
        "You are a learning orchestrator policy agent for a dental/medical "
        "education platform called OralSage.\n\n"
        "Your ONLY job is to decide which action the system should take next "
        "in a learning session. You MUST NOT generate teaching content, "
        "explanations, or any other text.\n\n"
        "## Available actions\n\n"
        "| action | target_agent | when to use |\n"
        "|---|---|---|\n"
        "| teach_current_node | tutor | no user input and a current outline node exists — teach it |\n"
        "| answer_interruption | qa | user asks a mid-lecture question (type=interruption_question) — do NOT advance progress |\n"
        "| assess_answer | assessment | user submitted an answer to a question (type=answer_submission) |\n"
        "| continue_to_next_section | orchestrator | user explicitly clicks continue / next section (type=continue) |\n"
        "| unknown_input | orchestrator | input type is not recognised — ask for clarification |\n"
        "| lesson_completed | none | no current outline node and no user input — session is done |\n\n"
        "## Output format\n\n"
        "Return EXACTLY one JSON object with these fields:\n"
        '  "schema_version": "policy.v1"\n'
        '  "action_type": one of "agent_call" | "terminal_state" | "clarification"\n'
        '  "action": one of the actions above\n'
        '  "target_agent": matching agent from the table\n'
        '  "payload": {{}} — empty object (or with input_type for interruption/answer)\n'
        '  "reason": short Chinese explanation (1 sentence)\n'
        '  "requires_user_input": true/false\n'
        '  "advance_progress_allowed": true/false (MUST be false for interruption_question!)\n'
        '  "trace": {{"mode": "llm_orchestrator_policy", "uses_llm": true}}\n\n'
        "DO NOT wrap the JSON in markdown code fences. DO NOT add any text "
        "before or after the JSON object."
    )

    user_prompt_parts = [
        "## Current policy state\n",
        json.dumps(state, ensure_ascii=False, indent=2),
    ]

    if user_input is not None:
        user_prompt_parts.append(
            "\n## User input\n\n" + json.dumps(user_input, ensure_ascii=False, indent=2)
        )
    else:
        user_prompt_parts.append("\n## User input\n\nnull (no user input)")

    user_prompt_parts.append(
        "\n\nReturn ONLY the JSON decision object for the next action."
    )

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "".join(user_prompt_parts)},
    ]


# ---------------------------------------------------------------------------
# Decision validator
# ---------------------------------------------------------------------------


def validate_policy_decision(decision: dict[str, Any]) -> tuple[bool, str]:
    """Validate a decision dict against the policy.v1 schema.

    Returns
    -------
    (is_valid, error_message)
        *is_valid* is ``True`` when the decision passes all checks.
        *error_message* is an empty string on success, or a human-readable
        reason on failure.
    """
    # --- schema_version ---------------------------------------------------
    if decision.get("schema_version") != "policy.v1":
        return False, f"schema_version must be 'policy.v1', got {decision.get('schema_version')!r}"

    # --- action_type ------------------------------------------------------
    action_type = decision.get("action_type", "")
    if action_type not in _VALID_ACTION_TYPES:
        return False, f"action_type must be one of {sorted(_VALID_ACTION_TYPES)}, got {action_type!r}"

    # --- action -----------------------------------------------------------
    action = decision.get("action", "")
    if action not in _VALID_ACTIONS:
        return False, f"action must be one of {sorted(_VALID_ACTIONS)}, got {action!r}"

    # --- target_agent -----------------------------------------------------
    target_agent = decision.get("target_agent", "")
    if target_agent not in _VALID_TARGET_AGENTS:
        return False, f"target_agent must be one of {sorted(_VALID_TARGET_AGENTS)}, got {target_agent!r}"

    # --- payload ----------------------------------------------------------
    payload = decision.get("payload")
    if not isinstance(payload, dict):
        return False, f"payload must be a dict, got {type(payload).__name__}"

    # --- trace ------------------------------------------------------------
    trace = decision.get("trace")
    if not isinstance(trace, dict):
        return False, f"trace must be a dict, got {type(trace).__name__}"

    if "uses_llm" not in trace:
        return False, "trace must include 'uses_llm' field"

    # --- semantic constraint: interruption must not advance ---------------
    if action == "answer_interruption" and decision.get("advance_progress_allowed") is True:
        return False, "advance_progress_allowed must be False for answer_interruption"

    # --- payload semantic checks (non-blocking for backward compat) --------
    if action == "assess_answer":
        ptype = payload.get("input_type", "")
        if ptype == "answer_submission" and "answer" not in payload:
            return False, "payload.answer is required when action=assess_answer and input_type=answer_submission"

    if action == "answer_interruption":
        ptype = payload.get("input_type", "")
        if ptype == "interruption_question" and "text" not in payload:
            return False, "payload.text is required when action=answer_interruption and input_type=interruption_question"

    # --- requires_user_input must be present ------------------------------
    if "requires_user_input" not in decision:
        return False, "requires_user_input field is required"

    return True, ""


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def decide_next_action_with_llm(
    state: dict[str, Any],
    user_input: dict[str, Any] | None = None,
    fallback_policy: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Produce a next-action decision, preferring the LLM when available.

    This is a **dry-run** decision function — it does not execute anything,
    write to the database, or modify state.

    Parameters
    ----------
    state:
        Policy state dict (from ``LearningOrchestrator.get_policy_state``).
    user_input:
        Optional user input dict.
    fallback_policy:
        Callable ``(state, user_input) -> decision`` used when the LLM is
        unavailable or produces invalid output.  Typically
        ``decide_next_action`` from the rule-based policy module.

    Returns
    -------
    dict
        A valid policy.v1 decision dict.  The ``trace`` field indicates
        whether the decision came from the LLM or the fallback.
    """
    # --- No LLM → fallback -------------------------------------------------
    if not is_learning_policy_llm_configured():
        if fallback_policy is None:
            raise ValueError(
                "LLM is not configured and no fallback_policy was provided"
            )
        decision = fallback_policy(state, user_input)
        decision.setdefault("trace", {})
        decision["trace"]["llm_policy_status"] = "skipped_no_llm"
        return decision

    # --- LLM configured → try it -------------------------------------------
    messages = build_policy_prompt(state, user_input)
    try:
        raw_response = chat_completion(
            messages,
            temperature=0.1,
            max_tokens=600,
            api_key_names=(
                "LEARNING_POLICY_LLM_API_KEY",
                "QWEN_API_KEY",
                "DASHSCOPE_API_KEY",
            ),
        )
    except Exception:
        # API error / network / timeout → fallback
        if fallback_policy is None:
            raise
        decision = fallback_policy(state, user_input)
        decision.setdefault("trace", {})
        decision["trace"]["llm_policy_status"] = "fallback_exception"
        return decision

    # --- Parse JSON --------------------------------------------------------
    try:
        parsed = _extract_json(raw_response)
    except (json.JSONDecodeError, ValueError):
        if fallback_policy is None:
            raise ValueError("LLM returned unparseable output and no fallback_policy was provided")
        decision = fallback_policy(state, user_input)
        decision.setdefault("trace", {})
        decision["trace"]["llm_policy_status"] = "fallback_invalid_llm_output"
        return decision

    # --- Validate schema ---------------------------------------------------
    valid, error = validate_policy_decision(parsed)
    if not valid:
        if fallback_policy is None:
            raise ValueError(f"LLM decision failed validation: {error}")
        decision = fallback_policy(state, user_input)
        decision.setdefault("trace", {})
        decision["trace"]["llm_policy_status"] = "fallback_invalid_llm_output"
        return decision

    # --- Valid LLM decision ------------------------------------------------
    parsed["trace"]["mode"] = "llm_orchestrator_policy"
    parsed["trace"]["uses_llm"] = True
    parsed["trace"]["llm_policy_status"] = "llm_decision"
    return parsed


# ---------------------------------------------------------------------------
# Debug entry point — returns internal details for diagnostic display
# ---------------------------------------------------------------------------


def decide_next_action_with_llm_debug(
    state: dict[str, Any],
    user_input: dict[str, Any] | None = None,
    fallback_policy: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Same logic as :func:`decide_next_action_with_llm`, but returns a
    debug dict that includes the prompt messages, raw LLM response, and
    validation details — useful for diagnostic terminals and demo scripts.

    **Does NOT** execute actions, write to the database, or leak API keys.

    Returns
    -------
    dict
        Keys: ``decision``, ``messages``, ``raw_response``,
        ``validation`` (with ``valid`` and ``error``), ``fallback_used``,
        ``fallback_reason``.
    """
    messages: list[dict[str, str]] | None = None
    raw_response: str | None = None
    validation: dict[str, Any] = {"valid": False, "error": ""}
    fallback_used = False
    fallback_reason: str | None = None

    # --- No LLM → fallback -------------------------------------------------
    if not is_learning_policy_llm_configured():
        if fallback_policy is None:
            raise ValueError(
                "LLM is not configured and no fallback_policy was provided"
            )
        decision = fallback_policy(state, user_input)
        decision.setdefault("trace", {})
        decision["trace"]["llm_policy_status"] = "skipped_no_llm"
        validation["valid"], validation["error"] = validate_policy_decision(decision)
        fallback_used = True
        fallback_reason = "skipped_no_llm"
        return {
            "decision": decision,
            "messages": None,
            "raw_response": None,
            "validation": validation,
            "fallback_used": fallback_used,
            "fallback_reason": fallback_reason,
        }

    # --- LLM configured → try it -------------------------------------------
    messages = build_policy_prompt(state, user_input)
    try:
        raw_response = chat_completion(
            messages,
            temperature=0.1,
            max_tokens=600,
            api_key_names=(
                "LEARNING_POLICY_LLM_API_KEY",
                "QWEN_API_KEY",
                "DASHSCOPE_API_KEY",
            ),
        )
    except Exception as exc:
        if fallback_policy is None:
            raise
        decision = fallback_policy(state, user_input)
        decision.setdefault("trace", {})
        decision["trace"]["llm_policy_status"] = "fallback_exception"
        validation["valid"], validation["error"] = validate_policy_decision(decision)
        fallback_used = True
        fallback_reason = f"fallback_exception: {exc}"
        return {
            "decision": decision,
            "messages": messages,
            "raw_response": None,
            "validation": validation,
            "fallback_used": fallback_used,
            "fallback_reason": fallback_reason,
        }

    # --- Parse JSON --------------------------------------------------------
    try:
        parsed = _extract_json(raw_response)
    except (json.JSONDecodeError, ValueError) as exc:
        if fallback_policy is None:
            raise ValueError(
                "LLM returned unparseable output and no fallback_policy was provided"
            )
        decision = fallback_policy(state, user_input)
        decision.setdefault("trace", {})
        decision["trace"]["llm_policy_status"] = "fallback_invalid_llm_output"
        validation["valid"], validation["error"] = validate_policy_decision(decision)
        fallback_used = True
        fallback_reason = f"json_parse_error: {exc}"
        return {
            "decision": decision,
            "messages": messages,
            "raw_response": raw_response,
            "validation": validation,
            "fallback_used": fallback_used,
            "fallback_reason": fallback_reason,
        }

    # --- Validate schema ---------------------------------------------------
    valid, error = validate_policy_decision(parsed)
    validation["valid"] = valid
    validation["error"] = error

    if not valid:
        if fallback_policy is None:
            raise ValueError(f"LLM decision failed validation: {error}")
        decision = fallback_policy(state, user_input)
        decision.setdefault("trace", {})
        decision["trace"]["llm_policy_status"] = "fallback_invalid_llm_output"
        fallback_used = True
        fallback_reason = f"validation_failed: {error}"
        return {
            "decision": decision,
            "messages": messages,
            "raw_response": raw_response,
            "validation": validation,
            "fallback_used": fallback_used,
            "fallback_reason": fallback_reason,
        }

    # --- Valid LLM decision ------------------------------------------------
    parsed["trace"]["mode"] = "llm_orchestrator_policy"
    parsed["trace"]["uses_llm"] = True
    parsed["trace"]["llm_policy_status"] = "llm_decision"

    return {
        "decision": parsed,
        "messages": messages,
        "raw_response": raw_response,
        "validation": validation,
        "fallback_used": False,
        "fallback_reason": None,
    }


def _extract_json(raw: str) -> dict[str, Any]:
    """Extract a JSON object from *raw*, stripping markdown fences if present."""
    text = raw.strip()
    # Remove markdown code fences if present
    if text.startswith("```"):
        # Find the end of the opening fence line
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    return json.loads(text)  # type: ignore[no-any-return]
