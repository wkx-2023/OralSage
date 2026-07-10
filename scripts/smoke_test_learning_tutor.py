"""Smoke test for the LLM-powered Tutor leaf agent (handout-section-driven).

Run from the project root::

    python scripts/smoke_test_learning_tutor.py --topic 牙周炎 --compare-sections --show-teaching --show-timing
    python scripts/smoke_test_learning_tutor.py --topic 牙周炎 --node-id node_1 --show-teaching

Expected output ends with ``tutor smoke test passed``.
"""

import argparse
import json
import sys
import time
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from backend.services.learning.planner import plan_lesson
from backend.services.learning.resource_factory import generate_handout_section
from backend.services.learning.tutor import (
    build_tutor_step,
    build_tutor_step_llm,
    build_tutor_step_rule,
    build_tutor_signals,
    build_tutor_feedback,
    validate_tutor_teaching_text,
)
from backend.services.learning.memory import LearningMemory


def _print_separator(title: str) -> None:
    print()
    print("=" * 60)
    print(f"  {title}")
    print("=" * 60)


# ============================================================================
# Self-check: rule-based fallback + validator
# ============================================================================


def _self_check_rule_based() -> None:
    """Verify rule-based fallback and validator guardrail."""
    print()
    print("=" * 60)
    print("  Self-check: rule-based fallback + validator")
    print("=" * 60)

    # --- rule-based fallback ---
    outline = {
        "title": "test",
        "nodes": [
            {
                "id": "node_1",
                "title": "测试节点",
                "learning_objectives": ["理解核心概念"],
                "kg_node_ids": ["kg://test/core_1"],
                "source_refs": [],
                "difficulty": "basic",
            }
        ],
    }
    step = build_tutor_step_rule(outline=outline, current_node_id="node_1")
    assert step["outline_node_id"] == "node_1"
    assert "测试节点" in step["teaching_text"]
    assert "生成未完成" in step["teaching_text"]
    assert step["content_status"] == "generation_failed"
    assert step["trace"]["fallback_rule_based"] is True
    assert len(step["questions"]) == 0
    assert step["expected_user_action"] == "choose_next_action"
    assert step["quiz_scope"] == "section"
    print("  [PASS] rule-based fallback OK")

    # --- validator: empty text rejected ---
    errs = validate_tutor_teaching_text("", [{"heading": "测试"}])
    assert len(errs) > 0, "empty text should fail"
    print("  [PASS] validator rejects empty text")

    # --- validator: shell-only text rejected ---
    shell_text = "这一节会帮你了解牙周炎的分类。你会学到慢性牙周炎和侵袭性牙周炎的区别。讲义内容目前还没有准备好。"
    errs = validate_tutor_teaching_text(shell_text, [
        {"heading": "慢性牙周炎的特点"},
        {"heading": "侵袭性牙周炎的特点"},
    ])
    assert len(errs) > 0, f"shell text should fail: {errs}"
    print("  [PASS] validator rejects shell/intro-only text")

    # --- validator: real lecture passes ---
    real_text = (
        "上一节你了解了牙周炎的定义。现在我们来谈慢性牙周炎。"
        "慢性牙周炎是最常见的类型，多见于成年人，进展缓慢但持续。"
        "它的核心特征是附着丧失和牙槽骨吸收。临床上需要和侵袭性牙周炎鉴别——"
        "侵袭性牙周炎进展迅速，好发于年轻人，有家族聚集倾向。"
        "两者的鉴别对于制定治疗方案至关重要。"
    )
    errs = validate_tutor_teaching_text(real_text, [
        {"heading": "慢性牙周炎的特点"},
        {"heading": "侵袭性牙周炎的特点"},
    ])
    assert len(errs) == 0, f"real lecture should pass: {errs}"
    print("  [PASS] validator accepts real lecture content")

    # --- validator: real lecture with good coverage ---
    good_text = (
        "牙周炎定义是核心，它是由菌斑生物膜引起的牙周支持组织的慢性感染性疾病，"
        "临床表现为牙周袋形成、附着丧失和牙槽骨吸收。"
        "慢性牙周炎特点包括附着丧失和牙槽骨吸收，临床上最为常见且进展缓慢，"
        "多数患者可以通过基础治疗控制病情。"
        "侵袭性牙周炎特点有家族聚集倾向，进展迅速且好发于年轻患者，需要及早干预，"
        "通常需要结合全身抗生素治疗才能取得良好效果。"
        "以上三类知识点是本节的核心内容。"
    )
    errs = validate_tutor_teaching_text(good_text, [
        {"heading": "牙周炎定义"},
        {"heading": "慢性牙周炎特点"},
        {"heading": "侵袭性牙周炎特点"},
    ])
    assert len(errs) == 0, f"good coverage should pass: {errs}"
    print("  [PASS] validator accepts text covering key_points")

    print("  All self-checks passed.")


