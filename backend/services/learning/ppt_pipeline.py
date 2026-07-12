"""Runtime bridge for the formal Slides and Pin & Paper PPT pipeline.

This module coordinates existing production components.  Medical content is
owned by handout caches, slide structure by ``slides.py``, and page planning by
``pin_and_paper_planner.py``.  The exporter only captures the canonical HTML
renderer and packages those rendered pages into a PPTX file.
"""

from __future__ import annotations

import functools
import hashlib
import http.server
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode, urlparse

from .pin_and_paper_planner import load_slide_types
from .pin_and_paper_real_llm_client import (
    create_real_llm_client,
    plan_deck_concurrent,
)
from .resource_factory import generate_slides_resource_from_session_cache
from .tutor_cache import get_tutor_cache


_REPO_ROOT = Path(__file__).resolve().parents[3]
_TEMPLATE_DIR = _REPO_ROOT / "tools" / "ppt_preview" / "templates" / "pin-and-paper"
_EXPORTER_PATH = _REPO_ROOT / "tools" / "ppt_export" / "export.mjs"
_LOCKS_GUARD = threading.Lock()
_SESSION_LOCKS: dict[str, threading.RLock] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _root(resource_root: str | Path | None) -> Path:
    return Path(resource_root) if resource_root else Path("data/learning/sessions")


def _session_lock(session_id: str) -> threading.RLock:
    with _LOCKS_GUARD:
        return _SESSION_LOCKS.setdefault(session_id, threading.RLock())


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
        for attempt in range(6):
            try:
                os.replace(str(tmp), str(path))
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.01 * (2 ** attempt))
    finally:
        tmp.unlink(missing_ok=True)


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {**default, "status": "failed", "error": f"read_failed: {exc}"}
    return value if isinstance(value, dict) else default


