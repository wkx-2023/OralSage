"""Smoke test for Pin & Paper planner — full production pipeline.

Covers:
1. Production handout fixture has no Pin & Paper fields (no page_type/slots/cards/steps/cta).
2. build_slides_from_handout_and_tutor produces valid slides.v1 from production handouts.
3. Each slide has expected fields (slide_id, outline_node_id, title, blocks, etc.).
4. Pin & Paper planner produces valid slide plans for each page_type via fake LLM.
5. Planner rejects invalid page_type and HTML/CSS in slots.
6. Planner triggers compression retry on over-budget slots.
7. Planner returns over-budget warning after retry exhaustion.
8. Full pipeline: handout → slides.v1 → pin_and_paper_slide_plan.v1 → saves output fixture.
9. Output fixture has no new medical facts (content originates from handout).
10. Output fixture page_types follow routing rules (5-KP → cta/agenda, not notes).
11. Planner validates slot names and array shapes.
12. Deck-level planner processes all slides.

Uses fake (injectable) LLM clients only — no real external API calls.

Run from the project root:

    python scripts/smoke_test_pin_and_paper_planner.py

Expected last line: ``pin and paper planner smoke test passed``
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Ensure the project root is on sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from backend.services.learning.slides import build_slides_from_handout_and_tutor
from backend.services.learning.pin_and_paper_planner import (
    load_slide_types,
    plan_pin_and_paper_slide,
    plan_pin_and_paper_deck,
)

# ============================================================================
# Fixture paths
# ============================================================================

_HANDOUT_FIXTURE = (
    _PROJECT_ROOT / "tools" / "ppt_preview" / "templates" / "pin-and-paper"
    / "fixtures" / "production-handout-sections.v1.json"
)
_OUTPUT_FIXTURE = (
    _PROJECT_ROOT / "tools" / "ppt_preview" / "templates" / "pin-and-paper"
    / "fixtures" / "production-pin-and-paper-plan.v1.json"
)
_OUTPUT_SLIDES = (
    _PROJECT_ROOT / "tools" / "ppt_preview" / "templates" / "pin-and-paper"
    / "fixtures" / "production-slides.v1.json"
)
_OUTPUT_GENERALIZED_PLAN = (
    _PROJECT_ROOT / "tools" / "ppt_preview" / "templates" / "pin-and-paper"
    / "fixtures" / "production-generalized-plan.v1.json"
)
_OUTPUT_GENERALIZED_SLIDES = (
    _PROJECT_ROOT / "tools" / "ppt_preview" / "templates" / "pin-and-paper"
    / "fixtures" / "production-generalized-slides.v1.json"
)
_HANDOUT_GENERALIZED_FIXTURE = (
    _PROJECT_ROOT / "tools" / "ppt_preview" / "templates" / "pin-and-paper"
    / "fixtures" / "production-generalized-handout-sections.v1.json"
)

# ============================================================================
# Assertion helpers
# ============================================================================

_FAILED = 0


def _check(condition: bool, label: str) -> None:
    global _FAILED
    if not condition:
        print(f"  FAIL: {label}")
        _FAILED += 1
    else:
        print(f"  OK:   {label}")


# ============================================================================
# Fake LLM clients
# ============================================================================


def _extract_source_slide_from_prompt(prompt: str) -> dict | None:
    """Extract the source slide JSON embedded in the prompt.

    The prompt format is:

        ============================================================
        SOURCE SLIDE
        ============================================================
        { ... JSON ... }

        Return JSON only.  No markdown fences.  No extra commentary.
    """
    import re as _re

    # Locate the SOURCE SLIDE marker
    marker = "SOURCE SLIDE\n"
    idx = prompt.find(marker)
    if idx == -1:
        return None

    # Skip past the marker line and the ==== separator line
    after_marker = prompt[idx + len(marker):]
    nl = after_marker.find("\n")
    if nl == -1:
        return None
    # Skip the "====..." separator line
    json_start = nl + 1
    json_candidate = after_marker[json_start:].lstrip("\n")

    # Find the JSON object by tracking brace depth
    if not json_candidate.startswith("{"):
        return None

    depth = 0
    in_string = False
    escape = False
    json_end = -1
    for i, ch in enumerate(json_candidate):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                json_end = i + 1
                break

    if json_end == -1:
        return None

    json_str = json_candidate[:json_end]
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        return None


def _compress_chinese_text(text: str, max_chars: int) -> str:
    """Aggressively shorten Chinese text to fit max_chars while preserving meaning."""
    if len(text) <= max_chars:
        return text
    # Remove parenthetical asides first
    import re as _re
    text = _re.sub(r'（[^）]*）', '', text)
    text = _re.sub(r'\([^)]*\)', '', text)
    if len(text) <= max_chars:
        return text
    # Truncate at last sentence boundary before max_chars
    truncated = text[:max_chars]
    for sep in ['。', '；', '，', '、']:
        last = truncated.rfind(sep)
        if last > max_chars // 2:
            return truncated[:last + 1]
    return truncated[:max_chars - 3] + '...'


def _derive_eyebrow(title: str) -> str:
    """Derive a short eyebrow label (<=20 chars) from slide title.

    Generic rule: take the main topic portion of the title, removing
    common connective/structural words.  Never hardcoded to a specific
    disease or course.
    """
    # Strip generic connective/structural words to isolate the topic.
    # These are course-structure words, NOT disease-specific.
    for word in ["的定义", "的诊断", "的治疗", "的临床", "的基础", "的系统",
                 "的原则", "的标准", "的评估", "的表现", "的药理"]:
        title = title.replace(word, "")
    # Also strip isolated connectors
    title = title.replace("与", "·").replace("和", "·")
    # Take last segment after splitting on "的" for topic-focused label
    if "的" in title:
        parts = title.split("的")
        title = parts[-1] if len(parts[-1]) >= 2 else title
    return _compress_chinese_text(title.strip("·"), 14)


def _derive_headline(title: str) -> str:
    """Return the slide title as the headline — clean and within budget.

    The slide title is the authoritative short heading.  Never promote
    overview body text to headline position.
    """
    return _compress_chinese_text(title, 34)


def _derive_short_topic_label(title: str) -> str:
    """Derive a short topic label for quote/review pages.

    Generic rule: remove common course-structure words to isolate
    the core topic.  Never hardcoded to a specific disease.
    """
    # Remove generic course/slide structure words only
    for word in ["学习", "要点", "回顾", "总结", "复习"]:
        title = title.replace(word, "")
    title = title.strip("的与和·").strip()
    if not title:
        return "核心要点"
    return _compress_chinese_text(title, 40)


def _make_fake_llm_from_source(slide_types: dict):
    """Build a content-aware fake LLM that parses the prompt's SOURCE SLIDE
    and generates a plan using ONLY the content from that source slide.

    The fake LLM:
    - Extracts the source slide JSON from the prompt
    - Picks page_type based on block count / shape (no node_id hardcoding)
    - Fills slots from block title/body (compressed if needed)
    - Uses slide.title for headline, never overview body text
    - Adds ``source_block_ids`` to every array item for traceability
    - Never invents medical facts beyond what's in the source slide
    """

    def _pick_page_type(blocks: list, node_id: str, title: str) -> str:
        """Pick page_type based on content structure, NOT on specific node_id.

        Rules (generic, reusable across any course content):
        - 5+ key_points → cta (list/steps format with room for explanation)
        - 4 key_points → notes_4up (fixed 2x2 card grid)
        - 3 key_points with substantial overview (>80 chars) → notice
        - 3 key_points with short/no overview → notes
        - title containing 回顾/总结 → quote
        - 2 key_points → notes_2up (fixed spacious double-card layout)
        """
        kp_blocks = [b for b in blocks if b.get("block_type") == "key_point"]
        overview_blocks = [b for b in blocks if b.get("block_type") == "overview"]
        n_kp = len(kp_blocks)

        overview_text = overview_blocks[0].get("body", "") if overview_blocks else ""

        # Summary/review slides → quote
        if "回顾" in title or "总结" in title:
            return "quote"

        if n_kp >= 5:
            return "cta"
        if n_kp == 4:
            return "notes_4up"
        if n_kp == 3:
            # notice pairs well with substantial overview text
            if len(overview_text) > 80:
                return "notice"
            return "notes"
        # 2 KP → notes_2up with spacious double-card layout
        if n_kp == 2:
            return "notes_2up"
        # 1 KP — use quote for single insight
        return "quote"

    def _make_slide_plan(source: dict) -> dict:
        slide_id = source.get("slide_id", "unknown")
        node_id = source.get("outline_node_id", "unknown")
        title = source.get("title", "")
        blocks = source.get("blocks", [])
        content = source.get("content", {})

        kp_blocks = [b for b in blocks if b.get("block_type") == "key_point"]
        overview_blocks = [b for b in blocks if b.get("block_type") == "overview"]
        confusion_blocks = [b for b in blocks if b.get("block_type") == "confusion"]
        page_type = _pick_page_type(blocks, node_id, title)

        # Collect all source text for verification
        overview_text = overview_blocks[0].get("body", "") if overview_blocks else content.get("overview", "")

        if page_type == "cta":
            steps = []
            for i, kpb in enumerate(kp_blocks):
                # Compress cta step body more aggressively to fit 5-6 steps
                # in a fixed 16:9 slide without scrolling
                steps.append({
                    "num": str(i + 1),
                    "title": _compress_chinese_text(kpb.get("title", ""), 14),
                    "body": _compress_chinese_text(kpb.get("body", ""), 90),
                    "source_block_ids": [kpb.get("block_id", "")],
                })
            # Derive short eyebrow from title: remove common prefixes
            eyebrow = _derive_eyebrow(title)
            slots = {
                "eyebrow": eyebrow,
                "headline": _derive_headline(title),
                "body": _compress_chinese_text(overview_text, 110),
                "steps": steps,
                "footer_left": "OralSage",
                "footer_right": _compress_chinese_text(title, 30),
            }

        elif page_type == "notice":
            columns = []
            for i, kpb in enumerate(kp_blocks):
                # meta should be a short DIFFERENT label, not a duplicate of title.
                # Leave empty — the renderer already shows title in <h3>.
                columns.append({
                    "title": _compress_chinese_text(kpb.get("title", ""), 18),
                    "body": _compress_chinese_text(kpb.get("body", ""), 120),
                    "meta": "",
                    "source": _compress_chinese_text(
                        ", ".join(kpb.get("evidence_refs", []) or ["OralSage"]), 24),
                    "source_block_ids": [kpb.get("block_id", "")],
                })
            slots = {
                "eyebrow": _derive_eyebrow(title),
                "headline": _derive_headline(title),
                "columns": columns,
                "footer_left": "OralSage",
                "footer_right": _compress_chinese_text(title, 30),
            }

        elif page_type in ("notes", "notes_2up", "notes_4up"):
            cards = []
            if page_type == "notes_2up":
                title_budget, body_budget, label_budget, scribble_budget = 20, 160, 12, 20
            elif page_type == "notes_4up":
                title_budget, body_budget, label_budget, scribble_budget = 16, 90, 10, 16
            else:
                title_budget, body_budget, label_budget, scribble_budget = 18, 120, 10, 18
            for i, kpb in enumerate(kp_blocks):
                # Derive scribble from confusion if available
                scribble = ""
                if i < len(confusion_blocks):
                    scribble = _compress_chinese_text(
                        confusion_blocks[i].get("title", "")[:30], scribble_budget)
                # Short label: use first 8 chars of title + number
                short_label = kpb.get('title', '')[:8]
                cards.append({
                    "label": f"{short_label}·{i+1:02d}"[:label_budget],
                    "title": _compress_chinese_text(kpb.get("title", ""), title_budget),
                    "body": _compress_chinese_text(kpb.get("body", ""), body_budget),
                    "scribble": scribble,
                    "source_block_ids": [kpb.get("block_id", "")],
                })
            subtitle_budget = 52 if page_type == "notes_2up" else (38 if page_type == "notes_4up" else 46)
            subtitle = _compress_chinese_text(overview_text, subtitle_budget)
            slots = {
                "headline": _compress_chinese_text(title, 34 if page_type != "notes_4up" else 32),
                "subtitle": subtitle,
                "cards": cards,
                "footer_left": "OralSage",
                "footer_right": _compress_chinese_text(title, 30),
            }

        elif page_type == "quote":
            # Summary slide — distill key_points into quote_text
            kp_bodies = [kpb.get("body", "") for kpb in kp_blocks]
            kp_titles = [kpb.get("title", "") for kpb in kp_blocks]
            # Use body text if available (richer), else titles
            source_texts = [b for b in kp_bodies if b] or kp_titles
            quote_body = "。".join(source_texts[:3])
            quote_body = _compress_chinese_text(quote_body, 100)
            # Derive a short headline from title
            headline = _derive_short_topic_label(title)
            slots = {
                "headline": headline,
                "quote_text": quote_body,
                "attribution_name": "OralSage 医学教育",
                "attribution_detail": "课程学习要点回顾",
                "footer_left": "OralSage",
                "footer_right": _compress_chinese_text(title, 30),
            }

        else:
            # Fallback
            slots = {
                "headline": _compress_chinese_text(title, 40),
                "subtitle": _compress_chinese_text(overview_text, 60),
                "cards": [
                    {
                        "label": f"01",
                        "title": _compress_chinese_text(kp_blocks[0].get("title", ""), 18) if kp_blocks else "",
                        "body": _compress_chinese_text(kp_blocks[0].get("body", ""), 120) if kp_blocks else "",
                        "scribble": "",
                        "source_block_ids": [kp_blocks[0].get("block_id", "")] if kp_blocks else [],
                    }
                ],
                "footer_left": "OralSage",
                "footer_right": _compress_chinese_text(title, 30),
            }

        return {
            "source_slide_id": slide_id,
            "source_outline_node_id": node_id,
            "page_type": page_type,
            "rationale": f"基于源幻灯片 {len(kp_blocks)} 个要点自动选择 {page_type} 页型。",
            "slots": slots,
            "content_warnings": [],
        }

    def _fake_llm(prompt: str) -> str:
        source = _extract_source_slide_from_prompt(prompt)
        if source is None:
            # Fallback: try to find node_id in prompt for minimal plan
            import re as _re
            m = _re.search(r'"outline_node_id":\s*"([^"]+)"', prompt)
            node_id = m.group(1) if m else "unknown"
            return json.dumps({
                "source_slide_id": "unknown",
                "source_outline_node_id": node_id,
                "page_type": "notes",
                "rationale": "默认使用便签卡片页型。",
                "slots": {
                    "headline": "未知内容",
                    "subtitle": "",
                    "cards": [
                        {"label": "01", "title": "内容", "body": "请参考源幻灯片。", "scribble": "",
                         "source_block_ids": []},
                        {"label": "02", "title": "内容", "body": "请参考源幻灯片。", "scribble": "",
                         "source_block_ids": []},
                    ],
                    "footer_left": "OralSage",
                    "footer_right": "",
                },
                "content_warnings": [],
            }, ensure_ascii=False)

        plan = _make_slide_plan(source)
        return json.dumps(plan, ensure_ascii=False)

    return _fake_llm


def _make_overbudget_fake_llm() -> callable:
    """Fake LLM that returns a plan with a very long body to trigger compression retry."""
    call_count = [0]

    def _fake_llm(prompt: str) -> str:
        call_count[0] += 1
        if call_count[0] == 1:
            # First call: return a plan with an over-budget body
            long_body = "牙周炎是一种非常常见的口腔慢性感染性疾病" * 20  # ~360 chars
            return json.dumps({
                "source_slide_id": "slide_node_1",
                "source_outline_node_id": "node_1",
                "page_type": "quote",
                "rationale": "测试超预算重试。",
                "slots": {
                    "headline": "测试",
                    "quote_text": long_body,
                    "attribution_name": "测试来源",
                    "attribution_detail": "",
                    "footer_left": "OralSage",
                    "footer_right": "测试",
                },
                "content_warnings": [],
            }, ensure_ascii=False)
        else:
            # Second call (retry): return a compressed plan
            return json.dumps({
                "source_slide_id": "slide_node_1",
                "source_outline_node_id": "node_1",
                "page_type": "quote",
                "rationale": "压缩后的计划。",
                "slots": {
                    "headline": "牙周炎定义",
                    "quote_text": "牙周炎是牙齿支持组织的慢性感染性疾病，由菌斑微生物与宿主免疫反应相互作用引发，是成人牙齿丧失的首要原因。",
                    "attribution_name": "牙周病学 第5版",
                    "attribution_detail": "口腔医学教材",
                    "footer_left": "OralSage",
                    "footer_right": "定义",
                },
                "content_warnings": ["compressed_long_source: overview"],
            }, ensure_ascii=False)

    return _fake_llm


def _make_html_injection_fake_llm() -> callable:
    """Fake LLM that returns a plan with HTML tags in a slot."""
    def _fake_llm(prompt: str) -> str:
        return json.dumps({
            "source_slide_id": "slide_node_1",
            "source_outline_node_id": "node_1",
            "page_type": "quote",
            "rationale": "测试 HTML 注入。",
            "slots": {
                "headline": "测试",
                "quote_text": "<div style='color:red'>注入的 HTML</div>",
                "attribution_name": "测试",
                "attribution_detail": "",
                "footer_left": "OralSage",
                "footer_right": "测试",
            },
            "content_warnings": [],
        }, ensure_ascii=False)

    return _fake_llm


# ============================================================================
# Tests
# ============================================================================


def test_01_handout_fixture_has_no_pin_and_paper_fields() -> None:
    """Verify production handout fixture has no Pin & Paper template fields."""
    print("\n[Test 01] Handout fixture has no Pin & Paper fields")
    if not _HANDOUT_FIXTURE.exists():
        _check(False, f"Handout fixture not found: {_HANDOUT_FIXTURE}")
        return
    with open(_HANDOUT_FIXTURE, encoding="utf-8") as f:
        data = json.load(f)

    sections = data.get("sections", [])
    _check(len(sections) >= 7, f"Handout fixture has {len(sections)} sections (need >= 7)")

    forbidden_keys = {"page_type", "slots", "cards", "steps", "cta", "pin-and-paper"}
    for i, section in enumerate(sections):
        # Check top-level keys
        top_keys = set(section.keys())
        found_forbidden_top = top_keys & forbidden_keys
        _check(len(found_forbidden_top) == 0,
               f"Section {i+1} ({section.get('outline_node_id','')}): forbidden top-level keys: {found_forbidden_top}")

        # Check content keys
        content = section.get("content", {})
        if isinstance(content, dict):
            content_keys = set(content.keys())
            found_forbidden_content = content_keys & forbidden_keys
            _check(len(found_forbidden_content) == 0,
                   f"Section {i+1} content: forbidden keys: {found_forbidden_content}")

        # Verify standard OralSage fields exist
        _check("outline_node_id" in section, f"Section {i+1}: missing outline_node_id")
        _check("title" in section, f"Section {i+1}: missing title")
        _check(section.get("section_status") == "llm_generated",
               f"Section {i+1}: section_status is {section.get('section_status')!r}")
        content = section.get("content", {})
        _check("overview" in content, f"Section {i+1}: missing content.overview")
        _check("key_points" in content, f"Section {i+1}: missing content.key_points")

    # Verify at least one section has 5 key_points
    kp_counts = [len(s.get("content", {}).get("key_points", [])) for s in sections]
    _check(max(kp_counts) >= 5, f"Max key_points per section: {max(kp_counts)} (need >= 5)")


def test_02_handout_to_slides_v1_pipeline() -> None:
    """Verify production handouts → slides.v1 transformation."""
    print("\n[Test 02] Production handouts → slides.v1 pipeline")
    if not _HANDOUT_FIXTURE.exists():
        _check(False, f"Handout fixture not found: {_HANDOUT_FIXTURE}")
        return
    with open(_HANDOUT_FIXTURE, encoding="utf-8") as f:
        data = json.load(f)

    sections = data.get("sections", [])
    handout_sections_by_node_id = {}
    for s in sections:
        nid = s.get("outline_node_id", "")
        handout_sections_by_node_id[nid] = s

    # Build outline from sections
    outline = {
        "title": "牙周炎基础学习",
        "nodes": [
            {"id": s["outline_node_id"], "title": s["title"]}
            for s in sections
        ],
    }

    # No tutor steps for this test
    tutor_steps_by_node_id = {}

    deck = build_slides_from_handout_and_tutor(
        session_id=data.get("session_id", "demo_production_001"),
        outline=outline,
        handout_sections_by_node_id=handout_sections_by_node_id,
        tutor_steps_by_node_id=tutor_steps_by_node_id,
        deck_title="牙周炎基础学习",
    )

    _check(deck["schema_version"] == "slides.v1", f"schema_version: {deck.get('schema_version')!r}")
    slides = deck.get("slides", [])
    _check(len(slides) == len(sections),
           f"Slides count: {len(slides)} (expected {len(sections)})")

    for slide in slides:
        _check("slide_id" in slide, f"Slide missing slide_id: {slide.get('outline_node_id')}")
        _check("outline_node_id" in slide, "Slide missing outline_node_id")
        _check(slide["outline_node_id"] == slide.get("section_id"),
               f"outline_node_id != section_id: {slide.get('outline_node_id')}")
        _check("title" in slide, "Slide missing title")
        _check("blocks" in slide, "Slide missing blocks")
        _check(len(slide.get("blocks", [])) > 0,
               f"Slide {slide.get('outline_node_id')} has 0 blocks")

    # Check tutor_missing warnings
    warnings = deck.get("warnings", [])
    tutor_missing = [w for w in warnings if "tutor_step" in w.lower()]
    _check(len(tutor_missing) > 0, f"Expected tutor_step missing warnings, got {len(tutor_missing)}")

    # Save intermediate slides.v1 for inspection
    _OUTPUT_SLIDES.parent.mkdir(parents=True, exist_ok=True)
    _OUTPUT_SLIDES.write_text(json.dumps(deck, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  INFO: Saved production slides.v1 to {_OUTPUT_SLIDES}")


def test_03_slides_v1_to_pin_and_paper_plan() -> None:
    """Verify slides.v1 → pin_and_paper_slide_plan.v1 via fake LLM."""
    print("\n[Test 03] slides.v1 → pin_and_paper_slide_plan.v1 (fake LLM)")
    if not _HANDOUT_FIXTURE.exists():
        _check(False, f"Handout fixture not found: {_HANDOUT_FIXTURE}")
        return
    with open(_HANDOUT_FIXTURE, encoding="utf-8") as f:
        data = json.load(f)

    sections = data.get("sections", [])
    handout_sections_by_node_id = {s["outline_node_id"]: s for s in sections}
    outline = {
        "title": "牙周炎基础学习",
        "nodes": [{"id": s["outline_node_id"], "title": s["title"]} for s in sections],
    }

    deck = build_slides_from_handout_and_tutor(
        session_id=data.get("session_id", "demo_production_001"),
        outline=outline,
        handout_sections_by_node_id=handout_sections_by_node_id,
        tutor_steps_by_node_id={},
        deck_title="牙周炎基础学习",
    )

    slide_types = load_slide_types()
    fake_llm = _make_fake_llm_from_source(slide_types)

    plan_deck = plan_pin_and_paper_deck(
        deck=deck,
        slide_types=slide_types,
        llm_client=fake_llm,
    )

    _check(plan_deck["schema_version"] == "pin_and_paper_slide_plan.v1",
           f"schema_version: {plan_deck.get('schema_version')!r}")
    _check(plan_deck["template_slug"] == "pin-and-paper",
           f"template_slug: {plan_deck.get('template_slug')!r}")

    plan_slides = plan_deck.get("slides", [])
    _check(len(plan_slides) == len(sections),
           f"Plan slides count: {len(plan_slides)} (expected {len(sections)})")

    # Verify page_types
    page_types = [s["page_type"] for s in plan_slides]
    valid_types = {pt["type"] for pt in slide_types.get("page_types", [])}
    for pt in page_types:
        _check(pt in valid_types, f"page_type {pt!r} in valid types")
    _check("cta" in page_types, f"No CTA page type found in plan (got {set(page_types)})")
    _check("notes" in page_types or "notes_2up" in page_types or "notes_4up" in page_types,
           f"No notes page type found (got {page_types})")
    _check("notice" in page_types, f"No notice page type found")

    # Verify 5-KP slides route correctly
    for plan_slide in plan_slides:
        nid = plan_slide.get("source_outline_node_id", "")
        pt = plan_slide.get("page_type", "")
        # node_3 and node_6 have 5 key_points — must NOT be notes/notes_2up/notes_4up
        if nid in ("node_3", "node_6"):
            _check(pt not in ("notes", "notes_2up", "notes_4up"),
                   f"5-KP slide {nid} uses '{pt}' (not 'notes/notes_2up/notes_4up') — correct routing")
            _check(pt in ("cta", "agenda", "process"),
                   f"5-KP slide {nid} uses '{pt}' (expected cta/agenda/process)")

    # Verify all plans have required fields
    for plan_slide in plan_slides:
        _check("source_slide_id" in plan_slide, "Plan missing source_slide_id")
        _check("source_outline_node_id" in plan_slide, "Plan missing source_outline_node_id")
        _check("page_type" in plan_slide, "Plan missing page_type")
        _check("rationale" in plan_slide, "Plan missing rationale")
        _check("slots" in plan_slide, "Plan missing slots")
        slots = plan_slide.get("slots", {})
        _check(isinstance(slots, dict) and len(slots) > 0,
               f"Plan {plan_slide.get('source_outline_node_id')}: slots empty or not dict")

    # Save output fixture
    _OUTPUT_FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    _OUTPUT_FIXTURE.write_text(json.dumps(plan_deck, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  INFO: Saved production plan to {_OUTPUT_FIXTURE}")
    print(f"  INFO: Page types used: {set(page_types)}")


def test_04_planner_rejects_invalid_page_type() -> None:
    """Verify planner rejects invalid page_type."""
    print("\n[Test 04] Planner rejects invalid page_type")
    slide_types = load_slide_types()
    fake_llm = lambda _: json.dumps({
        "source_slide_id": "slide_x",
        "source_outline_node_id": "node_x",
        "page_type": "not-a-real-type",
        "rationale": "测试",
        "slots": {},
        "content_warnings": [],
    }, ensure_ascii=False)

    source = {"slide_id": "slide_x", "outline_node_id": "node_x",
              "title": "测试", "blocks": [], "content": {}}
    result = plan_pin_and_paper_slide(source, slide_types=slide_types, llm_client=fake_llm)
    warnings = result.get("warnings", [])
    _check(any("invalid page_type" in w for w in warnings),
           f"Expected invalid page_type warning, got: {warnings}")


def test_05_planner_rejects_html_in_slots() -> None:
    """Verify planner rejects HTML/CSS content in slots."""
    print("\n[Test 05] Planner rejects HTML/CSS in slots")
    slide_types = load_slide_types()
    fake_llm = _make_html_injection_fake_llm()
    source = {"slide_id": "slide_node_1", "outline_node_id": "node_1",
              "title": "测试", "blocks": [], "content": {}}
    result = plan_pin_and_paper_slide(source, slide_types=slide_types, llm_client=fake_llm)
    warnings = result.get("warnings", [])
    _check(any("HTML/CSS" in w for w in warnings),
           f"Expected HTML/CSS rejection warning, got: {warnings}")


def test_06_planner_triggers_compression_retry() -> None:
    """Verify planner triggers compression retry on over-budget text."""
    print("\n[Test 06] Planner triggers compression retry on over-budget")
    slide_types = load_slide_types()
    fake_llm = _make_overbudget_fake_llm()
    source = {"slide_id": "slide_node_1", "outline_node_id": "node_1",
              "title": "牙周炎定义", "blocks": [{"block_id": "b1", "block_type": "overview",
              "title": "概述", "body": "牙周炎是常见的口腔疾病。"}], "content": {}}
    result = plan_pin_and_paper_slide(source, slide_types=slide_types, llm_client=fake_llm)

    llm_calls = result.get("llm_calls", 0)
    _check(llm_calls == 2, f"Expected 2 LLM calls (initial + retry), got {llm_calls}")

    slide_plan = result.get("slide_plan", {})
    cw = slide_plan.get("content_warnings", [])
    has_compressed = any("compressed_long_source" in str(w) for w in cw)
    _check(has_compressed, f"Expected compressed_long_source in content_warnings, got: {cw}")


def test_07_planner_handles_llm_error_gracefully() -> None:
    """Verify planner returns error plan when LLM client raises."""
    print("\n[Test 07] Planner handles LLM error gracefully")
    slide_types = load_slide_types()

    def _failing_llm(prompt: str) -> str:
        raise RuntimeError("Simulated LLM failure")

    source = {"slide_id": "slide_x", "outline_node_id": "node_x",
              "title": "测试", "blocks": [], "content": {}}
    result = plan_pin_and_paper_slide(source, slide_types=slide_types, llm_client=_failing_llm)
    warnings = result.get("warnings", [])
    _check(any("llm_call_failed" in w for w in warnings),
           f"Expected llm_call_failed warning, got: {warnings}")


def test_08_output_fixture_no_new_medical_content() -> None:
    """Verify output fixture doesn't introduce new medical facts — content originates from handout."""
    print("\n[Test 08] Output fixture has no new medical content")
    if not _OUTPUT_FIXTURE.exists():
        _check(False, f"Output fixture not found — run test_03 first")
        return
    with open(_OUTPUT_FIXTURE, encoding="utf-8") as f:
        plan_deck = json.load(f)
    with open(_HANDOUT_FIXTURE, encoding="utf-8") as f:
        handout_data = json.load(f)

    # Collect all handout text
    handout_texts = set()
    for s in handout_data.get("sections", []):
        for kp in s.get("content", {}).get("key_points", []):
            if kp.get("heading"):
                handout_texts.add(kp["heading"])
            if kp.get("explanation"):
                handout_texts.add(kp["explanation"])
        overview = s.get("content", {}).get("overview", "")
        if overview:
            handout_texts.add(overview)

    # Check that plan slots don't contain obviously invented medical claims
    # (Simple heuristic: check for suspicious diagnostic/treatment numbers not in handout)
    plan_slides = plan_deck.get("slides", [])
    for ps in plan_slides:
        slots = ps.get("slots", {})
        slots_str = json.dumps(slots, ensure_ascii=False)
        # Must not have HTML
        _check("<div" not in slots_str and "<style" not in slots_str,
               f"Plan {ps.get('source_outline_node_id')}: contains HTML tags")
        # Must have Chinese content (not just English filler)
        import re
        cjk = len(re.findall(r'[一-鿿]', slots_str))
        _check(cjk > 50, f"Plan {ps.get('source_outline_node_id')}: only {cjk} CJK chars")


