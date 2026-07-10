"""Smoke test for the per-node Handout Section Agent.

Run from the project root::

    python scripts/smoke_test_learning_handout_section.py --topic 牙周炎 --node-id node_1
    python scripts/smoke_test_learning_handout_section.py --topic 牙周炎 --node-id node_4
    python scripts/smoke_test_learning_handout_section.py --topic 牙周炎 --node-id node_1 --show-section --show-timing

Expected output ends with ``handout section smoke test passed``.
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
from backend.services.learning.resource_factory import (
    generate_handout_section,
    normalize_handout_section_output,
    validate_handout_section_output,
    _collect_allowed_evidence_sets,
    sanitize_handout_evidence_plan,
)


def _print_separator(title: str) -> None:
    print()
    print("=" * 60)
    print(f"  {title}")
    print("=" * 60)


# ============================================================================
# Self-check: type-specific validation (no LLM)
# ============================================================================


def _self_check_type_specific_validation() -> None:
    """Non-LLM self-check: verify type-specific evidence validation.

    Targeted search chunk ids must ONLY be allowed in:
      - used_evidence.evidence_chunk_ids
      - evidence_refs (content body)

    They must be REJECTED in:
      - used_evidence.kg_node_ids
      - used_evidence.kg_edge_ids
      - used_evidence.concept_ids
      - used_evidence.question_ids

    Unknown ids must be rejected everywhere.
    """
    print()
    print("=" * 60)
    print("  Self-check: type-specific validation")
    print("=" * 60)

    # Stub node_evidence with known ids
    node_evidence = {
        "outline_node_id": "node_1",
        "kg_node_ids": ["kg://test/core_1"],
        "kg_nodes": [{"node_id": "kg://test/core_1", "canonical_name": "test"}],
        "kg_edges": [{"edge_id": "edge_test_1", "relation": "HAS_FEATURE"}],
        "concepts": [{"node_id": "kg://test/core_1", "name": "test_concept"}],
        "questions": [{"question_id": "question:test_1", "stem": "test?"}],
        "evidence_chunks": [{"chunk_id": "chunk:node_owned", "title": "test"}],
    }

    ts_chunk_id = "chunk:from_targeted_search"
    unknown_id = "chunk:completely_unknown_xyz"

    extra_chunk_ids: set[str] = {ts_chunk_id}

    def _mk_section(**overrides) -> dict:
        base = normalize_handout_section_output({
            "outline_node_id": "node_1",
            "title": "test",
            "section_status": "llm_generated",
            "evidence_status": "sufficient",
            "used_evidence": {
                "kg_node_ids": ["kg://test/core_1"],
                "kg_edge_ids": [],
                "concept_ids": ["kg://test/core_1"],
                "question_ids": [],
                "evidence_chunk_ids": ["chunk:node_owned"],
            },
            "content": {
                "overview": "ok",
                "key_points": [
                    {"heading": "h1", "explanation": "e1", "evidence_refs": ["chunk:node_owned"]},
                    {"heading": "h2", "explanation": "e2", "evidence_refs": ["kg://test/core_1"]},
                ],
                "clinical_example": "",
                "common_confusions": [],
                "transition_to_next": "",
            },
        })
        # Apply overrides
        for k, v in overrides.items():
            if k == "ue_kg_node_ids":
                base["used_evidence"]["kg_node_ids"] = v
            elif k == "ue_kg_edge_ids":
                base["used_evidence"]["kg_edge_ids"] = v
            elif k == "ue_concept_ids":
                base["used_evidence"]["concept_ids"] = v
            elif k == "ue_question_ids":
                base["used_evidence"]["question_ids"] = v
            elif k == "ue_chunk_ids":
                base["used_evidence"]["evidence_chunk_ids"] = v
            elif k == "evidence_refs":
                base["content"]["key_points"][0]["evidence_refs"] = v
        return base

    all_passed = 0
    all_failed = 0

    # Case 1: targeted search chunk in evidence_chunk_ids → PASS
    s1 = _mk_section(ue_chunk_ids=["chunk:node_owned", ts_chunk_id])
    e1 = validate_handout_section_output(s1, node_evidence, extra_chunk_ids=extra_chunk_ids)
    assert len(e1) == 0, f"Case 1 should pass, got: {e1}"
    print(f"  [PASS] Case 1: ts chunk in evidence_chunk_ids -> valid")
    all_passed += 1

    # Case 2: targeted search chunk in kg_node_ids → FAIL
    s2 = _mk_section(ue_kg_node_ids=["kg://test/core_1", ts_chunk_id])
    e2 = validate_handout_section_output(s2, node_evidence, extra_chunk_ids=extra_chunk_ids)
    assert len(e2) > 0, "Case 2 should fail"
    assert any("kg_node_ids" in err for err in e2), f"Case 2 error should mention kg_node_ids: {e2}"
    print(f"  [PASS] Case 2: ts chunk in kg_node_ids -> rejected")
    all_passed += 1

    # Case 3: targeted search chunk in kg_edge_ids → FAIL
    s3 = _mk_section(ue_kg_edge_ids=[ts_chunk_id])
    e3 = validate_handout_section_output(s3, node_evidence, extra_chunk_ids=extra_chunk_ids)
    assert len(e3) > 0, "Case 3 should fail"
    assert any("kg_edge_ids" in err for err in e3), f"Case 3 error should mention kg_edge_ids: {e3}"
    print(f"  [PASS] Case 3: ts chunk in kg_edge_ids -> rejected")
    all_passed += 1

    # Case 4: targeted search chunk in concept_ids → FAIL
    s4 = _mk_section(ue_concept_ids=[ts_chunk_id])
    e4 = validate_handout_section_output(s4, node_evidence, extra_chunk_ids=extra_chunk_ids)
    assert len(e4) > 0, "Case 4 should fail"
    assert any("concept_ids" in err for err in e4), f"Case 4 error should mention concept_ids: {e4}"
    print(f"  [PASS] Case 4: ts chunk in concept_ids -> rejected")
    all_passed += 1

    # Case 5: targeted search chunk in question_ids → FAIL
    s5 = _mk_section(ue_question_ids=[ts_chunk_id])
    e5 = validate_handout_section_output(s5, node_evidence, extra_chunk_ids=extra_chunk_ids)
    assert len(e5) > 0, "Case 5 should fail"
    assert any("question_ids" in err for err in e5), f"Case 5 error should mention question_ids: {e5}"
    print(f"  [PASS] Case 5: ts chunk in question_ids -> rejected")
    all_passed += 1

    # Case 6: targeted search chunk in evidence_refs → PASS
    s6 = _mk_section(evidence_refs=[ts_chunk_id])
    e6 = validate_handout_section_output(s6, node_evidence, extra_chunk_ids=extra_chunk_ids)
    assert len(e6) == 0, f"Case 6 should pass, got: {e6}"
    print(f"  [PASS] Case 6: ts chunk in evidence_refs -> valid")
    all_passed += 1

    # Case 7: completely unknown id in evidence_chunk_ids → FAIL
    s7 = _mk_section(ue_chunk_ids=[unknown_id])
    e7 = validate_handout_section_output(s7, node_evidence, extra_chunk_ids=extra_chunk_ids)
    assert len(e7) > 0, "Case 7 should fail"
    print(f"  [PASS] Case 7: unknown id in evidence_chunk_ids -> rejected")
    all_passed += 1

    # Case 8: completely unknown id in evidence_refs → FAIL
    s8 = _mk_section(evidence_refs=[unknown_id])
    e8 = validate_handout_section_output(s8, node_evidence, extra_chunk_ids=extra_chunk_ids)
    assert len(e8) > 0, "Case 8 should fail"
    print(f"  [PASS] Case 8: unknown id in evidence_refs -> rejected")
    all_passed += 1

    # Case 9: valid node_evidence id + ts chunk in evidence_chunk_ids → PASS
    s9 = _mk_section(ue_chunk_ids=["chunk:node_owned", ts_chunk_id])
    e9 = validate_handout_section_output(s9, node_evidence, extra_chunk_ids=extra_chunk_ids)
    assert len(e9) == 0, f"Case 9 should pass, got: {e9}"
    print(f"  [PASS] Case 9: node chunk + ts chunk in evidence_chunk_ids -> valid")
    all_passed += 1

    # Case 10: normalize still doesn't default bad statuses
    bad = normalize_handout_section_output({
        "outline_node_id": "node_1",
        "title": "test",
        "content": {"key_points": []},
    })
    errs_status = validate_handout_section_output(bad, node_evidence)
    assert any("section_status" in e for e in errs_status), "should catch missing section_status"
    assert bad.get("section_status") != "llm_generated", "normalize must not default to llm_generated"
    assert bad.get("evidence_status") != "sufficient", "normalize must not default to sufficient"
    print(f"  [PASS] Case 10: normalize still rejects bad statuses")

    print(f"  All {all_passed} type-specific self-checks passed.")
    print()


# ============================================================================
# Self-check: Round 1 plan sanitize (no LLM)
# ============================================================================


def _self_check_plan_sanitize() -> None:
    """Non-LLM self-check: sanitize_handout_evidence_plan removes invalid ids
    without failing, and preserves valid ids and targeted_search_queries."""
    print()
    print("=" * 60)
    print("  Self-check: plan sanitize")
    print("=" * 60)

    node_evidence = {
        "outline_node_id": "node_1",
        "kg_node_ids": ["kg://test/core_1"],
        "kg_nodes": [{"node_id": "kg://test/core_1", "canonical_name": "test"}],
        "kg_edges": [{"edge_id": "edge_valid", "relation": "HAS_FEATURE"}],
        "concepts": [{"node_id": "kg://test/core_1", "name": "test"}],
        "questions": [{"question_id": "question:valid", "stem": "test?"}],
        "evidence_chunks": [{"chunk_id": "chunk:valid", "title": "test"}],
    }

    all_passed = 0

    # Case 1: valid ids — all kept, no warnings
    plan1 = {
        "requested_chunk_ids": ["chunk:valid"],
        "requested_kg_node_ids": ["kg://test/core_1"],
        "requested_kg_edge_ids": ["edge_valid"],
        "requested_question_ids": ["question:valid"],
        "targeted_search_queries": [{"query": "test query", "purpose": "test"}],
    }
    cleaned1, warnings1 = sanitize_handout_evidence_plan(plan1, node_evidence)
    assert cleaned1["requested_chunk_ids"] == ["chunk:valid"], f"Case 1 chunk: {cleaned1['requested_chunk_ids']}"
    assert cleaned1["requested_kg_node_ids"] == ["kg://test/core_1"], f"Case 1 kg_node: {cleaned1['requested_kg_node_ids']}"
    assert warnings1 == [], f"Case 1 should have no warnings: {warnings1}"
    print(f"  [PASS] Case 1: all valid ids kept, no warnings")
    all_passed += 1

    # Case 2: invalid ids — removed, warnings generated
    plan2 = {
        "requested_chunk_ids": ["chunk:valid", "chunk:invalid_xyz"],
        "requested_kg_node_ids": ["kg://test/core_1", "kg://fake/node"],
        "requested_kg_edge_ids": ["edge_valid", "edge_fake"],
        "requested_question_ids": ["question:valid", "question:fake"],
    }
    cleaned2, warnings2 = sanitize_handout_evidence_plan(plan2, node_evidence)
    assert cleaned2["requested_chunk_ids"] == ["chunk:valid"], f"Case 2 chunk: {cleaned2['requested_chunk_ids']}"
    assert cleaned2["requested_kg_node_ids"] == ["kg://test/core_1"], f"Case 2 kg_node: {cleaned2['requested_kg_node_ids']}"
    assert cleaned2["requested_kg_edge_ids"] == ["edge_valid"], f"Case 2 edge: {cleaned2['requested_kg_edge_ids']}"
    assert cleaned2["requested_question_ids"] == ["question:valid"], f"Case 2 question: {cleaned2['requested_question_ids']}"
    assert len(warnings2) == 4, f"Case 2 should have 4 warnings: {warnings2}"
    print(f"  [PASS] Case 2: 4 invalid ids removed, 4 warnings")
    all_passed += 1

    # Case 3: all ids invalid — all removed, warnings, still no crash
    plan3 = {
        "requested_chunk_ids": ["chunk:bad"],
        "requested_kg_node_ids": ["kg://bad"],
        "requested_kg_edge_ids": ["edge_bad"],
        "requested_question_ids": ["question:bad"],
    }
    cleaned3, warnings3 = sanitize_handout_evidence_plan(plan3, node_evidence)
    assert cleaned3["requested_chunk_ids"] == [], f"Case 3 chunk: {cleaned3['requested_chunk_ids']}"
    assert cleaned3["requested_kg_node_ids"] == [], f"Case 3 kg_node: {cleaned3['requested_kg_node_ids']}"
    assert len(warnings3) == 4
    print(f"  [PASS] Case 3: all ids invalid → all removed, no crash")
    all_passed += 1

    # Case 4: targeted_search_queries untouched by sanitize
    tqs = [{"query": "test", "purpose": "test purpose"}]
    plan4 = {
        "requested_chunk_ids": ["chunk:bad"],
        "targeted_search_queries": tqs,
    }
    cleaned4, warnings4 = sanitize_handout_evidence_plan(plan4, node_evidence)
    assert cleaned4["targeted_search_queries"] == tqs, f"Case 4 tqs: {cleaned4['targeted_search_queries']}"
    assert len(warnings4) == 1
    print(f"  [PASS] Case 4: targeted_search_queries untouched by sanitize")
    all_passed += 1

    # Case 5: original plan NOT mutated
    plan5 = {"requested_chunk_ids": ["chunk:bad"]}
    _original = list(plan5["requested_chunk_ids"])
    _, _ = sanitize_handout_evidence_plan(plan5, node_evidence)
    assert plan5["requested_chunk_ids"] == _original, f"Case 5: original mutated: {plan5['requested_chunk_ids']}"
    print(f"  [PASS] Case 5: original plan not mutated")
    all_passed += 1

    # Case 6: chunk id in requested_kg_node_ids → type mismatch, removed
    plan6 = {"requested_kg_node_ids": ["chunk:valid", "kg://test/core_1"]}
    cleaned6, warnings6 = sanitize_handout_evidence_plan(plan6, node_evidence)
    assert cleaned6["requested_kg_node_ids"] == ["kg://test/core_1"], f"Case 6: {cleaned6['requested_kg_node_ids']}"
    assert any("requested_kg_node_ids" in w and "chunk:valid" in w for w in warnings6), f"Case 6 warnings: {warnings6}"
    print(f"  [PASS] Case 6: chunk id in kg_node_ids -> type mismatch, removed")
    all_passed += 1

    # Case 7: kg_node id in requested_chunk_ids → type mismatch, removed
    plan7 = {"requested_chunk_ids": ["chunk:valid", "kg://test/core_1"]}
    cleaned7, warnings7 = sanitize_handout_evidence_plan(plan7, node_evidence)
    assert cleaned7["requested_chunk_ids"] == ["chunk:valid"], f"Case 7: {cleaned7['requested_chunk_ids']}"
    assert any("requested_chunk_ids" in w and "kg://test/core_1" in w for w in warnings7)
    print(f"  [PASS] Case 7: kg_node id in chunk_ids -> type mismatch, removed")
    all_passed += 1

    # Case 8: edge id in requested_question_ids → type mismatch, removed
    plan8 = {"requested_question_ids": ["question:valid", "edge_valid"]}
    cleaned8, warnings8 = sanitize_handout_evidence_plan(plan8, node_evidence)
    assert cleaned8["requested_question_ids"] == ["question:valid"], f"Case 8: {cleaned8['requested_question_ids']}"
    assert any("requested_question_ids" in w and "edge_valid" in w for w in warnings8)
    print(f"  [PASS] Case 8: edge id in question_ids -> type mismatch, removed")
    all_passed += 1

    # Case 9: question id in requested_kg_edge_ids → type mismatch, removed
    plan9 = {"requested_kg_edge_ids": ["edge_valid", "question:valid"]}
    cleaned9, warnings9 = sanitize_handout_evidence_plan(plan9, node_evidence)
    assert cleaned9["requested_kg_edge_ids"] == ["edge_valid"], f"Case 9: {cleaned9['requested_kg_edge_ids']}"
    assert any("requested_kg_edge_ids" in w and "question:valid" in w for w in warnings9)
    print(f"  [PASS] Case 9: question id in edge_ids -> type mismatch, removed")
    all_passed += 1

    # Case 10: _validate_plan_requested_ids rejects type-mismatched id (fatal fallback)
    from backend.services.learning.resource_factory import _validate_plan_requested_ids
    plan10 = {"requested_kg_node_ids": ["chunk:valid"]}
    errs10 = _validate_plan_requested_ids(plan10, node_evidence)
    assert any("kg_node_ids" in e and "chunk:valid" in e for e in errs10), f"Case 10: {errs10}"
    print(f"  [PASS] Case 10: validate rejects type-mismatched id (fatal fallback)")
    all_passed += 1

    print(f"  All {all_passed} plan-sanitize self-checks passed.")
    print()


# ============================================================================
# Timing display helper
# ============================================================================


def _print_timings(trace: dict, planner_seconds: float | None = None) -> None:
    """Print timing breakdown from generation trace."""
    timings = trace.get("timings", {})
    if not timings:
        print(f"  (no timing data)")
        return

    _print_separator("Timing")
    if planner_seconds is not None:
        print(f"  planner_total_seconds:         {planner_seconds:.2f}")
    print(f"  round1_plan_seconds:           {timings.get('round1_plan_seconds', '?'):.2f}" if isinstance(timings.get('round1_plan_seconds'), (int, float)) else f"  round1_plan_seconds:           {timings.get('round1_plan_seconds', '?')}")
    print(f"  targeted_search_seconds:       {timings.get('targeted_search_seconds', 0):.2f}")
    print(f"  targeted_search_query_count:   {timings.get('targeted_search_query_count', 0)}")
    for i, qt in enumerate(timings.get("targeted_search_query_timings", [])):
        print(f"    query {i+1}: {qt.get('seconds', 0):.2f}s, results: {qt.get('result_count', 0)}")
    r2s = timings.get('round2_write_seconds', '?')
    if isinstance(r2s, (int, float)):
        print(f"  round2_write_seconds:          {r2s:.2f}")
    else:
        print(f"  round2_write_seconds:          {r2s}")
    vs = timings.get('validate_seconds', '?')
    if isinstance(vs, (int, float)):
        print(f"  validate_seconds:              {vs:.3f}")
    else:
        print(f"  validate_seconds:              {vs}")
    ts = timings.get('total_section_seconds', '?')
    if isinstance(ts, (int, float)):
        print(f"  total_section_seconds:         {ts:.2f}")
    else:
        print(f"  total_section_seconds:         {ts}")


# ============================================================================
# Main
# ============================================================================


def main() -> None:
    # --- self-checks first (no LLM, fast) ---
    _self_check_type_specific_validation()
    _self_check_plan_sanitize()

    parser = argparse.ArgumentParser(
        description="Smoke-test the Handout Section Agent."
    )
    parser.add_argument(
        "--topic", default="牙周炎", help="Topic to plan (default: 牙周炎)"
    )
    parser.add_argument(
        "--node-id", default="node_1", help="Node ID to generate handout for (default: node_1)"
    )
    parser.add_argument(
        "--show-section",
        action="store_true",
        help="Print full section content JSON",
    )
    parser.add_argument(
        "--show-timing",
        action="store_true",
        help="Print per-phase timing breakdown",
    )
    args = parser.parse_args()

    topic: str = args.topic
    node_id: str = args.node_id
    show_section = args.show_section
    show_timing = args.show_timing

    # ====================================================================
    # 1. Plan the lesson
    # ====================================================================
    print(f"Planning lesson for topic: {topic}")
    t_planner_start = time.time()
    result = plan_lesson(topic, user_profile={"level": "unknown"})
    planner_seconds = round(time.time() - t_planner_start, 2)

    planning_trace = result.get("planning_trace", {})
    outline = result.get("outline", {})
    outline_evidence_map = result.get("outline_evidence_map", {})
    evidence_pool = result.get("evidence_pool", {})

    # ====================================================================
    # 2. Verify Agent Planner success
    # ====================================================================
    is_agent = planning_trace.get("is_agent_planned", False)
    _print_separator("Planner Status")
    print(f"  is_agent_planned: {is_agent}")
    print(f"  strategy:         {planning_trace.get('strategy', '?')}")
    print(f"  fallback_used:    {planning_trace.get('fallback_used', True)}")
    if show_timing:
        print(f"  planner_seconds:  {planner_seconds:.2f}")

    if not is_agent:
        fb_reason = planning_trace.get("fallback_reason", "unknown")
        print()
        print(f"  [FAIL] Planner is not on Agent success path.")
        print(f"     fallback_reason: {fb_reason[:300]}")
        print(f"     Cannot generate handout section from fallback outline.")
        sys.exit(1)

    print(f"  [PASS] Planner Agent success confirmed.")

    # ====================================================================
    # 3. Find the target node
    # ====================================================================
    outline_nodes = outline.get("nodes", [])
    target_node = None
    for node in outline_nodes:
        if node.get("id") == node_id:
            target_node = node
            break

    if target_node is None:
        available = [n.get("id") for n in outline_nodes]
        print(f"\n  [FAIL] node {node_id!r} not found in outline.")
        print(f"     Available nodes: {available}")
        sys.exit(1)

    # ====================================================================
    # 4. Get node_evidence
    # ====================================================================
    node_evidence = outline_evidence_map.get(node_id, {})
    if not node_evidence:
        print(f"\n  [FAIL] no evidence map entry for {node_id}")
        sys.exit(1)

    # ====================================================================
    # 5. Determine previous / next node titles
    # ====================================================================
    node_indices = {n["id"]: i for i, n in enumerate(outline_nodes)}
    idx = node_indices.get(node_id, -1)
    prev_title = outline_nodes[idx - 1]["title"] if idx > 0 else None
    next_title = outline_nodes[idx + 1]["title"] if idx >= 0 and idx + 1 < len(outline_nodes) else None

    # ====================================================================
    # 6. Generate handout section
    # ====================================================================
    _print_separator("Generating Handout Section")
    print(f"  topic:             {topic}")
    print(f"  node_id:           {node_id}")
    print(f"  node_title:        {target_node.get('title', '?')}")
    print(f"  previous:          {prev_title or '(none)'}")
    print(f"  next:              {next_title or '(none)'}")

    # Evidence counts
    kg_nodes_count = len(node_evidence.get("kg_nodes", []))
    kg_edges_count = len(node_evidence.get("kg_edges", []))
    concepts_count = len(node_evidence.get("concepts", []))
    questions_count = len(node_evidence.get("questions", []))
    chunks_count = len(node_evidence.get("evidence_chunks", []))

    print(f"  evidence counts:")
    print(f"    kg_nodes:        {kg_nodes_count}")
    print(f"    kg_edges:        {kg_edges_count}")
    print(f"    concepts:        {concepts_count}")
    print(f"    questions:       {questions_count}")
    print(f"    evidence_chunks: {chunks_count}")

    section = generate_handout_section(
        outline_node=target_node,
        node_evidence=node_evidence,
        evidence_pool=evidence_pool,
        user_profile={"level": "unknown"},
        previous_node_title=prev_title,
        next_node_title=next_title,
    )

    # ====================================================================
    # 7. Print results
    # ====================================================================
    _print_separator("Section Result")
    section_status = section.get("section_status", "?")
    evidence_status = section.get("evidence_status", "?")
    error = section.get("error")

    print(f"  section_status:    {section_status}")
    print(f"  evidence_status:   {evidence_status}")
    if error:
        print(f"  error:             {error}")

    # Used evidence
    ue = section.get("used_evidence", {})
    print(f"  used_evidence:")
    print(f"    kg_node_ids:     {len(ue.get('kg_node_ids', []))} {ue.get('kg_node_ids', [])}")
    print(f"    kg_edge_ids:     {len(ue.get('kg_edge_ids', []))} {ue.get('kg_edge_ids', [])}")
    print(f"    concept_ids:     {len(ue.get('concept_ids', []))} {ue.get('concept_ids', [])}")
    print(f"    question_ids:    {len(ue.get('question_ids', []))} {ue.get('question_ids', [])}")
    print(f"    evidence_chunk_ids: {len(ue.get('evidence_chunk_ids', []))} {ue.get('evidence_chunk_ids', [])}")

    # Content summary
    content = section.get("content", {})
    overview = content.get("overview", "")
    key_points = content.get("key_points", [])
    print(f"  overview:          {overview[:200]}{'...' if len(overview) > 200 else ''}")
    print(f"  overview_chars:    {len(overview)}")
    print(f"  key_points count:  {len(key_points)}")
    # Per-key-point detail: heading, explanation length, evidence_refs, preview
    total_exp_chars = 0
    for i, kp in enumerate(key_points):
        heading = kp.get("heading", "?")
        explanation = kp.get("explanation", "")
        refs = kp.get("evidence_refs", [])
        exp_chars = len(explanation)
        total_exp_chars += exp_chars
        has_refs = len(refs) > 0
        exp_preview = explanation[:400] if len(explanation) > 400 else explanation
        print(f"    [{i+1}] {heading}")
        print(f"         explanation_chars: {exp_chars}")
        print(f"         has_evidence_refs: {has_refs}  refs: {refs}")
        if exp_preview:
            print(f"         explanation_preview: {exp_preview}{'...' if len(explanation) > 400 else ''}")
    # Average explanation length
    if len(key_points) > 0:
        avg_exp = total_exp_chars / len(key_points)
        print(f"  avg_explanation_chars: {avg_exp:.0f}  (total: {total_exp_chars})")
    else:
        print(f"  avg_explanation_chars: N/A (0 key_points)")
    clinical = content.get("clinical_example", "")
    if clinical:
        print(f"  clinical_example:  {clinical[:200]}{'...' if len(clinical) > 200 else ''}")
        print(f"  clinical_example_chars: {len(clinical)}")
    confusions = content.get("common_confusions", [])
    if confusions:
        print(f"  common_confusions: {len(confusions)} items")
        for ci, cc in enumerate(confusions):
            c_text = cc.get("confusion", "")
            cl_text = cc.get("clarification", "")
            print(f"    [{ci+1}] confusion: {c_text[:120]}{'...' if len(c_text) > 120 else ''}")
            print(f"         clarification: {cl_text[:200]}{'...' if len(cl_text) > 200 else ''}")
    transition = content.get("transition_to_next", "")
    if transition:
        print(f"  transition:        {transition[:200]}{'...' if len(transition) > 200 else ''}")
    missing = section.get("missing_evidence", [])
    if missing:
        print(f"  missing_evidence:  {missing}")

    # Generation trace
    trace = section.get("_trace", {})
    if trace:
        print(f"  generation_trace:")
        print(f"    targeted_search_used:         {trace.get('targeted_search_used', False)}")
        tsqs = trace.get("targeted_search_queries", [])
        print(f"    targeted_search_queries:      {len(tsqs)}")
        for i, tsq in enumerate(tsqs):
            print(f"      [{i+1}] query: {tsq.get('query', '')[:80]}")
            print(f"           purpose: {tsq.get('purpose', '')[:120]}")
        print(f"    targeted_search_result_count: {trace.get('targeted_search_result_count', 0)}")

    # ====================================================================
    # 7b. Timing (if requested)
    # ====================================================================
    if show_timing and trace:
        _print_timings(trace, planner_seconds)

    # ====================================================================
    # 8. Print full section if requested
    # ====================================================================
    if show_section:
        _print_separator("Full Section JSON")
        print(json.dumps(section, ensure_ascii=False, indent=2))

    # ====================================================================
    # 9. Assertions
    # ====================================================================
    _print_separator("Assertions")

    errors: list[str] = []

    # --- section_status ---
    if section_status not in ("llm_generated", "failed"):
        errors.append(f"section_status is {section_status!r}, expected llm_generated or failed")

    # --- evidence_status ---
    if evidence_status not in ("sufficient", "insufficient", "not_generated"):
        errors.append(f"evidence_status is {evidence_status!r}, expected sufficient/insufficient/not_generated")

    # If failed, report and stop further assertions
    if section_status == "failed":
        print(f"  [WARN] section_status=failed")
        if error:
            print(f"    error: {error}")
        print(f"\n  Note: section_status=failed is valid when LLM is unavailable.")
        print(f"  This test cannot fully verify all assertions without LLM.")
        if errors:
            for e in errors:
                print(f"  [FAIL] {e}")
            sys.exit(1)
        print("\nhandout section smoke test passed (with failed status — LLM may be unavailable)")
        return

    # If llm_generated, do full assertions
    if section_status == "llm_generated":
        # --- if sufficient, at least 2 key_points ---
        if evidence_status == "sufficient":
            if len(key_points) < 2:
                errors.append(
                    f"evidence_status=sufficient but only {len(key_points)} key_point(s); need at least 2"
                )
            for i, kp in enumerate(key_points):
                refs = kp.get("evidence_refs", [])
                if not refs:
                    errors.append(f"key_point[{i}] has no evidence_refs")

        # --- build typed allowed sets ---
        # Collect targeted search chunk ids from trace
        ts_chunk_ids: set[str] = set(trace.get("targeted_search_result_chunk_ids", []))
        allowed = _collect_allowed_evidence_sets(node_evidence, extra_chunk_ids=ts_chunk_ids)

        # --- evidence_refs must be in allowed["evidence_refs"] ---
        for i, kp in enumerate(key_points):
            for ref in kp.get("evidence_refs", []):
                if ref not in allowed["evidence_refs"]:
                    errors.append(
                        f"key_point[{i}] references {ref!r} not in allowed evidence_refs"
                    )

        for i, cc in enumerate(confusions):
            for ref in cc.get("evidence_refs", []):
                if ref not in allowed["evidence_refs"]:
                    errors.append(
                        f"common_confusions[{i}] references {ref!r} not in allowed evidence_refs"
                    )

        # --- used_evidence: type-specific checks ---
        for eid in ue.get("kg_node_ids", []):
            if eid not in allowed["kg_node_ids"]:
                errors.append(
                    f"used_evidence.kg_node_ids references {eid!r} not in allowed kg_node_ids"
                )
        for eid in ue.get("kg_edge_ids", []):
            if eid not in allowed["kg_edge_ids"]:
                errors.append(
                    f"used_evidence.kg_edge_ids references {eid!r} not in allowed kg_edge_ids"
                )
        for eid in ue.get("concept_ids", []):
            if eid not in allowed["concept_ids"]:
                errors.append(
                    f"used_evidence.concept_ids references {eid!r} not in allowed concept_ids"
                )
        for eid in ue.get("question_ids", []):
            if eid not in allowed["question_ids"]:
                errors.append(
                    f"used_evidence.question_ids references {eid!r} not in allowed question_ids"
                )
        for eid in ue.get("evidence_chunk_ids", []):
            if eid not in allowed["evidence_chunk_ids"]:
                errors.append(
                    f"used_evidence.evidence_chunk_ids references {eid!r} not in allowed evidence_chunk_ids"
                )

    # --- report ---
    if errors:
        print(f"  [FAIL] {len(errors)} assertion(s) failed:")
        for e in errors:
            print(f"     - {e}")
        sys.exit(1)
    else:
        print(f"  [PASS] All assertions passed")
        if evidence_status == "insufficient":
            print(f"  [WARN] Note: evidence_status=insufficient — this is a valid outcome")
            missing = section.get("missing_evidence", [])
            if missing:
                print(f"     missing: {missing}")

    print(f"\nhandout section smoke test passed")


if __name__ == "__main__":
    main()