# ============================================================================
# Helpers
# ============================================================================


def _generate_one_section(
    outline_nodes: list[dict],
    outline_evidence_map: dict,
    evidence_pool: dict,
    idx: int,
) -> dict:
    """Generate handout section + tutor step for outline_nodes[idx]."""
    node = outline_nodes[idx]
    nid = node.get("id", "")
    ne = outline_evidence_map.get(nid, {})

    prev_title = outline_nodes[idx - 1]["title"] if idx > 0 else None
    next_title = (
        outline_nodes[idx + 1]["title"]
        if idx + 1 < len(outline_nodes)
        else None
    )
    total = len(outline_nodes)

    # Handout section
    hs = generate_handout_section(
        outline_node=node,
        node_evidence=ne,
        evidence_pool=evidence_pool,
        previous_node_title=prev_title,
        next_node_title=next_title,
    )

    hs_content = hs.get("content", {})
    hs_status = hs.get("section_status", "?")
    hs_evidence = hs.get("evidence_status", "?")
    hs_kps = hs_content.get("key_points", []) if isinstance(hs_content, dict) else []

    kg_ids = list(
        hs.get("used_evidence", {}).get("kg_node_ids", ne.get("kg_node_ids", []))
    )

    # Section position
    sp = {
        "is_first_section": idx == 0,
        "is_last_section": idx + 1 == total,
        "current_index": idx + 1,
        "total": total,
    }

    # --- Guard: do NOT call Tutor LLM if handout is invalid ---
    # This prevents the "Tutor generated from outline title" architecture violation.
    handout_valid = (
        hs_status == "llm_generated"
        and hs_evidence == "sufficient"
        and isinstance(hs_kps, list)
        and len(hs_kps) > 0
    )
    if not handout_valid:
        tutor_step = {
            "outline_node_id": nid,
            "teaching_text": "",
            "questions": [],
            "quiz_scope": "section",
            "quiz_timing": "after_section",
            "available_user_actions": ["ask_question", "take_quiz", "continue"],
            "expected_user_action": "choose_next_action",
            "signals": {
                "used_kg_node_ids": [],
                "used_handout_section_title": "",
                "advance_progress": False,
            },
            "trace": {
                "mode": "tutor_skipped",
                "uses_llm": False,
                "uses_handout_section": False,
                "advance_progress": False,
                "error": (
                    f"missing_valid_handout_section: "
                    f"handout_status={hs_status!r}, evidence_status={hs_evidence!r}, "
                    f"key_points_count={len(hs_kps)}. "
                    f"Tutor LLM was NOT called — no valid handout to teach from."
                ),
            },
            "content_status": "failed",
            "content_note": (
                f"Tutor skipped: handout section not valid "
                f"(status={hs_status}, evidence={hs_evidence}, kps={len(hs_kps)}). "
                f"Tutor must have a valid handout section to generate teaching content."
            ),
            "progress": {"current_index": -1, "total": -1},
        }
    else:
        # Tutor step (only called when handout is valid)
        tutor_step = build_tutor_step_llm(
            outline_node=node,
            handout_section_content=hs_content,
            handout_section_title=hs.get("title", ""),
            kg_node_ids=kg_ids,
            previous_node_title=prev_title,
            next_node_title=next_title,
            section_position=sp,
        )

    return {
        "outline_node": node,
        "handout_section": hs,
        "tutor_step": tutor_step,
        "section_position": sp,
    }


# ============================================================================
# Main
# ============================================================================