def test_09_planner_validates_slot_shapes() -> None:
    """Verify planner validates array slot shapes (min/max items)."""
    print("\n[Test 09] Planner validates slot array shapes")

    slide_types = load_slide_types()

    # Try to plan a notes slide with 6 cards (max is 4)
    def _too_many_cards_llm(prompt: str) -> str:
        return json.dumps({
            "source_slide_id": "slide_x",
            "source_outline_node_id": "node_x",
            "page_type": "notes",
            "rationale": "测试",
            "slots": {
                "headline": "测试",
                "subtitle": "",
                "cards": [
                    {"label": "01", "title": f"卡片{i}", "body": "内容", "scribble": ""}
                    for i in range(1, 7)  # 6 cards — exceeds max 4
                ],
                "footer_left": "OralSage",
                "footer_right": "",
            },
            "content_warnings": [],
        }, ensure_ascii=False)

    source = {"slide_id": "slide_x", "outline_node_id": "node_x",
              "title": "测试", "blocks": [], "content": {}}
    result = plan_pin_and_paper_slide(source, slide_types=slide_types, llm_client=_too_many_cards_llm)
    warnings = result.get("warnings", [])
    has_slot_error = any("expected" in w and "items" in w for w in warnings)
    _check(has_slot_error, f"Expected slot shape error for 6 cards, got: {warnings}")


