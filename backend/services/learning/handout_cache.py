"""Handout Section Cache — file-based persistence layer.

Stores ``generate_handout_section`` results on disk so that subsequent
reads (Tutor, QA) do not trigger re-generation.  Each cache record is a
single JSON file keyed by ``session_id`` + ``outline_node_id``.

Public API
----------
* ``get_handout_cache`` — read one cache record (returns ``missing`` on miss)
* ``save_handout_cache_pending`` — mark a node as pending
* ``save_handout_cache_ready`` — persist a successfully generated section
* ``save_handout_cache_failed`` — persist a failed generation with error
* ``list_handout_cache_status`` — summary table of all nodes for a session
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cache_dir(
    session_id: str,
    resource_root: str | Path | None = None,
) -> Path:
    root = Path(resource_root) if resource_root else Path("data/learning/sessions")
    return root / session_id / "handout_sections"


def _cache_path(
    session_id: str,
    outline_node_id: str,
    resource_root: str | Path | None = None,
) -> Path:
    return _cache_dir(session_id, resource_root) / f"{outline_node_id}.json"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_handout_cache(
    session_id: str,
    outline_node_id: str,
    resource_root: str | Path | None = None,
) -> dict[str, Any]:
    """Read a handout cache record from disk.

    Returns the full cache record on hit, or ``{"status": "missing"}`` on
    miss / file-not-found / corrupted JSON.
    """
    path = _cache_path(session_id, outline_node_id, resource_root)
    if not path.exists():
        return {"status": "missing"}

    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as exc:
        return {"status": "missing", "error": f"read_error: {exc}"}

    try:
        record = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {"status": "missing", "error": f"corrupted_json: {exc}"}

    if not isinstance(record, dict):
        return {"status": "missing", "error": "not_a_dict"}

    return record


def save_handout_cache_pending(
    session_id: str,
    outline_node_id: str,
    resource_root: str | Path | None = None,
) -> dict[str, Any]:
    """Mark an outline node as ``pending`` (about to be generated)."""
    now = _now()
    record: dict[str, Any] = {
        "session_id": session_id,
        "outline_node_id": outline_node_id,
        "status": "pending",
        "section_status": "",
        "handout_section": {},
        "created_at": now,
        "updated_at": now,
        "error": None,
        "trace": {
            "source": "",
            "used_json_mode": False,
            "fallback_rule_based": False,
        },
    }
    _write_record(session_id, outline_node_id, record, resource_root)
    return record


def save_handout_cache_ready(
    session_id: str,
    outline_node_id: str,
    handout_section: dict[str, Any],
    source: str,
    resource_root: str | Path | None = None,
) -> dict[str, Any]:
    """Persist a successfully generated handout section.

    Parameters
    ----------
    source:
        One of ``"first_section_sync"``, ``"background_prefetch"``,
        or ``"cache_miss_sync_generation"``.
    """
    now = _now()
    section_status = handout_section.get("section_status", "llm_generated")
    _trace = handout_section.get("_trace", {})
    used_json_mode = bool(_trace.get("used_json_mode", False))

    record: dict[str, Any] = {
        "session_id": session_id,
        "outline_node_id": outline_node_id,
        "status": "ready",
        "section_status": section_status,
        "handout_section": handout_section,
        "created_at": now,
        "updated_at": now,
        "error": None,
        "trace": {
            "source": source,
            "used_json_mode": used_json_mode,
            "fallback_rule_based": False,
        },
    }
    _write_record(session_id, outline_node_id, record, resource_root)
    return record


def save_handout_cache_failed(
    session_id: str,
    outline_node_id: str,
    error: str,
    source: str,
    resource_root: str | Path | None = None,
) -> dict[str, Any]:
    """Persist a failed handout generation with the error reason.

    Parameters
    ----------
    source:
        One of ``"first_section_sync"``, ``"background_prefetch"``,
        or ``"cache_miss_sync_generation"``.
    """
    now = _now()
    record: dict[str, Any] = {
        "session_id": session_id,
        "outline_node_id": outline_node_id,
        "status": "failed",
        "section_status": "failed",
        "handout_section": {},
        "created_at": now,
        "updated_at": now,
        "error": error,
        "trace": {
            "source": source,
            "used_json_mode": False,
            "fallback_rule_based": False,
        },
    }
    _write_record(session_id, outline_node_id, record, resource_root)
    return record


def list_handout_cache_status(
    session_id: str,
    resource_root: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Return a summary table of all cached handout sections for a session.

    Each row is a lightweight dict suitable for display:
    ``outline_node_id``, ``title``, ``status``, ``section_status``,
    ``source``, ``error_short``.
    """
    cdir = _cache_dir(session_id, resource_root)
    if not cdir.exists():
        return []

    rows: list[dict[str, Any]] = []
    try:
        files = sorted(cdir.glob("*.json"))
    except Exception:
        return []

    for fp in files:
        node_id = fp.stem
        try:
            record = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            rows.append({
                "outline_node_id": node_id,
                "title": "(corrupted)",
                "status": "missing",
                "section_status": "",
                "source": "",
                "error_short": "file_read_or_parse_error",
            })
            continue

        if not isinstance(record, dict):
            rows.append({
                "outline_node_id": node_id,
                "title": "(invalid)",
                "status": "missing",
                "section_status": "",
                "source": "",
                "error_short": "not_a_dict",
            })
            continue

        hs = record.get("handout_section", {}) or {}
        title = hs.get("title", "") or "(no title)"
        err = record.get("error", "") or ""
        rows.append({
            "outline_node_id": record.get("outline_node_id", node_id),
            "title": title[:60],
            "status": record.get("status", "unknown"),
            "section_status": record.get("section_status", ""),
            "source": (record.get("trace", {}) or {}).get("source", ""),
            "error_short": err[:80] if err else "",
        })

    return rows


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _write_record(
    session_id: str,
    outline_node_id: str,
    record: dict[str, Any],
    resource_root: str | Path | None = None,
) -> None:
    """Write a cache record to disk, creating parent directories as needed."""
    path = _cache_path(session_id, outline_node_id, resource_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Use a temp file + rename for atomic writes
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(record, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(str(tmp), str(path))
