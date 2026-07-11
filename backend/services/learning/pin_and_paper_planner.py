"""Pin & Paper planner — turns ``slides.v1`` slides into ``pin_and_paper_slide_plan.v1``.

Architecture
------------
This is an **independent planner/adapter layer** between the ``slides.v1``
generation chain and the Pin & Paper renderer.  It delegates page-type
selection, slot filling, and display-text compression to an injectable LLM
client.  Python handles prompt assembly, JSON parsing, schema validation,
budget checking, and at-most-one compression retry.

Responsibility split
--------------------
* **LLM**: choose page_type from Pin & Paper ``slide-types.json``, fill slots
  from source slide content, compress display text to fit budget.  Must NOT
  invent medical facts, write HTML/CSS, or reference other templates.
* **Python**: build prompts, call the injectable LLM client, parse + validate
  the output, check per-slot budgets, trigger a single full-page compression
  retry when budgets are exceeded, and aggregate warnings.
* **Renderer** (``preview.html``): maps ``page_type + slots`` into Pin & Paper
  HTML/CSS — never truncates, never generates content.

Two-phase flow per slide
------------------------
1. **Initial plan** — LLM receives source slide + Pin & Paper slide-types
   contract and returns a full ``pin_and_paper_slide_plan.v1`` slide entry.
2. **Compression retry** (only if budgets exceeded) — the full initial plan,
   budget-failure details, and target budgets are sent back to the LLM.  The
   LLM must return a **complete revised slide entry**, not just changed slots.

If the retry still exceeds budgets, the plan is returned as-is with a
``pin_and_paper_over_budget_after_retry`` warning — Python **never** truncates.

Public API
----------
* ``plan_pin_and_paper_slide`` — plan one slide (source dict → plan dict)
* ``plan_pin_and_paper_deck`` — plan all slides in a ``slides.v1`` deck
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Callable

# ============================================================================
# Per-slot budget definitions (Pin & Paper specific)
# ============================================================================

_TOP_LEVEL_BUDGETS: dict[str, dict[str, Any]] = {
    "eyebrow":            {"max_chars": 20,  "description": "section eyebrow label"},
    "headline":           {"max_chars": 40,  "description": "main headline"},
    "title":              {"max_chars": 30,  "description": "cover title (per line)"},
    "subtitle":           {"max_chars": 60,  "description": "subtitle or sub-headline"},
    "subtitle_line1":     {"max_chars": 30,  "description": "cover subtitle line 1"},
    "subtitle_line2":     {"max_chars": 30,  "description": "cover subtitle line 2"},
    "body":               {"max_chars": 160, "description": "narrative body paragraph"},
    "quote_text":         {"max_chars": 100, "description": "quote main text"},
    "attribution_name":   {"max_chars": 30,  "description": "quote attribution name"},
    "attribution_detail": {"max_chars": 60,  "description": "quote attribution detail"},
    "presenter":          {"max_chars": 20,  "description": "cover presenter name"},
    "presenter_role":     {"max_chars": 40,  "description": "cover presenter role"},
    "date_label":         {"max_chars": 30,  "description": "cover date label"},
    "handwritten_note":   {"max_chars": 60,  "description": "cover handwritten note"},
    "marker":             {"max_chars": 30,  "description": "section marker text"},
    "label":              {"max_chars": 30,  "description": "section label text"},
    "scribble":           {"max_chars": 50,  "description": "handwritten scribble annotation"},
    "footer_left":        {"max_chars": 30,  "description": "footer left text"},
    "footer_right":       {"max_chars": 30,  "description": "footer right text"},
}

_ITEM_BUDGETS: dict[str, dict[str, Any]] = {
    "num":       {"max_chars": 4,   "description": "step / card number"},
    "label":     {"max_chars": 20,  "description": "agenda item / card label"},
    "title":     {"max_chars": 30,  "description": "card / step / column title"},
    "body":      {"max_chars": 120, "description": "card / step / column body text"},
    "meta":      {"max_chars": 30,  "description": "metadata label"},
    "source":    {"max_chars": 40,  "description": "source citation"},
    "value":     {"max_chars": 12,  "description": "stat value"},
    "unit":      {"max_chars": 8,   "description": "stat unit"},
    "criterion": {"max_chars": 20,  "description": "matrix criterion label"},
}

_CELL_BUDGETS: dict[str, dict[str, Any]] = {
    "cells":     {"max_chars": 30,  "description": "matrix cell text"},
    "legend":    {"max_chars": 30,  "description": "chart legend item"},
    "values":    {"max_chars": 12,  "description": "chart series value"},
    "series":    {"max_chars": 20,  "description": "chart series label"},
    "columns":   {"max_chars": 12,  "description": "matrix column header"},
    "x_labels":  {"max_chars": 16,  "description": "chart x-axis label"},
    "y_labels":  {"max_chars": 10,  "description": "chart y-axis label"},
    "timeline":  {"max_chars": 30,  "description": "process timeline label"},
}

# Renderer capacity is not uniform across page types.  Card grids have much
# less room per item than notice/CTA pages, so the planner uses stricter,
# page-aware overrides before content reaches the browser renderer.
_PAGE_TYPE_TOP_LEVEL_BUDGETS: dict[str, dict[str, dict[str, Any]]] = {
    "cover": {
        "title": {"max_chars": 28, "description": "cover title"},
        "handwritten_note": {"max_chars": 44, "description": "cover note"},
    },
    "agenda": {
        "headline": {"max_chars": 34, "description": "agenda headline"},
    },
    "notes": {
        "headline": {"max_chars": 34, "description": "notes headline"},
        "subtitle": {"max_chars": 46, "description": "notes subtitle"},
    },
    "notes_2up": {
        "headline": {"max_chars": 34, "description": "two-card notes headline"},
        "subtitle": {"max_chars": 52, "description": "two-card notes subtitle"},
    },
    "notes_4up": {
        "headline": {"max_chars": 32, "description": "four-card notes headline"},
        "subtitle": {"max_chars": 38, "description": "four-card notes subtitle"},
    },
    "notice": {
        "eyebrow": {"max_chars": 14, "description": "notice eyebrow"},
        "headline": {"max_chars": 34, "description": "notice headline"},
    },
    "process": {
        "headline": {"max_chars": 34, "description": "process headline"},
        "subtitle": {"max_chars": 44, "description": "process subtitle"},
    },
    "matrix": {
        "headline": {"max_chars": 34, "description": "matrix headline"},
        "subtitle": {"max_chars": 44, "description": "matrix subtitle"},
    },
    "stats": {
        "headline": {"max_chars": 34, "description": "stats headline"},
        "subtitle": {"max_chars": 44, "description": "stats subtitle"},
    },
    "quote": {
        "headline": {"max_chars": 34, "description": "quote headline"},
        "quote_text": {"max_chars": 100, "description": "quote text"},
    },
    "cta": {
        "eyebrow": {"max_chars": 14, "description": "cta eyebrow"},
        "headline": {"max_chars": 34, "description": "cta headline"},
        "body": {"max_chars": 110, "description": "cta left body"},
    },
}

_PAGE_TYPE_ITEM_BUDGETS: dict[str, dict[str, dict[str, dict[str, Any]]]] = {
    "agenda": {
        "items": {
            "label": {"max_chars": 18, "description": "agenda item label"},
            "meta": {"max_chars": 18, "description": "agenda item metadata"},
        },
    },
    "notes": {
        "cards": {
            "label": {"max_chars": 10, "description": "3-card label"},
            "title": {"max_chars": 18, "description": "3-card title"},
            "body": {"max_chars": 120, "description": "3-card body"},
            "scribble": {"max_chars": 18, "description": "3-card note"},
        },
    },
    "notes_2up": {
        "cards": {
            "label": {"max_chars": 12, "description": "2-card label"},
            "title": {"max_chars": 20, "description": "2-card title"},
            "body": {"max_chars": 160, "description": "2-card body"},
            "scribble": {"max_chars": 20, "description": "2-card note"},
        },
    },
    "notes_4up": {
        "cards": {
            "label": {"max_chars": 10, "description": "4-card label"},
            "title": {"max_chars": 16, "description": "4-card title"},
            "body": {"max_chars": 90, "description": "4-card body"},
            "scribble": {"max_chars": 16, "description": "4-card note"},
        },
    },
    "notice": {
        "columns": {
            "title": {"max_chars": 18, "description": "notice column title"},
            "body": {"max_chars": 120, "description": "notice column body"},
            "meta": {"max_chars": 18, "description": "notice column metadata"},
            "source": {"max_chars": 24, "description": "notice source label"},
        },
    },
    "process": {
        "steps": {
            "title": {"max_chars": 14, "description": "process step title"},
            "body": {"max_chars": 70, "description": "process step body"},
        },
        "timeline": {
            "timeline": {"max_chars": 18, "description": "process timeline label"},
        },
    },
    "matrix": {
        "columns": {
            "columns": {"max_chars": 10, "description": "matrix column header"},
        },
        "rows": {
            "criterion": {"max_chars": 14, "description": "matrix criterion"},
            "cells": {"max_chars": 24, "description": "matrix cell"},
        },
    },
    "stats": {
        "stats": {
            "title": {"max_chars": 12, "description": "stat title"},
            "body": {"max_chars": 56, "description": "stat body"},
        },
    },
    "cta": {
        "steps": {
            "title": {"max_chars": 14, "description": "cta step title"},
            "body": {"max_chars": 90, "description": "cta step body"},
        },
    },
}

# ============================================================================
# Prompt templates
# ============================================================================

_INITIAL_PROMPT_HEADER = """\
You are planning slides for the Pin & Paper template ONLY.
Read the slide-types contract below.  Use ONLY the provided source slide content as truth.
Return JSON only — no markdown fences, no extra text.

