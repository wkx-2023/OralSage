"""Smoke test for the QA module with evidence + targeted search.

Run from the project root::

    python scripts/smoke_test_learning_qa.py --topic 牙周炎 --show-answer --show-trace --show-evidence

Tests two scenarios:
  A. Question answerable from current handout_section → should NOT need search.
  B. Question beyond current handout_section but topic-related → SHOULD trigger
     targeted search, and show search_plan / used_evidence.

Acceptance criteria:
  - qa_status MUST be "llm_generated" to pass.
  - fallback_rule_based / failed → NOT passed.
  - If handout_section failed, result is partial/degraded, not PASSED.
  - advance_progress must be False.
  - Full answer text MUST be displayed.
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
from backend.services.learning.qa import (
    answer_question_with_evidence,
    answer_interruption_question,
    _check_handout_valid,
)


def _print_separator(title: str) -> None:
    print()
    print("=" * 70)
    print(f"  {title}")
    print("=" * 70)


def _print_qa_result(qa_result: dict, label: str = "QA Result") -> None:
    """Print a structured QA result."""
    _print_separator(label)
    print(f"  qa_status:                 {qa_result.get('qa_status', '?')}")
    print(f"  question:                  {qa_result.get('question', '')[:120]}")
    print(f"  question_scope:            {qa_result.get('question_scope', '?')}")
    print(f"  evidence_status:           {qa_result.get('evidence_status', '?')}")
    print(f"  targeted_search_used:      {qa_result.get('targeted_search_used', False)}")
    print(f"  degraded_without_handout:  {qa_result.get('degraded_without_handout', '?')}")
    print(f"  advance_progress:          {qa_result.get('advance_progress', '?')}")
    print(f"  should_reinforce:          {qa_result.get('should_reinforce', '?')}")

    # search_plan
    search_plan = qa_result.get("search_plan", [])
    print(f"  search_plan:               {len(search_plan)} queries")
    for i, sq in enumerate(search_plan):
        print(f"    [{i+1}] query:           {sq.get('query', '')[:80]}")
        print(f"         purpose:            {sq.get('purpose', '')[:120]}")
        print(f"         expected_evidence:  {sq.get('expected_evidence', '')[:120]}")
        print(f"         relation:           {sq.get('relation_to_context', '')[:120]}")

    # used_evidence
    used_evidence = qa_result.get("used_evidence", [])
    print(f"  used_evidence:             {len(used_evidence)} items")
    for i, ue in enumerate(used_evidence):
        print(f"    [{i+1}] chunk_id:        {ue.get('chunk_id', '?')[:60]}")
        print(f"         source:             {ue.get('source', '?')[:80]}")
        print(f"         why_used:           {ue.get('why_used', '?')[:120]}")

    # kg node ids
    print(f"  related_kg_node_ids:       {qa_result.get('related_kg_node_ids', [])}")
    print(f"  confusion_kg_node_ids:     {qa_result.get('confusion_kg_node_ids', [])}")

    # trace
    trace = qa_result.get("trace", {})
    print(f"  trace.mode:                {trace.get('mode', '?')}")
    print(f"  trace.uses_llm:            {trace.get('uses_llm', False)}")
    print(f"  trace.uses_handout_section:{trace.get('uses_handout_section', False)}")
    print(f"  trace.degraded_without_handout: {trace.get('degraded_without_handout', '?')}")
    print(f"  trace.targeted_search_used:{trace.get('targeted_search_used', False)}")
    print(f"  trace.fallback_rule_based: {trace.get('fallback_rule_based', False)}")
    if trace.get("search_result_count", 0) > 0:
        print(f"  trace.search_result_count: {trace.get('search_result_count', 0)}")
    if trace.get("qa_status_detail"):
        print(f"  trace.qa_status_detail:    {trace.get('qa_status_detail', '')}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Smoke-test the QA module with evidence + targeted search."
    )
    parser.add_argument(
        "--topic", default="牙周炎", help="Topic to plan (default: 牙周炎)"
    )
    parser.add_argument(
        "--show-answer", action="store_true", help="Print full answer text"
    )
    parser.add_argument(
        "--show-trace", action="store_true", help="Print full trace dict"
    )
    parser.add_argument(
        "--show-evidence", action="store_true", help="Print full used_evidence"
    )
    args = parser.parse_args()

    topic: str = args.topic
    show_answer = args.show_answer
    show_trace = args.show_trace
    show_evidence = args.show_evidence

    # ====================================================================
    # 1. Plan the lesson
    # ====================================================================
    _print_separator("1. Planning Lesson")
    print(f"  topic: {topic}")

    t_planner_start = time.time()
    result = plan_lesson(topic, user_profile={"level": "unknown"})
    planner_seconds = round(time.time() - t_planner_start, 2)

    planning_trace = result.get("planning_trace", {})
    is_agent = planning_trace.get("is_agent_planned", False)
    print(f"  is_agent_planned: {is_agent}")
    print(f"  planner_seconds:  {planner_seconds:.2f}")

    if not is_agent:
        fb_reason = planning_trace.get("fallback_reason", "unknown")
        print(f"  [FAIL] Planner not on Agent success path.")
        print(f"    fallback_reason: {fb_reason[:300]}")
        sys.exit(1)

    outline = result.get("outline", {})
    outline_evidence_map = result.get("outline_evidence_map", {})
    evidence_pool = result.get("evidence_pool", {})

    outline_nodes = outline.get("nodes", [])
    if not outline_nodes:
        print(f"  [FAIL] Outline has no nodes.")
        sys.exit(1)

    # Use first node as current
    current_node = outline_nodes[0]
    current_node_id = current_node.get("id", "")
    current_node_title = current_node.get("title", "")

    print(f"  current_node_id:    {current_node_id}")
    print(f"  current_node_title: {current_node_title}")
    print(f"  total_nodes:        {len(outline_nodes)}")

    # Get node_evidence
    node_evidence = outline_evidence_map.get(current_node_id, {})
    if not node_evidence:
        print(f"  [FAIL] No evidence map entry for {current_node_id}")
        sys.exit(1)

    kg_nodes_count = len(node_evidence.get("kg_nodes", []))
    chunks_count = len(node_evidence.get("evidence_chunks", []))
    print(f"  evidence:           {kg_nodes_count} kg_nodes, {chunks_count} chunks")

    # ====================================================================
    # 2. Generate handout section for current node
    # ====================================================================
    _print_separator("2. Generating Handout Section")

    # Determine previous/next titles
    prev_title = None
    next_title = None
    for i, n in enumerate(outline_nodes):
        if n.get("id") == current_node_id:
            if i > 0:
                prev_title = outline_nodes[i - 1].get("title")
            if i + 1 < len(outline_nodes):
                next_title = outline_nodes[i + 1].get("title")
            break

    t_handout_start = time.time()
    handout_section = generate_handout_section(
        outline_node=current_node,
        node_evidence=node_evidence,
        evidence_pool=evidence_pool,
        user_profile={"level": "unknown"},
        previous_node_title=prev_title,
        next_node_title=next_title,
    )
    handout_seconds = round(time.time() - t_handout_start, 2)

    hs_status = handout_section.get("section_status", "?")
    handout_valid = _check_handout_valid(handout_section)
    print(f"  handout_section_status:  {hs_status}")
    print(f"  handout_valid:           {handout_valid}")
    print(f"  handout_seconds:         {handout_seconds:.2f}")

    if not handout_valid:
        hs_error = handout_section.get("error", "")
        print(f"  [WARN] Handout section NOT valid (status={hs_status})")
        if hs_error:
            print(f"    error: {hs_error}")

    # Extract handout content summary
    handout_content = handout_section.get("content", {})
    key_points = handout_content.get("key_points", [])
    overview = handout_content.get("overview", "")
    print(f"  key_points:              {len(key_points)}")
    print(f"  overview_chars:          {len(overview)}")
    for i, kp in enumerate(key_points):
        print(f"    [{i+1}] {kp.get('heading', '?')}")

    # ====================================================================
    # 3. Test A: In-section question (should NOT need search)
    # ====================================================================
    _print_separator("3. Test A: In-Section Question")

    if key_points and key_points[0].get("heading"):
        question_a = (
            f"关于{current_node_title}中提到的{key_points[0]['heading']}，"
            f"能再详细解释一下吗？"
        )
    else:
        question_a = f"关于{current_node_title}，能再详细解释一下吗？"

    print(f"  Question A: {question_a}")

    qa_result_a = answer_question_with_evidence(
        question=question_a,
        topic=topic,
        current_outline_node=current_node,
        current_handout_section=handout_section,
        node_evidence=node_evidence,
        evidence_pool=evidence_pool,
        user_profile={"level": "unknown"},
        outline=outline,
        current_node_id=current_node_id,
    )

    _print_qa_result(qa_result_a, "QA Result A: In-Section Question")

    # --- Always show full answer ---
    _print_separator("Answer A (FULL TEXT)")
    print(qa_result_a.get("answer", "(empty)"))

    if show_evidence:
        _print_separator("Used Evidence A")
        print(json.dumps(qa_result_a.get("used_evidence", []), ensure_ascii=False, indent=2))

    if show_trace:
        _print_separator("Trace A")
        print(json.dumps(qa_result_a.get("trace", {}), ensure_ascii=False, indent=2))

    # ====================================================================
    # 4. Test B: Adjacent topic question (SHOULD trigger targeted search)
    # ====================================================================
    _print_separator("4. Test B: Topic-Related Question (Beyond Section)")

    question_b = (
        f"我正在学{current_node_title}。{topic}在临床上有哪些治疗原则？"
        f"不同分期分级的治疗策略有什么不同？"
    )
    print(f"  Question B: {question_b}")

    qa_result_b = answer_question_with_evidence(
        question=question_b,
        topic=topic,
        current_outline_node=current_node,
        current_handout_section=handout_section,
        node_evidence=node_evidence,
        evidence_pool=evidence_pool,
        user_profile={"level": "unknown"},
        outline=outline,
        current_node_id=current_node_id,
    )

    _print_qa_result(qa_result_b, "QA Result B: Topic-Related Question")

    # --- Always show full answer ---
    _print_separator("Answer B (FULL TEXT)")
    print(qa_result_b.get("answer", "(empty)"))

    if show_evidence:
        _print_separator("Used Evidence B")
        print(json.dumps(qa_result_b.get("used_evidence", []), ensure_ascii=False, indent=2))

    if show_trace:
        _print_separator("Trace B")
        print(json.dumps(qa_result_b.get("trace", {}), ensure_ascii=False, indent=2))

    # ====================================================================
    # 5. Assertions
    # ====================================================================
    _print_separator("5. Assertions")

    errors: list[str] = []
    warnings: list[str] = []

    # --- Handout status ---
    if not handout_valid:
        warnings.append(
            f"Handout section NOT valid (status={hs_status}). "
            f"QA results will be degraded_without_handout=True."
        )

    # --- QA A assertions ---
    qa_status_a = qa_result_a.get("qa_status", "")
    if qa_status_a == "fallback_rule_based":
        errors.append(f"A: qa_status is fallback_rule_based — NOT acceptable for PASSED")
    elif qa_status_a == "failed":
        errors.append(f"A: qa_status is failed")
    elif qa_status_a != "llm_generated":
        errors.append(f"A: unexpected qa_status: {qa_status_a!r}")

    if qa_result_a.get("advance_progress") is not False:
        errors.append(f"A: advance_progress must be False")

    if not qa_result_a.get("answer"):
        errors.append("A: answer must not be empty")

    # Check handout trace consistency
    trace_a = qa_result_a.get("trace", {})
    if handout_valid:
        if trace_a.get("uses_handout_section") is not True:
            errors.append(
                "A: handout_valid=True but trace.uses_handout_section is not True"
            )
        if qa_result_a.get("degraded_without_handout") is not False:
            errors.append(
                "A: handout_valid=True but degraded_without_handout is not False"
            )
    else:
        if trace_a.get("uses_handout_section") is not False:
            errors.append(
                "A: handout_valid=False but trace.uses_handout_section is not False"
            )
        if qa_result_a.get("degraded_without_handout") is not True:
            errors.append(
                "A: handout_valid=False but degraded_without_handout is not True"
            )

    # For in-section question with valid handout, we expect no search
    search_plan_a = qa_result_a.get("search_plan", [])
    ts_used_a = qa_result_a.get("targeted_search_used", False)
    if handout_valid and ts_used_a:
        warnings.append(f"A: used targeted search despite valid handout — unusual for in-section Q")

    # --- QA B assertions ---
    qa_status_b = qa_result_b.get("qa_status", "")
    if qa_status_b == "fallback_rule_based":
        errors.append(f"B: qa_status is fallback_rule_based — NOT acceptable for PASSED")
    elif qa_status_b == "failed":
        errors.append(f"B: qa_status is failed")
    elif qa_status_b != "llm_generated":
        errors.append(f"B: unexpected qa_status: {qa_status_b!r}")

    if qa_result_b.get("advance_progress") is not False:
        errors.append(f"B: advance_progress must be False")

    if not qa_result_b.get("answer"):
        errors.append("B: answer must not be empty")

    # Check handout trace consistency for B
    trace_b = qa_result_b.get("trace", {})
    if handout_valid:
        if trace_b.get("uses_handout_section") is not True:
            errors.append(
                "B: handout_valid=True but trace.uses_handout_section is not True"
            )
    else:
        if trace_b.get("uses_handout_section") is not False:
            errors.append(
                "B: handout_valid=False but trace.uses_handout_section is not False"
            )

    # For topic-related question, we want targeted search
    search_plan_b = qa_result_b.get("search_plan", [])
    ts_used_b = qa_result_b.get("targeted_search_used", False)
    if ts_used_b and len(search_plan_b) > 0:
        print(f"  [OK] B used targeted search ({len(search_plan_b)} queries)")
    else:
        warnings.append(
            f"B: targeted_search_used={ts_used_b}, search_plan count={len(search_plan_b)}. "
            f"Expected search for cross-section question."
        )

    # --- QA must NOT advance progress ---
    if "next_outline_node_id" in qa_result_a or "next_outline_node_id" in qa_result_b:
        errors.append("QA result must NOT contain next_outline_node_id")

    # --- Report warnings ---
    if warnings:
        print()
        for w in warnings:
            print(f"  [WARN] {w}")

    # --- Verdict ---
    _print_separator("6. Verdict")

    handout_note = "handout=VALID" if handout_valid else f"handout=INVALID(status={hs_status})"
    print(f"  Handout:         {handout_note}")

    if errors:
        print(f"\n  [FAIL] {len(errors)} assertion(s) failed:")
        for e in errors:
            print(f"     - {e}")
        print(f"\n  Verdict: FAILED")
        sys.exit(1)

    # Determine final verdict
    if handout_valid:
        if qa_status_a == "llm_generated" and qa_status_b == "llm_generated":
            print(f"  QA A status:     {qa_status_a}")
            print(f"  QA B status:     {qa_status_b}")
            print(f"  advance_progress: A=False, B=False")
            print(f"\n  Verdict: PASSED")
        else:
            print(f"  QA A status:     {qa_status_a}")
            print(f"  QA B status:     {qa_status_b}")
            print(f"\n  Verdict: PARTIAL (one or both QA not llm_generated)")
    else:
        # Handout invalid — QA ran degraded
        degraded_a = qa_result_a.get("degraded_without_handout", False)
        degraded_b = qa_result_b.get("degraded_without_handout", False)
        print(f"  QA A: qa_status={qa_status_a}, degraded_without_handout={degraded_a}")
        print(f"  QA B: qa_status={qa_status_b}, degraded_without_handout={degraded_b}")
        print(f"  advance_progress: A=False, B=False")
        if qa_status_a == "llm_generated" and qa_status_b == "llm_generated":
            print(f"\n  Verdict: PARTIAL (QA llm_generated but degraded_without_handout)")
        else:
            print(f"\n  Verdict: DEGRADED (handout invalid + QA not fully llm_generated)")

    print(f"\nlearning qa smoke test completed")


if __name__ == "__main__":
    main()
