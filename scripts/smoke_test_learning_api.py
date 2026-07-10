"""Smoke test for the Learning Orchestrator FastAPI endpoints.

Uses FastAPI TestClient — does not start a real server.

Run from the project root::

    python scripts/smoke_test_learning_api.py --topic 牙周炎 --question "为什么牙周炎会导致牙槽骨吸收？"
    python scripts/smoke_test_learning_api.py --topic 根尖周炎 --question "根尖周炎和牙髓炎有什么区别？"

Expected output ends with ``learning api smoke test passed``.
"""

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from starlette.testclient import TestClient

from backend.main import app
from backend.services.learning.memory import LearningMemory

_DEFAULT_QUESTIONS = {
    "牙周炎": "为什么牙周炎会导致牙槽骨吸收？",
    "根尖周炎": "根尖周炎和牙髓炎有什么区别？",
}


def _first_question(step_result: dict) -> dict:
    questions = step_result.get("questions") or []
    if questions:
        return questions[0]
    question = step_result.get("question")
    if isinstance(question, dict):
        return question
    raise AssertionError("Tutor step should include at least one question")


def _wrong_answer_for(question: dict) -> str:
    correct = str(question.get("answer", "A")).strip().upper()
    for choice in ["A", "B", "C", "D"]:
        if choice != correct:
            return choice
    return "A"