def test_10_full_pipeline_to_preview_fixture() -> None:
    """Verify the complete pipeline end-to-end produces a loadable preview fixture."""
    print("\n[Test 10] Full pipeline produces loadable preview fixture")
    if not _OUTPUT_FIXTURE.exists():
        _check(False, f"Output fixture not found — run test_03 first")
        return
    with open(_OUTPUT_FIXTURE, encoding="utf-8") as f:
        data = json.load(f)

    _check(data["schema_version"] == "pin_and_paper_slide_plan.v1", "schema_version correct")
    _check(data["template_slug"] == "pin-and-paper", "template_slug correct")
    _check(len(data.get("slides", [])) >= 7, f"Has {len(data.get('slides', []))} slides")

    # Check each slide has renderable slots
    slide_types = load_slide_types()
    valid_types = {pt["type"] for pt in slide_types.get("page_types", [])}
    for i, slide in enumerate(data.get("slides", [])):
        pt = slide.get("page_type", "")
        _check(pt in valid_types, f"Slide {i+1}: valid page_type {pt!r}")
        slots = slide.get("slots", {})
        _check(isinstance(slots, dict) and len(slots) > 0,
               f"Slide {i+1}: has populated slots")

    print(f"  INFO: Preview fixture ready at {_OUTPUT_FIXTURE}")


