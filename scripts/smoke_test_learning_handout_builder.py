"""Smoke test for the multi-section Handout Builder.

Run from the project root::

    python scripts/smoke_test_learning_handout_builder.py --topic 牙周炎 --max-sections 2 --max-workers 2 --show-summary
    python scripts/smoke_test_learning_handout_builder.py --topic 根尖周炎 --max-sections 2 --max-workers 2 --show-summary

Expected output ends with ``handout builder smoke test passed``.
"""

import argparse
import json
import sys
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from backend.services.learning.resource_factory import generate_handout


def _print_separator(title: str) -> None:
    print()
    print("=" * 60)
    print(f"  {title}")
    print("=" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Smoke-test the multi-section Handout Builder."
    )
    parser.add_argument(
        "--topic", default="牙周炎", help="Topic to plan (default: 牙周炎)"
    )
    parser.add_argument(
        "--max-sections", type=int, default=2,
        help="Max sections to generate (default: 2)"
    )
    parser.add_argument(
        "--max-workers", type=int, default=2,
        help="Max concurrent workers (default: 2)"
    )
    parser.add_argument(
        "--show-summary", action="store_true",
        help="Print generation_summary"
    )
    parser.add_argument(
        "--show-sections", action="store_true",
        help="Print per-section titles and status"
    )
    parser.add_argument(
        "--show-source-index", action="store_true",
        help="Print source_index id counts"
    )
    parser.add_argument(
        "--show-timing", action="store_true",
        help="Print timing breakdown"
    )
    args = parser.parse_args()

    topic = args.topic
    max_sections = args.max_sections
    max_workers = args.max_workers

    # ====================================================================
    # 1. Generate handout
    # ====================================================================
    _print_separator("Handout Builder")
    print(f"  topic:        {topic}")
    print(f"  max_sections: {max_sections}")
    print(f"  max_workers:  {max_workers}")
    print()

    handout = generate_handout(
        topic=topic,
        user_profile={"level": "unknown"},
        max_workers=max_workers,
        max_sections=max_sections,
    )

    handout_status = handout.get("handout_status", "?")
    outline = handout.get("outline") or {}
    sections = handout.get("sections", [])
    source_index = handout.get("source_index", {})
    generation_summary = handout.get("generation_summary", {})
    warnings = handout.get("warnings", [])

    # ====================================================================
    # 2. Status
    # ====================================================================
    _print_separator("Status")
    print(f"  handout_status:       {handout_status}")
    print(f"  planner_status:       {generation_summary.get('planner_status', '?')}")
    print(f"  is_agent_planned:     {generation_summary.get('is_agent_planned', False)}")
    if warnings:
        print(f"  warnings:             {len(warnings)}")
        for w in warnings:
            print(f"    - {w[:200]}")

    # ====================================================================
    # 3. Sections
    # ====================================================================
    _print_separator("Sections")
    print(f"  section_count:  {len(sections)}")
    for i, sec in enumerate(sections):
        nid = sec.get("outline_node_id", "?")
        title = sec.get("title", "?")
        s_status = sec.get("section_status", "?")
        e_status = sec.get("evidence_status", "?")
        kp_count = len(sec.get("content", {}).get("key_points", []))
        err = sec.get("error", "")
        flag = " [FAILED]" if s_status == "failed" else ""
        print(f"  [{i+1}] {nid}  {title}")
        print(f"       section={s_status}  evidence={e_status}  key_points={kp_count}{flag}")
        if err:
            print(f"       error: {err[:150]}")

    # ====================================================================
    # 4. Show sections if requested
    # ====================================================================
    if args.show_sections:
        for i, sec in enumerate(sections):
            content = sec.get("content", {})
            kps = content.get("key_points", [])
            overview = content.get("overview", "")
            clinical = content.get("clinical_example", "")
            confusions = content.get("common_confusions", [])
            transition = content.get("transition_to_next", "")
            _print_separator(f"Section {i+1}: {sec.get('title', '?')}")
            print(f"  section_status:  {sec.get('section_status', '?')}")
            print(f"  evidence_status: {sec.get('evidence_status', '?')}")
            print(f"  overview ({len(overview)} chars): {overview[:300]}{'...' if len(overview) > 300 else ''}")
            print(f"  key_points count: {len(kps)}")
            total_exp_chars = 0
            for j, kp in enumerate(kps):
                heading = kp.get("heading", "?")
                explanation = kp.get("explanation", "")
                refs = kp.get("evidence_refs", [])
                exp_chars = len(explanation)
                total_exp_chars += exp_chars
                exp_preview = explanation[:300] if len(explanation) > 300 else explanation
                print(f"  key_point[{j}]: {heading}")
                print(f"    explanation_chars: {exp_chars}  has_evidence_refs: {len(refs) > 0}  refs: {refs}")
                if exp_preview:
                    print(f"    preview: {exp_preview}{'...' if len(explanation) > 300 else ''}")
            if len(kps) > 0:
                avg_exp = total_exp_chars / len(kps)
                print(f"  avg_explanation_chars: {avg_exp:.0f}  (total: {total_exp_chars})")
            else:
                print(f"  avg_explanation_chars: N/A (0 key_points)")
            if clinical:
                print(f"  clinical_example ({len(clinical)} chars): {clinical[:200]}{'...' if len(clinical) > 200 else ''}")
            if confusions:
                print(f"  common_confusions: {len(confusions)} items")
            if transition:
                print(f"  transition_to_next: {transition[:200]}{'...' if len(transition) > 200 else ''}")
            missing = sec.get("missing_evidence", [])
            if missing:
                print(f"  missing_evidence ({len(missing)}):")
                for me in missing[:5]:
                    print(f"    - {me[:150]}")

    # ====================================================================
    # 5. Source index
    # ====================================================================
    if args.show_source_index:
        _print_separator("Source Index")
        for key in ("evidence_chunk_ids", "kg_node_ids", "kg_edge_ids", "concept_ids", "question_ids"):
            ids = source_index.get(key, [])
            print(f"  {key}: {len(ids)}")
            if args.show_sections:
                for eid in ids[:5]:
                    print(f"    - {eid}")

    # ====================================================================
    # 6. Generation summary
    # ====================================================================
    if args.show_summary:
        _print_separator("Generation Summary")
        gs = generation_summary
        for key in (
            "section_count", "llm_generated_count", "failed_count",
            "sufficient_count", "insufficient_count",
            "targeted_search_used_count",
        ):
            print(f"  {key}: {gs.get(key, '?')}")

    # ====================================================================
    # 6b. Timing
    # ====================================================================
    if args.show_timing:
        gs = generation_summary
        print()
        print(f"  Timing:")
        print(f"    planner_seconds:        {gs.get('planner_seconds', '?'):.2f}" if isinstance(gs.get('planner_seconds'), (int, float)) else f"    planner_seconds:        {gs.get('planner_seconds', '?')}")
        print(f"    sections_wall_seconds:  {gs.get('sections_wall_seconds', '?'):.2f}" if isinstance(gs.get('sections_wall_seconds'), (int, float)) else f"    sections_wall_seconds:  {gs.get('sections_wall_seconds', '?')}")
        print(f"    total_seconds:          {gs.get('total_seconds', '?'):.2f}" if isinstance(gs.get('total_seconds'), (int, float)) else f"    total_seconds:          {gs.get('total_seconds', '?')}")
        print(f"    max_workers:            {gs.get('max_workers', '?')}")
        # Per-section timing from traces
        for i, sec in enumerate(sections):
            trace = sec.get("_trace", {})
            timings = trace.get("timings", {})
            if timings:
                ts = timings.get('total_section_seconds', '?')
                if isinstance(ts, (int, float)):
                    print(f"    sec[{i}] {sec.get('outline_node_id', '?')}: {ts:.2f}s")
                else:
                    print(f"    sec[{i}] {sec.get('outline_node_id', '?')}: {ts}s")

    # ====================================================================
    # 7. Assertions
    # ====================================================================
    _print_separator("Assertions")
    errors: list[str] = []

    # Planner must be Agent success path
    if not generation_summary.get("is_agent_planned"):
        errors.append("Planner is not on Agent success path")

    # handout_status="failed" is valid when Planner failed OR all sections failed
    if handout_status == "failed":
        if not generation_summary.get("is_agent_planned"):
            pass  # Planner failed — expected
        elif generation_summary.get("section_count", 0) > 0 and generation_summary.get("failed_count") == generation_summary.get("section_count"):
            pass  # All sections failed — valid per spec
        else:
            errors.append("handout_status is 'failed' unexpectedly")

    # sections count
    if len(sections) == 0 and handout_status != "failed":
        errors.append("sections is empty")

    # section order matches outline
    if outline:
        outline_node_ids = [n["id"] for n in outline.get("nodes", [])[:max_sections]]
        section_node_ids = [s.get("outline_node_id") for s in sections]
        if outline_node_ids != section_node_ids:
            errors.append(
                f"section order mismatch: expected {outline_node_ids}, got {section_node_ids}"
            )

    # each section has required fields
    for i, sec in enumerate(sections):
        nid = sec.get("outline_node_id", "?")
        for field in ("outline_node_id", "title", "section_status", "evidence_status", "content", "used_evidence"):
            if field not in sec:
                errors.append(f"section[{i}] ({nid}) missing field: {field}")

    # source_index contains chunk ids from used_evidence
    all_ue_chunk_ids: set[str] = set()
    for sec in sections:
        ue = sec.get("used_evidence", {})
        for cid in (ue.get("evidence_chunk_ids") or []):
            all_ue_chunk_ids.add(cid)
    si_chunk_ids = set(source_index.get("evidence_chunk_ids", []))
    missing_from_si = all_ue_chunk_ids - si_chunk_ids
    if missing_from_si:
        errors.append(
            f"source_index missing {len(missing_from_si)} chunk ids from used_evidence: "
            f"{list(missing_from_si)[:5]}"
        )

    # --- report ---
    if errors:
        print(f"  [FAIL] {len(errors)} assertion(s) failed:")
        for e in errors:
            print(f"     - {e}")
        sys.exit(1)
    else:
        print(f"  [PASS] All assertions passed")
        if warnings:
            print(f"  [WARN] {len(warnings)} warning(s) — see above")

    print(f"\nhandout builder smoke test passed")


if __name__ == "__main__":
    main()
