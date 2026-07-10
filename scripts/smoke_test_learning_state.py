"""Smoke test for learning state persistence (answer_records + mastery_records).

Run from the project root::

    python scripts/smoke_test_learning_state.py --topic 牙周炎
    python scripts/smoke_test_learning_state.py --topic 根尖周炎

Expected output ends with ``learning state smoke test passed``.
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke-test learning state persistence.")
    parser.add_argument("--topic", default="牙周炎", help="Topic to plan (default: 牙周炎)")
    args = parser.parse_args()

    topic: str = args.topic

    # 1. Setup — clean temp DB
    smoke_dir = Path("data/learning_smoke")
    if smoke_dir.exists():
        shutil.rmtree(smoke_dir)
    smoke_dir.mkdir(parents=True, exist_ok=True)

    temp_db = smoke_dir / "state.sqlite3"
    resource_root = smoke_dir / "resources"

    memory = LearningMemory(db_path=temp_db)
    orchestrator = LearningOrchestrator(memory=memory, resource_root=resource_root)

    # 2. Start lesson
    result = orchestrator.start_lesson(
        user_id="demo_state",
        topic=topic,
        display_name="State Test User",
    )
    session_id: str = result["session"]["id"]
    print(f"Topic: {topic}")
    print(f"session_id: {session_id}")

    # 3. Get first tutor step
    step = orchestrator.next_step(session_id)
    question = step["question"]
    print(f"Current node: {step['current_outline_node_id']}")

    # 4. Submit wrong answer
    print(f"\n=== submit_answer (wrong: A) ===")
    wrong_result = orchestrator.submit_answer(session_id, question, "A")
    wrong_assessment = wrong_result["assessment"]
    print(f"  is_correct: {wrong_assessment['is_correct']}")
    print(f"  mastery_delta: {wrong_assessment['mastery_delta']}")
    print(f"  mastery_updates count: {len(wrong_result['mastery_updates'])}")

    # 5. Check summary after wrong answer
    summary_1 = orchestrator.memory.session_summary(session_id)
    answers_1 = summary_1["answers"]
    mastery_1 = summary_1["mastery"]

    print(f"\n--- after wrong answer ---")
    print(f"  answers count: {len(answers_1)}")
    print(f"  mastery count: {len(mastery_1)}")
    for m in mastery_1:
        print(f"  mastery: concept={m['concept_name']!r}  "
              f"kg_node_id={m.get('kg_node_id')!r}  "
              f"score={m['mastery_score']:.2f}  "
              f"confidence={m['confidence']:.2f}")

    assert len(answers_1) == 1, f"Expected 1 answer record, got {len(answers_1)}"
    assert len(mastery_1) >= 1, f"Expected >= 1 mastery record, got {len(mastery_1)}"

    # Capture pre-update mastery scores for comparison
    mastery_scores_before = {
        m.get("id") or m.get("concept_name"): m["mastery_score"]
        for m in mastery_1
    }
    print(f"  [PASS] After wrong answer: 1 answer, {len(mastery_1)} mastery record(s)")

    # 6. Submit correct answer
    print(f"\n=== submit_answer (correct: B) ===")
    correct_result = orchestrator.submit_answer(session_id, question, "B")
    correct_assessment = correct_result["assessment"]
    print(f"  is_correct: {correct_assessment['is_correct']}")
    print(f"  mastery_delta: {correct_assessment['mastery_delta']}")
    print(f"  mastery_updates count: {len(correct_result['mastery_updates'])}")

    # 7. Check summary after correct answer
    summary_2 = orchestrator.memory.session_summary(session_id)
    answers_2 = summary_2["answers"]
    mastery_2 = summary_2["mastery"]

    print(f"\n--- after correct answer ---")
    print(f"  answers count: {len(answers_2)}")
    print(f"  mastery count: {len(mastery_2)}")
    for m in mastery_2:
        prev = mastery_scores_before.get(m.get("id") or m.get("concept_name"), "N/A")
        print(f"  mastery: concept={m['concept_name']!r}  "
              f"kg_node_id={m.get('kg_node_id')!r}  "
              f"score={m['mastery_score']:.2f} (was {prev})  "
              f"confidence={m['confidence']:.2f}")

    assert len(answers_2) == 2, f"Expected 2 answer records, got {len(answers_2)}"
    assert len(mastery_2) >= 1, f"Expected >= 1 mastery record, got {len(mastery_2)}"
    print(f"  [PASS] After correct answer: 2 answers, {len(mastery_2)} mastery record(s)")

    # 8. Print individual answer records
    print(f"\n=== answer records detail ===")
    for i, a in enumerate(answers_2):
        print(f"  [{i + 1}] question_id: {a['question_id']}")
        print(f"       outline_node_id: {a['outline_node_id']}")
        print(f"       user_answer: {a['user_answer']!r}")
        print(f"       is_correct: {bool(a['is_correct'])}")
        print(f"       score: {a['score']}")
        grading = a.get("grading", {})
        if grading:
            print(f"       grading.mastery_delta: {grading.get('mastery_delta')}")
            print(f"       grading.mistake_tags: {grading.get('mistake_tags')}")

    # 9. Print mastery records detail
    print(f"\n=== mastery records detail ===")
    for i, m in enumerate(mastery_2):
        print(f"  [{i + 1}] concept_name: {m['concept_name']}")
        print(f"       kg_node_id: {m.get('kg_node_id')}")
        print(f"       mastery_score: {m['mastery_score']:.4f}")
        print(f"       confidence: {m['confidence']:.4f}")
        evidence = m.get("evidence", {})
        if evidence:
            print(f"       evidence.question_id: {evidence.get('question_id')}")
            print(f"       evidence.is_correct: {evidence.get('is_correct')}")

    # 10. Verify mastery_score changed after correct answer
    mastery_updated = False
    for m2 in mastery_2:
        key = m2.get("id") or m2.get("concept_name")
        prev = mastery_scores_before.get(key)
        if prev is not None and m2["mastery_score"] != prev:
            mastery_updated = True
            break
    assert mastery_updated, (
        "mastery_score should have changed after correct answer"
    )
    print(f"\n  [PASS] mastery_score was updated")

    # 11. Verify answers field exists in session_summary
    assert "answers" in summary_2, "session_summary must include 'answers' field"
    print(f"  [PASS] session_summary includes 'answers' field")

    print(f"\nlearning state smoke test passed")


if __name__ == "__main__":
    main()