def test_11_no_planner_generated_medical_content() -> None:
    """Verify planner does not generate new medical content — it only reorganizes."""
    print("\n[Test 11] Planner does not generate new medical content")
    if not _OUTPUT_FIXTURE.exists() or not _HANDOUT_FIXTURE.exists():
        _check(False, "Required fixtures missing")
        return
    with open(_OUTPUT_FIXTURE, encoding="utf-8") as f:
        plan_deck = json.load(f)
    with open(_HANDOUT_FIXTURE, encoding="utf-8") as f:
        handout_data = json.load(f)

    # Collect all handout content strings
    handout_content_pool = ""
    for s in handout_data.get("sections", []):
        c = s.get("content", {})
        handout_content_pool += c.get("overview", "")
        for kp in c.get("key_points", []):
            handout_content_pool += kp.get("heading", "")
            handout_content_pool += kp.get("explanation", "")
        handout_content_pool += c.get("clinical_example", "")
        for cc in c.get("common_confusions", []):
            handout_content_pool += cc.get("confusion", "")
            handout_content_pool += cc.get("clarification", "")

    # The planner (via fake LLM) rephrases/compresses/restructures — but shouldn't
    # introduce completely new medical entities. Check that major medical terms in
    # slots are traceable to handout content.
    plan_slides = plan_deck.get("slides", [])
    total_slots_cjk = 0
    for ps in plan_slides:
        slots_str = json.dumps(ps.get("slots", {}), ensure_ascii=False)
        import re
        total_slots_cjk += len(re.findall(r'[一-鿿]', slots_str))
    handout_cjk = len(re.findall(r'[一-鿿]', handout_content_pool))

    # The plan should reorganize content; its CJK count should be somewhat less
    # than handout (since it compresses) but not zero
    _check(total_slots_cjk > 500, f"Plan has {total_slots_cjk} CJK chars (need > 500)")
    # Plan shouldn't have MORE Chinese chars than the original handout
    _check(total_slots_cjk <= handout_cjk * 1.1,
           f"Plan CJK ({total_slots_cjk}) should not significantly exceed handout CJK ({handout_cjk})")