RULES (hard constraints):
1. Choose ONE page_type from the list.  Never invent page types.
2. Fill EVERY slot declared by that page_type.  Do not add extra slots.
3. All user-visible text MUST be Chinese (zh-CN), unless the source is English.
4. Do NOT invent medical facts, diagnoses, treatments, statistics, or quiz questions.
   All medical content must come exclusively from the supplied slides.v1 / handout_section projection.
5. Do NOT output HTML tags, CSS, inline styles, or markdown formatting.
6. Keep text within the budget described below.
7. Return only a JSON object conforming to pin_and_paper_slide_plan.v1 (single slide entry).
8. Preserve normal teaching information density — do NOT pre-compress to match short English samples.
9. Key-point routing (MANDATORY — VIOLATING THIS RULE WILL CAUSE A CORRECTION RETRY):
   - 2 key_points  → notes_2up  ONLY.  NEVER use notes, notes_4up, or any other type.
   - 4 key_points  → notes_4up  ONLY.  NEVER use notes, notes_2up, or any other type.
   - 3 key_points  → notes (3-card grid) or notice (2-3 explanatory columns).
     NEVER use notes_2up (which requires exactly 2 cards) or notes_4up (which requires exactly 4 cards).
   - 5+ key_points → cta (preferred) or agenda or process.
     NEVER use notes, notes_2up, or notes_4up for >=5 items.
   COUNT THE KEY_POINTS in the source slide FIRST.  Then count AGAIN.
   notes_2up = EXACTLY 2 cards, no more, no less.
   notes_4up = EXACTLY 4 cards, no more, no less.
   If the source slide has 3 key_points, notes_4up is a HARD ERROR — you would need to invent a 4th card.
