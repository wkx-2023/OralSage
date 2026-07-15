"""Study-plan service for calendar to-do tracking and memory feedback."""

from __future__ import annotations

import copy
import json
import secrets
import threading
import time
from calendar import monthrange
from datetime import date, timedelta
from typing import Any, Callable

from .memory import LearningMemory
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


# ---------------------------------------------------------------------------
# Default LLM callable factory
# ---------------------------------------------------------------------------


def _make_default_llm_call() -> Callable[[str], str]:
    """Build a default LLM callable from the project's ``llm_client``.

    Returns a ``(prompt: str) -> str`` callable backed by
    :func:`backend.services.llm_client.chat_completion`.

    Raises
    ------
    LLMConfigurationError
        If no LLM API key is configured.
    """
    from backend.services.llm_client import chat_completion, is_llm_configured

    if not is_llm_configured():
        from backend.services.llm_client import LLMConfigurationError

        raise LLMConfigurationError("No LLM API key configured")

    def _call(prompt: str) -> str:
        return chat_completion(
            [{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=600,
        )

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


def _build_parse_prompt(message: str, candidates_text: str) -> str:
    """Build the LLM system+user prompt for parsing a plan request."""
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
        "5. If the user asks to review weaknesses, set intent to "
        '"review_weakness".\n'
        "6. If the user's request does not clearly map to any specific "
        'chapter or weakness, set intent to "custom".\n'
        "7. focus_text should capture the user's stated focus in their own "
        "words (e.g. the topic or key phrase they mentioned), or null if "
        "nothing specific was stated.\n"
        "8. Output ONLY the JSON object — no markdown fences, no extra text.\n"
        "\n"
        f"Available chapter candidates:\n{candidates_text}\n"
        "\n"
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
        # --- chapter_key is null → the other two must also be null --------
        if _bt is not None:
            raise PlanRequestParseError(
                "chapter_mismatch",
                f"book_title is {_bt!r} but chapter_key is null; "
                "chapter fields must be all-null or all-present from a candidate",
            )
        if _did is not None:
            raise PlanRequestParseError(
                "chapter_mismatch",
                f"document_id is {_did!r} but chapter_key is null; "
                "chapter fields must be all-null or all-present from a candidate",
            )

    # --- learn_chapter requires chapter_key -------------------------------
    if intent == "learn_chapter" and _ck is None:
        raise PlanRequestParseError(
            "missing_chapter_key",
            "learn_chapter intent requires a non-null chapter_key "
            "from the candidates",
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
) -> dict[str, Any]:
    """Parse a natural-language plan request into a structured request dict.

    This is a **pure internal service**.  It takes the user's raw message
    and the chapter candidates provided by the caller (e.g. the KG agent via
    the MasterRouter), calls an LLM to extract structured intent, and returns
    a dict that can be consumed directly by :func:`normalize_plan_request`.

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

    Returns
    -------
    dict
        A raw plan-request dict with the following keys, ready for
        :func:`normalize_plan_request`:

        * ``intent`` — one of the four allowed values
        * ``days`` — positive integer or ``None``
        * ``daily_minutes`` — positive integer or ``None``
        * ``focus_text`` — string or ``None``
        * ``book_title`` — verbatim from a candidate, or ``None``
        * ``document_id`` — verbatim from a candidate, or ``None``
        * ``chapter_key`` — verbatim from a candidate, or ``None``

    Raises
    ------
    PlanRequestParseError
        In any of these cases:

        * *message* is empty or whitespace-only
        * LLM is not configured and no *llm_call* was provided
        * The LLM request itself failed
        * The LLM output is not valid JSON
        * The ``intent`` field is missing or invalid
        * A field has an unexpected type
        * ``chapter_key`` does not match any candidate
        * ``book_title`` / ``document_id`` is inconsistent with the matched
          candidate, or is null when ``chapter_key`` is non-null
        * ``learn_chapter`` intent is used without a valid ``chapter_key``
        * The LLM output contains fields beyond the allowed set
        * :func:`normalize_plan_request` rejects the parsed output

        The exception carries a stable ``.code`` attribute (e.g.
        ``"invalid_json"``, ``"chapter_not_found"``) that callers can
        switch on without string-matching the message.
    """
    # ------------------------------------------------------------------
    # 1. Validate input
    # ------------------------------------------------------------------
    if not message or not str(message).strip():
        raise PlanRequestParseError(
            "empty_message", "message must be a non-empty string"
        )

    clean_message = str(message).strip()
    _chapters = [ch for ch in (chapters or []) if isinstance(ch, dict)]

    # ------------------------------------------------------------------
    # 2. Build the prompt
    # ------------------------------------------------------------------
    candidates_text = _build_chapter_candidate_text(_chapters)
    prompt = _build_parse_prompt(clean_message, candidates_text)

    # ------------------------------------------------------------------
    # 3. Resolve LLM callable
    # ------------------------------------------------------------------
    if llm_call is not None:
        _call = llm_call
    else:
        try:
            _call = _make_default_llm_call()
        except Exception as exc:
            # _make_default_llm_call raises LLMConfigurationError (imported
            # lazily).  Catch broadly so we never leak a raw exception type
            # that the caller doesn't import.
            raise PlanRequestParseError(
                "llm_not_configured",
                f"Failed to create default LLM callable: {exc}",
            ) from exc

    # ------------------------------------------------------------------
    # 4. Call LLM
    # ------------------------------------------------------------------
    try:
        raw_output = _call(prompt)
    except Exception as exc:
        # Distinguish configuration vs request errors when possible, but
        # always wrap in PlanRequestParseError so callers have one stable
        # exception type.
        exc_type_name = type(exc).__name__
        if "Config" in exc_type_name or "configured" in str(exc).lower():
            raise PlanRequestParseError(
                "llm_not_configured", f"LLM call failed: {exc}"
            ) from exc
        raise PlanRequestParseError(
            "llm_request_failed", f"LLM call failed: {exc}"
        ) from exc

    if not raw_output or not isinstance(raw_output, str) or not raw_output.strip():
        raise PlanRequestParseError(
            "invalid_json", "LLM returned empty or non-string output"
        )

    # ------------------------------------------------------------------
    # 5. Parse JSON
    # ------------------------------------------------------------------
    try:
        parsed = _extract_json(raw_output)
    except (json.JSONDecodeError, ValueError) as exc:
        raise PlanRequestParseError(
            "invalid_json", f"Failed to parse LLM output as JSON: {exc}"
        ) from exc

    # ------------------------------------------------------------------
    # 6. Validate fields
    # ------------------------------------------------------------------
    _validate_parsed_fields(parsed)

    # ------------------------------------------------------------------
    # 7. Validate chapter references against candidates
    # ------------------------------------------------------------------
    _validate_chapter_references(parsed, _chapters)

    # ------------------------------------------------------------------
    # 8. Normalize and return
    # ------------------------------------------------------------------
    raw_request = {
        "intent": str(parsed["intent"]).strip(),
        "days": parsed.get("days"),
        "daily_minutes": parsed.get("daily_minutes"),
        "focus_text": parsed.get("focus_text"),
        "book_title": parsed.get("book_title"),
        "document_id": parsed.get("document_id"),
        "chapter_key": parsed.get("chapter_key"),
    }
    try:
        return normalize_plan_request(raw_request)
    except ValueError as exc:
        raise PlanRequestParseError(
            "invalid_normalized_request", str(exc)
        ) from exc


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
        )
    except (PlanRequestParseError, KeyError, PermissionError, ValueError):
        raise
    except Exception as exc:
        raise RuntimeError(
            f"Failed to create custom plan draft: {exc}"
        ) from exc

    # 3. Map items → recommendation cards
    draft_id: str = draft["plan_draft_id"]
    items: list[dict[str, Any]] = draft["payload"]["items"]
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
            "parsed_intent": draft["payload"]["request"]["intent"],
        },
    }
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
    # 7. Return validated learning_scope
    # ------------------------------------------------------------------
    return {
        "book_title": resolved_book_title,
        "document_id": resolved_document_id,
        "chapter_key": key,
        "chapter_title": resolved_chapter_title,
        "kg_node_ids": kg_node_ids,
    }


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
# Combined service: natural-language → parsed request → scope → draft
# ---------------------------------------------------------------------------


