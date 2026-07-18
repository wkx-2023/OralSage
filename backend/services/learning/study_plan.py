"""Study-plan service for calendar to-do tracking and memory feedback."""

from __future__ import annotations

import copy
import json
import logging
import re
import secrets
import threading
import time
from calendar import monthrange
from datetime import date, timedelta
from typing import Any, Callable

from .memory import LearningMemory
from .planner import preflight_scope_resources
from .profile_memory import reduce_memory_signals

STUDY_PLAN_SESSION_ID = "study_plan"

# ---------------------------------------------------------------------------
# Semantic plan-request parsing from natural language
# ---------------------------------------------------------------------------

_ALLOWED_INTENTS = {"learn_chapter", "review_chapter", "review_weakness", "custom"}

_ALLOWED_FIELDS = {
    "intent",
    "days",
    "daily_minutes",
    "focus_text",
    "book_title",
    "document_id",
    "chapter_key",
}


class PlanRequestParseError(ValueError):
    """Raised when semantic parsing of a natural-language plan request fails.

    Attributes
    ----------
    code : str
        A stable, machine-readable error code that callers can switch on.
        Possible values:

        * ``"empty_message"`` — the input message is empty or whitespace-only
        * ``"llm_not_configured"`` — no LLM API key is available
        * ``"llm_request_failed"`` — the LLM call itself failed (network, etc.)
        * ``"invalid_json"`` — the LLM output could not be parsed as JSON
        * ``"missing_intent"`` — the JSON output is missing the ``intent`` field
        * ``"invalid_intent"`` — the ``intent`` value is not one of the allowed four
        * ``"invalid_field_type"`` — a field has the wrong type
        * ``"chapter_not_found"`` — the ``chapter_key`` does not match any candidate
        * ``"chapter_mismatch"`` — ``book_title`` or ``document_id`` is inconsistent
          with the matched candidate, or ``chapter_key`` is non-null while
          ``book_title`` / ``document_id`` is null
        * ``"missing_chapter_key"`` — ``learn_chapter`` intent without a valid
          ``chapter_key``
        * ``"unexpected_field"`` — the LLM output contains a field not in the
          allowed set
        * ``"invalid_normalized_request"`` — :func:`normalize_plan_request`
          raised a :exc:`ValueError` on the parsed output
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ScopeSelectorParseError(ValueError):
    """Raised when the unified scope selector cannot be parsed or validated.

    Attributes
    ----------
    code : str
        A stable, machine-readable error code.  Possible values:

        * ``"empty_message"`` — the input message is empty or whitespace-only
        * ``"llm_not_configured"`` — no LLM API key is available
        * ``"llm_request_failed"`` — the LLM call itself failed
        * ``"invalid_json"`` — the LLM output could not be parsed as JSON
        * ``"invalid_target_type"`` — target_type is not an allowed value
        * ``"invalid_operation"`` — operation is not an allowed value
        * ``"missing_required_field"`` — a required field is absent
        * ``"invalid_field_type"`` — a field has the wrong type
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Unified scope selector constants
# ---------------------------------------------------------------------------

_ALLOWED_SCOPE_TARGET_TYPES = frozenset({
    "book",
    "chapter",
    "chapter_set",
    "knowledge_point",
    "disease",
    "weakness_profile",
    "unknown",
})

_ALLOWED_SCOPE_OPERATIONS = frozenset({
    "all",
    "first_n",
    "last_n",
    "range",
    "list",
    "single",
    "exclude",
})

_SCOPE_SELECTOR_REQUIRED_FIELDS = frozenset({
    "target_type",
    "mention",
    "operation",
    "context_dependent",
})

_SCOPE_SELECTOR_OPTIONAL_FIELDS = frozenset({
    "parent_book_title",
    "ordinals",
    "titles",
    "scope_ids",
    "range_start",
    "range_end",
    "exclude_ordinals",
    "resolved_book_title",
    "resolved_document_id",
    # Plan decorator fields — the unified selector also extracts plan
    # parameters so that a single LLM call handles both scope selection
    # and plan request normalisation.
    "intent",
    "days",
    "daily_minutes",
    "focus_text",
    # Revision constraints — LLM-interpreted natural-language modification
    # semantics as typed, measurable comparison constraints.
    # Only present when the user is modifying an existing plan.
    "revision_constraints",
})

# ── Revision constraint allowed values ──────────────────────────────────
# These are the ONLY values the LLM may use for each constraint field.
# The validator enforces them — anything outside this set is rejected.
_ALLOWED_REVISION_COUNT_RELATIONS = frozenset({
    "decrease", "maintain", "increase", "unspecified",
})
_ALLOWED_REVISION_SCOPE_RELATIONS = frozenset({
    "maintain", "narrow", "expand", "unspecified",
})
# Allowed top-level keys in revision_constraints — anything extra is
# rejected to prevent LLM invention of unverifiable fields.
_ALLOWED_REVISION_CONSTRAINT_KEYS = frozenset({
    "scope_relation",
    "practice_count_relation",
    "learn_count_relation",
    "review_count_relation",
    "daily_minutes_max",
})

_ALLOWED_SCOPE_SELECTOR_FIELDS = (
    _SCOPE_SELECTOR_REQUIRED_FIELDS | _SCOPE_SELECTOR_OPTIONAL_FIELDS
)


# ---------------------------------------------------------------------------
# Default LLM callable factory
# ---------------------------------------------------------------------------


