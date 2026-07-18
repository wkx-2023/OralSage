"""User-facing text cleanup that preserves structured evidence metadata."""

from __future__ import annotations

import re
from typing import Any


_INTERNAL_ID_TOKEN = (
    r"(?:chunk(?:_id)?|core|chapter|node|scope(?:_ref)?|document|kg)"
    r"\s*[.:=]\s*[A-Za-z0-9][A-Za-z0-9._:/-]*"
)
_BRACKETED_INTERNAL_IDS = re.compile(
    rf"[\(\[（【]\s*{_INTERNAL_ID_TOKEN}"
    rf"(?:\s*[,，;；/]\s*{_INTERNAL_ID_TOKEN})*\s*[\)\]）】]",
    re.IGNORECASE,
)
_INTERNAL_ID = re.compile(_INTERNAL_ID_TOKEN, re.IGNORECASE)
_EMPTY_METADATA_BRACKETS = re.compile(
    r"[\(\[（【]\s*(?:(?:来源|证据|依据|引用|source|id)\s*[:：]?)?\s*[\)\]）】]",
    re.IGNORECASE,
)


def sanitize_display_text(value: Any) -> str:
    """Remove internal storage identifiers from text shown to end users.

    Structured ``citations`` and ``used_evidence`` objects are intentionally
    untouched; callers apply this function only to their visible answer field.
    """
    text = str(value or "")
    if not text:
        return ""
    text = _BRACKETED_INTERNAL_IDS.sub("", text)
    text = _INTERNAL_ID.sub("", text)
    text = _EMPTY_METADATA_BRACKETS.sub("", text)
    text = re.sub(r"(?m)^[ \t]*(?:[-*+]|\d+[.)、])\s*$\n?", "", text)
    text = re.sub(r"[ \t]+([，。；：,.!?])", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
