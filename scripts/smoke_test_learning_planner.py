"""Smoke test for the lesson planner — with optional refinement display.

Run from the project root::

    python scripts/smoke_test_learning_planner.py --topic 牙周炎 --show-refinement
    python scripts/smoke_test_learning_planner.py --topic 根尖周炎 --show-refinement
    python scripts/smoke_test_learning_planner.py --topic 儿童龋病

Expected output ends with ``learning planner smoke test passed``.
"""

import argparse
import sys
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from backend.services.learning.planner import plan_lesson
from backend.services.learning.kag_paths import resolve_kag_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke-test the lesson planner.")
    parser.add_argument("--topic", default="牙周炎", help="Topic to plan (default: 牙周炎)")
    parser.add_argument(
        "--show-refinement",
        action="store_true",
        help="Print draft vs refined outline comparison",
    )
    parser.add_argument(
        "--show-evidence",
        action="store_true",
        help="Print evidence chunk summaries",
    )
    parser.add_argument(
        "--show-llm-debug",
        action="store_true",
        help="Print LLM refinement diagnostic info (prompt chars, raw response, JSON errors, salvage)",
    )
    parser.add_argument(
        "--show-evidence-map",
        action="store_true",
        help="Print per-node evidence map summary (kg_nodes, kg_edges, concepts, questions, evidence_chunks)",
    )
    parser.add_argument(
        "--show-agent-plan",
        action="store_true",
        help="Print LLM Agent Planner output details (sequence_strategy, per-node sequence_reason)",
    )
    args = parser.parse_args()

    topic: str = args.topic
    show_refinement = args.show_refinement
    show_evidence = args.show_evidence
    show_llm_debug = args.show_llm_debug
    show_evidence_map = args.show_evidence_map
    show_agent_plan = args.show_agent_plan

    print(f"KAG 路径: {resolve_kag_dir()}")
    print(f"选择 topic: {topic}")

    result = plan_lesson(topic, user_profile={"level": "unknown"})

    trace = result["source_trace"]
    outline = result["outline"]
    mindmap = result["mindmap"]
    refinement_trace = result.get("outline_refinement_trace", {})
    planning_trace = result.get("planning_trace", {})
    evidence_pool = result.get("evidence_pool", {})

    # ====================================================================
    # Planner Summary
    # ====================================================================
    print()
    print("=" * 60)
    print("  Planner Summary")
    print("=" * 60)
    print(f"  topic:                       {topic}")
    print(f"  strategy:                    {trace['strategy']}")
    print(f"  planning_context_ok:         {trace.get('planning_context_ok')}")
    print(f"  planning_context_source:     {trace.get('planning_context_source')}")
    print(f"  evidence_chunk_count:        {trace.get('evidence_chunk_count')}")
    print(f"  dimension_count:             {trace.get('dimension_count')}")
    print(f"  matched_node_count:          {trace.get('matched_node_count')}")
    print(f"  outline_refinement:          {trace.get('outline_refinement', '(missing)')}")
    print(f"  outline_refinement_change_count: {trace.get('outline_refinement_change_count', 0)}")
    if trace.get("outline_refinement_model"):
        print(f"  refinement_model:            {trace['outline_refinement_model']}")
    if trace.get("outline_refinement_error"):
        print(f"  refinement_error:            {trace['outline_refinement_error'][:200]}")
    # Evidence pool / outline_evidence_map summary
    ep_counts = evidence_pool.get("counts", {})
    oem_node_count = trace.get("outline_evidence_node_count", 0)
    is_agent = planning_trace.get("is_agent_planned", False)
    fallback_used = planning_trace.get("fallback_used", True)
    print(f"  is_agent_planned:            {is_agent}")
    print(f"  fallback_used:               {fallback_used}")
    print(f"  evidence_pool_counts:        {ep_counts}")
    print(f"  outline_evidence_node_count: {oem_node_count}")
    if is_agent:
        seq_strategy = planning_trace.get("sequence_strategy", "")
        print(f"  sequence_strategy:           {seq_strategy[:200] if seq_strategy else '(empty)'}")
        print(f"  json_mode_used:              {planning_trace.get('json_mode_used', '?')}")
        print()
        print("  ✅ LLM Agent Planner 主路径已启用")
    else:
        fb_reason = planning_trace.get("fallback_reason", "unknown")
        print(f"  fallback_reason:             {fb_reason[:200]}")
        print()
        print("  ⚠ NOT_AGENT_PLANNED：当前使用 fallback 路径（旧固定维度），不代表 Agent Planner 已完成。")

    # ====================================================================
    # Planner LLM Debug (--show-llm-debug)
    # ====================================================================
    if show_llm_debug:
        print()
        print("=" * 60)
        print("  Planner LLM Debug")
        print("=" * 60)
        print(f"  prompt_chars:            {refinement_trace.get('outline_refinement_prompt_chars', 0)}")
        print(f"  system_prompt_chars:     {refinement_trace.get('outline_refinement_system_prompt_chars', 0)}")
        print(f"  user_prompt_chars:       {refinement_trace.get('outline_refinement_user_prompt_chars', 0)}")
        print(f"  raw_response_chars:      {refinement_trace.get('outline_refinement_raw_response_chars', 0)}")
        json_err = refinement_trace.get("outline_refinement_json_error")
        print(f"  json_error:              {json_err if json_err else '(none)'}")
        json_pos = refinement_trace.get("outline_refinement_json_error_pos")
        print(f"  json_error_pos:          {json_pos if json_pos is not None else '(none)'}")
        json_ln = refinement_trace.get("outline_refinement_json_error_lineno")
        print(f"  json_error_lineno:       {json_ln if json_ln is not None else '(none)'}")
        json_col = refinement_trace.get("outline_refinement_json_error_colno")
        print(f"  json_error_colno:        {json_col if json_col is not None else '(none)'}")
        print(f"  salvage_attempted:       {refinement_trace.get('outline_refinement_salvage_attempted', False)}")
        print(f"  salvage_succeeded:       {refinement_trace.get('outline_refinement_salvage_succeeded', False)}")

        raw_head = refinement_trace.get("outline_refinement_raw_response_head")
        print(f"\n  raw_response_head:")
        if raw_head:
            print(f"  {raw_head[:800]}")
        else:
            print(f"  (none)")

        raw_tail = refinement_trace.get("outline_refinement_raw_response_tail")
        print(f"\n  raw_response_tail:")
        if raw_tail:
            print(f"  {raw_tail[-800:]}")
        else:
            print(f"  (none)")

    # ====================================================================
    # Evidence (optional)
    # ====================================================================
    if show_evidence and trace.get("evidence_chunk_count", 0) > 0:
        print()
        print("=" * 60)
        print("  Evidence Chunks")
        print("=" * 60)
        chunks = result.get("_planning_context_chunks", [])
        if not chunks:
            print("  (chunks not exported — enable in planner if needed)")
        else:
            for i, ch in enumerate(chunks[:8]):
                print(f"  [{i+1}] {ch.get('title', '?')[:80]}")
                print(f"       source={ch.get('source_type', '?')}  score={ch.get('score', 0):.3f}")

    # ====================================================================
    # Draft Outline Before Refinement (--show-refinement)
    # ====================================================================
    if show_refinement:
        print()
        print("=" * 60)
        print("  Draft Outline Before Refinement")
        print("=" * 60)

        draft_nodes = refinement_trace.get("draft_outline_nodes", [])
        if not draft_nodes:
            print("  (draft_outline_nodes not available in trace)")
        else:
            for i, dn in enumerate(draft_nodes):
                print(f"  [{i+1}] {dn.get('id', '?')}")
                print(f"  title: {dn.get('title', '?')}")
                objectives = dn.get("learning_objectives", [])
                if objectives:
                    print(f"  objectives:")
                    for obj in objectives:
                        print(f"    - {obj}")
                print()

    # ====================================================================
    # Final Outline After Refinement
    # ====================================================================
    print()
    print("=" * 60)
    print("  Final Outline After Refinement")
    print("=" * 60)
    print(f"  Course title: {outline['title']}")
    print()

    final_nodes = outline.get("nodes", [])
    for i, node in enumerate(final_nodes):
        n_refs = len(node.get("source_refs", []))
        kg_ids = ", ".join(node.get("kg_node_ids", [])[:2])
        q_ids = node.get("question_ids", [])
        extra = f" [关联{len(q_ids)}题]" if q_ids else ""
        print(f"  [{i+1}] {node.get('id', '?')}")
        print(f"  title: {node.get('title', '?')}")
        print(f"  kg_node_ids: {kg_ids}...  refs={n_refs}{extra}")
        objectives = node.get("learning_objectives", [])
        if objectives:
            print(f"  objectives:")
            for obj in objectives:
                print(f"    - {obj}")
        print()

    # ====================================================================
    # Agent Plan Details (--show-agent-plan)
    # ====================================================================
    if show_agent_plan:
        print()
        print("=" * 60)
        print("  Agent Plan Details")
        print("=" * 60)
        is_agent = planning_trace.get("is_agent_planned", False)
        if is_agent:
            seq_strategy = planning_trace.get("sequence_strategy", "")
            print(f"  sequence_strategy: {seq_strategy if seq_strategy else '(empty)'}")
            print(f"  json_mode_used:    {planning_trace.get('json_mode_used', '?')}")
            norm_warnings = planning_trace.get("normalization_warnings", [])
            val_errors = planning_trace.get("validation_errors", [])
            if norm_warnings:
                print(f"  normalization_warnings: {norm_warnings}")
            if val_errors:
                print(f"  validation_errors ({len(val_errors)}):")
                for ve in val_errors[:10]:
                    print(f"    - {ve}")
            print()
            for node in final_nodes:
                nid = node.get("id", "?")
                seq_reason = node.get("sequence_reason", "")
                print(f"  [{nid}] {node.get('title', '?')}")
                if seq_reason:
                    print(f"  sequence_reason: {seq_reason}")
                else:
                    print(f"  sequence_reason: (empty)")
                print()
        else:
            fb_reason = planning_trace.get("fallback_reason", "unknown")
            print(f"  NOT_AGENT_PLANNED")
            print(f"  fallback_reason: {fb_reason[:300]}")
            print()

    # ====================================================================
    # Outline Evidence Map (--show-evidence-map)
    # ====================================================================
    if show_evidence_map:
        evidence_pool = result.get("evidence_pool", {})
        outline_evidence_map = result.get("outline_evidence_map", {})
        planning_trace = result.get("planning_trace", {})

        print()
        print("=" * 60)
        print("  Outline Evidence Map")
        print("=" * 60)

        for node in final_nodes:
            node_id = node.get("id", "?")
            entry = outline_evidence_map.get(node_id, {})
            if not entry:
                print(f"\n  [{node_id}] (no evidence map entry)")
                continue

            rt = entry.get("retrieval_trace", {})
            print(f"\n  [{node_id}] {entry.get('outline_node_title', '?')}")
            seq_reason = entry.get("sequence_reason", "")
            print(f"  sequence_reason: {seq_reason if seq_reason else '(none/current planner fallback)'}")

            # kg_nodes
            kg_nodes = entry.get("kg_nodes", [])
            print(f"  kg_nodes: {len(kg_nodes)}")
            for kn in kg_nodes[:5]:
                print(f"    - {kn.get('canonical_name', '?')} ({kn.get('node_id', '?')})")

            # kg_edges
            kg_edges = entry.get("kg_edges", [])
            print(f"  kg_edges: {len(kg_edges)}")
            for ke in kg_edges[:5]:
                rel = ke.get("relation", "")
                src = ke.get("source_node_id", "?")
                tgt = ke.get("target_node_id", "?")
                print(f"    - {src} --{rel}--> {tgt}")

            # concepts
            concepts = entry.get("concepts", [])
            print(f"  concepts: {len(concepts)}")
            for c in concepts[:5]:
                print(f"    - {c.get('name', '?')}")

            # questions
            questions = entry.get("questions", [])
            question_ids = entry.get("question_ids", [])
            print(f"  questions: {len(questions)}")
            for q in questions[:5]:
                qid = q.get("question_id", "?")
                stem = (q.get("stem", "") or "")[:80]
                print(f"    - {qid}: {stem}")

            # evidence_chunks
            evidence_chunks = entry.get("evidence_chunks", [])
            print(f"  evidence_chunks: {len(evidence_chunks)}")
            for ch in evidence_chunks[:5]:
                cid = ch.get("chunk_id", "?")
                ctitle = ch.get("title", "?")
                excerpt = (ch.get("text_excerpt", "") or "")[:120]
                print(f"    - {cid}  {ctitle}")
                if excerpt:
                    print(f"      excerpt: {excerpt}")

            # retrieval_trace summary
            print(f"  has_text_excerpt: {rt.get('has_text_excerpt', False)}")

    # ====================================================================
    # Refinement Changes (--show-refinement)
    # ====================================================================
    if show_refinement:
        print("=" * 60)
        print("  Refinement Changes")
        print("=" * 60)

        ref_status = trace.get("outline_refinement", "unknown")

        if ref_status == "llm_refined":
            changes = refinement_trace.get("outline_refinement_changes", [])
            if not changes:
                print("  (no refinement changes)")
            else:
                for ch in changes:
                    print(f"  {ch.get('id', '?')}:")
                    print(f"    before: {ch.get('before_title', '?')}")
                    print(f"    after:  {ch.get('after_title', '?')}")
                    reason = ch.get("refinement_reason", "")
                    if reason:
                        print(f"    reason: {reason[:120]}")
                    ev_ids = ch.get("evidence_chunk_ids", [])
                    if ev_ids:
                        print(f"    evidence_chunk_ids: {', '.join(str(e) for e in ev_ids[:3])}")
                    # Show objective changes
                    before_obj = ch.get("before_objectives", [])
                    after_obj = ch.get("after_objectives", [])
                    if before_obj != after_obj:
                        print(f"    before_objectives: {before_obj}")
                        print(f"    after_objectives:  {after_obj}")
                    print()

        elif ref_status == "skipped_no_llm":
            print("  说明：当前没有可用 Planner LLM key，本次展示的是 draft outline，不是 LLM 精修大纲。")
            print()

        elif ref_status.startswith("failed"):
            print(f"  说明：Planner LLM 精修失败（{ref_status}），本次回退为 draft outline。")
            err = trace.get("outline_refinement_error", "")
            if err:
                print(f"  错误：{err[:200]}")
            print()

        else:
            print(f"  (unknown refinement status: {ref_status})")
            print()

    # ====================================================================
    # Mindmap
    # ====================================================================
    print(f"  mindmap: {mindmap['root']['name']}  ({len(mindmap['root']['children'])} nodes)")

    # ====================================================================
    # Assertions
    # ====================================================================
    assert result["ok"] is True, "result['ok'] should be True"
    assert outline["title"], "outline title should not be empty"
    assert len(outline["nodes"]) >= 3, (
        f"expected at least 3 outline nodes, got {len(outline['nodes'])}"
    )
    assert mindmap["root"]["name"] == outline["title"], (
        "mindmap root name must match outline title"
    )
    VALID_STRATEGIES = {
        "llm_agent_planner", "retrieval_enhanced", "kag_node_match", "fallback_outline"
    }
    assert trace["strategy"] in VALID_STRATEGIES, (
        f"unknown strategy: {trace['strategy']}"
    )

    assert "evidence_chunk_count" in trace, "trace missing evidence_chunk_count"
    assert "dimension_count" in trace, "trace missing dimension_count"

    # --- Evidence pool / outline_evidence_map / planning_trace assertions ---
    outline_evidence_map = result.get("outline_evidence_map", {})
    evidence_pool = result.get("evidence_pool", {})
    planning_trace = result.get("planning_trace", {})

    assert "evidence_pool" in result, "result missing evidence_pool"
    assert "outline_evidence_map" in result, "result missing outline_evidence_map"
    assert "planning_trace" in result, "result missing planning_trace"

    # planning_trace assertions
    is_agent = planning_trace.get("is_agent_planned", False)
    if trace["strategy"] == "llm_agent_planner":
        assert is_agent is True, (
            "is_agent_planned must be True when strategy is llm_agent_planner"
        )
        # Agent Planner success must have clean validation
        assert planning_trace.get("validation_errors") == [], (
            f"Agent Planner success requires empty validation_errors, "
            f"got: {planning_trace.get('validation_errors')}"
        )
        assert planning_trace.get("fallback_used") is False, (
            "Agent Planner success requires fallback_used=False"
        )
    else:
        assert is_agent is False, (
            f"is_agent_planned must be False when strategy is {trace['strategy']}"
        )

    assert "evidence_pool_counts" in planning_trace, (
        "planning_trace missing evidence_pool_counts"
    )
    assert "outline_evidence_map_counts" in planning_trace, (
        "planning_trace missing outline_evidence_map_counts"
    )

    # outline_evidence_map assertions
    outline_nodes = outline.get("nodes", [])
    assert len(outline_evidence_map) == len(outline_nodes), (
        f"outline_evidence_map node count {len(outline_evidence_map)} "
        f"!= outline node count {len(outline_nodes)}"
    )

    total_evidence_chunks = 0
    nodes_without_evidence = 0
    nodes_without_sequence_reason = 0
    for node in outline_nodes:
        node_id = node.get("id", "")
        entry = outline_evidence_map.get(node_id, {})
        assert entry, f"outline_evidence_map missing entry for {node_id}"
        assert entry.get("outline_node_id") == node_id, (
            f"outline_evidence_map node_id mismatch for {node_id}"
        )
        assert "kg_nodes" in entry, f"{node_id} missing kg_nodes"
        assert "kg_edges" in entry, f"{node_id} missing kg_edges"
        assert "concepts" in entry, f"{node_id} missing concepts"
        assert "questions" in entry, f"{node_id} missing questions"
        assert "evidence_chunks" in entry, f"{node_id} missing evidence_chunks"
        assert "retrieval_trace" in entry, f"{node_id} missing retrieval_trace"
        total_evidence_chunks += len(entry.get("evidence_chunks", []))

        # Per-node checks for agent planner path
        if trace["strategy"] == "llm_agent_planner":
            # sequence_reason must be non-empty
            seq_reason = entry.get("sequence_reason", "")
            assert seq_reason, (
                f"Agent Planner node {node_id}: sequence_reason must be non-empty"
            )

            # Each node must have BOTH kg_nodes AND evidence_chunks
            has_kg = len(entry.get("kg_nodes", [])) > 0
            has_chunks = len(entry.get("evidence_chunks", [])) > 0
            assert has_kg, (
                f"Agent Planner node {node_id} has no kg_nodes"
            )
            assert has_chunks, (
                f"Agent Planner node {node_id} has no evidence_chunks"
            )

    assert total_evidence_chunks >= 1, (
        f"expected at least 1 total evidence chunk across all nodes, "
        f"got {total_evidence_chunks}"
    )

    VALID_REFINEMENT = {
        "llm_refined", "skipped_no_llm", "failed_invalid_json",
        "failed_exception", "failed_schema_mismatch",
        "skipped_agent_planner_active",
    }
    assert trace.get("outline_refinement") in VALID_REFINEMENT, (
        f"unexpected outline_refinement: {trace.get('outline_refinement')}"
    )

    # New refinement assertions
    assert "outline_refinement_trace" in result, (
        "result missing outline_refinement_trace"
    )
    assert "outline_refinement_change_count" in trace, (
        "source_trace missing outline_refinement_change_count"
    )

    if trace.get("outline_refinement") == "llm_refined":
        assert trace.get("outline_refinement_change_count", 0) >= 1, (
            "llm_refined should have at least 1 change"
        )
        draft_nodes = refinement_trace.get("draft_outline_nodes", [])
        refined_nodes = refinement_trace.get("refined_outline_nodes", [])
        outline_nodes = outline.get("nodes", [])
        assert len(draft_nodes) == len(outline_nodes), (
            f"draft node count {len(draft_nodes)} != outline node count {len(outline_nodes)}"
        )
        assert len(refined_nodes) == len(outline_nodes), (
            f"refined node count {len(refined_nodes)} != outline node count {len(outline_nodes)}"
        )
        for i, (dn, rn) in enumerate(zip(draft_nodes, refined_nodes)):
            assert dn["id"] == rn["id"], (
                f"node {i}: draft id {dn['id']!r} != refined id {rn['id']!r}"
            )
        # At least one title should differ
        titles_changed = sum(
            1 for dn, rn in zip(draft_nodes, refined_nodes)
            if dn.get("title") != rn.get("title")
        )
        assert titles_changed >= 1, "expected at least one node title to be refined"
        print(f"\n  [PASS] Refinement verified: {titles_changed}/{len(outline_nodes)} titles changed")

    if trace["strategy"] == "retrieval_enhanced":
        print("\n已接入现有 KAG 检索服务")
        dimension_keywords = {"定义", "病因", "机制", "诊断", "治疗"}
        titles = {node["title"] for node in outline["nodes"]}
        assert any(kw in t for t in titles for kw in dimension_keywords) or trace.get(
            "outline_refinement"
        ) == "llm_refined", (
            f"retrieval_enhanced outline should contain dimension titles or be LLM-refined, got: {titles}"
        )

    if trace.get("planning_context_warning_count", 0) > 0:
        print(f"\n⚠  retrieval warning(s): {trace['planning_context_warning_count']}")

    if topic == "牙周炎" and trace["strategy"] == "fallback_outline":
        print("\n⚠  WARNING: '牙周炎' 未命中 KAG，使用了 fallback outline")

    print("\nlearning planner smoke test passed")


if __name__ == "__main__":
    main()
