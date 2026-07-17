#!/usr/bin/env python3
"""Diagnose the two-stage LLM plan flow with JSON-mode compatibility retry.

Both the parse and orchestration stages now use ``json_mode=True`` in
``_make_default_llm_call``.  When a provider returns empty content with
``response_format={"type":"json_object"}``, the callable automatically
retries once **without** ``response_format`` (same model, same prompt).

This script exercises both stages with the real provider and reports:
- stage (parse / orchestration)
- response shape (empty / non-empty / error category)
- finish_reason (if available)
- whether a compatibility retry occurred

No API keys, full sensitive responses, or environment values are leaked.

Usage:
    cd /path/to/OralSage
    python scripts/diagnose_orchestration_llm.py
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

# Configure logging to see all diagnostic output
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)

from backend.services.learning.study_plan import (
    PlanDraftStore,
    PlanRequestParseError,
    OrchestrationValidationError,
    _default_chapter_provider,
    _build_orchestration_prompt,
    build_orchestration_context,
    _build_scope_candidates_for_orchestration,
    _make_default_llm_call,
    _extract_json,
    create_plan_draft_from_text,
    parse_plan_request_text,
)


def diagnose_orchestration():
    """Test the orchestration LLM call with real provider."""
    print("=" * 60)
    print("STEP 1: Load real KG chapters")
    print("=" * 60)
    try:
        chapters = _default_chapter_provider()
        print(f"Loaded {len(chapters)} chapters from KG")
        # Show first few for reference
        for ch in chapters[:3]:
            print(f"  - {ch.get('book_title')}: {ch.get('title')} (key={ch.get('key')})")
    except Exception as exc:
        print(f"FAILED to load chapters: {exc}")
        return

    # Pick a single chapter for the test
    test_chapter = None
    for ch in chapters:
        if ch.get("key") and ch.get("book_title") and ch.get("title"):
            test_chapter = ch
            break

    if test_chapter is None:
        print("FAILED: No valid chapter found in KG")
        return

    book_title = test_chapter["book_title"]
    chapter_title = test_chapter["title"]
    chapter_key = test_chapter["key"]
    print(f"\nUsing chapter: {book_title} / {chapter_title} (key={chapter_key})")

    # Build a test message
    test_message = f"学习《{book_title}》{chapter_title}"

    print("\n" + "=" * 60)
    print("STEP 2: Test first LLM call (parse) — json_mode=True with compatibility retry, max_tokens=600")
    print("=" * 60)
    try:
        parse_call = _make_default_llm_call(max_tokens=600, json_mode=True)
        # Build parse prompt manually to inspect it
        from backend.services.learning.study_plan import (
            _build_chapter_candidate_text,
            _build_parse_prompt,
        )
        candidates_text = _build_chapter_candidate_text(chapters)
        parse_prompt = _build_parse_prompt(test_message, candidates_text)
        print(f"Parse prompt length: {len(parse_prompt)} chars")
        print(f"Calling LLM for parse...")
        parse_raw = parse_call(parse_prompt)
        print(f"Parse response length: {len(parse_raw)} chars")
        print(f"Parse response preview: {parse_raw[:300]}...")
        parsed = _extract_json(parse_raw)
        print(f"Parsed intent: {parsed.get('intent')}")
        print(f"Parsed chapter_key: {parsed.get('chapter_key')}")
        print("FIRST LLM CALL: SUCCESS")
    except Exception as exc:
        print(f"FIRST LLM CALL FAILED: {type(exc).__name__}: {exc}")
        return

    print("\n" + "=" * 60)
    print("STEP 3: Build orchestration context and prompt")
    print("=" * 60)
    try:
        scope_candidates = _build_scope_candidates_for_orchestration(
            parsed, chapters, memory=None, user_id=None,
        )
        print(f"Scope candidates: {len(scope_candidates)}")
        for sc in scope_candidates:
            print(f"  - scope_ref={sc.get('scope_ref')}, chapter={sc.get('chapter_title')}")

        orch_context = build_orchestration_context(
            scope_candidates=scope_candidates,
            daily_minutes=parsed.get("daily_minutes", 30),
            total_days=parsed.get("days", 7),
            focus_text=parsed.get("focus_text"),
            intent=parsed.get("intent", "learn_chapter"),
        )
        orch_prompt = _build_orchestration_prompt(orch_context)
        print(f"Orchestration prompt length: {len(orch_prompt)} chars")
        print(f"Orchestration prompt preview (first 500 chars):")
        print(orch_prompt[:500])
        print("...")
        print(f"Orchestration prompt preview (last 500 chars):")
        print(orch_prompt[-500:])
    except Exception as exc:
        print(f"FAILED to build orchestration context: {type(exc).__name__}: {exc}")
        return

    print("\n" + "=" * 60)
    print("STEP 4: Test second LLM call (orchestration) — json_mode=True with compatibility retry, max_tokens=2000")
    print("=" * 60)
    try:
        orch_call = _make_default_llm_call(max_tokens=2000, json_mode=True)
        print("Calling LLM for orchestration...")
        orch_raw = orch_call(orch_prompt)
        print(f"Orchestration response type: {type(orch_raw).__name__}")
        print(f"Orchestration response length: {len(orch_raw) if isinstance(orch_raw, str) else 'N/A'}")
        if isinstance(orch_raw, str) and orch_raw.strip():
            print(f"Orchestration response preview: {orch_raw[:500]}...")
            print("SECOND LLM CALL: SUCCESS (non-empty response)")
            # Try parsing
            try:
                orch_parsed = _extract_json(orch_raw)
                print(f"Parsed orchestration JSON keys: {sorted(orch_parsed.keys())}")
                items = orch_parsed.get("items", [])
                print(f"Number of items: {len(items)}")
                print("JSON parsing: SUCCESS")
            except Exception as exc:
                print(f"JSON parsing failed: {type(exc).__name__}: {exc}")
        else:
            print("SECOND LLM CALL: FAILED — empty or non-string response!")
            print(f"  raw_output repr: {repr(orch_raw)[:200]}")
    except Exception as exc:
        print(f"SECOND LLM CALL FAILED with exception: {type(exc).__name__}: {exc}")

    print("\n" + "=" * 60)
    print("STEP 5: Test full create_plan_draft_from_text flow (both stages use json_mode=True)")
    print("=" * 60)
    store = PlanDraftStore(ttl_seconds=300)
    try:
        draft = create_plan_draft_from_text(
            store,
            user_id="diagnostic_test",
            message=test_message,
            chapters=chapters,
            llm_call=None,  # Use real provider
        )
        items = draft.get("payload", {}).get("items", [])
        clarification = draft.get("payload", {}).get("clarification_needed")
        print(f"Draft ID: {draft.get('plan_draft_id')}")
        print(f"Items: {len(items)}")
        print(f"Clarification needed: {clarification}")
        if items:
            for item in items:
                print(f"  - {item.get('task_type')}: {item.get('learning_target')} ({item.get('estimated_minutes')}min)")
        print("FULL FLOW: SUCCESS")
    except PlanRequestParseError as exc:
        print(f"FULL FLOW FAILED at parse stage: [{exc.code}] {exc}")
    except OrchestrationValidationError as exc:
        print(f"FULL FLOW FAILED at orchestration stage: [{exc.code}] {exc}")
    except Exception as exc:
        print(f"FULL FLOW FAILED: {type(exc).__name__}: {exc}")

    print("\n" + "=" * 60)
    print("STEP 6: Comparison — orchestration WITHOUT json_mode (bypasses retry)")
    print("=" * 60)
    try:
        orch_call_no_json = _make_default_llm_call(max_tokens=2000, json_mode=False)
        print("Calling LLM for orchestration (json_mode=False)...")
        orch_raw2 = orch_call_no_json(orch_prompt)
        print(f"Response type: {type(orch_raw2).__name__}")
        print(f"Response length: {len(orch_raw2) if isinstance(orch_raw2, str) else 'N/A'}")
        if isinstance(orch_raw2, str) and orch_raw2.strip():
            print(f"Response preview: {orch_raw2[:500]}...")
            try:
                orch_parsed2 = _extract_json(orch_raw2)
                print(f"Parsed JSON keys: {sorted(orch_parsed2.keys())}")
                print("JSON parsing WITHOUT json_mode: SUCCESS")
            except Exception as exc:
                print(f"JSON parsing failed: {type(exc).__name__}: {exc}")
                print(f"Raw response (first 1000 chars): {orch_raw2[:1000]}")
        else:
            print("WITHOUT json_mode: STILL empty or non-string!")
    except Exception as exc:
        print(f"WITHOUT json_mode FAILED: {type(exc).__name__}: {exc}")

    print("\n" + "=" * 60)
    print("STEP 7: Test shared compat_state across parse + orchestration")
    print("=" * 60)
    compat_state: dict = {}
    print(f"Initial compat_state: {compat_state}")

    # Build a parse callable with compat_state
    parse_call_with_state = _make_default_llm_call(
        max_tokens=600, json_mode=True, _compat_state=compat_state,
    )
    # Build an orchestration callable with the SAME compat_state
    orch_call_with_state = _make_default_llm_call(
        max_tokens=2000, json_mode=True, _compat_state=compat_state,
    )

    print("Running parse with compat_state...")
    try:
        parse_result = parse_call_with_state(parse_prompt)
        print(f"Parse result length: {len(parse_result)} chars")
        print(f"compat_state after parse: {compat_state}")
    except Exception as exc:
        print(f"Parse failed: {type(exc).__name__}: {exc}")
        print(f"compat_state after parse failure: {compat_state}")

    print("\nRunning orchestration with shared compat_state...")
    if compat_state.get("json_mode_supported") is False:
        print("SKIPPING JSON-mode probe — compat_state says unsupported")
    try:
        orch_result2 = orch_call_with_state(orch_prompt)
        print(f"Orchestration result length: {len(orch_result2)} chars")
        if isinstance(orch_result2, str) and orch_result2.strip():
            try:
                parsed2 = _extract_json(orch_result2)
                print(f"Orchestration JSON keys: {sorted(parsed2.keys())}")
                print("Orchestration with compat_state: SUCCESS")
            except Exception as exc:
                print(f"Orchestration JSON parsing failed: {type(exc).__name__}: {exc}")
        else:
            print("Orchestration with compat_state: empty response")
    except Exception as exc:
        print(f"Orchestration with compat_state FAILED: {type(exc).__name__}: {exc}")
    print(f"Final compat_state: {compat_state}")


if __name__ == "__main__":
    diagnose_orchestration()