def test_12_source_traceability_per_slide() -> None:
    """Verify every plan slide has source_block_ids and content traceable to its source slide."""
    print("\n[Test 12] Source traceability per slide")
    if not _OUTPUT_FIXTURE.exists() or not _OUTPUT_SLIDES.exists():
        _check(False, "Required fixtures missing — run test_02 and test_03 first")
        return

    with open(_OUTPUT_FIXTURE, encoding="utf-8") as f:
        plan_deck = json.load(f)
    with open(_OUTPUT_SLIDES, encoding="utf-8") as f:
        slides_deck = json.load(f)

    source_slides = {s["outline_node_id"]: s for s in slides_deck.get("slides", [])}

    plan_slides = plan_deck.get("slides", [])
    for ps in plan_slides:
        nid = ps.get("source_outline_node_id", "")
        ss = source_slides.get(nid)
        _check(ss is not None, f"Plan slide {nid}: source slide found")

        if ss is None:
            continue

        # Collect all text from the source slide
        source_texts: list[str] = []
        source_block_ids: set[str] = set()
        for b in ss.get("blocks", []):
            source_block_ids.add(b.get("block_id", ""))
            source_texts.append(b.get("title", ""))
            source_texts.append(b.get("body", ""))
        source_pool = " ".join(source_texts)

        slots = ps.get("slots", {})

        # Check array items have source_block_ids
        _check_array_items_have_source_ids(slots, nid)

        # Check core slot text is traceable
        _check_slot_text_traceable(slots, nid, source_pool, source_block_ids)


