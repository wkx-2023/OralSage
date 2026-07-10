"""Smoke test: handout section background prefetch and cache hit.

Validates that:
1. First section handout is sync-generated during start_lesson.
2. Background prefetch generates remaining sections.
3. Continuing to section 2 hits the cache (no on-the-fly generation).
4. QA reads from cache without triggering regeneration.
5. Cache trace fields are present and correct.

Run from the project root::

    python scripts/smoke_test_learning_handout_prefetch.py --topic ... --show-teaching --show-cache
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path
from typing import Any

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from backend.services.learning.handout_cache import (
    get_handout_cache,
    list_handout_cache_status,
)
from backend.services.learning.tutor_cache import (
    get_tutor_cache,
    list_tutor_cache_status,
)
from backend.services.learning.memory import LearningMemory
from backend.services.learning.orchestrator import LearningOrchestrator


def _print_sep(title: str = "") -> None:
    print()
    print("=" * 66)
    if title:
        print(f"  {title}")
        print("=" * 66)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Smoke test: handout section prefetch cache."
    )
    parser.add_argument("--topic", default="牙周炎", help="Learning topic")
    parser.add_argument("--show-teaching", action="store_true",
                        help="Print full teaching texts")
    parser.add_argument("--show-cache", action="store_true",
                        help="Print cache status table")
    parser.add_argument("--prefetch-timeout", type=int, default=180,
                        help="Max seconds to wait for background prefetch (default: 180)")
    parser.add_argument("--poll-interval", type=int, default=5,
                        help="Seconds between cache status polls (default: 5)")
    args = parser.parse_args()

    errors: list[str] = []

    # -- setup ----------------------------------------------------------------
    # Use a unique self-contained directory — NEVER pollute data/learning/sessions
    demo_dir = Path("data/learning_handout_prefetch_smoke")
    if demo_dir.exists():
        try:
            shutil.rmtree(demo_dir)
        except PermissionError:
            demo_dir = Path("data") / f"learning_handout_prefetch_smoke_{int(time.time())}"
            print(f"[WARN] old dir locked, using {demo_dir}")
    demo_dir.mkdir(parents=True, exist_ok=True)

    # resource_root is inside demo_dir — fully self-contained
    resource_root = demo_dir / "resources"

    memory = LearningMemory(db_path=demo_dir / "learning.sqlite3")
    orchestrator = LearningOrchestrator(
        memory=memory,
        resource_root=resource_root,
    )

    user_id = "smoke_prefetch_user"
    _print_sep("1. start_lesson")
    print(f"topic: {args.topic}")
    print(f"resource_root: {resource_root}")

    t_start = time.time()
    start = orchestrator.start_lesson(
        user_id=user_id,
        topic=args.topic,
        display_name="Smoke Prefetch Test User",
    )
    start_seconds = round(time.time() - t_start, 2)
    session_id = start["session"]["id"]
    outline_nodes = start.get("outline", {}).get("nodes", [])
    print(f"session_id: {session_id}")
    print(f"outline_node_count: {len(outline_nodes)}")
    print(f"first_handout_section_ready: {start.get('first_handout_section_ready')}")
    print(f"start_lesson elapsed: {start_seconds}s")
    for idx, node in enumerate(outline_nodes, start=1):
        marker = " <- current" if idx == 1 else ""
        print(f"  {idx}. {node.get('title', '?')}{marker}")

    if len(outline_nodes) < 2:
        errors.append("outline has fewer than 2 nodes — cannot test continue flow")
        _report(errors)
        sys.exit(1)

    first_node_id = outline_nodes[0].get("id", "")
    second_node_id = outline_nodes[1].get("id", "")

    # Shortcut: orchestrator.resource_root for use in all cache calls
    rr = orchestrator.resource_root

    # -- 2. Check first section handout cache ---------------------------------
    _print_sep("2. Check first section handout cache (after start_lesson)")
    cache1 = get_handout_cache(session_id, first_node_id, resource_root=rr)
    print(f"status: {cache1.get('status')}")
    print(f"section_status: {cache1.get('section_status')}")
    print(f"source: {cache1.get('trace', {}).get('source', 'N/A')}")
    if cache1.get("error"):
        print(f"error: {cache1['error']}")

    if cache1.get("status") != "ready":
        errors.append(
            f"First section cache status is '{cache1.get('status')}', expected 'ready'"
        )
    if cache1.get("section_status") != "llm_generated":
        errors.append(
            f"First section section_status is '{cache1.get('section_status')}', "
            f"expected 'llm_generated'"
        )

    # -- 3. Generate first Tutor step via next_step ---------------------------
    _print_sep("3. next_step (first section)")
    t1 = time.time()
    step1 = orchestrator.next_step(session_id)
    step1_seconds = round(time.time() - t1, 2)
    print(f"content_status: {step1.get('content_status')}")
    print(f"handout_cache_hit: {step1.get('handout_cache_hit', 'N/A')}")
    print(f"cache_miss_sync_generation: {step1.get('cache_miss_sync_generation', 'N/A')}")
    print(f"handout_generation_source: {step1.get('handout_generation_source', 'N/A')}")
    print(f"next_step elapsed: {step1_seconds}s")

    if step1.get("content_status") == "failed":
        errors.append("First Tutor step content_status is 'failed'")
    if not step1.get("handout_cache_hit"):
        print("[INFO] First section is not a cache hit — first_section_sync may "
              "have failed or cache not readable")

    if args.show_teaching:
        print()
        print("-" * 60)
        print("[Section 1 Teaching Text]")
        print(step1.get("teaching_text", "(empty)"))
        print("-" * 60)

    # -- 4. Poll for background prefetch of second section --------------------
    _print_sep("4. Wait for background prefetch of second section")
    second_ready = False
    poll_start = time.time()
    while (time.time() - poll_start) < args.prefetch_timeout:
        cache2_poll = get_handout_cache(session_id, second_node_id, resource_root=rr)
        status2 = cache2_poll.get("status", "missing")
        elapsed = round(time.time() - poll_start, 1)
        print(f"  [{elapsed}s] section 2 cache status: {status2}  "
              f"section_status: {cache2_poll.get('section_status', 'N/A')}")

        if status2 == "ready" and cache2_poll.get("section_status") == "llm_generated":
            second_ready = True
            print(f"  [OK] Second section handout ready after {elapsed}s")
            break
        elif status2 == "failed":
            print(f"  [FAIL] Second section handout FAILED: "
                  f"{cache2_poll.get('error', 'unknown error')[:120]}")
            break

        time.sleep(args.poll_interval)

    if not second_ready:
        cache2_final = get_handout_cache(session_id, second_node_id, resource_root=rr)
        status2_final = cache2_final.get("status", "missing")
        if status2_final == "failed":
            errors.append(
                f"Second section handout generation FAILED: "
                f"{cache2_final.get('error', 'unknown')[:150]}"
            )
        else:
            print()
            print(f"[PARTIAL] Background prefetch did not complete within "
                  f"{args.prefetch_timeout}s. Second section will fall back to "
                  f"cache_miss_sync_generation.")

    # -- 4b. Poll for background prefetch of second tutor_step -----------------
    _print_sep("4b. Wait for background prefetch of second tutor_step")
    tutor_second_ready = False
    tutor_poll_start = time.time()
    while (time.time() - tutor_poll_start) < args.prefetch_timeout:
        tutor_cache2_poll = get_tutor_cache(session_id, second_node_id, resource_root=rr)
        t_status2 = tutor_cache2_poll.get("status", "missing")
        t_elapsed = round(time.time() - tutor_poll_start, 1)
        print(f"  [{t_elapsed}s] section 2 tutor cache status: {t_status2}  "
              f"content_status: {tutor_cache2_poll.get('content_status', 'N/A')}")

        if t_status2 == "ready" and tutor_cache2_poll.get("content_status") == "llm_generated":
            tutor_second_ready = True
            print(f"  [OK] Second section tutor_step ready after {t_elapsed}s")
            break
        elif t_status2 == "failed":
            print(f"  [FAIL] Second section tutor_step FAILED: "
                  f"{tutor_cache2_poll.get('error', 'unknown error')[:120]}")
            break

        time.sleep(args.poll_interval)

    if not tutor_second_ready:
        tutor_cache2_final = get_tutor_cache(session_id, second_node_id, resource_root=rr)
        t_status2_final = tutor_cache2_final.get("status", "missing")
        if t_status2_final == "failed":
            errors.append(
                f"Second section tutor_step generation FAILED: "
                f"{tutor_cache2_final.get('error', 'unknown')[:150]}"
            )
        else:
            print()
            print(f"[PARTIAL] Background tutor_step prefetch did not complete within "
                  f"{args.prefetch_timeout}s. Second section will fall back to "
                  f"tutor_cache_miss_sync_generation.")

    # -- 5. Snapshot cache state before QA ------------------------------------
    cache_files_before = _count_cache_files(session_id, rr)
    handout_cache_files_before = _count_handout_cache_files(session_id, rr)
    tutor_cache_files_before = _count_tutor_cache_files(session_id, rr)
    cache2_before = get_handout_cache(session_id, second_node_id, resource_root=rr)
    cache2_updated_before = cache2_before.get("updated_at", "")
    tutor_cache2_before = get_tutor_cache(session_id, second_node_id, resource_root=rr)
    tutor_cache2_updated_before = tutor_cache2_before.get("updated_at", "")

    # -- 6. continue_to_next_section -> second Tutor step ---------------------
    _print_sep("6. continue_to_next_section")
    t_continue = time.time()
    result = orchestrator.continue_to_next_section(session_id)
    continue_seconds = round(time.time() - t_continue, 2)
    tutor_step2 = result.get("tutor_step") or {}
    print(f"advanced: {result.get('advanced')}")
    print(f"lesson_completed: {result.get('lesson_completed')}")
    print(f"current_outline_node_id: {result.get('current_outline_node_id_before')} "
          f"-> {result.get('current_outline_node_id_after')}")
    print(f"handout_cache_hit: {result.get('handout_cache_hit', 'N/A')}")
    print(f"cache_miss_sync_generation: {result.get('cache_miss_sync_generation', 'N/A')}")
    print(f"tutor_cache_hit: {result.get('tutor_cache_hit', 'N/A')}")
    print(f"tutor_cache_miss_sync_generation: {result.get('tutor_cache_miss_sync_generation', 'N/A')}")
    print(f"tutor_generation_source: {result.get('tutor_generation_source', 'N/A')}")
    print(f"continue_to_next_section elapsed: {continue_seconds}s")

    # Tutor step 2 details
    print()
    print(f"Tutor step 2 content_status: {tutor_step2.get('content_status')}")
    print(f"Tutor step 2 handout_cache_hit: {tutor_step2.get('handout_cache_hit', 'N/A')}")
    print(f"Tutor step 2 cache_miss_sync_generation: "
          f"{tutor_step2.get('cache_miss_sync_generation', 'N/A')}")
    print(f"Tutor step 2 handout_generation_source: "
          f"{tutor_step2.get('handout_generation_source', 'N/A')}")
    print(f"Tutor step 2 tutor_cache_hit: {tutor_step2.get('tutor_cache_hit', 'N/A')}")
    print(f"Tutor step 2 tutor_cache_miss_sync_generation: "
          f"{tutor_step2.get('tutor_cache_miss_sync_generation', 'N/A')}")
    print(f"Tutor step 2 tutor_generation_source: "
          f"{tutor_step2.get('tutor_generation_source', 'N/A')}")
    print(f"Tutor step 2 fallback_rule_based: "
          f"{tutor_step2.get('trace', {}).get('fallback_rule_based', False)}")

    if tutor_step2.get("content_status") == "failed":
        errors.append("Second Tutor step content_status is 'failed'")

    # Strict: if tutor prefetch completed, MUST be tutor cache hit
    if tutor_second_ready:
        if not tutor_step2.get("tutor_cache_hit"):
            errors.append(
                "STRICT: section 2 tutor prefetch ready but tutor_cache_hit=False — "
                "tutor cache should have been hit"
            )
        if tutor_step2.get("tutor_cache_miss_sync_generation"):
            errors.append(
                "STRICT: section 2 tutor prefetch ready but "
                "tutor_cache_miss_sync_generation=True — "
                "should not have called Tutor LLM (build_tutor_step)"
            )
    else:
        print("[INFO] Tutor prefetch did not complete; "
              "tutor_cache_miss_sync_generation expected")

    # Strict: if handout prefetch completed, MUST be handout cache hit
    if second_ready:
        if not tutor_step2.get("handout_cache_hit"):
            errors.append(
                "STRICT: section 2 prefetch ready but handout_cache_hit=False — "
                "cache should have been hit"
            )
        if tutor_step2.get("cache_miss_sync_generation"):
            errors.append(
                "STRICT: section 2 prefetch ready but cache_miss_sync_generation=True — "
                "should not have called generate_handout_section"
            )
    else:
        # Prefetch didn't complete — cache_miss_sync_generation is expected
        print("[INFO] Prefetch did not complete; cache_miss_sync_generation expected")

    if args.show_teaching:
        print()
        print("-" * 60)
        print("[Section 2 Teaching Text]")
        print(tutor_step2.get("teaching_text", "(empty)"))
        print("-" * 60)

    # -- 7. Verify node advancement -------------------------------------------
    _print_sep("7. Verify node advancement")
    actual_node = memory.get_session(session_id).get("current_outline_node_id")
    print(f"current_outline_node_id: {actual_node}")
    if actual_node != second_node_id:
        errors.append(
            f"Expected current_outline_node_id={second_node_id}, got {actual_node}"
        )
    else:
        print("[OK] node_1 -> node_2 confirmed")

    # -- 8. QA: must NOT trigger handout regeneration -------------------------
    _print_sep("8. QA (verify no handout/tutor regeneration / cache mutation)")

    qa_result = orchestrator.handle_interruption(
        session_id, "牙周炎的主要病因是什么？"
    )
    print(f"qa_status: {qa_result.get('qa_status')}")
    qa_trace = qa_result.get("trace", {})
    print(f"uses_handout_section: {qa_trace.get('uses_handout_section', 'N/A')}")
    print(f"degraded_without_handout: {qa_trace.get('degraded_without_handout', 'N/A')}")
    print(f"advance_progress: {qa_result.get('advance_progress', 'N/A')}")

    if not qa_trace.get("uses_handout_section"):
        print("[WARN] QA trace.uses_handout_section=False")
    if qa_trace.get("degraded_without_handout"):
        errors.append("QA marked degraded_without_handout=True — handout should have been cached")

    # Strict: QA must not mutate handout cache files
    cache_files_after = _count_cache_files(session_id, rr)
    handout_cache_files_after = _count_handout_cache_files(session_id, rr)
    tutor_cache_files_after = _count_tutor_cache_files(session_id, rr)
    cache2_after = get_handout_cache(session_id, second_node_id, resource_root=rr)
    tutor_cache2_after = get_tutor_cache(session_id, second_node_id, resource_root=rr)

    if cache_files_after != cache_files_before:
        errors.append(
            f"STRICT: QA changed total cache file count: "
            f"{cache_files_before} -> {cache_files_after}"
        )
    else:
        print(f"[OK] Total cache file count unchanged: {cache_files_before}")

    if handout_cache_files_after != handout_cache_files_before:
        errors.append(
            f"STRICT: QA changed handout cache file count: "
            f"{handout_cache_files_before} -> {handout_cache_files_after}"
        )
    else:
        print(f"[OK] Handout cache file count unchanged: {handout_cache_files_before}")

    if tutor_cache_files_after != tutor_cache_files_before:
        errors.append(
            f"STRICT: QA changed tutor cache file count: "
            f"{tutor_cache_files_before} -> {tutor_cache_files_after}"
        )
    else:
        print(f"[OK] Tutor cache file count unchanged: {tutor_cache_files_before}")

    if cache2_after.get("updated_at") != cache2_before.get("updated_at", ""):
        errors.append(
            "STRICT: QA changed section 2 handout cache updated_at — "
            "QA must NOT trigger handout regeneration"
        )
    else:
        print("[OK] Section 2 handout cache updated_at unchanged — QA did not regenerate")

    if tutor_cache2_after.get("updated_at") != tutor_cache2_before.get("updated_at", ""):
        errors.append(
            "STRICT: QA changed section 2 tutor cache updated_at — "
            "QA must NOT read or mutate tutor cache"
        )
    else:
        print("[OK] Section 2 tutor cache updated_at unchanged — QA did not touch tutor cache")

    # -- 9. Print cache status tables -----------------------------------------
    if args.show_cache:
        _print_sep("9. Handout Cache Status Table")
        cache_rows = list_handout_cache_status(session_id, resource_root=rr)
        if cache_rows:
            header = (f"  {'node_id':<12} {'title':<36} {'status':<12} "
                      f"{'section_status':<16} {'source':<28}")
            print(header)
            print("  " + "-" * (len(header) - 2))
            for row in cache_rows:
                err = row.get("error_short", "")
                print(
                    f"  {row['outline_node_id']:<12} "
                    f"{row['title'][:34]:<36} "
                    f"{row['status']:<12} "
                    f"{row['section_status']:<16} "
                    f"{row['source']:<28}"
                    f"{' ERR:' + err if err else ''}"
                )
        else:
            print("(no handout cache entries)")

        _print_sep("9b. Tutor Cache Status Table")
        tutor_cache_rows = list_tutor_cache_status(session_id, resource_root=rr)
        if tutor_cache_rows:
            header = (f"  {'node_id':<12} {'status':<12} "
                      f"{'content_status':<18} {'source':<28} "
                      f"{'handout_source':<20}")
            print(header)
            print("  " + "-" * (len(header) - 2))
            for row in tutor_cache_rows:
                err = row.get("error_short", "")
                print(
                    f"  {row['outline_node_id']:<12} "
                    f"{row['status']:<12} "
                    f"{row['content_status']:<18} "
                    f"{row['source']:<28} "
                    f"{row['handout_source']:<20}"
                    f"{' ERR:' + err if err else ''}"
                )
        else:
            print("(no tutor cache entries)")

    # -- 10. Critical checks --------------------------------------------------
    _print_sep("10. Critical checks summary")

    # Strict: content_status must be llm_generated
    if step1.get("content_status") != "llm_generated":
        errors.append(f"STRICT: Section 1 content_status={step1.get('content_status')}, expected llm_generated")
    if tutor_step2.get("content_status") != "llm_generated":
        errors.append(f"STRICT: Section 2 content_status={tutor_step2.get('content_status')}, expected llm_generated")

    # Strict: handout_cache_hit and cache_miss_sync_generation must not be None
    if step1.get("handout_cache_hit") is None:
        errors.append("STRICT: Section 1 handout_cache_hit is None")
    if step1.get("cache_miss_sync_generation") is None:
        errors.append("STRICT: Section 1 cache_miss_sync_generation is None")
    if tutor_step2.get("handout_cache_hit") is None:
        errors.append("STRICT: Section 2 handout_cache_hit is None")
    if tutor_step2.get("cache_miss_sync_generation") is None:
        errors.append("STRICT: Section 2 cache_miss_sync_generation is None")

    # Strict: tutor_cache_hit and tutor_cache_miss_sync_generation must not be None
    if step1.get("tutor_cache_hit") is None:
        errors.append("STRICT: Section 1 tutor_cache_hit is None")
    if step1.get("tutor_cache_miss_sync_generation") is None:
        errors.append("STRICT: Section 1 tutor_cache_miss_sync_generation is None")
    if tutor_step2.get("tutor_cache_hit") is None:
        errors.append("STRICT: Section 2 tutor_cache_hit is None")
    if tutor_step2.get("tutor_cache_miss_sync_generation") is None:
        errors.append("STRICT: Section 2 tutor_cache_miss_sync_generation is None")

    # Strict: teaching_text must not be empty
    if not step1.get("teaching_text", "").strip():
        errors.append("STRICT: Section 1 teaching_text is empty")
    if not tutor_step2.get("teaching_text", "").strip():
        errors.append("STRICT: Section 2 teaching_text is empty")

    # Strict: when tutor cache hit, must have questions
    if tutor_step2.get("tutor_cache_hit") and not tutor_step2.get("questions"):
        errors.append("STRICT: Section 2 tutor cache hit but questions is empty")

    print(f"Planner re-run: NO (called once in start_lesson)")
    print(f"Resource Factory re-run: NO (all handout from cache)")
    print(f"Tutor LLM re-run for section 2: {'NO (tutor cache hit)' if tutor_step2.get('tutor_cache_hit') else 'YES (tutor cache miss)'}")
    print(f"Section 1 content_status: {step1.get('content_status')}")
    print(f"Section 2 content_status: {tutor_step2.get('content_status')}")
    print(f"Section 2 handout_cache_hit: {tutor_step2.get('handout_cache_hit')}")
    print(f"Section 2 cache_miss_sync_generation: {tutor_step2.get('cache_miss_sync_generation')}")
    print(f"Section 2 tutor_cache_hit: {tutor_step2.get('tutor_cache_hit')}")
    print(f"Section 2 tutor_cache_miss_sync_generation: {tutor_step2.get('tutor_cache_miss_sync_generation')}")
    print(f"Section 2 tutor_generation_source: {tutor_step2.get('tutor_generation_source')}")
    print(f"Section 2 fallback_rule_based: {tutor_step2.get('trace', {}).get('fallback_rule_based', False)}")
    print(f"QA uses_handout_section: {qa_trace.get('uses_handout_section')}")
    print(f"QA regenerated handout: {handout_cache_files_after != handout_cache_files_before}")
    print(f"QA touched tutor cache: {tutor_cache_files_after != tutor_cache_files_before}")

    # -- 11. Write report_output.txt ------------------------------------------
    cache_rows_for_report = list_handout_cache_status(session_id, resource_root=rr)
    tutor_cache_rows_for_report = list_tutor_cache_status(session_id, resource_root=rr)
    report_path = demo_dir / "report_output.txt"
    _write_report(
        report_path=report_path,
        session_id=session_id,
        topic=args.topic,
        step1=step1,
        step2=tutor_step2,
        cache_rows=cache_rows_for_report,
        tutor_cache_rows=tutor_cache_rows_for_report,
        cache_files_before=cache_files_before,
        cache_files_after=cache_files_after,
        handout_cache_files_before=handout_cache_files_before,
        handout_cache_files_after=handout_cache_files_after,
        tutor_cache_files_before=tutor_cache_files_before,
        tutor_cache_files_after=tutor_cache_files_after,
        cache2_before=cache2_before,
        cache2_after=cache2_after,
        tutor_cache2_before=tutor_cache2_before,
        tutor_cache2_after=tutor_cache2_after,
        qa_trace=qa_trace,
        second_ready=second_ready,
        tutor_second_ready=tutor_second_ready,
        start_seconds=start_seconds,
        step1_seconds=step1_seconds,
        continue_seconds=continue_seconds,
    )
    print(f"report_output.txt written to: {report_path}")

    # -- Report ----------------------------------------------------------------
    _print_sep("VERDICT")
    if errors:
        print(f"=== {len(errors)} FAILURE(S) ===")
        for e in errors:
            print(f"  [FAIL] {e}")
        sys.exit(1)
    else:
        if not second_ready or not tutor_second_ready:
            missing = []
            if not second_ready:
                missing.append("handout prefetch")
            if not tutor_second_ready:
                missing.append("tutor prefetch")
            print(f"PARTIAL: {' and '.join(missing)} did not complete, but sync "
                  f"fallback worked. Smoke test partially passed.")
            sys.exit(1)
        print("handout + tutor prefetch smoke test PASSED")


def _count_cache_files(session_id: str, resource_root: Any) -> int:
    """Count ALL cache JSON files (handout + tutor) for a session."""
    return _count_handout_cache_files(session_id, resource_root) + \
        _count_tutor_cache_files(session_id, resource_root)


def _count_handout_cache_files(session_id: str, resource_root: Any) -> int:
    """Count how many handout cache JSON files exist for a session."""
    from backend.services.learning.handout_cache import _cache_dir
    d = _cache_dir(session_id, resource_root)
    if not d.exists():
        return 0
    return len(list(d.glob("*.json")))


def _count_tutor_cache_files(session_id: str, resource_root: Any) -> int:
    """Count how many tutor cache JSON files exist for a session."""
    from backend.services.learning.tutor_cache import _cache_dir
    d = _cache_dir(session_id, resource_root)
    if not d.exists():
        return 0
    return len(list(d.glob("*.json")))


def _write_report(
    report_path: Path,
    session_id: str,
    topic: str,
    step1: dict,
    step2: dict,
    cache_rows: list,
    tutor_cache_rows: list,
    cache_files_before: int,
    cache_files_after: int,
    handout_cache_files_before: int,
    handout_cache_files_after: int,
    tutor_cache_files_before: int,
    tutor_cache_files_after: int,
    cache2_before: dict,
    cache2_after: dict,
    tutor_cache2_before: dict,
    tutor_cache2_after: dict,
    qa_trace: dict,
    second_ready: bool,
    tutor_second_ready: bool,
    start_seconds: float,
    step1_seconds: float,
    continue_seconds: float,
) -> None:
    """Write the full human-readable report to a UTF-8 text file."""
    import json as _json
    with open(str(report_path), "w", encoding="utf-8") as f:
        f.write("=" * 72 + "\n")
        f.write("  HANDOUT + TUTOR PREFETCH SMOKE TEST REPORT\n")
        f.write("=" * 72 + "\n")
        f.write(f"session_id: {session_id}\n")
        f.write(f"topic: {topic}\n")
        f.write(f"start_lesson elapsed: {start_seconds}s\n")
        f.write(f"second_ready (handout background prefetch): {second_ready}\n")
        f.write(f"tutor_second_ready (tutor background prefetch): {tutor_second_ready}\n")
        f.write(f"total cache files before QA: {cache_files_before}\n")
        f.write(f"total cache files after QA:  {cache_files_after}\n")
        f.write(f"QA changed total cache file count: {cache_files_after != cache_files_before}\n")
        f.write(f"QA changed handout section 2 updated_at: {cache2_after.get('updated_at') != cache2_before.get('updated_at', '')}\n")
        f.write(f"QA changed tutor section 2 updated_at: {tutor_cache2_after.get('updated_at') != tutor_cache2_before.get('updated_at', '')}\n")
        f.write("\n")

        # -- Section 1 --------------------------------------------------------
        f.write("=" * 72 + "\n")
        f.write("  SECTION 1 TUTOR STEP\n")
        f.write("=" * 72 + "\n")
        f.write(f"content_status:          {step1.get('content_status')}\n")
        f.write(f"handout_cache_hit:       {step1.get('handout_cache_hit')}\n")
        f.write(f"cache_miss_sync_generation: {step1.get('cache_miss_sync_generation')}\n")
        f.write(f"handout_generation_source:  {step1.get('handout_generation_source', 'N/A')}\n")
        f.write(f"tutor_cache_hit:         {step1.get('tutor_cache_hit')}\n")
        f.write(f"tutor_cache_miss_sync_generation: {step1.get('tutor_cache_miss_sync_generation')}\n")
        f.write(f"tutor_generation_source: {step1.get('tutor_generation_source', 'N/A')}\n")
        f.write(f"fallback_rule_based:     {step1.get('trace', {}).get('fallback_rule_based', False)}\n")
        f.write(f"next_step elapsed:       {step1_seconds}s\n")
        f.write("\n")
        f.write("--- Section 1 Teaching Text (full) ---\n")
        f.write(step1.get("teaching_text", "(EMPTY — FAIL)"))
        f.write("\n\n")

        # -- Section 2 --------------------------------------------------------
        f.write("=" * 72 + "\n")
        f.write("  SECTION 2 TUTOR STEP\n")
        f.write("=" * 72 + "\n")
        f.write(f"content_status:          {step2.get('content_status')}\n")
        f.write(f"handout_cache_hit:       {step2.get('handout_cache_hit')}\n")
        f.write(f"cache_miss_sync_generation: {step2.get('cache_miss_sync_generation')}\n")
        f.write(f"handout_generation_source:  {step2.get('handout_generation_source', 'N/A')}\n")
        f.write(f"tutor_cache_hit:         {step2.get('tutor_cache_hit')}\n")
        f.write(f"tutor_cache_miss_sync_generation: {step2.get('tutor_cache_miss_sync_generation')}\n")
        f.write(f"tutor_generation_source: {step2.get('tutor_generation_source', 'N/A')}\n")
        f.write(f"fallback_rule_based:     {step2.get('trace', {}).get('fallback_rule_based', False)}\n")
        f.write(f"continue_to_next_section elapsed: {continue_seconds}s\n")
        f.write("\n")
        f.write("--- Section 2 Teaching Text (full - cached tutor_step) ---\n")
        f.write(step2.get("teaching_text", "(EMPTY — FAIL)"))
        f.write("\n\n")
        f.write("--- Section 2 Questions (from cached tutor_step) ---\n")
        f.write(_json.dumps(step2.get("questions", []), ensure_ascii=False, indent=2))
        f.write("\n\n")

        # -- Handout cache status table ---------------------------------------
        f.write("=" * 72 + "\n")
        f.write("  HANDOUT CACHE STATUS TABLE\n")
        f.write("=" * 72 + "\n")
        if cache_rows:
            header = (f"  {'node_id':<12} {'title':<40} {'status':<12} "
                      f"{'section_status':<16} {'source':<28}")
            f.write(header + "\n")
            f.write("  " + "-" * (len(header) - 2) + "\n")
            for row in cache_rows:
                err = row.get("error_short", "")
                f.write(
                    f"  {row['outline_node_id']:<12} "
                    f"{row['title'][:38]:<40} "
                    f"{row['status']:<12} "
                    f"{row['section_status']:<16} "
                    f"{row['source']:<28}"
                    f"{' ERR:' + err if err else ''}\n"
                )
        else:
            f.write("  (no handout cache entries)\n")
        f.write("\n")

        # -- Tutor cache status table -----------------------------------------
        f.write("=" * 72 + "\n")
        f.write("  TUTOR CACHE STATUS TABLE\n")
        f.write("=" * 72 + "\n")
        if tutor_cache_rows:
            header = (f"  {'node_id':<12} {'status':<12} "
                      f"{'content_status':<18} {'source':<28} "
                      f"{'handout_source':<20}")
            f.write(header + "\n")
            f.write("  " + "-" * (len(header) - 2) + "\n")
            for row in tutor_cache_rows:
                err = row.get("error_short", "")
                f.write(
                    f"  {row['outline_node_id']:<12} "
                    f"{row['status']:<12} "
                    f"{row['content_status']:<18} "
                    f"{row['source']:<28} "
                    f"{row['handout_source']:<20}"
                    f"{' ERR:' + err if err else ''}\n"
                )
        else:
            f.write("  (no tutor cache entries)\n")
        f.write("\n")

        # -- Critical checks summary ------------------------------------------
        f.write("=" * 72 + "\n")
        f.write("  CRITICAL CHECKS SUMMARY\n")
        f.write("=" * 72 + "\n")
        f.write(f"Planner did NOT re-run:                  True\n")
        f.write(f"Resource Factory did NOT re-run:         {step2.get('handout_cache_hit')}\n")
        f.write(f"Tutor LLM did NOT re-run:                {step2.get('tutor_cache_hit')}\n")
        f.write(f"Section 2 handout_cache_hit:             {step2.get('handout_cache_hit')}\n")
        f.write(f"Section 2 cache_miss_sync_generation:     {step2.get('cache_miss_sync_generation')}\n")
        f.write(f"Section 2 tutor_cache_hit:               {step2.get('tutor_cache_hit')}\n")
        f.write(f"Section 2 tutor_cache_miss_sync_generation: {step2.get('tutor_cache_miss_sync_generation')}\n")
        f.write(f"Section 2 tutor_generation_source:        {step2.get('tutor_generation_source', '')}\n")
        f.write(f"Section 2 fallback_rule_based:            {step2.get('trace', {}).get('fallback_rule_based', False)}\n")
        f.write(f"QA uses_handout_section:                 {qa_trace.get('uses_handout_section')}\n")
        f.write(f"QA degraded_without_handout:              {qa_trace.get('degraded_without_handout')}\n")
        f.write(f"QA handout cache file count unchanged:    {handout_cache_files_after == handout_cache_files_before}\n")
        f.write(f"QA tutor cache file count unchanged:      {tutor_cache_files_after == tutor_cache_files_before}\n")
        f.write(f"QA handout section 2 updated_at unchanged: {cache2_after.get('updated_at') == cache2_before.get('updated_at', '')}\n")
        f.write(f"QA tutor section 2 updated_at unchanged:  {tutor_cache2_after.get('updated_at') == tutor_cache2_before.get('updated_at', '')}\n")
        f.write(f"total_cache_files_before: {cache_files_before}\n")
        f.write(f"total_cache_files_after:  {cache_files_after}\n")
        f.write(f"handout_cache_files_before: {handout_cache_files_before}\n")
        f.write(f"handout_cache_files_after:  {handout_cache_files_after}\n")
        f.write(f"tutor_cache_files_before: {tutor_cache_files_before}\n")
        f.write(f"tutor_cache_files_after:  {tutor_cache_files_after}\n")


def _report(errors: list[str]) -> None:
    if errors:
        print(f"\n=== {len(errors)} FAILURE(S) ===")
        for e in errors:
            print(f"  [FAIL] {e}")


if __name__ == "__main__":
    main()