def main() -> None:
    _self_check_rule_based()

    parser = argparse.ArgumentParser(
        description="Smoke-test the LLM Tutor leaf agent."
    )
    parser.add_argument("--topic", default="牙周炎", help="Topic (default: 牙周炎)")
    parser.add_argument("--node-id", default="node_1", help="Node ID (single-node mode)")
    parser.add_argument("--compare-sections", action="store_true",
                        help="Generate and compare Section 1 + Section 2")
    parser.add_argument("--show-teaching", action="store_true",
                        help="Print full teaching text and questions")
    parser.add_argument("--show-timing", action="store_true",
                        help="Print timing breakdown")
    args = parser.parse_args()

    topic: str = args.topic
    show_teaching = args.show_teaching
    show_timing = args.show_timing

    # ========================================================================
    # 1. Plan lesson
    # ========================================================================
    _print_separator("1. Planner")
    print(f"  topic: {topic}")
    t_plan_start = time.time()
    result = plan_lesson(topic, user_profile={"level": "unknown"})
    planner_seconds = round(time.time() - t_plan_start, 2)

    planning_trace = result.get("planning_trace", {})
    if not planning_trace.get("is_agent_planned"):
        print(f"  [FAIL] Planner not on Agent success path")
        fb = planning_trace.get("fallback_reason", "unknown")
        print(f"    fallback_reason: {fb}")
        sys.exit(1)

    outline = result["outline"]
    outline_evidence_map = result["outline_evidence_map"]
    evidence_pool = result["evidence_pool"]
    outline_nodes = outline.get("nodes", [])
    total_nodes = len(outline_nodes)

    print(f"  is_agent_planned: True")
    print(f"  strategy: {planning_trace.get('strategy', '?')}")
    print(f"  planner_seconds: {planner_seconds:.2f}")
    print(f"  outline_nodes: {total_nodes}")

    if total_nodes < 2 and args.compare_sections:
        print(f"  [FAIL] need at least 2 outline nodes for --compare-sections, got {total_nodes}")
        sys.exit(1)

    # ========================================================================
    # 2. Determine sections to generate
    # ========================================================================
    if args.compare_sections:
        indices = [0, 1]  # first two sections
    else:
        # Find index of requested node_id
        idx = next((i for i, n in enumerate(outline_nodes) if n.get("id") == args.node_id), 0)
        indices = [idx]

    # ========================================================================
    # 3. Setup memory
    # ========================================================================
    memory = LearningMemory()
    user_id = "smoke_tutor_user"
    memory.ensure_user(user_id, "Tutor Smoke Test User")
    session = memory.create_session(user_id, topic)
    session_id = session["id"]
    first_nid = outline_nodes[0]["id"]
    memory.set_current_outline_node(session_id, first_nid)
    memory.save_outline(session_id, outline, result.get("mindmap", {}), result.get("source_trace", {}))
    session_after_setup = memory.get_session(session_id)
    current_node_before = session_after_setup.get("current_outline_node_id")

    # ========================================================================
    # 4. Generate sections
    # ========================================================================
    results: list[dict] = []
    for idx in indices:
        sec_num = idx + 1
        node_title = outline_nodes[idx].get("title", "")
        _print_separator(f"4a. Generating Section {sec_num}: {node_title}")
        t_start = time.time()
        r = _generate_one_section(outline_nodes, outline_evidence_map, evidence_pool, idx)
        elapsed = round(time.time() - t_start, 2)

        hs = r["handout_section"]
        ts = r["tutor_step"]
        sp = r["section_position"]

        hs_status = hs.get("section_status", "?")
        hs_ev = hs.get("evidence_status", "?")
        hs_kps = hs.get("content", {}).get("key_points", [])
        ts_status = ts.get("content_status", "?")
        teaching_text = ts.get("teaching_text", "")
        questions = ts.get("questions", [])

        print(f"  handout: status={hs_status} evidence={hs_ev} key_points={len(hs_kps)}")
        print(f"  tutor:   status={ts_status} teaching_chars={len(teaching_text)} questions={len(questions)}")
        print(f"  section_position: is_first={sp['is_first_section']} is_last={sp['is_last_section']}")
        if show_timing:
            print(f"  elapsed: {elapsed:.2f}s")

        results.append(r)

    # ========================================================================
    # 5. Show content
    # ========================================================================
    if show_teaching:
        for i, r in enumerate(results):
            sec_num = i + 1
            hs = r["handout_section"]
            ts = r["tutor_step"]
            sp = r["section_position"]
            hs_kps = hs.get("content", {}).get("key_points", [])

            _print_separator(f"5. Section {sec_num} — Handout key_points")
            for j, kp in enumerate(hs_kps):
                heading = kp.get("heading", "?")
                exp = kp.get("explanation", "")
                print(f"  [{j+1}] {heading} ({len(exp)} chars)")

            _print_separator(f"5. Section {sec_num} — Tutor teaching_text")
            print(ts.get("teaching_text", "(empty)"))

            questions = ts.get("questions", [])
            if questions:
                _print_separator(f"5. Section {sec_num} — Questions")
                for qi, q in enumerate(questions):
                    print(f"  Q{qi+1}: {q.get('stem', '')}")
                    for opt in q.get("options", []):
                        print(f"    {opt}")
                    print(f"  Answer: {q.get('answer', '')}")
                    print(f"  Explanation: {q.get('explanation', '')[:200]}")
                    print()

    # ========================================================================
    # 6. Assertions
    # ========================================================================
    _print_separator("6. Assertions")
    errors: list[str] = []
    warnings: list[str] = []
    any_section_failed = False
    any_handout_failed_but_tutor_succeeded = False

    for i, r in enumerate(results):
        sec_num = i + 1
        hs = r["handout_section"]
        ts = r["tutor_step"]
        sp = r["section_position"]
        node = r["outline_node"]
        node_title = node.get("title", "")

        hs_status = hs.get("section_status", "?")
        ts_status = ts.get("content_status", "?")
        teaching_text = ts.get("teaching_text", "")
        questions = ts.get("questions", [])
        signals = ts.get("signals", {})
        trace = ts.get("trace", {})
        hs_kps = hs.get("content", {}).get("key_points", [])

        label = f"Section {sec_num} [{node_title}]"

        # --- Per-section diagnostic snapshot ---
        hs_error = hs.get("error", "")
        ts_error = ts.get("content_note", ts.get("trace", {}).get("error", ""))
        trace_mode = trace.get("mode", "?")

        # --- HARD FAILURE: handout not llm_generated ---
        # Any status other than "llm_generated" (failed, partial, fallback_rule_based,
        # placeholder, error, etc.) means the section is NOT valid.
        if hs_status != "llm_generated":
            any_section_failed = True
            errors.append(
                f"{label}: HANDOUT NOT GENERATED "
                f"(section_status={hs_status}, evidence_status={hs_ev}, "
                f"error={hs_error[:200] if hs_error else 'none'}). "
                f"Source: Resource Factory handout section agent. "
                f"Expected section_status='llm_generated'."
            )

        # --- HARD FAILURE: tutor not llm_generated ---
        if ts_status != "llm_generated":
            any_section_failed = True
            errors.append(
                f"{label}: TUTOR NOT GENERATED "
                f"(content_status={ts_status}, "
                f"trace_mode={trace_mode}, "
                f"error={ts_error[:200] if ts_error else 'none'}). "
                f"Source: validator rejected empty/shell teaching_text, "
                f"LLM returned empty output, or JSON parse failed. "
                f"Expected content_status='llm_generated'."
            )

        # --- WARNING: tutor generated without handout (architecture violation) ---
        if hs_status != "llm_generated" and ts_status == "llm_generated" and len(teaching_text) > 50:
            any_section_failed = True
            any_handout_failed_but_tutor_succeeded = True
            warnings.append(
                f"{label}: handout_status={hs_status} but tutor_status=llm_generated "
                f"({len(teaching_text)} chars). Tutor likely generated from outline title, "
                f"not handout section — architecture violation."
            )

        # Skip further quality checks for failed sections (any non-llm_generated)
        if hs_status != "llm_generated" or ts_status != "llm_generated":
            continue

        # --- teaching_text not empty, reasonable length ---
        if not teaching_text or len(teaching_text) < 50:
            errors.append(f"{label}: teaching_text too short ({len(teaching_text)} chars)")

        # --- teaching_text must not contain quiz content ---
        # "小题自检"/"小题自测" = user choosing to self-check → allowed
        # "小测题"/"下面来做几道题"/"Q1:" = actual quiz embedded → NOT allowed
        quiz_markers = ["Q1:", "Q2:", "【小测", "小测题", "下面来做几道题", "来做几道题"]
        has_embedded_quiz = False
        for qm in quiz_markers:
            if qm in teaching_text:
                # Check context: if followed by actual question content (stem/options), flag it
                idx = teaching_text.find(qm)
                after = teaching_text[idx + len(qm):idx + len(qm) + 40]
                if any(c in after for c in ["A.", "B.", "选项", "解析"]):
                    has_embedded_quiz = True
                    errors.append(f"{label}: teaching_text contains embedded quiz '{qm}' — questions must be separate")
                    break
                # If marker appears but without actual question content, it's ok
        # Separately: check for actual question stems embedded in teaching_text
        teached_stems = [q.get("stem", "")[:20] for q in questions if q.get("stem")]
        for stem_prefix in teached_stems:
            if stem_prefix and len(stem_prefix) >= 8 and stem_prefix in teaching_text:
                errors.append(f"{label}: teaching_text contains question stem '{stem_prefix}...' — questions must be separate")
                break

        # --- Questions must exist if handout has key_points ---
        if hs_status == "llm_generated" and len(hs_kps) >= 2 and len(questions) == 0:
            errors.append(f"{label}: no questions generated despite {len(hs_kps)} key_points")

        # --- Question validation ---
        for qi, q in enumerate(questions):
            if not q.get("stem"):
                errors.append(f"{label}: Q{qi+1} empty stem")
            if not q.get("answer"):
                errors.append(f"{label}: Q{qi+1} empty answer")
            if len(q.get("options", [])) != 4:
                errors.append(f"{label}: Q{qi+1} has {len(q.get('options', []))} options, expected 4")

        # --- Quiz semantics ---
        if ts.get("expected_user_action") != "choose_next_action":
            errors.append(f"{label}: expected_user_action={ts.get('expected_user_action')!r}, expected 'choose_next_action'")
        if ts.get("quiz_scope") != "section":
            errors.append(f"{label}: quiz_scope={ts.get('quiz_scope')!r}")
        if ts.get("quiz_timing") != "after_section":
            errors.append(f"{label}: quiz_timing={ts.get('quiz_timing')!r}")
        if ts.get("available_user_actions") != ["ask_question", "take_quiz", "continue"]:
            errors.append(f"{label}: available_user_actions={ts.get('available_user_actions')!r}")

        # --- Signals ---
        if not signals.get("used_kg_node_ids"):
            errors.append(f"{label}: signals.used_kg_node_ids empty")
        if signals.get("advance_progress") is not False:
            errors.append(f"{label}: signals.advance_progress not False")

        # --- Trace ---
        if trace.get("advance_progress") is not False:
            errors.append(f"{label}: trace.advance_progress not False")

        # --- Section-specific checks ---
        if sp["is_first_section"]:
            # Section 1: must have OralSage intro
            has_intro = any(p in teaching_text[:200] for p in ["你好呀", "我是 OralSage"])
            if not has_intro:
                errors.append(f"{label}: Section 1 missing OralSage intro in opening")

            # Section 1: must NOT be only an intro/shell
            if hs_status == "llm_generated" and len(hs_kps) >= 2:
                # Validate against shell patterns
                val_errs = validate_tutor_teaching_text(teaching_text, hs_kps)
                if val_errs:
                    for ve in val_errs:
                        errors.append(f"{label}: {ve}")

                # teaching_text must be substantially longer than just intro
                if len(teaching_text) < 200:
                    errors.append(
                        f"{label}: teaching_text only {len(teaching_text)} chars — "
                        f"Section 1 must be a full lecture, not just an opening"
                    )

        if not sp["is_first_section"]:
            # Section 2+: must NOT repeat OralSage intro
            if "你好呀，我是 OralSage" in teaching_text[:200]:
                errors.append(f"{label}: Section 2+ must not repeat '你好呀，我是 OralSage'")

            # Should reference previous section
            prev_refs = ["上一节", "刚才", "前面", "之前", "了解了", "讲过"]
            has_prev = any(p in teaching_text[:200] for p in prev_refs)
            if not has_prev:
                errors.append(f"{label}: Section 2+ should reference previous section in opening")

        # --- Forbidden classroom expressions ---
        for expr in ["同学们好", "请大家", "各位同学"]:
            if expr in teaching_text:
                errors.append(f"{label}: forbidden '{expr}' in teaching_text")

    # --- Progress check ---
    session_after = memory.get_session(session_id)
    current_node_after = session_after.get("current_outline_node_id")
    if current_node_before != current_node_after:
        errors.append(
            f"Tutor advanced progress! {current_node_before} -> {current_node_after}"
        )

    # --- Explicit check: any_section_failed flag ---
    if any_section_failed and not errors:
        # Edge case: flag set but no error strings added — should never happen,
        # but guard against it explicitly.
        errors.append(
            "any_section_failed=True but no specific errors recorded — "
            "this is a smoke test logic bug, not a Tutor issue."
        )

    # --- Report ---
    if warnings:
        print(f"  [WARN] {len(warnings)} warning(s):")
        for w in warnings:
            print(f"     - {w}")

    if errors:
        print(f"  [FAIL] {len(errors)} error(s):")
        for e in errors:
            print(f"     - {e}")
        print()
        # Print per-section status summary even on failure
        print(f"  --- Per-section status ---")
        for i, r in enumerate(results):
            sec_num = i + 1
            hs = r["handout_section"]
            ts = r["tutor_step"]
            sp = r["section_position"]
            node = r["outline_node"]
            hs_status = hs.get("section_status", "?")
            hs_ev = hs.get("evidence_status", "?")
            ts_status = ts.get("content_status", "?")
            trace = ts.get("trace", {})
            teaching_text = ts.get("teaching_text", "")
            questions = ts.get("questions", [])
            print(f"  Section {sec_num} [{node.get('title', '?')}]:")
            print(f"    handout_status={hs_status!r}  evidence_status={hs_ev!r}")
            print(f"    tutor_status={ts_status!r}  trace_mode={trace.get('mode', '?')!r}")
            print(f"    teaching_chars={len(teaching_text)}  questions={len(questions)}")
            print(f"    is_first={sp['is_first_section']}  is_last={sp['is_last_section']}")
            if trace.get("error"):
                print(f"    trace_error={trace['error'][:200]}")
            if ts.get("content_note"):
                print(f"    content_note={ts['content_note'][:200]}")
        print()
        print(f"  === VERDICT: UNSTABLE ===")
        print(f"  At least one section failed or was in fallback/partial state.")
        print(f"  This is an intermittent LLM output quality issue — not a code bug.")
        print(f"  Rerunning may produce a different result.")
        print(f"  Do NOT treat this as 'passed'.")
        sys.exit(1)

    if any_handout_failed_but_tutor_succeeded:
        print(f"  [WARN] Tutor generated content without valid handout section.")
        print(f"  === VERDICT: UNSTABLE (architecture violation) ===")
        sys.exit(1)

    # --- All clear ---
    print(f"  [PASS] All sections generated successfully")
    print(f"  --- Per-section status ---")
    for i, r in enumerate(results):
        sec_num = i + 1
        hs = r["handout_section"]
        ts = r["tutor_step"]
        sp = r["section_position"]
        node = r["outline_node"]
        hs_status = hs.get("section_status", "?")
        hs_ev = hs.get("evidence_status", "?")
        ts_status = ts.get("content_status", "?")
        trace = ts.get("trace", {})
        teaching_text = ts.get("teaching_text", "")
        questions = ts.get("questions", [])
        print(f"  Section {sec_num} [{node.get('title', '?')}]:")
        print(f"    handout_status={hs_status!r}  evidence_status={hs_ev!r}")
        print(f"    tutor_status={ts_status!r}  trace_mode={trace.get('mode', '?')!r}")
        print(f"    teaching_chars={len(teaching_text)}  questions={len(questions)}")
        print(f"    is_first={sp['is_first_section']}  is_last={sp['is_last_section']}")
        print(f"    quiz_scope={ts.get('quiz_scope')!r}  expected_user_action={ts.get('expected_user_action')!r}")

    if show_timing:
        _print_separator("Timing")
        print(f"  planner: {planner_seconds:.2f}s")

    print(f"\ntutor smoke test passed")


if __name__ == "__main__":
    main()
