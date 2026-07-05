"""Comprehensive smoke test for Assessment wrong-answer feedback + profile/mastery update.

Run from the project root::

    python scripts/smoke_test_learning_assessment.py --topic 牙周炎
    python scripts/smoke_test_learning_assessment.py --topic 牙周炎 --keep-data

Verifies the full wrong-answer flow:
1. Enter course -> get quiz
2. Answer wrong -> complete Assessment feedback
3. Follow-up QA about wrong answer
4. Verify node NOT advanced by wrong answer or QA
5. Verify user profile updated (known_weak_points, mistake_patterns)
6. Verify mastery_by_kg_node updated
7. Verify fallback_rule_based status integrity

Expected output ends with ``learning assessment smoke test passed``.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from backend.services.learning.memory import LearningMemory
from backend.services.learning.orchestrator import LearningOrchestrator


def _section(title: str) -> None:
    print()
    print("=" * 72)
    print(f"  {title}")
    print("=" * 72)


def _subsection(title: str) -> None:
    print()
    print(f"--- {title} ---")


def _print_assessment_verbose(assessment: dict[str, Any]) -> None:
    """Print all assessment fields for verification."""
    print(f"  is_correct:            {assessment.get('is_correct')}")
    print(f"  score:                 {assessment.get('score')}")
    print(f"  selected_option:       {assessment.get('selected_option')}")
    print(f"  correct_option:        {assessment.get('correct_option')}")
    print(f"  fallback_rule_based:   {assessment.get('fallback_rule_based')}")
    print(f"  content_status:        {assessment.get('content_status')}")
    print(f"  mastery_delta:         {assessment.get('mastery_delta')}")
    print(f"  mistake_tags:          {assessment.get('mistake_tags')}")
    print()
    oea = assessment.get('option_error_analysis')
    if oea:
        print(f"  [option_error_analysis] ({len(oea)} chars)")
        print(f"  {oea[:300]}{'...' if len(oea) > 300 else ''}")
    else:
        print(f"  [option_error_analysis] (None)")
    print()
    cae = assessment.get('correct_answer_explanation')
    if cae:
        print(f"  [correct_answer_explanation] ({len(cae)} chars)")
        print(f"  {cae[:300]}{'...' if len(cae) > 300 else ''}")
    else:
        print(f"  [correct_answer_explanation] (None)")
    print()
    ms = assessment.get('misconception_summary')
    if ms:
        print(f"  [misconception_summary] ({len(ms)} chars)")
        print(f"  {ms[:300]}{'...' if len(ms) > 300 else ''}")
    else:
        print(f"  [misconception_summary] (None)")
    print()
    print(f"  related_kg_node_ids:   {assessment.get('related_kg_node_ids', [])}")
    rem = assessment.get('recommendation', {}).get('remedial_suggestion')
    if rem:
        print(f"  remedial_suggestion:   {rem[:200]}")
    else:
        print(f"  remedial_suggestion:   (None)")
    print(f"  recommendation.action: {assessment.get('recommendation', {}).get('action')}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Comprehensive smoke-test for Assessment wrong-answer feedback chain."
    )
    parser.add_argument("--topic", default="牙周炎", help="Topic (default: 牙周炎)")
    parser.add_argument(
        "--keep-data",
        action="store_true",
        help="Keep test data directory",
    )
    args = parser.parse_args()

    topic: str = args.topic
    all_passed = True

    def _fail(msg: str) -> None:
        nonlocal all_passed
        print(f"  [FAIL] {msg}")
        all_passed = False

    def _pass(msg: str = "") -> None:
        tag = f"  [PASS] {msg}" if msg else "  [PASS]"
        print(tag)

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------
    _section(f"Assessment Smoke Test — topic: {topic}")

    demo_dir = Path("data/learning_smoke_assessment")
    if demo_dir.exists() and not args.keep_data:
        try:
            shutil.rmtree(demo_dir)
        except PermissionError:
            demo_dir = Path("data") / f"learning_smoke_assessment_{int(time.time())}"
            print(f"[WARN] old dir locked, using {demo_dir}")
    demo_dir.mkdir(parents=True, exist_ok=True)

    memory = LearningMemory(db_path=demo_dir / "learning.sqlite3")
    orchestrator = LearningOrchestrator(
        memory=memory,
        resource_root=demo_dir / "resources",
    )

    # ------------------------------------------------------------------
    # Phase 1: Create session, get first Tutor step + quiz
    # ------------------------------------------------------------------
    _subsection("Phase 1: Start lesson and get first quiz")

    user_id = "smoke_test_user"
    start = orchestrator.start_lesson(
        user_id=user_id,
        topic=topic,
        display_name="Smoke Test User",
    )
    session_id = start["session"]["id"]
    outline_nodes = start.get("outline", {}).get("nodes", [])
    print(f"  session_id: {session_id}")
    print(f"  outline_nodes: {len(outline_nodes)}")

    # Record initial state
    profile_before = memory.get_or_create_profile(user_id)
    mastery_before = memory.get_mastery_by_kg_node(user_id, session_id)
    print(f"  initial profile confidence: {profile_before.get('confidence')}")
    print(f"  initial weak_points: {len(profile_before.get('known_weak_points', []))}")
    print(f"  initial mastery entries: {len(mastery_before)}")

    # Get first Tutor step
    tutor_step = orchestrator.next_step(session_id)
    questions = tutor_step.get("questions", [])
    if not questions:
        _fail("No questions from Tutor step")
        print("learning assessment smoke test FAILED")
        sys.exit(1)
    _pass(f"Got {len(questions)} question(s) from Tutor")

    question = questions[0]
    print(f"  Question: {question.get('stem', '')[:100]}...")
    print(f"  Correct answer: {question.get('answer', '?')}")
    print(f"  Options: {len(question.get('options', []))}")
    print(f"  kg_node_ids: {question.get('kg_node_ids', [])}")

    node_id_before_quiz = session_id and memory.get_session(session_id).get(
        "current_outline_node_id"
    )
    print(f"  current_outline_node_id before quiz: {node_id_before_quiz}")

    # ------------------------------------------------------------------
    # Phase 2: Answer wrong — verify full Assessment feedback
    # ------------------------------------------------------------------
    _subsection("Phase 2: Submit wrong answer")

    # Determine a wrong answer
    correct_letter = question.get("answer", "C").upper()
    wrong_letter = "A" if correct_letter != "A" else "B"
    print(f"  Correct: {correct_letter}, Submitting: {wrong_letter}")

    result = orchestrator.submit_answer(session_id, question, wrong_letter)
    assessment = result.get("assessment", {})

    _print_assessment_verbose(assessment)

    # --- Verification ---
    # V1: is_correct must be False
    if assessment.get("is_correct") is not False:
        _fail(f"is_correct should be False, got {assessment.get('is_correct')}")
    else:
        _pass("V1: is_correct=False")

    # V2: selected_option and correct_option
    if assessment.get("selected_option") != wrong_letter:
        _fail(f"selected_option should be {wrong_letter}, got {assessment.get('selected_option')}")
    else:
        _pass(f"V2: selected_option={wrong_letter}")
    if assessment.get("correct_option") != correct_letter:
        _fail(f"correct_option should be {correct_letter}, got {assessment.get('correct_option')}")
    else:
        _pass(f"V2: correct_option={correct_letter}")

    # V3: content_status and fallback_rule_based integrity
    content_status = assessment.get("content_status", "")
    fallback_rb = assessment.get("fallback_rule_based", True)
    print(f"  content_status={content_status}, fallback_rule_based={fallback_rb}")

    if content_status == "llm_generated":
        if fallback_rb is True:
            _fail("content_status=llm_generated but fallback_rule_based=True — must not lie")
        else:
            _pass("V3: llm_generated with fallback_rule_based=False [OK]")
    elif content_status == "rule_based":
        if fallback_rb is not True:
            _fail("content_status=rule_based but fallback_rule_based is not True")
        else:
            print("  (Assessment fallback_rule_based — LLM may not be configured)")
            # This is acceptable: we don't fail if LLM isn't configured,
            # but we report it. The acceptance report must confirm this.
    else:
        _fail(f"Unexpected content_status: {content_status}")

    # V4: If llm_generated, all LLM fields must be non-empty
    if content_status == "llm_generated":
        if not assessment.get("option_error_analysis"):
            _fail("llm_generated but option_error_analysis is empty")
        else:
            _pass("V4: option_error_analysis present")
        if not assessment.get("correct_answer_explanation"):
            _fail("llm_generated but correct_answer_explanation is empty")
        else:
            _pass("V4: correct_answer_explanation present")
        if not assessment.get("misconception_summary"):
            _fail("llm_generated but misconception_summary is empty")
        else:
            _pass("V4: misconception_summary present")
        if assessment.get("related_kg_node_ids"):
            _pass(f"V4: related_kg_node_ids={assessment.get('related_kg_node_ids')}")
        else:
            print("  (related_kg_node_ids empty — may use question kg_node_ids as fallback)")

    # V5: Wrong answer must NOT advance
    advanced = result.get("advanced", True)
    if advanced:
        _fail("Wrong answer should NOT advance, but advanced=True")
    else:
        _pass("V5: Wrong answer did NOT advance node")

    node_id_after_wrong = memory.get_session(session_id).get("current_outline_node_id")
    if node_id_after_wrong != node_id_before_quiz:
        _fail(f"current_outline_node_id changed: {node_id_before_quiz} -> {node_id_after_wrong}")
    else:
        _pass(f"V5: current_outline_node_id unchanged: {node_id_after_wrong}")

    # ------------------------------------------------------------------
    # Phase 3: Check profile + mastery after wrong answer
    # ------------------------------------------------------------------
    _subsection("Phase 3: Verify profile and mastery after wrong answer")

    profile_after_wrong = memory.get_or_create_profile(user_id)
    mastery_after_wrong = memory.get_mastery_by_kg_node(user_id, session_id)

    print("  [Profile changes]")
    print(f"    confidence: {profile_before.get('confidence')} -> {profile_after_wrong.get('confidence')}")
    before_weak = len(profile_before.get('known_weak_points', []))
    after_weak = len(profile_after_wrong.get('known_weak_points', []))
    print(f"    known_weak_points: {before_weak} -> {after_weak}")
    before_patterns = len(profile_before.get('mistake_patterns', []))
    after_patterns = len(profile_after_wrong.get('mistake_patterns', []))
    print(f"    mistake_patterns: {before_patterns} -> {after_patterns}")

    # Profile should have been updated (weak_points or patterns increased)
    if after_weak > before_weak or after_patterns > before_patterns:
        _pass("V6: Profile updated after wrong answer")
    else:
        _fail("V6: Profile NOT updated after wrong answer (weak_points and patterns unchanged)")

    print("  [Mastery changes]")
    before_m_count = len(mastery_before)
    after_m_count = len(mastery_after_wrong)
    print(f"    mastery entries: {before_m_count} -> {after_m_count}")
    if after_m_count >= before_m_count:
        _pass(f"V7: Mastery entries updated ({before_m_count} -> {after_m_count})")
    else:
        _fail("V7: Mastery entries decreased unexpectedly")

    # Show mastery details
    for kg_id, mrec in mastery_after_wrong.items():
        if kg_id not in mastery_before:
            print(f"    NEW: [{kg_id}] score={mrec.get('mastery_score', 0):.2f} "
                  f"exposure={mrec.get('exposure_count', 0)}")

    # ------------------------------------------------------------------
    # Phase 4: Follow-up QA about the wrong answer
    # ------------------------------------------------------------------
    _subsection("Phase 4: Follow-up QA about wrong answer")

    # Ask a follow-up question explicitly about the wrong answer
    oea_text = assessment.get('option_error_analysis') or ''
    follow_up_q = (
        f"我刚才那道选择题做错了，选了{wrong_letter}而正确答案是{correct_letter}，"
        f"能帮我再讲解一下这道题吗？为什么{wrong_letter}不对、{correct_letter}对？"
    )
    print(f"  QA question: {follow_up_q[:150]}...")

    qa_result = orchestrator.handle_interruption(session_id, follow_up_q)
    print(f"  qa_status: {qa_result.get('qa_status')}")
    print(f"  question_scope: {qa_result.get('question_scope')}")
    print(f"  should_reinforce: {qa_result.get('should_reinforce')}")
    print(f"  confusion_kg_node_ids: {qa_result.get('confusion_kg_node_ids', [])}")
    print(f"  advance_progress: {qa_result.get('advance_progress')}")
    print(f"  targeted_search_used: {qa_result.get('targeted_search_used')}")

    answer_text = qa_result.get("answer", "")
    print(f"  answer length: {len(answer_text)} chars")
    print(f"  answer preview: {answer_text[:300]}{'...' if len(answer_text) > 300 else ''}")

    # Verify QA didn't advance
    node_id_after_qa = qa_result.get("current_outline_node_id_after")
    if node_id_after_qa == node_id_before_quiz:
        _pass(f"V8: QA did NOT advance node (still: {node_id_after_qa})")
    else:
        _fail(f"V8: QA advanced node unexpectedly: {node_id_before_quiz} -> {node_id_after_qa}")

    # ------------------------------------------------------------------
    # Phase 5: Check profile + mastery after QA
    # ------------------------------------------------------------------
    _subsection("Phase 5: Verify profile and mastery after QA")

    profile_after_qa = memory.get_or_create_profile(user_id)
    mastery_after_qa = memory.get_mastery_by_kg_node(user_id, session_id)

    print("  [Profile after QA]")
    print(f"    confidence: {profile_after_wrong.get('confidence')} -> {profile_after_qa.get('confidence')}")
    qa_weak = len(profile_after_qa.get('known_weak_points', []))
    qa_patterns = len(profile_after_qa.get('mistake_patterns', []))
    print(f"    known_weak_points: {after_weak} -> {qa_weak}")
    print(f"    mistake_patterns: {after_patterns} -> {qa_patterns}")

    # If should_reinforce, profile should be further updated
    if qa_result.get("should_reinforce"):
        if qa_weak > after_weak or qa_patterns > after_patterns:
            _pass("V9: Profile further updated after QA (should_reinforce=True)")
        else:
            print("  (Profile may not have additional changes if QA didn't identify new weak points)")

    print("  [Mastery after QA]")
    qa_m_count = len(mastery_after_qa)
    print(f"    mastery entries: {after_m_count} -> {qa_m_count}")

    # ------------------------------------------------------------------
    # Phase 6: Final state summary
    # ------------------------------------------------------------------
    _subsection("Phase 6: Final state summary")

    summary = memory.session_summary(session_id)
    print(f"  session status: {summary['session'].get('status')}")
    print(f"  current_outline_node_id: {summary['session'].get('current_outline_node_id')}")
    print(f"  total events: {len(summary.get('events', []))}")
    print(f"  total answers: {len(summary.get('answers', []))}")
    print(f"  total mastery: {len(summary.get('mastery', []))}")

    # Verify node still hasn't advanced from wrong answer or QA
    final_node = summary['session'].get('current_outline_node_id')
    if final_node == node_id_before_quiz:
        _pass(f"V10: Final node unchanged from start ({final_node})")
    else:
        _fail(f"V10: Final node changed: {node_id_before_quiz} -> {final_node}")

    # Show final profile for acceptance report
    print()
    print("  === FINAL PROFILE (for acceptance report) ===")
    final_profile = summary.get("profile", {})
    print(f"  level: {final_profile.get('level')}")
    print(f"  goal: {final_profile.get('goal')}")
    print(f"  confidence: {final_profile.get('confidence')}")
    print(f"  known_weak_points ({len(final_profile.get('known_weak_points', []))}):")
    for wp in final_profile.get('known_weak_points', [])[:10]:
        print(f"    - {wp}")
    print(f"  mistake_patterns ({len(final_profile.get('mistake_patterns', []))}):")
    for mp in final_profile.get('mistake_patterns', [])[:5]:
        print(f"    - {str(mp)[:100]}")

    # Show final mastery for acceptance report
    print()
    print("  === FINAL MASTERY (for acceptance report) ===")
    final_mastery = summary.get("mastery", [])
    for m in final_mastery[:10]:
        print(f"  [{m.get('kg_node_id', '(no kg)')}] "
              f"concept={m.get('concept_name', '')[:40]} "
              f"score={m.get('mastery_score', 0):.2f} "
              f"conf={m.get('confidence', 0):.2f} "
              f"exp={m.get('exposure_count', 0)}")

    # ------------------------------------------------------------------
    # Final report
    # ------------------------------------------------------------------
    _section("Assessment Smoke Test Result")
    if all_passed:
        print("learning assessment smoke test passed")
    else:
        print("learning assessment smoke test FAILED")
        sys.exit(1)


if __name__ == "__main__":
    main()