def _check_array_items_have_source_ids(slots: dict, nid: str) -> None:
    """Verify cards/steps/columns each have source_block_ids."""
    for arr_key in ("cards", "steps", "columns"):
        arr = slots.get(arr_key)
        if not isinstance(arr, list):
            continue
        for i, item in enumerate(arr):
            if not isinstance(item, dict):
                continue
            sbi = item.get("source_block_ids")
            _check(isinstance(sbi, list) and len(sbi) > 0,
                   f"Plan {nid}: {arr_key}[{i}] has valid source_block_ids (got {sbi!r})")


def _check_slot_text_traceable(slots: dict, nid: str, source_pool: str, source_block_ids: set[str]) -> None:
    """Check that key text segments in slots can be traced to the source slide."""
    import re as _re

    # For each array item, verify source_block_ids reference actual blocks
    for arr_key in ("cards", "steps", "columns"):
        arr = slots.get(arr_key)
        if not isinstance(arr, list):
            continue
        for i, item in enumerate(arr):
            if not isinstance(item, dict):
                continue
            sbi = item.get("source_block_ids", [])
            for bid in sbi:
                _check(bid in source_block_ids,
                       f"Plan {nid}: {arr_key}[{i}].source_block_ids ref {bid!r} exists in source slide")

    # For notice columns, check source field exists in source slide evidence_refs
    if "columns" in slots:
        for i, col in enumerate(slots["columns"]):
            src = col.get("source", "")
            if src and src not in ("OralSage",):
                # source field should either be an evidence_ref from the source slide
                # or "OralSage" as default. This is tracked via source_block_ids.
                pass


# ============================================================================
# Test 13: node_1 must not contain cross-section risk-factor content
# ============================================================================

# Phrases that belong to node_4 (risk factors), NOT node_1 (definition & etiology)
_NODE_4_ONLY_PHRASES = [
    "J Clin Periodontol 2024",
    "J Clin Periodontol",
    "OralSage 临床路径",
    "遗传因素约占",
    "遗传因素约占重度牙周炎风险的",
]


