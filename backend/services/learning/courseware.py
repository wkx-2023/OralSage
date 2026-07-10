"""Build aligned slides, narration, audio references, and PPTX resources."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .audio import get_audio_manifest, reconcile_audio_manifest
from .lecture_script import get_lecture_script


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _root(resource_root: str | Path | None) -> Path:
    return Path(resource_root) if resource_root else Path("data/learning/sessions")


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(str(tmp_path), str(path))


def _display_bullets(narration: str, limit: int = 4) -> list[str]:
    sentences = [
        item.strip(" ，。；：")
        for item in re.split(r"[。！？；]+", str(narration or ""))
        if item.strip(" ，。；：")
    ]
    bullets: list[str] = []
    for sentence in sentences:
        if len(sentence) > 86:
            sentence = sentence[:83].rstrip("，、 ") + "..."
        bullets.append(sentence)
        if len(bullets) >= limit:
            break
    return bullets or ["本页内容请结合讲稿学习。"]


def build_slides_from_lecture_scripts(
    *,
    session_id: str,
    outline: dict[str, Any],
    resource_root: str | Path | None = None,
) -> dict[str, Any]:
    """Aggregate all ready lecture scripts into an aligned ``slides.v1`` deck."""
    nodes = [node for node in outline.get("nodes", []) if node.get("id")]
    audio_manifest = get_audio_manifest(session_id, resource_root)
    audio_sections = {
        section.get("outline_node_id"): section
        for section in audio_manifest.get("sections", [])
        if isinstance(section, dict)
    }

    slides: list[dict[str, Any]] = []
    ready_node_ids: list[str] = []
    missing_node_ids: list[str] = []
    for section_index, node in enumerate(nodes, start=1):
        node_id = str(node["id"])
        script = get_lecture_script(session_id, node_id, resource_root)
        if script.get("status") != "ready" or not script.get("slides"):
            missing_node_ids.append(node_id)
            continue
        ready_node_ids.append(node_id)

        audio_by_slide = {
            item.get("slide_id"): item
            for item in audio_sections.get(node_id, {}).get("slides", [])
            if isinstance(item, dict)
        }
        for script_slide in script["slides"]:
            local_slide_id = str(script_slide.get("slide_id", ""))
            slide_id = f"{node_id}_{local_slide_id}"
            narration = str(script_slide.get("narration") or script_slide.get("text") or "")
            audio = audio_by_slide.get(local_slide_id, {})
            kind = str(script_slide.get("kind") or "content")
            bullets = _display_bullets(narration)
            slides.append({
                "slide_id": slide_id,
                "outline_node_id": node_id,
                "section_index": section_index,
                "slide_index_in_section": int(script_slide.get("index", 0) or 0),
                "slide_type": kind,
                "title": str(script_slide.get("title") or node.get("title") or ""),
                "section_title": str(node.get("title") or script.get("title") or ""),
                "bullets": bullets,
                "content": {
                    "kind": kind,
                    "source_path": script_slide.get("source_path", ""),
                },
                "blocks": [
                    {
                        "block_id": f"{slide_id}_content",
                        "block_type": "bullet_list",
                        "semantic_role": kind,
                        "items": bullets,
                    }
                ],
                "layout_plan": {
                    "template_id": "pin-and-paper",
                    "page_type": kind,
                    "density": "standard",
                },
                "narration_ref": {
                    "source": "lecture_script.narration",
                    "outline_node_id": node_id,
                    "slide_id": local_slide_id,
                    "script_hash": script_slide.get("script_hash", ""),
                    "text": narration,
                },
                "audio_ref": {
                    "status": audio.get("status", "missing"),
                    "audio_file": audio.get("audio_file", ""),
                    "text_hash": audio.get("text_hash", ""),
                },
                "evidence_refs": list(script_slide.get("evidence_refs", []) or []),
            })

    expected_node_ids = [str(node["id"]) for node in nodes]
    status = "ready" if expected_node_ids and not missing_node_ids else (
        "partial" if ready_node_ids else "failed"
    )
    return {
        "schema_version": "slides.v1",
        "deck_id": f"deck_{session_id}",
        "session_id": session_id,
        "deck_title": str(outline.get("title") or "OralSage 学习课程"),
        "theme_id": "oralsage_clinical_light",
        "status": status,
        "source": {
            "type": "handout_sections+lecture_scripts+audio_manifest",
            "granularity": "one_slide_per_lecture_script_page",
        },
        "expected_node_ids": expected_node_ids,
        "ready_node_ids": ready_node_ids,
        "missing_node_ids": missing_node_ids,
        "expected_section_count": len(expected_node_ids),
        "ready_section_count": len(ready_node_ids),
        "slide_count": len(slides),
        "slides": slides,
        "warnings": [
            f"missing_lecture_script: {node_id}" for node_id in missing_node_ids
        ],
        "updated_at": _now(),
    }


def save_slides_deck(
    deck: dict[str, Any],
    resource_root: str | Path | None = None,
) -> Path:
    root = _root(resource_root)
    output_dir = root / str(deck["session_id"]) / "slides"
    output_path = output_dir / "slides.v1.json"
    output_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(deck, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp_path), str(output_path))
    return output_path


def build_and_save_pin_and_paper_plan(
    deck: dict[str, Any],
    resource_root: str | Path | None = None,
) -> tuple[dict[str, Any], Path]:
    """Persist the deterministic Pin & Paper layout contract used by export."""
    plan = {
        "schema_version": "pin_and_paper_slide_plan.v1",
        "template_slug": "pin-and-paper",
        "deck_title": deck.get("deck_title", ""),
        "session_id": deck.get("session_id", ""),
        "planning_mode": "deterministic_courseware_layout",
        "slides": [
            {
                "source_slide_id": slide.get("slide_id", ""),
                "source_outline_node_id": slide.get("outline_node_id", ""),
                "page_type": slide.get("layout_plan", {}).get(
                    "page_type", slide.get("slide_type", "content")
                ),
                "rationale": "Use the stable clinical-light layout and preserve aligned narration.",
                "slots": {
                    "section_title": slide.get("section_title", ""),
                    "title": slide.get("title", ""),
                    "body_items": slide.get("bullets", []),
                    "evidence_count": len(slide.get("evidence_refs", [])),
                },
                "content_warnings": [],
            }
            for slide in deck.get("slides", [])
        ],
        "warnings": list(deck.get("warnings", [])),
    }
    output_dir = _root(resource_root) / str(deck["session_id"]) / "slides"
    output_path = output_dir / "pin_and_paper_plan.v1.json"
    output_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp_path), str(output_path))
    return plan, output_path


def _validate_pptx(path: Path, expected_slide_count: int) -> dict[str, Any]:
    if not path.exists() or path.stat().st_size == 0:
        return {"ok": False, "error": "pptx_missing_or_empty"}
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            corrupt_entry = archive.testzip()
    except Exception as exc:
        return {"ok": False, "error": f"pptx_zip_invalid: {exc}"}
    required = {"[Content_Types].xml", "ppt/presentation.xml"}
    missing = sorted(required - names)
    slide_xml_count = len([
        name for name in names
        if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
    ])
    notes_xml_count = len([
        name for name in names
        if re.fullmatch(r"ppt/notesSlides/notesSlide\d+\.xml", name)
    ])
    expected_with_cover = expected_slide_count + 1
    if missing or corrupt_entry or slide_xml_count != expected_with_cover or notes_xml_count < expected_slide_count:
        return {
            "ok": False,
            "error": "pptx_structure_mismatch",
            "missing_entries": missing,
            "slide_xml_count": slide_xml_count,
            "expected_slide_xml_count": expected_with_cover,
            "notes_xml_count": notes_xml_count,
            "minimum_notes_xml_count": expected_slide_count,
            "corrupt_entry": corrupt_entry,
        }
    return {
        "ok": True,
        "byte_size": path.stat().st_size,
        "slide_count": slide_xml_count,
        "notes_count": notes_xml_count,
    }


def export_pptx(
    *,
    deck: dict[str, Any],
    slides_path: Path,
    resource_root: str | Path | None = None,
) -> dict[str, Any]:
    """Export a complete slides deck through the bundled PptxGenJS renderer."""
    root = _root(resource_root)
    session_dir = root / str(deck["session_id"])
    status_path = session_dir / "pptx_status.json"
    output_path = session_dir / "lesson.pptx"

    base_status = {
        "schema_version": "learning.pptx_status.v1",
        "session_id": deck["session_id"],
        "slides_path": str(slides_path),
        "expected_section_count": deck.get("expected_section_count", 0),
        "ready_section_count": deck.get("ready_section_count", 0),
        "missing_section_ids": deck.get("missing_node_ids", []),
        "generated_slide_count": deck.get("slide_count", 0),
        "updated_at": _now(),
    }

    if deck.get("status") != "ready":
        output_path.unlink(missing_ok=True)
        status = {
            **base_status,
            "ok": False,
            "status": "partial",
            "pptx_path": "",
            "error": "incomplete_lecture_scripts",
        }
        _write_json_atomic(status_path, status)
        return status

    node = shutil.which("node")
    exporter = Path(__file__).resolve().parents[3] / "tools" / "ppt_export" / "export.mjs"
    if not node or not exporter.exists():
        output_path.unlink(missing_ok=True)
        status = {
            **base_status,
            "ok": False,
            "status": "failed",
            "pptx_path": str(output_path),
            "error": "pptx_export_runtime_missing",
        }
        _write_json_atomic(status_path, status)
        return status

    pending_status = {
        **base_status,
        "ok": False,
        "status": "generating",
        "pptx_path": "",
        "error": None,
    }
    _write_json_atomic(status_path, pending_status)
    output_path.unlink(missing_ok=True)

    command = [node, str(exporter), str(slides_path), str(output_path)]
    try:
        completed = subprocess.run(
            command,
            cwd=str(Path(__file__).resolve().parents[3]),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
        )
    except Exception as exc:
        completed = None
        export_error = f"pptx_export_raised: {exc}"
    else:
        export_error = (
            f"pptx_export_exit_{completed.returncode}: {completed.stderr[-500:]}"
            if completed.returncode != 0 else ""
        )

    validation = _validate_pptx(output_path, int(deck.get("slide_count", 0)))
    ok = not export_error and validation.get("ok") is True
    status = {
        **base_status,
        "ok": ok,
        "status": "ready" if ok else "failed",
        "pptx_path": str(output_path),
        "error": None if ok else (export_error or validation.get("error")),
        "validation": validation,
    }
    if not ok:
        output_path.unlink(missing_ok=True)
        status["pptx_path"] = ""
    _write_json_atomic(status_path, status)
    return status


def build_courseware_resources(
    *,
    session_id: str,
    outline: dict[str, Any],
    resource_root: str | Path | None = None,
) -> dict[str, Any]:
    nodes = list(outline.get("nodes", []))
    audio_manifest = reconcile_audio_manifest(
        session_id=session_id,
        outline_nodes=nodes,
        resource_root=resource_root,
    )
    deck = build_slides_from_lecture_scripts(
        session_id=session_id,
        outline=outline,
        resource_root=resource_root,
    )
    slides_path = save_slides_deck(deck, resource_root)
    pin_and_paper_plan, pin_and_paper_plan_path = build_and_save_pin_and_paper_plan(
        deck,
        resource_root,
    )
    pptx_status = export_pptx(
        deck=deck,
        slides_path=slides_path,
        resource_root=resource_root,
    )
    overall_ready = all((
        deck.get("status") == "ready",
        pptx_status.get("ok") is True,
        audio_manifest.get("status") == "ready",
    ))
    result = {
        "status": "ready" if overall_ready else "partial",
        "slides": deck,
        "slides_path": str(slides_path),
        "pin_and_paper_plan": pin_and_paper_plan,
        "pin_and_paper_plan_path": str(pin_and_paper_plan_path),
        "audio_manifest": audio_manifest,
        "pptx_status": pptx_status,
    }
    status = get_courseware_status(
        session_id=session_id,
        outline=outline,
        resource_root=resource_root,
    )
    manifest = {
        "schema_version": "learning.generation_manifest.v1",
        "session_id": session_id,
        **status,
        "updated_at": _now(),
    }
    manifest_path = _root(resource_root) / session_id / "generation_manifest.json"
    tmp_path = manifest_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp_path), str(manifest_path))
    result["generation_manifest"] = manifest
    result["generation_manifest_path"] = str(manifest_path)
    return result


def get_courseware_status(
    *,
    session_id: str,
    outline: dict[str, Any],
    resource_root: str | Path | None = None,
) -> dict[str, Any]:
    """Read a truthful completeness summary without starting generation."""
    from .handout_cache import get_handout_cache

    nodes = [node for node in outline.get("nodes", []) if node.get("id")]
    expected_ids = [str(node["id"]) for node in nodes]
    handout_ready = [
        node_id for node_id in expected_ids
        if get_handout_cache(session_id, node_id, resource_root).get("status") == "ready"
    ]
    script_ready = [
        node_id for node_id in expected_ids
        if get_lecture_script(session_id, node_id, resource_root).get("status") == "ready"
    ]
    audio = get_audio_manifest(session_id, resource_root)
    session_dir = _root(resource_root) / session_id

    def read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
        if not path.exists():
            return default
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {**default, "status": "failed", "error": f"read_failed: {exc}"}
        return value if isinstance(value, dict) else default

    slides = read_json(
        session_dir / "slides" / "slides.v1.json",
        {"status": "missing", "slide_count": 0, "missing_node_ids": expected_ids},
    )
    pptx = read_json(
        session_dir / "pptx_status.json",
        {"status": "missing", "ok": False, "pptx_path": ""},
    )
    complete = bool(expected_ids) and all((
        len(handout_ready) == len(expected_ids),
        len(script_ready) == len(expected_ids),
        audio.get("status") == "ready",
        slides.get("status") == "ready",
        pptx.get("status") == "ready" and pptx.get("ok") is True,
    ))
    any_ready = bool(handout_ready or script_ready or slides.get("slide_count"))
    return {
        "status": "ready" if complete else ("partial" if any_ready else "pending"),
        "expected_section_count": len(expected_ids),
        "handout": {
            "ready_count": len(handout_ready),
            "missing_node_ids": [node_id for node_id in expected_ids if node_id not in handout_ready],
        },
        "lecture_script": {
            "ready_count": len(script_ready),
            "missing_node_ids": [node_id for node_id in expected_ids if node_id not in script_ready],
        },
        "audio": {
            "status": audio.get("status", "missing"),
            "ready_section_count": audio.get("ready_section_count", 0),
            "missing_section_ids": audio.get("missing_section_ids", expected_ids),
            "failed_section_ids": audio.get("failed_section_ids", []),
        },
        "slides": {
            "status": slides.get("status", "missing"),
            "slide_count": slides.get("slide_count", len(slides.get("slides", []))),
            "missing_node_ids": slides.get("missing_node_ids", expected_ids),
        },
        "pptx": {
            "status": pptx.get("status", "missing"),
            "ok": pptx.get("ok", False),
            "pptx_path": pptx.get("pptx_path", ""),
            "error": pptx.get("error"),
        },
    }
