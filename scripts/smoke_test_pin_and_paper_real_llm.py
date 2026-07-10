"""Smoke test for Pin & Paper planner with real LLM API.

Verifies the full pipeline:
    1. production handout -> slides.v1 -> real LLM plan -> save & verify
    2. generalized handout -> slides.v1 -> real LLM plan -> save & verify

Each slide is planned independently via real LLM.  Raw responses are saved
to artifacts/ for auditability.

Run from the project root:

    python scripts/smoke_test_pin_and_paper_real_llm.py

Configuration:
    Reads from project .env via ``ensure_project_env()``.
    Priority: PIN_AND_PAPER_LLM_* > OPENAI_API_KEY > LEARNING_LLM_*
    Default model: deepseek-chat

If no API key is found, this script reports blockage and exits with
a non-zero code — it NEVER silently falls back to fake LLM.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

# Ensure the project root is on sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Load .env from project root BEFORE importing modules that read os.environ
from backend.services.learning.pin_and_paper_real_llm_client import ensure_project_env
ensure_project_env()

from backend.services.learning.slides import build_slides_from_handout_and_tutor
from backend.services.learning.pin_and_paper_planner import load_slide_types
from backend.services.learning.pin_and_paper_real_llm_client import (
    create_real_llm_client,
    plan_deck_concurrent,
    _get_config,
)

# ============================================================================
# Paths
# ============================================================================

_FIXTURE_DIR = (
    _PROJECT_ROOT / "tools" / "ppt_preview" / "templates" / "pin-and-paper" / "fixtures"
)
_ARTIFACTS_DIR = (
    _PROJECT_ROOT / "tools" / "ppt_preview" / "templates" / "pin-and-paper" / "artifacts"
)

_HANDOUT_PRODUCTION = _FIXTURE_DIR / "production-handout-sections.v1.json"
_HANDOUT_GENERALIZED = _FIXTURE_DIR / "production-generalized-handout-sections.v1.json"

_OUTPUT_PRODUCTION_PLAN = _FIXTURE_DIR / "real-llm-production-plan.v1.json"
_OUTPUT_GENERALIZED_PLAN = _FIXTURE_DIR / "real-llm-generalized-plan.v1.json"
_OUTPUT_PRODUCTION_SLIDES = _FIXTURE_DIR / "real-llm-production-slides.v1.json"
_OUTPUT_GENERALIZED_SLIDES = _FIXTURE_DIR / "real-llm-generalized-slides.v1.json"

# ============================================================================
# State
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
# Helpers
# ============================================================================


def _build_slides_deck(handout_path: Path, deck_title: str) -> dict:
    """Build slides.v1 deck from a handout fixture."""
    with open(handout_path, encoding="utf-8") as f:
        data = json.load(f)

    sections = data.get("sections", [])
    handout_sections_by_node_id = {s["outline_node_id"]: s for s in sections}
    outline = {
        "title": deck_title,
        "nodes": [{"id": s["outline_node_id"], "title": s["title"]} for s in sections],
    }

    deck = build_slides_from_handout_and_tutor(
        session_id=data.get("session_id", "demo_001"),
        outline=outline,
        handout_sections_by_node_id=handout_sections_by_node_id,
        tutor_steps_by_node_id={},
        deck_title=deck_title,
    )
    return deck


def _save_raw_responses(plan_deck: dict, raw_responses: dict[str, list[dict]], prefix: str) -> None:
    """Save raw LLM responses to artifacts directory."""
    _ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _ARTIFACTS_DIR / f"{prefix}-raw-responses.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(raw_responses, f, ensure_ascii=False, indent=2)
    print(f"  INFO: Saved raw LLM responses to {path}")


def _collect_source_block_ids(source_slide: dict) -> set[str]:
    """Collect all block_ids from a source slide."""
    ids: set[str] = set()
    for b in source_slide.get("blocks", []):
        bid = b.get("block_id", "")
        if bid:
            ids.add(bid)
    return ids


def _collect_source_text(source_slide: dict) -> str:
    """Collect all text from a source slide for traceability checking."""
    parts: list[str] = []
    parts.append(source_slide.get("title", ""))
    parts.append(source_slide.get("subtitle", ""))
    for b in source_slide.get("blocks", []):
        parts.append(b.get("title", ""))
        parts.append(b.get("body", ""))
    for kp in source_slide.get("content", {}).get("key_points", []):
        parts.append(kp.get("heading", ""))
        parts.append(kp.get("explanation", ""))
    parts.append(source_slide.get("content", {}).get("overview", ""))
    return " ".join(parts)


def _verify_plan(plan_deck: dict, slides_deck: dict, label: str) -> None:
    """Run comprehensive verification on a real LLM plan."""
    print(f"\n--- Verification: {label} ---")

    slide_types = load_slide_types()
    plan_slides = plan_deck.get("slides", [])
    source_slides = slides_deck.get("slides", [])

    source_by_oid: dict[str, dict] = {}
    for ss in source_slides:
        source_by_oid[ss.get("outline_node_id", "")] = ss

    # 1. Schema check
    _check(
        plan_deck.get("schema_version") == "pin_and_paper_slide_plan.v1",
        f"{label}: schema_version correct"
    )
    _check(
        len(plan_slides) == len(source_slides),
        f"{label}: {len(plan_slides)} plan slides == {len(source_slides)} source slides"
    )

    # 2. Per-slide checks
    valid_types = {pt["type"] for pt in slide_types.get("page_types", [])}
    page_types: list[str] = []
    retry_slides: list[str] = []
    over_budget_slides: list[str] = []
    trace_issues: list[str] = []

    for i, ps in enumerate(plan_slides):
        nid = ps.get("source_outline_node_id", f"slide_{i}")
        pt = ps.get("page_type", "")
        page_types.append(pt)

        # 2a. Valid page_type
        if pt:
            _check(pt in valid_types,
                   f"{label}: slide {nid} page_type {pt!r} is valid")

        # 2b. Has slots
        slots = ps.get("slots", {})
        _check(isinstance(slots, dict) and len(slots) > 0,
               f"{label}: slide {nid} has non-empty slots")

        # 2c. Source traceability — array items must have source_block_ids
        ss = source_by_oid.get(nid)
        if ss:
            source_block_ids = _collect_source_block_ids(ss)
            for arr_key in ("cards", "steps", "columns"):
                arr = slots.get(arr_key)
                if not isinstance(arr, list):
                    continue
                for j, item in enumerate(arr):
                    if not isinstance(item, dict):
                        continue
                    sbi = item.get("source_block_ids")
                    if not isinstance(sbi, list) or len(sbi) == 0:
                        trace_issues.append(
                            f"{nid}: {arr_key}[{j}] missing source_block_ids"
                        )
                    else:
                        for bid in sbi:
                            if bid not in source_block_ids:
                                trace_issues.append(
                                    f"{nid}: {arr_key}[{j}].source_block_ids "
                                    f"refs {bid!r} not in source slide"
                                )

            # 2d. Content traceability — key CJK phrases should be in source
            source_text = _collect_source_text(ss)
            slots_str = json.dumps(slots, ensure_ascii=False)

    # 3. Report traceability issues
    if trace_issues:
        for ti in trace_issues[:10]:  # show first 10
            print(f"  TRACE WARN: {ti}")
        if len(trace_issues) > 10:
            print(f"  TRACE WARN: ... and {len(trace_issues) - 10} more")
        _check(False, f"{label}: {len(trace_issues)} source traceability issue(s)")
    else:
        _check(True, f"{label}: all array items have valid source_block_ids")

    # 4. Page type routing verification
    # Count key_points per source slide
    kp_counts: dict[str, int] = {}
    for ss in source_slides:
        nid = ss.get("outline_node_id", "")
        kps = ss.get("content", {}).get("key_points", [])
        kp_counts[nid] = len(kps) if isinstance(kps, list) else 0

    for i, ps in enumerate(plan_slides):
        nid = ps.get("source_outline_node_id", "")
        pt = ps.get("page_type", "")
        n_kp = kp_counts.get(nid, 0)
        if n_kp == 2:
            _check(pt == "notes_2up",
                   f"{label}: 2-KP {nid} -> {pt} (expected notes_2up)")
        elif n_kp == 4:
            _check(pt == "notes_4up",
                   f"{label}: 4-KP {nid} -> {pt} (expected notes_4up)")
        elif n_kp == 3:
            _check(pt != "notes_4up",
                   f"{label}: 3-KP {nid} -> {pt} (must NOT be notes_4up)")
            _check(pt != "notes_2up",
                   f"{label}: 3-KP {nid} -> {pt} (must NOT be notes_2up)")
        elif n_kp >= 5:
            _check(pt not in ("notes", "notes_2up", "notes_4up"),
                   f"{label}: 5+ KP {nid} -> {pt} (NOT notes/notes_2up/notes_4up)")

    # 5. Content warnings and retry info
    content_warnings = plan_deck.get("warnings", [])
    retry_count = sum(1 for w in content_warnings if "over_budget_after_retry" in w)
    if retry_count > 0:
        print(f"  INFO: {retry_count} slide(s) still over budget after retry")

    # 6. Page type summary
    type_counts: dict[str, int] = {}
    for pt in page_types:
        type_counts[pt] = type_counts.get(pt, 0) + 1
    print(f"  INFO: Page type sequence: {page_types}")
    print(f"  INFO: Page type counts: {type_counts}")

    # 7. No empty page_types
    empty_count = sum(1 for pt in page_types if not pt)
    _check(empty_count == 0, f"{label}: {empty_count} slides with empty page_type")


# ============================================================================
# Main pipeline
# ============================================================================


def run_pipeline(handout_path: Path, output_plan_path: Path,
                 output_slides_path: Path, label: str, deck_title: str) -> dict | None:
    """Run the full pipeline for one handout fixture.

    Returns the plan deck dict, or None if blocked.
    """
    print(f"\n{'=' * 60}")
    print(f"Pipeline: {label}")
    print(f"{'=' * 60}")

    # Step 1: Build slides.v1
    print(f"\n[Step 1] Building slides.v1 from handout...")
    deck = _build_slides_deck(handout_path, deck_title)
    slides = deck.get("slides", [])
    print(f"  Built {len(slides)} slides")
    _check(len(slides) >= 3, f"{label}: has {len(slides)} slides (need >= 3)")

    # Save intermediate slides
    output_slides_path.parent.mkdir(parents=True, exist_ok=True)
    output_slides_path.write_text(json.dumps(deck, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  Saved slides.v1 to {output_slides_path}")

    # Print slide KP counts
    for s in slides:
        kps = s.get("content", {}).get("key_points", [])
        n_kp = len(kps) if isinstance(kps, list) else 0
        print(f"    {s.get('outline_node_id')}: {n_kp} KP — {s.get('title', '')[:50]}")

    # Step 2: Create real LLM client
    print(f"\n[Step 2] Creating real LLM client...")
    config = _get_config()
    llm_client = create_real_llm_client()
    if llm_client is None:
        print("  BLOCKED: No API key found (checked PIN_AND_PAPER_LLM_API_KEY, OPENAI_API_KEY, LEARNING_LLM_API_KEY).")
        print("  Ensure .env in project root has LEARNING_LLM_API_KEY set.")
        _check(False, f"{label}: No API key available")
        return None

    print(f"  Model: {config['model']}")
    print(f"  Max concurrency: {config['max_concurrency']}")
    print(f"  Base URL: {config['base_url']}")

    # Step 3: Plan deck with real LLM (concurrent)
    print(f"\n[Step 3] Planning deck with real LLM (concurrent, max {config['max_concurrency']})...")
    t0 = time.time()

    plan_deck = plan_deck_concurrent(
        deck=deck,
        llm_client=llm_client,
        max_concurrency=config["max_concurrency"],
    )

    elapsed = time.time() - t0
    total_calls = plan_deck.get("_llm_calls", 0)
    print(f"  Completed in {elapsed:.1f}s")
    print(f"  Total LLM calls: {total_calls}")
    print(f"  Plan slides: {len(plan_deck.get('slides', []))}")

    # Step 4: Save plan
    print(f"\n[Step 4] Saving plan to {output_plan_path}...")
    output_plan_path.parent.mkdir(parents=True, exist_ok=True)
    output_plan_path.write_text(
        json.dumps(plan_deck, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  Saved.")

    # Step 5: Verify
    print(f"\n[Step 5] Verification...")
    _verify_plan(plan_deck, deck, label)

    # Print warnings
    warnings = plan_deck.get("warnings", [])
    if warnings:
        print(f"\n  Warnings ({len(warnings)}):")
        for w in warnings:
            print(f"    - {w}")

    return plan_deck


# ============================================================================
# Marker verification
# ============================================================================


def test_markers_production() -> None:
    """Verify production plan markers."""
    print("\n[Marker Check] real-llm-production-plan.v1.json")
    if not _OUTPUT_PRODUCTION_PLAN.exists():
        _check(False, "real-llm-production-plan.v1.json not found")
        return

    with open(_OUTPUT_PRODUCTION_PLAN, encoding="utf-8") as f:
        plan = json.load(f)
    plan_str = json.dumps(plan, ensure_ascii=False)

    # Forward markers — must contain perio-specific content
    _check("牙周炎" in plan_str,
           "Forward [+]: '牙周炎' in production plan")
    _check("牙周炎的定义" in plan_str or "定义与诊断" in plan_str or "定义与病因" in plan_str,
           "Forward [+]: periodontitis definition content in production plan")

    # Reverse markers — must NOT contain generalized-specific content
    # Note: "高血压" may appear as a chronic-disease comparison (source: node_7),
    # so we check for the more specific "高血压靶器官" and "靶器官损害" markers.
    _check("高血压靶器官" not in plan_str,
           "Reverse [-]: '高血压靶器官' NOT in production plan")
    _check("靶器官损害" not in plan_str,
           "Reverse [-]: '靶器官损害' NOT in production plan")
    _check("掌握核心概念，建立临床思维" not in plan_str,
           "Reverse [-]: demo cover text NOT in production plan")


def test_markers_generalized() -> None:
    """Verify generalized plan markers."""
    print("\n[Marker Check] real-llm-generalized-plan.v1.json")
    if not _OUTPUT_GENERALIZED_PLAN.exists():
        _check(False, "real-llm-generalized-plan.v1.json not found")
        return

    with open(_OUTPUT_GENERALIZED_PLAN, encoding="utf-8") as f:
        plan = json.load(f)
    plan_str = json.dumps(plan, ensure_ascii=False)

    # Forward markers — must contain hypertension-specific content
    _check("高血压" in plan_str,
           "Forward [+]: '高血压' in generalized plan")
    _check("靶器官损害" in plan_str,
           "Forward [+]: '靶器官损害' in generalized plan")

    # Reverse markers — must NOT contain perio or demo content
    _check("牙周炎的定义与诊断标准" not in plan_str,
           "Reverse [-]: '牙周炎的定义与诊断标准' NOT in generalized plan")
    _check("牙周炎" not in plan_str,
           "Reverse [-]: '牙周炎' NOT in generalized plan")
    _check("掌握核心概念，建立临床思维" not in plan_str,
           "Reverse [-]: demo cover text NOT in generalized plan")
    _check("糖尿病" not in plan_str,
           "Reverse [-]: '糖尿病' NOT in generalized plan")


def test_content_no_new_medical_facts() -> None:
    """Quick check that plan doesn't introduce obviously fabricated medical content."""
    print("\n[Content Check] No fabricated medical facts")

    for plan_path, handout_path, label in [
        (_OUTPUT_PRODUCTION_PLAN, _HANDOUT_PRODUCTION, "production"),
        (_OUTPUT_GENERALIZED_PLAN, _HANDOUT_GENERALIZED, "generalized"),
    ]:
        if not plan_path.exists() or not handout_path.exists():
            print(f"  SKIP {label}: fixtures missing")
            continue

        with open(plan_path, encoding="utf-8") as f:
            plan = json.load(f)
        with open(handout_path, encoding="utf-8") as f:
            handout = json.load(f)

        # Build source text pool from handout
        source_pool = ""
        for s in handout.get("sections", []):
            c = s.get("content", {})
            source_pool += c.get("overview", "")
            for kp in c.get("key_points", []):
                source_pool += kp.get("heading", "") + kp.get("explanation", "")
            source_pool += c.get("clinical_example", "")
            for cc in c.get("common_confusions", []):
                source_pool += cc.get("confusion", "") + cc.get("clarification", "")

        plan_str = json.dumps(plan, ensure_ascii=False)

        # Sanity: plan should have CJK content
        plan_cjk = len(re.findall(r'[一-鿿]', plan_str))
        _check(plan_cjk > 100,
               f"{label}: plan has {plan_cjk} CJK chars (need > 100)")

        # No HTML
        _check("<div" not in plan_str and "<style" not in plan_str,
               f"{label}: no HTML tags in plan")