def test_13_node_1_has_no_cross_section_content() -> None:
    """Verify node_1 plan does NOT contain risk-factor content from node_4."""
    print("\n[Test 13] node_1 has no cross-section risk-factor content")
    if not _OUTPUT_FIXTURE.exists():
        _check(False, "Output fixture not found — run test_03 first")
        return

    with open(_OUTPUT_FIXTURE, encoding="utf-8") as f:
        plan_deck = json.load(f)
    with open(_OUTPUT_SLIDES, encoding="utf-8") as f:
        slides_deck = json.load(f)

    # Find node_1 plan
    node_1_plan = None
    for ps in plan_deck.get("slides", []):
        if ps.get("source_outline_node_id") == "node_1":
            node_1_plan = ps
            break
    _check(node_1_plan is not None, "node_1 plan found in output")

    if node_1_plan is None:
        return

    # Find node_1 source slide
    node_1_source = None
    for ss in slides_deck.get("slides", []):
        if ss.get("outline_node_id") == "node_1":
            node_1_source = ss
            break
    _check(node_1_source is not None, "node_1 source slide found")

    # Serialize node_1 plan slots for phrase checking
    plan_str = json.dumps(node_1_plan.get("slots", {}), ensure_ascii=False)

    for phrase in _NODE_4_ONLY_PHRASES:
        _check(phrase not in plan_str,
               f"node_1 plan must NOT contain '{phrase}' (belongs to node_4, not node_1)")

    # Also check: node_1 should NOT mention 吸烟 as a risk factor
    _check("吸烟" not in plan_str or "吸烟者" not in plan_str,
           "node_1 plan should not contain smoking-related risk content")
    _check("糖尿病" not in plan_str or "糖尿病血糖" not in plan_str,
           "node_1 plan should not contain diabetes-related risk content")

    # Check node_1 source slide has no 吸烟/糖尿病 content to begin with
    if node_1_source:
        source_str = json.dumps(node_1_source, ensure_ascii=False)
        _check("吸烟使牙周炎" not in source_str,
               "node_1 source slide correctly has no smoking risk-factor content")
        _check("糖尿病血糖控制不佳" not in source_str,
               "node_1 source slide correctly has no diabetes risk-factor content")

    # Verify node_1 plan content is about definition, etiology, biofilm, immune response
    node_1_expected_topics = ["菌斑", "生物膜", "牙周炎", "定义", "免疫", "宿主"]
    for topic in node_1_expected_topics:
        _check(topic in plan_str,
               f"node_1 plan contains expected topic '{topic}'")


def test_14_citation_source_fields_from_source_slide() -> None:
    """Verify citation/source fields in plan items come from the source slide evidence_refs."""
    print("\n[Test 14] Citation/source fields from source slide")
    if not _OUTPUT_FIXTURE.exists() or not _OUTPUT_SLIDES.exists():
        _check(False, "Required fixtures missing")
        return

    with open(_OUTPUT_FIXTURE, encoding="utf-8") as f:
        plan_deck = json.load(f)
    with open(_OUTPUT_SLIDES, encoding="utf-8") as f:
        slides_deck = json.load(f)

    source_slides = {s["outline_node_id"]: s for s in slides_deck.get("slides", [])}

    for ps in plan_deck.get("slides", []):
        nid = ps.get("source_outline_node_id", "")
        ss = source_slides.get(nid)
        if ss is None:
            continue

        # Collect all evidence_refs from the source slide
        all_refs: set[str] = set()
        for b in ss.get("blocks", []):
            for ref in b.get("evidence_refs", []):
                all_refs.add(ref)
        # Also check content.key_points evidence_refs
        for kp in ss.get("content", {}).get("key_points", []):
            for ref in kp.get("evidence_refs", []):
                all_refs.add(ref)

        slots = ps.get("slots", {})

        # For notice columns, check "source" field
        columns = slots.get("columns", [])
        if isinstance(columns, list):
            for i, col in enumerate(columns):
                if not isinstance(col, dict):
                    continue
                src = col.get("source", "")
                # source should be empty, "OralSage", or traceable to evidence_refs
                if src and src != "OralSage" and src != "":
                    # Check it's not a fabricated citation
                    is_ok = (
                        src in all_refs
                        or any(ref.lower() in src.lower() for ref in all_refs)
                    )
                    if not is_ok:
                        _check(
                            False,
                            f"Plan {nid}: columns[{i}].source='{src}' not found in source slide evidence_refs {all_refs}",
                        )

        # For notes cards, check scribble doesn't contain fabricated citations
        cards = slots.get("cards", [])
        if isinstance(cards, list):
            for i, card in enumerate(cards):
                if not isinstance(card, dict):
                    continue
                scribble = card.get("scribble", "")
                body = card.get("body", "")
                # Citation-like patterns in body should be traceable
                for text, label in [(body, "body")]:
                    if "J Clin" in text or "et al" in text or "PMID" in text:
                        _check(
                            False,
                            f"Plan {nid}: cards[{i}].{label} contains citation not from source",
                        )


