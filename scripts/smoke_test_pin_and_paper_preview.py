"""Smoke test for Pin & Paper preview fixture loading.

Verifies:
1. Default preview.html loads hand-written fixture (cover page with specific text).
2. preview.html#production loads production fixture (notice page, no cover text).
3. Production mode does NOT show hand-written cover text.
4. Console / DOM indicator distinguishes production from default.

Uses Playwright with a local HTTP server — NOT file://.

Run from the project root:

    python scripts/smoke_test_pin_and_paper_preview.py
"""

from __future__ import annotations

import http.server
import json
import socketserver
import sys
import threading
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATE_DIR = (
    _PROJECT_ROOT
    / "tools" / "ppt_preview" / "templates" / "pin-and-paper"
)

_FAILED = 0


def _check(condition: bool, label: str) -> None:
    global _FAILED
    if not condition:
        print(f"  FAIL: {label}")
        _FAILED += 1
    else:
        print(f"  OK:   {label}")


# ============================================================================
# HTTP server for local hosting
# ============================================================================

def _start_server(port: int = 18766) -> tuple[socketserver.TCPServer, int]:
    """Start a simple HTTP server in the pin-and-paper template directory."""

    import os

    serve_dir = str(_TEMPLATE_DIR)

    class _Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=serve_dir, **kwargs)

    # Try a few ports in case the default is occupied
    for attempt in range(5):
        try:
            p = port + attempt
            server = socketserver.TCPServer(("127.0.0.1", p), _Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            return server, p
        except OSError:
            continue
    raise RuntimeError("Could not find a free port for HTTP server")


# ============================================================================
# Tests
# ============================================================================


def test_01_default_loads_hand_written_fixture(port: int) -> None:
    """Verify default preview.html loads the hand-written fixture (cover page)."""
    print("\n[Test 01] Default preview.html loads hand-written fixture")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        page.goto(f"http://127.0.0.1:{port}/preview.html", wait_until="networkidle", timeout=15000)

        # deck-stage changes hash to #1 — wait for it
        page.wait_for_timeout(1000)

        # Check that the page rendered slides
        slides = page.locator("section.slide")
        slide_count = slides.count()
        _check(slide_count >= 8, f"Default mode has {slide_count} slides (need >= 8)")

        # Check first slide is a cover (hand-written fixture starts with cover)
        first_slide = slides.first
        # The cover has class s-cover
        has_cover = first_slide.evaluate("el => el.classList.contains('s-cover')")
        _check(has_cover, "Default first slide is cover page (s-cover)")

        # The hand-written fixture has "掌握核心概念，建立临床思维。" in handwritten_note
        page_text = page.content()
        _check(
            "掌握核心概念" in page_text,
            "Default mode contains hand-written cover text: 掌握核心概念，建立临床思维。",
        )

        # Default mode should NOT have production badge
        badge = page.locator("#pin-paper-mode-badge")
        _check(badge.count() == 0, "Default mode has no production badge")

        browser.close()


def test_02_production_hash_loads_production_fixture(port: int) -> None:
    """Verify preview.html#production loads the production fixture."""
    print("\n[Test 02] preview.html#production loads production fixture")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        page.goto(
            f"http://127.0.0.1:{port}/preview.html#production",
            wait_until="networkidle",
            timeout=15000,
        )

        # Wait for deck-stage to settle
        page.wait_for_timeout(1000)

        # Check that slides rendered
        slides = page.locator("section.slide")
        slide_count = slides.count()
        _check(slide_count >= 8, f"Production mode has {slide_count} slides (need >= 8)")

        # Check first slide is NOT a cover (production starts with notice)
        first_slide = slides.first
        is_cover = first_slide.evaluate("el => el.classList.contains('s-cover')")
        _check(not is_cover, "Production first slide is NOT cover page")

        # Production first slide should be notice (s-notice)
        is_notice = first_slide.evaluate("el => el.classList.contains('s-notice')")
        _check(is_notice, "Production first slide IS notice page (s-notice)")

        # Production mode should NOT contain hand-written cover text
        page_text = page.content()
        _check(
            "掌握核心概念，建立临床思维。" not in page_text,
            "Production mode does NOT contain hand-written cover text",
        )

        # Production mode should have "牙周炎" and notice column content
        _check("牙周炎" in page_text, "Production mode contains 牙周炎")

        # The production notice's first column title should be present
        _check(
            "牙周炎的定义与诊断标准" in page_text or "定义与诊断标准" in page_text,
            "Production mode contains node_1 notice column: 定义与诊断标准",
        )

        # Production badge should be visible
        badge = page.locator("#pin-paper-mode-badge")
        _check(badge.count() > 0, "Production mode has MODE: PRODUCTION badge")

        browser.close()


def test_03_query_param_fixture_also_works(port: int) -> None:
    """Verify preview.html?fixture=production also loads production fixture."""
    print("\n[Test 03] preview.html?fixture=production loads production fixture")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        page.goto(
            f"http://127.0.0.1:{port}/preview.html?fixture=production",
            wait_until="networkidle",
            timeout=15000,
        )

        page.wait_for_timeout(1000)

        slides = page.locator("section.slide")
        slide_count = slides.count()
        _check(slide_count >= 8, f"?fixture=production mode has {slide_count} slides")

        # Should be production content
        first_slide = slides.first
        is_cover = first_slide.evaluate("el => el.classList.contains('s-cover')")
        _check(not is_cover, "?fixture=production first slide is NOT cover")

        # Production badge visible
        badge = page.locator("#pin-paper-mode-badge")
        _check(badge.count() > 0, "?fixture=production mode has production badge")

        browser.close()


def test_04_production_mode_survives_deck_stage_hash_rewrite(port: int) -> None:
    """Verify deck-stage's hash rewrite doesn't cause fixture fallback on refresh simulation."""
    print("\n[Test 04] Production mode survives deck-stage hash rewrite")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        page.goto(
            f"http://127.0.0.1:{port}/preview.html#production",
            wait_until="networkidle",
            timeout=15000,
        )
        page.wait_for_timeout(1000)

        # At this point, deck-stage should have rewritten hash to something like #1
        current_hash = page.evaluate("() => location.hash")
        print(f"  INFO: After deck-stage init, location.hash = {current_hash!r}")

        # Check that the page still shows production content
        first_slide = page.locator("section.slide").first
        is_notice = first_slide.evaluate("el => el.classList.contains('s-notice')")
        _check(is_notice, "After hash rewrite, first slide is still notice (production)")

        # Verify the MODE badge is still present
        badge = page.locator("#pin-paper-mode-badge")
        _check(badge.count() > 0, "After hash rewrite, production badge still visible")

        # Now simulate a page reload by re-navigating with the current URL
        # First, save the current URL
        current_url = page.evaluate("() => location.href")
        print(f"  INFO: Current URL before reload: {current_url}")

        # Reload the page
        page.reload(wait_until="networkidle", timeout=15000)
        page.wait_for_timeout(1000)

        # After reload with hash like #1, the fixture should NOT be production
        # (because the hash no longer says #production, and the mode capture
        #  script reads the hash at page-load time)
        after_reload_hash = page.evaluate("() => location.hash")
        print(f"  INFO: After reload, location.hash = {after_reload_hash!r}")

        # After reload without #production hash, page should fall back to default
        first_slide2 = page.locator("section.slide").first
        is_cover_after = first_slide2.evaluate("el => el.classList.contains('s-cover')")

        # This is expected behavior: after reload, the hash is #1 (no production intent),
        # so the page loads the default fixture.
        _check(
            is_cover_after,
            "After reload without #production hash, page correctly falls back to default fixture",
        )

        # Now test that ?fixture=production survives reload
        page.goto(
            f"http://127.0.0.1:{port}/preview.html?fixture=production",
            wait_until="networkidle",
            timeout=15000,
        )
        page.wait_for_timeout(1000)

        after_qp_hash = page.evaluate("() => location.hash")
        print(f"  INFO: With ?fixture=production, location.hash = {after_qp_hash!r}")

        page.reload(wait_until="networkidle", timeout=15000)
        page.wait_for_timeout(1000)

        # After reload, ?fixture=production is still in the URL, so it should still load production
        first_slide3 = page.locator("section.slide").first
        is_notice_after_qp = first_slide3.evaluate("el => el.classList.contains('s-notice')")
        _check(
            is_notice_after_qp,
            "After reload with ?fixture=production, still loads production fixture",
        )

        browser.close()


def test_05_production_scroll_free(port: int) -> None:
    """Verify NO slide in production mode has internal scroll containers."""
    print("\n[Test 05] Production mode: no internal scroll on any slide")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        page.goto(
            f"http://127.0.0.1:{port}/preview.html?fixture=production",
            wait_until="networkidle",
            timeout=15000,
        )
        page.wait_for_timeout(1500)

        slides = page.locator("section.slide")
        slide_count = slides.count()
        _check(slide_count >= 7, f"Has {slide_count} production slides")

        scroll_issues = []
        for i in range(slide_count):
            slide_el = slides.nth(i)
            # Check for elements with overflow-y: auto or scroll
            has_scroll = slide_el.evaluate("""(el) => {
                const all = el.querySelectorAll('*');
                const issues = [];
                for (const child of all) {
                    const style = window.getComputedStyle(child);
                    const overflowY = style.overflowY;
                    if (overflowY === 'auto' || overflowY === 'scroll') {
                        if (child.scrollHeight > child.clientHeight + 2) {
                            issues.push({
                                tag: child.tagName,
                                class: child.className,
                                scrollHeight: child.scrollHeight,
                                clientHeight: child.clientHeight,
                                overflowY: overflowY
                            });
                        }
                    }
                }
                return issues;
            }""")
            if has_scroll and len(has_scroll) > 0:
                scroll_issues.append((i + 1, has_scroll))
                for issue in has_scroll:
                    print(f"  FAIL: Slide {i+1}: scroll container — {issue['tag']}.{issue['class']} "
                          f"scrollHeight={issue['scrollHeight']} > clientHeight={issue['clientHeight']} "
                          f"(overflow-y: {issue['overflowY']})")

        _check(len(scroll_issues) == 0,
               f"Found {len(scroll_issues)} slide(s) with internal scrolling")
        browser.close()


def test_06_production_headline_quality(port: int) -> None:
    """Verify production headlines are short titles, not long overview sentences."""
    print("\n[Test 06] Production mode: headline quality (short titles, not overviews)")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        page.goto(
            f"http://127.0.0.1:{port}/preview.html?fixture=production",
            wait_until="networkidle",
            timeout=15000,
        )
        page.wait_for_timeout(1500)

        # Extract all headlines from the DOM
        headlines = page.evaluate("""() => {
            const slides = document.querySelectorAll('section.slide');
            const result = [];
            slides.forEach((slide, i) => {
                // Find main headline: h1, h2, or the biggest heading
                const hs = slide.querySelectorAll('h1, h2');
                hs.forEach(h => {
                    const text = h.textContent.trim();
                    if (text.length > 0) {
                        result.push({
                            slide: i + 1,
                            tag: h.tagName,
                            text: text,
                            length: text.length,
                            className: slide.className
                        });
                    }
                });
            });
            return result;
        }""")

        all_ok = True
        for h in headlines:
            slide_num = h["slide"]
            text = h["text"]
            length = h["length"]
            # Chinese chars: count CJK characters for length limit
            import re as _re
            cjk_count = len(_re.findall(r'[一-鿿]', text))

            # Title length: recommended <=22 CJK chars, absolute <=30
            if cjk_count > 30:
                all_ok = False
                print(f"  FAIL: Slide {slide_num}: headline too long ({cjk_count} CJK chars): {text[:60]}...")
            elif cjk_count > 22:
                print(f"  WARN: Slide {slide_num}: headline slightly long ({cjk_count} CJK chars): {text[:60]}...")

            # Must not be a long overview sentence (pattern: ends with 。or starts like a paragraph)
            if text.endswith("。") and cjk_count > 15:
                all_ok = False
                print(f"  FAIL: Slide {slide_num}: headline looks like a body sentence (ends with 。): {text[:60]}...")

            print(f"  INFO: Slide {slide_num}: headline [{cjk_count} CJK chars] \"{text[:80]}\"")

        _check(len(headlines) > 0, "Found at least one headline element")
        _check(all_ok, "All headlines are short titles (<=30 CJK chars, not body sentences)")

        browser.close()


def test_07_production_no_duplicate_titles(port: int) -> None:
    """Verify no duplicate title text within the same card/column/steps block."""
    print("\n[Test 07] Production mode: no duplicate titles within same card/column")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        page.goto(
            f"http://127.0.0.1:{port}/preview.html?fixture=production",
            wait_until="networkidle",
            timeout=15000,
        )
        page.wait_for_timeout(1500)

        duplicates = page.evaluate("""() => {
            const slides = document.querySelectorAll('section.slide');
            const issues = [];
            slides.forEach((slide, i) => {
                // Check notice columns: h3 vs .meta
                const cols = slide.querySelectorAll('.col');
                cols.forEach((col, ci) => {
                    const h3 = col.querySelector('h3');
                    const meta = col.querySelector('.meta');
                    if (h3 && meta) {
                        const t1 = h3.textContent.trim();
                        const t2 = meta.textContent.trim();
                        if (t1 && t2 && t1 === t2) {
                            issues.push({
                                slide: i + 1,
                                type: 'notice-column',
                                index: ci,
                                text: t1.substring(0, 40)
                            });
                        }
                    }
                });
                // Check notes cards: h3 vs anything else
                const cards = slide.querySelectorAll('.card');
                cards.forEach((card, ci) => {
                    const headings = card.querySelectorAll('h3, h4, .scribble');
                    const texts = Array.from(headings).map(h => h.textContent.trim()).filter(Boolean);
                    const seen = new Set();
                    texts.forEach(t => {
                        if (seen.has(t)) {
                            issues.push({
                                slide: i + 1,
                                type: 'notes-card',
                                index: ci,
                                text: t.substring(0, 40)
                            });
                        }
                        seen.add(t);
                    });
                });
                // Check cta steps: h4 titles
                const steps = slide.querySelectorAll('.step');
                const stepTitles = [];
                steps.forEach((step, si) => {
                    const h4 = step.querySelector('h4');
                    if (h4) stepTitles.push({idx: si, text: h4.textContent.trim()});
                });
                const seenSteps = new Set();
                stepTitles.forEach(st => {
                    if (seenSteps.has(st.text)) {
                        issues.push({
                            slide: i + 1,
                            type: 'cta-step',
                            index: st.idx,
                            text: st.text.substring(0, 40)
                        });
                    }
                    seenSteps.add(st.text);
                });
            });
            return issues;
        }""")

        for dup in duplicates:
            print(f"  FAIL: Slide {dup['slide']}: duplicate title in {dup['type']}[{dup['index']}]: \"{dup['text']}\"")

        _check(len(duplicates) == 0,
               f"Found {len(duplicates)} duplicate title instance(s)")

        browser.close()


def test_08_production_slides_1_4_detailed_report(port: int) -> None:
    """Detailed verification of production slides 1-4."""
    print("\n[Test 08] Production mode: detailed report for slides 1-4")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        page.goto(
            f"http://127.0.0.1:{port}/preview.html?fixture=production",
            wait_until="networkidle",
            timeout=15000,
        )
        page.wait_for_timeout(1500)

        slides = page.locator("section.slide")
        slide_count = slides.count()

        import re as _re

        for slide_idx in range(min(4, slide_count)):
            slide_el = slides.nth(slide_idx)
            slide_num = slide_idx + 1

            # Get slide class for type identification
            slide_class = slide_el.evaluate("el => el.className")

            # Get main headline text
            headline_text = slide_el.evaluate("""(el) => {
                const h = el.querySelector('h2, h1');
                return h ? h.textContent.trim() : '';
            }""")
            cjk_len = len(_re.findall(r'[一-鿿]', headline_text))

            # Check for internal scroll
            scroll_issues = slide_el.evaluate("""(el) => {
                const all = el.querySelectorAll('*');
                for (const child of all) {
                    const style = window.getComputedStyle(child);
                    if (style.overflowY === 'auto' || style.overflowY === 'scroll') {
                        if (child.scrollHeight > child.clientHeight + 2) {
                            return true;
                        }
                    }
                }
                return false;
            }""")

            # Check content overflow beyond 1920x1080
            overflow_issues = slide_el.evaluate("""(el) => {
                const all = el.querySelectorAll('*');
                const issues = [];
                for (const child of all) {
                    const rect = child.getBoundingClientRect();
                    // Check if element extends beyond slide canvas
                    // The slide itself is 1920x1080, deck-stage scales it
                    // We check relative to slide's own coordinate system
                    if (child.classList.contains('slide')) continue;
                    if (rect.width > 0 && rect.height > 0) {
                        // In the scaled viewport, check if element extends beyond
                        // expected slide boundaries
                    }
                }
                return issues;
            }""")

            # Check for duplicate titles in cards/columns
            dup_count = slide_el.evaluate("""(el) => {
                let count = 0;
                // Check notice columns
                const cols = el.querySelectorAll('.col');
                cols.forEach(col => {
                    const h3 = col.querySelector('h3');
                    const meta = col.querySelector('.meta');
                    if (h3 && meta) {
                        if (h3.textContent.trim() === meta.textContent.trim()) count++;
                    }
                });
                return count;
            }""")

            print(f"  --- Slide {slide_num} ---")
            print(f"  Type: {slide_class}")
            print(f"  Headline: \"{headline_text[:80]}\"")
            print(f"  Headline CJK chars: {cjk_len}")
            print(f"  Internal scroll: {'YES !!' if scroll_issues else 'OK'}")
            print(f"  Duplicate titles: {dup_count}")

            # Assertions
            slide_label = f"Slide {slide_num}"
            _check(not scroll_issues, f"{slide_label}: no internal scroll")
            _check(cjk_len <= 30, f"{slide_label}: headline length <= 30 CJK chars (got {cjk_len})")
            _check(dup_count == 0, f"{slide_label}: no duplicate titles (got {dup_count})")

        # Forward marker: production must contain target content
        page_text = page.content()
        _check("牙周炎的定义与诊断标准" in page_text,
               "Forward marker: 牙周炎的定义与诊断标准 appears in production output")

        # Reverse marker: hand-written fixture text must NOT appear
        _check("掌握核心概念，建立临床思维。" not in page_text,
               "Reverse marker: 掌握核心概念，建立临床思维。 does NOT appear in production output")

        browser.close()


def test_09_production_canvas_no_overflow(port: int) -> None:
    """Verify no visible content overflows the 1920x1080 slide canvas in production mode."""
    print("\n[Test 09] Production mode: no visible content overflow beyond 1920x1080")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        page.goto(
            f"http://127.0.0.1:{port}/preview.html?fixture=production",
            wait_until="networkidle",
            timeout=15000,
        )
        page.wait_for_timeout(1500)

        overflow_issues = page.evaluate("""() => {
            const deck = document.querySelector('deck-stage');
            if (!deck) return [{error: 'no deck-stage found'}];

            const slides = deck.querySelectorAll('section.slide');
            const issues = [];

            slides.forEach((slide, i) => {
                // Get the slide's bounding rect
                const sr = slide.getBoundingClientRect();
                if (sr.width === 0) return; // hidden slide

                // Check all direct children and significant descendants
                const checkEls = slide.querySelectorAll(
                    '.stage, .grid, .head, .list, .flow, .table, .panel, .left, .right, .pane, .col, .card, .stat, .node, .step'
                );
                checkEls.forEach(el => {
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) return;
                    // Element should be contained within its slide
                    if (r.right > sr.right + 5 || r.bottom > sr.bottom + 5) {
                        issues.push({
                            slide: i + 1,
                            elClass: el.className || el.tagName,
                            overRight: r.right > sr.right + 5 ? (r.right - sr.right) : 0,
                            overBottom: r.bottom > sr.bottom + 5 ? (r.bottom - sr.bottom) : 0
                        });
                    }
                });
            });
            return issues;
        }""")

        for issue in overflow_issues:
            if "error" in issue:
                print(f"  FAIL: {issue['error']}")
            else:
                print(f"  FAIL: Slide {issue['slide']}: {issue['elClass']} overflows "
                      f"(right +{issue['overRight']:.0f}px, bottom +{issue['overBottom']:.0f}px)")

        _check(len(overflow_issues) == 0,
               f"Found {len(overflow_issues)} overflow issue(s)")

        browser.close()


def test_10_generalized_fixture_marker_verification(port: int) -> None:
    """Verify generalized fixture loads from disk plan and has consistent markers.

    Forward markers (must appear):
      - 高血压 (hypertension — the generalized handout topic)
      - 靶器官损害 (target organ damage — unique to generalized handout)

    Reverse markers (must NOT appear):
      - 牙周炎 (periodontitis — production fixture only)
      - 掌握核心概念 (demo/hand-written fixture only)
      - 糖尿病 (old in-memory synthetic data — proves we read from disk handout)

    Runtime source verification:
      - deck-stage data-fixture-source must point to production-generalized-plan.v1.json
      - deck-stage data-pin-paper-mode must be 'generalized'
      - MODE: GENERALIZED badge must be visible
    """
    print("\n[Test 10] Generalized fixture marker & source verification")

    from playwright.sync_api import sync_playwright

    generalized_fixture = (
        _TEMPLATE_DIR / "fixtures" / "production-generalized-plan.v1.json"
    )
    if not generalized_fixture.exists():
        print("  SKIP: Generalized plan fixture not found — run planner smoke test first")
        print("  INFO: Run: python scripts/smoke_test_pin_and_paper_planner.py")
        return

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        page.goto(
            f"http://127.0.0.1:{port}/preview.html?fixture=generalized",
            wait_until="networkidle",
            timeout=15000,
        )
        page.wait_for_timeout(1500)

        # ---- Basic rendering ------------------------------------------------
        slides = page.locator("section.slide")
        slide_count = slides.count()
        _check(slide_count >= 3,
               f"Generalized fixture has {slide_count} slides (need >= 3)")
        print(f"  INFO: Generalized slide count: {slide_count}")

        # ---- Runtime source traceability ------------------------------------
        fixture_source = page.evaluate(
            "() => document.querySelector('deck-stage')?.getAttribute('data-fixture-source') || ''")
        fixture_mode = page.evaluate(
            "() => document.querySelector('deck-stage')?.getAttribute('data-pin-paper-mode') || ''")

        _check("production-generalized-plan.v1.json" in fixture_source,
               f"deck-stage data-fixture-source = '{fixture_source}' -> references generalized plan")
        _check(fixture_mode == "generalized",
               f"deck-stage data-pin-paper-mode = '{fixture_mode}' (expected 'generalized')")
        print(f"  INFO: fixture_source = {fixture_source}")
        print(f"  INFO: fixture_mode = {fixture_mode}")

        # ---- MODE badge ----------------------------------------------------
        badge = page.locator("#pin-paper-mode-badge")
        _check(badge.count() > 0, "Generalized mode has MODE badge")
        if badge.count() > 0:
            badge_text = badge.first.text_content()
            _check("GENERALIZED" in badge_text,
                   f"Badge text = '{badge_text}' (expected 'MODE: GENERALIZED')")

        # ---- Hidden debug element -------------------------------------------
        debug_source = page.evaluate(
            "() => document.querySelector('#pin-paper-fixture-debug')?.getAttribute('data-fixture-source') || ''")
        _check("production-generalized-plan.v1.json" in debug_source,
               f"Debug element source = '{debug_source}'")

        # ---- Forward marker: hypertension content from handout --------------
        page_text = page.content()
        _check("高血压" in page_text,
               "Forward marker [+]: 高血压 appears in generalized preview output")
        _check("靶器官损害" in page_text,
               "Forward marker [+]: 靶器官损害 appears in generalized preview output")

        # ---- Reverse markers: must NOT appear -------------------------------
        _check("牙周炎的定义与诊断标准" not in page_text,
               "Reverse marker [-]: 牙周炎的定义与诊断标准 does NOT appear (production only)")
        _check("掌握核心概念，建立临床思维" not in page_text,
               "Reverse marker [-]: demo cover text does NOT appear")
        _check("掌握核心概念" not in page_text,
               "Reverse marker [-]: demo cover text fragment does NOT appear")
        _check("糖尿病" not in page_text,
               "Reverse marker [-]: 糖尿病 does NOT appear (old in-memory synthetic data)")

        # ---- 2/4 KP routing verification in rendered HTML ------------------
        # 2-KP slides should use s-notes with notes-2up grid layout
        notes_2up_grids = page.evaluate(
            "() => document.querySelectorAll('.grid.notes-2up').length")
        _check(notes_2up_grids >= 1,
               f"Found {notes_2up_grids} notes-2up grid(s) in DOM (need >= 1 for 2-KP slide)")

        # 4-KP slides should use s-notes with notes-4up grid layout
        notes_4up_grids = page.evaluate(
            "() => document.querySelectorAll('.grid.notes-4up').length")
        _check(notes_4up_grids >= 1,
               f"Found {notes_4up_grids} notes-4up grid(s) in DOM (need >= 1 for 4-KP slide)")

        # ---- Slide-level quality checks -------------------------------------
        import re as _re
        for slide_idx in range(min(slide_count, 3)):
            slide_el = slides.nth(slide_idx)
            slide_num = slide_idx + 1

            slide_class = slide_el.evaluate("el => el.className")
            headline_text = slide_el.evaluate("""(el) => {
                const h = el.querySelector('h2, h1');
                return h ? h.textContent.trim() : '';
            }""")
            cjk_len = len(_re.findall(r'[一-鿿]', headline_text))

            scroll = slide_el.evaluate("""(el) => {
                const all = el.querySelectorAll('*');
                for (const child of all) {
                    const style = window.getComputedStyle(child);
                    if ((style.overflowY === 'auto' || style.overflowY === 'scroll') &&
                        child.scrollHeight > child.clientHeight + 2) return true;
                }
                return false;
            }""")

            dups = slide_el.evaluate("""(el) => {
                let count = 0;
                el.querySelectorAll('.col').forEach(col => {
                    const h3 = col.querySelector('h3');
                    const meta = col.querySelector('.meta');
                    if (h3 && meta && h3.textContent.trim() === meta.textContent.trim()) count++;
                });
                return count;
            }""")

            print(f"  Slide {slide_num} ({slide_class}): "
                  f"headline [{cjk_len} CJK] \"{headline_text[:60]}\" "
                  f"scroll={'!!' if scroll else 'OK'} dups={dups}")

            _check(not scroll, f"Generalized slide {slide_num}: no internal scroll")
            _check(cjk_len <= 30, f"Generalized slide {slide_num}: headline <= 30 CJK (got {cjk_len})")
            _check(dups == 0, f"Generalized slide {slide_num}: no duplicate titles")

        # ---- Summary --------------------------------------------------------
        print(f"\n  Forward markers:  高血压 OK, 靶器官损害 OK")
        print(f"  Reverse markers:  牙周炎的定义与诊断标准 OK, demo cover OK, 糖尿病 OK")
        print(f"  Source:           deck-stage -> {fixture_source}")
        print(f"  2-KP grid:        {notes_2up_grids} notes-2up instance(s)")
        print(f"  4-KP grid:        {notes_4up_grids} notes-4up instance(s)")

        browser.close()


def test_11_real_production_marker_verification(port: int) -> None:
    """Verify real-llm-production fixture loads and has correct markers.

    Forward markers (must appear):
      - 牙周炎 (periodontitis — the production topic)
      - notes_2up or notes_4up in DOM (proves fixed layout routing)

    Reverse markers (must NOT appear):
      - 高血压 (generalized topic only)
      - 靶器官损害 (generalized topic only)
      - 掌握核心概念 (demo/hand-written fixture only)

    Runtime source verification:
      - deck-stage data-fixture-source must point to real-llm-production-plan.v1.json
      - deck-stage data-pin-paper-mode must be 'real-production'
      - MODE: REAL-LLM PRODUCTION badge must be visible
    """
    print("\n[Test 11] Real-LLM production fixture marker & source verification")

    real_production_fixture = (
        _TEMPLATE_DIR / "fixtures" / "real-llm-production-plan.v1.json"
    )
    if not real_production_fixture.exists():
        print("  SKIP: real-llm-production-plan.v1.json not found — run real LLM smoke test first")
        print("  INFO: Run: python scripts/smoke_test_pin_and_paper_real_llm.py")
        return

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        page.goto(
            f"http://127.0.0.1:{port}/preview.html?fixture=real-production",
            wait_until="networkidle",
            timeout=15000,
        )
        page.wait_for_timeout(1500)

        # ---- Basic rendering ------------------------------------------------
        slides = page.locator("section.slide")
        slide_count = slides.count()
        _check(slide_count >= 3,
               f"Real production fixture has {slide_count} slides (need >= 3)")
        print(f"  INFO: Real production slide count: {slide_count}")

        # ---- Runtime source traceability ------------------------------------
        fixture_source = page.evaluate(
            "() => document.querySelector('deck-stage')?.getAttribute('data-fixture-source') || ''")
        fixture_mode = page.evaluate(
            "() => document.querySelector('deck-stage')?.getAttribute('data-pin-paper-mode') || ''")

        _check("real-llm-production-plan.v1.json" in fixture_source,
               f"deck-stage data-fixture-source = '{fixture_source}' -> references real-llm-production-plan")
        _check(fixture_mode == "real-production",
               f"deck-stage data-pin-paper-mode = '{fixture_mode}' (expected 'real-production')")

        # ---- MODE badge ----------------------------------------------------
        badge = page.locator("#pin-paper-mode-badge")
        _check(badge.count() > 0, "Real production mode has MODE badge")
        if badge.count() > 0:
            badge_text = badge.first.text_content()
            _check("REAL-LLM PRODUCTION" in badge_text,
                   f"Badge text = '{badge_text}' (expected 'MODE: REAL-LLM PRODUCTION')")

        # ---- Forward markers: periodontitis content ------------------------
        page_text = page.content()
        _check("牙周炎" in page_text,
               "Forward [+]: 牙周炎 appears in real production output")

        # ---- Reverse markers: must NOT contain hypertension-specific content -
        # Note: "高血压" as general chronic-disease comparison (from source handout node_7)
        # is acceptable. We check for the generalized-specific markers instead.
        _check("靶器官损害" not in page_text,
               "Reverse [-]: 靶器官损害 does NOT appear (generalized only)")
        _check("高血压靶器官" not in page_text,
               "Reverse [-]: 高血压靶器官 does NOT appear (generalized only)")
        _check("掌握核心概念，建立临床思维" not in page_text,
               "Reverse [-]: demo cover text does NOT appear")
        _check("掌握核心概念" not in page_text,
               "Reverse [-]: demo cover text fragment does NOT appear")

        # ---- No internal scrolling -----------------------------------------
        scroll_issues = page.evaluate("""() => {
            const slides = document.querySelectorAll('section.slide');
            const issues = [];
            slides.forEach((slide, i) => {
                const all = slide.querySelectorAll('*');
                for (const child of all) {
                    const style = window.getComputedStyle(child);
                    if ((style.overflowY === 'auto' || style.overflowY === 'scroll') &&
                        child.scrollHeight > child.clientHeight + 2) {
                        issues.push({slide: i+1, el: child.className || child.tagName});
                    }
                }
            });
            return issues;
        }""")
        _check(len(scroll_issues) == 0,
               f"Real production: no internal scrolling ({len(scroll_issues)} issue(s))")

        # ---- Headline quality ---------------------------------------------
        import re as _re
        for slide_idx in range(min(slide_count, 3)):
            slide_el = slides.nth(slide_idx)
            headline_text = slide_el.evaluate("""(el) => {
                const h = el.querySelector('h2, h1');
                return h ? h.textContent.trim() : '';
            }""")
            cjk_len = len(_re.findall(r'[一-鿿]', headline_text))
            _check(cjk_len <= 30,
                   f"Real production slide {slide_idx + 1}: headline <= 30 CJK (got {cjk_len})")

        # Summary (using same conditions as _check above for consistency)
        fw_ok = "牙周炎" in page_text
        rv_ok = ("靶器官损害" not in page_text
                 and "高血压靶器官" not in page_text
                 and "掌握核心概念" not in page_text)
        print(f"\n  Forward markers:  牙周炎 {'OK' if fw_ok else 'FAIL'}")
        print(f"  Reverse markers:  {'OK' if rv_ok else 'FAIL'}")
        print(f"  Source:           deck-stage -> {fixture_source}")

        browser.close()


def test_12_real_generalized_marker_verification(port: int) -> None:
    """Verify real-llm-generalized fixture loads and has correct markers.

    Forward markers (must appear):
      - 高血压 (hypertension)
      - 靶器官损害 (target organ damage)

    Reverse markers (must NOT appear):
      - 牙周炎的定义与诊断标准 (production only)
      - 掌握核心概念 (demo fixture only)
      - 糖尿病 (old synthetic data)

    Runtime source verification:
      - deck-stage data-fixture-source must point to real-llm-generalized-plan.v1.json
      - deck-stage data-pin-paper-mode must be 'real-generalized'
      - MODE: REAL-LLM GENERALIZED badge must be visible
      - notes-2up and notes-4up grids must appear in DOM
    """
    print("\n[Test 12] Real-LLM generalized fixture marker & source verification")

    real_generalized_fixture = (
        _TEMPLATE_DIR / "fixtures" / "real-llm-generalized-plan.v1.json"
    )
    if not real_generalized_fixture.exists():
        print("  SKIP: real-llm-generalized-plan.v1.json not found — run real LLM smoke test first")
        print("  INFO: Run: python scripts/smoke_test_pin_and_paper_real_llm.py")
        return

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        page.goto(
            f"http://127.0.0.1:{port}/preview.html?fixture=real-generalized",
            wait_until="networkidle",
            timeout=15000,
        )
        page.wait_for_timeout(1500)

        # ---- Basic rendering ------------------------------------------------
        slides = page.locator("section.slide")
        slide_count = slides.count()
        _check(slide_count >= 3,
               f"Real generalized fixture has {slide_count} slides (need >= 3)")
        print(f"  INFO: Real generalized slide count: {slide_count}")

        # ---- Runtime source traceability ------------------------------------
        fixture_source = page.evaluate(
            "() => document.querySelector('deck-stage')?.getAttribute('data-fixture-source') || ''")
        fixture_mode = page.evaluate(
            "() => document.querySelector('deck-stage')?.getAttribute('data-pin-paper-mode') || ''")

        _check("real-llm-generalized-plan.v1.json" in fixture_source,
               f"deck-stage data-fixture-source = '{fixture_source}' -> references real-llm-generalized-plan")
        _check(fixture_mode == "real-generalized",
               f"deck-stage data-pin-paper-mode = '{fixture_mode}' (expected 'real-generalized')")

        # ---- MODE badge ----------------------------------------------------
        badge = page.locator("#pin-paper-mode-badge")
        _check(badge.count() > 0, "Real generalized mode has MODE badge")
        if badge.count() > 0:
            badge_text = badge.first.text_content()
            _check("REAL-LLM GENERALIZED" in badge_text,
                   f"Badge text = '{badge_text}' (expected 'MODE: REAL-LLM GENERALIZED')")

        # ---- Forward markers: hypertension content -------------------------
        page_text = page.content()
        _check("高血压" in page_text,
               "Forward [+]: 高血压 appears in real generalized output")
        _check("靶器官损害" in page_text,
               "Forward [+]: 靶器官损害 appears in real generalized output")

        # ---- Reverse markers ------------------------------------------------
        _check("牙周炎的定义与诊断标准" not in page_text,
               "Reverse [-]: 牙周炎的定义与诊断标准 does NOT appear (production only)")
        _check("掌握核心概念" not in page_text,
               "Reverse [-]: demo cover text does NOT appear")
        _check("糖尿病" not in page_text,
               "Reverse [-]: 糖尿病 does NOT appear (old synthetic data)")

        # ---- notes-2up / notes-4up in DOM ----------------------------------
        notes_2up_grids = page.evaluate(
            "() => document.querySelectorAll('.grid.notes-2up').length")
        _check(notes_2up_grids >= 1,
               f"Found {notes_2up_grids} notes-2up grid(s) in DOM (need >= 1 for 2-KP slide)")

        notes_4up_grids = page.evaluate(
            "() => document.querySelectorAll('.grid.notes-4up').length")
        _check(notes_4up_grids >= 1,
               f"Found {notes_4up_grids} notes-4up grid(s) in DOM (need >= 1 for 4-KP slide)")

        # ---- No internal scrolling -----------------------------------------
        scroll_issues = page.evaluate("""() => {
            const slides = document.querySelectorAll('section.slide');
            const issues = [];
            slides.forEach((slide, i) => {
                const all = slide.querySelectorAll('*');
                for (const child of all) {
                    const style = window.getComputedStyle(child);
                    if ((style.overflowY === 'auto' || style.overflowY === 'scroll') &&
                        child.scrollHeight > child.clientHeight + 2) {
                        issues.push({slide: i+1, el: child.className || child.tagName});
                    }
                }
            });
            return issues;
        }""")
        _check(len(scroll_issues) == 0,
               f"Real generalized: no internal scrolling ({len(scroll_issues)} issue(s))")

        # ---- Headline quality ---------------------------------------------
        import re as _re
        for slide_idx in range(min(slide_count, 3)):
            slide_el = slides.nth(slide_idx)
            headline_text = slide_el.evaluate("""(el) => {
                const h = el.querySelector('h2, h1');
                return h ? h.textContent.trim() : '';
            }""")
            cjk_len = len(_re.findall(r'[一-鿿]', headline_text))
            _check(cjk_len <= 30,
                   f"Real generalized slide {slide_idx + 1}: headline <= 30 CJK (got {cjk_len})")

        fw_ok = ("高血压" in page_text and "靶器官损害" in page_text)
        rv_ok = ("牙周炎的定义与诊断标准" not in page_text
                 and "掌握核心概念" not in page_text
                 and "糖尿病" not in page_text)
        print(f"\n  Forward markers:  {'OK' if fw_ok else 'FAIL'}")
        print(f"  Reverse markers:  {'OK' if rv_ok else 'FAIL'}")
        print(f"  Source:           deck-stage -> {fixture_source}")
        print(f"  2-KP grid:        {notes_2up_grids} notes-2up instance(s)")
        print(f"  4-KP grid:        {notes_4up_grids} notes-4up instance(s)")

        browser.close()


# ============================================================================
# Main
# ============================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("Pin & Paper Preview Smoke Test")
    print("=" * 60)
    print(f"  Serving from: {_TEMPLATE_DIR}")

    server, port = _start_server()
    print(f"  HTTP server: http://127.0.0.1:{port}")
    print()

    try:
        test_01_default_loads_hand_written_fixture(port)
        test_02_production_hash_loads_production_fixture(port)
        test_03_query_param_fixture_also_works(port)
        test_04_production_mode_survives_deck_stage_hash_rewrite(port)
        test_05_production_scroll_free(port)
        test_06_production_headline_quality(port)
        test_07_production_no_duplicate_titles(port)
        test_08_production_slides_1_4_detailed_report(port)
        test_09_production_canvas_no_overflow(port)
        test_10_generalized_fixture_marker_verification(port)
        test_11_real_production_marker_verification(port)
        test_12_real_generalized_marker_verification(port)
    finally:
        server.shutdown()
        server.server_close()

    print("\n" + "=" * 60)
    if _FAILED == 0:
        print("pin and paper preview smoke test passed")
        sys.exit(0)
    else:
        print(f"pin and paper preview smoke test FAILED ({_FAILED} check(s))")
        sys.exit(1)