10. For EVERY array item (cards, steps, columns), include a
   ``source_block_ids`` field: a list of block_id(s) from the source slide
   whose content was used to create this item.  This is REQUIRED.
11. Respect PAGE-SPECIFIC TEXT CAPACITY.  Card grids have strict per-card
   budgets: notes cards are shorter than notes_2up; notes_4up is the tightest.
   If source content is too long, compress it before returning JSON.  Do not
   expect the renderer to resize, scroll, hide, or truncate text.

If source content is insufficient for a slot, use an empty string "".
If source content needs compression to fit, shorten aggressively while keeping meaning.
"""

_COMPRESSION_PROMPT_HEADER = """\
COMPRESSION RETRY — same slide, same rules.

Your previous plan for this slide exceeded text budgets.  Below are:
- The original source slide (unchanged).
- Your previous FULL page plan.
- A list of budget failures (which slots/cards were too long, with current length and target max).

You must return a COMPLETE revised page plan (not just the changed fields).
Keep the same page_type.  Compress the over-budget slots while preserving source meaning.
Shorten aggressively: use fewer characters, shorter phrases, or drop non-essential detail.
But RETAIN essential teaching content: definitions, mechanisms, clinical significance, key distinctions, common misconceptions.

RULES (same as before):
1. Do NOT invent medical facts.
2. Do NOT output HTML/CSS.
3. All visible text in Chinese.
4. Return JSON only.
"""

_CORRECTION_PROMPT_HEADER = """\
ROUTE CORRECTION — your previous page_type was wrong for this slide.

Your previous plan used page_type "{wrong_page_type}", which is INVALID for this source slide.
The source slide has {n_key_points} key_points.

KEY-POINT ROUTING RULES (must follow exactly):
- 2 key_points  → notes_2up  ONLY
- 4 key_points  → notes_4up  ONLY
- 3 key_points  → notes or notice
- 5+ key_points → cta (preferred) or agenda or process (NEVER notes/notes_2up/notes_4up)

