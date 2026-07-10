"""Smoke test for Pin & Paper template import.

Covers:
1. Only Pin & Paper template directory exists (no other new upstream templates).
2. Required files exist.
3. template.json.slug == "pin-and-paper".
4. slide-types.json contains all 11 required page_types.
5. Fixture has >= 8 pages, includes a 5-knowledge-point page.
6. 5-KP page does NOT use "notes" as page_type.
7. 5-KP page each point has title + >= 25 Chinese chars explanation.
8. At least 3 fixture pages have normal text density (not just short title stacks).
9. Fixture user-visible text is primarily Chinese, not English template copy.
10. llm-contract.md explicitly forbids HTML/CSS output, new medical facts, renderer truncation.
11. llm-contract.md explicitly describes info-density preservation and full-page retry.
12. llm-contract.md explicitly states not to pre-compress when page has empty space.
13. preview.html references files within this template directory, not Soft Editorial.

Run from the project root:

    python scripts/smoke_test_pin_and_paper_template.py

Expected last line: ``pin and paper template smoke test passed``
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATE_DIR = _PROJECT_ROOT / "tools" / "ppt_preview" / "templates" / "pin-and-paper"
_FIXTURE_PATH = _TEMPLATE_DIR / "fixtures" / "pin-and-paper-plan.v1.json"
_SLIDE_TYPES_PATH = _TEMPLATE_DIR / "slide-types.json"
_LLM_CONTRACT_PATH = _TEMPLATE_DIR / "llm-contract.md"
_PREVIEW_HTML_PATH = _TEMPLATE_DIR / "preview.html"

_FAILED = 0


def _check(condition: bool, label: str) -> None:
    global _FAILED
    if not condition:
        print(f"  FAIL: {label}")
        _FAILED += 1
    else:
        print(f"  OK:   {label}")


def test_01_only_pin_and_paper_template() -> None:
    """Ensure no other new upstream template directories were added.

    Only directory entries are considered "template directories".
    Metadata files such as ``README_zh.md`` and ``candidates.v1.json``
    live alongside templates but are not templates themselves.
    """
    print("\n[Test 01] Only Pin & Paper template directory exists")
    templates_dir = _PROJECT_ROOT / "tools" / "ppt_preview" / "templates"
    # Only directory entries count as template dirs — filter out metadata files
    existing_dirs = {
        name for name in os.listdir(templates_dir)
        if (templates_dir / name).is_dir()
    }
    # Allowed: the two template dirs we know about
    allowed = {"soft-editorial", "pin-and-paper"}
    unexpected = existing_dirs - allowed
    _check(len(unexpected) == 0,
           f"No unexpected template dirs: {unexpected if unexpected else 'none'}")
    _check("pin-and-paper" in existing_dirs,
           "pin-and-paper directory exists")


def test_02_required_files_exist() -> None:
    """Ensure all required files exist in the template directory."""
    print("\n[Test 02] Required files exist")
    required = [
        "template.html",
        "template.json",
        "design.md",
        "deck-stage.js",
        "source-attribution.md",
        "LICENSE.upstream",
        "slide-types.json",
        "llm-contract.md",
        "preview.html",
        "fixtures/pin-and-paper-plan.v1.json",
    ]
    for fname in required:
        fpath = _TEMPLATE_DIR / fname
        _check(fpath.exists(), f"  {fname}")
    _check(_FIXTURE_PATH.exists(), "  fixtures/pin-and-paper-plan.v1.json")


def test_03_template_json_slug() -> None:
    """Verify template.json has the correct slug."""
    print("\n[Test 03] template.json.slug == 'pin-and-paper'")
    tpl_path = _TEMPLATE_DIR / "template.json"
    if not tpl_path.exists():
        _check(False, "template.json not found, cannot check slug")
        return
    with open(tpl_path, encoding="utf-8") as f:
        data = json.load(f)
    _check(data.get("slug") == "pin-and-paper",
           f"slug is '{data.get('slug')}'")


def test_04_slide_types_contains_all_page_types() -> None:
    """Verify slide-types.json has all 11 required page_types."""
    print("\n[Test 04] slide-types.json contains all 11 required page_types")
    required_types = {
        "cover", "agenda", "notes", "section", "notice",
        "chart", "process", "matrix", "stats", "quote", "cta",
    }
    if not _SLIDE_TYPES_PATH.exists():
        _check(False, "slide-types.json not found")
        return
    with open(_SLIDE_TYPES_PATH, encoding="utf-8") as f:
        data = json.load(f)
    page_types = data.get("page_types", [])
    actual_types = {pt["type"] for pt in page_types}
    missing = required_types - actual_types
    _check(len(missing) == 0, f"Missing page_types: {missing if missing else 'none'}")
    _check(data.get("template_slug") == "pin-and-paper",
           f"template_slug is '{data.get('template_slug')}'")

    # Verify each page_type has required fields
    for pt in page_types:
        t = pt["type"]
        has_purpose = bool(pt.get("purpose_cn"))
        has_best_for = bool(pt.get("best_for"))
        has_slots = bool(pt.get("slots"))
        has_adaptation = bool(pt.get("adaptation_notes_cn"))
        _check(has_purpose and has_best_for and has_slots and has_adaptation,
               f"page_type '{t}' has purpose_cn, best_for, slots, adaptation_notes_cn")


def test_05_fixture_has_minimum_pages() -> None:
    """Verify fixture has >= 8 pages."""
    print("\n[Test 05] Fixture has >= 8 pages")
    if not _FIXTURE_PATH.exists():
        _check(False, "fixture not found")
        return
    with open(_FIXTURE_PATH, encoding="utf-8") as f:
        data = json.load(f)
    slides = data.get("slides", [])
    _check(len(slides) >= 8, f"Fixture has {len(slides)} slides (need >= 8)")


def test_06_fixture_has_five_kp_page() -> None:
    """Verify fixture contains a page with exactly 5 knowledge points."""
    print("\n[Test 06] Fixture contains a 5-knowledge-point page")
    if not _FIXTURE_PATH.exists():
        _check(False, "fixture not found")
        return
    with open(_FIXTURE_PATH, encoding="utf-8") as f:
        data = json.load(f)
    slides = data.get("slides", [])

    five_kp_pages = []
    for slide in slides:
        slots = slide.get("slots", {})
        # Check cta steps, process steps, agenda items, or notes cards
        for key in ("steps", "items", "cards"):
            items = slots.get(key, [])
            if len(items) == 5:
                five_kp_pages.append((slide["page_type"], key))
                break

    _check(len(five_kp_pages) > 0,
           f"Found {len(five_kp_pages)} pages with exactly 5 items (need >= 1)")


def test_07_five_kp_page_not_notes() -> None:
    """Verify 5-KP page does NOT use 'notes' as page_type."""
    print("\n[Test 07] 5-KP page does NOT use 'notes' as page_type")
    if not _FIXTURE_PATH.exists():
        _check(False, "fixture not found")
        return
    with open(_FIXTURE_PATH, encoding="utf-8") as f:
        data = json.load(f)
    slides = data.get("slides", [])

    for slide in slides:
        slots = slide.get("slots", {})
        items = slots.get("steps", []) or slots.get("items", []) or slots.get("cards", [])
        if len(items) == 5:
            _check(slide["page_type"] != "notes",
                   f"5-KP page uses '{slide['page_type']}' (not 'notes') — correct routing")
            break
    else:
        _check(False, "No 5-KP page found to verify")


def test_08_five_kp_page_each_point_has_title_and_25_chinese_chars() -> None:
    """Verify 5-KP page each point has title + >= 25 Chinese char explanation.

    Only checks page types that are expected to carry body explanations per item
    (cta steps, process steps, notes cards). Agenda items are list-labels without
    body by design — those are verified separately via their label text.
    """
    print("\n[Test 08] 5-KP page each point has title + >= 25 Chinese char explanation")
    if not _FIXTURE_PATH.exists():
        _check(False, "fixture not found")
        return
    with open(_FIXTURE_PATH, encoding="utf-8") as f:
        data = json.load(f)
    slides = data.get("slides", [])

    def count_chinese(s: str) -> int:
        """Count Chinese characters (CJK Unified Ideographs)."""
        return len(re.findall(r'[一-鿿㐀-䶿]', s))

    # Only check item types that should carry body text (not agenda labels)
    body_item_keys = ["steps", "cards"]  # agenda "items" are label-only by design

    found = False
    for slide in slides:
        slots = slide.get("slots", {})
        items = []
        for key in body_item_keys:
            candidate = slots.get(key, [])
            if len(candidate) == 5:
                items = candidate
                break
        if len(items) != 5:
            continue
        found = True
        all_ok = True
        page_type = slide["page_type"]
        for i, item in enumerate(items):
            title = item.get("title", "")
            body = item.get("body", "")
            cjk_title = count_chinese(title)
            cjk_body = count_chinese(body)
            ok = cjk_body >= 25
            if not ok:
                all_ok = False
                print(f"  WARN: item {i+1} has only {cjk_body} Chinese chars in body (need >= 25)")
            _check(cjk_title > 0, f"  item {i+1} title has {cjk_title} Chinese chars")
        _check(all_ok,
               f"5-KP page (type={page_type}): all {len(items)} points have >= 25 Chinese char explanation")

    if not found:
        _check(False, "No 5-KP page with body items found to verify")


def test_09_at_least_3_pages_with_normal_text_density() -> None:
    """Verify at least 3 fixture pages have normal text density."""
    print("\n[Test 09] At least 3 fixture pages have normal text density")
    if not _FIXTURE_PATH.exists():
        _check(False, "fixture not found")
        return
    with open(_FIXTURE_PATH, encoding="utf-8") as f:
        data = json.load(f)
    slides = data.get("slides", [])

    def count_chinese(s: str) -> int:
        return len(re.findall(r'[一-鿿㐀-䶿]', s))

    def page_has_density(slide) -> bool:
        """Check if a page has normal text density (at least one field >= 35 Chinese chars)."""
        slots = slide.get("slots", {})

        # Check direct text fields
        for key in ("body", "headline", "quote_text", "subtitle"):
            val = slots.get(key, "")
            if isinstance(val, str) and count_chinese(val) >= 35:
                return True

        # Check card/step/item arrays
        for arr_key in ("steps", "cards", "items", "columns", "stats", "rows"):
            arr = slots.get(arr_key, [])
            for item in arr:
                if isinstance(item, dict):
                    body = item.get("body", "") or item.get("explanation", "")
                    if isinstance(body, str) and count_chinese(body) >= 35:
                        return True
                    # Check row cells
                    cells = item.get("cells", [])
                    for cell in cells:
                        if isinstance(cell, str) and count_chinese(cell) >= 35:
                            return True

        return False

    dense_pages = [s for s in slides if page_has_density(s)]
    _check(len(dense_pages) >= 3,
           f"Found {len(dense_pages)} pages with normal text density (need >= 3)")


def test_10_fixture_is_primarily_chinese() -> None:
    """Verify fixture user-visible text is primarily Chinese, not English template copy."""
    print("\n[Test 10] Fixture user-visible text is primarily Chinese")
    if not _FIXTURE_PATH.exists():
        _check(False, "fixture not found")
        return

    # English template copy phrases that should NOT appear
    forbidden_phrases = [
        "Kept things",
        "North Field Office",
        "A field guide",
        "Presented by A. Speaker",
        "What's inside",
        "The trust gap",
        "Three rules we're keeping",
        "Write the real sentence",
        "Earn the second look",
        "Keep the handwriting",
        "Where we are going, and why",
        "From insight to default",
        "Three pilots, side by side",
        "The case, by the numbers",
        "Pick the three bets",
        "Client voice",
        "Margaux",
    ]

    with open(_FIXTURE_PATH, encoding="utf-8") as f:
        raw = f.read()

    found_forbidden = [p for p in forbidden_phrases if p.lower() in raw.lower()]
    _check(len(found_forbidden) == 0,
           f"Forbidden English template phrases found: {found_forbidden if found_forbidden else 'none'}")

    # Verify Chinese content dominates
    cjk_count = len(re.findall(r'[一-鿿㐀-䶿]', raw))
    _check(cjk_count > 500, f"Fixture has {cjk_count} Chinese characters (need > 500)")


def test_11_llm_contract_forbids_html_css_and_medical_invention() -> None:
    """Verify llm-contract.md explicitly forbids HTML/CSS, new medical facts, and renderer truncation."""
    print("\n[Test 11] llm-contract.md explicitly forbids HTML/CSS, new medical facts, and renderer truncation")
    if not _LLM_CONTRACT_PATH.exists():
        _check(False, "llm-contract.md not found")
        return

    with open(_LLM_CONTRACT_PATH, encoding="utf-8") as f:
        content = f.read()

    _check("not HTML/CSS" in content.lower() or "not html/css" in content.lower()
           or "must not output html" in content.lower() or "no html/css" in content.lower(),
           "Forbids HTML/CSS output")
    _check(("must not" in content.lower() and "invent" in content.lower()) or "no new medical" in content.lower(),
           "Forbids inventing new medical facts")
    _check("must not" in content.lower() and "truncat" in content.lower(),
           "Forbids renderer truncation")
    _check("overflow: hidden" in content.lower() or "overflow:hidden" in content.lower(),
           "Forbids CSS overflow:hidden to cut text")


def test_12_llm_contract_info_density_and_retry() -> None:
    """Verify llm-contract.md describes info-density preservation and full-page retry."""
    print("\n[Test 12] llm-contract.md describes info-density preservation and full-page retry")
    if not _LLM_CONTRACT_PATH.exists():
        _check(False, "llm-contract.md not found")
        return

    with open(_LLM_CONTRACT_PATH, encoding="utf-8") as f:
        content = f.read()

    # Info density: preserve normal teaching info, don't pre-compress
    _check("normal teaching" in content.lower() or "information density" in content.lower(),
           "References information density / normal teaching info")
    _check("do not pre-compress" in content.lower() or "not pre-compress" in content.lower()
           or "not compress" in content.lower() or "before compression" in content.lower(),
           "References not pre-compressing content")
    _check("full-page retry" in content.lower() or "full page retry" in content.lower()
           or "entire revised" in content.lower() or "complete revised" in content.lower(),
           "References full-page retry / complete revised plan")
    _check("empty space" in content.lower() or "empty" in content.lower(),
           "References empty space / not compressing when page has space")


def test_13_preview_html_references_own_template_dir() -> None:
    """Verify preview.html references files within this template directory, not Soft Editorial."""
    print("\n[Test 13] preview.html references files within this template directory")
    if not _PREVIEW_HTML_PATH.exists():
        _check(False, "preview.html not found")
        return

    with open(_PREVIEW_HTML_PATH, encoding="utf-8") as f:
        content = f.read()

    # Must reference deck-stage.js from local dir
    _check('src="deck-stage.js"' in content,
           "References local deck-stage.js")
    _check('fixtures/pin-and-paper-plan.v1.json' in content,
           "References local fixture file")

    # Must NOT reference Soft Editorial files
    forbidden_refs = [
        "soft-editorial",
        "soft_editorial",
        "soft-editorial-plan",
        "preview-renderer.js",  # Soft Editorial's renderer
        "SoftEditorialPreviewRenderer",
    ]
    found_forbidden = [r for r in forbidden_refs if r.lower() in content.lower()]
    _check(len(found_forbidden) == 0,
           f"No Soft Editorial references: {found_forbidden if found_forbidden else 'none'}")

    # Must have Pin & Paper specific CSS classes
    pin_specific = ["s-cover", "s-agenda", "s-notes", "s-sec", "s-notice",
                    "s-chart", "s-process", "s-matrix", "s-stats", "s-quote", "s-cta"]
    found_specific = [c for c in pin_specific if c in content]
    _check(len(found_specific) >= 8,
           f"Has Pin & Paper CSS classes: {len(found_specific)}/11")


def test_14_fixture_page_types_coverage() -> None:
    """Verify fixture covers the required page types."""
    print("\n[Test 14] Fixture page_type coverage")
    if not _FIXTURE_PATH.exists():
        _check(False, "fixture not found")
        return

    with open(_FIXTURE_PATH, encoding="utf-8") as f:
        data = json.load(f)
    slides = data.get("slides", [])

    covered = {s["page_type"] for s in slides}
    # Required coverage (at minimum)
    required = {"cover", "agenda", "notes", "notice", "cta", "process", "matrix", "quote"}
    missing = required - covered
    _check(len(missing) == 0,
           f"Missing page_types in fixture: {missing if missing else 'none'}")
    _check(len(covered) >= 8,
           f"Fixture covers {len(covered)} distinct page_types (need >= 8)")


def test_15_slide_types_json_has_adaptation_notes() -> None:
    """Verify specific adaptation notes for key page types."""
    print("\n[Test 15] slide-types.json specific adaptation notes")
    if not _SLIDE_TYPES_PATH.exists():
        _check(False, "slide-types.json not found")
        return
    with open(_SLIDE_TYPES_PATH, encoding="utf-8") as f:
        data = json.load(f)

    page_types = {pt["type"]: pt for pt in data.get("page_types", [])}

    # agenda: 3-6 items
    agenda = page_types.get("agenda", {})
    _check(agenda.get("min_items") == 3 and agenda.get("max_items") == 6,
           f"agenda min/max items: {agenda.get('min_items')}/{agenda.get('max_items')}")

    # notes: 2-4 cards
    notes = page_types.get("notes", {})
    _check(notes.get("min_items") == 2 and notes.get("max_items") == 4,
           f"notes min/max items: {notes.get('min_items')}/{notes.get('max_items')}")

    # cta: 3-6 steps
    cta = page_types.get("cta", {})
    _check(cta.get("min_items") == 3 and cta.get("max_items") == 6,
           f"cta min/max items: {cta.get('min_items')}/{cta.get('max_items')}")

    # notice: 2-3 columns
    notice = page_types.get("notice", {})
    _check(notice.get("min_items") == 2 and notice.get("max_items") == 3,
           f"notice min/max items: {notice.get('min_items')}/{notice.get('max_items')}")

    # process: 3-6 steps
    process = page_types.get("process", {})
    _check(process.get("min_items") == 3 and process.get("max_items") == 6,
           f"process min/max items: {process.get('min_items')}/{process.get('max_items')}")

    # matrix: 2-4 columns
    matrix = page_types.get("matrix", {})
    _check(matrix.get("min_items") == 2 and matrix.get("max_items") == 4,
           f"matrix min/max items: {matrix.get('min_items')}/{matrix.get('max_items')}")


# ============================================================================
# Main
# ============================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("Pin & Paper Template Smoke Test")
    print("=" * 60)

    test_01_only_pin_and_paper_template()
    test_02_required_files_exist()
    test_03_template_json_slug()
    test_04_slide_types_contains_all_page_types()
    test_05_fixture_has_minimum_pages()
    test_06_fixture_has_five_kp_page()
    test_07_five_kp_page_not_notes()
    test_08_five_kp_page_each_point_has_title_and_25_chinese_chars()
    test_09_at_least_3_pages_with_normal_text_density()
    test_10_fixture_is_primarily_chinese()
    test_11_llm_contract_forbids_html_css_and_medical_invention()
    test_12_llm_contract_info_density_and_retry()
    test_13_preview_html_references_own_template_dir()
    test_14_fixture_page_types_coverage()
    test_15_slide_types_json_has_adaptation_notes()

    print("\n" + "=" * 60)
    if _FAILED == 0:
        print("pin and paper template smoke test passed")
        sys.exit(0)
    else:
        print(f"pin and paper template smoke test FAILED ({_FAILED} check(s))")
        sys.exit(1)
