"""Planner quality audit — multi-run, per-node evidence matrix, risk report.

Run from the project root::

    python scripts/audit_planner_quality.py
    python scripts/audit_planner_quality.py --topics 牙周炎 根尖周炎 --runs 3
    python scripts/audit_planner_quality.py --topics 儿童龋病 --runs 2

Does NOT modify Planner logic — read-only diagnostics.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from backend.services.learning.planner import plan_lesson
from backend.services.learning.kag_paths import resolve_kag_dir


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _short_id(rid: str, max_len: int = 60) -> str:
    """Truncate long ids for display."""
    if len(rid) <= max_len:
        return rid
    return rid[:max_len - 3] + "..."


# ---------------------------------------------------------------------------
# quality risk checks
# ---------------------------------------------------------------------------


def check_quality_risks(
    topic: str,
    run_index: int,
    result: dict[str, Any],
) -> list[dict[str, Any]]:
    """Collect quality risk items from a single Planner run."""
    risks: list[dict[str, Any]] = []
    planning_trace = result.get("planning_trace", {})
    outline_evidence_map = result.get("outline_evidence_map", {})
    outline = result.get("outline", {})
    evidence_pool = result.get("evidence_pool", {})

    for node in outline.get("nodes", []):
        node_id = node.get("id", "")
        entry = outline_evidence_map.get(node_id, {})
        title = entry.get("outline_node_title", "")

        # --- missing_sequence_reason ---
        seq_reason = entry.get("sequence_reason", "")
        if not seq_reason:
            risks.append({
                "topic": topic, "run": run_index, "node_id": node_id,
                "node_title": title,
                "risk": "missing_sequence_reason",
                "detail": "",
            })

        # --- no_kg_node ---
        kg_nodes = entry.get("kg_nodes", [])
        if len(kg_nodes) == 0:
            risks.append({
                "topic": topic, "run": run_index, "node_id": node_id,
                "node_title": title,
                "risk": "no_kg_node",
                "detail": "0 kg_nodes bound to this node",
            })

        # --- no_edge ---
        kg_edges = entry.get("kg_edges", [])
        if len(kg_edges) == 0:
            risks.append({
                "topic": topic, "run": run_index, "node_id": node_id,
                "node_title": title,
                "risk": "no_edge",
                "detail": "0 kg_edges bound to this node",
            })

        # --- no_concept ---
        concepts = entry.get("concepts", [])
        if len(concepts) == 0:
            risks.append({
                "topic": topic, "run": run_index, "node_id": node_id,
                "node_title": title,
                "risk": "no_concept",
                "detail": "0 concepts bound to this node",
            })

        # --- no_question ---
        questions = entry.get("questions", [])
        if len(questions) == 0:
            risks.append({
                "topic": topic, "run": run_index, "node_id": node_id,
                "node_title": title,
                "risk": "no_question",
                "detail": "0 questions bound to this node",
            })

        # --- no_chunk ---
        evidence_chunks = entry.get("evidence_chunks", [])
        if len(evidence_chunks) == 0:
            risks.append({
                "topic": topic, "run": run_index, "node_id": node_id,
                "node_title": title,
                "risk": "no_chunk",
                "detail": "0 evidence_chunks bound to this node",
            })

    # --- unknown_id ---
    for verr in planning_trace.get("validation_errors", []):
        if "unknown" in str(verr).lower():
            risks.append({
                "topic": topic, "run": run_index, "node_id": "",
                "node_title": "",
                "risk": "unknown_id",
                "detail": str(verr)[:200],
            })

    # --- edge_relation_mismatch (manual-review flag) ---
    # Flag edges whose relation seems unrelated to topic keywords
    topic_keywords = set(topic)
    for node in outline.get("nodes", []):
        node_id = node.get("id", "")
        entry = outline_evidence_map.get(node_id, {})
        for edge in entry.get("kg_edges", []):
            rel = edge.get("relation", "")
            src = edge.get("source_node_id", "")
            tgt = edge.get("target_node_id", "")
            # Just flag for manual review — no automated mismatch detection
            risks.append({
                "topic": topic, "run": run_index, "node_id": node_id,
                "node_title": entry.get("outline_node_title", ""),
                "risk": "edge_relation_mismatch",
                "detail": f"[MANUAL] {_short_id(src)} --{rel}--> {_short_id(tgt)}",
            })

    # --- question_topic_mismatch (manual-review flag) ---
    for node in outline.get("nodes", []):
        node_id = node.get("id", "")
        entry = outline_evidence_map.get(node_id, {})
        for q in entry.get("questions", []):
            stem = q.get("stem", "")[:120]
            risks.append({
                "topic": topic, "run": run_index, "node_id": node_id,
                "node_title": entry.get("outline_node_title", ""),
                "risk": "question_topic_mismatch",
                "detail": f"[MANUAL] {q.get('question_id', '?')}: {stem}",
            })

    return risks


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def print_header(title: str) -> None:
    print()
    print("=" * 72)
    print(f"  {title}")
    print("=" * 72)


def print_subheader(title: str) -> None:
    print()
    print(f"--- {title} ---")


def run_audit(topics: list[str], runs_per_topic: int) -> None:
    print(f"KAG path: {resolve_kag_dir()}")
    print(f"Topics: {topics}")
    print(f"Runs per topic: {runs_per_topic}")

    all_timings: dict[str, list[float]] = {}
    all_results: dict[str, list[dict[str, Any]]] = {}
    all_risks: list[dict[str, Any]] = []

    for topic in topics:
        all_timings[topic] = []
        all_results[topic] = []

        for run_i in range(1, runs_per_topic + 1):
            print(f"\n  [{topic}] run {run_i}/{runs_per_topic} ...", end=" ", flush=True)
            t0 = time.perf_counter()
            try:
                result = plan_lesson(topic, user_profile={"level": "unknown"})
            except Exception as exc:
                print(f"ERROR: {exc}")
                all_timings[topic].append(time.perf_counter() - t0)
                continue
            elapsed = time.perf_counter() - t0
            all_timings[topic].append(elapsed)
            all_results[topic].append(result)

            pt = result.get("planning_trace", {})
            is_agent = pt.get("is_agent_planned", False)
            strategy = result.get("source_trace", {}).get("strategy", "?")
            outline = result.get("outline", {})
            n_nodes = len(outline.get("nodes", []))
            fb_reason = pt.get("fallback_reason", "")
            is_truncated = "truncated" in fb_reason.lower()
            prompt_chars = pt.get("agent_planner_prompt_chars", 0)
            raw_chars = pt.get("agent_planner_raw_response_chars", 0)

            print(f"{elapsed:.2f}s  strategy={strategy}  "
                  f"agent={'YES' if is_agent else 'NO'}  "
                  f"nodes={n_nodes}  "
                  f"val_errors={len(pt.get('validation_errors', []))}  "
                  f"prompt_chars={prompt_chars}  raw_chars={raw_chars}")
            if not is_agent:
                tag = "TRUNCATED" if is_truncated else "FALLBACK"
                print(f"    {tag}: {fb_reason[:200]}")

            risks = check_quality_risks(topic, run_i, result)
            all_risks.extend(risks)

    # ========================================================================
    # Timing summary
    # ========================================================================
    print_header("Timing Trace")
    for topic in topics:
        times = all_timings.get(topic, [])
        if not times:
            print(f"  {topic}: no successful runs")
            continue
        avg_t = sum(times) / len(times)
        min_t = min(times)
        max_t = max(times)
        print(f"  {topic}: avg={avg_t:.2f}s  min={min_t:.2f}s  max={max_t:.2f}s  "
              f"n={len(times)}")
        for i, t in enumerate(times):
            print(f"    run {i+1}: {t:.2f}s")

    # ========================================================================
    # Agent Planner success summary
    # ========================================================================
    print_header("Agent Planner Success Summary")
    for topic in topics:
        results = all_results.get(topic, [])
        for run_i, result in enumerate(results):
            pt = result.get("planning_trace", {})
            is_agent = pt.get("is_agent_planned", False)
            fallback = pt.get("fallback_used", True)
            strategy = result.get("source_trace", {}).get("strategy", "?")
            status = "✅ AGENT" if is_agent else "⚠ FALLBACK"
            fb_reason = pt.get("fallback_reason", "")
            is_truncated = "truncated" in fb_reason.lower()
            tag = "TRUNCATED" if is_truncated else ("FALLBACK" if not is_agent else "")
            print(f"  [{topic}] run {run_i+1}: {status}  strategy={strategy}  "
                  f"fallback_used={fallback}")
            if not is_agent:
                print(f"    reason: {fb_reason[:250]}")
                print(f"    prompt_chars={pt.get('agent_planner_prompt_chars', 0)}  "
                      f"raw_chars={pt.get('agent_planner_raw_response_chars', 0)}")
            if is_agent:
                cov_warns = pt.get("coverage_warnings", [])
                if cov_warns:
                    print(f"    coverage_warnings ({len(cov_warns)}):")
                    for cw in cov_warns[:8]:
                        print(f"      - {cw}")
            val_errs = pt.get("validation_errors", [])
            if val_errs:
                print(f"    validation_errors ({len(val_errs)}):")
                for ve in val_errs[:5]:
                    print(f"      - {ve}")

    # ========================================================================
    # Key Statistics
    # ========================================================================
    print_header("Key Statistics")

    total_runs = len(topics) * runs_per_topic
    agent_successes = 0
    truncated_count = 0
    validation_failed_count = 0
    other_fallback_count = 0
    agent_nodes_without_kg = 0
    agent_nodes_without_chunk = 0
    agent_nodes_without_edge = 0
    agent_nodes_without_concept = 0
    agent_nodes_without_question = 0
    fallback_nodes_without_kg = 0
    fallback_nodes_without_chunk = 0

    for topic in topics:
        for result in all_results.get(topic, []):
            pt = result.get("planning_trace", {})
            is_agent = pt.get("is_agent_planned", False)
            fb_reason = pt.get("fallback_reason", "")
            oem = result.get("outline_evidence_map", {})

            if is_agent:
                agent_successes += 1
                for entry in oem.values():
                    if len(entry.get("kg_nodes", [])) == 0:
                        agent_nodes_without_kg += 1
                    if len(entry.get("evidence_chunks", [])) == 0:
                        agent_nodes_without_chunk += 1
                    if len(entry.get("kg_edges", [])) == 0:
                        agent_nodes_without_edge += 1
                    if len(entry.get("concepts", [])) == 0:
                        agent_nodes_without_concept += 1
                    if len(entry.get("questions", [])) == 0:
                        agent_nodes_without_question += 1
            else:
                if "truncated" in fb_reason.lower():
                    truncated_count += 1
                elif "validation_failed" in fb_reason:
                    validation_failed_count += 1
                else:
                    other_fallback_count += 1
                for entry in oem.values():
                    if len(entry.get("kg_nodes", [])) == 0:
                        fallback_nodes_without_kg += 1
                    if len(entry.get("evidence_chunks", [])) == 0:
                        fallback_nodes_without_chunk += 1

    agent_rate = (agent_successes / total_runs * 100) if total_runs > 0 else 0
    print(f"  agent_success_rate:        {agent_successes}/{total_runs} ({agent_rate:.0f}%)")
    print(f"  truncated_json_count:      {truncated_count}")
    print(f"  validation_failed_count:   {validation_failed_count}")
    print(f"  other_fallback_count:      {other_fallback_count}")
    print()
    print(f"  --- Agent-success path only ---")
    print(f"  agent node_without_kg_node:    {agent_nodes_without_kg}")
    print(f"  agent node_without_chunk:      {agent_nodes_without_chunk}")
    print(f"  agent node_without_edge:       {agent_nodes_without_edge}")
    print(f"  agent node_without_concept:    {agent_nodes_without_concept}")
    print(f"  agent node_without_question:   {agent_nodes_without_question}")
    print()
    print(f"  --- Fallback path only ---")
    print(f"  fallback node_without_kg_node: {fallback_nodes_without_kg}")
    print(f"  fallback node_without_chunk:   {fallback_nodes_without_chunk}")
    print()
    if agent_nodes_without_kg > 0 or agent_nodes_without_chunk > 0:
        print("  ⚠ CRITICAL: Agent-success path has nodes without kg_node or chunk!")
    else:
        print("  ✅ All Agent-success nodes have both kg_node and chunk")

    # ========================================================================
    # Per-node evidence coverage matrix
    # ========================================================================
    for topic in topics:
        results = all_results.get(topic, [])
        if not results:
            continue

        print_header(f"Evidence Coverage Matrix — {topic}")

        # Collect all runs' node data
        for run_i, result in enumerate(results):
            outline = result.get("outline", {})
            oem = result.get("outline_evidence_map", {})
            pt = result.get("planning_trace", {})

            print_subheader(f"Run {run_i+1}  "
                            f"(agent={'YES' if pt.get('is_agent_planned') else 'NO'})")
            print(f"  {'Node':<10} {'Title':<40} {'kg_node':>8} {'edge':>6} "
                  f"{'concept':>8} {'question':>9} {'chunk':>6} {'excerpt':>8}")
            print(f"  {'-'*10} {'-'*40} {'-'*8} {'-'*6} {'-'*8} {'-'*9} {'-'*6} {'-'*8}")

            for node in outline.get("nodes", []):
                node_id = node.get("id", "")
                entry = oem.get(node_id, {})
                rt = entry.get("retrieval_trace", {})
                title = (entry.get("outline_node_title", "") or "")[:38]

                print(f"  {node_id:<10} {title:<40} "
                      f"{rt.get('kg_node_count',0):>8} "
                      f"{rt.get('kg_edge_count',0):>6} "
                      f"{rt.get('concept_count',0):>8} "
                      f"{rt.get('question_count',0):>9} "
                      f"{rt.get('evidence_chunk_count',0):>6} "
                      f"{'YES' if rt.get('has_text_excerpt') else 'NO':>8}")

    # ========================================================================
    # Per-node evidence ID details
    # ========================================================================
    for topic in topics:
        results = all_results.get(topic, [])
        if not results:
            continue

        print_header(f"Evidence ID Details — {topic}")

        for run_i, result in enumerate(results):
            outline = result.get("outline", {})
            oem = result.get("outline_evidence_map", {})

            print_subheader(f"Run {run_i+1}")

            for node in outline.get("nodes", []):
                node_id = node.get("id", "")
                entry = oem.get(node_id, {})
                print(f"\n  [{node_id}] {entry.get('outline_node_title', '?')}")

                # sequence_reason
                sr = entry.get("sequence_reason", "")
                print(f"    sequence_reason: {sr if sr else '(EMPTY)'}")

                # kg_node_ids
                kg_ids = entry.get("kg_node_ids", [])
                kg_nodes = entry.get("kg_nodes", [])
                print(f"    kg_node_ids ({len(kg_ids)}):")
                for kn in kg_nodes[:8]:
                    print(f"      {kn.get('node_id', '?')}  "
                          f"name={kn.get('canonical_name', '?')}  "
                          f"type={kn.get('node_type', '?')}")

                # kg_edges
                kg_edges = entry.get("kg_edges", [])
                print(f"    kg_edges ({len(kg_edges)}):")
                for ke in kg_edges[:8]:
                    print(f"      {_short_id(ke.get('source_node_id','?'))} "
                          f"--{ke.get('relation','?')}--> "
                          f"{_short_id(ke.get('target_node_id','?'))}")

                # concepts
                concepts = entry.get("concepts", [])
                print(f"    concepts ({len(concepts)}):")
                for c in concepts[:8]:
                    print(f"      {c.get('name', '?')}  (node={_short_id(c.get('node_id','?'))})")

                # questions
                questions = entry.get("questions", [])
                qids = entry.get("question_ids", [])
                print(f"    question_ids ({len(qids)}): {qids}")
                print(f"    questions ({len(questions)}):")
                for q in questions[:8]:
                    print(f"      {q.get('question_id','?')}  "
                          f"type={q.get('type','?')}  "
                          f"stem={(q.get('stem','') or '')[:100]}")

                # evidence_chunks
                evidence_chunks = entry.get("evidence_chunks", [])
                print(f"    evidence_chunks ({len(evidence_chunks)}):")
                for ch in evidence_chunks[:8]:
                    cid = ch.get("chunk_id", "?")
                    ctitle = ch.get("title", "?")
                    excerpt = (ch.get("text_excerpt", "") or "")[:100]
                    print(f"      {cid}  {ctitle}")
                    if excerpt:
                        print(f"        excerpt: {excerpt}")

    # ========================================================================
    # Quality risks summary
    # ========================================================================
    print_header("Quality Risks Summary")

    # Count by risk type
    risk_counts: dict[str, int] = {}
    manual_risks: dict[str, list[dict]] = {"edge_relation_mismatch": [], "question_topic_mismatch": []}

    for r in all_risks:
        risk_type = r["risk"]
        if risk_type in manual_risks:
            manual_risks[risk_type].append(r)
        else:
            risk_counts[risk_type] = risk_counts.get(risk_type, 0) + 1

    print()
    print("  Automated risks:")
    if risk_counts:
        for risk_type, count in sorted(risk_counts.items()):
            print(f"    {risk_type:<35} {count:>5}")
    else:
        print("    (none)")

    print()
    print("  Manual-review flags:")
    for risk_type, items in manual_risks.items():
        print(f"    {risk_type}: {len(items)} items (sampled below)")
        for item in items[:5]:
            print(f"      [{item['topic']} run{item['run']}] "
                  f"{item.get('node_id','')} | {item['detail'][:150]}")

    # ========================================================================
    # Risk breakdown — Agent-success path
    # ========================================================================
    print_header("Risk Breakdown — Agent-Success Path Only")

    agent_risks = [r for r in all_risks
                   if r["risk"] not in ("edge_relation_mismatch", "question_topic_mismatch")]
    agent_run_risks: dict[str, dict[int, list[dict]]] = {}
    for r in agent_risks:
        agent_run_risks.setdefault(r["topic"], {}).setdefault(r["run"], []).append(r)

    for topic in topics:
        for run_i in range(1, runs_per_topic + 1):
            results = all_results.get(topic, [])
            if run_i - 1 >= len(results):
                continue
            pt = results[run_i - 1].get("planning_trace", {})
            if not pt.get("is_agent_planned"):
                continue  # skip fallback runs
            run_risks = [
                r for r in agent_risks
                if r["topic"] == topic and r["run"] == run_i
            ]
            node_risk_map: dict[str, list[str]] = {}
            for r in run_risks:
                nid = r.get("node_id", "?")
                node_risk_map.setdefault(nid, []).append(r["risk"])

            print(f"\n  [{topic}] run {run_i}: {len(run_risks)} risk(s) "
                  f"across {len(node_risk_map)} node(s)")
            if not node_risk_map:
                print(f"    (no risks)")
                continue
            print(f"  {'Node':<10} {'Title':<36} {'Risks'}")
            print(f"  {'-'*10} {'-'*36} {'-'*30}")
            oem = results[run_i - 1].get("outline_evidence_map", {})
            for node in results[run_i - 1].get("outline", {}).get("nodes", []):
                nid = node.get("id", "")
                if nid in node_risk_map:
                    title = (oem.get(nid, {}).get("outline_node_title", "") or "")[:34]
                    risks_str = ", ".join(node_risk_map[nid])
                    print(f"  {nid:<10} {title:<36} {risks_str}")

    # ========================================================================
    # Risk breakdown — Fallback path
    # ========================================================================
    print_header("Risk Breakdown — Fallback Path Only")

    for topic in topics:
        for run_i in range(1, runs_per_topic + 1):
            results = all_results.get(topic, [])
            if run_i - 1 >= len(results):
                continue
            pt = results[run_i - 1].get("planning_trace", {})
            if pt.get("is_agent_planned"):
                continue  # skip agent runs
            run_risks = [
                r for r in agent_risks
                if r["topic"] == topic and r["run"] == run_i
            ]
            node_risk_map: dict[str, list[str]] = {}
            for r in run_risks:
                nid = r.get("node_id", "?")
                node_risk_map.setdefault(nid, []).append(r["risk"])

            print(f"\n  [{topic}] run {run_i}: {len(run_risks)} risk(s) "
                  f"across {len(node_risk_map)} node(s)")
            if not node_risk_map:
                print(f"    (no risks)")
                continue
            print(f"  {'Node':<10} {'Title':<36} {'Risks'}")
            print(f"  {'-'*10} {'-'*36} {'-'*30}")
            oem = results[run_i - 1].get("outline_evidence_map", {})
            for node in results[run_i - 1].get("outline", {}).get("nodes", []):
                nid = node.get("id", "")
                if nid in node_risk_map:
                    title = (oem.get(nid, {}).get("outline_node_title", "") or "")[:34]
                    risks_str = ", ".join(node_risk_map[nid])
                    print(f"  {nid:<10} {title:<36} {risks_str}")

    # ========================================================================
    # Final verdict
    # ========================================================================
    print_header("Audit Verdict")
    print(f"  Total runs:              {total_runs}")
    print(f"  Agent Planner wins:      {agent_successes}/{total_runs} ({agent_rate:.0f}%)")
    print(f"  Truncated JSON:          {truncated_count}")
    print(f"  Validation failed:       {validation_failed_count}")
    print(f"  Other fallback:          {other_fallback_count}")
    print(f"  ---")
    print(f"  Agent nodes w/o kg_node: {agent_nodes_without_kg}")
    print(f"  Agent nodes w/o chunk:   {agent_nodes_without_chunk}")
    print(f"  Agent nodes w/o edge:    {agent_nodes_without_edge}")
    print(f"  Agent nodes w/o concept: {agent_nodes_without_concept}")
    print(f"  Agent nodes w/o ques:    {agent_nodes_without_question}")
    print(f"  Fallback nodes w/o kg:   {fallback_nodes_without_kg}")
    print(f"  Fallback nodes w/o chunk:{fallback_nodes_without_chunk}")
    print(f"  Manual-review flags:     {sum(len(v) for v in manual_risks.values())}")
    print()
    if agent_nodes_without_kg > 0 or agent_nodes_without_chunk > 0:
        print("  ❌ FAIL: Agent-success nodes missing kg_node or chunk")
        print("     Next step: tighten prompt or increase max_tokens")
    elif agent_successes < total_runs:
        print("  ⚠ PARTIAL: Agent success rate below 100%")
        print(f"     {total_runs - agent_successes} runs fell back to dimension-based")
    else:
        print("  ✅ PASS: All runs Agent-success, all nodes have kg_node + chunk")
    print()
    print("  audit complete")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Planner quality audit — multi-run evidence matrix & risk report"
    )
    parser.add_argument(
        "--topics", nargs="+", default=["牙周炎", "根尖周炎"],
        help="Topics to audit (default: 牙周炎 根尖周炎)",
    )
    parser.add_argument(
        "--runs", type=int, default=2,
        help="Runs per topic (default: 2)",
    )
    args = parser.parse_args()

    run_audit(args.topics, args.runs)


if __name__ == "__main__":
    main()