def _print_separator(title: str) -> None:
    print()
    print("=" * 70)
    print(f"  {title}")
    print("=" * 70)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Smoke-test the Learning API endpoints via TestClient."
    )
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

    # 1. Setup — clean temp DB and patch orchestrator with a fresh memory.
    #    We also set check_same_thread=False because TestClient runs API
    #    handlers in a different OS thread from where we create the memory.
    smoke_dir = Path("data/learning_smoke")
    if smoke_dir.exists():
        shutil.rmtree(smoke_dir)
    smoke_dir.mkdir(parents=True, exist_ok=True)

    temp_db = smoke_dir / "api.sqlite3"
    resource_root = smoke_dir / "resources"

    memory = LearningMemory(db_path=temp_db)
    # Re-open the connection with check_same_thread=False so TestClient
    # (which dispatches to a different thread) can use it.
    # Preserve row_factory and other settings from the original connection.
    memory._conn.close()
    new_conn = sqlite3.connect(str(temp_db), check_same_thread=False)
    new_conn.row_factory = sqlite3.Row
    memory._conn = new_conn

    # Patch the module-level orchestrator
    from backend import main as main_mod

    main_mod.learning_orchestrator.memory = memory
    main_mod.learning_orchestrator.resource_root = resource_root

    client = TestClient(app)

    # ====================================================================
    # 2. POST /api/learning/session/start
    # ====================================================================
    print(f"=== POST /api/learning/session/start ===")
    print(f"Topic: {topic}")

    resp_start = client.post(
        "/api/learning/session/start",
        json={"user_id": "api_demo", "topic": topic},
    )
    assert resp_start.status_code == 200, (
        f"start: expected 200, got {resp_start.status_code}"
    )
    body_start = resp_start.json()
    assert body_start["ok"] is True, f"start: expected ok=True, got {body_start}"
    assert body_start["module"] == "learning"
    assert body_start["action"] == "start_lesson"

    result = body_start["result"]
    session_id: str = result["session"]["id"]
    outline = result["outline"]
    current_node_id = result["current_outline_node_id"]
    strategy = result["source_trace"]["strategy"]

    print(f"  session_id: {session_id}")
    print(f"  current_outline_node_id: {current_node_id}")
    print(f"  outline_node_count: {len(outline['nodes'])}")
    print(f"  planner strategy: {strategy}")
    print(f"  [PASS] start_lesson")

    # ====================================================================
    # 3. POST /api/learning/step
    # ====================================================================
    print(f"\n=== POST /api/learning/step ===")

    resp_step = client.post(
        "/api/learning/step",
        json={"session_id": session_id},
    )
    assert resp_step.status_code == 200, (
        f"step: expected 200, got {resp_step.status_code}"
    )
    body_step = resp_step.json()
    assert body_step["ok"] is True
    assert body_step["action"] == "step"

    step_decision = body_step["result"]["decision"]
    step_exec = body_step["result"]["execution"]

    assert step_decision["action"] == "teach_current_node", (
        f"expected teach_current_node, got {step_decision['action']!r}"
    )
    assert step_decision["action_type"] == "agent_call"
    assert step_decision["schema_version"] == "policy.v1"
    assert step_exec["executed"] is True, "Tutor step should execute"
    step_result = step_exec["result"]
    step_teaching = step_result.get("teaching_text", "")
    assert any(p in step_teaching[:200] for p in ["你好呀", "我是 OralSage"]), (
        "First section Tutor should introduce OralSage in the opening"
    )
    step_question = _first_question(step_result)
    correct_answer = str(step_question.get("answer", "A")).strip().upper()
    wrong_answer = _wrong_answer_for(step_question)

    print(f"  decision.action: {step_decision['action']}")
    print(f"  decision.action_type: {step_decision['action_type']}")
    print(f"  execution.executed: {step_exec['executed']}")
    print(f"  questions.count: {len(step_result.get('questions', []))}")
    print(f"  question.id: {step_question.get('id', '?')}")
    print(f"  [PASS] step → route(None) + execute (Tutor)")

    _print_separator("Tutor Step 1 Teaching Text (FULL)")
    print(step_teaching or "(empty)")
    _print_separator("Tutor Step 1 Questions (FULL)")
    print(json.dumps(step_result.get("questions", []), ensure_ascii=False, indent=2))

    # ====================================================================
    # 4. POST /api/learning/interruption
    # ====================================================================
    print(f"\n=== POST /api/learning/interruption ===")

    resp_int = client.post(
        "/api/learning/interruption",
        json={"session_id": session_id, "question": question},
    )
    assert resp_int.status_code == 200, (
        f"interruption: expected 200, got {resp_int.status_code}"
    )
    body_int = resp_int.json()
    assert body_int["ok"] is True
    assert body_int["action"] == "interruption"

    int_decision = body_int["result"]["decision"]
    int_exec = body_int["result"]["execution"]

    assert int_decision["action"] == "answer_interruption", (
        f"expected answer_interruption, got {int_decision['action']!r}"
    )
    assert int_decision["action_type"] == "agent_call"
    assert int_exec["executed"] is True, "QA should execute"
    qa_result = int_exec["result"]
    assert qa_result["advance_progress"] is False, "QA must not advance progress"
    assert qa_result.get("qa_status") == "llm_generated", (
        f"QA must be llm_generated for this smoke, got {qa_result.get('qa_status')!r}; "
        f"trace={qa_result.get('trace', {})}"
    )
    assert qa_result["current_outline_node_id_before"] == qa_result["current_outline_node_id_after"], (
        "QA should not change outline node"
    )

    print(f"  decision.action: {int_decision['action']}")
    print(f"  advance_progress: {qa_result['advance_progress']}")
    print(f"  qa_status: {qa_result.get('qa_status', '?')}")
    print(f"  question_scope: {qa_result.get('question_scope', '?')}")
    print(f"  [PASS] interruption → route + execute (QA), did not advance")

    _print_separator("QA Answer (FULL)")
    print(qa_result.get("answer", "(empty)"))

    # ====================================================================
    # 5. POST /api/learning/answer (wrong)
    # ====================================================================
    print(f"\n=== POST /api/learning/answer (wrong: {wrong_answer}) ===")

    resp_wrong = client.post(
        "/api/learning/answer",
        json={
            "session_id": session_id,
            "question": step_question,
            "answer": wrong_answer,
        },
    )
    assert resp_wrong.status_code == 200, (
        f"answer wrong: expected 200, got {resp_wrong.status_code}"
    )
    body_wrong = resp_wrong.json()
    assert body_wrong["ok"] is True
    assert body_wrong["action"] == "answer"

    wrong_decision = body_wrong["result"]["decision"]
    wrong_exec = body_wrong["result"]["execution"]

    assert wrong_decision["action"] == "assess_answer", (
        f"expected assess_answer, got {wrong_decision['action']!r}"
    )
    assert wrong_decision["action_type"] == "agent_call"
    assert wrong_exec["executed"] is True, "Assessment should execute"
    wrong_result = wrong_exec["result"]
    assert wrong_result["advanced"] is False, "Wrong answer must not advance"
    assert wrong_result["assessment"]["is_correct"] is False

    print(f"  decision.action: {wrong_decision['action']}")
    print(f"  is_correct: {wrong_result['assessment']['is_correct']}")
    print(f"  advanced: {wrong_result['advanced']}")
    print(f"  recommendation.action: {wrong_result['assessment']['recommendation']['action']}")
    print(f"  [PASS] answer (wrong) → route + execute (Assessment), did not advance")

    # ====================================================================
    # 6. POST /api/learning/continue
    # ====================================================================
    print(f"\n=== POST /api/learning/continue ===")

    resp_continue = client.post(
        "/api/learning/continue",
        json={"session_id": session_id},
    )
    assert resp_continue.status_code == 200, (
        f"continue: expected 200, got {resp_continue.status_code}"
    )
    body_continue = resp_continue.json()
    assert body_continue["ok"] is True
    assert body_continue["action"] == "continue"

    continue_decision = body_continue["result"]["decision"]
    continue_exec = body_continue["result"]["execution"]
    assert continue_decision["action"] == "continue_to_next_section", (
        f"expected continue_to_next_section, got {continue_decision['action']!r}"
    )
    assert continue_exec["executed"] is True, "Continue should execute"
    continue_result = continue_exec["result"]
    assert continue_result["advanced"] is True, "Continue should advance"
    next_tutor_step = continue_result.get("tutor_step")
    assert next_tutor_step is not None, "Continue should return next Tutor step"
    next_teaching = next_tutor_step.get("teaching_text", "")
    assert "你好呀，我是 OralSage" not in next_teaching[:200], (
        "Non-first section Tutor must not repeat the OralSage self-intro"
    )
    assert any(p in next_teaching[:200] for p in ["上一节", "刚才", "前面", "之前"]), (
        "Non-first section Tutor should connect from the previous section"
    )
    assert (
        next_tutor_step.get("current_outline_node_id")
        == continue_result["current_outline_node_id_after"]
    )

    print(f"  decision.action: {continue_decision['action']}")
    print(f"  advanced: {continue_result['advanced']}")
    print(f"  next_node: {continue_result['current_outline_node_id_after']}")
    print(f"  next tutor content_status: {next_tutor_step.get('content_status')}")
    print(f"  [PASS] continue → next section + Tutor")

    _print_separator("Tutor Step 2 Teaching Text (FULL)")
    print(next_teaching or "(empty)")
    _print_separator("Tutor Step 2 Questions (FULL)")
    print(json.dumps(next_tutor_step.get("questions", []), ensure_ascii=False, indent=2))

    # ====================================================================
    # 7. POST /api/learning/answer (correct)
    # ====================================================================
    current_question = _first_question(next_tutor_step)
    current_correct = str(current_question.get("answer", correct_answer)).strip().upper()
    print(f"\n=== POST /api/learning/answer (correct: {current_correct}) ===")

    resp_correct = client.post(
        "/api/learning/answer",
        json={
            "session_id": session_id,
            "question": current_question,
            "answer": current_correct,
        },
    )
    assert resp_correct.status_code == 200, (
        f"answer correct: expected 200, got {resp_correct.status_code}"
    )
    body_correct = resp_correct.json()
    assert body_correct["ok"] is True

    correct_decision = body_correct["result"]["decision"]
    correct_exec = body_correct["result"]["execution"]

    assert correct_decision["action"] == "assess_answer"
    assert correct_exec["executed"] is True
    correct_result = correct_exec["result"]
    assert correct_result["advanced"] is True, "Correct answer must advance"
    assert correct_result["assessment"]["is_correct"] is True

    print(f"  is_correct: {correct_result['assessment']['is_correct']}")
    print(f"  advanced: {correct_result['advanced']}")
    print(f"  recommendation.action: {correct_result['assessment']['recommendation']['action']}")
    print(f"  [PASS] answer (correct) → route + execute (Assessment), advanced")

    # ====================================================================
    # 8. GET /api/learning/session/{session_id}/state
    # ====================================================================
    print(f"\n=== GET /api/learning/session/{session_id}/state ===")

    resp_state = client.get(f"/api/learning/session/{session_id}/state")
    assert resp_state.status_code == 200, (
        f"state: expected 200, got {resp_state.status_code}"
    )
    body_state = resp_state.json()
    assert body_state["ok"] is True

    state_data = body_state["result"]
    policy_state = state_data["policy_state"]
    summary = state_data["summary"]

    answers = summary.get("answers", [])
    mastery = summary.get("mastery", [])
    events = summary.get("events", [])

    assert len(answers) >= 2, f"Expected >= 2 answer records, got {len(answers)}"
    assert len(mastery) >= 1, f"Expected >= 1 mastery record, got {len(mastery)}"

    print(f"  policy_state.event_count: {policy_state['event_count']}")
    print(f"  policy_state.answer_count: {policy_state['answer_count']}")
    print(f"  policy_state.mastery_count: {policy_state['mastery_count']}")
    print(f"  summary.answers count: {len(answers)}")
    print(f"  summary.mastery count: {len(mastery)}")
    print(f"  summary.events count: {len(events)}")
    print(f"  [PASS] state → answers={len(answers)}, mastery={len(mastery)}")

    # ====================================================================
    # 9. POST /api/learning/route (unknown input) — route only, no execution
    # ====================================================================
    print(f"\n=== POST /api/learning/route (unknown input) ===")

    resp_route = client.post(
        "/api/learning/route",
        json={
            "session_id": session_id,
            "user_input": {"type": "something_else", "data": "???"},
        },
    )
    assert resp_route.status_code == 200, (
        f"route unknown: expected 200, got {resp_route.status_code}"
    )
    body_route = resp_route.json()
    assert body_route["ok"] is True
    assert body_route["action"] == "route"

    route_decision = body_route["result"]["decision"]
    assert route_decision["action"] == "unknown_input", (
        f"expected unknown_input, got {route_decision['action']!r}"
    )
    assert route_decision["action_type"] == "clarification"

    print(f"  decision.action: {route_decision['action']}")
    print(f"  decision.action_type: {route_decision['action_type']}")
    print(f"  [PASS] route unknown → clarification, no execution")

    print(f"\nlearning api smoke test passed")


if __name__ == "__main__":
    main()