def _source_hash(value: dict[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _expected_node_ids(outline: dict[str, Any]) -> list[str]:
    return [
        str(node.get("id"))
        for node in outline.get("nodes", [])
        if str(node.get("id") or "")
    ]


def _build_cover_plan(
    deck_title: str,
    *,
    subtitle_line1: str = "从关键概念到临床决策",
    subtitle_line2: str = "循证、结构化、可迁移",
) -> dict[str, Any]:
    title = str(deck_title or "OralSage 学习课程").strip()
    return {
        "source_slide_id": "cover",
        "source_outline_node_id": "",
        "page_type": "cover",
        "rationale": "Runtime course cover derived from the deck title.",
        "slots": {
            "eyebrow": "ORALSAGE / 循证学习",
            "title": title,
            "subtitle_line1": subtitle_line1,
            "subtitle_line2": subtitle_line2,
            "presenter": "OralSage",
            "presenter_role": "智能口腔医学学习助手",
            "date_label": datetime.now().strftime("%Y.%m.%d"),
            "handwritten_note": "从证据出发，建立临床思维。",
        },
        "content_warnings": [],
    }


def _fallback_cover_title(deck_title: str, outline: dict[str, Any]) -> str:
    value = str(deck_title or outline.get("title") or "").strip()
    previous = None
    while value and value != previous:
        previous = value
        value = re.sub(
            r"^(?:请(?:帮我)?|我想|我要|希望)?(?:系统地?|全面地?)?(?:学习|了解|掌握|讲解)",
            "",
            value,
        ).strip(" ：:，,。")
    value = re.sub(
        r"(?:的)?(?:系统学习路径|系统学习|学习路径|课程讲义|学习课程)$",
        "",
        value,
    ).strip(" ：:，,。")
    if not value:
        value = str(outline.get("title") or "口腔医学专题").strip()
    return f"{value}：核心机制与临床决策"


def _parse_cover_copy(raw: str) -> dict[str, str]:
    value = str(raw or "").strip()
    value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.I)
    value = re.sub(r"\s*```$", "", value)
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("cover copy must be a JSON object")
    return {
        "title": str(parsed.get("title") or "").strip(),
        "subtitle_line1": str(parsed.get("subtitle_line1") or "").strip(),
        "subtitle_line2": str(parsed.get("subtitle_line2") or "").strip(),
    }


def _valid_cover_title(title: str, source_title: str) -> bool:
    compact = "".join(str(title or "").split())
    source = "".join(str(source_title or "").split())
    return bool(
        6 <= len(compact) <= 32
        and compact != source
        and "学习路径" not in compact
        and not any(marker in compact for marker in ("我想", "我要", "请帮我"))
    )


def _build_cover_copy(
    *,
    deck_title: str,
    outline: dict[str, Any],
    cache_path: Path,
    llm_client: Callable[[str], str] | None,
) -> tuple[dict[str, str], list[str], int]:
    source = {
        "deck_title": deck_title,
        "outline_title": outline.get("title"),
        "section_titles": [
            str(node.get("title") or "")
            for node in outline.get("nodes", [])
            if isinstance(node, dict)
        ],
    }
    source_hash = _source_hash(source)
    cached = _read_json(cache_path, {"status": "missing"})
    cached_copy = cached.get("copy")
    if (
        cached.get("status") == "ready"
        and cached.get("source_hash") == source_hash
        and isinstance(cached_copy, dict)
        and _valid_cover_title(str(cached_copy.get("title") or ""), deck_title)
    ):
        return {
            "title": str(cached_copy.get("title") or ""),
            "subtitle_line1": str(cached_copy.get("subtitle_line1") or "从关键概念到临床决策"),
            "subtitle_line2": str(cached_copy.get("subtitle_line2") or "循证、结构化、可迁移"),
        }, [], 0

    fallback = {
        "title": _fallback_cover_title(deck_title, outline),
        "subtitle_line1": "从关键概念到临床决策",
        "subtitle_line2": "循证、结构化、可迁移",
    }
    warnings: list[str] = []
    calls = 0
    client = llm_client or create_real_llm_client()
    copy = fallback
    mode = "fallback"
    if client is not None:
        prompt = (
            "为口腔医学课程 PPT 重写封面文案。不要复述用户请求，不要出现‘我想学习’、"
            "‘我要学习’或‘系统学习路径’。标题应体现医学主题与课程主线，10-22 个中文字符；"
            "两行副标题各 8-16 个中文字符，具体、克制，不使用营销措辞。严格返回 JSON："
            '{"title":"...","subtitle_line1":"...","subtitle_line2":"..."}。\n\n'
            f"课程上下文：{json.dumps(source, ensure_ascii=False)}"
        )
        try:
            candidate = _parse_cover_copy(client(prompt))
            calls = 1
            if _valid_cover_title(candidate["title"], deck_title):
                copy = {
                    "title": candidate["title"],
                    "subtitle_line1": candidate["subtitle_line1"] or fallback["subtitle_line1"],
                    "subtitle_line2": candidate["subtitle_line2"] or fallback["subtitle_line2"],
                }
                mode = "llm_rewritten"
            else:
                warnings.append("cover_title_rewrite_invalid")
        except Exception as exc:
            warnings.append(f"cover_title_rewrite_failed: {exc}")
    else:
        warnings.append("cover_title_llm_not_configured")

    _write_json_atomic(cache_path, {
        "schema_version": "learning.pin_and_paper_cover_copy.v1",
        "status": "ready",
        "source_hash": source_hash,
        "source": source,
        "copy": copy,
        "mode": mode,
        "warnings": warnings,
        "updated_at": _now(),
    })
    return copy, warnings, calls


def _upgrade_legacy_cover_plan(
    *,
    session_dir: Path,
    plan_path: Path,
    plan: dict[str, Any],
) -> dict[str, Any]:
    if plan.get("cover_title"):
        return plan
    deck_path = session_dir / "slides" / "slides.v1.json"
    if not deck_path.exists():
        return plan
    deck = _read_json(deck_path, {"status": "missing", "slides": []})
    deck_title = str(deck.get("deck_title") or plan.get("deck_title") or "")
    outline = {
        "title": deck_title,
        "nodes": [
            {
                "id": slide.get("outline_node_id"),
                "title": slide.get("title"),
            }
            for slide in (deck.get("slides") or [])
            if isinstance(slide, dict) and slide.get("outline_node_id")
        ],
    }
    cover_copy, warnings, calls = _build_cover_copy(
        deck_title=deck_title,
        outline=outline,
        cache_path=session_dir / "slides" / "pin_and_paper" / "cover.json",
        llm_client=None,
    )
    slides = list(plan.get("slides") or [])
    cover = _build_cover_plan(
        cover_copy["title"],
        subtitle_line1=cover_copy["subtitle_line1"],
        subtitle_line2=cover_copy["subtitle_line2"],
    )
    if slides and isinstance(slides[0], dict) and slides[0].get("page_type") == "cover":
        slides[0] = cover
    else:
        slides.insert(0, cover)
    upgraded = {
        **plan,
        "cover_title": cover_copy["title"],
        "slides": slides,
        "warnings": list(dict.fromkeys([*(plan.get("warnings") or []), *warnings])),
        "_llm_calls": int(plan.get("_llm_calls", 0) or 0) + calls,
        "updated_at": _now(),
    }
    _write_json_atomic(plan_path, upgraded)
    return upgraded


def _valid_slide_plan(plan: Any, valid_page_types: set[str]) -> bool:
    return bool(
        isinstance(plan, dict)
        and str(plan.get("source_outline_node_id") or "")
        and plan.get("page_type") in valid_page_types
        and isinstance(plan.get("slots"), dict)
        and plan.get("slots")
    )


def build_and_save_pin_and_paper_plan(
    *,
    session_id: str,
    outline: dict[str, Any],
    deck: dict[str, Any],
    resource_root: str | Path | None = None,
    llm_client: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Plan stale single-node slides and aggregate cached plans in outline order."""
    root = _root(resource_root)
    plan_dir = root / session_id / "slides" / "pin_and_paper"
    output_path = root / session_id / "slides" / "pin_and_paper_plan.v1.json"
    plan_dir.mkdir(parents=True, exist_ok=True)

    expected_ids = _expected_node_ids(outline)
    slides_by_id = {
        str(slide.get("outline_node_id")): slide
        for slide in deck.get("slides", [])
        if isinstance(slide, dict) and slide.get("outline_node_id")
    }
    slide_types = load_slide_types()
    valid_page_types = {
        str(item.get("type"))
        for item in slide_types.get("page_types", [])
        if item.get("type") and item.get("type") != "cover"
    }

    cover_copy, cover_warnings, cover_llm_calls = _build_cover_copy(
        deck_title=str(deck.get("deck_title") or ""),
        outline=outline,
        cache_path=plan_dir / "cover.json",
        llm_client=llm_client,
    )

    stale_slides: list[dict[str, Any]] = []
    source_hashes: dict[str, str] = {}
    for node_id, slide in slides_by_id.items():
        digest = _source_hash(slide)
        source_hashes[node_id] = digest
        cached = _read_json(plan_dir / f"{node_id}.json", {"status": "missing"})
        if not (
            cached.get("status") == "ready"
            and cached.get("source_slide_hash") == digest
            and _valid_slide_plan(cached.get("slide_plan"), valid_page_types)
        ):
            stale_slides.append(slide)

    planner_warnings: list[str] = list(cover_warnings)
    llm_calls = cover_llm_calls
    if stale_slides:
        client = llm_client or create_real_llm_client()
        if client is None:
            error = "pin_and_paper_llm_not_configured"
            planner_warnings.append(error)
            for slide in stale_slides:
                node_id = str(slide.get("outline_node_id") or "")
                node_plan_path = plan_dir / f"{node_id}.json"
                existing = _read_json(node_plan_path, {"status": "missing"})
                if (
                    existing.get("status") == "ready"
                    and existing.get("source_slide_hash") == source_hashes[node_id]
                    and _valid_slide_plan(existing.get("slide_plan"), valid_page_types)
                ):
                    continue
                _write_json_atomic(node_plan_path, {
                    "schema_version": "learning.pin_and_paper_node_plan.v1",
                    "session_id": session_id,
                    "outline_node_id": node_id,
                    "source_slide_hash": source_hashes[node_id],
                    "status": "failed",
                    "slide_plan": {},
                    "warnings": [error],
                    "error": error,
                    "updated_at": _now(),
                })
        else:
            stale_deck = {
                **deck,
                "slides": stale_slides,
                "warnings": [],
            }
            try:
                planned = plan_deck_concurrent(
                    stale_deck,
                    slide_types=slide_types,
                    llm_client=client,
                )
            except Exception as exc:
                planned = {"slides": [], "warnings": [f"planner_raised: {exc}"], "_llm_calls": 0}
            planner_warnings.extend(str(item) for item in planned.get("warnings", []))
            llm_calls += int(planned.get("_llm_calls", 0) or 0)
            planned_by_id = {
                str(item.get("source_outline_node_id")): item
                for item in planned.get("slides", [])
                if isinstance(item, dict) and item.get("source_outline_node_id")
            }
            for slide in stale_slides:
                node_id = str(slide.get("outline_node_id") or "")
                slide_plan = planned_by_id.get(node_id, {})
                ready = _valid_slide_plan(slide_plan, valid_page_types)
                error = None if ready else "pin_and_paper_plan_invalid_or_missing"
                node_plan_path = plan_dir / f"{node_id}.json"
                existing = _read_json(node_plan_path, {"status": "missing"})
                existing_ready = (
                    existing.get("status") == "ready"
                    and existing.get("source_slide_hash") == source_hashes[node_id]
                    and _valid_slide_plan(existing.get("slide_plan"), valid_page_types)
                )
                # An older in-flight refresh must not downgrade a valid cache
                # written by a faster terminal or background refresh.
                if not ready and existing_ready:
                    continue
                _write_json_atomic(node_plan_path, {
                    "schema_version": "learning.pin_and_paper_node_plan.v1",
                    "session_id": session_id,
                    "outline_node_id": node_id,
                    "source_slide_hash": source_hashes[node_id],
                    "status": "ready" if ready else "failed",
                    "slide_plan": slide_plan if isinstance(slide_plan, dict) else {},
                    "warnings": list(slide_plan.get("content_warnings", [])) if isinstance(slide_plan, dict) else [],
                    "error": error,
                    "updated_at": _now(),
                })

    content_plans: list[dict[str, Any]] = []
    ready_ids: list[str] = []
    failed_ids: list[str] = []
    for node_id in expected_ids:
        if node_id not in slides_by_id:
            continue
        cached = _read_json(plan_dir / f"{node_id}.json", {"status": "missing"})
        slide_plan = cached.get("slide_plan")
        if (
            cached.get("status") == "ready"
            and cached.get("source_slide_hash") == source_hashes.get(node_id)
            and _valid_slide_plan(slide_plan, valid_page_types)
        ):
            ready_ids.append(node_id)
            content_plans.append(slide_plan)
        else:
            failed_ids.append(node_id)

    missing_ids = [node_id for node_id in expected_ids if node_id not in ready_ids]
    if expected_ids and len(ready_ids) == len(expected_ids):
        status = "ready"
    elif ready_ids:
        status = "partial"
    else:
        status = "failed"

    plan = {
        "schema_version": "pin_and_paper_slide_plan.v1",
        "template_slug": "pin-and-paper",
        "deck_title": deck.get("deck_title", ""),
        "cover_title": cover_copy["title"],
        "session_id": session_id,
        "status": status,
        "expected_node_ids": expected_ids,
        "ready_node_ids": ready_ids,
        "missing_node_ids": missing_ids,
        "failed_node_ids": failed_ids,
        "content_slide_count": len(content_plans),
        "slides": [
            _build_cover_plan(
                cover_copy["title"],
                subtitle_line1=cover_copy["subtitle_line1"],
                subtitle_line2=cover_copy["subtitle_line2"],
            ),
            *content_plans,
        ],
        "warnings": list(dict.fromkeys([*deck.get("warnings", []), *planner_warnings])),
        "_llm_calls": llm_calls,
        "updated_at": _now(),
    }
    _write_json_atomic(output_path, plan)
    return {**plan, "plan_path": str(output_path)}


def _find_browser(explicit: str | Path | None = None) -> Path | None:
    candidates = [
        str(explicit or ""),
        os.getenv("PPT_BROWSER_PATH", ""),
        shutil.which("msedge") or "",
        shutil.which("chrome") or "",
        shutil.which("chromium") or "",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


class _RuntimePlanHandler(http.server.SimpleHTTPRequestHandler):
    plan_path: Path

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
        if urlparse(self.path).path == "/runtime-plan.json":
            data = self.plan_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        super().do_GET()

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def _browser_command(
    browser: Path,
    *,
    profile_dir: Path,
    url: str,
    screenshot_path: Path | None = None,
    dump_dom: bool = False,
) -> list[str]:
    command = [
        str(browser),
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-first-run",
        "--force-device-scale-factor=1",
        "--window-size=1920,1080",
        "--virtual-time-budget=5000",
        f"--user-data-dir={profile_dir}",
    ]
    if dump_dom:
        command.append("--dump-dom")
    if screenshot_path is not None:
        command.append(f"--screenshot={screenshot_path}")
    command.append(url)
    return command


def _validate_png(path: Path) -> None:
    if not path.exists() or path.stat().st_size < 1000:
        raise RuntimeError(f"rendered_slide_missing_or_empty: {path}")
    header = path.read_bytes()[:24]
    if header[:8] != b"\x89PNG\r\n\x1a\n":
        raise RuntimeError(f"rendered_slide_not_png: {path}")
    width, height = struct.unpack(">II", header[16:24])
    if (width, height) != (1920, 1080):
        raise RuntimeError(f"rendered_slide_wrong_size: {width}x{height}")


def _render_plan_to_pngs(
    *,
    plan_path: Path,
    output_dir: Path,
    slide_count: int,
    browser_path: str | Path | None = None,
) -> list[Path]:
    browser = _find_browser(browser_path)
    if browser is None:
        raise RuntimeError("ppt_browser_runtime_missing")
    if not _TEMPLATE_DIR.exists():
        raise RuntimeError("pin_and_paper_template_missing")

    handler = functools.partial(_RuntimePlanHandler, directory=str(_TEMPLATE_DIR))
    handler.plan_path = plan_path  # type: ignore[attr-defined]
    # functools.partial does not expose class attributes to instances; bind a
    # dedicated subclass so concurrent exporters keep independent plan paths.
    runtime_handler = type(
        f"RuntimePlanHandler_{hash(plan_path)}",
        (_RuntimePlanHandler,),
        {"plan_path": plan_path},
    )
    handler = functools.partial(runtime_handler, directory=str(_TEMPLATE_DIR))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        base_url = f"http://127.0.0.1:{port}/preview.html"
        dom_profile = output_dir / "browser-dom-profile"
        dom_query = urlencode({"plan": "/runtime-plan.json", "page": 1})
        dom_result = subprocess.run(
            _browser_command(
                browser,
                profile_dir=dom_profile,
                url=f"{base_url}?{dom_query}",
                dump_dom=True,
            ),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
        if dom_result.returncode != 0:
            raise RuntimeError(f"ppt_browser_dom_failed: {dom_result.stderr[-500:]}")
        rendered_count = len(re.findall(r"<section\b[^>]*class=\"[^\"]*\bslide\b", dom_result.stdout))
        if rendered_count < slide_count:
            raise RuntimeError(
                f"pin_and_paper_render_incomplete: {rendered_count}/{slide_count}"
            )

        images: list[Path] = []
        for index in range(slide_count):
            image_path = output_dir / f"slide_{index + 1:03d}.png"
            profile_dir = output_dir / f"browser-profile-{index + 1:03d}"
            page_query = urlencode({"plan": "/runtime-plan.json", "page": index + 1})
            completed = subprocess.run(
                _browser_command(
                    browser,
                    profile_dir=profile_dir,
                    url=f"{base_url}?{page_query}",
                    screenshot_path=image_path,
                ),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(
                    f"ppt_browser_screenshot_failed[{index + 1}]: {completed.stderr[-500:]}"
                )
            _validate_png(image_path)
            images.append(image_path)
        return images
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _persist_rendered_previews(
    *,
    session_dir: Path,
    plan: dict[str, Any],
    images: list[Path],
) -> dict[str, Any]:
    """Persist the exact renderer output used by the PPTX exporter."""
    plan_hash = _source_hash(plan)
    preview_root = session_dir / "slides" / "rendered_ppt"
    version_dir = preview_root / plan_hash
    version_dir.mkdir(parents=True, exist_ok=True)

    slide_rows: list[dict[str, Any]] = []
    plan_slides = plan.get("slides", []) if isinstance(plan.get("slides"), list) else []
    for index, (image, slide_plan) in enumerate(zip(images, plan_slides), start=1):
        source_plan = slide_plan if isinstance(slide_plan, dict) else {}
        filename = f"slide_{index:03d}.png"
        destination = version_dir / filename
        shutil.copy2(image, destination)
        slots = source_plan.get("slots", {})
        title = str(
            slots.get("headline")
            or slots.get("title")
            or source_plan.get("page_type")
            or f"第 {index} 页"
        ).strip()
        slide_rows.append({
            "index": index,
            "page_type": str(source_plan.get("page_type") or ""),
            "source_slide_id": str(source_plan.get("source_slide_id") or ""),
            "source_outline_node_id": str(
                source_plan.get("source_outline_node_id") or ""
            ),
            "title": title,
            "plan_hash": plan_hash,
            "filename": filename,
            "relative_path": f"{plan_hash}/{filename}",
            "width": 1920,
            "height": 1080,
        })

    manifest = {
        "schema_version": "learning.ppt_preview.v1",
        "session_id": str(plan.get("session_id") or session_dir.name),
        "template_slug": str(plan.get("template_slug") or "pin-and-paper"),
        "deck_title": str(plan.get("deck_title") or ""),
        "plan_hash": plan_hash,
        "status": "ready" if slide_rows else "failed",
        "slide_count": len(slide_rows),
        "slides": slide_rows,
        "updated_at": _now(),
    }
    _write_json_atomic(preview_root / "manifest.json", manifest)
    return manifest


def ensure_pin_and_paper_previews(
    *,
    session_id: str,
    resource_root: str | Path | None = None,
    browser_path: str | Path | None = None,
) -> dict[str, Any]:
    """Return cached real PPT pages, rendering the canonical plan if needed."""
    session_dir = _root(resource_root) / session_id
    plan_path = session_dir / "slides" / "pin_and_paper_plan.v1.json"
    plan = _read_json(plan_path, {"status": "missing", "slides": []})
    plan = _upgrade_legacy_cover_plan(
        session_dir=session_dir,
        plan_path=plan_path,
        plan=plan,
    )
    slides = plan.get("slides", []) if isinstance(plan.get("slides"), list) else []
    if not plan_path.exists() or not slides:
        return {
            "schema_version": "learning.ppt_preview.v1",
            "session_id": session_id,
            "status": "pending",
            "slide_count": 0,
            "slides": [],
            "error": "pin_and_paper_plan_not_ready",
        }

    plan_hash = _source_hash(plan)
    preview_root = session_dir / "slides" / "rendered_ppt"
    manifest_path = preview_root / "manifest.json"

    def current_manifest() -> dict[str, Any] | None:
        manifest = _read_json(manifest_path, {"status": "missing"})
        rows = manifest.get("slides", [])
        if (
            manifest.get("status") != "ready"
            or manifest.get("plan_hash") != plan_hash
            or len(rows) != len(slides)
        ):
            return None
        for row in rows:
            candidate = preview_root / str(row.get("relative_path") or "")
            if not candidate.is_file():
                return None
        return manifest

    cached = current_manifest()
    if cached is not None:
        return cached

    with _session_lock(session_id):
        cached = current_manifest()
        if cached is not None:
            return cached
        render_dir = Path(tempfile.mkdtemp(prefix="ppt-preview-", dir=str(session_dir)))
        try:
            images = _render_plan_to_pngs(
                plan_path=plan_path,
                output_dir=render_dir,
                slide_count=len(slides),
                browser_path=browser_path,
            )
            return _persist_rendered_previews(
                session_dir=session_dir,
                plan=plan,
                images=images,
            )
        except Exception as exc:
            return {
                "schema_version": "learning.ppt_preview.v1",
                "session_id": session_id,
                "status": "failed",
                "slide_count": 0,
                "slides": [],
                "error": f"pin_and_paper_preview_raised: {exc}",
            }
        finally:
            shutil.rmtree(render_dir, ignore_errors=True)


def _speaker_notes(
    session_id: str,
    outline_node_id: str,
    resource_root: str | Path | None,
) -> tuple[str, str]:
    if not outline_node_id:
        return "", ""
    tutor = get_tutor_cache(session_id, outline_node_id, resource_root=resource_root)
    if tutor.get("status") == "ready":
        teaching_text = str((tutor.get("tutor_step") or {}).get("teaching_text") or "").strip()
        if teaching_text:
            return teaching_text, "tutor_step.teaching_text"

    script_path = _root(resource_root) / session_id / "lecture_scripts" / f"{outline_node_id}.json"
    script = _read_json(script_path, {"status": "missing", "slides": []})
    narrations = [
        str(item.get("narration") or item.get("text") or "").strip()
        for item in script.get("slides", [])
        if isinstance(item, dict) and str(item.get("narration") or item.get("text") or "").strip()
    ]
    if narrations:
        return "\n\n".join(narrations), "lecture_scripts"
    return "", ""


def _validate_pptx(path: Path, expected_slides: int, expected_notes: int) -> dict[str, Any]:
    if not path.exists() or path.stat().st_size == 0:
        return {"ok": False, "error": "pptx_missing_or_empty"}
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            corrupt_entry = archive.testzip()
    except Exception as exc:
        return {"ok": False, "error": f"pptx_zip_invalid: {exc}"}
    slide_count = len([name for name in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)])
    notes_count = len([name for name in names if re.fullmatch(r"ppt/notesSlides/notesSlide\d+\.xml", name)])
    ok = (
        corrupt_entry is None
        and "[Content_Types].xml" in names
        and "ppt/presentation.xml" in names
        and slide_count == expected_slides
        and notes_count >= expected_notes
    )
    return {
        "ok": ok,
        "error": None if ok else "pptx_structure_mismatch",
        "byte_size": path.stat().st_size,
        "slide_count": slide_count,
        "expected_slide_count": expected_slides,
        "notes_count": notes_count,
        "minimum_notes_count": expected_notes,
        "corrupt_entry": corrupt_entry,
    }


def export_pin_and_paper_pptx(
    *,
    session_id: str,
    plan: dict[str, Any],
    plan_path: str | Path,
    resource_root: str | Path | None = None,
    browser_path: str | Path | None = None,
) -> dict[str, Any]:
    """Render the canonical HTML plan and package the pages into a PPTX."""
    session_dir = _root(resource_root) / session_id
    status_path = session_dir / "pptx_status.json"
    output_path = session_dir / "lesson.pptx"
    content_slide_count = int(plan.get("content_slide_count", 0) or 0)
    base_status = {
        "schema_version": "learning.pptx_status.v2",
        "session_id": session_id,
        "template_slug": "pin-and-paper",
        "plan_path": str(plan_path),
        "expected_section_count": len(plan.get("expected_node_ids", [])),
        "ready_section_count": len(plan.get("ready_node_ids", [])),
        "missing_section_ids": list(plan.get("missing_node_ids", [])),
        "content_slide_count": content_slide_count,
        "updated_at": _now(),
    }
    if content_slide_count <= 0:
        output_path.unlink(missing_ok=True)
        status = {
            **base_status,
            "ok": False,
            "status": "failed",
            "pptx_path": "",
            "error": "no_ready_pin_and_paper_content_slides",
        }
        _write_json_atomic(status_path, status)
        return status

    node = shutil.which("node")
    runtime = _EXPORTER_PATH.parent / "node_modules" / "pptxgenjs"
    if not node or not _EXPORTER_PATH.exists() or not runtime.exists():
        status = {
            **base_status,
            "ok": False,
            "status": "failed",
            "pptx_path": "",
            "error": "pptx_packaging_runtime_missing",
        }
        _write_json_atomic(status_path, status)
        return status

    _write_json_atomic(status_path, {
        **base_status,
        "ok": False,
        "status": "generating",
        "pptx_path": "",
        "error": None,
    })
    render_dir = Path(tempfile.mkdtemp(prefix="ppt-render-", dir=str(session_dir)))
    candidate_output_path = render_dir / "lesson.pptx"
    try:
        images = _render_plan_to_pngs(
            plan_path=Path(plan_path),
            output_dir=render_dir,
            slide_count=len(plan.get("slides", [])),
            browser_path=browser_path,
        )
        preview_manifest = _persist_rendered_previews(
            session_dir=session_dir,
            plan=plan,
            images=images,
        )
        export_slides: list[dict[str, Any]] = []
        expected_notes = 0
        for image_path, slide_plan in zip(images, plan.get("slides", [])):
            node_id = str(slide_plan.get("source_outline_node_id") or "")
            notes, notes_source = _speaker_notes(session_id, node_id, resource_root)
            if notes:
                expected_notes += 1
            export_slides.append({
                "image_path": str(image_path.resolve()),
                "outline_node_id": node_id,
                "speaker_notes": notes,
                "speaker_notes_source": notes_source,
            })
        manifest_path = render_dir / "pptx_export_manifest.json"
        _write_json_atomic(manifest_path, {
            "title": plan.get("deck_title", "OralSage"),
            "slides": export_slides,
        })
        completed = subprocess.run(
            [node, str(_EXPORTER_PATH), str(manifest_path), str(candidate_output_path)],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
        )
        export_error = "" if completed.returncode == 0 else (
            f"pptx_export_exit_{completed.returncode}: {completed.stderr[-500:]}"
        )
        validation = _validate_pptx(candidate_output_path, len(export_slides), expected_notes)
        ok = not export_error and validation.get("ok") is True
        if ok:
            os.replace(str(candidate_output_path), str(output_path))
        status = {
            **base_status,
            "ok": ok,
            "status": plan.get("status") if ok else "failed",
            "complete": ok and plan.get("status") == "ready",
            "pptx_path": str(output_path) if ok else "",
            "error": None if ok else (export_error or validation.get("error")),
            "validation": validation,
            "preview_manifest_path": str(
                session_dir / "slides" / "rendered_ppt" / "manifest.json"
            ),
            "preview_slide_count": preview_manifest.get("slide_count", 0),
            "updated_at": _now(),
        }
    except Exception as exc:
        status = {
            **base_status,
            "ok": False,
            "status": "failed",
            "complete": False,
            "pptx_path": "",
            "error": f"pin_and_paper_export_raised: {exc}",
            "updated_at": _now(),
        }
    finally:
        shutil.rmtree(render_dir, ignore_errors=True)
    _write_json_atomic(status_path, status)
    return status


def refresh_ppt_resources(
    *,
    session_id: str,
    outline: dict[str, Any],
    resource_root: str | Path | None = None,
    llm_client: Callable[[str], str] | None = None,
    export_pptx: bool = True,
    browser_path: str | Path | None = None,
) -> dict[str, Any]:
    """Refresh formal slides, Pin & Paper plans, and the current PPTX."""
    with _session_lock(session_id):
        slides_result = generate_slides_resource_from_session_cache(
            session_id=session_id,
            outline=outline,
            output_root=resource_root,
        )
        deck = slides_result.get("resource_json", {})
        if slides_result.get("status") != "ok" or not isinstance(deck, dict):
            return {
                "status": "failed",
                "slides": slides_result,
                "error": slides_result.get("error", "slides_generation_failed"),
            }
        plan = build_and_save_pin_and_paper_plan(
            session_id=session_id,
            outline=outline,
            deck=deck,
            resource_root=resource_root,
            llm_client=llm_client,
        )
        if export_pptx:
            pptx = export_pin_and_paper_pptx(
                session_id=session_id,
                plan=plan,
                plan_path=plan["plan_path"],
                resource_root=resource_root,
                browser_path=browser_path,
            )
        else:
            pptx = {"status": "skipped", "ok": False}
        return {
            "status": pptx.get("status", plan.get("status", "failed")),
            "slides": slides_result,
            "pin_and_paper_plan": plan,
            "pptx": pptx,
        }


def get_ppt_resource_status(
    *,
    session_id: str,
    outline: dict[str, Any] | None = None,
    resource_root: str | Path | None = None,
) -> dict[str, Any]:
    """Read current PPT resource status without starting generation."""
    session_dir = _root(resource_root) / session_id
    slides = _read_json(session_dir / "slides" / "slides.v1.json", {"status": "missing", "slides": []})
    plan = _read_json(
        session_dir / "slides" / "pin_and_paper_plan.v1.json",
        {"status": "missing", "ready_node_ids": [], "missing_node_ids": []},
    )
    pptx = _read_json(
        session_dir / "pptx_status.json",
        {"status": "missing", "ok": False, "pptx_path": ""},
    )
    expected_ids = _expected_node_ids(outline or {})
    return {
        "status": pptx.get("status", "missing"),
        "expected_section_count": len(expected_ids) or len(plan.get("expected_node_ids", [])),
        "slides": {
            "status": "ready" if slides.get("schema_version") == "slides.v1" else slides.get("status", "missing"),
            "slide_count": len(slides.get("slides", [])),
            "resource_path": str(session_dir / "slides" / "slides.v1.json"),
        },
        "pin_and_paper": {
            "status": plan.get("status", "missing"),
            "ready_node_ids": list(plan.get("ready_node_ids", [])),
            "missing_node_ids": list(plan.get("missing_node_ids", [])),
            "plan_path": str(session_dir / "slides" / "pin_and_paper_plan.v1.json"),
        },
        "pptx": pptx,
    }