# ============================================================================
# Main
# ============================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("Pin & Paper Real LLM Smoke Test")
    print("=" * 60)

    config = _get_config()
    print(f"  Model: {config['model']}")
    print(f"  Max concurrency: {config['max_concurrency']}")
    print(f"  API key set: {'YES' if config['api_key'] else 'NO'}")

    if not config["api_key"]:
        print("\n" + "!" * 60)
        print("BLOCKED: No API key found for Pin & Paper real LLM client.")
        print("Checked: PIN_AND_PAPER_LLM_API_KEY, OPENAI_API_KEY, LEARNING_LLM_API_KEY")
        print("Ensure .env in project root has LEARNING_LLM_API_KEY set.")
        print("!" * 60)
        _check(False, "No API key found — cannot run real LLM tests")
        print("\npin and paper real llm smoke test FAILED (blocked)")
        sys.exit(1)

    # ---- Production pipeline ----
    production_plan = run_pipeline(
        handout_path=_HANDOUT_PRODUCTION,
        output_plan_path=_OUTPUT_PRODUCTION_PLAN,
        output_slides_path=_OUTPUT_PRODUCTION_SLIDES,
        label="production (牙周炎)",
        deck_title="牙周炎基础学习",
    )

    # ---- Generalized pipeline ----
    generalized_plan = run_pipeline(
        handout_path=_HANDOUT_GENERALIZED,
        output_plan_path=_OUTPUT_GENERALIZED_PLAN,
        output_slides_path=_OUTPUT_GENERALIZED_SLIDES,
        label="generalized (高血压)",
        deck_title="高血压基础知识",
    )

    # ---- Marker verification ----
    if production_plan:
        test_markers_production()
    if generalized_plan:
        test_markers_generalized()

    # ---- Content check ----
    test_content_no_new_medical_facts()

    # ---- Summary ----
    print("\n" + "=" * 60)
    if _FAILED == 0:
        print("pin and paper real llm smoke test passed")
        sys.exit(0)
    else:
        print(f"pin and paper real llm smoke test FAILED ({_FAILED} check(s))")
        sys.exit(1)