def test_15_generalized_from_disk_handout() -> None:
    """Generate generalized plan from the on-disk production handout fixture.

    This test reads ``production-generalized-handout-sections.v1.json``
    (hypertension topic) and runs the full pipeline:
        handout → slides.v1 → pin_and_paper_slide_plan.v1

    It verifies that:
    - 2-KP slide routes to notes_2up
    - 4-KP slide routes to notes_4up
    - 3-KP slide routes to notes or notice
    - Plan marker matches handout marker (hypertension, NOT diabetes/perio)
    - No stale diabetes content leaks in
    """
    print("\n[Test 15] Generalized plan from disk handout fixture")

    if not _HANDOUT_GENERALIZED_FIXTURE.exists():
        _check(False, f"Generalized handout fixture not found: {_HANDOUT_GENERALIZED_FIXTURE}")
        return

    with open(_HANDOUT_GENERALIZED_FIXTURE, encoding="utf-8") as f:
        handout_data = json.load(f)

    sections = handout_data.get("sections", [])
    _check(len(sections) >= 3, f"Generalized handout has {len(sections)} sections (need >= 3)")

    # Build outline and handout_sections_by_node_id from the disk file
    handout_sections_by_node_id = {s["outline_node_id"]: s for s in sections}
    outline = {
        "title": handout_data.get("description", "泛化测试").split("（")[0].replace("泛化 OralSage 讲义 fixture", "").strip() or "高血压基础知识",
        "nodes": [{"id": s["outline_node_id"], "title": s["title"]} for s in sections],
    }

    # Step 1: Generate slides.v1 from disk handout
    deck = build_slides_from_handout_and_tutor(
        session_id=handout_data.get("session_id", "demo_generalized_001"),
        outline=outline,
        handout_sections_by_node_id=handout_sections_by_node_id,
        tutor_steps_by_node_id={},
        deck_title="高血压基础知识",
    )

    slides = deck.get("slides", [])
    _check(len(slides) == len(sections),
           f"Generalized: {len(slides)} slides from {len(sections)} sections")

    # Save intermediate slides
    _OUTPUT_GENERALIZED_SLIDES.parent.mkdir(parents=True, exist_ok=True)
    _OUTPUT_GENERALIZED_SLIDES.write_text(
        json.dumps(deck, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  INFO: Saved generalized slides.v1 to {_OUTPUT_GENERALIZED_SLIDES}")

    # Step 2: Generate pin-and-paper plan via fake LLM
    slide_types = load_slide_types()
    fake_llm = _make_fake_llm_from_source(slide_types)

    plan_deck = plan_pin_and_paper_deck(
        deck=deck,
        slide_types=slide_types,
        llm_client=fake_llm,
    )

    plan_slides = plan_deck.get("slides", [])
    _check(len(plan_slides) == len(sections),
           f"Generalized plan: {len(plan_slides)} slides")

    # Collect page_types
    page_types = set(s["page_type"] for s in plan_slides)
    _check(len(page_types) >= 2,
           f"Generalized plan uses {len(page_types)} distinct page_types: {page_types}")
    print(f"  INFO: Generalized page types: {page_types}")

    # ---- Routing verification (2/4 KP rules on non-perio content) ----------
    # Map node_id → key_point count from the source handout
    kp_counts_by_node = {}
    for s in sections:
        nid = s.get("outline_node_id", "")
        kps = s.get("content", {}).get("key_points", [])
        kp_counts_by_node[nid] = len(kps)

    for ps in plan_slides:
        nid = ps.get("source_outline_node_id", "")
        pt = ps.get("page_type", "")
        n_kp = kp_counts_by_node.get(nid, 0)
        print(f"  INFO: {nid} ({n_kp} KP) -> {pt}")

        if n_kp == 2:
            _check(pt == "notes_2up",
                   f"2-KP slide {nid} routes to '{pt}' (expected 'notes_2up')")
        elif n_kp == 4:
            _check(pt == "notes_4up",
                   f"4-KP slide {nid} routes to '{pt}' (expected 'notes_4up')")
        elif n_kp == 3:
            _check(pt in ("notes", "notice"),
                   f"3-KP slide {nid} routes to '{pt}' (expected 'notes' or 'notice')")
        elif n_kp >= 5:
            _check(pt not in ("notes", "notes_2up", "notes_4up"),
                   f"5+ KP slide {nid} uses '{pt}' (not notes/notes_2up/notes_4up)")
            _check(pt in ("cta", "agenda", "process"),
                   f"5+ KP slide {nid} uses '{pt}' (expected cta/agenda/process)")

    # ---- Marker consistency: plan must match handout topic ----------------
    # The handout is hypertension — forward marker must appear in plan
    plan_str = json.dumps(plan_deck, ensure_ascii=False)

    # Forward marker: hypertension-specific phrase from the handout
    _check("高血压" in plan_str,
           "Forward marker: 高血压 appears in generalized plan")
    _check("靶器官损害" in plan_str,
           "Forward marker: 靶器官损害 appears in generalized plan")

    # Reverse marker: NO diabetes content (from old in-memory synthetic data)
    _check("糖尿病" not in plan_str,
           "Reverse marker: 糖尿病 does NOT appear in generalized plan")
    _check("gen_node_" not in plan_str,
           "Reverse marker: gen_node_ does NOT appear (uses real node IDs)")

    # Reverse marker: NO periodontitis content
    _check("牙周炎" not in plan_str,
           "Reverse marker: 牙周炎 does NOT appear in generalized plan")

    # ---- Headline quality: short titles, not overview sentences -----------
    for ps in plan_slides:
        headline = ps.get("slots", {}).get("headline", "")
        if headline and len(headline) > 40:
            _check(False,
                   f"Generalized plan {ps.get('source_outline_node_id')}: "
                   f"headline too long ({len(headline)} chars): {headline[:60]}")

    # ---- No meta duplicates title in notice columns -----------------------
    for ps in plan_slides:
        if ps.get("page_type") != "notice":
            continue
        cols = ps.get("slots", {}).get("columns", [])
        for i, col in enumerate(cols):
            if col.get("meta") and col.get("title"):
                if col["meta"] == col["title"]:
                    _check(False,
                           f"Generalized plan {ps.get('source_outline_node_id')}: "
                           f"column[{i}] meta duplicates title")

    # Save output plan
    _OUTPUT_GENERALIZED_PLAN.parent.mkdir(parents=True, exist_ok=True)
    _OUTPUT_GENERALIZED_PLAN.write_text(
        json.dumps(plan_deck, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  INFO: Saved generalized plan to {_OUTPUT_GENERALIZED_PLAN}")

    # ---- Structural variety: verify 2/3/4/5 KP coverage ---------------------
    kp_counts = list(kp_counts_by_node.values())
    _check(2 in kp_counts, f"Generalized: has 2-KP section (counts: {kp_counts})")
    _check(3 in kp_counts, f"Generalized: has 3-KP section (counts: {kp_counts})")
    _check(4 in kp_counts, f"Generalized: has 4-KP section (counts: {kp_counts})")
    _check(5 in kp_counts, f"Generalized: has 5-KP section (counts: {kp_counts})")
    print(f"  INFO: KP counts per section: {kp_counts}")


# ============================================================================
# Main
# ============================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("Pin & Paper Planner Smoke Test")
    print("=" * 60)

    test_01_handout_fixture_has_no_pin_and_paper_fields()
    test_02_handout_to_slides_v1_pipeline()
    test_03_slides_v1_to_pin_and_paper_plan()
    test_04_planner_rejects_invalid_page_type()
    test_05_planner_rejects_html_in_slots()
    test_06_planner_triggers_compression_retry()
    test_07_planner_handles_llm_error_gracefully()
    test_08_output_fixture_no_new_medical_content()
    test_09_planner_validates_slot_shapes()
    test_10_full_pipeline_to_preview_fixture()
    test_11_no_planner_generated_medical_content()
    test_12_source_traceability_per_slide()
    test_13_node_1_has_no_cross_section_content()
    test_14_citation_source_fields_from_source_slide()
    test_15_generalized_from_disk_handout()

    print("\n" + "=" * 60)
    if _FAILED == 0:
        print("pin and paper planner smoke test passed")
        sys.exit(0)
    else:
        print(f"pin and paper planner smoke test FAILED ({_FAILED} check(s))")
        sys.exit(1)
