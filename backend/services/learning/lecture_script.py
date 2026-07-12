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


def _split_teaching_text(text: Any, max_chars: int = 850) -> list[str]:
    value = str(text or "").strip()
    if not value:
        return []
    paragraphs = [
        item.strip()
        for item in re.split(r"\n\s*\n+", value)
        if item.strip()
    ]
    if not paragraphs:
        paragraphs = [value]

    chunks: list[str] = []
    for paragraph in paragraphs:
        cleaned = _clean_text(paragraph)
        if not cleaned:
            continue
        if len(cleaned) <= max_chars:
            chunks.append(cleaned)
            continue

        sentences = [
            item.strip()
            for item in re.findall(r".+?(?:[。！？!?；;]|$)", cleaned)
            if item.strip()
        ]
        current = ""
        for sentence in sentences or [cleaned]:
            if not current:
                current = sentence
                continue
            if len(current) + len(sentence) <= max_chars:
                current += sentence
            else:
                chunks.append(current)
                current = sentence
        if current:
            chunks.append(current)
    return chunks


def build_tutor_lecture_script(
    *,
    session_id: str,
    outline_node_id: str,
    tutor_step: dict[str, Any],
    title: str = "",
) -> dict[str, Any]:
    teaching_text = str(tutor_step.get("teaching_text") or "").strip()
    section_title = str(title or tutor_step.get("_handout_section_title") or "").strip()
    chunks = _split_teaching_text(teaching_text)
    slides: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks, start=1):
        slides.append({
            "slide_id": f"slide_{index:03d}",
            "index": index,
            "kind": "tutor_teaching_text",
            "title": f"{section_title or 'Tutor'} {index}",
            "narration": chunk,
            "text": chunk,
            "text_chars": len(chunk),
            "text_hash": _short_hash(chunk),
            "script_hash": _short_hash(chunk),
            "source_path": "tutor_step.teaching_text",
            "evidence_refs": [],
        })

    source_payload = json.dumps(
        {
            "outline_node_id": outline_node_id,
            "teaching_text": teaching_text,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    source_hash = _short_hash(source_payload)
    return {
        "schema_version": "learning.lecture_script.v1",
        "session_id": session_id,
        "outline_node_id": outline_node_id,
        "title": section_title,
        "status": "ready" if slides else "failed",
        "source": "tutor_step.teaching_text",
        # Kept for the existing audio manifest cache comparison.
        "source_handout_hash": source_hash,
        "source_tutor_hash": source_hash,
        "slide_count": len(slides),
        "slides": slides,
        "updated_at": _now(),
        "error": None if slides else "tutor_step_has_no_teaching_text",
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


def build_and_save_tutor_lecture_script(
    *,
    session_id: str,
    outline_node_id: str,
    tutor_step: dict[str, Any],
    title: str = "",
    resource_root: str | Path | None = None,
) -> dict[str, Any]:
    script = build_tutor_lecture_script(
        session_id=session_id,
        outline_node_id=outline_node_id,
        tutor_step=tutor_step,
        title=title,
    )
    existing = get_lecture_script(session_id, outline_node_id, resource_root)
    if (
        existing.get("status") == "ready"
        and existing.get("source") == "tutor_step.teaching_text"
        and existing.get("source_tutor_hash") == script.get("source_tutor_hash")
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