You used "{wrong_page_type}" which has {card_constraint}.
Please return a COMPLETE revised page plan with a CORRECTED page_type.
Keep the same source content but adapt the page structure to fit the correct page_type.
All other rules still apply: no invented medical facts, no HTML/CSS, all text in Chinese,
return JSON only, include source_block_ids on every array item.
"""

# ============================================================================
# Internal helpers — slide_types loading
# ============================================================================

_SLIDE_TYPES_CACHE: dict[str, Any] | None = None
_SLIDE_TYPES_PATH: str | None = None


def _resolve_slide_types_path() -> str:
    """Resolve the absolute path to Pin & Paper slide-types.json."""
    global _SLIDE_TYPES_PATH
    if _SLIDE_TYPES_PATH:
        return _SLIDE_TYPES_PATH
    candidate = (
        Path(__file__).resolve().parent.parent.parent.parent
        / "tools" / "ppt_preview" / "templates" / "pin-and-paper" / "slide-types.json"
    )
    if candidate.exists():
        _SLIDE_TYPES_PATH = str(candidate)
        return _SLIDE_TYPES_PATH
    fallback = Path("tools/ppt_preview/templates/pin-and-paper/slide-types.json")
    if fallback.exists():
        _SLIDE_TYPES_PATH = str(fallback.resolve())
        return _SLIDE_TYPES_PATH
    raise FileNotFoundError("Cannot resolve Pin & Paper slide-types.json path")


def load_slide_types(path: str | None = None) -> dict:
    """Load Pin & Paper slide-types.json, caching the result."""
    global _SLIDE_TYPES_CACHE
    if _SLIDE_TYPES_CACHE is not None and path is None:
        return _SLIDE_TYPES_CACHE
    resolved = path or _resolve_slide_types_path()
    with open(resolved, "r", encoding="utf-8") as f:
        data = json.load(f)
    if path is None:
        _SLIDE_TYPES_CACHE = data
    return data


def _get_page_type_def(page_type: str, slide_types: dict) -> dict | None:
    for pt in slide_types.get("page_types", []):
        if pt.get("type") == page_type:
            return pt
    return None


def _get_page_type_slot_names(page_type: str, slide_types: dict) -> list[str]:
    pt = _get_page_type_def(page_type, slide_types)
    if pt is None:
        return []
    return list(pt.get("slots", []))


def _get_slot_shapes(page_type: str, slide_types: dict) -> dict:
    pt = _get_page_type_def(page_type, slide_types)
    if pt is None:
        return {}
    return pt.get("slot_shapes", {})


# ============================================================================
# Attribution / traceability fields
# ============================================================================

# Fields that carry source-traceability metadata on array items (cards,
# steps, columns, etc.).  They are NOT declared in slide-types.json item_slots
# and must pass through validation + budget checking without errors.
_ATTRIBUTION_META_FIELDS: set[str] = {
    "source_block_ids",  # list[str] — block_id(s) in the source slide
    "source_fields",     # str — field path(s) in the source slide, e.g. "blocks[0].body"
}

# ============================================================================
# Validation helpers
# ============================================================================

_HTML_CSS_RE = re.compile(
    r"</?[a-zA-Z][a-zA-Z0-9]*(?:\s[^>]*)?>|"
    r"style\s*=\s*[\"'][^\"']*[\"']|"
    r"<style\b|</style\b|"
    r"url\s*\(|"
    r"@import\b|"
    r"font-family\s*:|"
    r"background-color\s*:|"
    r"color\s*:\s*#",
    re.IGNORECASE,
)


def _contains_html_css(text: str) -> bool:
    if not isinstance(text, str):
        return False
    return bool(_HTML_CSS_RE.search(text))


def _validate_page_type(page_type: str, slide_types: dict) -> list[str]:
    errors: list[str] = []
    valid_types = {pt["type"] for pt in slide_types.get("page_types", [])}
    if not page_type:
        errors.append("page_type is empty")
    elif page_type not in valid_types:
        errors.append(
            f"invalid page_type: {page_type!r} (allowed: {sorted(valid_types)})"
        )
    return errors


def _validate_slots(
    page_type: str,
    slots: dict,
    slide_types: dict,
) -> list[str]:
    errors: list[str] = []
    declared = _get_page_type_slot_names(page_type, slide_types)
    declared_set = set(declared)
    actual_set = set(slots.keys())

    missing = declared_set - actual_set
    for m in sorted(missing):
        errors.append(f"missing slot: {page_type}.{m}")

    extra = actual_set - declared_set
    for e in sorted(extra):
        errors.append(f"undeclared slot: {page_type}.{e} (not in {declared})")

    if missing:
        return errors

    # HTML/CSS check
    html_errors = _check_slots_for_html_css(slots, page_type)
    errors.extend(html_errors)

    # Array slot shapes
    slot_shapes = _get_slot_shapes(page_type, slide_types)
    for slot_name, shape in slot_shapes.items():
        if shape.get("type") != "array":
            continue
        arr = slots.get(slot_name)
        if not isinstance(arr, list):
            errors.append(f"slot {page_type}.{slot_name} must be an array, got {type(arr).__name__}")
            continue
        min_items = shape.get("min_items", 0)
        max_items = shape.get("max_items", 0)
        actual = len(arr)
        if actual < min_items or actual > max_items:
            errors.append(
                f"slot {page_type}.{slot_name}: expected {min_items}-{max_items} items, got {actual}"
            )
        item_slots = shape.get("item_slots", [])
        for i, item in enumerate(arr):
            if item_slots:
                if not isinstance(item, dict):
                    errors.append(f"slot {page_type}.{slot_name}[{i}]: must be dict, got {type(item).__name__}")
                    continue
                for islot in item_slots:
                    if islot not in item:
                        errors.append(f"slot {page_type}.{slot_name}[{i}]: missing sub-slot {islot!r}")

    return errors


def _check_slots_for_html_css(slots: dict, page_type: str) -> list[str]:
    errors: list[str] = []

    def _check(value: Any, path: str) -> None:
        if isinstance(value, str):
            if _contains_html_css(value):
                errors.append(f"HTML/CSS content in slot {path}: {value[:80]!r}...")
        elif isinstance(value, dict):
            for k, v in value.items():
                _check(v, f"{path}.{k}")
        elif isinstance(value, list):
            for i, v in enumerate(value):
                _check(v, f"{path}[{i}]")

    for key, val in slots.items():
        _check(val, f"{page_type}.{key}")
    return errors


# ============================================================================
# Budget checking
# ============================================================================


def _get_top_level_budget(page_type: str, slot_name: str) -> dict[str, Any] | None:
    page_budgets = _PAGE_TYPE_TOP_LEVEL_BUDGETS.get(page_type, {})
    return page_budgets.get(slot_name) or _TOP_LEVEL_BUDGETS.get(slot_name)


def _get_item_budget(page_type: str, slot_name: str, item_slot_name: str) -> dict[str, Any] | None:
    page_budgets = _PAGE_TYPE_ITEM_BUDGETS.get(page_type, {})
    slot_budgets = page_budgets.get(slot_name, {})
    return (
        slot_budgets.get(item_slot_name)
        or _ITEM_BUDGETS.get(item_slot_name)
        or _TOP_LEVEL_BUDGETS.get(item_slot_name)
        or _CELL_BUDGETS.get(item_slot_name)
    )


def _get_primitive_array_budget(page_type: str, slot_name: str) -> dict[str, Any] | None:
    page_budgets = _PAGE_TYPE_ITEM_BUDGETS.get(page_type, {})
    slot_budgets = page_budgets.get(slot_name, {})
    return slot_budgets.get(slot_name) or _CELL_BUDGETS.get(slot_name)


def _check_slide_budget(
    slots: dict,
    page_type: str,
    slide_types: dict,
) -> list[dict]:
    failures: list[dict] = []
    slot_shapes = _get_slot_shapes(page_type, slide_types)

    def _check_str(value: str, path: str, max_chars: int, desc: str) -> None:
        if isinstance(value, str) and len(value) > max_chars:
            failures.append({
                "path": path,
                "current_len": len(value),
                "max_chars": max_chars,
                "description": desc,
            })

    for slot_name, slot_value in slots.items():
        # Top-level string slot
        budget = _get_top_level_budget(page_type, slot_name)
        if budget and isinstance(slot_value, str):
            _check_str(slot_value, f"{page_type}.{slot_name}",
                       budget["max_chars"], budget["description"])

        # Array slot
        if isinstance(slot_value, list):
            shape = slot_shapes.get(slot_name, {})
            item_slot_names = shape.get("item_slots", [])

            if not item_slot_names:
                # Primitive array (e.g., matrix.columns, chart legend/x_labels/y_labels, process timeline)
                cell_budget = _get_primitive_array_budget(page_type, slot_name)
                if cell_budget:
                    for i, item in enumerate(slot_value):
                        if isinstance(item, str):
                            _check_str(item, f"{page_type}.{slot_name}[{i}]",
                                       cell_budget["max_chars"], cell_budget["description"])
            else:
                # Array of dicts with declared sub-slots
                for i, item in enumerate(slot_value):
                    if not isinstance(item, dict):
                        continue
                    for islot in item_slot_names:
                        islot_val = item.get(islot)
                        if isinstance(islot_val, str):
                            cb = _get_item_budget(page_type, slot_name, islot)
                            if cb:
                                _check_str(islot_val, f"{page_type}.{slot_name}[{i}].{islot}",
                                           cb["max_chars"], cb["description"])
                        elif isinstance(islot_val, list):
                            cb = _get_item_budget(page_type, slot_name, islot) or _CELL_BUDGETS.get("cells")
                            if cb:
                                for j, cell in enumerate(islot_val):
                                    if isinstance(cell, str):
                                        _check_str(cell, f"{page_type}.{slot_name}[{i}].{islot}[{j}]",
                                                   cb["max_chars"], cb["description"])
                    # extra keys not in item_slots
                    for ek, ev in item.items():
                        if ek in item_slot_names:
                            continue
                        if ek in _ATTRIBUTION_META_FIELDS:
                            continue
                        if isinstance(ev, str):
                            cb = _get_item_budget(page_type, slot_name, ek)
                            if cb:
                                _check_str(ev, f"{page_type}.{slot_name}[{i}].{ek}",
                                           cb["max_chars"], cb["description"])
                        elif isinstance(ev, list):
                            for j, cell in enumerate(ev):
                                if isinstance(cell, str):
                                    cb = _get_item_budget(page_type, slot_name, ek) or _CELL_BUDGETS.get("cells")
                                    if cb:
                                        _check_str(cell, f"{page_type}.{slot_name}[{i}].{ek}[{j}]",
                                                   cb["max_chars"], cb["description"])

    return failures


# ============================================================================
# Prompt builders
# ============================================================================


def _build_slide_types_compact(slide_types: dict) -> str:
    lines: list[str] = []
    for pt in slide_types.get("page_types", []):
        t = pt["type"]
        purpose_cn = pt.get("purpose_cn", "")
        slots = pt.get("slots", [])
        shapes = pt.get("slot_shapes", {})
        best_for = pt.get("best_for", [])
        adaptation = pt.get("adaptation_notes_cn", "")

        lines.append(f"## {t}")
        if purpose_cn:
            lines.append(f"  中文说明: {purpose_cn}")
        lines.append(f"  Best for: {', '.join(best_for)}")
        lines.append(f"  Slots: {json.dumps(slots, ensure_ascii=False)}")
        if shapes:
            lines.append(f"  Slot shapes: {json.dumps(shapes, ensure_ascii=False)}")
        if adaptation:
            lines.append(f"  Adaptation: {adaptation}")
        lines.append("")
    return "\n".join(lines)


def _build_text_budget_compact() -> str:
    lines: list[str] = ["## Text Budget (character limits)"]
    lines.append("### Top-level slots:")
    for name, info in _TOP_LEVEL_BUDGETS.items():
        lines.append(f"  {name}: max {info['max_chars']} chars ({info['description']})")
    lines.append("### Card / item sub-slots:")
    for name, info in _ITEM_BUDGETS.items():
        lines.append(f"  {name}: max {info['max_chars']} chars ({info['description']})")
    lines.append("### Cell / list entries:")
    for name, info in _CELL_BUDGETS.items():
        lines.append(f"  {name}: max {info['max_chars']} chars ({info['description']})")
    lines.append("### Page-specific layout overrides (use these when present):")
    for page_type, slot_infos in _PAGE_TYPE_TOP_LEVEL_BUDGETS.items():
        for slot_name, info in slot_infos.items():
            lines.append(
                f"  {page_type}.{slot_name}: max {info['max_chars']} chars ({info['description']})"
            )
    for page_type, slot_infos in _PAGE_TYPE_ITEM_BUDGETS.items():
        for slot_name, item_infos in slot_infos.items():
            for item_slot_name, info in item_infos.items():
                lines.append(
                    f"  {page_type}.{slot_name}[*].{item_slot_name}: "
                    f"max {info['max_chars']} chars ({info['description']})"
                )
    return "\n".join(lines)


def _format_source_slide(source_slide: dict) -> str:
    allowed_keys = {
        "slide_id", "outline_node_id", "title", "subtitle",
        "bullets", "blocks", "content", "layout_plan",
    }
    compact = {k: v for k, v in source_slide.items() if k in allowed_keys}
    return json.dumps(compact, ensure_ascii=False, indent=2)


def _build_initial_prompt(source_slide: dict, slide_types: dict) -> str:
    parts: list[str] = []
    parts.append(_INITIAL_PROMPT_HEADER)
    parts.append("")
    parts.append("=" * 60)
    parts.append("PIN & PAPER SLIDE-TYPES CONTRACT")
    parts.append("=" * 60)
    parts.append(_build_slide_types_compact(slide_types))
    parts.append("=" * 60)
    parts.append("TEXT BUDGET")
    parts.append("=" * 60)
    parts.append(_build_text_budget_compact())
    parts.append("")
    parts.append("=" * 60)
    parts.append("MULTI-POINT ROUTING RULES")
    parts.append("=" * 60)
    parts.append("2 key_points  → notes_2up  (fixed 2-card spacious layout)")
    parts.append("4 key_points  → notes_4up  (fixed 2x2 grid, four equal cards)")
    parts.append("3 key_points  → notes or notice (depending on overview length)")
    parts.append("5+ key_points → cta (preferred) or agenda.  NEVER use notes/notes_2up/notes_4up for >=5 items.")
    parts.append("process = 3-6 steps.")
    parts.append("")
    parts.append("=" * 60)
    parts.append("OUTPUT SCHEMA")
    parts.append("=" * 60)
    parts.append("""Return ONE slide entry (not the full deck envelope):
{
  "source_slide_id": "<slide_id from input>",
  "source_outline_node_id": "<outline_node_id from input>",
  "page_type": "<one of the 13 Pin & Paper types>",
  "rationale": "<one short sentence in Chinese: why this page_type>",
  "slots": {
    ... fill all declared slots for this page_type ...
    // Array items (cards, steps, columns) MUST include:
    // "source_block_ids": ["block_id_1", ...]
  },
  "content_warnings": []
}""")
    parts.append("")
    parts.append("=" * 60)
    parts.append("SOURCE SLIDE")
    parts.append("=" * 60)
    parts.append(_format_source_slide(source_slide))
    parts.append("")
    parts.append("Return JSON only.  No markdown fences.  No extra commentary.")
    return "\n".join(parts)


def _build_compression_prompt(
    source_slide: dict,
    first_page_plan: dict,
    budget_failures: list[dict],
    slide_types: dict,
) -> str:
    parts: list[str] = []
    parts.append(_COMPRESSION_PROMPT_HEADER)
    parts.append("")
    parts.append("=" * 60)
    parts.append("SOURCE SLIDE")
    parts.append("=" * 60)
    parts.append(_format_source_slide(source_slide))
    parts.append("")
    parts.append("=" * 60)
    parts.append("YOUR PREVIOUS PAGE PLAN (the one that exceeded budgets)")
    parts.append("=" * 60)
    parts.append(json.dumps(first_page_plan, ensure_ascii=False, indent=2))
    parts.append("")
    parts.append("=" * 60)
    parts.append("BUDGET FAILURES")
    parts.append("=" * 60)
    for bf in budget_failures:
        parts.append(
            f"  {bf['path']}: {bf['current_len']} chars (max {bf['max_chars']}) — {bf['description']}"
        )
    parts.append("")
    parts.append("=" * 60)
    parts.append("TEXT BUDGET (same as before)")
    parts.append("=" * 60)
    parts.append(_build_text_budget_compact())
    parts.append("")
    parts.append("Return the COMPLETE revised slide entry as JSON only.")
    parts.append("Keep the same page_type.  Compress aggressively to meet budgets.")
    parts.append("Do NOT invent new medical facts.  Do NOT output HTML/CSS.")
    return "\n".join(parts)


# ============================================================================
# Item-count page_type invariant check
# ============================================================================


def _count_source_key_points(source_slide: dict) -> int:
    """Count key_points in the source slide."""
    kps = source_slide.get("content", {}).get("key_points", [])
    if isinstance(kps, list):
        return len(kps)
    return 0


def _check_item_count_invariant(page_type: str, n_kp: int) -> list[str]:
    """Return a list of invariant violations for (page_type, n_kp).

    Empty list = invariant satisfied.
    """
    violations: list[str] = []

    if page_type == "notes_2up":
        if n_kp != 2:
            violations.append(
                f"notes_2up requires exactly 2 key_points, but source has {n_kp}"
            )
    elif page_type == "notes_4up":
        if n_kp != 4:
            violations.append(
                f"notes_4up requires exactly 4 key_points, but source has {n_kp}"
            )
    elif page_type in ("notes",):
        if n_kp >= 5:
            violations.append(
                f"notes accepts max 4 cards, but source has {n_kp} key_points — "
                f"use cta/agenda/process instead"
            )

    return violations


def _get_card_constraint(page_type: str) -> str:
    """Human-readable card constraint for a given page_type."""
    constraints = {
        "notes_2up": "exactly 2 cards (fixed 2-card layout)",
        "notes_4up": "exactly 4 cards (fixed 2x2 grid)",
        "notes": "2-4 cards",
        "cta": "3-6 steps",
        "agenda": "3-6 items",
        "process": "3-6 steps",
    }
    return constraints.get(page_type, "unknown card count")


def _build_correction_prompt(
    source_slide: dict,
    wrong_page_type: str,
    n_key_points: int,
    slide_types: dict,
    previous_page_plan: dict | None = None,
) -> str:
    """Build a route-correction retry prompt."""
    card_constraint = _get_card_constraint(wrong_page_type)
    parts: list[str] = []
    parts.append(
        _CORRECTION_PROMPT_HEADER.format(
            wrong_page_type=wrong_page_type,
            n_key_points=n_key_points,
            card_constraint=card_constraint,
        )
    )
    parts.append("")
    target_page_type = {
        2: "notes_2up",
        3: "notes",
        4: "notes_4up",
    }.get(n_key_points, "cta" if n_key_points >= 5 else "")
    if target_page_type:
        target_slots = _get_page_type_slot_names(target_page_type, slide_types)
        target_shapes = _get_slot_shapes(target_page_type, slide_types)
        parts.append("=" * 60)
        parts.append("MANDATORY TARGET FOR THIS RETRY")
        parts.append("=" * 60)
        parts.append(f'The corrected page_type MUST be "{target_page_type}".')
        parts.append(
            "The slots value MUST be a non-empty JSON object with exactly these "
            f"top-level keys: {json.dumps(target_slots, ensure_ascii=False)}"
        )
        for slot_name, shape in target_shapes.items():
            item_slots = list(shape.get("item_slots", []))
            item_count = n_key_points if slot_name in {"cards", "steps", "columns"} else None
            if item_count is not None:
                parts.append(
                    f'Populate slots.{slot_name} with exactly {item_count} items. '
                    f"Each item must contain {json.dumps(item_slots, ensure_ascii=False)} "
                    'plus "source_block_ids".'
                )
        parts.append("Do not return an empty slots object or an array for slots.")
        parts.append("")
    if previous_page_plan:
        parts.append("=" * 60)
        parts.append("YOUR PREVIOUS FULL PAGE PLAN (reuse its sourced content)")
        parts.append("=" * 60)
        parts.append(json.dumps(previous_page_plan, ensure_ascii=False, indent=2))
        parts.append("")
    parts.append("=" * 60)
    parts.append("SOURCE SLIDE")
    parts.append("=" * 60)
    parts.append(_format_source_slide(source_slide))
    parts.append("")
    parts.append("=" * 60)
    parts.append("PIN & PAPER SLIDE-TYPES CONTRACT (same as before)")
    parts.append("=" * 60)
    parts.append(_build_slide_types_compact(slide_types))
    parts.append("")
    parts.append("Return the COMPLETE revised slide entry with a CORRECTED page_type as JSON only.")
    parts.append("Do NOT invent new medical facts.  Do NOT output HTML/CSS.")
    return "\n".join(parts)


# ============================================================================
# LLM response parsing
# ============================================================================


def _parse_llm_response(raw: str) -> dict:
    text = raw.strip()
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if not fence_match:
        fence_match = re.search(r"```(?:json)?\s*\n?(.*)$", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM response is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"Expected JSON object, got {type(parsed).__name__}")
    return parsed


# ============================================================================
# Public API
# ============================================================================


def plan_pin_and_paper_slide(
    source_slide: dict,
    slide_types: dict | None = None,
    llm_client: Callable[[str], str] | None = None,
    slide_types_path: str | None = None,
    max_retries: int = 1,
) -> dict:
    """Plan one Pin & Paper slide from a ``slides.v1`` source slide.

    Parameters
    ----------
    source_slide:
        A single slide dict from ``slides.v1``.
    slide_types:
        Pre-loaded Pin & Paper slide-types dict.  If None, loaded from default path.
    llm_client:
        Injectable callable ``(prompt: str) -> str``.
    slide_types_path:
        Optional override path to Pin & Paper ``slide-types.json``.
    max_retries:
        Maximum compression retries (default 1).

    Returns
    -------
    dict with keys: ``slide_plan``, ``warnings``, ``llm_calls``.
    """
    if llm_client is None:
        raise ValueError("llm_client is required — inject a callable (prompt) -> str")

    if slide_types is None:
        slide_types = load_slide_types(slide_types_path)

    warnings: list[str] = []
    llm_calls = 0

    source_slide_id = source_slide.get("slide_id", "")
    source_oid = source_slide.get("outline_node_id", "")

    # ---- Phase 1: initial plan ----------------------------------------------
    prompt = _build_initial_prompt(source_slide, slide_types)
    try:
        raw = llm_client(prompt)
    except Exception as exc:
        return {
            "slide_plan": {
                "source_slide_id": source_slide_id,
                "source_outline_node_id": source_oid,
                "page_type": "",
                "rationale": "",
                "slots": {},
                "content_warnings": [],
            },
            "warnings": [f"llm_call_failed: {exc}"],
            "llm_calls": 0,
        }
    llm_calls += 1

    try:
        plan = _parse_llm_response(raw)
    except ValueError as exc:
        return {
            "slide_plan": {
                "source_slide_id": source_slide_id,
                "source_outline_node_id": source_oid,
                "page_type": "",
                "rationale": "",
                "slots": {},
                "content_warnings": [],
            },
            "warnings": [f"llm_response_parse_failed: {exc}"],
            "llm_calls": llm_calls,
        }

    page_type = plan.get("page_type", "")
    slots = plan.get("slots", {})
    if not isinstance(slots, dict):
        slots = {}
    content_warnings = plan.get("content_warnings", [])
    if not isinstance(content_warnings, list):
        content_warnings = []
    rationale = plan.get("rationale", "")

    slide_plan = {
        "source_slide_id": plan.get("source_slide_id", source_slide_id),
        "source_outline_node_id": plan.get("source_outline_node_id", source_oid),
        "page_type": page_type,
        "rationale": rationale,
        "slots": slots,
        "content_warnings": list(content_warnings),
    }

    # Validate page_type. An invented type is recoverable through the same
    # full-page correction used for item-count routing violations.
    pt_errors = _validate_page_type(page_type, slide_types)
    for e in pt_errors:
        warnings.append(e)

    # Validate slots
    slot_errors = [] if pt_errors else _validate_slots(page_type, slots, slide_types)
    if slot_errors:
        for e in slot_errors:
            warnings.append(e)
        fatal = [e for e in slot_errors if "missing slot" in e or "HTML/CSS" in e]
        if fatal:
            return {"slide_plan": slide_plan, "warnings": warnings, "llm_calls": llm_calls}

    # ---- Phase 1.5: route correction retry (item-count invariant) -------------
    n_kp = _count_source_key_points(source_slide)
    invariant_violations = (
        [] if pt_errors else _check_item_count_invariant(page_type, n_kp)
    )
    if pt_errors or invariant_violations:
        # Trigger a route-correction retry
        reasons = [*pt_errors, *invariant_violations]
        warnings.append(
            f"route_correction_retry: {source_slide_id} — "
            f"page_type={page_type!r} for {n_kp} KP: {'; '.join(reasons)}"
        )
        correction_prompt = _build_correction_prompt(
            source_slide,
            page_type,
            n_kp,
            slide_types,
            previous_page_plan=slide_plan,
        )
        try:
            raw_corr = llm_client(correction_prompt)
        except Exception as exc:
            warnings.append(f"correction_llm_call_failed: {exc}")
            return {"slide_plan": slide_plan, "warnings": warnings, "llm_calls": llm_calls}
        llm_calls += 1

        try:
            plan_corr = _parse_llm_response(raw_corr)
        except ValueError as exc:
            warnings.append(f"correction_response_parse_failed: {exc}")
            return {"slide_plan": slide_plan, "warnings": warnings, "llm_calls": llm_calls}

        page_type = plan_corr.get("page_type", page_type)
        slots = plan_corr.get("slots", {})
        if not isinstance(slots, dict):
            slots = {}
        content_warnings = plan_corr.get("content_warnings", [])
        if not isinstance(content_warnings, list):
            content_warnings = []
        rationale = plan_corr.get("rationale", rationale)

        slide_plan = {
            "source_slide_id": plan_corr.get("source_slide_id", source_slide_id),
            "source_outline_node_id": plan_corr.get("source_outline_node_id", source_oid),
            "page_type": page_type,
            "rationale": rationale,
            "slots": slots,
            "content_warnings": list(content_warnings),
        }

        # Re-validate page_type
        pt_errors = _validate_page_type(page_type, slide_types)
        if pt_errors:
            for e in pt_errors:
                warnings.append(e)
            return {"slide_plan": slide_plan, "warnings": warnings, "llm_calls": llm_calls}

        # Re-validate slots
        slot_errors = _validate_slots(page_type, slots, slide_types)
        if slot_errors:
            for e in slot_errors:
                warnings.append(e)
            fatal = [e for e in slot_errors if "missing slot" in e or "HTML/CSS" in e]
            if fatal:
                return {"slide_plan": slide_plan, "warnings": warnings, "llm_calls": llm_calls}

        # Re-check invariant after correction
        invariant_violations2 = _check_item_count_invariant(page_type, n_kp)
        if invariant_violations2:
            for v in invariant_violations2:
                warnings.append(
                    f"route_correction_failed: {source_slide_id} — {v}"
                )
            return {"slide_plan": slide_plan, "warnings": warnings, "llm_calls": llm_calls}

    # Check budgets
    budget_failures = _check_slide_budget(slots, page_type, slide_types)

    if not budget_failures:
        return {"slide_plan": slide_plan, "warnings": warnings, "llm_calls": llm_calls}

    # ---- Phase 2: compression retry -----------------------------------------
    if llm_calls >= max_retries + 1:
        warnings.append(f"pin_and_paper_over_budget_after_retry: {source_slide_id}")
        return {"slide_plan": slide_plan, "warnings": warnings, "llm_calls": llm_calls}

    compress_prompt = _build_compression_prompt(
        source_slide, slide_plan, budget_failures, slide_types,
    )
    try:
        raw2 = llm_client(compress_prompt)
    except Exception as exc:
        warnings.append(f"compression_llm_call_failed: {exc}")
        warnings.append(f"pin_and_paper_over_budget_after_retry: {source_slide_id}")
        return {"slide_plan": slide_plan, "warnings": warnings, "llm_calls": llm_calls}
    llm_calls += 1

    try:
        plan2 = _parse_llm_response(raw2)
    except ValueError as exc:
        warnings.append(f"compression_response_parse_failed: {exc}")
        warnings.append(f"pin_and_paper_over_budget_after_retry: {source_slide_id}")
        return {"slide_plan": slide_plan, "warnings": warnings, "llm_calls": llm_calls}

    page_type2 = plan2.get("page_type", page_type)
    slots2 = plan2.get("slots", {})
    if not isinstance(slots2, dict):
        slots2 = {}
    content_warnings2 = plan2.get("content_warnings", [])

    slide_plan2 = {
        "source_slide_id": plan2.get("source_slide_id", source_slide_id),
        "source_outline_node_id": plan2.get("source_outline_node_id", source_oid),
        "page_type": page_type2,
        "rationale": plan2.get("rationale", rationale),
        "slots": slots2,
        "content_warnings": list(content_warnings2) if isinstance(content_warnings2, list) else [],
    }

    pt_errors2 = _validate_page_type(page_type2, slide_types)
    for e in pt_errors2:
        warnings.append(e)

    slot_errors2 = _validate_slots(page_type2, slots2, slide_types)
    for e in slot_errors2:
        warnings.append(e)

    budget_failures2 = _check_slide_budget(slots2, page_type2, slide_types)
    if budget_failures2:
        warnings.append(f"pin_and_paper_over_budget_after_retry: {source_slide_id}")
        for bf in budget_failures2:
            slide_plan2.setdefault("content_warnings", []).append(
                f"over_budget: {bf['path']} ({bf['current_len']}/{bf['max_chars']})"
            )

    return {"slide_plan": slide_plan2, "warnings": warnings, "llm_calls": llm_calls}


def plan_pin_and_paper_deck(
    deck: dict,
    slide_types: dict | None = None,
    llm_client: Callable[[str], str] | None = None,
    slide_types_path: str | None = None,
    max_retries: int = 1,
) -> dict:
    """Plan Pin & Paper slides for every slide in a ``slides.v1`` deck.

    Parameters
    ----------
    deck:
        A ``slides.v1`` dict with at least ``slides``, ``session_id``, ``deck_title``.
    slide_types:
        Pre-loaded Pin & Paper slide-types dict.
    llm_client:
        Injectable LLM client callable.
    slide_types_path:
        Optional override path to ``slide-types.json``.
    max_retries:
        Maximum compression retries per slide (default 1).

    Returns
    -------
    A ``pin_and_paper_slide_plan.v1`` dict.
    """
    if llm_client is None:
        raise ValueError("llm_client is required — inject a callable (prompt) -> str")

    if slide_types is None:
        slide_types = load_slide_types(slide_types_path)

    all_warnings: list[str] = list(deck.get("warnings", []))
    total_llm_calls = 0
    slides: list[dict] = []

    source_slides = deck.get("slides", [])
    for source_slide in source_slides:
        result = plan_pin_and_paper_slide(
            source_slide=source_slide,
            slide_types=slide_types,
            llm_client=llm_client,
            max_retries=max_retries,
        )
        slides.append(result["slide_plan"])
        all_warnings.extend(result["warnings"])
        total_llm_calls += result["llm_calls"]

    seen: set[str] = set()
    deduped: list[str] = []
    for w in all_warnings:
        if w not in seen:
            seen.add(w)
            deduped.append(w)

    return {
        "schema_version": "pin_and_paper_slide_plan.v1",
        "template_slug": "pin-and-paper",
        "deck_title": deck.get("deck_title", ""),
        "session_id": deck.get("session_id", ""),
        "slides": slides,
        "warnings": deduped,
        "_llm_calls": total_llm_calls,
    }