def _make_default_llm_call(
    max_tokens: int = 600,
    json_mode: bool = False,
    _compat_state: dict[str, Any] | None = None,
) -> Callable[[str], str]:
    """Build a default LLM callable from the project's ``llm_client``.

    Returns a ``(prompt: str) -> str`` callable backed by
    :func:`backend.services.llm_client.chat_completion`.

    Parameters
    ----------
    max_tokens:
        Maximum tokens in the LLM response.  Use a higher value for
        orchestration calls (which can produce multi-item plans).
    json_mode:
        When ``True``, sets ``response_format={"type": "json_object"}``
        to request structured JSON output from the provider.  If the
        JSON-mode response is empty or non-string, the callable retries
        **once** with the same model and prompt but **without**
        ``response_format`` — this works around providers that return
        empty content when ``response_format`` is set (e.g. DeepSeek).
    _compat_state:
        Optional shared mutable dict that tracks JSON-mode compatibility
        across the two stages of a single plan flow (parse →
        orchestration).  When the parse stage determines that this
        provider does **not** support ``response_format``, it writes
        ``{"json_mode_supported": False}`` so the orchestration stage
        can skip the probe entirely.  Callers that do not need
        cross-stage sharing can omit this argument.

        **Keys that callers may read after the call completes:**

        * ``"json_mode_supported"`` — ``True`` (probe succeeded),
          ``False`` (probe failed — timeout / empty / unsupported), or
          absent / ``None`` (no probe ran because *json_mode* was
          ``False``).

        The dict is never consumed or mutated outside this function.

    Raises
    ------
    LLMConfigurationError
        If no LLM API key is configured.
    """
    import logging as _logging
    _logger = _logging.getLogger(__name__)

    from backend.services.llm_client import (
        LLMConfigurationError,
        LLMRequestError,
        chat_completion,
        classify_llm_error,
        is_llm_configured,
        _json_probe_timeout_seconds,
    )

    if not is_llm_configured():
        raise LLMConfigurationError("No LLM API key configured")

    def _call(prompt: str) -> str:
        # ── 0. Determine effective json_mode from compat_state ──────────
        effective_json_mode = json_mode
        if (
            effective_json_mode
            and _compat_state is not None
            and _compat_state.get("json_mode_supported") is False
        ):
            # A prior stage already determined JSON mode is unsupported
            effective_json_mode = False
            _logger.info(
                "Skipping JSON-mode probe — compat_state reports "
                "json_mode_supported=False. max_tokens=%s, prompt_len=%s",
                max_tokens,
                len(prompt),
            )

        # ── 1. JSON-mode path with bounded probe / retry ────────────────
        if effective_json_mode:
            result: str = ""
            probe_error_cat: str | None = None

            try:
                result = chat_completion(
                    [{"role": "user", "content": prompt}],
                    temperature=0.1,
                    max_tokens=max_tokens,
                    response_format={"type": "json_object"},
                    timeout=_json_probe_timeout_seconds(),
                )
            except LLMRequestError as exc:
                probe_error_cat = classify_llm_error(exc)
                if probe_error_cat in ("timeout", "unsupported", "network"):
                    # These categories indicate the provider may not
                    # support JSON mode — retry once without it.
                    _logger.warning(
                        "JSON-mode probe failed (category=%s); "
                        "retrying without response_format. "
                        "max_tokens=%s, prompt_len=%s",
                        probe_error_cat,
                        max_tokens,
                        len(prompt),
                    )
                elif probe_error_cat in ("auth",):
                    # Auth / permission errors are not JSON-mode issues.
                    # Do NOT blindly retry — let the error propagate.
                    raise
                else:
                    # http_client_error, http_server_error, unknown:
                    # don't retry — the problem is unlikely to be
                    # JSON-mode-related.
                    _logger.warning(
                        "JSON-mode probe failed (category=%s); "
                        "NOT retrying — error is unlikely to be "
                        "JSON-mode-related. max_tokens=%s, prompt_len=%s",
                        probe_error_cat,
                        max_tokens,
                        len(prompt),
                    )
                    raise
            except Exception:
                # Non-LLM exceptions (e.g. LLMConfigurationError) are
                # never retried — they signal a permanent setup issue.
                raise

            # Successful probe — update compat_state
            if probe_error_cat is None:
                if result and isinstance(result, str) and result.strip():
                    if _compat_state is not None:
                        _compat_state["json_mode_supported"] = True
                    return result

                # Empty response — log and fall through to retry
                _logger.warning(
                    "JSON-mode response empty; retrying without "
                    "response_format. max_tokens=%s, prompt_len=%s",
                    max_tokens,
                    len(prompt),
                )
                probe_error_cat = "empty"

            # ── 1a. Probe failed → retry once without response_format ──
            if _compat_state is not None:
                _compat_state["json_mode_supported"] = False

            result = chat_completion(
                [{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=max_tokens,
            )

            if result and isinstance(result, str) and result.strip():
                return result

            # Retry also returned empty — two bounded recovery attempts before
            # giving up: (a) increase max_tokens when the prompt is long,
            # (b) strip down the candidate summary for a compact retry.
            # Neither changes the model, prompt semantics, or provider.
            _logger.warning(
                "JSON-mode retry (no response_format) also returned empty. "
                "max_tokens=%s, prompt_len=%s, probe_error=%s",
                max_tokens,
                len(prompt),
                probe_error_cat,
            )

            # ── 1b. Bounded max-tokens increase -------------------------
            if max_tokens < 3000 and len(prompt) > 4000:
                increased = max(max_tokens * 4, 3200)
                increased = min(increased, 8192)
                _logger.warning(
                    "Empty after JSON retry; retrying once with "
                    "increased max_tokens=%s (was %s). prompt_len=%s",
                    increased, max_tokens, len(prompt),
                )
                result = chat_completion(
                    [{"role": "user", "content": prompt}],
                    temperature=0.1,
                    max_tokens=increased,
                )
                if result and isinstance(result, str) and result.strip():
                    return result
                _logger.warning(
                    "Increased-tokens retry also returned empty. "
                    "max_tokens=%s, prompt_len=%s",
                    increased, len(prompt),
                )

            return result

        # ── 2. Non-JSON mode: single call + bounded max-tokens retry ───
        result = chat_completion(
            [{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=max_tokens,
        )

        if result and isinstance(result, str) and result.strip():
            return result

        # Empty response — log and attempt bounded max-tokens increase
        _logger.warning(
            "Non-JSON response empty. "
            "max_tokens=%s, prompt_len=%s",
            max_tokens, len(prompt),
        )

        if max_tokens < 3000 and len(prompt) > 4000:
            increased = max(max_tokens * 4, 3200)
            increased = min(increased, 8192)
            _logger.warning(
                "Retrying once with increased max_tokens=%s (was %s). "
                "prompt_len=%s",
                increased, max_tokens, len(prompt),
            )
            result = chat_completion(
                [{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=increased,
            )
            if result and isinstance(result, str) and result.strip():
                return result
            _logger.warning(
                "Increased-tokens retry also returned empty. "
                "max_tokens=%s, prompt_len=%s",
                increased, len(prompt),
            )

        return result

    return _call


# ---------------------------------------------------------------------------
# Prompt construction helpers
# ---------------------------------------------------------------------------


def _build_chapter_candidate_text(chapters: list[dict[str, Any]]) -> str:
    """Build a compact text table of chapter candidates for the LLM prompt."""
    if not chapters:
        return "(none)"

    lines: list[str] = []
    for i, ch in enumerate(chapters, 1):
        if not isinstance(ch, dict):
            continue
        key = str(ch.get("key", "")).strip()
        if not key:
            continue
        book = str(ch.get("book_title", "")).strip()
        doc = str(ch.get("document_id", "")).strip()
        title = str(ch.get("title", "")).strip()
        lines.append(
            f"  [{i}] key={key!r}  book_title={book!r}  "
            f"document_id={doc!r}  title={title!r}"
        )

    return "\n".join(lines) if lines else "(none)"


def _is_explicit_whole_book_request(message: str) -> bool:
    """Return whether the user explicitly selected complete-book coverage."""
    text = re.sub(r"\s+", "", str(message or ""))
    markers = (
        "整本", "全书", "全本", "全部章节", "所有章节",
        "全部知识点", "所有知识点", "从头到尾", "完整学完",
        "系统学完", "系统复习完", "通读",
    )
    return any(marker in text for marker in markers)


def _stabilize_explicit_whole_book_intent(
    message: str,
    selector: dict[str, Any],
) -> None:
    """Keep an explicit whole-book plan out of the generic custom path.

    ``custom`` is appropriate when the learning action is genuinely unclear,
    but phrases such as "为整本《牙周病学》制定学习计划" already express a
    complete-book learning request.  Some models interpret "制定计划" as a
    custom action, which unnecessarily sends a first-time request through the
    less reliable free-form orchestration path.

    The correction is deliberately narrow: it only applies to a resolved book
    selector with explicit whole-book wording, and it never overwrites an
    already specific learning, review, or weakness intent.
    """
    if selector.get("target_type") != "book":
        return
    if selector.get("intent") != "custom":
        return
    if not _is_explicit_whole_book_request(message):
        return

    text = re.sub(r"\s+", "", str(message or "")).lower()
    review_markers = ("复习", "回顾", "重温", "巩固", "备考")
    selector["intent"] = (
        "review_chapter"
        if any(marker in text for marker in review_markers)
        else "learn_chapter"
    )


def _book_scope_clarification_options(book_title: str) -> list[dict[str, str]]:
    title = str(book_title or "").strip()
    quoted = f"《{title}》" if title else "该教材"
    return [
        {
            "label": "按薄弱点安排",
            "prompt": "请根据我的真实薄弱点制定复习计划",
        },
        {
            "label": "填写具体章节",
            "fill": f"请为{quoted}第__章制定学习计划",
        },
        {
            "label": "规划整本教材",
            "prompt": f"请为整本{quoted}制定学习计划",
        },
    ]


def _build_parse_prompt(
    message: str,
    candidates_text: str,
    history: list[dict[str, Any]] | None = None,
) -> str:
    """Build the LLM system+user prompt for parsing a plan request."""
    history_text = ""
    if history:
        recent = history[-6:]
        history_lines = []
        for entry in recent:
            role = entry.get("role", "user")
            content = str(entry.get("content", "")).strip()
            if content:
                history_lines.append(f"{role}: {content[:300]}")
        if history_lines:
            history_text = (
                "\nRecent conversation history (for context — the current "
                "message may be a short response to a prior question):\n"
                + "\n".join(history_lines)
                + "\n"
            )

    return (
        "You are a study plan parser. Given a user's natural language request "
        "and available textbook chapters, output a SINGLE JSON object with "
        "exactly these fields:\n\n"
        '- intent: one of "learn_chapter", "review_chapter", '
        '"review_weakness", "custom"\n'
        "- days: integer (1-90), days to study. "
        "If the user does not specify, default to 7.\n"
        "- daily_minutes: integer (5-240), minutes per day. "
        "If the user does not specify, default to 30.\n"
        "- focus_text: string or null — the user's stated focus area "
        "in their own words. null if not mentioned.\n"
        "- book_title: string or null — MUST be copied VERBATIM from a "
        "candidate below. NEVER invent.\n"
        "- document_id: string or null — MUST be copied VERBATIM from a "
        "candidate below. NEVER invent.\n"
        "- chapter_key: string or null — MUST be copied VERBATIM from a "
        "candidate below. NEVER invent.\n"
        "\n"
        "CRITICAL RULES:\n"
        "1. chapter_key, book_title, document_id MUST be copied VERBATIM "
        "(character-for-character) from the candidates below. "
        "NEVER invent, guess, translate, or modify these values.\n"
        "2. If no candidate matches what the user wants, set chapter_key, "
        "book_title, AND document_id ALL to null.\n"
        "3. If the user asks to learn a specific chapter, set intent to "
        '"learn_chapter" and chapter_key MUST come from a matching candidate.\n'
        "4. If the user asks to review a specific chapter, set intent to "
        '"review_chapter" and chapter_key MUST come from a matching candidate.\n'
        "5. If the user asks to review weaknesses or make a plan based on "
        'their weaknesses, set intent to "review_weakness".\n'
        "6. If the user's request does not clearly map to any specific "
        'chapter or weakness, set intent to "custom".\n'
        "7. focus_text should capture the user's stated focus in their own "
        "words (e.g. the topic or key phrase they mentioned), or null if "
        "nothing specific was stated.\n"
        "8. Output ONLY the JSON object — no markdown fences, no extra text.\n"
        "9. If the user EXPLICITLY asks to learn or review an ENTIRE book "
        "(e.g. '学习整本牙周病学', '复习全书', '系统学完整本牙体牙髓病学'), "
        "set chapter_key to null but copy book_title and document_id "
        "VERBATIM from any candidate of that book. "
        "Do NOT invent or pick a random chapter_key for whole-book requests.\n"
        "10. IMPORTANT: If the conversation history shows a prior system "
        "question asking the user to specify a textbook/chapter/topic, "
        "and the current user message appears to be a direct answer "
        "(e.g. just a book name or chapter title), resolve it against the "
        "candidates below. Treat the user's answer as the scope for the "
        "plan they previously requested.\n"
        "10a. BOOK-NAME-ONLY RULE: A matching book name without a chapter "
        "or an explicit whole-book phrase identifies the textbook but does "
        "NOT authorize selecting every chapter. Keep chapter_key null and "
        "copy book_title + document_id VERBATIM; the resolver will ask the "
        "user to choose a chapter or explicitly confirm the whole book.\n"
        "10b. BOOK-NAME vs CHAPTER-TITLE: If the user's message matches "
        "a book_title field in candidates (not a chapter title), use "
        "chapter_key=null and the matched book_title. If it matches a "
        "specific chapter title, set both chapter_key and book_title from "
        "that candidate. Never guess — always verify against candidates.\n"
        "11. If the user mentions reviewing or learning based on their "
        'weaknesses, weak points, or areas they struggle with, set intent '
        'to "review_weakness" with chapter_key/book_title null. '
        "The system will resolve real weakness evidence separately.\n"
        "12. MODIFICATION DETECTION: If the conversation history shows a "
        "study plan was already generated in a prior turn, and the current "
        "message is modifying constraints (e.g. changing daily time, number "
        "of days — like '每天只能学20分钟', '改成5天', '重新排一个') WITHOUT "
        "re-stating the chapter/book, set intent to 'custom' and set ONLY "
        "the modified fields (daily_minutes, days). Set chapter_key, "
        "book_title, and document_id ALL to null — the system will inherit "
        "scope from the prior plan. Do NOT invent scope values.\n"
        "\n"
        f"Available chapter candidates:\n{candidates_text}\n"
        f"{history_text}"
        f"User request:\n{message}"
    )


# ---------------------------------------------------------------------------
# JSON extraction and field validation
# ---------------------------------------------------------------------------


def _extract_json(raw: str) -> dict[str, Any]:
    """Extract a JSON object from LLM output, handling markdown fences."""
    text = raw.strip()

    # Strip ```json / ``` fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("LLM output is not a JSON object")
    return parsed


def _validate_parsed_fields(parsed: dict[str, Any]) -> None:
    """Validate the types and values of every field in the parsed LLM output."""
    # --- reject extra / unknown fields ------------------------------------
    extra_fields = set(parsed.keys()) - _ALLOWED_FIELDS
    if extra_fields:
        raise PlanRequestParseError(
            "unexpected_field",
            f"Unexpected fields in LLM output: {sorted(extra_fields)}",
        )

    # --- intent (required) ------------------------------------------------
    intent = parsed.get("intent")
    if intent is None:
        raise PlanRequestParseError(
            "missing_intent", "LLM output missing required field: intent"
        )
    if not isinstance(intent, str):
        raise PlanRequestParseError(
            "invalid_intent",
            f"intent must be a string, got {type(intent).__name__}",
        )
    intent_str = str(intent).strip()
    if intent_str not in _ALLOWED_INTENTS:
        raise PlanRequestParseError(
            "invalid_intent",
            f"Invalid intent: {intent_str!r}. "
            f"Allowed: {sorted(_ALLOWED_INTENTS)}",
        )

    # --- numeric fields ---------------------------------------------------
    for field in ("days", "daily_minutes"):
        val = parsed.get(field)
        if val is None:
            continue
        if isinstance(val, bool):
            raise PlanRequestParseError(
                "invalid_field_type",
                f"{field} must be an integer or null, got bool",
            )
        if isinstance(val, int):
            continue
        if isinstance(val, float):
            if val != int(val):
                raise PlanRequestParseError(
                    "invalid_field_type",
                    f"{field} must be an integer, got {val!r}",
                )
        else:
            raise PlanRequestParseError(
                "invalid_field_type",
                f"{field} must be an integer or null, got {type(val).__name__}",
            )

    # --- text fields ------------------------------------------------------
    for field in ("focus_text", "book_title", "document_id", "chapter_key"):
        val = parsed.get(field)
        if val is not None and not isinstance(val, str):
            raise PlanRequestParseError(
                "invalid_field_type",
                f"{field} must be a string or null, got {type(val).__name__}",
            )


def _validate_chapter_references(
    parsed: dict[str, Any],
    chapters: list[dict[str, Any]],
) -> None:
    """Validate that every chapter field in *parsed* matches a candidate exactly.

    The three chapter fields (``chapter_key``, ``book_title``,
    ``document_id``) must **all** come from the same candidate, or **all** be
    null / empty.  Partial or fabricated combinations are rejected.

    Additionally, ``learn_chapter`` intent **requires** a valid
    ``chapter_key``.
    """
    _ck = _normalise_optional_str(parsed.get("chapter_key"))
    _bt = _normalise_optional_str(parsed.get("book_title"))
    _did = _normalise_optional_str(parsed.get("document_id"))
    intent = str(parsed.get("intent", "")).strip()

    # Build an index: chapter_key → chapter dict
    candidate_by_key: dict[str, dict[str, Any]] = {}
    for ch in chapters:
        if not isinstance(ch, dict):
            continue
        ck = str(ch.get("key", "")).strip()
        if ck:
            candidate_by_key[ck] = ch

    if _ck is not None:
        # --- chapter_key was provided → must exist in candidates ----------
        if _ck not in candidate_by_key:
            raise PlanRequestParseError(
                "chapter_not_found",
                f"chapter_key {_ck!r} does not match any candidate. "
                f"Available keys: {sorted(candidate_by_key.keys())}",
            )

        matched = candidate_by_key[_ck]

        # --- book_title MUST be present and consistent --------------------
        if _bt is None:
            raise PlanRequestParseError(
                "chapter_mismatch",
                f"chapter_key is {_ck!r} but book_title is null; "
                "chapter fields must be all-null or all-present from a candidate",
            )
        expected_bt = str(matched.get("book_title", "")).strip()
        if _bt != expected_bt:
            raise PlanRequestParseError(
                "chapter_mismatch",
                f"book_title {_bt!r} does not match candidate {_ck!r} "
                f"(expected {expected_bt!r})",
            )

        # --- document_id MUST be present and consistent --------------------
        if _did is None:
            raise PlanRequestParseError(
                "chapter_mismatch",
                f"chapter_key is {_ck!r} but document_id is null; "
                "chapter fields must be all-null or all-present from a candidate",
            )
        expected_did = str(matched.get("document_id", "")).strip()
        if _did != expected_did:
            raise PlanRequestParseError(
                "chapter_mismatch",
                f"document_id {_did!r} does not match candidate {_ck!r} "
                f"(expected {expected_did!r})",
            )
    else:
        # --- chapter_key is null — may be whole-book or no-chapter case ---
        if _bt is not None:
            # Validate book_title exists in candidates (whole-book pattern)
            matching_books: set[str] = set()
            for ch in chapters:
                if not isinstance(ch, dict):
                    continue
                cb = str(ch.get("book_title", "")).strip()
                if cb:
                    matching_books.add(cb)
            if _bt not in matching_books:
                raise PlanRequestParseError(
                    "chapter_not_found",
                    f"book_title {_bt!r} does not match any candidate. "
                    f"Available books: {sorted(matching_books)}",
                )
        if _did is not None:
            # Validate document_id exists in candidates
            matching_docs: set[str] = set()
            for ch in chapters:
                if not isinstance(ch, dict):
                    continue
                cd = str(ch.get("document_id", "")).strip()
                if cd:
                    matching_docs.add(cd)
            if _did not in matching_docs:
                raise PlanRequestParseError(
                    "chapter_not_found",
                    f"document_id {_did!r} does not match any candidate. "
                    f"Available docs: {sorted(matching_docs)}",
                )
            # If both book_title and document_id are provided without
            # chapter_key, verify they appear together in at least one
            # candidate (whole-book cross-validation).
            if _bt is not None:
                consistent = any(
                    isinstance(ch, dict)
                    and str(ch.get("book_title", "")).strip() == _bt
                    and str(ch.get("document_id", "")).strip() == _did
                    for ch in chapters
                )
                if not consistent:
                    raise PlanRequestParseError(
                        "chapter_mismatch",
                        f"book_title {_bt!r} and document_id {_did!r} "
                        f"do not appear together in any candidate",
                    )

    # --- learn_chapter requires chapter_key or book_title ----------------
    if intent == "learn_chapter" and _ck is None and _bt is None:
        raise PlanRequestParseError(
            "missing_chapter_key",
            "learn_chapter intent requires a non-null chapter_key "
            "or book_title from the candidates",
        )


def _normalise_optional_str(value: object) -> str | None:
    """Return a trimmed string, or ``None`` for null / empty / whitespace."""
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    s = value.strip()
    return s if s else None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def parse_plan_request_text(
    message: str,
    *,
    chapters: list[dict[str, Any]] | None = None,
    llm_call: Callable[[str], str] | None = None,
    history: list[dict[str, Any]] | None = None,
    memory: Any | None = None,
    user_id: str | None = None,
    _compat_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Parse a natural-language plan request into a structured request dict.

    This is a **pure internal service**.  It delegates to
    :func:`parse_scope_selector_from_text` — a single unified LLM call
    that simultaneously extracts the learning scope (book, chapter,
    knowledge point, disease, weakness) AND plan decorator fields
    (intent, days, daily_minutes, focus_text).

    The returned dict carries the normalised request fields expected by
    :func:`normalize_plan_request` PLUS the unified ``scope_selector``
    and ``resolved_scopes`` so that downstream consumers
    (:func:`create_plan_draft_from_text`) can consume pre-validated
    scopes directly without re-resolving.

    The function does **not** call the Router, HTTP layer, database, or any
    downstream plan-draft service.

    Parameters
    ----------
    message:
        Non-empty user natural-language text (e.g.
        ``"三天复习《牙周病学》第三章，每天30分钟，重点炎症机制"``).
    chapters:
        Chapter candidate dicts as provided by the caller.  Each dict must
        carry at least ``key``, ``book_title``, ``document_id`` and
        ``title`` (the production shape from the KG agent).  May be
        ``None`` or empty when no chapter context is available.
    llm_call:
        Injectable ``(prompt: str) -> str`` callable.  When ``None`` (the
        default), a callable is built from
        :func:`backend.services.llm_client.chat_completion`.  Tests should
        inject a fake to avoid real network calls.
    history:
        Optional conversation history for multi-turn clarification
        resolution.  When provided, the most recent entries are included
        in the LLM prompt so the parser can resolve short answers (e.g.
        just a book name) against prior system questions.
    memory:
        Optional :class:`LearningMemory` instance for resolving real
        weakness evidence during scope resolution.  When provided together
        with *user_id*, the unified scope resolver can populate
        ``weakness_profile`` scopes from learner-memory data (weakness
        items, low-mastery scores, profile-state summaries).  When
        ``None``, weakness-profile resolution produces empty scopes
        (requiring later enrichment or clarification).
    user_id:
        Required when *memory* is provided — forwarded to the scope
        resolver for learner-memory queries.
    _compat_state:
        Optional shared mutable dict for JSON-mode compatibility tracking
        across the two stages of a single plan flow.  Forwarded to
        :func:`_make_default_llm_call`.  See that function's docstring
        for the contract.

    Returns
    -------
    dict
        A normalised plan-request dict with the following keys:

        * ``intent`` — one of the four allowed values
        * ``days`` — positive integer
        * ``daily_minutes`` — positive integer
        * ``focus_text`` — string or ``None``
        * ``book_title`` — compatibility projection from resolved scopes
        * ``document_id`` — compatibility projection from resolved scopes
        * ``chapter_key`` — compatibility projection (single chapter only)
        * ``scope_selector`` — the validated unified scope selector dict
        * ``resolved_scopes`` — ordered list of pre-validated scope dicts
        * ``clarification_needed`` — bool
        * ``clarification_message`` — string or ``None``

        The old ``book_title`` / ``document_id`` / ``chapter_key`` fields
        are compatibility projections from the unified result — they are
        NOT produced by a separate semantic parser.

    Raises
    ------
    PlanRequestParseError
        In any of these cases:

        * *message* is empty or whitespace-only
        * LLM is not configured and no *llm_call* was provided
        * The LLM request itself failed
        * The LLM output is not valid JSON
        * The scope selector fields are invalid
        * :func:`normalize_plan_request` rejects the derived request

        The exception carries a stable ``.code`` attribute (e.g.
        ``"invalid_json"``, ``"empty_message"``) that callers can
        switch on without string-matching the message.
    """
    # ------------------------------------------------------------------
    # 1. Validate input
    # ------------------------------------------------------------------
    if not message or not str(message).strip():
        raise PlanRequestParseError(
            "empty_message", "message must be a non-empty string"
        )

    # ------------------------------------------------------------------
    # 2. Delegate to unified scope selector — ONE LLM call that
    #    simultaneously extracts scope AND plan decorator fields.
    #    The old _build_parse_prompt / chapter_key-book_title parser
    #    is NOT called from this production path.
    # ------------------------------------------------------------------
    try:
        unified = parse_scope_selector_from_text(
            message,
            chapters=chapters,
            llm_call=llm_call,
            history=history,
            memory=memory,
            user_id=user_id,
            _compat_state=_compat_state,
        )
    except ScopeSelectorParseError as exc:
        # Map scope-selector error codes to plan-request error codes
        # so that callers can still switch on PlanRequestParseError.code.
        _CODE_MAP: dict[str, str] = {
            "empty_message": "empty_message",
            "llm_not_configured": "llm_not_configured",
            "llm_request_failed": "llm_request_failed",
            "invalid_json": "invalid_json",
            "invalid_target_type": "invalid_json",
            "invalid_operation": "invalid_json",
            "missing_required_field": "invalid_json",
            "invalid_field_type": "invalid_field_type",
            "invalid_intent": "invalid_intent",
        }
        raise PlanRequestParseError(
            _CODE_MAP.get(exc.code, "invalid_json"), str(exc)
        ) from exc

    # ------------------------------------------------------------------
    # 3. Extract plan fields from the unified selector result
    # ------------------------------------------------------------------
    selector: dict[str, Any] = unified["scope_selector"]
    resolved: list[dict[str, Any]] = unified["resolved_scopes"]

    # --- 3a. intent -----------------------------------------------------
    intent = selector.get("intent")
    if not intent:
        # Infer intent from target_type when the LLM did not provide one
        if selector["target_type"] == "weakness_profile":
            intent = "review_weakness"
        elif selector["target_type"] == "unknown":
            intent = "custom"
        else:
            intent = "learn_chapter"

    # --- 3b. days / daily_minutes / focus_text -------------------------
    days = selector.get("days") or 7
    daily_minutes = selector.get("daily_minutes") or 30
    focus_text = selector.get("focus_text")

    # --- 3c. book_title / document_id / chapter_key --------------------
    # These are COMPATIBILITY PROJECTIONS from the unified result.
    # They are NOT produced by a separate semantic parser.
    book_title: str | None = None
    document_id: str | None = None
    chapter_key: str | None = None

    if resolved:
        first_scope = resolved[0]
        book_title = first_scope.get("book_title")
        document_id = first_scope.get("document_id")
        # Only project chapter_key for a single-chapter scope
        if first_scope.get("scope_type") == "chapter" and len(resolved) == 1:
            chapter_key = first_scope.get("scope_ref")

    # ------------------------------------------------------------------
    # 4. Build and normalize the request
    # ------------------------------------------------------------------
    raw_request: dict[str, Any] = {
        "intent": intent,
        "days": days,
        "daily_minutes": daily_minutes,
        "focus_text": focus_text,
        "book_title": book_title,
        "document_id": document_id,
        "chapter_key": chapter_key,
    }
    try:
        result = normalize_plan_request(raw_request)
    except ValueError as exc:
        raise PlanRequestParseError(
            "invalid_normalized_request", str(exc)
        ) from exc

    # ------------------------------------------------------------------
    # 5. Attach unified scope selector and resolved scopes so that
    #    downstream consumers (create_plan_draft_from_text) can use
    #    the pre-validated resolved_scopes directly.
    # ------------------------------------------------------------------
    result["scope_selector"] = selector
    result["resolved_scopes"] = resolved
    result["clarification_needed"] = unified["clarification_needed"]
    result["clarification_message"] = unified.get("clarification_message")
    result["clarification_options"] = unified.get("clarification_options", [])

    return result


class PlanDraftStore:
    """Thread-safe, in-memory store for short-lived plan drafts.

    Each draft is bound to a single *user_id* and expires after
    *ttl_seconds*.  The store generates opaque, unpredictable draft IDs
    on :meth:`save` — callers cannot choose or predict them.

    Both :meth:`save` and :meth:`get` deep-copy the payload so that
    callers cannot mutate stored contents through a reference.

    Parameters
    ----------
    ttl_seconds:
        Lifetime of each draft in seconds (default 1800 = 30 min).
    _clock:
        Injectable clock callable returning a float timestamp.
        Defaults to ``time.time``.  Exposed for tests that need to
        freeze or fast-forward time.
    """

    def __init__(
        self,
        ttl_seconds: int = 1800,
        *,
        _clock: Callable[[], float] | None = None,
    ) -> None:
        self._ttl = max(1, int(ttl_seconds))
        self._clock: Callable[[], float] = _clock if _clock is not None else time.time
        self._lock = threading.Lock()
        self._drafts: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def save(
        self,
        user_id: str,
        payload: dict[str, Any],
        *,
        parent_plan_draft_id: str | None = None,
    ) -> str:
        """Persist *payload* and return an opaque ``plan_draft_id``.

        When *parent_plan_draft_id* is given, the parent draft must exist,
        belong to the same *user_id*, and still be within its TTL.  The
        child draft always receives a fresh, independent record — the
        parent is never mutated or extended.

        The returned ID is the only way to retrieve the draft later.
        """
        if parent_plan_draft_id is not None:
            self._validate_parent(parent_plan_draft_id, user_id)

        draft_id = f"draft_{secrets.token_hex(16)}"
        now = self._clock()
        record: dict[str, Any] = {
            "plan_draft_id": draft_id,
            "user_id": user_id,
            "created_at": now,
            "expires_at": now + self._ttl,
            "payload": copy.deepcopy(payload),
        }
        if parent_plan_draft_id is not None:
            record["parent_plan_draft_id"] = parent_plan_draft_id

        with self._lock:
            self._drafts[draft_id] = record
        return draft_id

    def _validate_parent(self, parent_plan_draft_id: str, user_id: str) -> None:
        """Check that *parent_plan_draft_id* is valid for *user_id*.

        Raises
        ------
        KeyError
            The parent draft ID is unknown.
        PermissionError
            The parent draft belongs to a different user.
        ValueError
            The parent draft has expired.
        """
        with self._lock:
            parent = self._drafts.get(parent_plan_draft_id)
        if parent is None:
            raise KeyError(
                f"Unknown parent plan draft: {parent_plan_draft_id!r}"
            )
        if parent["user_id"] != user_id:
            raise PermissionError(
                f"Parent plan draft {parent_plan_draft_id!r} does not belong "
                f"to user {user_id!r}"
            )
        if self._clock() >= parent["expires_at"]:
            raise ValueError(
                f"Parent plan draft {parent_plan_draft_id!r} has expired"
            )

    def get(self, plan_draft_id: str, user_id: str) -> dict[str, Any]:
        """Return the draft record, validating ownership and TTL.

        The returned dict is a deep copy — mutating it does not affect
        the stored record.

        Raises
        ------
        KeyError
            *plan_draft_id* is unknown.
        PermissionError
            The draft belongs to a different user.
        ValueError
            The draft has expired.
        """
        with self._lock:
            record = self._drafts.get(plan_draft_id)
        if record is None:
            raise KeyError(f"Unknown plan draft: {plan_draft_id!r}")
        if record["user_id"] != user_id:
            raise PermissionError(
                f"Plan draft {plan_draft_id!r} does not belong to user {user_id!r}"
            )
        if self._clock() >= record["expires_at"]:
            raise ValueError(f"Plan draft {plan_draft_id!r} has expired")
        return copy.deepcopy(record)


# ---------------------------------------------------------------------------
# Module-level default draft store (shared, short-lived)
# ---------------------------------------------------------------------------

_default_draft_store: PlanDraftStore | None = None


def _get_default_draft_store() -> PlanDraftStore:
    """Return the module-level shared :class:`PlanDraftStore` singleton.

    Created lazily on first access with the default TTL (30 min).
    Tests should inject their own store via *draft_store* on
    :func:`recommend_study_plan` — they never touch this singleton.
    """
    global _default_draft_store
    if _default_draft_store is None:
        _default_draft_store = PlanDraftStore(ttl_seconds=1800)
    return _default_draft_store


def get_draft_from_default_store(
    plan_draft_id: str,
    user_id: str,
) -> dict[str, Any]:
    """Read a draft from the module-level default store.

    This is a **pure service-layer helper** — not an API endpoint.  It
    exists so that other service modules (or future API tasks) can read
    back drafts created by :func:`recommend_study_plan` without needing
    their own store reference.

    Parameters
    ----------
    plan_draft_id:
        The opaque draft ID returned by a prior :func:`recommend_study_plan`
        call in ``custom_draft`` mode.
    user_id:
        The same user who owns the draft.

    Returns
    -------
    dict
        The full draft record (deep copy) from :meth:`PlanDraftStore.get`.

    Raises
    ------
    KeyError, PermissionError, ValueError
        From :class:`PlanDraftStore` when the draft is unknown, cross-user,
        or expired.
    """
    return _get_default_draft_store().get(plan_draft_id, user_id)


# ---------------------------------------------------------------------------
# Custom-draft recommendation helpers
# ---------------------------------------------------------------------------


def _map_draft_items_to_recommendations(
    draft_id: str,
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Map draft payload items to study-plan recommendation cards.

    Every field in the returned cards comes exclusively from the draft
    *items* — nothing is recalculated or invented.  The
    ``recommendation_id`` is bound to *draft_id* + index so each card
    is stable and traceable.
    """
    recommendations: list[dict[str, Any]] = []
    for i, item in enumerate(items):
        scope = item.get("learning_scope") or {}
        recommendations.append(
            {
                "recommendation_id": f"{draft_id}:{i}",
                "title": item["learning_target"],
                "task_type": item["task_type"],
                "learning_target": item["learning_target"],
                "reason": item["reason"],
                "estimated_minutes": item["estimated_minutes"],
                "day_offset": item.get("day_offset", 0),
                "kg_node_ids": list(scope.get("kg_node_ids", []))
                if isinstance(scope, dict)
                else [],
                "learning_scope": item.get("learning_scope"),
                "evidence_source": item["evidence_source"],
            }
        )
    return recommendations


def _default_chapter_provider() -> list[dict[str, Any]]:
    """Fetch chapters from the production KG overview.

    Raises :exc:`RuntimeError` when the overview is unavailable, ``ok``
    is false, or the chapter list is missing / empty.  Never falls back
    to guessing or fixed recommendations.
    """
    from backend.services.knowledge_graph.agent import get_graph_overview

    overview = get_graph_overview()
    if not overview.get("ok"):
        raise RuntimeError(
            "Knowledge graph overview is not available "
            "(ok=False or missing)"
        )
    chapters = overview.get("chapters")
    if not chapters or not isinstance(chapters, list) or len(chapters) == 0:
        raise RuntimeError(
            "Knowledge graph overview returned no valid chapters"
        )
    return list(chapters)


def _recommend_custom_draft(
    *,
    user_id: str,
    plan_date: str,
    request_text: str,
    parent_plan_draft_id: str | None,
    draft_store: PlanDraftStore,
    chapter_provider: Callable[[], list[dict[str, Any]]],
    llm_call: Callable[[str], str] | None,
    memory: LearningMemory | None = None,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Custom-draft branch: text → parse → scope → persist → cards.

    This is called exclusively when *request_text* is a non-empty string.
    It chains the existing :func:`create_plan_draft_from_text` composition
    with draft-item-to-card mapping.
    """
    # 1. Fetch chapters
    try:
        chapters = chapter_provider()
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(
            f"Failed to fetch KG chapters: {exc}"
        ) from exc

    if not isinstance(chapters, list) or len(chapters) == 0:
        raise RuntimeError(
            "Chapter provider returned no valid chapters"
        )

    # 2. Parse + scope + persist
    try:
        draft = create_plan_draft_from_text(
            draft_store,
            user_id=user_id,
            message=request_text,
            chapters=chapters,
            llm_call=llm_call,
            parent_plan_draft_id=parent_plan_draft_id,
            memory=memory,
            history=history,
        )
    except (PlanRequestParseError, OrchestrationValidationError, KeyError, PermissionError, ValueError):
        raise
    except Exception as exc:
        raise RuntimeError(
            f"Failed to create custom plan draft: {exc}"
        ) from exc

    # 3. Map items → recommendation cards (or expose clarification)
    draft_id: str = draft["plan_draft_id"]
    payload: dict[str, Any] = draft["payload"]
    items: list[dict[str, Any]] = payload.get("items", [])
    recommendations = _map_draft_items_to_recommendations(draft_id, items)

    result: dict[str, Any] = {
        "user_id": user_id,
        "plan_date": plan_date,
        "mode": "custom_draft",
        "plan_draft_id": draft_id,
        "recommendations": recommendations,
        "trace": {
            "strategy": "custom_draft_from_request_text",
            "draft_id": draft_id,
            "item_count": len(items),
            "chapter_count": len(chapters),
            "parent_plan_draft_id": parent_plan_draft_id,
            "parsed_intent": payload["request"]["intent"],
        },
    }

    # Expose explicit clarification state so the chat can render the
    # LLM's question instead of a false-success plan card.
    if payload.get("clarification_needed"):
        result["clarification_needed"] = True
        result["clarification_message"] = str(
            payload.get("clarification_message", "")
        ).strip()
        # Ensure recommendations are empty — no plan items exist.
        result["recommendations"] = []
        options = payload.get("clarification_options")
        if isinstance(options, list):
            result["clarification_options"] = options

    if parent_plan_draft_id is not None:
        result["parent_plan_draft_id"] = parent_plan_draft_id
    return result


def normalize_plan_request(request: dict[str, Any]) -> dict[str, Any]:
    """Normalize and validate a structured plan-request dict.

    This is a **pure, internal contract function**.  It does **not** call any
    LLM, does **not** touch the Router/API/UI, and does **not** persist
    anything.  Its only job is to take a raw JSON dict — as produced by the
    future semantic layer — and return a clean, validated, JSON-safe
    normalized request that downstream scope / draft services can consume
    without further guard logic.

    Parameters
    ----------
    request:
        A raw plan-request dict.  The recognised keys are:

        * ``intent`` — **required**; one of ``"learn_chapter"``,
          ``"review_chapter"``, ``"review_weakness"``, ``"custom"``.
        * ``days`` — optional positive integer; default **7**, range [1, 90].
        * ``daily_minutes`` — optional positive integer; default **30**,
          range [5, 240].
        * ``focus_text`` — optional string; trimmed, empty → ``None``.
        * ``book_title`` — optional string; trimmed, empty → ``None``.
        * ``document_id`` — optional string; trimmed, empty → ``None``.
        * ``chapter_key`` — optional string; trimmed, empty → ``None``.

    Returns
    -------
    dict
        A **new** dict with exactly the keys listed above.  The original
        *request* is never mutated.

    Raises
    ------
    ValueError
        If *intent* is missing or not in the allowed set.
    ValueError
        If *days* or *daily_minutes* is not a valid integer or out of range.
    """
    _ALLOWED_INTENTS = {"learn_chapter", "review_chapter", "review_weakness", "custom"}

    # -- intent ----------------------------------------------------------
    raw_intent = request.get("intent")
    if raw_intent is None:
        raise ValueError(
            f"intent is required; allowed: {sorted(_ALLOWED_INTENTS)}"
        )
    intent = str(raw_intent).strip()
    if intent not in _ALLOWED_INTENTS:
        raise ValueError(
            f"Unknown intent: {intent!r}. Allowed: {sorted(_ALLOWED_INTENTS)}"
        )

    # -- days ------------------------------------------------------------
    days = _normalize_int_field(
        request.get("days"), default=7, min_val=1, max_val=90, label="days"
    )

    # -- daily_minutes ---------------------------------------------------
    daily_minutes = _normalize_int_field(
        request.get("daily_minutes"),
        default=30,
        min_val=5,
        max_val=240,
        label="daily_minutes",
    )

    # -- text fields (trim, empty → None) --------------------------------
    def _trim(value: object) -> str | None:
        if value is None:
            return None
        s = str(value).strip()
        return s if s else None

    return {
        "intent": intent,
        "days": days,
        "daily_minutes": daily_minutes,
        "focus_text": _trim(request.get("focus_text")),
        "book_title": _trim(request.get("book_title")),
        "document_id": _trim(request.get("document_id")),
        "chapter_key": _trim(request.get("chapter_key")),
    }


def _normalize_int_field(
    raw: object,
    *,
    default: int,
    min_val: int,
    max_val: int,
    label: str,
) -> int:
    """Validate and coerce a single integer field, or return *default* for ``None``."""
    if raw is None:
        return default
    if isinstance(raw, bool):
        raise ValueError(f"{label} must be an integer, got {raw!r}")
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, float):
        if raw != int(raw):
            raise ValueError(f"{label} must be an integer, got {raw!r}")
        value = int(raw)
    else:
        raise ValueError(f"{label} must be an integer, got {raw!r}")
    if value < min_val or value > max_val:
        raise ValueError(
            f"{label} must be between {min_val} and {max_val}, got {value}"
        )
    return value


def resolve_learning_scope(
    chapters: list[dict[str, Any]],
    *,
    chapter_key: str,
    book_title: str | None = None,
    document_id: str | None = None,
) -> dict[str, Any]:
    """Resolve a validated ``learning_scope`` from KG chapter records.

    This is a **pure, internal helper**.  It does **not** call any API, does
    **not** perform fuzzy / title-based matching, and does **not** fall back
    to topic text or arbitrary defaults.

    Every field in the returned scope comes directly from the matched
    chapter record — nothing is invented.

    Parameters
    ----------
    chapters:
        Chapter dicts as produced by the knowledge-graph overview.  Each
        dict must carry at least ``key``, ``book_title``, ``document_id``,
        ``title`` and ``core_ids`` (the production shape from
        :mod:`backend.services.knowledge_graph.agent`).
    chapter_key:
        **Required.**  The exact chapter key to match (e.g.
        ``"chapter:abc123"``).  An empty or whitespace-only value raises
        :exc:`ValueError`.
    book_title:
        Optional.  When given (non-empty), the matched chapter's
        ``book_title`` must equal this value exactly — otherwise a
        :exc:`ValueError` is raised.
    document_id:
        Optional.  When given (non-empty), the matched chapter's
        ``document_id`` must equal this value exactly — otherwise a
        :exc:`ValueError` is raised.

    Returns
    -------
    dict
        A ``learning_scope`` dict with the following keys:

        * ``book_title`` — from the matched chapter
        * ``document_id`` — from the matched chapter
        * ``chapter_key`` — the validated input key
        * ``chapter_title`` — the chapter's ``title`` field
        * ``kg_node_ids`` — deduplicated, non-empty list of the
          chapter's ``core_ids``
        * ``kg_node_names`` — optional ``dict[str, str]`` mapping each
          ``kg_node_id`` to its real knowledge-point name from the
          chapter's ``cores`` records.  Only includes IDs that have a
          verified non-empty name; an ID without a matching name in
          ``cores`` is simply absent from this mapping.  May be empty
          when ``cores`` is missing or names are unavailable — the
          ``kg_node_ids`` list is never affected.

    Raises
    ------
    ValueError
        In any of these cases:

        * *chapter_key* is empty / whitespace-only
        * no chapter with the given *chapter_key* is found in *chapters*
        * more than one chapter shares the same *chapter_key*
        * the matched chapter's ``book_title``, ``document_id`` or
          ``title`` is empty, whitespace-only or missing
        * *book_title* is provided but does not match the chapter
        * *document_id* is provided but does not match the chapter
        * the matched chapter has no valid (non-empty) core IDs
    """
    # ------------------------------------------------------------------
    # 1. Validate chapter_key
    # ------------------------------------------------------------------
    if not chapter_key or not str(chapter_key).strip():
        raise ValueError("chapter_key must be a non-empty string")

    key = str(chapter_key).strip()

    # ------------------------------------------------------------------
    # 2. Exact match by chapter key
    # ------------------------------------------------------------------
    candidates: list[dict[str, Any]] = [
        ch
        for ch in chapters
        if isinstance(ch, dict) and str(ch.get("key") or "") == key
    ]

    if not candidates:
        raise ValueError(f"Chapter not found in provided records: {key!r}")

    if len(candidates) > 1:
        raise ValueError(
            f"Ambiguous chapter key {key!r}: "
            f"{len(candidates)} records share the same key"
        )

    chapter = candidates[0]

    # ------------------------------------------------------------------
    # 3. Extract resolved fields from the matched chapter
    # ------------------------------------------------------------------
    resolved_book_title = str(chapter.get("book_title") or "").strip()
    resolved_document_id = str(chapter.get("document_id") or "").strip()
    resolved_chapter_title = str(chapter.get("title") or "").strip()

    # ------------------------------------------------------------------
    # 4. Validate that every scope field resolved from the chapter is
    #    non-empty — a scope with a blank book_title, document_id or
    #    title is incomplete and must be rejected.
    # ------------------------------------------------------------------
    if not resolved_book_title:
        raise ValueError(
            f"Chapter {key!r} has no valid book_title "
            f"(field is empty or missing)"
        )
    if not resolved_document_id:
        raise ValueError(
            f"Chapter {key!r} has no valid document_id "
            f"(field is empty or missing)"
        )
    if not resolved_chapter_title:
        raise ValueError(
            f"Chapter {key!r} has no valid title "
            f"(field is empty or missing)"
        )

    # ------------------------------------------------------------------
    # 5. Optional consistency checks
    # ------------------------------------------------------------------
    if book_title is not None and str(book_title).strip():
        expected_bt = str(book_title).strip()
        if resolved_book_title != expected_bt:
            raise ValueError(
                f"Book title mismatch for chapter {key!r}: "
                f"expected {expected_bt!r}, got {resolved_book_title!r}"
            )

    if document_id is not None and str(document_id).strip():
        expected_did = str(document_id).strip()
        if resolved_document_id != expected_did:
            raise ValueError(
                f"Document ID mismatch for chapter {key!r}: "
                f"expected {expected_did!r}, got {resolved_document_id!r}"
            )

    # ------------------------------------------------------------------
    # 6. Gather and deduplicate core IDs → kg_node_ids
    # ------------------------------------------------------------------
    raw_ids = chapter.get("core_ids")
    kg_node_ids: list[str] = []
    seen: set[str] = set()
    for cid in raw_ids if isinstance(raw_ids, list) else []:
        sid = str(cid).strip()
        if sid and sid not in seen:
            seen.add(sid)
            kg_node_ids.append(sid)

    if not kg_node_ids:
        raise ValueError(
            f"Chapter {key!r} ({resolved_chapter_title!r}) "
            f"has no valid core IDs"
        )

    # ------------------------------------------------------------------
    # 6b. Build kg_node_names from chapter.cores (id → real name)
    # ------------------------------------------------------------------
    kg_node_names: dict[str, str] = {}
    raw_cores = chapter.get("cores")
    if isinstance(raw_cores, list):
        # Build a deduplicated id→name index (first non-empty name wins)
        core_name_by_id: dict[str, str] = {}
        for core in raw_cores:
            if not isinstance(core, dict):
                continue
            cid = str(core.get("id", "")).strip()
            cname = str(core.get("name", "")).strip()
            if cid and cname and cid not in core_name_by_id:
                core_name_by_id[cid] = cname
        # Only include names for IDs validated in kg_node_ids
        for nid in kg_node_ids:
            name = core_name_by_id.get(nid)
            if name:
                kg_node_names[nid] = name

    # ------------------------------------------------------------------
    # 7. Return validated learning_scope
    # ------------------------------------------------------------------
    return {
        "book_title": resolved_book_title,
        "document_id": resolved_document_id,
        "chapter_key": key,
        "chapter_title": resolved_chapter_title,
        "kg_node_ids": kg_node_ids,
        "kg_node_names": kg_node_names,
    }


# ---------------------------------------------------------------------------
# Unified scope selector — candidate summary, prompt, validation, resolution
# ---------------------------------------------------------------------------

def _build_unified_candidate_summary(
    chapters: list[dict[str, Any]],
    *,
    memory: Any | None = None,
    user_id: str | None = None,
) -> str:
    """Build a compact text summary of all real entities the LLM can select.

    The summary includes:
    - Books (deduplicated by book_title, with chapter counts)
    - Chapters (book → ordered chapter list with keys and titles)
    - Knowledge points (from chapter.cores, grouped by book+chapter)
    - Disease topics (from KG nodes with type ``Disease`` / ``DiseaseSubtype``)

    The text is designed to be compact enough for the LLM prompt while
    providing enough detail for semantic matching (fuzzy title, abbreviations,
    ordinals, etc.).
    """
    lines: list[str] = []

    # --- 1. Books ----------------------------------------------------------
    books: dict[str, dict[str, Any]] = {}
    for ch in chapters:
        if not isinstance(ch, dict):
            continue
        bt = str(ch.get("book_title", "")).strip()
        did = str(ch.get("document_id", "")).strip()
        if not bt:
            continue
        key = f"{bt}\0{did}"
        if key not in books:
            books[key] = {
                "book_title": bt,
                "document_id": did,
                "chapter_count": 0,
            }
        books[key]["chapter_count"] += 1

    lines.append("=== 可用教材 (Textbooks) ===")
    for i, (bkey, b) in enumerate(sorted(books.items(), key=lambda x: x[1]["book_title"]), 1):
        lines.append(
            f"  [{i}] 《{b['book_title']}》(document_id={b['document_id']!r}) "
            f"— {b['chapter_count']} 章"
        )

    # --- 2. Chapters (book → ordered list) --------------------------------
    # Build book → sorted chapter index
    book_chapters: dict[str, list[dict[str, Any]]] = {}
    for ch in chapters:
        if not isinstance(ch, dict):
            continue
        bt = str(ch.get("book_title", "")).strip()
        if not bt:
            continue
        book_chapters.setdefault(bt, []).append(ch)

    # Sort chapters within each book by number, then title
    for bt in book_chapters:
        book_chapters[bt].sort(
            key=lambda c: (
                int(c.get("number", 0) or 0),
                str(c.get("title", "")).strip(),
            )
        )

    lines.append("\n=== 章节 (Chapters) ===")
    for bt in sorted(book_chapters.keys()):
        ch_list = book_chapters[bt]
        lines.append(f"《{bt}》(共{len(ch_list)}章):")
        for idx, ch in enumerate(ch_list):
            ckey = str(ch.get("key", "")).strip()
            ctitle = str(ch.get("title", "")).strip()
            cnum = ch.get("number", idx + 1)
            lines.append(
                f"  [{idx + 1}] key={ckey!r}  "
                f"第{cnum}章 {ctitle}"
            )

    # --- 3. Knowledge points (from chapter.cores) -------------------------
    lines.append("\n=== 知识点 (Knowledge Points) ===")
    kp_count = 0
    for bt in sorted(book_chapters.keys()):
        ch_list = book_chapters[bt]
        for ch in ch_list:
            ctitle = str(ch.get("title", "")).strip()
            cores = ch.get("cores")
            if not isinstance(cores, list) or not cores:
                continue
            for core in cores:
                if not isinstance(core, dict):
                    continue
                cid = str(core.get("id", "")).strip()
                cname = str(core.get("name", "")).strip()
                if not cid or not cname:
                    continue
                ctype = str(core.get("type_label", "")).strip() or str(core.get("type", "")).strip()
                type_suffix = f"({ctype})" if ctype else ""
                lines.append(f"  - core_id={cid!r} 《{bt}》{ctitle}: {cname} {type_suffix}")
                kp_count += 1
                if kp_count > 200:
                    break
            if kp_count > 200:
                break
        if kp_count > 200:
            lines.append("  ... (truncated — more knowledge points available)")
            break
    if kp_count == 0:
        lines.append("  (no knowledge point details available)")

    # --- 4. Disease topics (from KG nodes with Disease/DiseaseSubtype) ----
    lines.append("\n=== 疾病主题 (Disease Topics) ===")
    disease_count = 0
    try:
        from backend.services.knowledge_graph.agent import get_graph_overview
        overview = get_graph_overview()
        if overview.get("ok"):
            all_nodes = overview.get("overview", {}).get("nodes", [])
            for node in all_nodes:
                if not isinstance(node, dict):
                    continue
                ntype = str(node.get("type", "")).strip()
                if ntype not in ("Disease", "DiseaseSubtype"):
                    continue
                nid = str(node.get("id", "")).strip()
                nname = str(node.get("name", "")).strip()
                nlabel = str(node.get("type_label", "")).strip() or ntype
                if not nid or not nname:
                    continue
                lines.append(f"  - node_id={nid!r} {nlabel}: {nname}")
                disease_count += 1
                if disease_count > 100:
                    break
        if disease_count > 100:
            lines.append("  ... (truncated)")
    except Exception:
        lines.append("  (disease topic data not available from KG provider)")

    if disease_count == 0:
        lines.append("  (no disease topics available — use knowledge points instead)")

    # --- 5. Weakness profile hint -----------------------------------------
    lines.append("\n=== 画像薄弱点 (Weakness Profile) ===")
    if memory is not None and user_id is not None:
        try:
            weakness_items = memory.get_weakness(user_id, limit=20)
            if weakness_items:
                for wi in weakness_items[:20]:
                    nid = str(wi.get("kg_node_id", "")).strip()
                    nname = str(wi.get("kg_node_name", "")).strip()
                    if nid and nname:
                        lines.append(f"  - {nname} (node_id={nid!r})")
            else:
                lines.append("  (no weakness data recorded)")
        except Exception:
            lines.append("  (unable to fetch weakness data)")
    else:
        lines.append("  (no learner memory available)")

    return "\n".join(lines)


def _build_scope_selector_prompt(
    message: str,
    candidates_text: str,
    history: list[dict[str, Any]] | None = None,
) -> str:
    """Build the unified LLM prompt for scope selection.

    One prompt for ALL entity types — no branching by book/chapter/knowledge
    point/disease/weakness.  The LLM receives the user message, conversation
    history, and the real candidate summary, then outputs a single scope
    selector JSON.
    """
    history_text = ""
    if history:
        recent = history[-6:]
        history_lines = []
        for entry in recent:
            role = entry.get("role", "user")
            content = str(entry.get("content", "")).strip()
            if content:
                history_lines.append(f"{role}: {content[:300]}")
        if history_lines:
            history_text = (
                "\nRecent conversation history (for context — the current "
                "message may be a short response to a prior question):\n"
                + "\n".join(history_lines)
                + "\n"
            )

    target_types_desc = (
        "- book: an entire textbook\n"
        "- chapter: a single chapter\n"
        "- chapter_set: multiple chapters (by ordinal, range, or list)\n"
        "- knowledge_point: a specific knowledge point / concept\n"
        "- disease: a disease topic/subtype\n"
        "- weakness_profile: the user's weak areas\n"
        "- unknown: cannot determine from the message"
    )

    operations_desc = (
        "- all: select ALL items in the parent context (e.g. all chapters of a book)\n"
        "- first_n: select the first N items (e.g. first 3 chapters)\n"
        "- last_n: select the last N items\n"
        "- range: select items from ordinal A to B (inclusive)\n"
        "- list: select specific items by title or ordinal\n"
        "- single: select exactly one item\n"
        "- exclude: select all EXCEPT the specified items"
    )

    return (
        "You are a study scope selector. Given a natural-language user "
        "request and a catalogue of available learning entities, output "
        "a SINGLE JSON object that identifies WHAT the user wants to study.\n"
        "\n"
        "Available target types:\n"
        f"{target_types_desc}\n"
        "\n"
        "Available operations:\n"
        f"{operations_desc}\n"
        "\n"
        "Output format (MUST be exactly this JSON structure):\n"
        "{\n"
        '  "target_type": "<one of the types above>",\n'
        '  "mention": "<the user\'s exact mention of the target, in their own words>",\n'
        '  "operation": "<one of the operations above>",\n'
        '  "ordinals": [],\n'
        '  "titles": [],\n'
        '  "scope_ids": [],\n'
        '  "range_start": null,\n'
        '  "range_end": null,\n'
        '  "exclude_ordinals": [],\n'
        '  "parent_book_title": "<book title if the target belongs to a book, else null>",\n'
        '  "context_dependent": false,\n'
        '  "intent": "<learn_chapter|review_chapter|review_weakness|custom>",\n'
        '  "days": <integer 1-90, default 7>,\n'
        '  "daily_minutes": <integer 5-240, default 30>,\n'
        '  "focus_text": "<user stated focus in their own words, or null>"\n'
        "}\n"
        "\n"
        "When the user is MODIFYING a previous plan, you MAY add an "
        "optional revision_constraints field:\n"
        "{\n"
        "  ... (all the standard fields above) ...\n"
        '  "revision_constraints": {\n'
        '    "scope_relation": "maintain",\n'
        '    "practice_count_relation": "decrease",\n'
        '    "daily_minutes_max": 30\n'
        "  }\n"
        "}\n"
        "Only include revision_constraints when modification semantics "
        "are clearly present. Omit it entirely for new plans.\n"
        "\n"
        "FIELD RULES:\n"
        "1. target_type: MUST be one of the listed types. Use "
        "\"chapter_set\" when the user mentions multiple chapters "
        "(by ordinal, range, list, etc.). Use \"unknown\" only when "
        "absolutely no scope hint exists.\n"
        "2. mention: copy the user's description of the target VERBATIM "
        "in their own words — do NOT replace with database names.\n"
        "3. operation: how to select items. When target_type=unknown because "
        "the request has no scope hint, use operation=\"all\" as a schema "
        "placeholder; it does not select any scope and the resolver will ask "
        "a clarification question. Never output null for operation. For "
        "\"single\" (one item), "
        "use operation=\"single\". For \"first N\", use \"first_n\". "
        "For \"chapter 3 to 5\", use \"range\".\n"
        "4. ordinals: for first_n/last_n/range/list — the 1-based "
        "positions. e.g. [1,2,3] for \"前三章\". For single with "
        "ordinal (\"第三章\"), use [3].\n"
        "5. titles: for list/single operations — the matched title "
        "names from the user's message. Use the user's actual words, "
        "NOT database-canonical names. The resolver will match.\n"
        "6. scope_ids: for list/single — copy the VERBATIM key/core_id "
        "from the candidates below when you can map a title to a "
        "specific candidate. Leave empty [] if uncertain.\n"
        "7. range_start / range_end: for range operation — 1-based "
        "ordinals (inclusive). null when not range.\n"
        "8. exclude_ordinals: for exclude operation — ordinals to "
        "remove from the full set.\n"
        "9. parent_book_title: when a chapter/knowledge_point/disease "
        "belongs to a specific book, copy the book_title VERBATIM "
        "from the textbooks list below. null when book-independent.\n"
        "10. context_dependent: true when the message cannot be "
        "understood without conversation history (e.g. \"就它\" or "
        "\"前三个\"). false for self-contained requests.\n"
        "11. intent: the user's learning intent. Use \"learn_chapter\" "
        "when the user wants to learn new material, \"review_chapter\" "
        "when they want to review, \"review_weakness\" when they "
        "mention weaknesses/薄弱点/弱项, and \"custom\" when the "
        "intent is unclear or they are modifying an existing plan. "
        "A request to formulate or create a study plan for an explicitly "
        "complete book is learn_chapter, NOT custom; 制定学习计划 describes "
        "the requested output rather than an unclear learning action. "
        "If the conversation history suggests a review context, "
        "prefer \"review_chapter\" over \"learn_chapter\".\n"
        "12. days: the number of days the user wants to study (integer "
        "1-90). Default to 7 if not specified. Extract from phrases "
        "like \"三天\"→3, \"一周\"→7, \"两周\"→14.\n"
        "13. daily_minutes: minutes per day (integer 5-240). Default "
        "to 30 if not specified. Extract from phrases like "
        "\"每天30分钟\"→30, \"每天一小时\"→60.\n"
        "14. focus_text: the user's stated focus area in their own "
        "words (e.g. topic, key phrase, or specific concept they "
        "mentioned). null if nothing specific was stated.\n"
        "15. revision_constraints: OPTIONAL — only include this field "
        "when the user is clearly MODIFYING or ADJUSTING a previous "
        "plan (e.g. \"改成...\", \"减少...\", \"保持...不变\", "
        "\"增加到...\", \"缩短为...\"). When present, it must be a "
        "JSON object containing ONLY the relevant constraint fields. "
        "Omit this field entirely (do NOT set to null) when the user "
        "is requesting a new plan from scratch.\n"
        "  revision_constraints fields (ALL OPTIONAL — only include "
        "fields that the user explicitly stated):\n"
        "  - scope_relation: \"maintain\" (scope/chapters must stay "
        "the same), \"narrow\" (fewer chapters), \"expand\" (more "
        "chapters), \"unspecified\" (skip this field). Extract from "
        "phrases like \"范围不变\"→maintain, \"只学第三章\"→narrow, "
        "\"再加两章\"→expand.\n"
        "  - practice_count_relation: \"decrease\" (fewer practice "
        "tasks), \"maintain\" (same amount), \"increase\" (more "
        "practice), \"unspecified\" (skip). Extract from \"减少练习\""
        "→decrease, \"多加练习\"→increase.\n"
        "  - learn_count_relation: same as above for learning tasks.\n"
        "  - review_count_relation: same as above for review tasks.\n"
        "  - daily_minutes_max: an integer upper bound in minutes. "
        "Extract from phrases like \"每天最多30分钟\"→30, \"每次不"
        "超过20分钟\"→20. null if not specified.\n"
        "\n"
        "CRITICAL RULES:\n"
        "- intent MUST be one of: learn_chapter, review_chapter, "
        "review_weakness, custom. NEVER omit this field.\n"
        "- ALL entity IDs (scope_ids, key, core_id, node_id) MUST be "
        "copied VERBATIM from the candidates below. NEVER invent IDs.\n"
        "- parent_book_title MUST be copied VERBATIM from the textbooks "
        "list when the target is book-scoped.\n"
        "- For the \"前三章\" pattern: target_type=chapter_set, "
        "operation=first_n, ordinals=[1,2,3], parent_book_title must "
        "come from context or the most recently mentioned book.\n"
        "- For \"第三章 牙周病的发病机制\": target_type=chapter, "
        "operation=single, titles=[\"牙周病的发病机制\"], "
        "ordinals=[3], parent_book_title from context.\n"
        "- For just a book name (\"牙体材料学\"): target_type=book, "
        "operation=all, scope_ids empty, parent_book_title = the book itself. "
        "A bare book name identifies the book but does NOT confirm whole-book "
        "coverage; the resolver will ask for scope.\n"
        "- Only phrases that explicitly mean the complete material, such as "
        "整本/全书/全部章节/所有章节/系统学完, authorize a whole-book plan.\n"
        "- For disease (\"龋病\"): target_type=disease, "
        "operation=single, titles=[\"龋病\"].\n"
        "- For knowledge point (\"粘接剂的粘接机制\"): "
        "target_type=knowledge_point, operation=single.\n"
        "- For weakness (\"我的薄弱点\"): target_type=weakness_profile, "
        "operation=all.\n"
        "- When the message has NO scope hint (\"帮我制定计划\"), set "
        "target_type=unknown and context_dependent=true.\n"
        "- If the history shows a prior clarification question and the "
        "current message answers it (book name, chapter title, topic), "
        "resolve the answer against candidates — do NOT return unknown.\n"
        "- Output ONLY the JSON object — no markdown fences, no extra text.\n"
        "- When the user message contains CLEAR modification language "
        "(e.g. \"改成\", \"减少\", \"保持\", \"增加到\", \"缩短\", "
        "\"加长\", \"压缩到\"), include revision_constraints with "
        "only the fields the user explicitly stated. Omit "
        "revision_constraints entirely for new plan requests.\n"
        "\n"
        f"Available candidates:\n{candidates_text}\n"
        f"{history_text}"
        f"User request:\n{message}"
    )


def _validate_scope_selector_fields(parsed: dict[str, Any]) -> dict[str, Any]:
    """Validate the LLM scope selector JSON and return a normalised dict.

    Raises :class:`ScopeSelectorParseError` on any validation failure.
    """
    # --- reject extra / unknown fields ------------------------------------
    extra_fields = set(parsed.keys()) - _ALLOWED_SCOPE_SELECTOR_FIELDS
    if extra_fields:
        raise ScopeSelectorParseError(
            "invalid_field_type",
            f"Unexpected fields in LLM output: {sorted(extra_fields)}",
        )

    # --- required fields --------------------------------------------------
    for field in _SCOPE_SELECTOR_REQUIRED_FIELDS:
        if field not in parsed:
            raise ScopeSelectorParseError(
                "missing_required_field",
                f"Missing required field: {field}",
            )

    # --- target_type ------------------------------------------------------
    target_type = str(parsed.get("target_type", "")).strip()
    if target_type not in _ALLOWED_SCOPE_TARGET_TYPES:
        raise ScopeSelectorParseError(
            "invalid_target_type",
            f"target_type must be one of {sorted(_ALLOWED_SCOPE_TARGET_TYPES)}, "
            f"got {target_type!r}",
        )

    # --- operation --------------------------------------------------------
    raw_operation = parsed.get("operation")
    # A no-scope request is resolved entirely by target_type=unknown.  Some
    # providers emit JSON null for the otherwise inapplicable operation field.
    # Normalise only that semantically empty combination to the schema's
    # neutral placeholder so the existing resolver can return clarification.
    # Null remains invalid for every target that can select real content.
    if target_type == "unknown" and raw_operation is None:
        operation = "all"
    else:
        operation = str(raw_operation if raw_operation is not None else "").strip()
    if operation not in _ALLOWED_SCOPE_OPERATIONS:
        raise ScopeSelectorParseError(
            "invalid_operation",
            f"operation must be one of {sorted(_ALLOWED_SCOPE_OPERATIONS)}, "
            f"got {operation!r}",
        )

    # --- mention (required string) ----------------------------------------
    mention = parsed.get("mention")
    if not isinstance(mention, str):
        raise ScopeSelectorParseError(
            "invalid_field_type",
            f"mention must be a string, got {type(mention).__name__}",
        )

    # --- context_dependent (required bool) --------------------------------
    context_dep = parsed.get("context_dependent")
    if not isinstance(context_dep, bool):
        raise ScopeSelectorParseError(
            "invalid_field_type",
            f"context_dependent must be a boolean, got {type(context_dep).__name__}",
        )

    # --- optional fields: type check --------------------------------------
    for field in ("parent_book_title",):
        val = parsed.get(field)
        if val is not None and not isinstance(val, str):
            raise ScopeSelectorParseError(
                "invalid_field_type",
                f"{field} must be a string or null, got {type(val).__name__}",
            )

    for field in ("ordinals",):
        val = parsed.get(field)
        if val is not None:
            if not isinstance(val, list):
                raise ScopeSelectorParseError(
                    "invalid_field_type",
                    f"{field} must be a list, got {type(val).__name__}",
                )
            for i, item in enumerate(val):
                if not isinstance(item, (int, float)) or isinstance(item, bool):
                    raise ScopeSelectorParseError(
                        "invalid_field_type",
                        f"{field}[{i}] must be an integer, got {type(item).__name__}",
                    )

    for field in ("titles", "scope_ids", "exclude_ordinals"):
        val = parsed.get(field)
        if val is not None and not isinstance(val, list):
            raise ScopeSelectorParseError(
                "invalid_field_type",
                f"{field} must be a list or null, got {type(val).__name__}",
            )

    for field in ("range_start", "range_end"):
        val = parsed.get(field)
        if val is not None and not isinstance(val, (int, float, type(None))):
            raise ScopeSelectorParseError(
                "invalid_field_type",
                f"{field} must be an integer or null, got {type(val).__name__}",
            )

    # --- plan decorator fields (optional) ---------------------------------
    intent_val = parsed.get("intent")
    if intent_val is not None:
        if not isinstance(intent_val, str):
            raise ScopeSelectorParseError(
                "invalid_field_type",
                f"intent must be a string, got {type(intent_val).__name__}",
            )
        intent_str = str(intent_val).strip()
        if intent_str and intent_str not in _ALLOWED_INTENTS:
            raise ScopeSelectorParseError(
                "invalid_intent",
                f"intent must be one of {sorted(_ALLOWED_INTENTS)}, "
                f"got {intent_str!r}",
            )

    for field in ("days", "daily_minutes"):
        val = parsed.get(field)
        if val is not None:
            if isinstance(val, bool):
                raise ScopeSelectorParseError(
                    "invalid_field_type",
                    f"{field} must be an integer or null, got bool",
                )
            if not isinstance(val, (int, float)):
                raise ScopeSelectorParseError(
                    "invalid_field_type",
                    f"{field} must be an integer or null, got {type(val).__name__}",
                )

    focus_val = parsed.get("focus_text")
    if focus_val is not None and not isinstance(focus_val, str):
        raise ScopeSelectorParseError(
            "invalid_field_type",
            f"focus_text must be a string or null, got {type(focus_val).__name__}",
        )

    # --- revision_constraints (optional dict) -------------------------------
    raw_rc = parsed.get("revision_constraints")
    normalized_rc: dict[str, Any] | None = None
    if raw_rc is not None:
        if not isinstance(raw_rc, dict):
            raise ScopeSelectorParseError(
                "invalid_field_type",
                f"revision_constraints must be a dict or null, "
                f"got {type(raw_rc).__name__}",
            )
        # Reject unknown keys — the LLM must only use the defined set
        extra_rc_keys = set(raw_rc.keys()) - _ALLOWED_REVISION_CONSTRAINT_KEYS
        if extra_rc_keys:
            raise ScopeSelectorParseError(
                "invalid_field_type",
                f"Unexpected keys in revision_constraints: "
                f"{sorted(extra_rc_keys)}. "
                f"Allowed: {sorted(_ALLOWED_REVISION_CONSTRAINT_KEYS)}",
            )
        normalized_rc = {}
        # --- scope_relation ------------------------------------------------
        sr = raw_rc.get("scope_relation")
        if sr is not None:
            if not isinstance(sr, str):
                raise ScopeSelectorParseError(
                    "invalid_field_type",
                    f"revision_constraints.scope_relation must be a string "
                    f"or null, got {type(sr).__name__}",
                )
            sr_s = str(sr).strip()
            if sr_s not in _ALLOWED_REVISION_SCOPE_RELATIONS:
                raise ScopeSelectorParseError(
                    "invalid_field_type",
                    f"revision_constraints.scope_relation must be one of "
                    f"{sorted(_ALLOWED_REVISION_SCOPE_RELATIONS)}, "
                    f"got {sr_s!r}",
                )
            if sr_s != "unspecified":
                normalized_rc["scope_relation"] = sr_s
        # --- practice_count_relation ---------------------------------------
        pcr = raw_rc.get("practice_count_relation")
        if pcr is not None:
            if not isinstance(pcr, str):
                raise ScopeSelectorParseError(
                    "invalid_field_type",
                    f"revision_constraints.practice_count_relation must be "
                    f"a string or null, got {type(pcr).__name__}",
                )
            pcr_s = str(pcr).strip()
            if pcr_s not in _ALLOWED_REVISION_COUNT_RELATIONS:
                raise ScopeSelectorParseError(
                    "invalid_field_type",
                    f"revision_constraints.practice_count_relation must be "
                    f"one of {sorted(_ALLOWED_REVISION_COUNT_RELATIONS)}, "
                    f"got {pcr_s!r}",
                )
            if pcr_s != "unspecified":
                normalized_rc["practice_count_relation"] = pcr_s
        # --- learn_count_relation ------------------------------------------
        lcr = raw_rc.get("learn_count_relation")
        if lcr is not None:
            if not isinstance(lcr, str):
                raise ScopeSelectorParseError(
                    "invalid_field_type",
                    f"revision_constraints.learn_count_relation must be "
                    f"a string or null, got {type(lcr).__name__}",
                )
            lcr_s = str(lcr).strip()
            if lcr_s not in _ALLOWED_REVISION_COUNT_RELATIONS:
                raise ScopeSelectorParseError(
                    "invalid_field_type",
                    f"revision_constraints.learn_count_relation must be "
                    f"one of {sorted(_ALLOWED_REVISION_COUNT_RELATIONS)}, "
                    f"got {lcr_s!r}",
                )
            if lcr_s != "unspecified":
                normalized_rc["learn_count_relation"] = lcr_s
        # --- review_count_relation -----------------------------------------
        rcr = raw_rc.get("review_count_relation")
        if rcr is not None:
            if not isinstance(rcr, str):
                raise ScopeSelectorParseError(
                    "invalid_field_type",
                    f"revision_constraints.review_count_relation must be "
                    f"a string or null, got {type(rcr).__name__}",
                )
            rcr_s = str(rcr).strip()
            if rcr_s not in _ALLOWED_REVISION_COUNT_RELATIONS:
                raise ScopeSelectorParseError(
                    "invalid_field_type",
                    f"revision_constraints.review_count_relation must be "
                    f"one of {sorted(_ALLOWED_REVISION_COUNT_RELATIONS)}, "
                    f"got {rcr_s!r}",
                )
            if rcr_s != "unspecified":
                normalized_rc["review_count_relation"] = rcr_s
        # --- daily_minutes_max ---------------------------------------------
        dmm = raw_rc.get("daily_minutes_max")
        if dmm is not None:
            if isinstance(dmm, bool):
                raise ScopeSelectorParseError(
                    "invalid_field_type",
                    "revision_constraints.daily_minutes_max must be an "
                    "integer or null, got bool",
                )
            if not isinstance(dmm, (int, float)):
                raise ScopeSelectorParseError(
                    "invalid_field_type",
                    f"revision_constraints.daily_minutes_max must be an "
                    f"integer or null, got {type(dmm).__name__}",
                )
            dmm_i = int(dmm)
            if dmm_i < 5 or dmm_i > 240:
                raise ScopeSelectorParseError(
                    "invalid_field_type",
                    f"revision_constraints.daily_minutes_max must be "
                    f"between 5 and 240, got {dmm_i}",
                )
            normalized_rc["daily_minutes_max"] = dmm_i
        # Normalize to None when all fields were "unspecified" / omitted
        if not normalized_rc:
            normalized_rc = None

    # --- build normalised result ------------------------------------------
    return {
        "target_type": target_type,
        "mention": str(mention).strip(),
        "operation": operation,
        "ordinals": [
            int(o) for o in (parsed.get("ordinals") or [])
            if isinstance(o, (int, float)) and not isinstance(o, bool)
        ],
        "titles": [
            str(t).strip() for t in (parsed.get("titles") or [])
            if isinstance(t, str) and t.strip()
        ],
        "scope_ids": [
            str(s).strip() for s in (parsed.get("scope_ids") or [])
            if isinstance(s, str) and s.strip()
        ],
        "range_start": (
            int(parsed["range_start"])
            if parsed.get("range_start") is not None
            and isinstance(parsed["range_start"], (int, float))
            and not isinstance(parsed["range_start"], bool)
            else None
        ),
        "range_end": (
            int(parsed["range_end"])
            if parsed.get("range_end") is not None
            and isinstance(parsed["range_end"], (int, float))
            and not isinstance(parsed["range_end"], bool)
            else None
        ),
        "exclude_ordinals": [
            int(o) for o in (parsed.get("exclude_ordinals") or [])
            if isinstance(o, (int, float)) and not isinstance(o, bool)
        ],
        "parent_book_title": (
            str(parsed["parent_book_title"]).strip()
            if parsed.get("parent_book_title")
            and isinstance(parsed["parent_book_title"], str)
            else None
        ),
        "context_dependent": context_dep,
        # Plan decorator fields (optional — defaults applied by caller)
        "intent": (
            str(intent_val).strip()
            if intent_val is not None
            and isinstance(intent_val, str)
            and str(intent_val).strip()
            else None
        ),
        "days": (
            int(parsed["days"])
            if parsed.get("days") is not None
            and isinstance(parsed["days"], (int, float))
            and not isinstance(parsed["days"], bool)
            else None
        ),
        "daily_minutes": (
            int(parsed["daily_minutes"])
            if parsed.get("daily_minutes") is not None
            and isinstance(parsed["daily_minutes"], (int, float))
            and not isinstance(parsed["daily_minutes"], bool)
            else None
        ),
        "focus_text": (
            str(parsed["focus_text"]).strip()
            if parsed.get("focus_text") is not None
            and isinstance(parsed["focus_text"], str)
            and str(parsed["focus_text"]).strip()
            else None
        ),
        # Revision constraints — LLM-interpreted modification semantics
        "revision_constraints": normalized_rc,
    }


def _resolve_scope_selector(
    selector: dict[str, Any],
    chapters: list[dict[str, Any]],
    *,
    memory: Any | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Resolve a validated scope selector against real candidate data.

    Returns a dict with:
    - ``resolved_scopes``: ordered list of scope dicts, each with real IDs
      and provenance
    - ``clarification_needed``: bool — True when scope cannot be uniquely
      resolved
    - ``clarification_message``: specific message when clarification needed

    Resolution rules:
    - Unique high-confidence match → direct landing
    - Zero candidates → specific "not found" message
    - Multiple reliable candidates → list them for user choice
    - Never fall back to "all books" or first random match
    """
    # Build lookup indices from chapters
    chapters_by_book: dict[str, list[dict[str, Any]]] = {}
    chapter_by_key: dict[str, dict[str, Any]] = {}
    for ch in chapters:
        if not isinstance(ch, dict):
            continue
        ckey = str(ch.get("key", "")).strip()
        bt = str(ch.get("book_title", "")).strip()
        if ckey:
            chapter_by_key[ckey] = ch
        if bt:
            chapters_by_book.setdefault(bt, []).append(ch)

    # Sort chapters within each book
    for bt in chapters_by_book:
        chapters_by_book[bt].sort(
            key=lambda c: (
                int(c.get("number", 0) or 0),
                str(c.get("title", "")).strip(),
            )
        )

    # Build knowledge point index from chapter cores
    core_by_id: dict[str, dict[str, Any]] = {}
    for ch in chapters:
        if not isinstance(ch, dict):
            continue
        cores = ch.get("cores")
        if not isinstance(cores, list):
            continue
        for core in cores:
            if not isinstance(core, dict):
                continue
            cid = str(core.get("id", "")).strip()
            if cid:
                core_by_id[cid] = {
                    "id": cid,
                    "name": str(core.get("name", "")).strip(),
                    "type": str(core.get("type", "")).strip(),
                    "type_label": str(core.get("type_label", "")).strip(),
                    "chapter_key": str(ch.get("key", "")).strip(),
                    "book_title": str(ch.get("book_title", "")).strip(),
                    "document_id": str(ch.get("document_id", "")).strip(),
                }

    # Build disease index from KG nodes
    disease_by_id: dict[str, dict[str, Any]] = {}
    try:
        from backend.services.knowledge_graph.agent import get_graph_overview
        overview = get_graph_overview()
        if overview.get("ok"):
            for node in overview.get("overview", {}).get("nodes", []):
                if not isinstance(node, dict):
                    continue
                ntype = str(node.get("type", "")).strip()
                if ntype not in ("Disease", "DiseaseSubtype"):
                    continue
                nid = str(node.get("id", "")).strip()
                if nid:
                    disease_by_id[nid] = {
                        "id": nid,
                        "name": str(node.get("name", "")).strip(),
                        "type": ntype,
                        "type_label": str(node.get("type_label", "")).strip() or ntype,
                    }
    except Exception:
        pass  # Disease data unavailable — resolver handles gracefully

    target_type = selector["target_type"]
    operation = selector["operation"]
    parent_book = selector.get("parent_book_title")

    # ------------------------------------------------------------------
    # Helper: normalize a book title by stripping common formatting marks
    # that LLMs may add (e.g. 《》 from Chinese book-title markers) so
    # that lookup against bare KG book_title keys succeeds.
    # ------------------------------------------------------------------
    def _normalize_book_title(raw: str | None) -> str:
        if not raw:
            return ""
        s = str(raw).strip()
        # Strip paired Chinese book-title marks
        if s.startswith("《") and s.endswith("》"):
            s = s[1:-1].strip()
        # Strip remaining stray marks
        for ch in ("《", "》", "「", "」", "『", "』", "（", "）", "(", ")"):
            s = s.replace(ch, "")
        return s.strip()

    # ------------------------------------------------------------------
    # Helper: resolve chapters by operation within a book
    # ------------------------------------------------------------------
    def _resolve_chapters_in_book(
        book_title: str,
        op: str,
        ordinals: list[int],
        titles: list[str],
        scope_ids: list[str],
        range_start: int | None,
        range_end: int | None,
        exclude_ordinals: list[int],
    ) -> list[dict[str, Any]]:
        # Try exact match first, then normalized match
        ch_list = chapters_by_book.get(book_title, [])
        if not ch_list:
            normalized = _normalize_book_title(book_title)
            if normalized and normalized != book_title:
                ch_list = chapters_by_book.get(normalized, [])
        if not ch_list:
            return []

        if op == "all":
            selected = list(ch_list)
        elif op == "first_n":
            n = ordinals[-1] if ordinals else 1
            selected = ch_list[:n]
        elif op == "last_n":
            n = ordinals[-1] if ordinals else 1
            selected = ch_list[-n:] if n <= len(ch_list) else list(ch_list)
        elif op == "range":
            s = (range_start or 1) - 1
            e = range_end or len(ch_list)
            selected = ch_list[max(0, s):min(e, len(ch_list))]
        elif op == "list":
            selected = []
            # By ordinal
            for o in ordinals:
                if 1 <= o <= len(ch_list):
                    selected.append(ch_list[o - 1])
            # By scope_ids (chapter key)
            for sid in scope_ids:
                ch = chapter_by_key.get(sid)
                if ch and ch not in selected:
                    selected.append(ch)
            # By titles (fuzzy match against chapter title)
            for title in titles:
                title_lower = title.lower()
                for ch in ch_list:
                    ctitle = str(ch.get("title", "")).strip()
                    if ctitle.lower() == title_lower or title_lower in ctitle.lower():
                        if ch not in selected:
                            selected.append(ch)
                        break
        elif op == "single":
            selected = []
            # Try exact chapter key first
            for sid in scope_ids:
                ch = chapter_by_key.get(sid)
                if ch:
                    selected.append(ch)
                    break
            if not selected:
                # Try ordinal
                for o in ordinals:
                    if 1 <= o <= len(ch_list):
                        selected.append(ch_list[o - 1])
                        break
            if not selected:
                # Try title fuzzy match
                for title in titles:
                    title_lower = title.lower()
                    for ch in ch_list:
                        ctitle = str(ch.get("title", "")).strip()
                        if ctitle.lower() == title_lower or title_lower in ctitle.lower():
                            selected.append(ch)
                            break
        elif op == "exclude":
            exclude_set = set(exclude_ordinals)
            selected = [
                ch for i, ch in enumerate(ch_list)
                if (i + 1) not in exclude_set
            ]
        else:
            selected = []

        # Remove excluded ordinals (applies to all ops)
        if exclude_ordinals and op != "exclude":
            exclude_set = set(exclude_ordinals)
            selected = [
                ch for i, ch in enumerate(selected)
                if (i + 1) not in exclude_set
            ]

        return selected

    # ------------------------------------------------------------------
    # Main resolution by target_type
    # ------------------------------------------------------------------
    resolved: list[dict[str, Any]] = []

    if target_type == "book":
        # Find matching book
        matched_book = None
        for bt in chapters_by_book:
            if selector["mention"].lower() in bt.lower() or bt.lower() in selector["mention"].lower():
                matched_book = bt
                break
        if not matched_book and selector.get("titles"):
            for title in selector["titles"]:
                for bt in chapters_by_book:
                    if title.lower() in bt.lower() or bt.lower() in title.lower():
                        matched_book = bt
                        break
        if not matched_book and selector.get("parent_book_title"):
            pbt = selector["parent_book_title"]
            for bt in chapters_by_book:
                if pbt.lower() in bt.lower() or bt.lower() in pbt.lower():
                    matched_book = bt
                    break
        if matched_book:
            ch_list = chapters_by_book[matched_book]
            doc_id = str(ch_list[0].get("document_id", "")).strip() if ch_list else None
            # Return the book as a scope (not individual chapters)
            resolved.append({
                "scope_ref": matched_book,
                "scope_type": "book",
                "book_title": matched_book,
                "document_id": doc_id,
                "chapter_title": None,
                "kg_node_ids": [],
                "kg_node_names": {},
                "provenance": "kg_chapter",
                "chapter_count": len(ch_list),
            })

    elif target_type in ("chapter", "chapter_set"):
        candidates = _resolve_chapters_in_book(
            parent_book or "",
            operation,
            selector.get("ordinals", []),
            selector.get("titles", []),
            selector.get("scope_ids", []),
            selector.get("range_start"),
            selector.get("range_end"),
            selector.get("exclude_ordinals", []),
        )

        # If parent_book didn't yield candidates, try:
        #   (a) normalized match against each book in chapters_by_book
        #   (b) fuzzy substring match across all books
        if not candidates and parent_book:
            norm_parent = _normalize_book_title(parent_book)
            for bt in chapters_by_book:
                if norm_parent and (
                    norm_parent.lower() in bt.lower()
                    or bt.lower() in norm_parent.lower()
                    or _normalize_book_title(bt).lower() == norm_parent.lower()
                ):
                    candidates = _resolve_chapters_in_book(
                        bt, operation, selector.get("ordinals", []),
                        selector.get("titles", []), selector.get("scope_ids", []),
                        selector.get("range_start"), selector.get("range_end"),
                        selector.get("exclude_ordinals", []),
                    )
                    if candidates:
                        parent_book = bt
                        break
        # If no parent_book at all, try fuzzy matching across all books
        if not candidates and not parent_book:
            for bt in chapters_by_book:
                candidates = _resolve_chapters_in_book(
                    bt, operation, selector.get("ordinals", []),
                    selector.get("titles", []), selector.get("scope_ids", []),
                    selector.get("range_start"), selector.get("range_end"),
                    selector.get("exclude_ordinals", []),
                )
                if candidates:
                    parent_book = bt
                    break

        for ch in candidates:
            ckey = str(ch.get("key", "")).strip()
            scope = resolve_learning_scope(
                chapters,
                chapter_key=ckey,
                book_title=str(ch.get("book_title", "")).strip() or None,
                document_id=str(ch.get("document_id", "")).strip() or None,
            )
            # Enrich with weakness evidence if memory available
            has_weakness = False
            weakness_detail: list[dict[str, Any]] = []
            if memory is not None and user_id is not None:
                has_weakness = _check_weakness_evidence(
                    memory, user_id, scope["kg_node_ids"]
                )
                weakness_detail = _collect_weakness_detail(
                    memory, user_id, scope["kg_node_ids"],
                    scope.get("kg_node_names", {}),
                )

            resolved.append({
                "scope_ref": ckey,
                "scope_type": "chapter",
                "book_title": scope["book_title"],
                "document_id": scope["document_id"],
                "chapter_title": scope["chapter_title"],
                "kg_node_ids": scope["kg_node_ids"],
                "kg_node_names": scope.get("kg_node_names", {}),
                "provenance": "kg_chapter",
                "has_weakness_evidence": has_weakness,
                "weakness_detail": weakness_detail,
            })

    elif target_type == "knowledge_point":
        for sid in selector.get("scope_ids", []):
            core = core_by_id.get(sid)
            if core:
                # Find parent chapter for context
                parent_ch = chapter_by_key.get(core["chapter_key"])
                resolved.append({
                    "scope_ref": core["id"],
                    "scope_type": "knowledge_point",
                    "book_title": core.get("book_title"),
                    "document_id": core.get("document_id"),
                    "chapter_title": str(parent_ch.get("title", "")).strip() if parent_ch else None,
                    "chapter_key": core.get("chapter_key"),
                    "kg_node_ids": [core["id"]],
                    "kg_node_names": {core["id"]: core["name"]} if core["name"] else {},
                    "provenance": "kg_core",
                    "kp_name": core["name"],
                    "kp_type_label": core["type_label"],
                })

        # Fuzzy match by title if no IDs
        if not resolved:
            for title in selector.get("titles", []):
                title_lower = title.lower()
                for core_id, core in core_by_id.items():
                    if title_lower in core["name"].lower():
                        parent_ch = chapter_by_key.get(core["chapter_key"])
                        resolved.append({
                            "scope_ref": core["id"],
                            "scope_type": "knowledge_point",
                            "book_title": core.get("book_title"),
                            "document_id": core.get("document_id"),
                            "chapter_title": str(parent_ch.get("title", "")).strip() if parent_ch else None,
                            "chapter_key": core.get("chapter_key"),
                            "kg_node_ids": [core["id"]],
                            "kg_node_names": {core["id"]: core["name"]} if core["name"] else {},
                            "provenance": "kg_core",
                            "kp_name": core["name"],
                            "kp_type_label": core["type_label"],
                        })
                        break
                if resolved:
                    break

    elif target_type == "disease":
        for sid in selector.get("scope_ids", []):
            disease = disease_by_id.get(sid)
            if disease:
                resolved.append({
                    "scope_ref": disease["id"],
                    "scope_type": "disease",
                    "book_title": None,
                    "document_id": None,
                    "chapter_title": None,
                    "kg_node_ids": [disease["id"]],
                    "kg_node_names": {disease["id"]: disease["name"]} if disease["name"] else {},
                    "provenance": "kg_core",
                    "disease_name": disease["name"],
                    "disease_type_label": disease["type_label"],
                })

        # Fuzzy match by title
        if not resolved:
            for title in selector.get("titles", []):
                title_lower = title.lower()
                for did, disease in disease_by_id.items():
                    if title_lower in disease["name"].lower():
                        resolved.append({
                            "scope_ref": disease["id"],
                            "scope_type": "disease",
                            "book_title": None,
                            "document_id": None,
                            "chapter_title": None,
                            "kg_node_ids": [disease["id"]],
                            "kg_node_names": {disease["id"]: disease["name"]} if disease["name"] else {},
                            "provenance": "kg_core",
                            "disease_name": disease["name"],
                            "disease_type_label": disease["type_label"],
                        })
                        break
                if resolved:
                    break

    elif target_type == "weakness_profile":
        if memory is not None and user_id is not None:
            try:
                weakness_items = memory.get_weakness(user_id, limit=20)
                for wi in (weakness_items or [])[:20]:
                    nid = str(wi.get("kg_node_id", "")).strip()
                    nname = str(wi.get("kg_node_name", "")).strip()
                    if nid:
                        resolved.append({
                            "scope_ref": nid,
                            "scope_type": "weakness_profile",
                            "book_title": str(wi.get("book_title", "")).strip() or None,
                            "document_id": str(wi.get("document_id", "")).strip() or None,
                            "chapter_title": str(wi.get("chapter_title", "")).strip() or None,
                            "kg_node_ids": [nid],
                            "kg_node_names": {nid: nname} if nname else {},
                            "provenance": "weakness_profile",
                            "has_weakness_evidence": True,
                            "weakness_detail": [{
                                "kg_node_id": nid,
                                "kg_node_name": nname,
                                "evidence_source": "learner_memory_weakness",
                            }],
                        })
            except Exception:
                pass  # weakness data unavailable

    # ------------------------------------------------------------------
    # Clarification logic
    # ------------------------------------------------------------------
    clarification_needed = False
    clarification_message: str | None = None

    if target_type == "unknown":
        clarification_needed = True
        clarification_message = (
            '请告诉我您想学习的教材章节或主题，也可以直接说"薄弱点"，'
            '我会根据您的学习情况安排复习。'
            '目前支持《牙周病学》《口腔颌面外科学》等口腔医学教材。'
        )
    elif not resolved:
        # Zero candidates found
        mention = selector.get("mention", "")
        clarification_needed = True
        if mention:
            clarification_message = (
                f'未找到与「{mention}」匹配的学习内容。'
                '请确认教材名称、章节号或知识点名称是否正确。'
            )
        else:
            clarification_message = (
                "未找到匹配的学习内容。请提供更具体的教材名称、"
                "章节或知识点信息。"
            )
    elif len(resolved) > 1 and operation == "single":
        # Multiple candidates for single operation → list them
        options = []
        for i, sc in enumerate(resolved, 1):
            options.append(
                f"  [{i}] {sc.get('chapter_title') or sc.get('kp_name') or sc.get('disease_name') or sc['scope_ref']}"
                + (f"（《{sc['book_title']}》）" if sc.get("book_title") else "")
            )
        if len(options) <= 5:
            clarification_needed = True
            clarification_message = (
                "找到多个匹配结果，请问您想学习哪一个？\n"
                + "\n".join(options)
            )

    return {
        "resolved_scopes": resolved,
        "clarification_needed": clarification_needed,
        "clarification_message": clarification_message,
        "target_type": target_type,
        "mention": selector["mention"],
        "context_dependent": selector["context_dependent"],
    }


def _adapt_old_request_to_scope_selector(
    request: dict[str, Any],
) -> dict[str, Any]:
    """Convert an old-style parsed request (chapter_key/book_title format)
    into a unified scope selector dict for backward compatibility.

    This is a thin adapter — when the old parse path produces chapter_key
    fields, this converts them to the new selector format so the resolver
    can handle them uniformly.
    """
    intent = str(request.get("intent", "")).strip()
    chapter_key = request.get("chapter_key")
    book_title = request.get("book_title")
    document_id = request.get("document_id")

    if chapter_key:
        return {
            "target_type": "chapter",
            "mention": str(request.get("focus_text") or f"chapter {chapter_key}"),
            "operation": "single",
            "ordinals": [],
            "titles": [],
            "scope_ids": [str(chapter_key).strip()],
            "range_start": None,
            "range_end": None,
            "exclude_ordinals": [],
            "parent_book_title": (
                str(book_title).strip()
                if book_title and str(book_title).strip()
                else None
            ),
            "context_dependent": False,
            "resolved_book_title": str(book_title).strip() if book_title else None,
            "resolved_document_id": str(document_id).strip() if document_id else None,
        }
    elif book_title:
        # Whole book request (old style: chapter_key=null, book_title set)
        return {
            "target_type": "book",
            "mention": str(book_title).strip(),
            "operation": "all",
            "ordinals": [],
            "titles": [str(book_title).strip()],
            "scope_ids": [],
            "range_start": None,
            "range_end": None,
            "exclude_ordinals": [],
            "parent_book_title": str(book_title).strip(),
            "context_dependent": False,
            "resolved_book_title": str(book_title).strip(),
            "resolved_document_id": str(document_id).strip() if document_id else None,
        }
    elif intent == "review_weakness":
        return {
            "target_type": "weakness_profile",
            "mention": "薄弱点",
            "operation": "all",
            "ordinals": [],
            "titles": [],
            "scope_ids": [],
            "range_start": None,
            "range_end": None,
            "exclude_ordinals": [],
            "parent_book_title": None,
            "context_dependent": False,
        }
    else:
        return {
            "target_type": "unknown",
            "mention": str(request.get("focus_text", "")).strip() or "无范围",
            "operation": "all",
            "ordinals": [],
            "titles": [],
            "scope_ids": [],
            "range_start": None,
            "range_end": None,
            "exclude_ordinals": [],
            "parent_book_title": None,
            "context_dependent": False,
        }



def parse_scope_selector_from_text(
    message: str,
    *,
    chapters: list[dict[str, Any]] | None = None,
    llm_call: Callable[[str], str] | None = None,
    history: list[dict[str, Any]] | None = None,
    memory: Any | None = None,
    user_id: str | None = None,
    _compat_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Parse a natural-language message into a unified scope selector + resolution.

    This is a **unified entry point** that replaces the old entity-type-specific
    parsing.  It calls an LLM with a single prompt that produces a
    ``ScopeSelector`` JSON, validates the selector, and resolves it against
    real candidate data.

    The returned dict carries ``scope_selector`` (the validated selector),
    ``resolved_scopes`` (ordered list of real scopes), and
    ``clarification_needed`` / ``clarification_message``.

    Parameters
    ----------
    message:
        Non-empty user natural-language text.
    chapters:
        Chapter candidate dicts from the KG overview.  May be None or empty.
    llm_call:
        Injectable ``(prompt: str) -> str`` callable.
    history:
        Optional conversation history for multi-turn resolution.
    memory:
        Optional :class:`LearningMemory` for weakness evidence.
    user_id:
        Required when *memory* is provided.
    _compat_state:
        Optional shared mutable dict for JSON-mode compatibility tracking.

    Returns
    -------
    dict
        With keys: ``scope_selector``, ``resolved_scopes``,
        ``clarification_needed``, ``clarification_message``,
        ``target_type``, ``mention``, ``context_dependent``.

    Raises
    ------
    ScopeSelectorParseError
        On parse/validation failures.
    """
    # ------------------------------------------------------------------
    # 1. Validate input
    # ------------------------------------------------------------------
    if not message or not str(message).strip():
        raise ScopeSelectorParseError(
            "empty_message", "message must be a non-empty string"
        )

    clean_message = str(message).strip()
    _chapters = [ch for ch in (chapters or []) if isinstance(ch, dict)]

    # ------------------------------------------------------------------
    # 2. Build candidate summary
    # ------------------------------------------------------------------
    candidates_text = _build_unified_candidate_summary(
        _chapters, memory=memory, user_id=user_id,
    )

    # ------------------------------------------------------------------
    # 3. Build prompt
    # ------------------------------------------------------------------
    prompt = _build_scope_selector_prompt(
        clean_message, candidates_text, history=history,
    )

    # ------------------------------------------------------------------
    # 4. Resolve LLM callable
    # ------------------------------------------------------------------
    if llm_call is not None:
        _call = llm_call
    else:
        try:
            _call = _make_default_llm_call(
                max_tokens=1200, json_mode=True, _compat_state=_compat_state,
            )
        except Exception as exc:
            raise ScopeSelectorParseError(
                "llm_not_configured",
                f"Failed to create default LLM callable: {exc}",
            ) from exc

    # ------------------------------------------------------------------
    # 5. Call LLM
    # ------------------------------------------------------------------
    try:
        raw_output = _call(prompt)
    except Exception as exc:
        exc_type_name = type(exc).__name__
        if "Config" in exc_type_name or "configured" in str(exc).lower():
            raise ScopeSelectorParseError(
                "llm_not_configured", f"LLM call failed: {exc}",
            ) from exc
        raise ScopeSelectorParseError(
            "llm_request_failed", f"LLM call failed: {exc}",
        ) from exc

    if not raw_output or not isinstance(raw_output, str) or not raw_output.strip():
        raise ScopeSelectorParseError(
            "invalid_json", "LLM returned empty or non-string output"
        )

    # ------------------------------------------------------------------
    # 6. Parse JSON (with bounded truncation recovery)
    # ------------------------------------------------------------------
    try:
        parsed = _extract_json(raw_output)
    except (json.JSONDecodeError, ValueError) as exc:
        exc_msg = str(exc)
        # Detect truncation / incomplete JSON (e.g. unterminated string,
        # unexpected EOF) and retry ONCE with a much higher output budget.
        # Only trigger when raw_output looks like a plausible but
        # truncated LLM response (>100 chars, starts with '{' or '```').
        raw_stripped = raw_output.strip()
        is_truncation = (
            len(raw_output) > 100
            and (raw_stripped.startswith("{") or raw_stripped.startswith("```"))
            and any(
                kw in exc_msg.lower()
                for kw in ("unterminated string", "unexpected end of data",
                           "unexpected eof", "truncat")
            )
        )
        if is_truncation:
            _sl = logging.getLogger(__name__)
            _sl.warning(
                "Scope selector JSON appears truncated (%s); "
                "retrying once with increased max_tokens=4096. "
                "raw_len=%s",
                exc_msg[:120],
                len(raw_output),
            )
            try:
                retry_call = _make_default_llm_call(
                    max_tokens=4096, json_mode=True, _compat_state=_compat_state,
                )
                raw_retry = retry_call(prompt)
                if raw_retry and isinstance(raw_retry, str) and raw_retry.strip():
                    parsed = _extract_json(raw_retry)
                else:
                    raise ScopeSelectorParseError(
                        "invalid_json",
                        f"Truncation retry also returned empty. Original error: {exc_msg[:200]}",
                    )
            except (json.JSONDecodeError, ValueError) as exc2:
                raise ScopeSelectorParseError(
                    "invalid_json",
                    f"Failed to parse LLM output as JSON after truncation retry: {exc2}",
                ) from exc2
            except ScopeSelectorParseError:
                raise
            except Exception as exc2:
                raise ScopeSelectorParseError(
                    "llm_request_failed",
                    f"Truncation retry LLM call failed: {exc2}",
                ) from exc2
        else:
            raise ScopeSelectorParseError(
                "invalid_json", f"Failed to parse LLM output as JSON: {exc}",
            ) from exc

    # ------------------------------------------------------------------
    # 7. Validate scope selector fields
    # ------------------------------------------------------------------
    selector = _validate_scope_selector_fields(parsed)
    _stabilize_explicit_whole_book_intent(clean_message, selector)

    # ------------------------------------------------------------------
    # 8. Resolve against real candidates
    # ------------------------------------------------------------------
    resolution = _resolve_scope_selector(
        selector, _chapters, memory=memory, user_id=user_id,
    )

    if (
        selector.get("target_type") == "book"
        and resolution.get("resolved_scopes")
        and not _is_explicit_whole_book_request(clean_message)
    ):
        book_title = str(
            resolution["resolved_scopes"][0].get("book_title")
            or selector.get("parent_book_title")
            or selector.get("mention")
            or ""
        ).strip(" 《》")
        resolution["resolved_scopes"] = []
        resolution["clarification_needed"] = True
        resolution["clarification_message"] = (
            f"已识别到《{book_title}》，但还不能确定您希望覆盖的学习范围。"
            "请选择具体章节，或明确说明要规划整本/全书；"
            "也可以按您的真实薄弱点安排复习。"
        )
        resolution["clarification_options"] = _book_scope_clarification_options(
            book_title
        )

    return {
        "scope_selector": selector,
        "resolved_scopes": resolution["resolved_scopes"],
        "clarification_needed": resolution["clarification_needed"],
        "clarification_message": resolution["clarification_message"],
        "target_type": resolution["target_type"],
        "mention": resolution["mention"],
        "context_dependent": resolution["context_dependent"],
        "clarification_options": resolution.get("clarification_options", []),
    }


# ---------------------------------------------------------------------------
# LLM orchestration contract — context builder and output validator
# ---------------------------------------------------------------------------

_ALLOWED_ORCHESTRATION_TASK_TYPES = frozenset({
    "learn_chapter",
    "review_chapter",
    "basic_practice",
    "advanced_practice",
})


class OrchestrationValidationError(ValueError):
    """Raised when an LLM orchestration candidate fails validation.

    Attributes
    ----------
    code : str
        Stable machine-readable error code that callers can switch on.

        Possible values:

        * ``"invalid_candidate"`` — candidate is not a dict
        * ``"invalid_items"`` — items is not a list
        * ``"clarification_with_items"`` — clarification_needed=true but items
          is non-empty
        * ``"clarification_with_scope_candidates"`` — clarification_needed=true
          but scope_candidates is non-empty (scope already resolved)
        * ``"missing_clarification_message"`` — clarification_needed=true but
          clarification_message is missing, empty, or not a string
        * ``"empty_plan"`` — no clarification and items is empty
        * ``"invalid_context"`` — context.scope_candidates is not a list
        * ``"invalid_item"`` — an item is not a dict
        * ``"invalid_task_type"`` — task_type not in the allowed set
        * ``"missing_scope_ref"`` — scope_ref is empty or missing
        * ``"invalid_scope_ref"`` — scope_ref not found in context
        * ``"advanced_practice_without_weakness"`` — advanced_practice
          requested for a scope without weakness evidence
        * ``"missing_day_offset"`` — day_offset is missing
        * ``"invalid_day_offset"`` — day_offset is not a non-negative integer
        * ``"day_offset_exceeds_total_days"`` — day_offset >= total_days
        * ``"missing_minutes"`` — minutes is missing
        * ``"invalid_minutes"`` — minutes is not a positive integer
        * ``"minutes_exceeds_daily_budget"`` — single item minutes > daily
        * ``"missing_reason"`` — reason is empty or missing
        * ``"invalid_reason"`` — reason is not a string
        * ``"invalid_dependency"`` — dependency is not a valid index or null
        * ``"daily_budget_exceeded"`` — day total minutes > daily_minutes
        * ``"scope_coverage_incomplete"`` — whole-book plan does not reference
          all scope_candidates from context
        * ``"task_type_composition_incomplete"`` — whole-book plan is missing
          one or more required task types (learn_chapter, review_chapter,
          basic_practice)
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


_ORCHESTRATION_SCHEDULE_ERROR_CODES = frozenset({
    "missing_day_offset",
    "invalid_day_offset",
    "day_offset_exceeds_total_days",
    "missing_minutes",
    "invalid_minutes",
    "minutes_exceeds_daily_budget",
    "daily_budget_exceeded",
})


def _compute_parent_metrics(
    parent_items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compute measurable parent-draft metrics for revision comparison.

    Takes the parent draft's items list and returns a dict with:
    - ``scope_refs``: frozenset of all scope_ref values
    - ``counts_by_type``: dict mapping task_type → count
    - ``total_items``: total number of items
    - ``daily_minutes``: None (filled by caller from parent request)

    This is a pure helper — no side effects, no LLM calls.
    """
    scope_refs: set[str] = set()
    counts_by_type: dict[str, int] = {}
    for item in parent_items:
        if not isinstance(item, dict):
            continue
        ls = item.get("learning_scope")
        if isinstance(ls, dict):
            sr = str(ls.get("chapter_key", "")).strip()
            if sr:
                scope_refs.add(sr)
        elif isinstance(item.get("scope_ref"), str):
            sr = str(item["scope_ref"]).strip()
            if sr:
                scope_refs.add(sr)
        tt = str(item.get("task_type", "")).strip()
        if tt:
            counts_by_type[tt] = counts_by_type.get(tt, 0) + 1
    return {
        "scope_refs": frozenset(scope_refs),
        "counts_by_type": dict(counts_by_type),
        "total_items": len(parent_items),
    }


def build_orchestration_context(
    scope_candidates: list[dict[str, Any]],
    daily_minutes: int,
    total_days: int,
    *,
    focus_text: str | None = None,
    intent: str = "learn_chapter",
    parent_metrics: dict[str, Any] | None = None,
    revision_constraints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a validated context dict for the LLM orchestration stage.

    This is a **pure helper** — it does not call any API, LLM, or database.
    It takes server-validated truth (scope candidates resolved from KG,
    plan parameters, and real weakness evidence) and produces a dict that
    the caller can serialise and include in the LLM prompt.

    Every field in the returned context comes from the provided arguments
    — nothing is invented.

    Parameters
    ----------
    scope_candidates:
        List of scope dicts already resolved from the KG.  Each must carry
        at least a non-empty ``scope_ref`` (the chapter key).  Optional
        fields include ``book_title``, ``chapter_title``, ``kg_node_ids``,
        ``kg_node_names`` and ``has_weakness_evidence``.
    daily_minutes:
        Per-day time budget (5–240).
    total_days:
        Number of plan days (1–90).
    focus_text:
        Optional user-stated focus area.
    intent:
        The plan intent label (informational — not used as a constraint).
    parent_metrics:
        Optional dict from :func:`_compute_parent_metrics` — when a parent
        draft exists, its scope_refs, counts_by_type, and total_items are
        included so the LLM can satisfy revision constraints.
    revision_constraints:
        Optional dict of typed modification constraints from the unified
        LLM selector (e.g. ``scope_relation: "maintain"``,
        ``practice_count_relation: "decrease"``).  When present, it flows
        into the prompt and enables post-orchestration revision validation.

    Returns
    -------
    dict
        A validated context dict with keys ``scope_candidates``,
        ``daily_minutes``, ``total_days``, ``focus_text``, ``intent``,
        and optionally ``parent_metrics``, ``revision_constraints``.
    """
    # Validate daily_minutes
    if not isinstance(daily_minutes, int) or daily_minutes < 5 or daily_minutes > 240:
        raise ValueError(
            f"daily_minutes must be an integer between 5 and 240, "
            f"got {daily_minutes!r}"
        )

    # Validate total_days
    if not isinstance(total_days, int) or total_days < 1 or total_days > 90:
        raise ValueError(
            f"total_days must be an integer between 1 and 90, "
            f"got {total_days!r}"
        )

    # Normalise scope_candidates
    normalized_scopes: list[dict[str, Any]] = []
    seen_refs: set[str] = set()
    for i, sc in enumerate(scope_candidates):
        if not isinstance(sc, dict):
            raise ValueError(
                f"scope_candidates[{i}] must be a dict, "
                f"got {type(sc).__name__}"
            )
        scope_ref = str(sc.get("scope_ref", "")).strip()
        if not scope_ref:
            raise ValueError(
                f"scope_candidates[{i}] missing non-empty scope_ref"
            )
        if scope_ref in seen_refs:
            raise ValueError(
                f"Duplicate scope_ref in scope_candidates: {scope_ref!r}"
            )
        seen_refs.add(scope_ref)
        normalized_scopes.append(
            {
                "scope_ref": scope_ref,
                "book_title": str(sc.get("book_title", "")).strip() or None,
                "document_id": str(sc.get("document_id", "")).strip() or None,
                "chapter_title": str(sc.get("chapter_title", "")).strip() or None,
                "kg_node_ids": [
                    str(nid).strip()
                    for nid in (sc.get("kg_node_ids") or [])
                    if str(nid).strip()
                ],
                "kg_node_names": dict(sc.get("kg_node_names") or {}),
                "has_weakness_evidence": bool(
                    sc.get("has_weakness_evidence", False)
                ),
                "weakness_detail": [
                    {
                        "kg_node_id": str(wd.get("kg_node_id", "")).strip(),
                        "kg_node_name": str(wd.get("kg_node_name", "")).strip(),
                        "evidence_source": str(wd.get("evidence_source", "")).strip(),
                    }
                    for wd in (sc.get("weakness_detail") or [])
                    if isinstance(wd, dict) and str(wd.get("kg_node_id", "")).strip()
                ],
            }
        )

    result: dict[str, Any] = {
        "scope_candidates": normalized_scopes,
        "daily_minutes": daily_minutes,
        "total_days": total_days,
        "focus_text": str(focus_text).strip() if focus_text else None,
        "intent": intent,
    }

    # --- review_priority: summarise weakness evidence for review intents ---
    if intent == "review_chapter":
        total_weak_nodes = sum(
            len(sc.get("weakness_detail", [])) for sc in normalized_scopes
        )
        scopes_with_weakness = [
            sc["scope_ref"]
            for sc in normalized_scopes
            if sc.get("has_weakness_evidence")
        ]
        result["review_priority"] = {
            "total_weak_knowledge_points": total_weak_nodes,
            "scopes_with_weakness_evidence": scopes_with_weakness,
        }

    # --- whole-book invariants: derived from scope candidate count ----------
    # When multiple scope candidates exist (whole-book or multi-chapter
    # request), the validator and prompt must enforce full scope coverage
    # and mandatory task-type composition.  These invariants are server-side
    # truth — the LLM must satisfy them; the validator only checks, never
    # supplements.
    is_whole_book = len(normalized_scopes) > 1
    if is_whole_book:
        result["is_whole_book"] = True
        result["scope_count"] = len(normalized_scopes)
        # Mandatory task types for every whole-book plan
        result["required_task_types"] = [
            "learn_chapter",
            "review_chapter",
            "basic_practice",
        ]
    else:
        result["is_whole_book"] = False
        result["scope_count"] = len(normalized_scopes)

    # --- parent metrics (for revision constraint validation) ----------------
    if isinstance(parent_metrics, dict):
        result["parent_metrics"] = {
            "scope_refs": sorted(parent_metrics.get("scope_refs", set())),
            "counts_by_type": dict(parent_metrics.get("counts_by_type", {})),
            "total_items": int(parent_metrics.get("total_items", 0)),
        }

    # --- revision constraints (LLM-interpreted modification semantics) -----
    if isinstance(revision_constraints, dict) and revision_constraints:
        result["revision_constraints"] = dict(revision_constraints)

    return result


def validate_orchestration_candidate(
    candidate: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    """Validate an LLM orchestration output against the server-provided context.

    Returns a **normalised orchestration dict** on success.
    Raises :class:`OrchestrationValidationError` with a stable ``.code``
    on any validation failure.

    Rules enforced (in order):

    1. ``clarification_needed=true`` → items must be absent or empty.
    2. No clarification and empty items → rejected.
    3. Every item must carry a valid ``task_type`` (from the allowed set),
       ``scope_ref`` (present in context), non-negative ``day_offset``,
       positive-integer ``minutes``, non-empty ``reason``, and valid
       ``dependency`` (null or earlier item index).
    4. Single-day minutes total must not exceed ``context.daily_minutes``.
    5. Dependencies must reference an earlier, existing item — no
       self-dependency or forward dependency.
    6. ``advanced_practice`` is only accepted when the context scope
       candidate has ``has_weakness_evidence=true``.

    Parameters
    ----------
    candidate:
        The raw dict output from the LLM.  Expected shape:

        .. code-block:: json

           {
             "clarification_needed": false,
             "items": [
               {
                 "task_type": "learn_chapter",
                 "scope_ref": "chapter:perio-ch3",
                 "day_offset": 0,
                 "minutes": 30,
                 "reason": "...",
                 "dependency": null
               }
             ]
           }

    context:
        A context dict as returned by :func:`build_orchestration_context`.

    Returns
    -------
    dict
        A normalised dict with keys ``clarification_needed`` (bool) and
        ``items`` (list of normalised item dicts).  Every item dict has
        exactly six keys: ``task_type``, ``scope_ref``, ``day_offset``,
        ``minutes``, ``reason``, ``dependency``.

    Raises
    ------
    OrchestrationValidationError
        With a stable ``.code`` attribute (see the class docstring for
        the full list).
    """
    # --- 0. Validate candidate is a dict ----------------------------------
    if not isinstance(candidate, dict):
        raise OrchestrationValidationError(
            "invalid_candidate",
            f"candidate must be a dict, got {type(candidate).__name__}",
        )

    # --- 1. clarification_needed ------------------------------------------
    clarification = bool(candidate.get("clarification_needed", False))

    raw_items = candidate.get("items")
    if raw_items is None:
        raw_items = []
    if not isinstance(raw_items, list):
        raise OrchestrationValidationError(
            "invalid_items",
            "items must be a list",
        )

    if clarification and len(raw_items) > 0:
        raise OrchestrationValidationError(
            "clarification_with_items",
            "clarification_needed=true must not include items",
        )

    if not clarification and len(raw_items) == 0:
        raise OrchestrationValidationError(
            "empty_plan",
            "Non-empty items required when clarification_needed is not set",
        )

    # --- Extract scope_candidates early for clarification-vs-scope check ---
    scope_candidates_early = context.get("scope_candidates", [])
    if not isinstance(scope_candidates_early, list):
        scope_candidates_early = []

    # Clarification-only result — validate message and return early
    if clarification:
        # Reject clarification when scope_candidates is non-empty —
        # the server has already resolved the scope; the LLM must
        # generate a plan, not ask for clarification again.
        if len(scope_candidates_early) > 0:
            raise OrchestrationValidationError(
                "clarification_with_scope_candidates",
                "clarification_needed=true is not allowed when "
                "scope_candidates is non-empty — scope has already "
                "been resolved by the server",
            )
        clarification_message = candidate.get("clarification_message")
        if (
            clarification_message is None
            or not isinstance(clarification_message, str)
            or not clarification_message.strip()
        ):
            raise OrchestrationValidationError(
                "missing_clarification_message",
                "clarification_needed=true requires a non-empty "
                "clarification_message string",
            )
        return {
            "clarification_needed": True,
            "items": [],
            "clarification_message": clarification_message.strip(),
        }

    # --- 2. Validate context shape ----------------------------------------
    scope_candidates = context.get("scope_candidates", [])
    if not isinstance(scope_candidates, list):
        raise OrchestrationValidationError(
            "invalid_context",
            "context.scope_candidates must be a list",
        )

    # Build scope_ref → candidate lookup
    scope_by_ref: dict[str, dict[str, Any]] = {}
    for sc in scope_candidates:
        if isinstance(sc, dict):
            sr = str(sc.get("scope_ref", "")).strip()
            if sr:
                scope_by_ref[sr] = sc

    daily_minutes = int(context.get("daily_minutes", 30))
    total_days = int(context.get("total_days", 90))

    # --- 3. Validate each item --------------------------------------------
    normalized_items: list[dict[str, Any]] = []

    for i, raw_item in enumerate(raw_items):
        if not isinstance(raw_item, dict):
            raise OrchestrationValidationError(
                "invalid_item",
                f"items[{i}] must be a dict, got {type(raw_item).__name__}",
            )

        # --- 3a. task_type -----------------------------------------------
        task_type = str(raw_item.get("task_type", "")).strip()
        if task_type not in _ALLOWED_ORCHESTRATION_TASK_TYPES:
            raise OrchestrationValidationError(
                "invalid_task_type",
                f"items[{i}].task_type={task_type!r} not in allowed set: "
                f"{sorted(_ALLOWED_ORCHESTRATION_TASK_TYPES)}",
            )

        # --- 3b. scope_ref -----------------------------------------------
        scope_ref = str(raw_item.get("scope_ref", "")).strip()
        if not scope_ref:
            raise OrchestrationValidationError(
                "missing_scope_ref",
                f"items[{i}] missing non-empty scope_ref",
            )
        if scope_ref not in scope_by_ref:
            raise OrchestrationValidationError(
                "invalid_scope_ref",
                f"items[{i}].scope_ref={scope_ref!r} not in context "
                f"scope_candidates. "
                f"Available: {sorted(scope_by_ref.keys())}",
            )

        scope_data = scope_by_ref[scope_ref]

        # --- 3c. advanced_practice requires weakness evidence ------------
        if task_type == "advanced_practice":
            if not bool(scope_data.get("has_weakness_evidence", False)):
                raise OrchestrationValidationError(
                    "advanced_practice_without_weakness",
                    f"items[{i}] is advanced_practice but "
                    f"scope_ref={scope_ref!r} has no weakness evidence",
                )

        # --- 3d. day_offset ----------------------------------------------
        raw_offset = raw_item.get("day_offset")
        if raw_offset is None:
            raise OrchestrationValidationError(
                "missing_day_offset",
                f"items[{i}] missing required day_offset",
            )
        if isinstance(raw_offset, bool):
            raise OrchestrationValidationError(
                "invalid_day_offset",
                f"items[{i}].day_offset must be an integer, got bool",
            )
        try:
            day_offset = int(raw_offset)
        except (ValueError, TypeError):
            raise OrchestrationValidationError(
                "invalid_day_offset",
                f"items[{i}].day_offset must be an integer, "
                f"got {raw_offset!r}",
            )
        if day_offset < 0:
            raise OrchestrationValidationError(
                "invalid_day_offset",
                f"items[{i}].day_offset={day_offset} must be non-negative",
            )
        if day_offset >= total_days:
            raise OrchestrationValidationError(
                "day_offset_exceeds_total_days",
                f"items[{i}].day_offset={day_offset} must be less than "
                f"total_days={total_days}",
            )

        # --- 3e. minutes ------------------------------------------------
        raw_minutes = raw_item.get("minutes")
        if raw_minutes is None:
            raise OrchestrationValidationError(
                "missing_minutes",
                f"items[{i}] missing required minutes",
            )
        if isinstance(raw_minutes, bool):
            raise OrchestrationValidationError(
                "invalid_minutes",
                f"items[{i}].minutes must be an integer, got bool",
            )
        try:
            minutes = int(raw_minutes)
        except (ValueError, TypeError):
            raise OrchestrationValidationError(
                "invalid_minutes",
                f"items[{i}].minutes must be an integer, "
                f"got {raw_minutes!r}",
            )
        if minutes <= 0:
            raise OrchestrationValidationError(
                "invalid_minutes",
                f"items[{i}].minutes={minutes} must be a positive integer",
            )
        if minutes > daily_minutes:
            raise OrchestrationValidationError(
                "minutes_exceeds_daily_budget",
                f"items[{i}].minutes={minutes} exceeds "
                f"daily_minutes={daily_minutes}",
            )

        # --- 3f. reason -------------------------------------------------
        reason = raw_item.get("reason")
        if reason is None or (isinstance(reason, str) and not reason.strip()):
            raise OrchestrationValidationError(
                "missing_reason",
                f"items[{i}] missing non-empty reason",
            )
        if not isinstance(reason, str):
            raise OrchestrationValidationError(
                "invalid_reason",
                f"items[{i}].reason must be a string, "
                f"got {type(reason).__name__}",
            )
        reason = reason.strip()

        # --- 3g. dependency ---------------------------------------------
        dependency = raw_item.get("dependency")
        dep_val: int | None = None
        if dependency is not None:
            if isinstance(dependency, bool):
                raise OrchestrationValidationError(
                    "invalid_dependency",
                    f"items[{i}].dependency must be an integer index "
                    f"or null, got bool",
                )
            try:
                dep_val = int(dependency)
            except (ValueError, TypeError):
                raise OrchestrationValidationError(
                    "invalid_dependency",
                    f"items[{i}].dependency must be an integer index "
                    f"or null, got {dependency!r}",
                )
            if dep_val < 0:
                raise OrchestrationValidationError(
                    "invalid_dependency",
                    f"items[{i}].dependency={dep_val} must be non-negative",
                )
            if dep_val >= i:
                raise OrchestrationValidationError(
                    "invalid_dependency",
                    f"items[{i}].dependency={dep_val} must reference an "
                    f"earlier item (index < {i}); self-dependency and "
                    f"forward dependency are forbidden",
                )

        normalized_items.append(
            {
                "task_type": task_type,
                "scope_ref": scope_ref,
                "day_offset": day_offset,
                "minutes": minutes,
                "reason": reason,
                "dependency": dep_val,
            }
        )

    # --- 3b. Generic practice dependency semantic check -------------------
    # basic_practice / advanced_practice MUST depend on an earlier item
    # whose task_type is learn_chapter or review_chapter AND whose
    # scope_ref is the SAME scope.  practice→practice, null dependency,
    # and cross-scope practice chains are all illegal.
    for i, item in enumerate(normalized_items):
        task_type = item["task_type"]
        if task_type not in ("basic_practice", "advanced_practice"):
            continue

        dep_val = item["dependency"]
        # practice items MUST have a non-null dependency
        if dep_val is None:
            raise OrchestrationValidationError(
                "practice_dependency_invalid",
                f"items[{i}] ({task_type}) has no dependency — "
                f"{task_type} must depend on an earlier "
                f"learn_chapter or review_chapter with the same scope_ref. "
                f"scope_ref={item['scope_ref']!r}",
            )

        # dependency must be a valid integer index (already checked above,
        # but guard against theoretical edge case)
        if not isinstance(dep_val, int) or dep_val < 0 or dep_val >= len(normalized_items):
            raise OrchestrationValidationError(
                "practice_dependency_invalid",
                f"items[{i}] ({task_type}) dependency={dep_val!r} is not a "
                f"valid index into items[0..{len(normalized_items) - 1}]. "
                f"scope_ref={item['scope_ref']!r}",
            )

        dep_item = normalized_items[dep_val]
        dep_task_type = dep_item["task_type"]
        dep_scope_ref = dep_item["scope_ref"]

        # dependency task_type MUST be learn_chapter or review_chapter
        if dep_task_type not in ("learn_chapter", "review_chapter"):
            raise OrchestrationValidationError(
                "practice_dependency_invalid",
                f"items[{i}] ({task_type}) depends on items[{dep_val}] "
                f"({dep_task_type}) — {task_type} may only depend on "
                f"learn_chapter or review_chapter, not {dep_task_type}. "
                f"scope_ref={item['scope_ref']!r}",
            )

        # dependency scope_ref MUST match the practice item's scope_ref
        if dep_scope_ref != item["scope_ref"]:
            raise OrchestrationValidationError(
                "practice_dependency_invalid",
                f"items[{i}] ({task_type}) depends on items[{dep_val}] "
                f"({dep_task_type}) with scope_ref={dep_scope_ref!r} — "
                f"scope mismatch: practice scope_ref={item['scope_ref']!r} "
                f"≠ dependency scope_ref={dep_scope_ref!r}. "
                f"Practice must share the same scope as its prerequisite.",
            )

    # --- 4. Daily budget check --------------------------------------------
    daily_totals: dict[int, int] = {}
    for item in normalized_items:
        do = item["day_offset"]
        daily_totals[do] = daily_totals.get(do, 0) + item["minutes"]

    for day_offset, total in sorted(daily_totals.items()):
        if total > daily_minutes:
            raise OrchestrationValidationError(
                "daily_budget_exceeded",
                f"Day {day_offset} total minutes={total} exceeds "
                f"daily_minutes={daily_minutes}",
            )

    # --- 5. Whole-book scope coverage check --------------------------------
    # When is_whole_book=True (multiple scope candidates), every scope_ref
    # in the context MUST be referenced by at least one item.  The validator
    # only checks — it never supplements missing scopes or selects chapters.
    # Exception: when revision_constraints specify scope_relation=narrow,
    # partial coverage is expected (user wants fewer chapters).
    is_whole_book = bool(context.get("is_whole_book", False))
    _rc = context.get("revision_constraints")
    _scope_narrow = (
        isinstance(_rc, dict) and _rc.get("scope_relation") == "narrow"
    )
    if is_whole_book and len(scope_by_ref) > 1 and not _scope_narrow:
        referenced_refs: set[str] = {
            item["scope_ref"] for item in normalized_items
        }
        all_refs: set[str] = set(scope_by_ref.keys())
        missing_refs = all_refs - referenced_refs
        if missing_refs:
            raise OrchestrationValidationError(
                "scope_coverage_incomplete",
                f"Whole-book plan must reference ALL scope_candidates. "
                f"Missing scope_refs ({len(missing_refs)}): "
                f"{sorted(missing_refs)}. "
                f"Referenced ({len(referenced_refs)}): "
                f"{sorted(referenced_refs)}. "
                f"Total context scopes: {len(all_refs)}.",
            )

    # --- 6. Whole-book task-type composition check -------------------------
    # Every whole-book plan must include at least one of each required
    # task type: learn_chapter, review_chapter, basic_practice.
    # advanced_practice is only allowed with weakness evidence (checked
    # per-item above).  The validator only checks — it never invents
    # task types or re-labels items.  Also skipped when scope_relation=narrow
    # (fewer chapters → fewer tasks of each type is expected).
    if is_whole_book and not _scope_narrow:
        required_task_types: list[str] = context.get("required_task_types", [])
        if required_task_types:
            present_types: set[str] = {
                item["task_type"] for item in normalized_items
            }
            missing_types = set(required_task_types) - present_types
            if missing_types:
                raise OrchestrationValidationError(
                    "task_type_composition_incomplete",
                    f"Whole-book plan must include at least one of each "
                    f"required task type. "
                    f"Missing: {sorted(missing_types)}. "
                    f"Required: {sorted(required_task_types)}. "
                    f"Present: {sorted(present_types)}.",
                )

    return {"clarification_needed": False, "items": normalized_items}


# ---------------------------------------------------------------------------
# Internal draft-item builder — shared label / evidence helpers
# ---------------------------------------------------------------------------

_INTENT_LABEL: dict[str, str] = {
    "learn_chapter": "学习章节",
    "review_chapter": "复习章节",
    "review_weakness": "复习薄弱点",
    "custom": "自定义学习",
}


def create_custom_plan_draft(
    store: PlanDraftStore,
    *,
    user_id: str,
    request: dict[str, Any],
    chapters: list[dict[str, Any]] | None = None,
    parent_plan_draft_id: str | None = None,
) -> dict[str, Any]:
    """Create a structured plan draft from a normalised request and KG chapters.

    This is a **pure internal service**.  It does **not** call any API, LLM,
    Router, or read any database / learner profile.  Its only job is to:

    1. Normalise the structured *request* via :func:`normalize_plan_request`.
    2. Derive one or more draft items whose ``task_type``,
       ``learning_target``, ``estimated_minutes``, ``reason``,
       ``evidence_source`` and ``learning_scope`` are fully determined by
       the request parameters and the caller-provided *chapters*.
    3. Persist the draft payload to *store* and return the full draft
       record (a deep copy).

    Parameters
    ----------
    store:
        The :class:`PlanDraftStore` instance to persist into.
    user_id:
        Owner of the draft.
    request:
        A raw plan-request dict as consumed by :func:`normalize_plan_request`.
    chapters:
        Chapter dicts in the production KG shape (same format as
        :func:`resolve_learning_scope`).  May be ``None`` or empty for
        intents that do not require KG chapter evidence
        (``review_weakness``, ``custom``, and chapter-less ``review_chapter``).
    parent_plan_draft_id:
        Optional lineage — passed through to :meth:`PlanDraftStore.save`.
        The parent draft is never mutated.

    Returns
    -------
    dict
        The full draft record as returned by :meth:`PlanDraftStore.get`.

    Raises
    ------
    ValueError
        If the normalised request is invalid, a required chapter scope
        cannot be resolved, or *learn_chapter* is used without a
        ``chapter_key``.
    KeyError, PermissionError
        From :class:`PlanDraftStore` when *parent_plan_draft_id* is
        invalid, cross-user, or expired.
    """
    _chapters = list(chapters) if chapters is not None else []

    # 1. Normalise
    norm = normalize_plan_request(request)

    # 2. Derive items from the normalised request + chapters
    items = _build_draft_items(norm, _chapters)

    # 2b. Preflight: every item must have a usable scope before persistence.
    #     Uses the real default resource inspector (KG nodes + evidence chunks).
    _pf = _preflight_draft_items(items)
    if not _pf["ok"]:
        raise OrchestrationValidationError(
            "scope_preflight_failed",
            f"Custom plan draft preflight failed: "
            f"{_pf['failed']} item(s) have unusable scopes. "
            f"First failure: {_pf['failures'][0]['message']}",
        )

    # 3. Assemble payload
    payload: dict[str, Any] = {
        "request": norm,
        "items": items,
    }

    # 4. Persist
    draft_id = store.save(
        user_id=user_id,
        payload=payload,
        parent_plan_draft_id=parent_plan_draft_id,
    )

    # 5. Return full record (deep copy)
    return store.get(draft_id, user_id=user_id)


def _build_draft_items(
    norm: dict[str, Any],
    chapters: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Derive draft-line items from a normalised plan request and KG chapters.

    Task count is bounded by ``days`` (one item per planned day, capped at 5)
    and each item's ``estimated_minutes`` equals ``daily_minutes`` so that
    the total budget respects the plan period.
    """
    intent: str = norm["intent"]
    days: int = norm["days"]
    daily_minutes: int = norm["daily_minutes"]
    focus_text: str | None = norm["focus_text"]
    chapter_key: str | None = norm["chapter_key"]
    book_title: str | None = norm["book_title"]
    document_id: str | None = norm["document_id"]

    task_count = max(1, min(days, 5))

    # --- resolve scope and evidence source --------------------------------
    learning_scope: dict[str, Any] | None = None
    evidence_source: str

    if intent in ("learn_chapter", "review_chapter"):
        if chapter_key is not None:
            # Chapter-scoped → resolve via KG records
            learning_scope = resolve_learning_scope(
                chapters,
                chapter_key=chapter_key,
                book_title=book_title,
                document_id=document_id,
            )
            evidence_source = "kg_chapter"
        elif intent == "learn_chapter":
            raise ValueError(
                "learn_chapter intent requires a non-empty chapter_key"
            )
        else:
            # review_chapter without chapter_key → user evidence only
            evidence_source = "user_specified"
    else:
        # review_weakness / custom — never claim KG evidence
        evidence_source = "user_specified"

    # --- derive target label and reason -----------------------------------
    if learning_scope is not None:
        target_base = learning_scope["chapter_title"]
        bk = learning_scope["book_title"]
        if intent == "learn_chapter":
            reason = f"学习《{bk}》{target_base}"
        else:
            reason = f"复习《{bk}》{target_base}"
    elif focus_text is not None:
        target_base = focus_text
        reason = f"用户指定：{focus_text}"
    else:
        target_base = _INTENT_LABEL.get(intent, intent)
        reason = "用户自定义计划"

    # --- build items ------------------------------------------------------
    items: list[dict[str, Any]] = []
    for i in range(task_count):
        suffix = f"（{i + 1}/{task_count}）" if task_count > 1 else ""
        items.append(
            {
                "task_type": intent,
                "learning_target": f"{target_base}{suffix}",
                "estimated_minutes": daily_minutes,
                "reason": reason,
                "evidence_source": evidence_source,
                "learning_scope": learning_scope,
            }
        )

    return items


# ---------------------------------------------------------------------------
# Orchestration helpers: scope candidates, prompt, item mapping
# ---------------------------------------------------------------------------


def _build_scope_candidates_for_orchestration(
    parsed_request: dict[str, Any],
    chapters: list[dict[str, Any]],
    *,
    memory: LearningMemory | None = None,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    """Build scope-candidate dicts for the orchestration LLM from a parsed request.

    Each candidate carries a ``scope_ref`` (the chapter key) plus resolved
    fields from the KG chapter record.  The returned list is ready to pass
    directly to :func:`build_orchestration_context`.

    **Single-chapter path:** When *chapter_key* is non-null, resolves that
    specific chapter as a single scope candidate.

    **Whole-book path:** When *chapter_key* is null but *book_title* is
    non-null (the user asked to learn/review an entire book), expands to ALL
    chapters matching that book, sorted by KG chapter number (ascending).
    Each chapter becomes a scope candidate with real KG data and weakness
    evidence.  The orchestration LLM can then arrange tasks across chapters
    in any order — the list preserves real chapter order but does not
    constrain the LLM's scheduling.

    For intents without any chapter or book reference (``review_weakness``,
    ``custom``, or ``review_chapter`` without a ``chapter_key`` or
    ``book_title``), the list is empty — the orchestration LLM is expected
    to return ``clarification_needed``.

    When *memory* and *user_id* are both provided, ``has_weakness_evidence``
    is derived from real learner-memory data (weakness items and low-mastery
    scores) rather than hardcoded to ``False``.  When either is ``None``, the
    field defaults to ``False`` for backward compatibility.
    """
    intent: str = parsed_request["intent"]
    chapter_key: str | None = parsed_request.get("chapter_key")
    book_title: str | None = parsed_request.get("book_title")
    document_id: str | None = parsed_request.get("document_id")

    # --- Whole-book expansion: book_title without specific chapter_key ---
    if chapter_key is None and book_title is not None:
        if intent not in ("learn_chapter", "review_chapter"):
            return []
        return _expand_whole_book_candidates(
            chapters,
            book_title=book_title,
            document_id=document_id,
            memory=memory,
            user_id=user_id,
        )

    # --- review_weakness: build scope candidates from real weakness evidence ---
    if chapter_key is None and intent == "review_weakness":
        return _build_weakness_scope_candidates(
            chapters, memory=memory, user_id=user_id,
        )

    if chapter_key is None:
        return []

    if intent not in ("learn_chapter", "review_chapter"):
        return []

    # Resolve scope from KG — ValueError propagates on corrupt chapter data
    # (empty core_ids, missing title, etc.) so the caller gets a clear error.
    scope = resolve_learning_scope(
        chapters,
        chapter_key=chapter_key,
        book_title=book_title,
        document_id=document_id,
    )

    # --- resolve has_weakness_evidence from real learner memory ----------
    has_weakness_evidence = False
    weakness_detail: list[dict[str, Any]] = []
    if memory is not None and user_id is not None:
        has_weakness_evidence = _check_weakness_evidence(
            memory, user_id, scope["kg_node_ids"]
        )
        weakness_detail = _collect_weakness_detail(
            memory, user_id, scope["kg_node_ids"],
            scope.get("kg_node_names", {}),
        )

    return [
        {
            "scope_ref": scope["chapter_key"],
            "book_title": scope["book_title"],
            "document_id": scope["document_id"],
            "chapter_title": scope["chapter_title"],
            "kg_node_ids": scope["kg_node_ids"],
            "kg_node_names": scope.get("kg_node_names", {}),
            "has_weakness_evidence": has_weakness_evidence,
            "weakness_detail": weakness_detail,
        }
    ]


def _expand_whole_book_candidates(
    chapters: list[dict[str, Any]],
    *,
    book_title: str,
    document_id: str | None = None,
    memory: LearningMemory | None = None,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    """Expand a whole-book request into scope candidates ordered by KG chapter.

    Filters *chapters* by *book_title* (and optionally *document_id*),
    sorts by chapter ``number`` (ascending — real KG chapter order), resolves
    each chapter's scope, and returns a list of scope-candidate dicts ready
    for :func:`build_orchestration_context`.

    Chapters with corrupt or incomplete data (missing core_ids, empty titles,
    etc.) are silently skipped — only valid, resolved scopes are included.
    An empty result means no valid chapters matched the book filter; the
    orchestration LLM is expected to return ``clarification_needed``.

    Every field in the returned candidates comes from the KG chapter records
    — nothing is invented.
    """
    # 1. Filter chapters matching book_title (and optionally document_id)
    matching: list[dict[str, Any]] = []
    for ch in chapters:
        if not isinstance(ch, dict):
            continue
        ch_bt = str(ch.get("book_title", "")).strip()
        if ch_bt != book_title:
            continue
        if document_id:
            ch_did = str(ch.get("document_id", "")).strip()
            if ch_did != document_id:
                continue
        matching.append(ch)

    if not matching:
        return []

    # 2. Sort by chapter number (real KG chapter order, ascending)
    matching.sort(
        key=lambda ch: (
            int(ch.get("number", 0))
            if isinstance(ch.get("number"), (int, float))
            else 0
        )
    )

    # 3. Resolve each chapter as a scope candidate
    candidates: list[dict[str, Any]] = []
    for ch in matching:
        ch_key = str(ch.get("key", "")).strip()
        if not ch_key:
            continue
        try:
            scope = resolve_learning_scope(
                chapters,
                chapter_key=ch_key,
                book_title=book_title,
                document_id=document_id,
            )
        except ValueError:
            # Skip chapters with corrupt data (empty core_ids, etc.)
            continue

        has_weakness = False
        weakness_detail: list[dict[str, Any]] = []
        if memory is not None and user_id is not None:
            has_weakness = _check_weakness_evidence(
                memory, user_id, scope["kg_node_ids"]
            )
            weakness_detail = _collect_weakness_detail(
                memory, user_id, scope["kg_node_ids"],
                scope.get("kg_node_names", {}),
            )

        candidates.append(
            {
                "scope_ref": scope["chapter_key"],
                "book_title": scope["book_title"],
                "document_id": scope["document_id"],
                "chapter_title": scope["chapter_title"],
                "kg_node_ids": scope["kg_node_ids"],
                "kg_node_names": scope.get("kg_node_names", {}),
                "has_weakness_evidence": has_weakness,
                "weakness_detail": weakness_detail,
            }
        )

    return candidates


def _build_weakness_scope_candidates(
    chapters: list[dict[str, Any]],
    *,
    memory: LearningMemory | None = None,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    """Build scope candidates from real learner-memory weakness evidence.

    Uses :func:`backend.services.learning.profile_memory.get_weakness_overview`
    to read weakness and low-mastery KG node IDs from the learner memory,
    then maps those IDs to matching KG chapters.  Each matching chapter
    becomes a scope candidate with real weakness_detail populated from the
    overview.

    Returns an empty list when:
    * *memory* or *user_id* is ``None``
    * No weakness/low-mastery evidence with KG node IDs exists
    * No chapters match the weakness KG node IDs

    Every field in the returned candidates comes from the KG chapter records
    or the learner-memory evidence — nothing is invented.
    """
    if memory is None or user_id is None:
        return []

    from backend.services.learning.profile_memory import get_weakness_overview

    overview = get_weakness_overview(memory, user_id)
    if not overview["has_evidence"]:
        return []

    all_kg_ids = set(overview["all_kg_node_ids"])

    # Build kg_node_id → weakness detail lookup from overview
    weakness_by_kg_id: dict[str, dict[str, Any]] = {}
    for w in overview["weaknesses"]:
        for kid in w["kg_node_ids"]:
            if kid not in weakness_by_kg_id:
                weakness_by_kg_id[kid] = {
                    "kg_node_id": kid,
                    "kg_node_name": w["label"],
                    "evidence_source": w["evidence_source"],
                }
    for lm in overview["low_mastery"]:
        for kid in lm["kg_node_ids"]:
            if kid not in weakness_by_kg_id:
                weakness_by_kg_id[kid] = {
                    "kg_node_id": kid,
                    "kg_node_name": lm["label"],
                    "evidence_source": lm["evidence_source"],
                }

    # Build chapter index: kg_node_id → list of chapter dicts
    kg_to_chapters: dict[str, list[dict[str, Any]]] = {}
    for ch in chapters:
        if not isinstance(ch, dict):
            continue
        core_ids = [
            str(cid).strip()
            for cid in (ch.get("core_ids") or [])
            if str(cid).strip()
        ]
        for cid in core_ids:
            if cid in all_kg_ids:
                kg_to_chapters.setdefault(cid, []).append(ch)

    # Build scope candidates: one per unique chapter_key found
    candidates: list[dict[str, Any]] = []
    seen_chapter_keys: set[str] = set()

    for kg_id in sorted(all_kg_ids):
        matching_chapters = kg_to_chapters.get(kg_id, [])
        for ch in matching_chapters:
            ch_key = str(ch.get("key", "")).strip()
            if not ch_key or ch_key in seen_chapter_keys:
                continue
            seen_chapter_keys.add(ch_key)

            try:
                scope = resolve_learning_scope(
                    chapters,
                    chapter_key=ch_key,
                    book_title=str(ch.get("book_title", "")).strip() or None,
                    document_id=str(ch.get("document_id", "")).strip() or None,
                )
            except ValueError:
                continue

            # Collect weakness_detail for KG IDs in this chapter that have evidence
            chapter_kg_ids = set(scope["kg_node_ids"])
            wd: list[dict[str, Any]] = []
            for cid in sorted(chapter_kg_ids & all_kg_ids):
                entry = weakness_by_kg_id.get(cid)
                if entry:
                    name = scope.get("kg_node_names", {}).get(cid, entry["kg_node_name"])
                    wd.append({
                        "kg_node_id": cid,
                        "kg_node_name": name,
                        "evidence_source": entry["evidence_source"],
                    })

            candidates.append({
                "scope_ref": scope["chapter_key"],
                "book_title": scope["book_title"],
                "document_id": scope["document_id"],
                "chapter_title": scope["chapter_title"],
                "kg_node_ids": scope["kg_node_ids"],
                "kg_node_names": scope.get("kg_node_names", {}),
                "has_weakness_evidence": True,
                "weakness_detail": wd,
            })

    return candidates


def _build_scope_candidates_from_resolved_scopes(
    resolved_scopes: list[dict[str, Any]],
    chapters: list[dict[str, Any]],
    *,
    memory: LearningMemory | None = None,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    """Convert unified resolved scopes into orchestration scope candidates.

    This is the bridge between the unified scope selector's output
    (:func:`parse_scope_selector_from_text` → ``resolved_scopes``) and
    the orchestration layer (:func:`build_orchestration_context` →
    ``scope_candidates``).

    - For ``scope_type="book"``: expands to all chapters of that book
      via :func:`_expand_whole_book_candidates`.
    - For ``scope_type="chapter"``: uses the resolved scope directly,
      already carrying full KG data and weakness evidence.
    - For ``scope_type="knowledge_point"``, ``"disease"``,
      ``"weakness_profile"``: uses directly — these carry kg_node_ids
      but may not map to chapter-level scopes.

    Returns an empty list when *resolved_scopes* is empty.
    """
    if not resolved_scopes:
        return []

    candidates: list[dict[str, Any]] = []
    for scope in resolved_scopes:
        scope_type = scope.get("scope_type")

        if scope_type == "book":
            book_title = scope.get("book_title")
            if book_title:
                expanded = _expand_whole_book_candidates(
                    chapters,
                    book_title=book_title,
                    document_id=scope.get("document_id"),
                    memory=memory,
                    user_id=user_id,
                )
                candidates.extend(expanded)

        elif scope_type == "chapter":
            candidates.append({
                "scope_ref": scope["scope_ref"],
                "book_title": scope.get("book_title"),
                "document_id": scope.get("document_id"),
                "chapter_title": scope.get("chapter_title"),
                "kg_node_ids": list(scope.get("kg_node_ids", [])),
                "kg_node_names": dict(scope.get("kg_node_names", {})),
                "has_weakness_evidence": bool(
                    scope.get("has_weakness_evidence", False)
                ),
                "weakness_detail": list(scope.get("weakness_detail", [])),
            })

        elif scope_type in ("knowledge_point", "disease", "weakness_profile"):
            # These scope types carry kg_node_ids but may not directly map
            # to chapter-level scopes.  Include them as-is so the
            # orchestration LLM can plan around them.
            candidates.append({
                "scope_ref": scope["scope_ref"],
                "book_title": scope.get("book_title"),
                "document_id": scope.get("document_id"),
                "chapter_title": scope.get("chapter_title"),
                "kg_node_ids": list(scope.get("kg_node_ids", [])),
                "kg_node_names": dict(scope.get("kg_node_names", {})),
                "has_weakness_evidence": bool(
                    scope.get("has_weakness_evidence", False)
                ),
                "weakness_detail": list(scope.get("weakness_detail", [])),
            })

    return candidates


def _check_weakness_evidence(
    memory: LearningMemory,
    user_id: str,
    kg_node_ids: list[str],
) -> bool:
    """Return ``True`` when any of *kg_node_ids* has real weakness evidence.

    Evidence sources (checked in order, short-circuit on first match):

    1. **Learner-memory weakness items** — any weakness item whose
       ``kg_node_ids`` overlaps with the scope's node IDs.
    2. **Low-mastery scores** — any mastery record (from recent sessions)
       whose ``mastery_score`` is below 0.62 for a matching KG node.

    Only existing :class:`LearningMemory` read APIs are used — no new
    schema, tables, or endpoints are added.
    """
    if not kg_node_ids:
        return False

    kg_node_set = set(kg_node_ids)

    # 1. Weakness items from learner memory --------------------------------
    learner_memory = memory.get_learner_memory(user_id)
    weakness_items = _slot_items(learner_memory, "weaknesses")
    for item in weakness_items:
        item_ids = {
            str(kid).strip()
            for kid in (item.get("kg_node_ids") or [])
            if str(kid).strip()
        }
        if kg_node_set & item_ids:
            return True

    # 2. Low-mastery scores from recent sessions ---------------------------
    sessions = memory.list_sessions_for_user(user_id, limit=8)
    for session in sessions:
        sid = session.get("id")
        if not sid:
            continue
        try:
            mastery_map = memory.get_mastery_by_kg_node(user_id, sid)
        except Exception:
            continue
        for kg_id, row in mastery_map.items():
            if kg_id in kg_node_set:
                try:
                    score = float(row.get("mastery_score", 1.0))
                except (TypeError, ValueError):
                    continue
                if score < 0.62:
                    return True

    return False


def _collect_weakness_detail(
    memory: LearningMemory,
    user_id: str,
    kg_node_ids: list[str],
    kg_node_names: dict[str, str],
) -> list[dict[str, Any]]:
    """Return detailed weakness evidence for specific *kg_node_ids*.

    Unlike :func:`_check_weakness_evidence` which returns a boolean, this
    function returns a list of dicts describing exactly which nodes have
    weakness evidence and why — enabling the orchestration context to
    carry real review-priority information rather than a binary flag.

    Returns an empty list when no weakness evidence is found.
    """
    if not kg_node_ids:
        return []

    kg_node_set = set(kg_node_ids)
    detail: list[dict[str, Any]] = []

    # 1. Weakness items from learner memory
    learner_memory = memory.get_learner_memory(user_id)
    weakness_items = _slot_items(learner_memory, "weaknesses")
    for item in weakness_items:
        item_ids = {
            str(kid).strip()
            for kid in (item.get("kg_node_ids") or [])
            if str(kid).strip()
        }
        overlap = kg_node_set & item_ids
        for kid in sorted(overlap):
            name = kg_node_names.get(kid, kid)
            detail.append({
                "kg_node_id": kid,
                "kg_node_name": name,
                "evidence_source": "learner_memory_weakness",
            })

    # 2. Low-mastery scores from recent sessions
    sessions = memory.list_sessions_for_user(user_id, limit=8)
    for session in sessions:
        sid = session.get("id")
        if not sid:
            continue
        try:
            mastery_map = memory.get_mastery_by_kg_node(user_id, sid)
        except Exception:
            continue
        for kg_id, row in mastery_map.items():
            if kg_id in kg_node_set:
                try:
                    score = float(row.get("mastery_score", 1.0))
                except (TypeError, ValueError):
                    continue
                if score < 0.62:
                    name = kg_node_names.get(kg_id, kg_id)
                    detail.append({
                        "kg_node_id": kg_id,
                        "kg_node_name": name,
                        "evidence_source": "low_mastery",
                        "mastery_score": round(score, 3),
                    })

    # Deduplicate by kg_node_id (first source wins)
    seen: set[str] = set()
    unique_detail: list[dict[str, Any]] = []
    for d in detail:
        if d["kg_node_id"] not in seen:
            seen.add(d["kg_node_id"])
            unique_detail.append(d)

    return unique_detail


def _build_orchestration_prompt(context: dict[str, Any]) -> str:
    """Build the LLM system+user prompt for plan orchestration.

    The prompt includes the full context (scope candidates, budget,
    intent) serialised as JSON and instructs the LLM to return a single
    JSON object conforming to the orchestration contract.

    ``document_id`` is stripped from scope candidates before serialisation
    — it is server-side truth that must never flow through LLM output.
    """
    # Strip document_id from scope candidates before serialising for the LLM.
    # document_id is server-side truth carried in context for the mapping
    # function; the LLM must never see or output it.
    ctx_for_llm: dict[str, Any] = dict(context)
    raw_scopes = ctx_for_llm.get("scope_candidates")
    if isinstance(raw_scopes, list):
        ctx_for_llm["scope_candidates"] = [
            {k: v for k, v in sc.items() if k != "document_id"}
            if isinstance(sc, dict) else sc
            for sc in raw_scopes
        ]

    # --- Compute bounded item cap for the LLM -----------------------------
    # Whole-book: scope_count + mandatory foundation items (learn, review, practice = 3)
    # Single-chapter: total_days × 2, capped at 30
    # The LLM must respect this cap and fit within the output token budget.
    scope_count = int(context.get("scope_count", 0))
    is_whole_book = bool(context.get("is_whole_book", False))
    if is_whole_book and scope_count > 0:
        required_count = len(context.get("required_task_types", []))
        item_cap = scope_count + max(required_count, 3)
    else:
        total_days = int(context.get("total_days", 7))
        item_cap = min(total_days * 2, 30)
    # Hard floor: never less than 3 (one of each required type)
    item_cap = max(item_cap, 3)
    # Hard ceiling: 30 (prevent unbounded output)
    item_cap = min(item_cap, 30)
    ctx_for_llm["item_cap"] = item_cap
    ctx_json = json.dumps(ctx_for_llm, ensure_ascii=False, indent=2, separators=(",", ": "))

    is_review = context.get("intent") == "review_chapter"

    base_prompt = (
        "You are a study plan orchestrator. Given a validated learning "
        "context, output a SINGLE JSON object that defines a personalized "
        "study plan.\n"
        "\n"
        "Context (server-validated — never invent or modify these values):\n"
        f"{ctx_json}\n"
        "\n"
        "Output format:\n"
        "{\n"
        '  "clarification_needed": false,\n'
        '  "clarification_message": "<when clarification_needed=true, '
        'a helpful question in Chinese guiding the user to specify '
        'textbook/chapter/topic; otherwise null>",\n'
        '  "items": [\n'
        "    {\n"
        '      "task_type": "learn_chapter|review_chapter|'
        'basic_practice|advanced_practice",\n'
        '      "scope_ref": "<exact scope_ref from a candidate above>",\n'
        '      "day_offset": <0-based day index, integer>,\n'
        '      "minutes": <positive integer ≤ daily_minutes>,\n'
        '      "reason": "<personalized reason in Chinese>",\n'
        '      "dependency": <null or integer index of prerequisite item>\n'
        "    }\n"
        "  ]\n"
        "}\n"
        "\n"
        "CRITICAL RULES:\n"
        "1. scope_ref MUST be copied VERBATIM from the scope_candidates "
        "above. NEVER invent, guess, or modify these values.\n"
        "2. task_type MUST be one of: learn_chapter, review_chapter, "
        "basic_practice, advanced_practice.\n"
        "3. day_offset must be a non-negative integer less than total_days.\n"
        "4. Per-day total minutes must NOT exceed daily_minutes.\n"
        "5. advanced_practice is ONLY allowed when the scope candidate has "
        "has_weakness_evidence=true.\n"
        "6. dependency must be null (no prerequisite) or an integer index "
        "of an EARLIER item (index < current item's position).\n"
        "7. PRACTICE DEPENDENCY RULE (CRITICAL): basic_practice and "
        "advanced_practice items MUST have a non-null dependency that "
        "references an EARLIER item whose task_type is learn_chapter or "
        "review_chapter AND whose scope_ref is the SAME as this practice "
        "item. practice→practice, practice→null-dependency, and "
        "cross-scope practice dependencies are ALL ILLEGAL and will be "
        "rejected by the validator.\n"
        "8. If the context has no scope_candidates (empty list), you MUST "
        'set clarification_needed=true and omit items — you cannot '
        "invent scope_ref values.\n"
        "9. If the context HAS scope_candidates (non-empty list), you MUST "
        "generate items and set clarification_needed=false — the scope "
        "has already been resolved by the server. Do NOT ask for "
        "clarification when scope_candidates are provided.\n"
        "10. Output ONLY the JSON object — no markdown fences, "
        "no extra text.\n"
        "11. Personalize the plan: vary task order, proportions, pacing, "
        "and focus based on intent, focus_text, and total_days. "
        "Do NOT produce a fixed day-1-learn/day-2-review template.\n"
        "12. Write all reasons in Chinese, referencing the specific "
        "chapter/concept names from the context.\n"
        "13. If clarification_needed is true, items MUST be absent or an "
        "empty list []. Do NOT include both clarification and items.\n"
        "14. When clarification_needed is true, you MUST include a non-empty "
        "clarification_message field — a helpful, specific question in "
        "Chinese that asks the user to specify what textbook, chapter, or "
        "topic they want to learn or review. Example: "
        '"请告诉我您想学习的教材章节或主题，也可以直接说"薄弱点"，'
        '我会根据您的学习情况安排复习。'
        '目前支持《牙周病学》《口腔颌面外科学》等口腔医学教材。"\n'
        "15. When clarification_needed is false, set clarification_message "
        "to null.\n"
        "16. OUTPUT LENGTH CONTROL: The item count must respect the bounded cap "
        "from context (item_cap field). DO NOT exceed this cap. For whole-book "
        "plans with many chapters, spread chapters across days — do NOT "
        "create a separate item for every chapter on every day. Merge related "
        "chapters into single items when reasonable.\n"
        "17. Keep each reason concise — 1–2 short Chinese sentences, "
        "ideally under 30 characters. Reference chapter/concept names "
        "briefly rather than writing full pedagogical explanations.\n"
        "18. Prefer compact JSON formatting — avoid unnecessary whitespace "
        "while keeping the JSON valid and readable. Every byte saved in "
        "formatting helps prevent output truncation."
    )

    # ── Whole-book mandatory rules (when is_whole_book=true) ────────────
    if is_whole_book:
        required_types = context.get("required_task_types", [])
        required_str = ", ".join(required_types) if required_types else (
            "learn_chapter, review_chapter, basic_practice"
        )
        base_prompt += (
            "\n\nWHOLE-BOOK RULES (is_whole_book=true — these are MANDATORY):\n"
            f"19. SCOPE COVERAGE: You MUST reference EVERY scope_candidate "
            f"({scope_count} total) in your items. Every chapter must appear "
            f"in at least one item. Do NOT silently drop chapters — the plan "
            f"must cover the full book.\n"
            f"20. TASK TYPE COMPOSITION: You MUST include at least ONE of "
            f"EACH of these task types: {required_str}. This is not optional "
            f"— a whole-book plan without review or practice tasks is "
            f"INVALID.\n"
            f"21. ITEM CAP: You MUST generate at most {item_cap} items total. "
            f"To fit within this cap while covering {scope_count} scopes and "
            f"the required task types, merge related chapters into single "
            f"learn/review items where reasonable, and keep practice items "
            f"focused.\n"
            f"22. Do NOT output all items as learn_chapter — a valid "
            f"whole-book plan includes review and practice, not just "
            f"sequential learning.\n"
            f"23. advanced_practice is ONLY allowed for scopes with "
            f"has_weakness_evidence=true. Do not invent weakness evidence."
        )

    if is_review:
        review_rule_start = 24 if is_whole_book else 19
        base_prompt += (
            "\n\nREVIEW-SPECIFIC RULES (apply when intent is review_chapter):\n"
            f"{review_rule_start}. This is a REVIEW plan — the user has already studied this "
            "material. Prioritize review_chapter and basic_practice tasks "
            "over learn_chapter. Do NOT treat this as learning new content.\n"
            f"{review_rule_start + 1}. When a scope candidate has weakness_detail (specific weak "
            "knowledge points), reference those weak concepts by name in "
            "your reasons. Place review tasks for scopes with "
            "has_weakness_evidence=true earlier in the plan (lower day_offset).\n"
            f"{review_rule_start + 2}. Use advanced_practice ONLY for scopes with real weakness "
            "evidence (has_weakness_evidence=true).\n"
            f"{review_rule_start + 3}. The review plan must have independent review semantics — "
            "task types, pacing, and reasons must reflect consolidation "
            "and gap-filling, not sequential chapter traversal. A review "
            "plan is NOT the same as a learning plan with different labels.\n"
            f"{review_rule_start + 4}. When review_priority is present in the context, use "
            "total_weak_knowledge_points to proportionally allocate review "
            "tasks — more weak points → more review tasks for that scope."
        )

    # ── Revision / modification constraint rules ─────────────────────────
    revision_constraints = context.get("revision_constraints")
    if isinstance(revision_constraints, dict) and revision_constraints:
        parent_metrics = context.get("parent_metrics")
        rc_json = json.dumps(revision_constraints, ensure_ascii=False)
        pm_json = json.dumps(parent_metrics, ensure_ascii=False) if parent_metrics else "{}"
        mod_start = 25  # after review rules
        base_prompt += (
            "\n\nMODIFICATION RULES (revision_constraints — CRITICAL):\n"
            f"The user is MODIFYING a previous plan. You MUST satisfy these "
            f"constraints that were parsed from the user's request:\n"
            f"{rc_json}\n"
            f"\nParent plan metrics (for comparison):\n"
            f"{pm_json}\n"
            f"\n{mod_start}. SCOPE CONSTRAINT: If scope_relation is "
            f"\"maintain\", your items MUST use EXACTLY the same set of "
            f"scope_ref values that appear in the scope_candidates — do NOT "
            f"add or remove chapters. If \"narrow\", use a STRICT SUBSET. "
            f"If \"expand\", the current scope_candidates already include the "
            f"expanded set — use them all.\n"
            f"{mod_start + 1}. PRACTICE COUNT CONSTRAINT: If "
            f"practice_count_relation is \"decrease\", the total number of "
            f"basic_practice + advanced_practice items in your output MUST be "
            f"STRICTLY LESS than the parent's practice count. If "
            f"\"maintain\", use the SAME number. If \"increase\", use MORE.\n"
            f"{mod_start + 2}. LEARN COUNT CONSTRAINT: Same rule applied to "
            f"learn_chapter items vs parent's learn count.\n"
            f"{mod_start + 3}. REVIEW COUNT CONSTRAINT: Same rule applied to "
            f"review_chapter items vs parent's review count.\n"
            f"{mod_start + 4}. DAILY MINUTES CONSTRAINT: If "
            f"daily_minutes_max is set, EVERY single item's minutes AND "
            f"every day's total minutes MUST be ≤ daily_minutes_max. "
            f"Do NOT exceed this upper bound.\n"
            f"{mod_start + 5}. CONSTRAINTS ARE MANDATORY — the server "
            f"will compare your output against the parent metrics and "
            f"REJECT any plan that violates these constraints. "
            f"Unsatisfied constraints cause a retry or failure; "
            f"there is no template fallback."
        )

    return base_prompt


# ---------------------------------------------------------------------------
# Orchestration JSON error classification
# ---------------------------------------------------------------------------


def _classify_orchestration_json_error(exc: Exception, raw_text: str) -> str:
    """Classify a JSON parse error from LLM orchestration output.

    Distinguishes truncation (output cut off mid-stream, retryable) from
    malformed JSON (structural issues, non-retryable) and empty responses.

    Classification is based **only** on the parse error characteristics
    — no dependency on ``finish_reason`` (which may not be exposed by
    all providers).  This is the conservative approach required when the
    provider does not surface token-level truncation metadata.

    Parameters
    ----------
    exc:
        The exception raised during JSON parsing.  Expected to be a
        :class:`json.JSONDecodeError` or :class:`ValueError`.
    raw_text:
        The raw LLM output string that failed to parse.

    Returns
    -------
    str
        One of:

        * ``"truncated"`` — JSON appears cut off mid-stream (unterminated
          string, unexpected EOF at the very end).  **Retryable.**
        * ``"malformed"`` — JSON has structural issues not consistent with
          truncation (error position far from end of text, type mismatches).
          **Not retryable.**
        * ``"empty"`` — The raw text is empty or whitespace-only.
          **Not retryable via truncation path** (may have its own handling).
    """
    if not isinstance(exc, json.JSONDecodeError):
        # Non-JSON errors (ValueError from _extract_json wrapper) —
        # conservatively treat as malformed unless text is empty.
        text = raw_text.strip() if isinstance(raw_text, str) else ""
        if not text:
            return "empty"
        return "malformed"

    msg = str(exc)
    pos = getattr(exc, "pos", 0)
    text_len = len(raw_text) if isinstance(raw_text, str) else 0

    if text_len == 0:
        return "empty"

    # Unterminated string is the strongest truncation signal —
    # the JSON was cut off in the middle of a string value.
    if "Unterminated string" in msg:
        return "truncated"

    # Error position at or beyond the text length means the parser
    # hit EOF while expecting more content — classic truncation.
    if pos >= text_len:
        return "truncated"

    # Error within the last 3 characters — very likely truncation
    # (EOF reached before the expected token could appear).
    if pos >= text_len - 3:
        return "truncated"

    # "Expecting" error near the end (within last 20 chars) with a
    # non-zero position — the parser was cut off before a closing
    # delimiter, value, or bracket.  Require pos > 0 to avoid
    # classifying entirely non-JSON text (error at column 1) as
    # truncated (e.g. plain Chinese text like "这是你的学习计划...").
    if "Expecting" in msg and pos > 0 and pos >= text_len - 20:
        return "truncated"

    # Otherwise: error is deep inside the text or at position 0
    # (entirely non-JSON content) — this is a structural problem,
    # not a truncation.
    return "malformed"


# ---------------------------------------------------------------------------
# Truncation retry prompt builder
# ---------------------------------------------------------------------------


def _build_truncation_retry_prompt(context: dict[str, Any]) -> str:
    """Build a compact retry prompt after orchestration JSON was truncated.

    The prompt explicitly requests:
    * Compact, single-line JSON (no unnecessary whitespace)
    * A hard cap on total items
    * Concise reasons (≤25 Chinese characters)
    * Complete, parseable output — no markdown, no explanation

    All critical validation rules from the original prompt are preserved.
    ``document_id`` is stripped from scope candidates before serialisation.
    """
    # Strip document_id from scope candidates
    ctx_for_llm: dict[str, Any] = dict(context)
    raw_scopes = ctx_for_llm.get("scope_candidates")
    if isinstance(raw_scopes, list):
        ctx_for_llm["scope_candidates"] = [
            {k: v for k, v in sc.items() if k != "document_id"}
            if isinstance(sc, dict) else sc
            for sc in raw_scopes
        ]

    # Compact JSON — no indentation, minimal separators
    ctx_json = json.dumps(ctx_for_llm, ensure_ascii=False, separators=(",", ":"))

    total_days = int(context.get("total_days", 7))
    daily_minutes = int(context.get("daily_minutes", 30))
    scope_count = len(context.get("scope_candidates", []))
    is_whole_book = bool(context.get("is_whole_book", False))

    # Bounded item cap: scope_count + mandatory foundation items for
    # whole-book, or total_days × 2 for single-chapter.  Hard ceiling at 30.
    if is_whole_book and scope_count > 1:
        required_count = len(context.get("required_task_types", []))
        max_items = scope_count + max(required_count, 3)
    else:
        max_items = min(total_days * 2, 30)
    max_items = max(max_items, 3)   # floor: at least one of each required type
    max_items = min(max_items, 30)  # ceiling: prevent unbounded output

    is_review = context.get("intent") == "review_chapter"

    prompt = (
        "!!! RETRY — YOUR PREVIOUS JSON OUTPUT WAS TRUNCATED (INCOMPLETE) !!!\n"
        "Your previous response was cut off before the JSON completed. "
        "You MUST return a COMPLETE, parseable JSON object with no "
        "truncation.\n"
        "\n"
        "To fit within output limits, follow these rules STRICTLY:\n"
        f"1. Generate AT MOST {max_items} items total — spread learning "
        "across days, do NOT create one item per chapter per day.\n"
        "2. Keep each 'reason' under 25 Chinese characters — be extremely "
        "concise.  Example: '学习牙周组织正常结构' (10 chars).\n"
        "3. Output COMPACT single-line JSON with NO line breaks, NO extra "
        "spaces — use the minimum valid JSON representation.\n"
        "4. Every required field MUST be present: task_type, scope_ref, "
        "day_offset, minutes, reason, dependency.\n"
        "5. scope_ref MUST be copied VERBATIM from scope_candidates above.\n"
        "6. day_offset must be a non-negative integer less than total_days.\n"
        "7. Per-day total minutes must NOT exceed daily_minutes.\n"
        "8. dependency must be null or an integer index of an EARLIER item.\n"
        f"9. Scope candidates are NON-EMPTY ({scope_count} scopes) — "
        "you MUST generate items with clarification_needed=false.\n"
        "10. Output ONLY the compact JSON object — no markdown fences, "
        "no explanation, no extra text before or after the JSON.\n"
        f"\n"
        f"Context: {ctx_json}\n"
        f"\n"
        f"Compact output format (single line, no whitespace):\n"
        f'{{"clarification_needed":false,"clarification_message":null,'
        f'"items":[{{"task_type":"learn_chapter","scope_ref":"<exact_ref>",'
        f'"day_offset":0,"minutes":{min(30, daily_minutes)},"reason":"<≤25chars>",'
        f'"dependency":null}}]}}\n'
    )

    # ── Whole-book rules in retry context ───────────────────────────────
    if is_whole_book and scope_count > 1:
        required_types = context.get("required_task_types", [])
        required_str = ", ".join(required_types) if required_types else (
            "learn_chapter, review_chapter, basic_practice"
        )
        prompt += (
            "\nWHOLE-BOOK (MANDATORY — even in retry):\n"
            f"- Reference EVERY scope candidate ({scope_count} total). "
            f"Do NOT silently drop chapters.\n"
            f"- Include at least one of each: {required_str}.\n"
            f"- Do NOT output all items as learn_chapter.\n"
        )

    if is_review:
        prompt += (
            "\nREVIEW-SPECIFIC:\n"
            "- This is a REVIEW plan. Prioritize review_chapter and "
            "basic_practice over learn_chapter.\n"
            "- Place review tasks for scopes with has_weakness_evidence=true "
            "earlier in the plan.\n"
            "- Use advanced_practice ONLY for scopes with real weakness evidence.\n"
        )

    return prompt


# ---------------------------------------------------------------------------
# Revision constraint validator
# ---------------------------------------------------------------------------


def _validate_revision_constraints(
    candidate_items: list[dict[str, Any]],
    context: dict[str, Any],
) -> None:
    """Validate that an orchestration candidate satisfies revision constraints.

    Compares the candidate's measurable structure against the parent plan's
    metrics according to the typed revision constraints parsed from the user's
    natural-language modification request.

    All constraints are LLM-interpreted, server-validated — NO Chinese
    keyword search or hardcoded chapter names.

    Raises :class:`OrchestrationValidationError` with stable ``.code`` on
    any violation.  Returns ``None`` when all non-unspecified constraints
    are satisfied.

    Parameters
    ----------
    candidate_items:
        Normalised orchestration items (post :func:`validate_orchestration_candidate`).
        Each item must have ``task_type`` and ``scope_ref`` keys.
    context:
        Orchestration context dict containing ``revision_constraints`` and
        ``parent_metrics``.
    """
    rc = context.get("revision_constraints")
    if not isinstance(rc, dict) or not rc:
        return  # No constraints → nothing to validate

    parent = context.get("parent_metrics")
    if not isinstance(parent, dict):
        return  # No parent metrics → cannot validate, pass through

    # ── Compute candidate metrics ───────────────────────────────────────
    cand_scope_refs: set[str] = set()
    cand_counts: dict[str, int] = {}
    for item in candidate_items:
        sr = str(item.get("scope_ref", "")).strip()
        if sr:
            cand_scope_refs.add(sr)
        tt = str(item.get("task_type", "")).strip()
        if tt:
            cand_counts[tt] = cand_counts.get(tt, 0) + 1

    parent_scope_list: list[str] = list(parent.get("scope_refs", []))
    parent_scope_set: set[str] = set(parent_scope_list)
    parent_counts: dict[str, int] = dict(parent.get("counts_by_type", {}))
    parent_total = int(parent.get("total_items", 0))

    # ── Helper: practice count = basic_practice + advanced_practice ──────
    def _practice_count(counts: dict[str, int]) -> int:
        return counts.get("basic_practice", 0) + counts.get("advanced_practice", 0)

    cand_practice = _practice_count(cand_counts)
    parent_practice = _practice_count(parent_counts)
    cand_learn = cand_counts.get("learn_chapter", 0)
    parent_learn = parent_counts.get("learn_chapter", 0)
    cand_review = cand_counts.get("review_chapter", 0)
    parent_review = parent_counts.get("review_chapter", 0)

    # ── Validate each non-unspecified constraint ─────────────────────────
    scope_rel = rc.get("scope_relation")
    if scope_rel == "maintain":
        if cand_scope_refs != parent_scope_set:
            added = sorted(cand_scope_refs - parent_scope_set)
            removed = sorted(parent_scope_set - cand_scope_refs)
            detail_parts: list[str] = []
            if added:
                detail_parts.append(f"added: {added}")
            if removed:
                detail_parts.append(f"removed: {removed}")
            raise OrchestrationValidationError(
                "revision_scope_not_maintained",
                f"scope_relation=maintain but candidate scope_refs differ "
                f"from parent: {', '.join(detail_parts)}. "
                f"Parent scopes: {sorted(parent_scope_set)}. "
                f"Candidate scopes: {sorted(cand_scope_refs)}.",
            )
    elif scope_rel == "narrow":
        if not (cand_scope_refs < parent_scope_set):
            raise OrchestrationValidationError(
                "revision_scope_not_narrowed",
                f"scope_relation=narrow but candidate scope_refs are not "
                f"a strict subset of parent. "
                f"Parent: {sorted(parent_scope_set)}. "
                f"Candidate: {sorted(cand_scope_refs)}.",
            )
    elif scope_rel == "expand":
        if not (cand_scope_refs > parent_scope_set):
            raise OrchestrationValidationError(
                "revision_scope_not_expanded",
                f"scope_relation=expand but candidate scope_refs are not "
                f"a strict superset of parent. "
                f"Parent: {sorted(parent_scope_set)}. "
                f"Candidate: {sorted(cand_scope_refs)}.",
            )

    def _check_count_relation(
        label: str, cand_val: int, parent_val: int, relation: str | None,
    ) -> None:
        if relation is None:
            return
        if relation == "decrease" and cand_val >= parent_val:
            raise OrchestrationValidationError(
                "revision_count_not_decreased",
                f"{label}_count_relation=decrease but candidate "
                f"{label} count ({cand_val}) is NOT strictly less than "
                f"parent ({parent_val}).",
            )
        if relation == "maintain" and cand_val != parent_val:
            raise OrchestrationValidationError(
                "revision_count_not_maintained",
                f"{label}_count_relation=maintain but candidate "
                f"{label} count ({cand_val}) differs from "
                f"parent ({parent_val}).",
            )
        if relation == "increase" and cand_val <= parent_val:
            raise OrchestrationValidationError(
                "revision_count_not_increased",
                f"{label}_count_relation=increase but candidate "
                f"{label} count ({cand_val}) is NOT strictly greater "
                f"than parent ({parent_val}).",
            )

    _check_count_relation("practice", cand_practice, parent_practice,
                          rc.get("practice_count_relation"))
    _check_count_relation("learn", cand_learn, parent_learn,
                          rc.get("learn_count_relation"))
    _check_count_relation("review", cand_review, parent_review,
                          rc.get("review_count_relation"))

    # ── daily_minutes_max ────────────────────────────────────────────────
    dmm = rc.get("daily_minutes_max")
    if isinstance(dmm, (int, float)) and dmm > 0:
        dmm_i = int(dmm)
        for i, item in enumerate(candidate_items):
            item_mins = item.get("minutes", 0)
            if isinstance(item_mins, (int, float)) and item_mins > dmm_i:
                raise OrchestrationValidationError(
                    "revision_daily_minutes_exceeded",
                    f"daily_minutes_max={dmm_i} but items[{i}] has "
                    f"minutes={int(item_mins)}.",
                )
        # Per-day total check
        day_totals: dict[int, int] = {}
        for item in candidate_items:
            do = item.get("day_offset", 0)
            if isinstance(do, (int, float)):
                day_totals[int(do)] = (
                    day_totals.get(int(do), 0)
                    + int(item.get("minutes", 0))
                )
        for day_idx, total in day_totals.items():
            if total > dmm_i:
                raise OrchestrationValidationError(
                    "revision_daily_budget_exceeded",
                    f"daily_minutes_max={dmm_i} but day {day_idx} "
                    f"total is {total} minutes.",
                )


def _build_content_retry_prompt(
    context: dict[str, Any],
    *,
    missing_scope_refs: list[str] | None = None,
    missing_task_types: list[str] | None = None,
) -> str:
    """Build a content-specific retry prompt when the LLM output fails
    scope-coverage or task-type-composition validation.

    The prompt carries the **specific missing contract** — exactly which
    scope_refs or task types are absent — so the LLM can fix the gap
    without guessing.  It does NOT repeat generic instructions; it only
    adds the missing-contract information on top of the base orchestration
    prompt.

    Parameters
    ----------
    context:
        The orchestration context (same as passed to the base prompt).
    missing_scope_refs:
        List of scope_ref strings that were NOT referenced in the LLM output.
    missing_task_types:
        List of task_type strings that are required but missing.
    """
    scope_count = len(context.get("scope_candidates", []))
    is_whole_book = bool(context.get("is_whole_book", False))

    # Build the base prompt first (contains all rules + context)
    base = _build_orchestration_prompt(context)

    # ── Prepend retry header with specific missing contract ────────────
    header = (
        "!!! RETRY — YOUR PREVIOUS RESPONSE FAILED VALIDATION !!!\n"
        "Your plan was rejected because it did not satisfy the "
        "whole-book contract. Fix ONLY the issues listed below while "
        "keeping everything else valid.\n"
        "\n"
        "SPECIFIC MISSING CONTRACT:\n"
    )

    if missing_scope_refs:
        header += (
            f"- MISSING SCOPE COVERAGE: The following {len(missing_scope_refs)} "
            f"chapter(s) were NOT referenced in your items but MUST be: "
            f"{missing_scope_refs}\n"
            f"  Add items referencing these scope_refs. "
            f"Total scopes: {scope_count}.\n"
        )

    if missing_task_types:
        header += (
            f"- MISSING TASK TYPES: Your plan is missing these required "
            f"task types: {missing_task_types}\n"
            f"  Add at least one item for each missing type. "
            f"A valid whole-book plan MUST include learn_chapter, "
            f"review_chapter, AND basic_practice.\n"
        )

    header += (
        "\nRULES FOR THIS RETRY:\n"
        "1. Reference ALL scope_candidates — every chapter must appear.\n"
        "2. Include at least one of EACH: learn_chapter, review_chapter, "
        "basic_practice.\n"
        "3. Do NOT change items that were already correct — only add/fix "
        "what's missing.\n"
        "4. Respect all original constraints (budget, day_offset, dependency).\n"
        "5. Output ONLY the JSON object — no markdown, no explanation.\n"
        "\n"
    )

    # Combine: retry header first, then the base prompt (which has context)
    return header + "\n" + base


def _build_revision_constraint_retry_prompt(
    context: dict[str, Any],
    *,
    validation_error: str,
) -> str:
    """Build a retry prompt for revision constraint violations.

    The prompt includes:
    - A RETRY header signalling this is a rejected attempt
    - The specific validation error from the validator
    - The original orchestration context (rebuilt -> prompt)

    This gives the LLM concrete feedback so it can correct its output.
    The validator NEVER rewrites the candidate — it only describes the gap.
    """
    base = _build_orchestration_prompt(context)
    return (
        "!!! RETRY — YOUR PREVIOUS RESPONSE WAS REJECTED !!!\n"
        "Your plan did NOT satisfy the revision (modification) constraints "
        "that were parsed from the user's request.\n"
        "\n"
        f"Validation error: {validation_error}\n"
        "\n"
        "You MUST generate a new plan that:\n"
        "- Respects ALL revision_constraints exactly as specified\n"
        "- Compares correctly against the parent metrics\n"
        "- Does NOT ignore any non-unspecified constraint\n"
        "\n"
        "Generate a COMPLETE, VALID JSON plan below:\n\n"
        + base
    )


def _build_practice_dependency_retry_prompt(
    context: dict[str, Any],
    *,
    validation_error: str,
) -> str:
    """Build a retry prompt when practice dependency validation fails.

    The prompt carries the **specific validation error message** so the LLM
    can fix exactly which dependency was wrong (practice→practice,
    null dependency, or cross-scope) without guessing.  It adds this
    feedback on top of the base orchestration prompt.

    Parameters
    ----------
    context:
        The orchestration context (same as passed to the base prompt).
    validation_error:
        The exact error message from the validator, describing which
        item index, dependency index, and what the mismatch was.
    """
    # Build the base prompt first (contains all rules + context)
    base = _build_orchestration_prompt(context)

    header = (
        "!!! RETRY — YOUR PREVIOUS RESPONSE FAILED DEPENDENCY VALIDATION !!!\n"
        "Your plan was rejected because one or more practice items had "
        "illegal dependencies. Fix ONLY the dependency issues described "
        "below while keeping everything else valid.\n"
        "\n"
        "SPECIFIC DEPENDENCY ERROR:\n"
        f"{validation_error}\n"
        "\n"
        "LEGAL DEPENDENCY RULES FOR PRACTICE ITEMS:\n"
        "1. basic_practice and advanced_practice MUST have a non-null "
        "dependency — they cannot be orphaned.\n"
        "2. The dependency MUST point to an EARLIER item whose task_type "
        "is learn_chapter or review_chapter.  practice→practice and "
        "practice→anything-else are ILLEGAL.\n"
        "3. The dependency item MUST share the SAME scope_ref as the "
        "practice item.  Cross-scope practice dependencies (e.g. "
        "practicing chapter 3 but depending on chapter 2's learn) are "
        "ILLEGAL.\n"
        "4. Fix ONLY the dependency field of the rejected practice item(s). "
        "Ensure each basic_practice/advanced_practice depends on (points "
        "to) the index of a preceding learn_chapter or review_chapter "
        "with the identical scope_ref.\n"
        "\n"
        "RULES FOR THIS RETRY:\n"
        "1. Do NOT change items that were already correct — only fix "
        "the rejected dependency values.\n"
        "2. Do NOT add new items or remove items unless necessary to "
        "fix the dependency chain.\n"
        "3. Respect ALL original constraints (budget, day_offset, "
        "scope_ref VEBATIM, task_type).\n"
        "4. Output ONLY the JSON object — no markdown, no explanation.\n"
        "\n"
    )

    return header + "\n" + base


def _build_schedule_constraint_retry_prompt(
    context: dict[str, Any],
    *,
    validation_error: str,
) -> str:
    """Build a single corrective retry for day and minute violations."""
    base = _build_orchestration_prompt(context)
    total_days = int(context.get("total_days", 1))
    daily_minutes = int(context.get("daily_minutes", 30))
    return (
        "!!! RETRY — YOUR PREVIOUS RESPONSE FAILED SCHEDULE VALIDATION !!!\n"
        f"Specific validation error: {validation_error}\n\n"
        "Generate the complete plan again and obey BOTH hard bounds:\n"
        f"- Every day_offset must be an integer from 0 through {total_days - 1}.\n"
        f"- The sum of minutes on each day must be at most {daily_minutes}.\n"
        "Each item must still have positive minutes. Preserve valid scope_refs, "
        "task-type coverage, and dependency rules. Output JSON only.\n\n"
        + base
    )


def _compute_missing_scope_refs(
    candidate: dict[str, Any],
    context: dict[str, Any],
) -> list[str]:
    """Return scope_refs from context that are NOT referenced in *candidate* items.

    Used by the retry loop to build a specific missing-contract prompt.
    """
    raw_items = candidate.get("items", [])
    if not isinstance(raw_items, list):
        raw_items = []
    candidate_refs: set[str] = {
        str(item.get("scope_ref", "")).strip()
        for item in raw_items
        if isinstance(item, dict) and str(item.get("scope_ref", "")).strip()
    }
    scope_candidates = context.get("scope_candidates", [])
    if not isinstance(scope_candidates, list):
        scope_candidates = []
    all_refs: set[str] = {
        str(sc.get("scope_ref", "")).strip()
        for sc in scope_candidates
        if isinstance(sc, dict) and str(sc.get("scope_ref", "")).strip()
    }
    return sorted(all_refs - candidate_refs)


def _compute_missing_task_types(
    candidate: dict[str, Any],
    context: dict[str, Any],
) -> list[str]:
    """Return required task types from context that are NOT present in *candidate* items.

    Used by the retry loop to build a specific missing-contract prompt.
    """
    raw_items = candidate.get("items", [])
    if not isinstance(raw_items, list):
        raw_items = []
    candidate_types: set[str] = {
        str(item.get("task_type", "")).strip()
        for item in raw_items
        if isinstance(item, dict) and str(item.get("task_type", "")).strip()
    }
    required_types = context.get("required_task_types", [])
    if not isinstance(required_types, list):
        required_types = []
    required_set: set[str] = {
        str(t).strip() for t in required_types if str(t).strip()
    }
    return sorted(required_set - candidate_types)


def _map_orchestration_items_to_draft_items(
    validated_items: list[dict[str, Any]],
    context: dict[str, Any],
    parsed_request: dict[str, Any],
) -> list[dict[str, Any]]:
    """Map validated orchestration items to the draft-item shape expected
    by downstream consumers (:func:`_map_draft_items_to_recommendations`).

    Every field in the returned items comes from either the validated
    orchestration output or the server-provided *context* — nothing is
    invented.
    """
    # Build scope_ref → resolved scope lookup from context
    scope_by_ref: dict[str, dict[str, Any]] = {}
    for sc in context.get("scope_candidates", []):
        if isinstance(sc, dict):
            sr = str(sc.get("scope_ref", "")).strip()
            if sr:
                scope_by_ref[sr] = sc

    intent: str = parsed_request["intent"]
    evidence_source: str
    if intent in ("learn_chapter", "review_chapter") and (
        parsed_request.get("chapter_key") or parsed_request.get("book_title")
    ):
        evidence_source = "kg_chapter"
    else:
        evidence_source = "user_specified"

    draft_items: list[dict[str, Any]] = []
    for item in validated_items:
        task_type: str = item["task_type"]
        scope_ref: str = item["scope_ref"]
        minutes: int = item["minutes"]
        reason: str = item["reason"]

        # Resolve learning_scope from context
        scope_data = scope_by_ref.get(scope_ref, {})
        learning_scope: dict[str, Any] | None = None
        if scope_data:
            # ── chapter_key: only set when scope_ref IS a real chapter key ──
            #     Chapter scopes use "chapter:<hex>" scope_refs — these are
            #     real, resolvable chapter identifiers.
            #     Knowledge-point / weakness / disease scopes use "core:<hex>"
            #     or "kg://..." scope_refs — these are KG node IDs, NOT
            #     chapter keys.  Impersonating a KG node ID as a chapter_key
            #     causes downstream preflight / plan_lesson failures because
            #     the ID space mismatch prevents resource lookups.
            _scope_is_chapter = str(scope_ref).startswith("chapter:")
            _explicit_chapter_key = scope_data.get("chapter_key")
            _chapter_key: str | None = None
            if _explicit_chapter_key:
                _chapter_key = str(_explicit_chapter_key).strip() or None
            elif _scope_is_chapter:
                _chapter_key = scope_ref
            # else: knowledge point / weakness / disease — no chapter_key;
            #        kg_node_ids carry the real resource identity.

            # ── Build learning_scope — only include non-None values ──
            #     preflight_scope_resources does str(value) on dict fields,
            #     turning Python None into the string "None" which then
            #     passes truthiness checks and becomes a fake
            #     document_id / chapter_key filter.  We only store real,
            #     non-None values so that preflight sees clean constraints.
            learning_scope = {
                "kg_node_ids": list(scope_data.get("kg_node_ids", [])),
                "kg_node_names": dict(scope_data.get("kg_node_names", {})),
            }
            _bt = scope_data.get("book_title")
            if _bt is not None and str(_bt).strip():
                learning_scope["book_title"] = _bt
            _did = scope_data.get("document_id")
            if _did is not None and str(_did).strip():
                learning_scope["document_id"] = _did
            if _chapter_key is not None:
                learning_scope["chapter_key"] = _chapter_key
            _ct = scope_data.get("chapter_title")
            if _ct is not None and str(_ct).strip():
                learning_scope["chapter_title"] = _ct

        # Derive learning_target from scope or task_type
        chapter_title = scope_data.get("chapter_title") if scope_data else None
        if chapter_title:
            learning_target = chapter_title
        elif parsed_request.get("focus_text"):
            learning_target = parsed_request["focus_text"]
        else:
            learning_target = task_type

        draft_item: dict[str, Any] = {
            "task_type": task_type,
            "learning_target": learning_target,
            "estimated_minutes": minutes,
            "reason": reason,
            "evidence_source": evidence_source,
            "learning_scope": learning_scope,
            "dependency": item.get("dependency"),
            "day_offset": item.get("day_offset"),
        }
        _pl = item.get("practice_level")
        if _pl is not None:
            draft_item["practice_level"] = _pl
        _sid = item.get("session_id")
        if _sid is not None:
            draft_item["session_id"] = _sid
        draft_items.append(draft_item)

    return draft_items


# ---------------------------------------------------------------------------
# Deterministic first-time chapter plan generator
# ---------------------------------------------------------------------------

_SOURCE_DETERMINISTIC_CHAPTER_PLAN = "chapter_sequence_v1"

# Day-1 weights: learn_chapter=3, review_chapter=2
_DAY1_WEIGHTS = [3, 2]
# Middle-day (Day 2..N) weights: basic_practice=2, learn_chapter=3, review_chapter=1
_MIDDLE_DAY_WEIGHTS = [2, 3, 1]
# Last-day (Day N+1) weight: basic_practice=1
_LAST_DAY_WEIGHTS = [1]

# ---------------------------------------------------------------------------
# Deterministic first-time weakness plan generator
# ---------------------------------------------------------------------------

_SOURCE_DETERMINISTIC_WEAKNESS_PLAN = "weakness_first_time_v1"

# Weakness day weights: review_chapter=2, basic_practice=1
_WEAKNESS_DAY_WEIGHTS = [2, 1]


# ---------------------------------------------------------------------------
# Plan draft scope preflight (shared by all persistence paths)
# ---------------------------------------------------------------------------

# Stable error codes returned by :func:`_preflight_draft_items`.
_PF_ITEM_SCOPE_UNUSABLE = "scope_unusable"
_PF_ITEM_MISSING_SCOPE = "missing_learning_scope"
_PF_ITEM_ORPHAN_PRACTICE = "orphan_basic_practice"
_PF_ITEM_PRACTICE_DEPENDENCY_MISSING = "practice_dependency_missing"
_PF_ITEM_PRACTICE_SCOPE_MISMATCH = "practice_scope_mismatch"
_PF_ITEM_UNKNOWN_TASK_TYPE = "unknown_task_type"


def _scopes_match(
    scope_a: dict[str, Any] | None,
    scope_b: dict[str, Any] | None,
) -> bool:
    """Return True when two learning_scopes share the same normalized identity.

    Two scopes match when they share at least one concrete resource
    identifier (KG node IDs intersect, or same document_id + chapter_key).
    """
    if not isinstance(scope_a, dict) or not isinstance(scope_b, dict):
        return False
    if not scope_a or not scope_b:
        return False

    # --- KG node ID intersection ---
    a_ids = set(
        str(sid).strip()
        for sid in (scope_a.get("kg_node_ids") or [])
        if str(sid).strip()
    )
    b_ids = set(
        str(sid).strip()
        for sid in (scope_b.get("kg_node_ids") or [])
        if str(sid).strip()
    )
    if a_ids and b_ids and a_ids & b_ids:
        return True

    # --- document_id + chapter_key ---
    a_doc = str(scope_a.get("document_id", "")).strip()
    b_doc = str(scope_b.get("document_id", "")).strip()
    a_ch = str(
        scope_a.get("chapter_key", "")
        or scope_a.get("chapter_title", "")
    ).strip()
    b_ch = str(
        scope_b.get("chapter_key", "")
        or scope_b.get("chapter_title", "")
    ).strip()
    if a_doc and b_doc and a_ch and b_ch:
        if a_doc == b_doc:
            if a_ch == b_ch or a_ch in b_ch or b_ch in a_ch:
                return True

    return False


def _preflight_draft_items(
    draft_items: list[dict[str, Any]],
    *,
    resource_inspector: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Preflight all *draft_items* before persistence — no side effects.

    For every item, validates that its ``learning_scope`` has usable
    resources (via *resource_inspector*) and that ``basic_practice`` items
    are properly chained to a learn/review predecessor with the same scope.

    This is the **last hard gate** before ``store.save()`` — a plan draft
    with any failing item is NEVER persisted.

    Parameters
    ----------
    draft_items:
        The items to validate.
    resource_inspector:
        Optional callable ``(learning_scope: dict) -> preflight_result``.
        When ``None`` (production default), calls the real
        :func:`~.planner.preflight_scope_resources`, accepting a conclusive
        KG-node hit first and falling back to evidence retrieval otherwise.
        Identical scopes share one result within this preflight call. Tests
        may inject a fake inspector that returns a controlled preflight dict.
        Production code must never pass a fake/disabled inspector.

    Returns
    -------
    dict
        ``{"ok": bool, "failures": [...], "passed": int, "failed": int}``.
        Each failure entry has ``item_index``, ``task_type``, ``title``,
        ``scope_title``, ``error_code``, and ``message``.
    """
    if not draft_items:
        return {"ok": True, "failures": [], "passed": 0, "failed": 0}

    # ── Resolve default inspector (production: real KG + evidence checks) ─
    if resource_inspector is None:
        preflight_cache: dict[str, dict[str, Any]] = {}

        def _default_inspector(scope: dict[str, Any]) -> dict[str, Any]:
            try:
                cache_key = json.dumps(
                    scope, ensure_ascii=False, sort_keys=True, default=str,
                )
            except (TypeError, ValueError):
                cache_key = repr(scope)
            cached = preflight_cache.get(cache_key)
            if cached is not None:
                return cached

            kg_ids = [
                str(value).strip()
                for value in (scope.get("kg_node_ids") or [])
                if str(value).strip()
            ]
            has_evidence_locator = bool(
                str(scope.get("document_id") or "").strip()
                or str(
                    scope.get("chapter_key")
                    or scope.get("chapter_title")
                    or ""
                ).strip()
            )

            result: dict[str, Any] | None = None
            if kg_ids:
                result = preflight_scope_resources(
                    scope,
                    check_kg_nodes=True,
                    check_evidence_chunks=False,
                )

            # Resource usability is an OR contract. Evidence retrieval is the
            # slower fallback only when KG data could not prove the scope.
            if (result is None or not result.get("usable")) and has_evidence_locator:
                result = preflight_scope_resources(
                    scope,
                    check_kg_nodes=False,
                    check_evidence_chunks=True,
                )

            if result is None:
                result = preflight_scope_resources(
                    scope,
                    check_kg_nodes=True,
                    check_evidence_chunks=True,
                )

            preflight_cache[cache_key] = result
            return result

        resource_inspector = _default_inspector

    failures: list[dict[str, Any]] = []
    passed = 0

    for idx, item in enumerate(draft_items):
        if not isinstance(item, dict):
            continue

        task_type: str = str(item.get("task_type", "")).strip()
        learning_scope: dict[str, Any] | None = item.get("learning_scope")
        evidence_source: str = str(item.get("evidence_source", "")).strip()
        scope_title: str = ""
        if isinstance(learning_scope, dict):
            scope_title = str(
                learning_scope.get("chapter_title")
                or learning_scope.get("book_title")
                or ""
            ).strip()
        item_title: str = str(item.get("learning_target", "")).strip()

        # ── learn_chapter / review_chapter: scope must be usable ────────
        if task_type in ("learn_chapter", "review_chapter"):
            # All learn/review items MUST have a usable learning_scope.
            # user_specified evidence does NOT bypass scope validation —
            # if the plan has not resolved to a real scope, it must be
            # specifically rejected, not saved as an unexecutable item.
            if not isinstance(learning_scope, dict) or not learning_scope:
                failures.append({
                    "item_index": idx,
                    "task_type": task_type,
                    "title": item_title,
                    "scope_title": scope_title,
                    "error_code": _PF_ITEM_MISSING_SCOPE,
                    "message": (
                        f"Item [{idx}] ({task_type}) \"{item_title}\" has no "
                        f"learning_scope — a chapter-scoped task requires a "
                        f"valid scope with kg_node_ids or document/chapter."
                    ),
                })
                continue

            pf = resource_inspector(learning_scope)
            if not pf["usable"]:
                failures.append({
                    "item_index": idx,
                    "task_type": task_type,
                    "title": item_title,
                    "scope_title": scope_title,
                    "error_code": _PF_ITEM_SCOPE_UNUSABLE,
                    "message": (
                        f"Item [{idx}] ({task_type}) \"{item_title}\" — "
                        f"scope \"{scope_title}\" is not usable: "
                        f"{pf['error_message']}"
                    ),
                    "preflight": pf,
                })
                continue

            passed += 1
            continue

        # ── basic_practice: scope must be usable AND dependency must
        #    point to a learn/review predecessor with the same scope ─────
        if task_type == "basic_practice":
            if not isinstance(learning_scope, dict) or not learning_scope:
                failures.append({
                    "item_index": idx,
                    "task_type": task_type,
                    "title": item_title,
                    "scope_title": scope_title,
                    "error_code": _PF_ITEM_MISSING_SCOPE,
                    "message": (
                        f"Item [{idx}] (basic_practice) \"{item_title}\" has no "
                        f"learning_scope — practice must be tied to a "
                        f"learn/review scope."
                    ),
                })
                continue

            pf = resource_inspector(learning_scope)
            if not pf["usable"]:
                failures.append({
                    "item_index": idx,
                    "task_type": task_type,
                    "title": item_title,
                    "scope_title": scope_title,
                    "error_code": _PF_ITEM_SCOPE_UNUSABLE,
                    "message": (
                        f"Item [{idx}] (basic_practice) \"{item_title}\" — "
                        f"scope \"{scope_title}\" is not usable: "
                        f"{pf['error_message']}"
                    ),
                    "preflight": pf,
                })
                continue

            # Validate dependency chain
            dep = item.get("dependency")
            if dep is None:
                failures.append({
                    "item_index": idx,
                    "task_type": task_type,
                    "title": item_title,
                    "scope_title": scope_title,
                    "error_code": _PF_ITEM_ORPHAN_PRACTICE,
                    "message": (
                        f"Item [{idx}] (basic_practice) \"{item_title}\" has "
                        f"no dependency — basic_practice must depend on a "
                        f"preceding learn_chapter or review_chapter."
                    ),
                })
                continue

            # Dependency is an integer index into draft_items
            if not isinstance(dep, int) or dep < 0 or dep >= len(draft_items):
                failures.append({
                    "item_index": idx,
                    "task_type": task_type,
                    "title": item_title,
                    "scope_title": scope_title,
                    "error_code": _PF_ITEM_PRACTICE_DEPENDENCY_MISSING,
                    "message": (
                        f"Item [{idx}] (basic_practice) \"{item_title}\" "
                        f"dependency {dep!r} is out of range [0, "
                        f"{len(draft_items)})."
                    ),
                })
                continue

            # Dependency must be an EARLIER item (future/cross-order forbidden)
            if dep >= idx:
                failures.append({
                    "item_index": idx,
                    "task_type": task_type,
                    "title": item_title,
                    "scope_title": scope_title,
                    "error_code": _PF_ITEM_PRACTICE_DEPENDENCY_MISSING,
                    "message": (
                        f"Item [{idx}] (basic_practice) \"{item_title}\" "
                        f"dependency [{dep}] must be an earlier item "
                        f"(got dep={dep} >= self={idx})."
                    ),
                })
                continue

            dep_item = draft_items[dep]
            if not isinstance(dep_item, dict):
                failures.append({
                    "item_index": idx,
                    "task_type": task_type,
                    "title": item_title,
                    "scope_title": scope_title,
                    "error_code": _PF_ITEM_PRACTICE_DEPENDENCY_MISSING,
                    "message": (
                        f"Item [{idx}] (basic_practice) \"{item_title}\" "
                        f"dependency [{dep}] is not a valid item dict."
                    ),
                })
                continue

            dep_task_type: str = str(dep_item.get("task_type", "")).strip()
            # Dependency task type MUST be learn_chapter or review_chapter.
            # practice→practice chains are forbidden.
            if dep_task_type not in ("learn_chapter", "review_chapter"):
                failures.append({
                    "item_index": idx,
                    "task_type": task_type,
                    "title": item_title,
                    "scope_title": scope_title,
                    "error_code": _PF_ITEM_PRACTICE_DEPENDENCY_MISSING,
                    "message": (
                        f"Item [{idx}] (basic_practice) \"{item_title}\" "
                        f"depends on [{dep}] ({dep_task_type}) — "
                        f"basic_practice may only depend on learn_chapter "
                        f"or review_chapter, not {dep_task_type}."
                    ),
                })
                continue

            # Dependency scope MUST match practice scope (normalized).
            # Cross-scope practice chains are forbidden.
            dep_scope = dep_item.get("learning_scope")
            if not _scopes_match(learning_scope, dep_scope):
                failures.append({
                    "item_index": idx,
                    "task_type": task_type,
                    "title": item_title,
                    "scope_title": scope_title,
                    "error_code": _PF_ITEM_PRACTICE_SCOPE_MISMATCH,
                    "message": (
                        f"Item [{idx}] (basic_practice) \"{item_title}\" "
                        f"scope does not match dependency [{dep}] "
                        f"(\"{dep_item.get('learning_target', '')}\") scope. "
                        f"Practice must share the same scope as its "
                        f"learn/review predecessor."
                    ),
                })
                continue

            passed += 1
            continue

        # ── advanced_practice: must have usable scope and legal same-scope
        #     predecessor chain (same rules as basic_practice) ──────────
        if task_type == "advanced_practice":
            if not isinstance(learning_scope, dict) or not learning_scope:
                failures.append({
                    "item_index": idx,
                    "task_type": task_type,
                    "title": item_title,
                    "scope_title": scope_title,
                    "error_code": _PF_ITEM_MISSING_SCOPE,
                    "message": (
                        f"Item [{idx}] (advanced_practice) \"{item_title}\" "
                        f"has no learning_scope — advanced practice must be "
                        f"tied to a weakness scope."
                    ),
                })
                continue

            pf = resource_inspector(learning_scope)
            if not pf["usable"]:
                failures.append({
                    "item_index": idx,
                    "task_type": task_type,
                    "title": item_title,
                    "scope_title": scope_title,
                    "error_code": _PF_ITEM_SCOPE_UNUSABLE,
                    "message": (
                        f"Item [{idx}] (advanced_practice) \"{item_title}\" — "
                        f"scope \"{scope_title}\" is not usable: "
                        f"{pf['error_message']}"
                    ),
                    "preflight": pf,
                })
                continue

            # Dependency chain validation (same rules as basic_practice)
            dep = item.get("dependency")
            if dep is not None:
                if not isinstance(dep, int) or dep < 0 or dep >= len(draft_items):
                    failures.append({
                        "item_index": idx,
                        "task_type": task_type,
                        "title": item_title,
                        "scope_title": scope_title,
                        "error_code": _PF_ITEM_PRACTICE_DEPENDENCY_MISSING,
                        "message": (
                            f"Item [{idx}] (advanced_practice) "
                            f"\"{item_title}\" dependency {dep!r} is out of "
                            f"range [0, {len(draft_items)})."
                        ),
                    })
                    continue
                if dep >= idx:
                    failures.append({
                        "item_index": idx,
                        "task_type": task_type,
                        "title": item_title,
                        "scope_title": scope_title,
                        "error_code": _PF_ITEM_PRACTICE_DEPENDENCY_MISSING,
                        "message": (
                            f"Item [{idx}] (advanced_practice) "
                            f"\"{item_title}\" dependency [{dep}] must be "
                            f"an earlier item (got dep={dep} >= self={idx})."
                        ),
                    })
                    continue
                dep_item = draft_items[dep]
                if isinstance(dep_item, dict):
                    dep_task_type = str(dep_item.get("task_type", "")).strip()
                    if dep_task_type not in ("learn_chapter", "review_chapter"):
                        failures.append({
                            "item_index": idx,
                            "task_type": task_type,
                            "title": item_title,
                            "scope_title": scope_title,
                            "error_code": _PF_ITEM_PRACTICE_DEPENDENCY_MISSING,
                            "message": (
                                f"Item [{idx}] (advanced_practice) "
                                f"\"{item_title}\" depends on [{dep}] "
                                f"({dep_task_type}) — advanced_practice may "
                                f"only depend on learn_chapter or "
                                f"review_chapter."
                            ),
                        })
                        continue
                    dep_scope = dep_item.get("learning_scope")
                    if not _scopes_match(learning_scope, dep_scope):
                        failures.append({
                            "item_index": idx,
                            "task_type": task_type,
                            "title": item_title,
                            "scope_title": scope_title,
                            "error_code": _PF_ITEM_PRACTICE_SCOPE_MISMATCH,
                            "message": (
                                f"Item [{idx}] (advanced_practice) "
                                f"\"{item_title}\" scope does not match "
                                f"dependency [{dep}] scope. Advanced "
                                f"practice must share the same scope as "
                                f"its predecessor."
                            ),
                        })
                        continue

            passed += 1
            continue

        # ── practice (legacy): same as basic_practice scope check ────────
        if task_type == "practice":
            if isinstance(learning_scope, dict) and learning_scope:
                pf = resource_inspector(learning_scope)
                if not pf["usable"]:
                    failures.append({
                        "item_index": idx,
                        "task_type": task_type,
                        "title": item_title,
                        "scope_title": scope_title,
                        "error_code": _PF_ITEM_SCOPE_UNUSABLE,
                        "message": (
                            f"Item [{idx}] (practice) \"{item_title}\" — "
                            f"scope \"{scope_title}\" is not usable: "
                            f"{pf['error_message']}"
                        ),
                        "preflight": pf,
                    })
                    continue
            passed += 1
            continue

        # ── stage_exam: allowed formal type, no scope check required ────
        if task_type == "stage_exam":
            passed += 1
            continue

        # ── custom: allowed legacy type from create_custom_plan_draft ───
        #     No scope check required (user-specified custom plans).
        if task_type == "custom":
            passed += 1
            continue

        # ── review_weakness: allowed legacy type from custom plan path ──
        if task_type == "review_weakness":
            passed += 1
            continue

        # ── Unknown / unlisted task types — explicitly REJECTED ─────────
        #     No generic pass-through.  Every formal plan type must be in
        #     the explicit allowlist above.
        if task_type:
            failures.append({
                "item_index": idx,
                "task_type": task_type,
                "title": item_title,
                "scope_title": scope_title,
                "error_code": _PF_ITEM_UNKNOWN_TASK_TYPE,
                "message": (
                    f"Item [{idx}] has unlisted task_type "
                    f"\"{task_type}\" — formal plan items must use one of: "
                    f"learn_chapter, review_chapter, basic_practice, "
                    f"advanced_practice, practice, stage_exam, custom, "
                    f"review_weakness."
                ),
            })
            continue

        # Completely empty item
        failures.append({
            "item_index": idx,
            "task_type": task_type or "(empty)",
            "title": item_title,
            "scope_title": scope_title,
            "error_code": _PF_ITEM_UNKNOWN_TASK_TYPE,
            "message": (
                f"Item [{idx}] has no task_type and cannot be validated."
            ),
        })

    return {
        "ok": len(failures) == 0,
        "failures": failures,
        "passed": passed,
        "failed": len(failures),
    }


def _allocate_daily_budget(daily_minutes: int, weights: list[int]) -> list[int]:
    """Allocate *daily_minutes* across items proportionally by integer *weights*.

    Returns a list of positive integers whose sum is ≤ *daily_minutes*.
    Every allocated minute is a positive integer — zero-minute items are
    never produced.

    Raises :exc:`ValueError` with a specific conflict message when the
    budget is insufficient for the number of items.
    """
    num_items = len(weights)
    if num_items == 0:
        return []

    if daily_minutes < num_items:
        raise ValueError(
            f"Budget conflict: daily_minutes={daily_minutes} is insufficient "
            f"for {num_items} items (need at least {num_items} minute(s)). "
            f"Either increase daily_minutes or reduce item count."
        )

    total_weight = sum(weights)
    if total_weight <= 0:
        raise ValueError(f"Invalid weights: sum={total_weight}")

    # Proportional floor allocation — every item gets at least 1 minute
    allocated = [max(1, daily_minutes * w // total_weight) for w in weights]

    # If floor allocation exceeded budget (possible with rounding),
    # trim excess from the largest items first, never below 1.
    total = sum(allocated)
    excess = total - daily_minutes
    if excess > 0:
        idxs_by_size = sorted(range(num_items), key=lambda i: -allocated[i])
        for idx in idxs_by_size:
            if excess <= 0:
                break
            trim = min(allocated[idx] - 1, excess)
            allocated[idx] -= trim
            excess -= trim

    assert sum(allocated) <= daily_minutes, (
        f"Budget allocation overflow: {sum(allocated)} > {daily_minutes}"
    )
    assert all(m > 0 for m in allocated), (
        f"Zero-minute allocation: {allocated}"
    )
    return allocated


def _has_explicit_plan_revision_semantics(
    message: str,
    parsed_request: dict[str, Any],
) -> bool:
    """Return whether the current message explicitly modifies a prior plan.

    The validated selector is the primary signal.  A small language fallback
    keeps the decision conservative when the selector resolves the scope but
    omits ``revision_constraints`` despite clear modification wording.
    """
    selector = parsed_request.get("scope_selector")
    if isinstance(selector, dict):
        constraints = selector.get("revision_constraints")
        if isinstance(constraints, dict) and constraints:
            return True

    text = re.sub(r"\s+", "", str(message or "")).lower()
    revision_markers = (
        "修改", "调整", "改成", "改为", "变成", "换成", "减少",
        "增加", "加到", "再加", "缩短", "延长", "加长", "压缩",
        "保持", "不变", "少一些", "多一些", "去掉", "删除",
        "替换", "只学", "只安排", "仅保留",
    )
    if any(marker in text for marker in revision_markers):
        return True

    return bool(re.search(
        r"\b(modify|adjust|reduce|increase|shorten|extend|keep\s+the\s+same|"
        r"remove|replace)\b",
        str(message or "").lower(),
    ))


def _is_explicit_standalone_chapter_plan_request(
    message: str,
    parsed_request: dict[str, Any],
) -> bool:
    """Return whether this turn states a complete scope for a new plan.

    A stored parent draft is conversation context, not proof that every later
    plan request is a revision.  The current turn is standalone only when the
    user explicitly asks to create a plan, the unified selector finds a
    chapter/book mention without depending on prior context, and the user does
    not express modification semantics.
    """
    compact_message = re.sub(r"\s+", "", str(message or "")).lower()
    asks_for_new_plan = (
        (
            "计划" in compact_message
            and any(
                marker in compact_message
                for marker in (
                    "制定", "创建", "生成", "新建", "规划", "做一个",
                    "做一份", "来一个", "来一份",
                )
            )
        )
        or "规划整本" in compact_message
        or "规划全书" in compact_message
        or bool(re.search(
            r"\b(create|generate|build|make|draft)\b.{0,40}\bplan\b",
            str(message or "").lower(),
        ))
    )
    if not asks_for_new_plan:
        return False

    selector = parsed_request.get("scope_selector")
    if not isinstance(selector, dict):
        return False
    if selector.get("target_type") not in ("chapter", "chapter_set", "book"):
        return False
    if selector.get("context_dependent") is not False:
        return False
    if not str(selector.get("mention") or "").strip():
        return False
    return not _has_explicit_plan_revision_semantics(message, parsed_request)


def _should_use_deterministic_chapter_plan_generator(
    message: str,
    parsed_request: dict[str, Any],
    scope_candidates: list[dict[str, Any]],
    parent_plan_draft_id: str | None,
    store: PlanDraftStore,
    user_id: str,
) -> bool:
    """Return ``True`` when the request qualifies for deterministic first-time
    chapter plan generation — bypassing the LLM orchestration stage.

    Conditions (ALL must be true):

    1. Intent is ``learn_chapter`` or ``review_chapter``.
    2. ``resolved_scopes`` is non-empty and every scope has
       ``scope_type == "chapter"`` — only real chapter scopes from the
       unified selector enter this generator.  Knowledge-point, disease,
       weakness, and ``unknown`` scopes are excluded.
    3. *scope_candidates* is non-empty — the resolved scopes produced
       at least one valid chapter candidate.
    4. No existing plan items to modify:

       * If *parent_plan_draft_id* is ``None`` → first-time request.
       * If it refers to a clarification-only parent draft
         (``clarification_needed=true`` with empty ``items``) → still
         first-time request.
       * If the parent has items but the current turn explicitly states a
         complete chapter/book scope with no revision semantics → it is a
         new standalone request and still qualifies.
       * Otherwise, a populated parent means this turn modifies the existing
         plan and must go through LLM orchestration.

    This gate handles single-chapter, continuous multi-chapter, discrete
    multi-chapter, and whole-book scopes uniformly — the old whole-book-only
    gate has been merged into this single entry point.

    Non-chapter scopes (knowledge_point, disease, weakness_profile) and
    requests that require the user to make a choice do NOT enter this
    generator — they continue to the orchestration LLM.
    """
    # 1. Intent gate
    if parsed_request.get("intent") not in ("learn_chapter", "review_chapter"):
        return False

    # 2. Must have resolved_scopes, and every scope must be a chapter or
    #    book (which expands to chapters in scope_candidates).
    #    Non-chapter types (knowledge_point, disease, weakness_profile)
    #    are excluded.
    resolved_scopes: list[dict[str, Any]] = parsed_request.get("resolved_scopes", [])
    if not resolved_scopes:
        return False
    if not all(
        isinstance(s, dict) and s.get("scope_type") in ("chapter", "book")
        for s in resolved_scopes
    ):
        return False

    # 3. Must have at least one valid scope candidate
    if len(scope_candidates) == 0:
        return False

    # 4. Parent draft check. A populated parent is a modification only when
    #    the current turn does not independently state a complete new scope.
    if parent_plan_draft_id is not None:
        try:
            parent = store.get(parent_plan_draft_id, user_id=user_id)
        except (KeyError, PermissionError, ValueError):
            # Parent inaccessible → cannot verify it is clarification-only;
            # conservatively treat as modification → go LLM.
            return False

        parent_payload = parent.get("payload", {})
        parent_items = parent_payload.get("items", [])
        if isinstance(parent_items, list) and len(parent_items) > 0:
            if not _is_explicit_standalone_chapter_plan_request(
                message, parsed_request,
            ):
                return False
        # Parent is None-entity or clarification-only → still first-time

    return True


def _generate_deterministic_chapter_plan(
    store: PlanDraftStore,
    *,
    user_id: str,
    parsed_request: dict[str, Any],
    scope_candidates: list[dict[str, Any]],
    orch_context: dict[str, Any],
    parent_plan_draft_id: str | None = None,
) -> dict[str, Any]:
    """Generate a first-time chapter study plan deterministically.

    The plan follows a fixed interleaved progression using real KG chapter
    order (stable, not hardcoded).  For *N* chapters the plan spans *N+1*
    natural days:

    * **Day 1**: ``learn_chapter(ch1)`` + ``review_chapter(ch1)``
    * **Day 2..N**: ``basic_practice(chN-1)`` + ``learn_chapter(chN)``
      + ``review_chapter(chN)``
    * **Day N+1**: ``basic_practice(chN)``

    Every chapter receives exactly one ``learn_chapter``, one
    ``review_chapter``, and one ``basic_practice`` — total **3N** items.
    No ``advanced_practice`` items are produced.

    Budget is allocated via :func:`_allocate_daily_budget` using stable
    weight ratios — no hardcoded minute values.

    Every item carries ``evidence_source = "chapter_sequence_v1"`` so
    that auditing and modification detection can distinguish deterministic
    first-time drafts from LLM-generated ones.

    This generator handles single-chapter, continuous multi-chapter,
    discrete multi-chapter, and whole-book scopes uniformly — the old
    whole-book-only generator has been merged into this single entry point.

    Returns the full draft record from :meth:`PlanDraftStore.get`.
    """
    chapters = list(scope_candidates)
    num_chapters = len(chapters)
    daily_minutes = parsed_request["daily_minutes"]

    # Derive book_title from the first chapter candidate — never from
    # hardcoded values or user phrasing.
    book_title: str = (
        str(chapters[0].get("book_title", "")).strip()
        if chapters
        else ""
    )

    # ── Generate items day by day ──────────────────────────────────────
    raw_items: list[dict[str, Any]] = []
    item_index = 0
    # Track the index of the last review_chapter item for each chapter
    # so the next day's basic_practice can depend on it.
    last_review_idx_by_chapter: dict[str, int] = {}

    # Total days = N + 1
    total_days = num_chapters + 1

    for day_idx in range(total_days):
        if day_idx == 0:
            # ── Day 1: learn_chapter(ch1) + review_chapter(ch1) ──────
            ch = chapters[0]
            scope_ref: str = ch["scope_ref"]
            ch_title: str = ch.get("chapter_title") or scope_ref

            try:
                learn_min, review_min = _allocate_daily_budget(
                    daily_minutes, _DAY1_WEIGHTS
                )
            except ValueError as exc:
                raise OrchestrationValidationError(
                    "daily_budget_exceeded",
                    f"Day 1 budget allocation failed: {exc}",
                ) from exc

            # learn_chapter(ch1)
            raw_items.append({
                "task_type": "learn_chapter",
                "scope_ref": scope_ref,
                "day_offset": 0,
                "minutes": learn_min,
                "reason": f"系统学习《{book_title}》{ch_title}",
                "dependency": None,
            })
            learn_idx = item_index
            item_index += 1

            # review_chapter(ch1) → depends on learn_chapter(ch1)
            raw_items.append({
                "task_type": "review_chapter",
                "scope_ref": scope_ref,
                "day_offset": 0,
                "minutes": review_min,
                "reason": f"即时回顾《{book_title}》{ch_title}核心内容",
                "dependency": learn_idx,
            })
            last_review_idx_by_chapter[scope_ref] = item_index
            item_index += 1

        elif 1 <= day_idx < num_chapters:
            # ── Day 2..N: basic_practice(chN-1) + learn(chN) + review(chN)
            prev_ch = chapters[day_idx - 1]
            prev_scope_ref: str = prev_ch["scope_ref"]
            prev_ch_title: str = prev_ch.get("chapter_title") or prev_scope_ref

            cur_ch = chapters[day_idx]
            cur_scope_ref: str = cur_ch["scope_ref"]
            cur_ch_title: str = cur_ch.get("chapter_title") or cur_scope_ref

            try:
                practice_min, learn_min, review_min = _allocate_daily_budget(
                    daily_minutes, _MIDDLE_DAY_WEIGHTS
                )
            except ValueError as exc:
                raise OrchestrationValidationError(
                    "daily_budget_exceeded",
                    f"Day {day_idx + 1} budget allocation failed: {exc}",
                ) from exc

            # basic_practice(chN-1) → depends on prev chapter's review
            prev_review_idx = last_review_idx_by_chapter.get(prev_scope_ref)
            raw_items.append({
                "task_type": "basic_practice",
                "scope_ref": prev_scope_ref,
                "day_offset": day_idx,
                "minutes": practice_min,
                "reason": f"巩固《{book_title}》{prev_ch_title}基础知识",
                "dependency": prev_review_idx,
            })
            item_index += 1

            # learn_chapter(chN)
            raw_items.append({
                "task_type": "learn_chapter",
                "scope_ref": cur_scope_ref,
                "day_offset": day_idx,
                "minutes": learn_min,
                "reason": f"系统学习《{book_title}》{cur_ch_title}",
                "dependency": None,
            })
            learn_idx = item_index
            item_index += 1

            # review_chapter(chN) → depends on learn_chapter(chN)
            raw_items.append({
                "task_type": "review_chapter",
                "scope_ref": cur_scope_ref,
                "day_offset": day_idx,
                "minutes": review_min,
                "reason": f"即时回顾《{book_title}》{cur_ch_title}核心内容",
                "dependency": learn_idx,
            })
            last_review_idx_by_chapter[cur_scope_ref] = item_index
            item_index += 1

        else:
            # ── Day N+1: basic_practice(chN) ──────────────────────────
            last_ch = chapters[-1]
            last_scope_ref: str = last_ch["scope_ref"]
            last_ch_title: str = last_ch.get("chapter_title") or last_scope_ref

            try:
                (practice_min,) = _allocate_daily_budget(
                    daily_minutes, _LAST_DAY_WEIGHTS
                )
            except ValueError as exc:
                raise OrchestrationValidationError(
                    "daily_budget_exceeded",
                    f"Day {total_days} budget allocation failed: {exc}",
                ) from exc

            prev_review_idx = last_review_idx_by_chapter.get(last_scope_ref)
            raw_items.append({
                "task_type": "basic_practice",
                "scope_ref": last_scope_ref,
                "day_offset": day_idx,
                "minutes": practice_min,
                "reason": f"巩固《{book_title}》{last_ch_title}基础知识",
                "dependency": prev_review_idx,
            })
            item_index += 1

    # ── Build candidate dict for validation ────────────────────────────
    candidate = {"clarification_needed": False, "items": raw_items}

    # ── Validate against context (budget, dependencies, scope coverage,
    #     task-type composition, no advanced_practice with weakness) ─────
    #     The deterministic schedule always uses N+1 days — override the
    #     user-requested total_days so that validation accepts the
    #     day_offset range.
    orch_context["total_days"] = total_days
    validated = validate_orchestration_candidate(candidate, orch_context)

    # ── Map to draft-item shape ───────────────────────────────────────
    base_items = _map_orchestration_items_to_draft_items(
        validated["items"], orch_context, parsed_request,
    )

    # ── Override evidence_source to the non-forgeable deterministic mark
    draft_items: list[dict[str, Any]] = []
    for item in base_items:
        item["evidence_source"] = _SOURCE_DETERMINISTIC_CHAPTER_PLAN
        draft_items.append(item)

    # ── Preflight: validate item structure (scope existence, dependency
    #     chains, and real resource availability).
    _pf = _preflight_draft_items(draft_items)
    if not _pf["ok"]:
        raise OrchestrationValidationError(
            "scope_preflight_failed",
            f"Deterministic chapter plan preflight failed: "
            f"{_pf['failed']} item(s) have unusable scopes. "
            f"First failure: {_pf['failures'][0]['message']}",
        )

    # ── Persist ───────────────────────────────────────────────────────
    payload: dict[str, Any] = {
        "request": parsed_request,
        "items": draft_items,
    }
    draft_id = store.save(
        user_id=user_id,
        payload=payload,
        parent_plan_draft_id=parent_plan_draft_id,
    )
    return store.get(draft_id, user_id=user_id)


def _should_use_deterministic_weakness_plan_generator(
    parsed_request: dict[str, Any],
    scope_candidates: list[dict[str, Any]],
    parent_plan_draft_id: str | None,
    store: PlanDraftStore,
    user_id: str,
) -> bool:
    """Return ``True`` when the request qualifies for deterministic first-time
    weakness plan generation — bypassing the LLM orchestration stage.

    Conditions (ALL must be true):

    1. Intent is ``review_weakness``.
    2. The unified scope selector's ``target_type`` is ``weakness_profile``.
    3. ``resolved_scopes`` is non-empty and every scope has
       ``scope_type == "weakness_profile"`` with valid provenance/evidence
       (``has_weakness_evidence`` is ``True`` and ``weakness_detail`` is
       non-empty).
    4. *scope_candidates* is non-empty — the resolved scopes produced at
       least one valid candidate with real evidence.
    5. No existing plan items to modify:

       * If *parent_plan_draft_id* is ``None`` → first-time request.
       * If it refers to a clarification-only parent draft
         (``clarification_needed=true`` with empty ``items``) → still
         first-time request.
       * If the parent draft has non-empty ``items`` → this is a
         **modification** of an existing plan — must go through LLM
         orchestration.

    This gate is the weakness counterpart of
    :func:`_should_use_deterministic_chapter_plan_generator`.  It shares
    the same parent-draft check pattern and does NOT duplicate chapter
    gate logic.
    """
    # 1. Intent gate — only review_weakness
    if parsed_request.get("intent") != "review_weakness":
        return False

    # 2. Scope selector must target weakness_profile
    scope_selector = parsed_request.get("scope_selector", {})
    if not isinstance(scope_selector, dict):
        return False
    if scope_selector.get("target_type") != "weakness_profile":
        return False

    # 3. Must have resolved_scopes, and every scope must be
    #    weakness_profile with real evidence (has_weakness_evidence
    #    and non-empty weakness_detail).
    resolved_scopes: list[dict[str, Any]] = parsed_request.get("resolved_scopes", [])
    if not resolved_scopes:
        return False
    if not all(
        isinstance(s, dict)
        and s.get("scope_type") == "weakness_profile"
        and s.get("has_weakness_evidence") is True
        and s.get("weakness_detail")
        for s in resolved_scopes
    ):
        return False

    # 4. Must have at least one valid scope candidate
    if len(scope_candidates) == 0:
        return False

    # 5. Parent draft check — only clarification-only parents qualify
    if parent_plan_draft_id is not None:
        try:
            parent = store.get(parent_plan_draft_id, user_id=user_id)
        except (KeyError, PermissionError, ValueError):
            # Parent inaccessible → cannot verify it is clarification-only;
            # conservatively treat as modification → go LLM.
            return False

        parent_payload = parent.get("payload", {})
        parent_items = parent_payload.get("items", [])
        if isinstance(parent_items, list) and len(parent_items) > 0:
            # Parent has real plan items → this is a modification
            return False
        # Parent is None-entity or clarification-only → still first-time

    return True


def _generate_deterministic_weakness_plan(
    store: PlanDraftStore,
    *,
    user_id: str,
    parsed_request: dict[str, Any],
    scope_candidates: list[dict[str, Any]],
    orch_context: dict[str, Any],
    parent_plan_draft_id: str | None = None,
) -> dict[str, Any]:
    """Generate a first-time weakness study plan deterministically.

    The plan follows a fixed daily structure using real weakness evidence
    ordering from the learner portrait/mastery (stable, not hardcoded).
    For *N* validated weaknesses the plan spans exactly *N* natural days:

    * **Day i** (0 ≤ i < N): ``review_chapter(weakness_i)``
      + ``basic_practice(weakness_i)`` (practice depends on review)

    Every weakness receives exactly one ``review_chapter`` and one
    ``basic_practice`` — total **2N** items.
    No ``learn_chapter`` or ``advanced_practice`` items are produced.

    Budget is allocated via :func:`_allocate_daily_budget` using the
    stable ``_WEAKNESS_DAY_WEIGHTS`` ratio — no hardcoded minute values.

    Every item carries ``evidence_source = "weakness_first_time_v1"`` so
    that auditing and modification detection can distinguish deterministic
    first-time drafts from LLM-generated ones.

    Returns the full draft record from :meth:`PlanDraftStore.get`.
    """
    weaknesses = list(scope_candidates)
    num_weaknesses = len(weaknesses)
    daily_minutes = parsed_request["daily_minutes"]

    # ── Generate items day by day ──────────────────────────────────────
    raw_items: list[dict[str, Any]] = []
    item_index = 0

    # Total days = N (one weakness per day)
    total_days = num_weaknesses

    for day_idx in range(total_days):
        w = weaknesses[day_idx]
        scope_ref: str = w["scope_ref"]

        # Derive a human-readable label for the weakness
        wd_list: list[dict[str, Any]] = w.get("weakness_detail", [])
        if wd_list and isinstance(wd_list, list) and len(wd_list) > 0:
            wd = wd_list[0] if isinstance(wd_list[0], dict) else {}
            weak_label = (
                str(wd.get("kg_node_name", "")).strip()
                or str(wd.get("kg_node_id", "")).strip()
                or scope_ref
            )
        else:
            weak_label = scope_ref

        book_title: str = str(w.get("book_title", "")).strip()
        ch_title: str = str(w.get("chapter_title", "")).strip()
        # Build location suffix from real metadata
        location_parts: list[str] = []
        if book_title:
            location_parts.append(f"《{book_title}》")
        if ch_title:
            location_parts.append(ch_title)
        location_suffix = "".join(location_parts)

        try:
            review_min, practice_min = _allocate_daily_budget(
                daily_minutes, _WEAKNESS_DAY_WEIGHTS
            )
        except ValueError as exc:
            raise OrchestrationValidationError(
                "daily_budget_exceeded",
                f"Day {day_idx + 1} weakness plan budget allocation failed: {exc}",
            ) from exc

        # review_chapter(weakness_i)
        review_reason = f"复习薄弱点：{weak_label}"
        if location_suffix:
            review_reason += f"（{location_suffix}）"
        raw_items.append({
            "task_type": "review_chapter",
            "scope_ref": scope_ref,
            "day_offset": day_idx,
            "minutes": review_min,
            "reason": review_reason,
            "dependency": None,
        })
        review_idx = item_index
        item_index += 1

        # basic_practice(weakness_i) → depends on review_chapter(weakness_i)
        practice_reason = f"基础练习巩固薄弱点：{weak_label}"
        if location_suffix:
            practice_reason += f"（{location_suffix}）"
        raw_items.append({
            "task_type": "basic_practice",
            "scope_ref": scope_ref,
            "day_offset": day_idx,
            "minutes": practice_min,
            "reason": practice_reason,
            "dependency": review_idx,
        })
        item_index += 1

    # ── Build candidate dict for validation ────────────────────────────
    candidate = {"clarification_needed": False, "items": raw_items}

    # ── Validate against context (budget, dependencies, scope coverage —
    #     no advanced_practice items exist so the weakness-evidence check
    #     is irrelevant).  Override total_days to match N weaknesses
    #     so that validation accepts the day_offset range.
    #     Also override is_whole_book=False — weakness plans are never
    #     whole-book plans; they target individual weak knowledge points,
    #     not sequential chapter progression.  The whole-book scope
    #     coverage and task-type composition rules do not apply.
    orch_context["total_days"] = total_days
    orch_context["is_whole_book"] = False
    validated = validate_orchestration_candidate(candidate, orch_context)

    # ── Map to draft-item shape ───────────────────────────────────────
    base_items = _map_orchestration_items_to_draft_items(
        validated["items"], orch_context, parsed_request,
    )

    # ── Override evidence_source to the non-forgeable deterministic mark
    draft_items: list[dict[str, Any]] = []
    for item in base_items:
        item["evidence_source"] = _SOURCE_DETERMINISTIC_WEAKNESS_PLAN
        draft_items.append(item)

    # ── Preflight: validate item structure (scope existence, dependency
    #     chains, and real resource availability).
    _pf = _preflight_draft_items(draft_items)
    if not _pf["ok"]:
        raise OrchestrationValidationError(
            "scope_preflight_failed",
            f"Deterministic weakness plan preflight failed: "
            f"{_pf['failed']} item(s) have unusable scopes. "
            f"First failure: {_pf['failures'][0]['message']}",
        )

    # ── Persist ───────────────────────────────────────────────────────
    payload: dict[str, Any] = {
        "request": parsed_request,
        "items": draft_items,
    }
    draft_id = store.save(
        user_id=user_id,
        payload=payload,
        parent_plan_draft_id=parent_plan_draft_id,
    )
    return store.get(draft_id, user_id=user_id)


# ---------------------------------------------------------------------------
# Combined service: natural-language → parsed request → scope → draft
# ---------------------------------------------------------------------------


def _build_raw_kg_id_to_chapter_lookup(
    chapters: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Build a lookup from raw ``kg://`` node IDs to chapter records.

    Iterates over the real KG nodes (via :func:`~.planner.load_kg_nodes`)
    and matches each raw node to a chapter from *chapters* by comparing
    ``document_id`` and chapter title (bidirectional containment).

    This is the complement of the stable-core lookup in
    :func:`_enrich_weakness_resolved_scopes`: chapters carry ``core_ids``
    (stable ``core:*`` digests) which do not match old-style ``kg://``
    node IDs stored in learner memory weakness slots.  This lookup bridges
    the two identifier spaces using the real KG node metadata.
    """
    from backend.services.learning.planner import load_kg_nodes

    try:
        all_nodes = load_kg_nodes()
    except Exception:
        return {}

    # Build document_id → chapter list index for efficient matching
    doc_to_chapters: dict[str, list[dict[str, Any]]] = {}
    for ch in chapters:
        if not isinstance(ch, dict):
            continue
        did = str(ch.get("document_id", "")).strip()
        if did:
            doc_to_chapters.setdefault(did, []).append(ch)

    result: dict[str, dict[str, Any]] = {}
    for node in all_nodes:
        nid = str(node.get("node_id", "")).strip()
        if not nid:
            continue

        meta = node.get("metadata")
        node_doc = (
            str(meta.get("document_id", "")).strip()
            if isinstance(meta, dict) else ""
        )
        if not node_doc:
            continue

        node_chapter = str(node.get("chapter", "")).strip()
        candidates = doc_to_chapters.get(node_doc, [])
        for ch in candidates:
            ch_title = str(ch.get("title", "")).strip()
            if node_chapter and ch_title:
                # Bidirectional containment — same rule as
                # _resolve_stable_core_to_raw_nodes in planner.py
                if node_chapter in ch_title or ch_title in node_chapter:
                    result[nid] = ch
                    break

    return result


def _enrich_weakness_resolved_scopes(
    parsed_request: dict[str, Any],
    *,
    chapters: list[dict[str, Any]],
    memory: Any,
    user_id: str,
) -> list[dict[str, Any]] | None:
    """Enrich resolved_scopes from real learner-memory weakness evidence.

    Called during :func:`create_plan_draft_from_text` when the unified
    scope selector's ``target_type`` is ``weakness_profile`` and a
    :class:`LearningMemory` instance is available.  Reads the weakness
    overview from :func:`~profile_memory.get_weakness_overview`,
    maps each weakness/low-mastery KG node ID to its parent chapter
    record, and returns an ordered list of ``resolved_scopes`` dicts
    — one per unique weakness entry — suitable for building scope
    candidates and feeding the deterministic weakness plan generator.

    Returns ``None`` when:
    * No weakness/low-mastery evidence with KG node IDs exists
    * No chapters match any weakness KG node IDs

    Every field in the returned scopes comes from real learner-memory
    evidence or KG chapter records — nothing is invented.
    """
    from backend.services.learning.profile_memory import get_weakness_overview

    overview = get_weakness_overview(memory, user_id)
    if not overview["has_evidence"]:
        return None

    # Build KG node ID → chapter lookup from stable core:* IDs
    kg_to_chapter: dict[str, dict[str, Any]] = {}
    for ch in chapters:
        if not isinstance(ch, dict):
            continue
        core_ids = [
            str(cid).strip()
            for cid in (ch.get("core_ids") or [])
            if str(cid).strip()
        ]
        for cid in core_ids:
            if cid not in kg_to_chapter:
                kg_to_chapter[cid] = ch

    # Build raw kg:// node ID → chapter lookup for backward compatibility
    # with old-style raw KG node IDs stored in learner memory weakness
    # slots.  The direct kg_to_chapter uses stable core:* IDs as keys;
    # raw kg:// IDs need a separate resolution path through the real KG
    # node metadata (document_id + chapter title matching).
    _raw_to_chapter: dict[str, dict[str, Any]] = {}
    _has_raw_ids = any(
        str(kid).startswith("kg://")
        for entry in (overview["weaknesses"] + overview["low_mastery"])
        for kid in entry.get("kg_node_ids", [])
    )
    if _has_raw_ids:
        _raw_to_chapter = _build_raw_kg_id_to_chapter_lookup(chapters)

    # Build weakness_detail lookup from overview
    detail_by_kg_id: dict[str, dict[str, Any]] = {}
    for w in overview["weaknesses"]:
        for kid in w["kg_node_ids"]:
            if kid not in detail_by_kg_id:
                detail_by_kg_id[kid] = {
                    "kg_node_id": kid,
                    "kg_node_name": w["label"],
                    "evidence_source": w["evidence_source"],
                }
    for lm in overview["low_mastery"]:
        for kid in lm["kg_node_ids"]:
            if kid not in detail_by_kg_id:
                detail_by_kg_id[kid] = {
                    "kg_node_id": kid,
                    "kg_node_name": lm["label"],
                    "evidence_source": lm["evidence_source"],
                }

    # Build resolved scopes: one per unique weakness entry, preserving
    # the ordering from the overview (weaknesses first, then low_mastery).
    resolved: list[dict[str, Any]] = []
    seen_kg_ids: set[str] = set()

    for w in overview["weaknesses"]:
        for kid in w["kg_node_ids"]:
            if kid in seen_kg_ids:
                continue
            seen_kg_ids.add(kid)
            ch = kg_to_chapter.get(kid) or _raw_to_chapter.get(kid)
            wd = detail_by_kg_id.get(kid, {
                "kg_node_id": kid,
                "kg_node_name": kid,
                "evidence_source": "learner_memory_weakness",
            })
            kg_node_names: dict[str, str] = {kid: wd["kg_node_name"]}
            resolved.append({
                "scope_ref": kid,
                "scope_type": "weakness_profile",
                "book_title": (
                    str(ch.get("book_title", "")).strip()
                    if ch else None
                ),
                "document_id": (
                    str(ch.get("document_id", "")).strip()
                    if ch else None
                ),
                "chapter_title": (
                    str(ch.get("title", "")).strip()
                    if ch else None
                ),
                "kg_node_ids": [kid],
                "kg_node_names": kg_node_names,
                "provenance": "weakness_profile",
                "has_weakness_evidence": True,
                "weakness_detail": [wd],
            })

    for lm in overview["low_mastery"]:
        for kid in lm["kg_node_ids"]:
            if kid in seen_kg_ids:
                continue
            seen_kg_ids.add(kid)
            ch = kg_to_chapter.get(kid) or _raw_to_chapter.get(kid)
            wd = detail_by_kg_id.get(kid, {
                "kg_node_id": kid,
                "kg_node_name": kid,
                "evidence_source": "low_mastery",
            })
            kg_node_names = {kid: wd["kg_node_name"]}
            resolved.append({
                "scope_ref": kid,
                "scope_type": "weakness_profile",
                "book_title": (
                    str(ch.get("book_title", "")).strip()
                    if ch else None
                ),
                "document_id": (
                    str(ch.get("document_id", "")).strip()
                    if ch else None
                ),
                "chapter_title": (
                    str(ch.get("title", "")).strip()
                    if ch else None
                ),
                "kg_node_ids": [kid],
                "kg_node_names": kg_node_names,
                "provenance": "weakness_profile",
                "has_weakness_evidence": True,
                "weakness_detail": [wd],
            })

    if not resolved:
        return None

    return resolved


def create_plan_draft_from_text(
    store: PlanDraftStore,
    *,
    user_id: str,
    message: str,
    chapters: list[dict[str, Any]] | None = None,
    llm_call: Callable[[str], str] | None = None,
    parent_plan_draft_id: str | None = None,
    memory: LearningMemory | None = None,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Parse a natural-language plan request, orchestrate via LLM, and persist.

    This is a **pure composition service** that chains:

    1. :func:`parse_plan_request_text` — unified LLM scope + plan-parameter
       extraction (FIRST and only content-semantic LLM call).  Returns a
       normalised ``request`` dict PLUS ``scope_selector`` and
       ``resolved_scopes``.
    2. Scope candidate conversion — :func:`_build_scope_candidates_from_resolved_scopes`
       converts pre-validated ``resolved_scopes`` into orchestration
       ``scope_candidates`` (no additional LLM call).
    3. :func:`build_orchestration_context` — validated context dict.
    4. Second LLM call — personalized plan orchestration within validated
       context constraints.
    5. :func:`validate_orchestration_candidate` — server-side validation of
       the LLM output against the context.
    6. Mapping validated items → draft-item shape → persistence in *store*.

    It does **not** duplicate parsing, scope validation, ID generation, TTL,
    or storage logic.  It does **not** call the MasterRouter, HTTP layer,
    database, or any downstream API.

    **For non-empty requests, this function MUST call the LLM for
    orchestration — it MUST NOT fall back to** :func:`_build_draft_items`
    **or any fixed template.**

    Parameters
    ----------
    store:
        The :class:`PlanDraftStore` instance to persist into.
    user_id:
        Owner of the draft.
    message:
        Non-empty user natural-language text (e.g.
        ``"三天复习《牙周病学》第三章，每天30分钟"``).
    chapters:
        Chapter candidate dicts as provided by the caller (the production
        KG shape).  May be ``None`` or empty when no chapter context is
        available.
    llm_call:
        Injectable ``(prompt: str) -> str`` callable used for **both** the
        parse step (forwarded to :func:`parse_plan_request_text`) and the
        orchestration step.  When ``None``, a default callable is built from
        the project's LLM client.  Tests should inject a fake that handles
        both calls.
    parent_plan_draft_id:
        Optional lineage forwarded to :meth:`PlanDraftStore.save`.  The
        parent draft is never mutated.
    memory:
        Optional :class:`LearningMemory` instance for resolving real
        weakness evidence.  When provided together with *user_id*,
        ``has_weakness_evidence`` in the orchestration context is derived
        from learner-memory data (weakness items, low-mastery scores).
        When ``None``, the field defaults to ``False``.

    Returns
    -------
    dict
        The full draft record as returned by :meth:`PlanDraftStore.get`,
        containing ``plan_draft_id``, ``user_id``, ``payload.request``
        (normalised) and ``payload.items``.  Chapter-scoped items carry a
        real ``learning_scope`` resolved from *chapters*.

        When the orchestration LLM returns ``clarification_needed=true``,
        the payload includes ``"clarification_needed": true`` and an empty
        ``items`` list — no tasks are invented.

    Raises
    ------
    PlanRequestParseError
        From :func:`parse_plan_request_text` when the message cannot be
        parsed or validated (empty message, LLM failure, invalid JSON,
        etc.).
    OrchestrationValidationError
        When the orchestration LLM call fails, returns empty/invalid JSON,
        or the validator rejects the candidate.  No draft is persisted in
        any of these cases — there is no hardcoded template fallback.
    KeyError, PermissionError
        From :class:`PlanDraftStore` when *parent_plan_draft_id* is
        unknown, cross-user, or expired.

    Notes
    -----
    Every error path is designed to leave the store untouched — no
    half-written draft is ever persisted on failure.
    """
    _chapters = [ch for ch in (chapters or []) if isinstance(ch, dict)]

    # ── Shared JSON-mode compatibility state across parse + orchestration ─
    # Both stages use json_mode=True by default.  If the parse probe finds
    # that the provider does not support response_format (timeout / empty /
    # unsupported), it writes json_mode_supported=False.  The orchestration
    # stage reads this and skips its own probe — saving a second timeout.
    _compat_state: dict[str, Any] = {}

    # 1. Parse natural-language message → unified result (ONE LLM call
    #    for content understanding — scope + plan decorator fields).
    parsed_request = parse_plan_request_text(
        message,
        chapters=chapters,
        llm_call=llm_call,
        history=history,
        memory=memory,
        user_id=user_id,
        _compat_state=_compat_state,
    )

    # 1b. Inherit scope from parent draft when current request lacks it.
    #     This enables modification flows: the user changes daily_minutes/days
    #     without re-stating the chapter/book, and the system retains the
    #     original scope from the prior plan draft.
    #
    #     The inherited scope is stored in TWO forms:
    #     - Old compatibility fields (book_title, document_id, chapter_key)
    #       for output projection to external readers.
    #     - resolved_scopes (the unified contract) for scope candidate
    #       building — constructed from parent items' learning_scope data.
    if parent_plan_draft_id is not None:
        _bt = parsed_request.get("book_title")
        _ck = parsed_request.get("chapter_key")
        if _bt is None and _ck is None:
            try:
                parent_draft = store.get(parent_plan_draft_id, user_id=user_id)
                parent_request: dict[str, Any] = (
                    parent_draft.get("payload", {}).get("request", {})
                )
                if isinstance(parent_request, dict):
                    _p_bt = parent_request.get("book_title")
                    _p_ck = parent_request.get("chapter_key")
                    if _p_bt is not None or _p_ck is not None:
                        # --- Compatibility projection for old readers ---
                        parsed_request["book_title"] = _p_bt
                        parsed_request["document_id"] = parent_request.get("document_id")
                        parsed_request["chapter_key"] = _p_ck
                        # Preserve parent focus_text only when current is None
                        if parsed_request.get("focus_text") is None:
                            parsed_request["focus_text"] = parent_request.get("focus_text")
                        # Inherit parent intent when current is custom / generic
                        _p_intent = parent_request.get("intent")
                        if (
                            parsed_request.get("intent") == "custom"
                            and _p_intent in ("learn_chapter", "review_chapter")
                        ):
                            parsed_request["intent"] = _p_intent

                        # --- Build resolved_scopes from parent items ---
                        # The unified contract for scope handoff.  Extract
                        # every unique learning_scope from the parent draft's
                        # items so that scope candidates are built from real
                        # KG data — not from the old chapter_key field.
                        if not parsed_request.get("resolved_scopes"):
                            parent_items = (
                                parent_draft.get("payload", {}).get("items", [])
                            )
                            _inherited: list[dict[str, Any]] = []
                            _seen_keys: set[str] = set()
                            for item in parent_items:
                                ls = item.get("learning_scope")
                                if not isinstance(ls, dict):
                                    continue
                                ck = str(ls.get("chapter_key", "")).strip()
                                if not ck or ck in _seen_keys:
                                    continue
                                _seen_keys.add(ck)
                                _inherited.append({
                                    "scope_type": "chapter",
                                    "scope_ref": ck,
                                    "book_title": ls.get("book_title"),
                                    "document_id": ls.get("document_id"),
                                    "chapter_title": ls.get("chapter_title"),
                                    "kg_node_ids": list(ls.get("kg_node_ids", [])),
                                    "kg_node_names": dict(ls.get("kg_node_names", {})),
                                    "has_weakness_evidence": False,
                                    "weakness_detail": [],
                                })
                            if _inherited:
                                parsed_request["resolved_scopes"] = _inherited
            except (KeyError, PermissionError, ValueError):
                # Parent draft inaccessible — clear the parent reference so
                # store.save() below does not attempt its own validation.
                # The orchestration LLM will see empty scope_candidates and
                # return clarification_needed rather than invent a scope.
                parent_plan_draft_id = None

    _selector = parsed_request.get("scope_selector", {})
    if (
        parsed_request.get("clarification_needed")
        and not parsed_request.get("resolved_scopes")
        and isinstance(_selector, dict)
        and _selector.get("target_type") == "book"
        and parsed_request.get("clarification_options")
    ):
        payload = {
            "request": parsed_request,
            "items": [],
            "clarification_needed": True,
            "clarification_message": parsed_request.get("clarification_message"),
            "clarification_options": parsed_request.get("clarification_options", []),
        }
        draft_id = store.save(
            user_id=user_id,
            payload=payload,
            parent_plan_draft_id=parent_plan_draft_id,
        )
        return store.get(draft_id, user_id=user_id)

    # 1c. Enrich resolved_scopes for weakness_profile from real learner
    #      memory evidence.  The unified scope selector's own weakness
    #      resolution may produce empty scopes (it does not have direct
    #      access to the learner-memory API used by profile_memory).
    #      This step calls get_weakness_overview — the canonical
    #      deterministic reader for weakness evidence — and populates
    #      resolved_scopes with validated, ordered weakness entries.
    if (
        isinstance(_selector, dict)
        and _selector.get("target_type") == "weakness_profile"
        and memory is not None
        and user_id is not None
    ):
        enriched = _enrich_weakness_resolved_scopes(
            parsed_request,
            chapters=_chapters,
            memory=memory,
            user_id=user_id,
        )
        if enriched is not None:
            parsed_request["resolved_scopes"] = enriched

    # 2. Build scope candidates exclusively from pre-validated
    #    resolved_scopes (the unified contract).  There is NO fallback
    #    to _build_scope_candidates_for_orchestration — the old
    #    chapter_key-based resolution is never called from this path.
    #    When resolved_scopes is empty and the request is not a
    #    clarification, that is a hard error (no scope-less plans).
    resolved_scopes: list[dict[str, Any]] = parsed_request.get("resolved_scopes", [])
    if not resolved_scopes:
        if parsed_request.get("clarification_needed"):
            # Empty scope + clarification → let the orchestration LLM
            # handle the clarification flow naturally.
            scope_candidates = []
        else:
            raise OrchestrationValidationError(
                "missing_resolved_scope",
                "No resolved scope available.  The unified scope selector "
                "did not produce any resolved_scopes, and no parent draft "
                "scope could be inherited.  A plan cannot be generated "
                "without a validated learning scope.",
            )
    else:
        scope_candidates = _build_scope_candidates_from_resolved_scopes(
            resolved_scopes, _chapters, memory=memory, user_id=user_id,
        )

    # 3. Build orchestration context — with optional parent metrics and
    #    revision constraints for modification request validation.
    _parent_metrics: dict[str, Any] | None = None
    _revision_constraints: dict[str, Any] | None = None
    if parent_plan_draft_id is not None:
        # Parent draft was already loaded above for scope inheritance;
        # reload to get its items for metrics computation.
        try:
            _pd = store.get(parent_plan_draft_id, user_id=user_id)
            _p_items = _pd.get("payload", {}).get("items", [])
            if isinstance(_p_items, list) and _p_items:
                _parent_metrics = _compute_parent_metrics(_p_items)
        except (KeyError, PermissionError, ValueError):
            pass  # Parent inaccessible → no metrics comparison possible

    # Extract revision_constraints from the unified selector
    if isinstance(parsed_request.get("scope_selector"), dict):
        _rc = parsed_request["scope_selector"].get("revision_constraints")
        if isinstance(_rc, dict) and _rc:
            _revision_constraints = _rc

    orch_context = build_orchestration_context(
        scope_candidates=scope_candidates,
        daily_minutes=parsed_request["daily_minutes"],
        total_days=parsed_request["days"],
        focus_text=parsed_request.get("focus_text"),
        intent=parsed_request["intent"],
        parent_metrics=_parent_metrics,
        revision_constraints=_revision_constraints,
    )

    # 3b. Deterministic first-time chapter plan fast path — bypass LLM
    #      orchestration entirely when the request has resolved chapter
    #      scopes from the unified selector with no existing items to
    #      modify.  The parsed request has already been validated, scope
    #      candidates resolved from the real KG, and the orchestration
    #      context built.  This path produces exactly one plan with stable
    #      interleaved progression (learn → review → practice), no
    #      advanced_practice, and a non-forgeable evidence_source.
    #      Handles single-chapter, multi-chapter, and whole-book uniformly.
    if _should_use_deterministic_chapter_plan_generator(
        message,
        parsed_request,
        scope_candidates,
        parent_plan_draft_id,
        store,
        user_id,
    ):
        return _generate_deterministic_chapter_plan(
            store,
            user_id=user_id,
            parsed_request=parsed_request,
            scope_candidates=scope_candidates,
            orch_context=orch_context,
            parent_plan_draft_id=parent_plan_draft_id,
        )

    # 3c. Deterministic first-time weakness plan fast path — bypass LLM
    #      orchestration entirely when the unified selector has resolved
    #      to weakness_profile with real validated evidence from learner
    #      memory/profile/mastery.  Each weakness becomes one day with
    #      exactly one review_chapter + one basic_practice (practice
    #      depends on review).  No learn_chapter, no advanced_practice.
    #      Order comes from the resolved_scopes ordering contract (real
    #      portrait/mastery rank — not hardcoded).
    #
    # 3c-pre. No-evidence weakness_profile gate: when the unified selector
    #      targeted weakness_profile AND memory is available (caller opted
    #      into weakness resolution), but no real evidence exists (zero
    #      scope candidates), return a direct no-evidence message — no LLM
    #      orchestration, no template plan, no fallback to global plan.
    #      When memory is NOT available, fall through to the LLM
    #      orchestration path for backward compatibility.
    if (
        isinstance(_selector, dict)
        and _selector.get("target_type") == "weakness_profile"
        and memory is not None
        and user_id is not None
    ):
        if len(scope_candidates) == 0:
            # No real weakness evidence → direct no-evidence draft
            _no_ev_payload: dict[str, Any] = {
                "request": parsed_request,
                "items": [],
                "clarification_needed": True,
                "clarification_message": (
                    "当前没有积累足够的薄弱点证据来制定针对性复习计划。"
                    "薄弱点证据来自测验错题、重复混淆和低掌握度评分——"
                    "完成更多学习会话和练习后，系统会自动识别并记录您的薄弱环节。"
                ),
            }
            _no_ev_draft_id = store.save(
                user_id=user_id,
                payload=_no_ev_payload,
                parent_plan_draft_id=parent_plan_draft_id,
            )
            return store.get(_no_ev_draft_id, user_id=user_id)

    if _should_use_deterministic_weakness_plan_generator(
        parsed_request,
        scope_candidates,
        parent_plan_draft_id,
        store,
        user_id,
    ):
        return _generate_deterministic_weakness_plan(
            store,
            user_id=user_id,
            parsed_request=parsed_request,
            scope_candidates=scope_candidates,
            orch_context=orch_context,
            parent_plan_draft_id=parent_plan_draft_id,
        )

    # 4. Build orchestration prompt
    orch_prompt = _build_orchestration_prompt(orch_context)

    # 5. Resolve LLM callable for orchestration (separate from parse step —
    #    orchestration needs higher max_tokens for multi-item plans.
    #    json_mode=True enables the compatibility retry in _make_default_llm_call:
    #    first try with response_format={"type":"json_object"}, and if the
    #    provider returns empty content, retry once without response_format.
    #    The prompt already instructs the LLM to output a single JSON object,
    #    and validate_orchestration_candidate enforces the contract server-side.
    #    max_tokens=4000 gives enough room for verbose Chinese reasons across
    #    multi-day plans without truncation.)
    if llm_call is not None:
        _call = llm_call
    else:
        try:
            _call = _make_default_llm_call(max_tokens=4000, json_mode=True, _compat_state=_compat_state)
        except Exception as exc:
            raise OrchestrationValidationError(
                "llm_not_configured",
                f"Failed to create LLM callable for orchestration: {exc}",
            ) from exc

    # 6-8. Call LLM for orchestration, parse, and validate.
    #      Retry budget: at most ONE retry total across all error categories
    #      (truncation, clarification-with-scope, scope-coverage-incomplete,
    #      task-type-composition-incomplete, practice_dependency_invalid).
    #      These cannot stack — if the first attempt is truncated, the
    #      retry targets truncation; if it fails validation, the retry
    #      targets the specific missing contract.  A second failure returns
    #      a readable error — no further retries.
    _MAX_ORCH_RETRIES = 1
    _orch_raw_output: str = ""
    for _orch_attempt in range(_MAX_ORCH_RETRIES + 1):
        # 6. Call LLM for orchestration
        try:
            _orch_raw_output = _call(orch_prompt)
        except Exception as exc:
            raise OrchestrationValidationError(
                "llm_request_failed",
                f"LLM orchestration call failed: {exc}",
            ) from exc

        if (
            not _orch_raw_output
            or not isinstance(_orch_raw_output, str)
            or not _orch_raw_output.strip()
        ):
            import logging
            _logger = logging.getLogger(__name__)
            _logger.warning(
                "Orchestration LLM returned empty/non-string output. "
                "raw_type=%s, raw_len=%s, prompt_len=%s, intent=%s",
                type(_orch_raw_output).__name__ if _orch_raw_output is not None else "None",
                len(_orch_raw_output) if isinstance(_orch_raw_output, str) else "N/A",
                len(orch_prompt),
                parsed_request.get("intent"),
            )
            raise OrchestrationValidationError(
                "invalid_json",
                "LLM orchestration returned empty or non-string output",
            )

        # 7. Parse orchestration JSON — classify errors for retry decisions
        try:
            orch_candidate = _extract_json(_orch_raw_output)
        except (json.JSONDecodeError, ValueError) as exc:
            error_cat = _classify_orchestration_json_error(exc, _orch_raw_output)

            if error_cat == "truncated" and _orch_attempt < _MAX_ORCH_RETRIES:
                import logging
                _logger = logging.getLogger(__name__)
                _logger.warning(
                    "Orchestration JSON truncated (attempt %s/%s). "
                    "Retrying with compact prompt. "
                    "error_pos=%s, text_len=%s, scope_count=%s, intent=%s",
                    _orch_attempt + 1,
                    _MAX_ORCH_RETRIES + 1,
                    getattr(exc, "pos", "N/A") if isinstance(exc, json.JSONDecodeError) else "N/A",
                    len(_orch_raw_output),
                    len(scope_candidates),
                    parsed_request.get("intent"),
                )
                orch_prompt = _build_truncation_retry_prompt(orch_context)
                continue

            # Non-truncation parse failure (malformed JSON, empty, etc.)
            # or truncation on the final attempt — do NOT retry.
            raise OrchestrationValidationError(
                "invalid_json",
                f"Failed to parse orchestration JSON: {exc}",
            ) from exc

        # 8. Validate orchestration candidate against context
        try:
            validated = validate_orchestration_candidate(orch_candidate, orch_context)

            # 8.1. Revision constraint validation — after standard validation
            #      passes, verify that candidate satisfies modification constraints.
            _validate_revision_constraints(validated["items"], orch_context)

            break  # All validation (standard + revision) passed
        except OrchestrationValidationError as exc:
            _retryable = _orch_attempt < _MAX_ORCH_RETRIES

            # 8a. Clarification-with-scope → retry with anti-clarification
            if exc.code == "clarification_with_scope_candidates" and _retryable:
                import logging
                _logger = logging.getLogger(__name__)
                _logger.warning(
                    "Orchestration LLM returned clarification_needed=true "
                    "despite non-empty scope_candidates (attempt %s/%s). "
                    "Retrying with stronger prompt. "
                    "scope_refs=%s, intent=%s",
                    _orch_attempt + 1,
                    _MAX_ORCH_RETRIES + 1,
                    [sc.get("scope_ref") for sc in scope_candidates],
                    parsed_request.get("intent"),
                )
                orch_prompt = (
                    "!!! RETRY — YOUR PREVIOUS RESPONSE WAS REJECTED !!!\n"
                    "You set clarification_needed=true but scope_candidates "
                    "is NON-EMPTY — the learning scope has already been "
                    "resolved by the server. You MUST generate a plan with "
                    "items. Set clarification_needed=false and provide "
                    "a valid items list. DO NOT ask for clarification.\n\n"
                    + _build_orchestration_prompt(orch_context)
                )
                continue

            # 8b. Scope coverage incomplete → retry with specific missing contract
            if exc.code == "scope_coverage_incomplete" and _retryable:
                import logging
                _logger = logging.getLogger(__name__)
                _missing_refs = _compute_missing_scope_refs(
                    orch_candidate, orch_context,
                )
                _logger.warning(
                    "Orchestration LLM returned incomplete scope coverage "
                    "(attempt %s/%s). Retrying with missing-contract prompt. "
                    "scope_count=%s, missing=%s, intent=%s",
                    _orch_attempt + 1,
                    _MAX_ORCH_RETRIES + 1,
                    len(scope_candidates),
                    _missing_refs,
                    parsed_request.get("intent"),
                )
                orch_prompt = _build_content_retry_prompt(
                    orch_context,
                    missing_scope_refs=_missing_refs,
                )
                continue

            # 8c. Task-type composition incomplete → retry with specific contract
            if exc.code == "task_type_composition_incomplete" and _retryable:
                import logging
                _logger = logging.getLogger(__name__)
                _missing_types = _compute_missing_task_types(
                    orch_candidate, orch_context,
                )
                _logger.warning(
                    "Orchestration LLM returned incomplete task-type "
                    "composition (attempt %s/%s). Retrying with "
                    "missing-contract prompt. intent=%s, missing=%s",
                    _orch_attempt + 1,
                    _MAX_ORCH_RETRIES + 1,
                    parsed_request.get("intent"),
                    _missing_types,
                )
                orch_prompt = _build_content_retry_prompt(
                    orch_context,
                    missing_task_types=_missing_types,
                )
                continue

            # 8d. Practice dependency invalid → retry with specific feedback
            if exc.code == "practice_dependency_invalid" and _retryable:
                import logging
                _logger = logging.getLogger(__name__)
                _logger.warning(
                    "Orchestration LLM returned illegal practice dependency "
                    "(attempt %s/%s). Retrying with dependency-rules prompt. "
                    "error=%s, intent=%s",
                    _orch_attempt + 1,
                    _MAX_ORCH_RETRIES + 1,
                    str(exc),
                    parsed_request.get("intent"),
                )
                orch_prompt = _build_practice_dependency_retry_prompt(
                    orch_context,
                    validation_error=str(exc),
                )
                continue

            # 8e. Day/minute constraint violation → retry with exact bounds
            if exc.code in _ORCHESTRATION_SCHEDULE_ERROR_CODES and _retryable:
                import logging
                _logger = logging.getLogger(__name__)
                _logger.warning(
                    "Orchestration LLM violated schedule constraints "
                    "(attempt %s/%s). Retrying with exact bounds. "
                    "code=%s, error=%s, intent=%s",
                    _orch_attempt + 1,
                    _MAX_ORCH_RETRIES + 1,
                    exc.code,
                    str(exc),
                    parsed_request.get("intent"),
                )
                orch_prompt = _build_schedule_constraint_retry_prompt(
                    orch_context,
                    validation_error=str(exc),
                )
                continue

            # 8f. Revision constraint violation → retry with specific feedback
            if exc.code.startswith("revision_") and _retryable:
                import logging
                _logger = logging.getLogger(__name__)
                _logger.warning(
                    "Orchestration LLM violated revision constraints "
                    "(attempt %s/%s). Retrying with specific violation "
                    "feedback. code=%s, intent=%s",
                    _orch_attempt + 1,
                    _MAX_ORCH_RETRIES + 1,
                    exc.code,
                    parsed_request.get("intent"),
                )
                orch_prompt = _build_revision_constraint_retry_prompt(
                    orch_context,
                    validation_error=str(exc),
                )
                continue

            raise

    # 9. Handle clarification_needed
    if validated["clarification_needed"]:
        payload: dict[str, Any] = {
            "request": parsed_request,
            "items": [],
            "clarification_needed": True,
            "clarification_message": validated["clarification_message"],
        }
        draft_id = store.save(
            user_id=user_id,
            payload=payload,
            parent_plan_draft_id=parent_plan_draft_id,
        )
        return store.get(draft_id, user_id=user_id)

    # 10. Map validated orchestration items to draft items
    draft_items = _map_orchestration_items_to_draft_items(
        validated["items"], orch_context, parsed_request
    )

    # 10b. Preflight: every item must have a usable scope before persistence.
    #     Uses the real default resource inspector (KG nodes + evidence chunks).
    _pf = _preflight_draft_items(draft_items)
    if not _pf["ok"]:
        raise OrchestrationValidationError(
            "scope_preflight_failed",
            f"LLM-orchestrated plan preflight failed: "
            f"{_pf['failed']} item(s) have unusable scopes. "
            f"First failure: {_pf['failures'][0]['message']}",
        )

    # 11. Persist
    payload = {
        "request": parsed_request,
        "items": draft_items,
    }
    draft_id = store.save(
        user_id=user_id,
        payload=payload,
        parent_plan_draft_id=parent_plan_draft_id,
    )
    return store.get(draft_id, user_id=user_id)


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
    task_type: str = "topic_learning",
    learning_target: str | None = None,
    kg_node_ids: list[str] | None = None,
    learning_scope: dict[str, Any] | None = None,
    dependency: str | None = None,
    practice_level: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Create a plan item and record a lightweight plan event."""
    item = memory.create_study_plan_item(
        user_id=user_id,
        plan_date=plan_date,
        title=title,
        notes=notes,
        linked_course_id=linked_course_id,
        linked_course_title=linked_course_title,
        task_type=task_type,
        learning_target=learning_target,
        kg_node_ids=kg_node_ids,
        learning_scope=learning_scope,
        dependency=dependency,
        practice_level=practice_level,
        session_id=session_id,
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
    request_text: str | None = None,
    parent_plan_draft_id: str | None = None,
    draft_store: PlanDraftStore | None = None,
    chapter_provider: Callable[[], list[dict[str, Any]]] | None = None,
    llm_call: Callable[[str], str] | None = None,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return preview recommendations for one study-plan day.

    Two modes:

    * **Deterministic** (default / when *request_text* is ``None`` or empty):
      lightweight, no LLM, no KG, no side-effects.
    * **Custom draft** (when *request_text* is a non-empty string): parses
      the user's text, resolves KG chapter scope, persists a real plan draft,
      and returns cards mapped from the draft items.

    When *history* is provided, it is forwarded to the LLM parser so that
    multi-turn clarification conversations can resolve references to prior
    exchanges (e.g. a user answering "which textbook?" with just the book
    name).
    """
    target_date = _normalize_recommendation_date(plan_date)

    # ── custom draft branch ────────────────────────────────────────────
    if request_text is not None and request_text.strip():
        _store = (
            draft_store
            if draft_store is not None
            else _get_default_draft_store()
        )
        _chapters = (
            chapter_provider
            if chapter_provider is not None
            else _default_chapter_provider
        )
        return _recommend_custom_draft(
            user_id=user_id,
            plan_date=target_date,
            request_text=request_text.strip(),
            parent_plan_draft_id=parent_plan_draft_id,
            draft_store=_store,
            chapter_provider=_chapters,
            llm_call=llm_call,
            memory=memory,
            history=history,
        )

    # ── deterministic branch (unchanged) ───────────────────────────────
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
        "task_type": item.get("task_type"),
        "learning_target": item.get("learning_target"),
        "kg_node_ids": item.get("kg_node_ids", []),
        "deferred_count": item.get("deferred_count", 0),
        "dependency": item.get("dependency"),
        "practice_level": item.get("practice_level"),
        "session_id": item.get("session_id"),
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


def _normalize_course_topic(value: Any) -> str:
    """Remove conversational wrappers before persisting or reusing a topic."""
    text = _clean_text(value, limit=180)
    prefixes = ("我想要学习", "我想学习", "我想要了解", "我想了解", "请学习", "学习")
    changed = True
    while text and changed:
        changed = False
        for prefix in prefixes:
            if text.startswith(prefix) and len(text) > len(prefix):
                text = text[len(prefix):].lstrip(" ：:，,。")
                changed = True
                break
    return text


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
    task_type: str | None = None,
    learning_target: str | None = None,
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
        "task_type": _clean_text(task_type or category, limit=64),
        "learning_target": _clean_text(learning_target, limit=180) or None,
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
    topic = _normalize_course_topic(active_session.get("topic")) or "当前课程"
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
        learning_target=node_title,
        kg_node_ids=node.get("kg_node_ids") or [],
    )


def _recommend_weakness_reviews(
    *,
    profile: dict[str, Any],
    learner_memory: dict[str, Any],
    active_session: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    topic = _normalize_course_topic(active_session.get("topic")) if active_session else None

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
                learning_target=label,
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
                learning_target=label,
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
                    linked_course_title=_normalize_course_topic(session.get("topic")),
                    learning_target=label,
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
                task_type=item.get("task_type") or "topic_learning",
                learning_target=item.get("learning_target") or item.get("linked_course_title") or item.get("title"),
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
        focus_topic = _normalize_course_topic(session.get("topic"))
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
        learning_target=f"{focus_topic}错题回顾" if focus_topic else "当前主题错题回顾",
    )


def _fallback_recommendations(
    *,
    profile: dict[str, Any],
    active_session: dict[str, Any] | None,
    daily_minutes: int,
) -> list[dict[str, Any]]:
    topic = _normalize_course_topic(active_session.get("topic")) if active_session else None
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
            learning_target=f"{topic or '本周'}的关键知识点",
        ),
        _recommendation(
            recommendation_id="fallback:practice",
            title="安排一次 10 分钟自测回顾",
            category="practice",
            reason=f"画像目标当前偏向 {goal}，保留一个短时自测任务有助于形成稳定节奏。",
            priority=0.44,
            estimated_minutes=min(10, max(6, daily_minutes // 5)),
            linked_course_title=topic,
            learning_target=f"{topic}自测回顾" if topic else "基础知识自测回顾",
        ),
    ]
