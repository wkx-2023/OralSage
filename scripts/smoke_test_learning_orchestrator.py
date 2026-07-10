"""Smoke test for the Learning Orchestrator — full terminal flow.

Run from the project root::

    python scripts/smoke_test_learning_orchestrator.py --topic 牙周炎 --question "为什么牙周炎会导致牙槽骨吸收？"
    python scripts/smoke_test_learning_orchestrator.py --topic 根尖周炎 --question "根尖周炎和牙髓炎有什么区别？"

Expected output ends with ``learning orchestrator smoke test passed``.
"""

import argparse
import shutil
import sys
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from backend.services.learning.memory import LearningMemory
from backend.services.learning.orchestrator import LearningOrchestrator


_DEFAULT_QUESTIONS = {
    "牙周炎": "为什么牙周炎会导致牙槽骨吸收？",
    "根尖周炎": "根尖周炎和牙髓炎有什么区别？",
}


def _first_question(step: dict) -> dict:
    questions = step.get("questions") or []
    if questions:
        return questions[0]
    question = step.get("question")
    if isinstance(question, dict):
        return question
    raise AssertionError("Tutor step should include at least one question")


def _wrong_answer_for(question: dict) -> str:
    correct = str(question.get("answer", "A")).strip().upper()
    for choice in ["A", "B", "C", "D"]:
        if choice != correct:
            return choice
    return "A"


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke-test the Learning Orchestrator.")
    parser.add_argument("--topic", default="牙周炎", help="Topic to plan (default: 牙周炎)")
    parser.add_argument(
        "--question",
        default=None,
        help="Interruption question (default: topic-appropriate)",
    )
    args = parser.parse_args()

    topic: str = args.topic
    question: str = args.question or _DEFAULT_QUESTIONS.get(
        topic, "为什么这个疾病会发生？"
    )

    # 1. Setup — clean temp DB
    smoke_dir = Path("data/learning_smoke")
    if smoke_dir.exists():
        shutil.rmtree(smoke_dir)
    smoke_dir.mkdir(parents=True, exist_ok=True)

    temp_db = smoke_dir / "orchestrator.sqlite3"
    resource_root = smoke_dir / "resources"

    memory = LearningMemory(db_path=temp_db)
    orchestrator = LearningOrchestrator(memory=memory, resource_root=resource_root)

    # 2. Start lesson
    print(f"=== start_lesson ===")
    print(f"Topic: {topic}")
    result = orchestrator.start_lesson(
        user_id="demo_orchestrator",
        topic=topic,
        display_name="Smoke Test User",
    )

    session_id: str = result["session"]["id"]
    outline = result["outline"]
    nodes = outline["nodes"]
    node_count = len(nodes)
    first_node_id = result["current_outline_node_id"]

    print(f"session_id: {session_id}")
    print(f"topic: {topic}")
    print(f"planner strategy: {result['source_trace']['strategy']}")
    print(f"outline node count: {node_count}")
    print(f"current node (after start): {first_node_id}")
    print(f"resource count: {len(result['resources'])}")
    for r in result["resources"]:
        print(f"  [{r['resource_type']}] {r['resource_path']}")

    # 3. Next step — get first tutor content
    print(f"\n=== next_step ===")
    step = orchestrator.next_step(session_id)
    print(f"  tutor content_status: {step.get('content_status')}")
    print(f"  questions count: {len(step.get('questions', []))}")
    print(f"  trace.mode: {step['trace']['mode']}")
    step_question = _first_question(step)
    correct_answer = str(step_question.get("answer", "A")).strip().upper()
    wrong_answer = _wrong_answer_for(step_question)

    # 4. Handle interruption — QA, no progress
    print(f"\n=== handle_interruption ===")
    qa_result = orchestrator.handle_interruption(session_id, question)
    before = qa_result["current_outline_node_id_before"]
    after_qa = qa_result["current_outline_node_id_after"]

    print(f"  current_outline_node_id_before: {before}")
    print(f"  current_outline_node_id_after:  {after_qa}")
    print(f"  advance_progress: {qa_result['advance_progress']}")
    print(f"  qa_status: {qa_result.get('qa_status', '?')}")
    print(f"  question_scope: {qa_result.get('question_scope', '?')}")

    assert before == after_qa, (
        f"QA must not advance progress: {before} != {after_qa}"
    )
    assert qa_result["advance_progress"] is False, "advance_progress must be False"
    print(f"  [PASS] QA did not advance progress")

    # 5. Submit wrong answer — should NOT advance
    print(f"\n=== submit_answer (wrong: {wrong_answer}) ===")
    submit_wrong = orchestrator.submit_answer(session_id, step_question, wrong_answer)
    wrong_before = submit_wrong["current_outline_node_id_before"]
    wrong_after = submit_wrong["current_outline_node_id_after"]

    print(f"  current_outline_node_id_before: {wrong_before}")
    print(f"  current_outline_node_id_after:  {wrong_after}")
    print(f"  is_correct: {submit_wrong['assessment']['is_correct']}")
    print(f"  advanced: {submit_wrong['advanced']}")
    print(f"  recommendation.action: {submit_wrong['assessment']['recommendation']['action']}")

    assert wrong_before == wrong_after, (
        f"Wrong answer must not advance: {wrong_before} != {wrong_after}"
    )
    assert submit_wrong["advanced"] is False, "advanced must be False for wrong answer"
    print(f"  [PASS] Wrong answer did not advance progress")

    # 6. Continue button — should advance and return the next Tutor step
    print(f"\n=== continue_to_next_section ===")
    continue_result = orchestrator.continue_to_next_section(session_id)
    continue_before = continue_result["current_outline_node_id_before"]
    continue_after = continue_result["current_outline_node_id_after"]
    next_tutor_step = continue_result.get("tutor_step")

    print(f"  current_outline_node_id_before: {continue_before}")
    print(f"  current_outline_node_id_after:  {continue_after}")
    print(f"  advanced: {continue_result['advanced']}")
    print(f"  lesson_completed: {continue_result['lesson_completed']}")
    if node_count > 1:
        assert continue_result["advanced"] is True, "continue should advance"
        assert continue_after != continue_before, (
            f"Continue should advance: {continue_before} == {continue_after}"
        )
        assert next_tutor_step is not None, "continue should return next Tutor step"
        assert next_tutor_step.get("current_outline_node_id") == continue_after
        print(f"  next tutor content_status: {next_tutor_step.get('content_status')}")
        print(f"  [PASS] Continue advanced and returned next Tutor step")

    # 7. Submit correct answer — SHOULD advance from the current section
    current_question = _first_question(next_tutor_step or step)
    current_correct = str(current_question.get("answer", correct_answer)).strip().upper()
    print(f"\n=== submit_answer (correct: {current_correct}) ===")
    submit_correct = orchestrator.submit_answer(session_id, current_question, current_correct)
    correct_before = submit_correct["current_outline_node_id_before"]
    correct_after = submit_correct["current_outline_node_id_after"]

    print(f"  current_outline_node_id_before: {correct_before}")
    print(f"  current_outline_node_id_after:  {correct_after}")
    print(f"  is_correct: {submit_correct['assessment']['is_correct']}")
    print(f"  advanced: {submit_correct['advanced']}")
    print(f"  lesson_completed: {submit_correct['lesson_completed']}")
    print(f"  recommendation.action: {submit_correct['assessment']['recommendation']['action']}")

    assert submit_correct["advanced"] is True, "advanced must be True for correct answer"
    if node_count > 2:
        assert correct_after != correct_before, (
            f"Correct answer should advance: {correct_before} == {correct_after}"
        )
    print(f"  [PASS] Correct answer advanced progress")

    # 7. Session summary — check events
    print(f"\n=== session_summary ===")
    summary = orchestrator.memory.session_summary(session_id)
    events = summary["events"]
    event_types = [e["event_type"] for e in events]

    print(f"  event count: {len(events)}")
    print(f"  event_type list: {event_types}")

    required_events = [
        "lesson_started",
        "tutor_step_generated",
        "user_interruption_question",
        "answer_submitted",
        "progress_updated",
    ]
    for req in required_events:
        assert req in event_types, f"Missing required event: {req}"
    print(f"  [PASS] All required event types present")

    print(f"\nlearning orchestrator smoke test passed")


if __name__ == "__main__":
    main()