def create_plan_draft_from_text(
    store: PlanDraftStore,
    *,
    user_id: str,
    message: str,
    chapters: list[dict[str, Any]] | None = None,
    llm_call: Callable[[str], str] | None = None,
    parent_plan_draft_id: str | None = None,
) -> dict[str, Any]:
    """Parse a natural-language plan request and create a plan draft in one call.

    This is a **pure composition service** that chains two existing internal
    capabilities:

    1. :func:`parse_plan_request_text` — LLM semantic parsing + field/chapter
       validation → a normalised ``request`` dict.
    2. :func:`create_custom_plan_draft` — scope resolution + persistence → a
       full draft record from :meth:`PlanDraftStore.get`.

    It does **not** duplicate parsing, scope validation, ID generation, TTL,
    or storage logic.  It does **not** call the MasterRouter, HTTP layer,
    database, or any downstream API.

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
        Injectable ``(prompt: str) -> str`` callable forwarded to
        :func:`parse_plan_request_text`.  When ``None``, a default callable
        is built from the project's LLM client.  Tests should inject a fake.
    parent_plan_draft_id:
        Optional lineage forwarded to :meth:`PlanDraftStore.save`.  The
        parent draft is never mutated.

    Returns
    -------
    dict
        The full draft record as returned by :meth:`PlanDraftStore.get`,
        containing ``plan_draft_id``, ``user_id``, ``payload.request``
        (normalised) and ``payload.items``.  Chapter-scoped items carry a
        real ``learning_scope`` resolved from *chapters*.

    Raises
    ------
    PlanRequestParseError
        From :func:`parse_plan_request_text` when the message cannot be
        parsed or validated (empty message, LLM failure, invalid JSON,
        fabricated chapter references, missing intent, etc.).
    ValueError
        From :func:`create_custom_plan_draft` when the normalised request
        is invalid, a required chapter scope cannot be resolved, or
        ``learn_chapter`` is used without a valid ``chapter_key``.
    KeyError, PermissionError
        From :class:`PlanDraftStore` when *parent_plan_draft_id* is
        unknown, cross-user, or expired.

    Notes
    -----
    Every error path is designed to leave the store untouched — no
    half-written draft is ever persisted on failure.
    """
    # 1. Parse natural-language message → normalised request dict
    parsed_request = parse_plan_request_text(
        message,
        chapters=chapters,
        llm_call=llm_call,
    )

    # 2. Resolve scope, build items, persist → full draft record
    return create_custom_plan_draft(
        store,
        user_id=user_id,
        request=parsed_request,
        chapters=chapters,
        parent_plan_draft_id=parent_plan_draft_id,
    )


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
) -> dict[str, Any]:
    """Return preview recommendations for one study-plan day.

    Two modes:

    * **Deterministic** (default / when *request_text* is ``None`` or empty):
      lightweight, no LLM, no KG, no side-effects.
    * **Custom draft** (when *request_text* is a non-empty string): parses
      the user's text, resolves KG chapter scope, persists a real plan draft,
      and returns cards mapped from the draft items.
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
