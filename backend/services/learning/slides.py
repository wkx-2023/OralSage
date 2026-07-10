"""Slides builder — generates ``slides.v1`` JSON from handout sections + tutor steps.

Produces one content slide per valid handout section.  No LLM.  No pptx.  No
frontend changes.

Architecture
------------
The PPT generation chain now has three layers:

1. **Single slide builder** — ``build_slide_from_handout_section``
   Builds one slide dict from one handout section + optional tutor step.
   Pure function — no file I/O.

2. **Single slide cache** — ``build_and_save_slide_for_node_from_session_cache``
   Reads handout/tutor caches for one node, builds a slide, saves it to
   ``slides/{outline_node_id}.json``.  A node's slide can be generated as
   soon as its handout section is ready — no waiting for the full course.

3. **Course-level aggregator** — ``aggregate_slides_from_node_cache``
   Reads existing single-slide caches and assembles them in outline order
   into a ``slides.v1`` deck dict.  Missing nodes produce warnings, not
   errors — the deck is always the best-effort view of what is ready.

   ``build_and_save_slides_from_session_cache`` now uses this two-step
   pipeline internally: build single slides → aggregate deck → save.

Public API
----------
* ``build_slide_from_handout_section`` — pure single slide (dict → dict)
* ``build_and_save_slide_for_node_from_session_cache`` — single node slide cache
* ``aggregate_slides_from_node_cache`` — deck from single-slide caches
* ``build_slides_from_handout_and_tutor`` — pure data transform (dict → dict)
* ``build_and_save_slides_from_session_cache`` — session cache → deck save
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


# ============================================================================
# Public entry points
# ============================================================================


def build_slide_from_handout_section(
    session_id: str,
    outline_node: dict,
    handout_section: dict,
    tutor_step: dict | None = None,
    theme_id: str = "clean_light",
) -> dict:
    """Build a single content slide from one handout section + optional tutor step.

    This is a **pure function** — it does not read or write files, does not
    call LLM, and does not generate PPTX.

    Parameters
    ----------
    session_id:
        Session identifier (reserved for future use; not embedded in the slide).
    outline_node:
        An outline node dict with at least ``"id"`` and ``"title"``.
    handout_section:
        The handout section dict for this node.  Content is read from
        ``handout_section["content"]``.
    tutor_step:
        Optional tutor step dict.  If missing, ``narration_ref`` and
        ``quiz_ref`` still point to ``outline_node_id``, and a warning is
        recorded.  Missing tutor step does NOT block slide generation.
    theme_id:
        Theme identifier (reserved for future use; default ``"clean_light"``).

    Returns
    -------
    dict with keys:
    - ``slide``: the single slide dict
    - ``warnings``: list of warning strings (may be empty)
    """
    warnings: list[str] = []
    outline_node_id = outline_node.get("id", "")
    if not outline_node_id:
        outline_node_id = handout_section.get("outline_node_id", "")

    raw_content: dict = handout_section.get("content", {})
    if not isinstance(raw_content, dict):
        raw_content = {}

    # Build PPT-only content — excludes transition_to_next
    ppt_content: dict = {}
    for _field in ("overview", "key_points", "clinical_example", "common_confusions"):
        if _field in raw_content:
            ppt_content[_field] = raw_content[_field]

    # -- Title ----------------------------------------------------------------
    title = _resolve_slide_title(handout_section, outline_node)

    # -- Bullets --------------------------------------------------------------
    bullets = _build_bullets(raw_content, outline_node_id, warnings)

    # -- Related KG node ids --------------------------------------------------
    used_evidence = handout_section.get("used_evidence", {})
    if not isinstance(used_evidence, dict):
        used_evidence = {}
    related_kg_node_ids: list[str] = list(used_evidence.get("kg_node_ids", []) or [])

    # -- Blocks ---------------------------------------------------------------
    blocks = _build_blocks(raw_content, outline_node_id)

    # -- Tutor step refs ------------------------------------------------------
    tutor_step_ref = outline_node_id
    narration_ref = {
        "source": "tutor_step.teaching_text",
        "outline_node_id": outline_node_id,
    }
    quiz_ref = {
        "source": "tutor_step.questions",
        "outline_node_id": outline_node_id,
    }

    if tutor_step is None:
        warnings.append(
            f"node {outline_node_id}: tutor_step missing — slide generated without narration/quiz"
        )
    else:
        questions = tutor_step.get("questions", [])
        if not questions or (isinstance(questions, list) and len(questions) == 0):
            warnings.append(
                f"node {outline_node_id}: tutor_step.questions is empty"
            )

    slide = {
        "slide_id": f"slide_{outline_node_id}",
        "outline_node_id": outline_node_id,
        "section_id": outline_node_id,
        "slide_type": "mixed_learning",
        "title": title,
        "subtitle": "",
        "bullets": bullets,
        "content": ppt_content,
        "blocks": blocks,
        "tutor_step_ref": tutor_step_ref,
        "narration_ref": narration_ref,
        "quiz_ref": quiz_ref,
        "related_kg_node_ids": related_kg_node_ids,
        "visual_hint": {
            "diagram_type": "mixed_learning_card",
        },
    }

    return {"slide": slide, "warnings": warnings}


def build_and_save_slide_for_node_from_session_cache(
    session_id: str,
    outline: dict,
    outline_node_id: str,
    resource_root: str | Path | None = None,
    theme_id: str = "clean_light",
) -> dict:
    """Build a single slide from session cache and save to ``slides/{node_id}.json``.

    Reads:
    - ``{resource_root}/{session_id}/handout_sections/{outline_node_id}.json``
    - ``{resource_root}/{session_id}/tutor_steps/{outline_node_id}.json`` (optional)

    Saves:
    - ``{resource_root}/{session_id}/slides/{outline_node_id}.json``

    Does NOT write the aggregated deck path (``slides/slides.v1.json``).

    Parameters
    ----------
    session_id:
        Session identifier.
    outline:
        A ``LessonOutline``-compatible dict as returned by the Planner.
    outline_node_id:
        The outline node id for which to build a slide.
    resource_root:
        Root directory for session data.  Defaults to ``data/learning/sessions``.
    theme_id:
        Theme identifier for frontend rendering (default ``"clean_light"``).

    Returns
    -------
    A dict with keys: ``status``, ``resource_type``, ``resource_path``,
    ``resource_json``, ``warnings``, and optionally ``error``.
    """
    from backend.services.learning.handout_cache import get_handout_cache
    from backend.services.learning.tutor_cache import get_tutor_cache

    warnings: list[str] = []
    root = Path(resource_root) if resource_root else Path("data/learning/sessions")

    # -- Find outline node ----------------------------------------------------
    outline_nodes: list[dict] = outline.get("nodes", [])
    outline_node: dict | None = None
    for node in outline_nodes:
        if node.get("id") == outline_node_id:
            outline_node = node
            break

    if outline_node is None:
        return {
            "status": "failed",
            "resource_type": "single_slide_json",
            "resource_path": "",
            "resource_json": {},
            "warnings": [f"outline_node_id {outline_node_id} not found in outline"],
            "error": "outline_node_not_found",
        }

    # -- Read handout cache ---------------------------------------------------
    hc = get_handout_cache(session_id, outline_node_id, resource_root=resource_root)
    hc_status = hc.get("status", "missing")
    if hc_status != "ready":
        return {
            "status": "failed",
            "resource_type": "single_slide_json",
            "resource_path": "",
            "resource_json": {},
            "warnings": [f"handout cache status={hc_status!r} for {outline_node_id}"],
            "error": "handout_not_ready",
        }

    handout_section = hc.get("handout_section")
    if not isinstance(handout_section, dict) or not handout_section:
        return {
            "status": "failed",
            "resource_type": "single_slide_json",
            "resource_path": "",
            "resource_json": {},
            "warnings": [
                f"handout cache ready but handout_section missing/invalid for {outline_node_id}"
            ],
            "error": "handout_section_missing",
        }

    # -- Read tutor cache (optional — missing tutor does NOT block) -----------
    tutor_step: dict | None = None
    tc = get_tutor_cache(session_id, outline_node_id, resource_root=resource_root)
    tc_status = tc.get("status", "missing")
    if tc_status == "ready":
        ts = tc.get("tutor_step")
        if isinstance(ts, dict) and ts:
            tutor_step = ts
    else:
        warnings.append(
            f"node {outline_node_id}: tutor cache status={tc_status!r} — "
            f"slide generated without tutor_step"
        )

    # -- Build slide ----------------------------------------------------------
    result = build_slide_from_handout_section(
        session_id=session_id,
        outline_node=outline_node,
        handout_section=handout_section,
        tutor_step=tutor_step,
        theme_id=theme_id,
    )

    slide = result["slide"]
    slide_warnings: list[str] = result["warnings"]
    all_warnings = warnings + slide_warnings

    # -- Save to disk ---------------------------------------------------------
    slides_dir = root / session_id / "slides"
    output_path = slides_dir / f"{outline_node_id}.json"

    try:
        slides_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = output_path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(slide, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(str(tmp_path), str(output_path))
    except Exception as exc:
        return {
            "status": "failed",
            "resource_type": "single_slide_json",
            "resource_path": str(output_path),
            "resource_json": slide,
            "warnings": all_warnings,
            "error": f"write_failed: {exc}",
        }

    return {
        "status": "ok",
        "resource_type": "single_slide_json",
        "resource_path": str(output_path),
        "resource_json": slide,
        "warnings": all_warnings,
    }


def aggregate_slides_from_node_cache(
    session_id: str,
    outline: dict,
    resource_root: str | Path | None = None,
    deck_title: str | None = None,
    theme_id: str = "clean_light",
) -> dict:
    """Aggregate existing single-slide caches into a ``slides.v1`` deck dict.

    Reads ``{resource_root}/{session_id}/slides/{outline_node_id}.json`` for
    each outline node, and assembles the found slides in outline order.

    Missing or corrupt slide caches produce warnings — they do **not** block
    the deck from being returned with the slides that *are* available.

    This function does **not**:
    - Re-generate missing slides from handout sections
    - Wait for all handout sections to complete
    - Call LLM
    - Save the deck to disk (callers decide whether to persist)

    Parameters
    ----------
    session_id:
        Session identifier.
    outline:
        A ``LessonOutline``-compatible dict as returned by the Planner.
    resource_root:
        Root directory for session data.  Defaults to ``data/learning/sessions``.
    deck_title:
        Optional deck title.  Falls back to ``outline["title"]``, then to
        ``"OralSage 学习讲义"``.
    theme_id:
        Theme identifier for frontend rendering (default ``"clean_light"``).

    Returns
    -------
    A ``slides.v1`` dict with keys: ``schema_version``, ``deck_id``,
    ``session_id``, ``deck_title``, ``theme_id``, ``source``, ``slides``,
    ``warnings``.
    """
    warnings: list[str] = []
    root = Path(resource_root) if resource_root else Path("data/learning/sessions")
    slides_dir = root / session_id / "slides"

    outline_nodes: list[dict] = outline.get("nodes", [])

    # -- Resolve deck title ---------------------------------------------------
    resolved_title = _resolve_deck_title(deck_title, outline)

    # -- Collect existing slides in outline order ------------------------------
    slides: list[dict] = []
    for node in outline_nodes:
        node_id = node.get("id", "")
        if not node_id:
            warnings.append("outline node missing id — skipping")
            continue

        slide_path = slides_dir / f"{node_id}.json"
        if not slide_path.exists():
            warnings.append(f"missing_slide_cache: {node_id}")
            continue

        try:
            slide = json.loads(slide_path.read_text(encoding="utf-8"))
        except Exception as exc:
            warnings.append(f"corrupted_slide_cache: {node_id} — {exc}")
            continue

        if not isinstance(slide, dict):
            warnings.append(f"invalid_slide_cache: {node_id} — not a dict")
            continue

        # Validate slide identity
        if slide.get("outline_node_id") != node_id:
            warnings.append(
                f"slide_cache_mismatch: {node_id} — "
                f"slide.outline_node_id={slide.get('outline_node_id')!r}"
            )
            continue

        slides.append(slide)

    return {
        "schema_version": "slides.v1",
        "deck_id": f"deck_{session_id}",
        "session_id": session_id,
        "deck_title": resolved_title,
        "theme_id": theme_id,
        "source": {
            "type": "handout_sections+tutor_steps",
            "granularity": "one_slide_per_outline_node",
            "aggregated_from": "single_slide_cache",
        },
        "slides": slides,
        "warnings": warnings,
    }


def build_slides_from_handout_and_tutor(
    session_id: str,
    outline: dict,
    handout_sections_by_node_id: dict[str, dict],
    tutor_steps_by_node_id: dict[str, dict],
    deck_title: str | None = None,
    theme_id: str = "clean_light",
) -> dict:
    """Build a ``slides.v1`` dict from handout sections and tutor steps.

    One content slide per valid handout section.  No cover slide is generated.
    This is a **pure function** — it does not read or write files.

    Parameters
    ----------
    session_id:
        Session identifier, used to build ``deck_id``.
    outline:
        A ``LessonOutline``-compatible dict as returned by the Planner.
    handout_sections_by_node_id:
        Dict mapping ``outline_node_id`` -> handout section dict (the
        per-section result from :func:`~resource_factory.generate_handout_section`).
    tutor_steps_by_node_id:
        Dict mapping ``outline_node_id`` -> tutor step dict (the per-section
        result from :func:`~tutor.build_tutor_step`).
    deck_title:
        Optional deck title.  Falls back to ``outline["title"]``, then to
        ``"OralSage 学习讲义"``.
    theme_id:
        Theme identifier for frontend rendering (default ``"clean_light"``).

    Returns
    -------
    A ``slides.v1`` dict with keys: ``schema_version``, ``deck_id``,
    ``session_id``, ``deck_title``, ``theme_id``, ``source``, ``slides``,
    ``warnings``.
    """
    warnings: list[str] = []

    # -- Resolve deck_title ---------------------------------------------------
    resolved_title = _resolve_deck_title(deck_title, outline)

    # -- Outline nodes (ordered) ----------------------------------------------
    outline_nodes: list[dict] = outline.get("nodes", [])

    # -- Build slides in outline order ----------------------------------------
    slides: list[dict] = []

    for outline_node in outline_nodes:
        node_id = outline_node.get("id", "")
        if not node_id:
            warnings.append("outline node missing id — skipping")
            continue

        # --- Look up handout section ---
        section = handout_sections_by_node_id.get(node_id)
        if section is None:
            warnings.append(
                f"node {node_id}: no handout_section — skipping"
            )
            continue

        # --- Validate handout_section.outline_node_id ---
        section_oid = section.get("outline_node_id")
        if not section_oid:
            # Missing outline_node_id in handout_section — use outline node id as fallback
            warnings.append(
                f"node {node_id}: handout_section missing outline_node_id field, "
                f"falling back to outline node id"
            )
        elif str(section_oid) != node_id:
            # Mismatch — skip, do not generate a slide with wrong binding
            warnings.append(
                f"node {node_id}: skipped — handout_section.outline_node_id="
                f"{section_oid!r} does not match outline node id {node_id!r}"
            )
            continue

        # --- Validate section_status ---
        section_status = section.get("section_status", "")
        if section_status != "llm_generated":
            warnings.append(
                f"node {node_id}: skipped — section_status={section_status!r}, "
                f"expected 'llm_generated'"
            )
            continue

        slide_warnings: list[str] = []

        # Look up tutor step
        tutor_step = tutor_steps_by_node_id.get(node_id)

        # Build slide
        slide = _build_one_slide(
            outline_node_id=node_id,
            handout_section=section,
            tutor_step=tutor_step,
            outline_node=outline_node,
            slide_warnings=slide_warnings,
        )
        slides.append(slide)

        # Per-slide warnings
        for w in slide_warnings:
            warnings.append(w)

    return {
        "schema_version": "slides.v1",
        "deck_id": f"deck_{session_id}",
        "session_id": session_id,
        "deck_title": resolved_title,
        "theme_id": theme_id,
        "source": {
            "type": "handout_sections+tutor_steps",
            "granularity": "one_slide_per_outline_node",
        },
        "slides": slides,
        "warnings": warnings,
    }


def build_and_save_slides_from_session_cache(
    session_id: str,
    outline: dict,
    resource_root: str | Path | None = None,
    deck_title: str | None = None,
    theme_id: str = "clean_light",
    output_filename: str = "slides.v1.json",
) -> dict:
    """Read handout and tutor cache for *session_id*, build slides, and save to disk.

    Uses a two-step pipeline:

    1. For each outline node with a ready handout cache, build a single slide
       and save it to ``slides/{outline_node_id}.json``.
    2. Aggregate all existing single-slide caches into a ``slides.v1`` deck
       and save it to ``slides/slides.v1.json``.

    A node whose handout cache is not ready is skipped (with a warning).
    A node whose tutor cache is missing is NOT skipped — its slide is still
    generated with ``narration_ref``/``quiz_ref`` and a warning.

    Parameters
    ----------
    session_id:
        Session identifier.
    outline:
        A ``LessonOutline``-compatible dict as returned by the Planner.
    resource_root:
        Root directory for session data.  Defaults to ``data/learning/sessions``.
    deck_title:
        Optional deck title.
    theme_id:
        Theme identifier for frontend rendering (default ``"clean_light"``).
    output_filename:
        Output filename (default ``"slides.v1.json"``).

    Returns
    -------
    A dict with keys: ``status``, ``resource_type``, ``resource_path``,
    ``resource_json``, ``warnings``, and optionally ``error``.
    """
    from backend.services.learning.handout_cache import get_handout_cache

    warnings: list[str] = []
    root = Path(resource_root) if resource_root else Path("data/learning/sessions")
    outline_nodes: list[dict] = outline.get("nodes", [])

    # =========================================================================
    # Step 1: Build and save single slides for nodes with ready handout caches
    # =========================================================================
    generated_node_ids: set[str] = set()
    for node in outline_nodes:
        node_id = node.get("id", "")
        if not node_id:
            continue

        # --- Check handout cache ---
        hc = get_handout_cache(session_id, node_id, resource_root=resource_root)
        hc_status = hc.get("status", "missing")
        if hc_status != "ready":
            warnings.append(
                f"node {node_id}: handout cache status={hc_status!r} — skipping"
            )
            continue

        handout_section = hc.get("handout_section")
        if not isinstance(handout_section, dict) or not handout_section:
            warnings.append(
                f"node {node_id}: handout cache ready but handout_section missing/invalid — skipping"
            )
            continue

        # --- Build and save single slide ---
        single_result = build_and_save_slide_for_node_from_session_cache(
            session_id=session_id,
            outline=outline,
            outline_node_id=node_id,
            resource_root=resource_root,
            theme_id=theme_id,
        )

        if single_result.get("status") == "ok":
            generated_node_ids.add(node_id)

        # Collect per-slide warnings (deduplicate)
        for w in single_result.get("warnings", []):
            if w not in warnings:
                warnings.append(w)

    # =========================================================================
    # Step 2: Aggregate from single slide caches into deck
    # =========================================================================
    deck = aggregate_slides_from_node_cache(
        session_id=session_id,
        outline=outline,
        resource_root=resource_root,
        deck_title=deck_title,
        theme_id=theme_id,
    )

    # Merge aggregation warnings (deduplicate)
    for w in deck.get("warnings", []):
        if w not in warnings:
            warnings.append(w)
    deck["warnings"] = warnings

    # =========================================================================
    # Save deck to disk
    # =========================================================================
    slides_dir = root / session_id / "slides"
    output_path = slides_dir / output_filename

    try:
        slides_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = output_path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(deck, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(str(tmp_path), str(output_path))
    except Exception as exc:
        return {
            "status": "failed",
            "resource_type": "slides_json",
            "resource_path": str(output_path),
            "resource_json": deck,
            "warnings": warnings,
            "error": f"write_failed: {exc}",
        }

    return {
        "status": "ok",
        "resource_type": "slides_json",
        "resource_path": str(output_path),
        "resource_json": deck,
        "warnings": warnings,
    }


# ============================================================================
# Internal helpers
# ============================================================================


def _resolve_deck_title(deck_title: str | None, outline: dict) -> str:
    """Resolve deck_title: explicit > outline.title > default."""
    if deck_title:
        return deck_title
    outline_title = outline.get("title", "")
    if outline_title:
        return outline_title
    return "OralSage 学习讲义"


def _resolve_slide_title(
    handout_section: dict,
    outline_node: dict,
) -> str:
    """Resolve slide title.

    Priority: handout_section.title > outline node title > "未命名小节".
    """
    hs_title = str(handout_section.get("title", "")).strip()
    if hs_title:
        return hs_title
    on_title = str(outline_node.get("title", "")).strip()
    if on_title:
        return on_title
    return "未命名小节"


def _build_one_slide(
    outline_node_id: str,
    handout_section: dict,
    tutor_step: dict | None,
    outline_node: dict,
    slide_warnings: list[str],
) -> dict:
    """Build a single content slide — delegates to :func:`build_slide_from_handout_section`."""
    result = build_slide_from_handout_section(
        session_id="",
        outline_node=outline_node,
        handout_section=handout_section,
        tutor_step=tutor_step,
    )
    slide_warnings.extend(result["warnings"])
    return result["slide"]


def _build_bullets(
    content: dict,
    node_id: str,
    slide_warnings: list[str],
) -> list[str]:
    """Extract bullets from key_points headings (max 5, no empty, no explanation)."""
    key_points = content.get("key_points", [])
    if not isinstance(key_points, list):
        key_points = []

    if len(key_points) == 0:
        slide_warnings.append(
            f"node {node_id}: content.key_points is empty — slide generated without bullets"
        )
        return []

    bullets: list[str] = []
    for kp in key_points:
        if not isinstance(kp, dict):
            continue
        heading = str(kp.get("heading", "")).strip()
        if heading:
            bullets.append(heading)
        if len(bullets) >= 5:
            break

    return bullets


def _build_blocks(content: dict, node_id: str) -> list[dict]:
    """Derive display blocks from content fields.

    Does NOT rewrite or generate medical content — each block carries the
    original field value as its body.
    """
    blocks: list[dict] = []
    block_idx = 0

    def _next_id() -> str:
        nonlocal block_idx
        block_idx += 1
        return f"block_{node_id}_{block_idx:02d}"

    # overview
    overview_text = str(content.get("overview", "")).strip()
    if overview_text:
        blocks.append({
            "block_id": _next_id(),
            "block_type": "overview",
            "title": "本节概述",
            "body": overview_text,
            "source_field": "content.overview",
        })

    # key_points
    key_points = content.get("key_points", [])
    if isinstance(key_points, list):
        for kp in key_points:
            if not isinstance(kp, dict):
                continue
            heading = str(kp.get("heading", "")).strip()
            explanation = str(kp.get("explanation", "")).strip()
            evidence_refs = kp.get("evidence_refs", [])
            if not isinstance(evidence_refs, list):
                evidence_refs = []
            body = explanation if explanation else heading
            if not body:
                continue
            blocks.append({
                "block_id": _next_id(),
                "block_type": "key_point",
                "title": heading if heading else "要点",
                "body": body,
                "source_field": "content.key_points",
                "semantic_role": "concept",
                "evidence_refs": list(evidence_refs),
            })

    # clinical_example
    clinical = str(content.get("clinical_example", "")).strip()
    if clinical:
        blocks.append({
            "block_id": _next_id(),
            "block_type": "clinical_example",
            "title": "临床示例",
            "body": clinical,
            "source_field": "content.clinical_example",
        })

    # common_confusions
    common_confusions = content.get("common_confusions", [])
    if isinstance(common_confusions, list):
        for cc in common_confusions:
            if not isinstance(cc, dict):
                continue
            confusion = str(cc.get("confusion", "")).strip()
            clarification = str(cc.get("clarification", "")).strip()
            body = f"混淆：{confusion}\n澄清：{clarification}" if confusion and clarification else (confusion or clarification)
            if not body:
                continue
            blocks.append({
                "block_id": _next_id(),
                "block_type": "confusion",
                "title": confusion if confusion else "常见混淆",
                "body": body,
                "source_field": "content.common_confusions",
            })

    return blocks
