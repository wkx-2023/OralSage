"""Stable per-slide lecture scripts derived from generated handout sections."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(text: Any) -> str:
    value = str(text or "").replace("\t", " ")
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)
    value = re.sub(r"<[^>]+>", "", value)
    value = re.sub(r"[`*_>#\[\]{}|]", "", value)
    return re.sub(r"\s+", " ", value).strip()


def _short_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def build_lecture_script(
    *,
    session_id: str,
    outline_node_id: str,
    handout_section: dict[str, Any],
) -> dict[str, Any]:
    """Build a deterministic narration script with one record per page."""
    content = handout_section.get("content", {}) or {}
    section_title = str(handout_section.get("title") or "").strip()
    slides: list[dict[str, Any]] = []

    def add(
        kind: str,
        title: str,
        narration: Any,
        source_path: str,
        evidence_refs: list[Any] | None = None,
    ) -> None:
        cleaned = _clean_text(narration)
        if not cleaned:
            return
        index = len(slides) + 1
        refs = [str(ref) for ref in (evidence_refs or []) if str(ref)]
        slides.append({
            "slide_id": f"slide_{index:03d}",
            "index": index,
            "kind": kind,
            "title": str(title or section_title or f"第{index}页").strip(),
            "narration": cleaned,
            # Backward-compatible alias consumed by the existing audio layer.
            "text": cleaned,
            "text_chars": len(cleaned),
            "text_hash": _short_hash(cleaned),
            "script_hash": _short_hash(cleaned),
            "source_path": source_path,
            "evidence_refs": refs,
        })

    add(
        "overview",
        f"{section_title}：概述" if section_title else "概述",
        content.get("overview", ""),
        "content.overview",
    )

    key_points = content.get("key_points", [])
    if isinstance(key_points, list):
        for index, item in enumerate(key_points, start=1):
            if not isinstance(item, dict):
                continue
            heading = str(item.get("heading") or f"要点{index}").strip()
            explanation = str(item.get("explanation") or "").strip()
            add(
                "key_point",
                heading,
                f"要点{index}：{heading}。{explanation}",
                f"content.key_points[{index - 1}]",
                item.get("evidence_refs", []),
            )

    add(
        "clinical_example",
        "临床例子",
        content.get("clinical_example", ""),
        "content.clinical_example",
    )

    confusions = content.get("common_confusions", [])
    if isinstance(confusions, list):
        for index, item in enumerate(confusions, start=1):
            if not isinstance(item, dict):
                continue
            confusion = str(item.get("confusion") or "").strip()
            clarification = str(item.get("clarification") or "").strip()
            add(
                "common_confusion",
                confusion or f"易混点{index}",
                f"常见误区：{confusion}。正确理解：{clarification}",
                f"content.common_confusions[{index - 1}]",
                item.get("evidence_refs", []),
            )

    add(
        "transition",
        "过渡到下一节",
        content.get("transition_to_next", ""),
        "content.transition_to_next",
    )

    source_payload = json.dumps(
        handout_section,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "schema_version": "learning.lecture_script.v1",
        "session_id": session_id,
        "outline_node_id": outline_node_id,
        "title": section_title,
        "status": "ready" if slides else "failed",
        "source_handout_hash": _short_hash(source_payload),
        "slide_count": len(slides),
        "slides": slides,
        "updated_at": _now(),
        "error": None if slides else "handout_has_no_narratable_content",
    }


def save_lecture_script(
    script: dict[str, Any],
    resource_root: str | Path | None = None,
) -> Path:
    root = Path(resource_root) if resource_root else Path("data/learning/sessions")
    output_dir = root / str(script["session_id"]) / "lecture_scripts"
    output_path = output_dir / f"{script['outline_node_id']}.json"
    output_dir.mkdir(parents=True, exist_ok=True)
    descriptor, tmp_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.",
        suffix=".tmp",
        dir=str(output_dir),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(script, stream, ensure_ascii=False, indent=2)
        for attempt in range(6):
            try:
                os.replace(str(tmp_path), str(output_path))
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.01 * (2 ** attempt))
    finally:
        tmp_path.unlink(missing_ok=True)
    return output_path


def build_and_save_lecture_script(
    *,
    session_id: str,
    outline_node_id: str,
    handout_section: dict[str, Any],
    resource_root: str | Path | None = None,
) -> dict[str, Any]:
    script = build_lecture_script(
        session_id=session_id,
        outline_node_id=outline_node_id,
        handout_section=handout_section,
    )
    existing = get_lecture_script(session_id, outline_node_id, resource_root)
    if (
        existing.get("status") == "ready"
        and existing.get("source_handout_hash") == script.get("source_handout_hash")
    ):
        root = Path(resource_root) if resource_root else Path("data/learning/sessions")
        output_path = root / session_id / "lecture_scripts" / f"{outline_node_id}.json"
        return {**existing, "script_path": str(output_path)}
    output_path = save_lecture_script(script, resource_root)
    return {**script, "script_path": str(output_path)}


def get_lecture_script(
    session_id: str,
    outline_node_id: str,
    resource_root: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(resource_root) if resource_root else Path("data/learning/sessions")
    path = root / session_id / "lecture_scripts" / f"{outline_node_id}.json"
    if not path.exists():
        return {"status": "missing", "slides": []}
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "failed", "slides": [], "error": f"read_failed: {exc}"}
    return result if isinstance(result, dict) else {"status": "failed", "slides": []}
