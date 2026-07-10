"""Fixed-handout Tutor smoke test — zero Planner / Resource Factory dependency.

Uses a static handout_section JSON fixture to test Tutor output quality
for both first-section and second-section positions, isolating Tutor from
upstream instability.

Run from the project root::

    python scripts/smoke_test_tutor_fixture.py
    python scripts/smoke_test_tutor_fixture.py --show-teaching
"""

import argparse
import json
import sys
import time
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from backend.services.learning.tutor import (
    build_tutor_step_llm,
    validate_tutor_teaching_text,
)


FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "handout_section_fixture.json"


def _load_fixture() -> dict:
    """Load the fixed handout section fixture."""
    if not FIXTURE_PATH.exists():
        print(f"  [FATAL] Fixture not found: {FIXTURE_PATH}")
        sys.exit(2)
    with open(FIXTURE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _print_separator(title: str) -> None:
    print()
    print("=" * 70)
    print(f"  {title}")
    print("=" * 70)


def _run_tutor_for_position(
    handout_content: dict,
    section_title: str,
    is_first: bool,
    previous_node_title: str | None,
    label: str,
) -> dict:
    """Run Tutor LLM for one section position and return the result dict."""
    total = 5  # Fixed — fixture represents a mid-course section
    current_index = 1 if is_first else 2

    sp = {
        "is_first_section": is_first,
        "is_last_section": False,
        "current_index": current_index,
        "total": total,
    }

    outline_node = {
        "id": "fixture_node_1",
        "title": section_title,
        "learning_objectives": ["理解牙周炎的病因与发病机制"],
        "sequence_reason": "病因是理解临床表现和防治策略的基础",
    }

    kg_node_ids = [
        "kg://牙周病学/010-牙周炎/etiology_1",
        "kg://牙周病学/010-牙周炎/etiology_2",
    ]

    t_start = time.time()
    step = build_tutor_step_llm(
        outline_node=outline_node,
        handout_section_content=handout_content,
        handout_section_title=section_title,
        kg_node_ids=kg_node_ids,
        previous_node_title=previous_node_title,
        next_node_title="牙周炎的临床表现与诊断",
        section_position=sp,
    )
    elapsed = round(time.time() - t_start, 2)

    return {
        "label": label,
        "is_first": is_first,
        "elapsed": elapsed,
        "tutor_step": step,
        "section_position": sp,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fixed-handout Tutor smoke test (no Planner / Resource Factory)"
    )
    parser.add_argument("--show-teaching", action="store_true",
                        help="Print full teaching text and questions")
    args = parser.parse_args()

    # ==========================================================================
    # 1. Load fixture
    # ==========================================================================
    _print_separator("1. Load Fixture")
    fixture = _load_fixture()
    handout_content = {
        "overview": fixture["overview"],
        "key_points": fixture["key_points"],
        "clinical_example": fixture.get("clinical_example", ""),
        "common_confusions": fixture.get("common_confusions", ""),
        "transition_to_next": fixture.get("transition_to_next", ""),
    }
    section_title = fixture["_section_title"]
    kp_headings = [kp["heading"] for kp in fixture["key_points"]]
    print(f"  fixture: {FIXTURE_PATH}")
    print(f"  section_title: {section_title}")
    print(f"  key_points: {len(kp_headings)} — {kp_headings}")
    print(f"  has common_confusions: {bool(fixture.get('common_confusions'))}")
    print(f"  has clinical_example: {bool(fixture.get('clinical_example'))}")

    # ==========================================================================
    # 2. Run Tutor as first_section
    # ==========================================================================
    _print_separator("2. Tutor as FIRST section")
    r1 = _run_tutor_for_position(
        handout_content=handout_content,
        section_title=section_title,
        is_first=True,
        previous_node_title=None,
        label="Section 1 (first)",
    )
    ts1 = r1["tutor_step"]
    print(f"  content_status: {ts1.get('content_status')}")
    print(f"  teaching_chars: {len(ts1.get('teaching_text', ''))}")
    print(f"  questions: {len(ts1.get('questions', []))}")
    print(f"  elapsed: {r1['elapsed']:.2f}s")

    # ==========================================================================
    # 3. Run Tutor as second_section
    # ==========================================================================
    _print_separator("3. Tutor as SECOND section")
    r2 = _run_tutor_for_position(
        handout_content=handout_content,
        section_title=section_title,
        is_first=False,
        previous_node_title="牙周炎的定义、分类与流行病学",
        label="Section 2 (non-first)",
    )
    ts2 = r2["tutor_step"]
    print(f"  content_status: {ts2.get('content_status')}")
    print(f"  teaching_chars: {len(ts2.get('teaching_text', ''))}")
    print(f"  questions: {len(ts2.get('questions', []))}")
    print(f"  elapsed: {r2['elapsed']:.2f}s")

    # ==========================================================================
    # 4. Show real output
    # ==========================================================================
    for r in [r1, r2]:
        ts = r["tutor_step"]
        label = r["label"]

        _print_separator(f"4. {label} — teaching_text")
        print(ts.get("teaching_text", "(EMPTY)"))

        questions = ts.get("questions", [])
        if questions:
            _print_separator(f"4. {label} — Questions")
            for qi, q in enumerate(questions):
                print(f"  Q{qi+1}: {q.get('stem', '')}")
                for opt in q.get("options", []):
                    print(f"    {opt}")
                print(f"  Answer: {q.get('answer', '')}")
                print(f"  Explanation: {q.get('explanation', '')[:200]}")
                print()

    # ==========================================================================
    # 5. Assertions
    # ==========================================================================
    _print_separator("5. Assertions")
    errors: list[str] = []
    kps = handout_content["key_points"]

    # --- 5a. Both sections must be llm_generated ---
    for r in [r1, r2]:
        ts = r["tutor_step"]
        label = r["label"]
        ts_status = ts.get("content_status", "?")
        if ts_status != "llm_generated":
            err_detail = ts.get("content_note", ts.get("trace", {}).get("error", "unknown"))
            errors.append(
                f"{label}: tutor_status={ts_status!r}, expected 'llm_generated'. "
                f"error={err_detail[:200]}"
            )

    # --- 5b. First section: must NOT be only an intro ---
    text1 = ts1.get("teaching_text", "")
    if len(text1) < 200:
        errors.append(
            f"Section 1 (first): teaching_text only {len(text1)} chars — "
            f"must be a full lecture, not just an opening (min 200 chars)"
        )

    # First section: must cover key_points
    val_errs_1 = validate_tutor_teaching_text(text1, kps)
    if val_errs_1:
        for ve in val_errs_1:
            errors.append(f"Section 1 (first): {ve}")

    # First section: must have oral intro
    if not any(p in text1[:200] for p in ["你好呀", "我是 OralSage"]):
        errors.append("Section 1 (first): missing OralSage intro in opening")

    # --- 5c. Second section: must NOT repeat OralSage intro ---
    text2 = ts2.get("teaching_text", "")
    if "你好呀，我是 OralSage" in text2[:200]:
        errors.append("Section 2 (non-first): must NOT repeat '你好呀，我是 OralSage'")

    # Second section: must reference previous section
    prev_refs = ["上一节", "刚才", "前面", "之前", "了解了", "讲过", "上节"]
    if not any(p in text2[:200] for p in prev_refs):
        errors.append(
            "Section 2 (non-first): should reference previous section in opening. "
            f"Opening: {text2[:120]}"
        )

    # Second section: must also be a full lecture (not just transition)
    if len(text2) < 200:
        errors.append(
            f"Section 2 (non-first): teaching_text only {len(text2)} chars — "
            f"must be a full lecture, not just a transition (min 200 chars)"
        )

    val_errs_2 = validate_tutor_teaching_text(text2, kps)
    if val_errs_2:
        for ve in val_errs_2:
            errors.append(f"Section 2 (non-first): {ve}")

    # --- 5d. Both: questions separate from teaching_text ---
    for r in [r1, r2]:
        ts = r["tutor_step"]
        label = r["label"]
        text = ts.get("teaching_text", "")
        questions = ts.get("questions", [])

        # teaching_text must not contain question stems
        for q in questions:
            stem = q.get("stem", "")
            if stem and len(stem) >= 8 and stem[:20] in text:
                errors.append(f"{label}: teaching_text contains question stem '{stem[:40]}...'")

        # Must have questions
        if len(questions) == 0:
            errors.append(f"{label}: no questions generated")
        elif len(questions) < 1:
            errors.append(f"{label}: expected at least 1 question, got {len(questions)}")

        for qi, q in enumerate(questions):
            if not q.get("stem"):
                errors.append(f"{label}: Q{qi+1} empty stem")
            if not q.get("answer"):
                errors.append(f"{label}: Q{qi+1} empty answer")
            if len(q.get("options", [])) != 4:
                errors.append(f"{label}: Q{qi+1} has {len(q.get('options', []))} options, expected 4")

    # --- 5e. Both: required fields ---
    for r in [r1, r2]:
        ts = r["tutor_step"]
        label = r["label"]
        if ts.get("expected_user_action") != "choose_next_action":
            errors.append(f"{label}: expected_user_action={ts.get('expected_user_action')!r}")
        if ts.get("quiz_scope") != "section":
            errors.append(f"{label}: quiz_scope={ts.get('quiz_scope')!r}")
        if ts.get("quiz_timing") != "after_section":
            errors.append(f"{label}: quiz_timing={ts.get('quiz_timing')!r}")

    # --- 5f. No classroom expressions ---
    for r in [r1, r2]:
        ts = r["tutor_step"]
        label = r["label"]
        text = ts.get("teaching_text", "")
        for expr in ["同学们好", "请大家", "各位同学"]:
            if expr in text:
                errors.append(f"{label}: forbidden '{expr}' in teaching_text")

    # ==========================================================================
    # 6. Report
    # ==========================================================================
    if errors:
        _print_separator("6. VERDICT")
        print(f"  [FAIL] {len(errors)} error(s):")
        for e in errors:
            print(f"     - {e}")
        print()
        print(f"  === VERDICT: UNSTABLE ===")
        print(f"  Tutor output quality issues detected with FIXED handout input.")
        print(f"  This is NOT caused by Planner or Resource Factory instability.")
        print(f"  Rerunning may produce a different result.")
        print(f"  Do NOT treat this as 'passed'.")
        sys.exit(1)

    # --- All clear ---
    _print_separator("6. VERDICT")
    print(f"  [PASS] Tutor fixture test passed")
    for r in [r1, r2]:
        ts = r["tutor_step"]
        label = r["label"]
        text = ts.get("teaching_text", "")
        questions = ts.get("questions", [])
        trace = ts.get("trace", {})
        print(f"  {label}:")
        print(f"    teaching_chars={len(text)}  questions={len(questions)}")
        print(f"    tutor_status={ts.get('content_status')!r}  trace_mode={trace.get('mode', '?')!r}")
        print(f"    is_first={r['is_first']}")
        print(f"    elapsed={r['elapsed']:.2f}s")

    print(f"\n  === VERDICT: STABLE (fixed fixture) ===")
    print(f"  Tutor produces full lectures for both first and non-first sections")
    print(f"  with fixed handout input. This result does NOT depend on Planner")
    print(f"  or Resource Factory stability.")
    print(f"\ntutor fixture test passed")


if __name__ == "__main__":
    main()
