"""Interactive terminal experience for the OralSage learning backend.

This is not a fixed scripted demo.  It starts a real learning session and
lets the user choose the same actions the frontend will expose:

    1. 提问 -> QA
    2. 小测 -> answer current section quiz
    3. 继续 -> continue to next section
    4. 状态 -> inspect session state
    0. 退出

Run from the project root::

    python scripts/demo_learning_agent_terminal.py --topic 牙周炎

All user-visible content is returned by the backend agents.  The script does
not mock teaching text, QA answers, quiz questions, or grading.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable


def _configure_stdio() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


_configure_stdio()

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from backend.services.learning.handout_cache import list_handout_cache_status
from backend.services.learning.memory import LearningMemory
from backend.services.learning.orchestrator import LearningOrchestrator


def _clean_text(text: str) -> str:
    return text.encode("utf-8", errors="replace").decode("utf-8", errors="replace")


def _run_with_progress(
    title: str,
    fn: Callable[[], Any],
    messages: list[str],
    *,
    interval_seconds: float = 8.0,
) -> Any:
    done = threading.Event()

    started_at = time.perf_counter()
    print(f"[stream] {title} ...", flush=True)

    def _progress() -> None:
        tick = 0
        while not done.wait(interval_seconds):
            tick += 1
            message = messages[(tick - 1) % len(messages)] if messages else "仍在处理"
            elapsed = time.perf_counter() - started_at
            print(f"[stream +{elapsed:.1f}s] {message}", flush=True)

    progress_thread = threading.Thread(target=_progress, daemon=True)
    progress_thread.start()
    try:
        value = fn()
    except BaseException:
        done.set()
        progress_thread.join(timeout=1.0)
        elapsed = time.perf_counter() - started_at
        print(f"[stream +{elapsed:.1f}s] {title} 失败", flush=True)
        raise
    done.set()
    progress_thread.join(timeout=1.0)
    elapsed = time.perf_counter() - started_at
    print(f"[stream +{elapsed:.1f}s] {title} 完成", flush=True)
    return value


def _print_bar(title: str) -> None:
    print()
    print("=" * 72)
    print(f"  {title}")
    print("=" * 72)


def _print_actions() -> None:
    print()
    print("请选择下一步：")
    print("  1 / ask      提问，调用 QA，不推进课程")
    print("  2 / quiz     做当前小节小测")
    print("  3 / next     继续，进入下一节并讲解")
    print("  4 / state    查看当前 session 状态")
    print("  0 / quit     退出")


def _first_question(step: dict[str, Any]) -> dict[str, Any] | None:
    questions = step.get("questions") or []
    if questions:
        return questions[0]
    question = step.get("question")
    if isinstance(question, dict):
        return question
    return None


def _print_tutor_step(step: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not step:
        print("没有新的 Tutor step。")
        return []

    title = step.get("_handout_section_title") or step.get("outline_node_title") or step.get("outline_node_id")
    _print_bar(f"Tutor 当前讲解：{title}")
    print(f"content_status: {step.get('content_status')}")
    print(f"current_outline_node_id: {step.get('current_outline_node_id')}")
    print(f"handout_cache_hit: {step.get('handout_cache_hit', 'N/A')}")
    print(f"cache_miss_sync_generation: {step.get('cache_miss_sync_generation', 'N/A')}")
    print(f"handout_generation_source: {step.get('handout_generation_source', 'N/A')}")
    print(f"handout_generation_error: {step.get('handout_generation_error', 'N/A')}")
    print(f"fallback_rule_based: {step.get('trace', {}).get('fallback_rule_based', 'N/A')}")
    print()
    print(step.get("teaching_text", "(empty)"))

    questions = step.get("questions") or []
    _print_bar("本节 optional section quiz")
    if not questions:
        print("当前 Tutor step 没有返回题目。")
        return []
    for idx, q in enumerate(questions, start=1):
        print(f"\n[{idx}] {q.get('stem', '')}")
        for opt in q.get("options", []):
            print(f"    {opt}")
    return questions


def _print_qa_result(result: dict[str, Any]) -> None:
    _print_bar("QA Answer")
    print(f"qa_status: {result.get('qa_status')}")
    print(f"question_scope: {result.get('question_scope')}")
    print(f"evidence_status: {result.get('evidence_status')}")
    print(f"targeted_search_used: {result.get('targeted_search_used')}")
    print(f"advance_progress: {result.get('advance_progress')}")
    print()
    print(result.get("answer", "(empty)"))
    before = result.get("current_outline_node_id_before")
    after = result.get("current_outline_node_id_after")
    print()
    print(f"current_outline_node_id: {before} -> {after}")


def _print_assessment(result: dict[str, Any]) -> None:
    assessment = result.get("assessment", {})
    _print_bar("Assessment Result")
    print(f"is_correct: {assessment.get('is_correct')}")
    print(f"score: {assessment.get('score')}")
    print(f"selected_option: {assessment.get('selected_option')}")
    print(f"correct_option: {assessment.get('correct_option')}")
    print(f"fallback_rule_based: {assessment.get('fallback_rule_based')}")
    print(f"content_status: {assessment.get('content_status')}")
    print(f"advanced: {result.get('advanced')}")
    print(f"current_outline_node_id: {result.get('current_outline_node_id_before')} -> {result.get('current_outline_node_id_after')}")

    # Show full wrong-answer analysis
    if not assessment.get('is_correct'):
        print()
        print("─" * 60)
        print("【错因分析】")
        oea = assessment.get('option_error_analysis')
        if oea:
            print(oea)
        else:
            print("(无 LLM 错因分析 — fallback_rule_based)")
        print()
        print("【正确答案解释】")
        cae = assessment.get('correct_answer_explanation')
        if cae:
            print(cae)
        else:
            print("(无 LLM 正确解释 — fallback_rule_based)")
        print()
        print("【误区归纳】")
        ms = assessment.get('misconception_summary')
        if ms:
            print(ms)
        else:
            print("(无误区归纳)")
        print()
        print("【关联知识点】")
        rkg = assessment.get('related_kg_node_ids', [])
        if rkg:
            for kg in rkg:
                print(f"  - {kg}")
        else:
            print("  (无)")
        print()
        print("【补救建议】")
        rem = assessment.get('recommendation', {}).get('remedial_suggestion')
        if rem:
            print(rem)
        else:
            print("  (无 — 建议回顾本节讲义)")
        print()
        recommendation = assessment.get('recommendation', {})
        print(f"recommendation.action: {recommendation.get('action')}")
        print(f"recommendation.reason: {recommendation.get('reason')}")
        print("─" * 60)

        # Show mastery updates
        mastery_updates = result.get('mastery_updates', [])
        if mastery_updates:
            print()
            print("【Mastery 更新】")
            for m in mastery_updates:
                print(f"  - {m.get('kg_node_id', '(no kg)')}: "
                      f"score={m.get('mastery_score', '?')}, "
                      f"exposure={m.get('exposure_count', '?')}")
    else:
        # Correct answer — brief
        print()
        print(assessment.get("explanation", ""))
        recommendation = assessment.get("recommendation", {})
        if recommendation:
            print()
            print(f"recommendation.action: {recommendation.get('action')}")
            print(f"recommendation.reason: {recommendation.get('reason')}")


def _choose_question(questions: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not questions:
        print("当前没有可做的小测题。")
        return None
    if len(questions) == 1:
        return questions[0]
    raw = input(f"选择题号 1-{len(questions)}，直接回车默认第 1 题 > ").strip()
    if not raw:
        return questions[0]
    try:
        idx = int(raw)
    except ValueError:
        print("题号不是数字，本次取消。")
        return None
    if idx < 1 or idx > len(questions):
        print("题号超出范围，本次取消。")
        return None
    return questions[idx - 1]


def _print_state(orchestrator: LearningOrchestrator, session_id: str) -> None:
    state = orchestrator.get_policy_state(session_id)
    summary = orchestrator.memory.session_summary(session_id)
    _print_bar("Session State")
    print(f"session_id: {state.get('session_id')}")
    print(f"topic: {state.get('topic')}")
    print(f"status: {state.get('status')}")
    print(f"current_outline_node_id: {state.get('current_outline_node_id')}")
    print(f"events: {state.get('event_count')}")
    print(f"answers: {state.get('answer_count')}")
    print(f"mastery: {state.get('mastery_count')}")
    resources = state.get("resource_status", {})
    if resources:
        print()
        print("【PPT Slides Status】")
        print(f"  status:           {resources.get('status')}")
        print(f"  slide_count:      {resources.get('slide_count')}")
        print(f"  resource_path:    {resources.get('resource_path')}")
        if resources.get('error'):
            print(f"  error:            {resources.get('error')}")

    # Show handout cache status table
    try:
        cache_statuses = list_handout_cache_status(session_id, resource_root=orchestrator.resource_root)
        if cache_statuses:
            print()
            print("【Handout Cache Status】")
            header = f"  {'node_id':<12} {'title':<32} {'status':<12} {'attempts':<10} {'section_status':<16} {'source':<28}"
            print(header)
            print("  " + "-" * (len(header) - 2))
            for cs in cache_statuses:
                err = cs.get('error_short', '')
                print(
                    f"  {cs['outline_node_id']:<12} "
                    f"{cs['title'][:30]:<32} "
                    f"{cs['status']:<12} "
                    f"{cs.get('attempt_count', 0):<10} "
                    f"{cs['section_status']:<16} "
                    f"{cs['source']:<28}"
                    f"{' ERR:' + err if err else ''}"
                )
    except Exception:
        pass  # Non-fatal: cache display is diagnostic only

    # Show user profile
    profile = summary.get("profile", {})
    print()
    print("【User Profile】")
    print(f"  level: {profile.get('level', '?')}")
    print(f"  goal: {profile.get('goal', '?')}")
    print(f"  pace: {profile.get('pace', '?')}")
    print(f"  preferred_explanation: {profile.get('preferred_explanation', '?')}")
    print(f"  confidence: {profile.get('confidence', '?')}")
    weak = profile.get('known_weak_points', [])
    print(f"  known_weak_points ({len(weak)}): {', '.join(weak[:5])}{'...' if len(weak) > 5 else ''}")
    patterns = profile.get('mistake_patterns', [])
    print(f"  mistake_patterns ({len(patterns)}): {', '.join(str(p)[:60] for p in patterns[:3])}{'...' if len(patterns) > 3 else ''}")

    # Show mastery details
    mastery = summary.get("mastery", [])
    if mastery:
        print()
        print("【Mastery by KG Node】")
        for m in mastery[:10]:
            kg = m.get('kg_node_id', '(no kg)')
            score = m.get('mastery_score', 0)
            conf = m.get('confidence', 0)
            exp = m.get('exposure_count', 0)
            concept = m.get('concept_name', '')
            print(f"  [{kg}] score={score:.2f} conf={conf:.2f} exp={exp} | {concept[:50]}")

    events = summary.get("events", [])[-5:]
    if events:
        print()
        print("最近事件：")
        for event in events:
            print(f"  - {event.get('event_type')} node={event.get('outline_node_id')}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Interactive OralSage backend learning demo.")
    parser.add_argument("--topic", default="牙周炎", help="学习主题，默认：牙周炎")
    parser.add_argument("--user-id", default="terminal_demo", help="User id")
    parser.add_argument(
        "--keep-data",
        action="store_true",
        help="保留 data/learning_terminal_demo；默认每次清空重建",
    )
    args = parser.parse_args()

    demo_dir = Path("data/learning_terminal_demo")
    if demo_dir.exists() and not args.keep_data:
        try:
            shutil.rmtree(demo_dir)
        except PermissionError:
            demo_dir = Path("data") / f"learning_terminal_demo_{int(time.time())}"
            print(f"[WARN] 旧 demo 数据目录被占用，改用 {demo_dir}")
    demo_dir.mkdir(parents=True, exist_ok=True)

    memory = LearningMemory(db_path=demo_dir / "learning.sqlite3")
    orchestrator = LearningOrchestrator(
        memory=memory,
        resource_root=demo_dir / "resources",
    )

    _print_bar("OralSage Backend Terminal Experience")
    print(f"topic: {args.topic}")
    print("正在创建课程和生成大纲，这一步会真实调用 Planner / Resource Factory...")

    start = _run_with_progress(
        "课程规划与第一节讲义生成",
        lambda: orchestrator.start_lesson(
            user_id=args.user_id,
            topic=args.topic,
            display_name="Terminal Demo User",
        ),
        [
            "正在检索 KAG 证据并生成课程大纲。",
            "正在绑定小节 evidence / KG node。",
            "正在生成第一节 handout_section。",
            "正在启动后续小节后台预生成。",
        ],
    )
    session_id = start["session"]["id"]
    outline_nodes = start.get("outline", {}).get("nodes", [])
    print(f"session_id: {session_id}")
    print(f"outline_node_count: {len(outline_nodes)}")
    for idx, node in enumerate(outline_nodes, start=1):
        marker = " <- current" if idx == 1 else ""
        print(f"  {idx}. {node.get('title', '?')}{marker}")

    _print_bar("生成第一节 Tutor")
    current_step = _run_with_progress(
        "第一节 Tutor 讲解生成",
        lambda: orchestrator.next_step(session_id),
        [
            "正在基于第一节 handout 生成 Tutor 讲解。",
            "正在生成本节 quiz 与教学信号。",
            "正在写入学习事件与掌握度信号。",
        ],
    )
    current_questions = _print_tutor_step(current_step)

    while True:
        _print_actions()
        raw = input("\n选择 > ")
        text = _clean_text(raw).strip()
        if not text:
            continue
        lower = text.lower()

        if lower in {"0", "quit", "q", "exit"}:
            print("已退出。")
            break

        if lower in {"4", "state"}:
            _print_state(orchestrator, session_id)
            continue

        if lower in {"3", "next", "continue", "c", "下一节", "继续"}:
            result = _run_with_progress(
                "继续到下一节",
                lambda: orchestrator.continue_to_next_section(session_id),
                [
                    "正在检查下一节 handout/tutor cache。",
                    "如果后台已生成，将直接返回缓存讲解。",
                    "如果缓存未就绪，正在等待或同步生成下一节。",
                    "正在写入学习进度与事件。",
                ],
            )
            _print_bar("Continue Result")
            print(f"advanced: {result.get('advanced')}")
            print(f"lesson_completed: {result.get('lesson_completed')}")
            print(f"current_outline_node_id: {result.get('current_outline_node_id_before')} -> {result.get('current_outline_node_id_after')}")
            print(f"handout_cache_hit: {result.get('handout_cache_hit', 'N/A')}")
            print(f"cache_miss_sync_generation: {result.get('cache_miss_sync_generation', 'N/A')}")
            print(f"generation_blocked: {result.get('generation_blocked', False)}")
            if result.get("generation_blocked"):
                print(f"blocked_outline_node_id: {result.get('blocked_outline_node_id')}")
                print(f"handout_generation_error: {result.get('handout_generation_error')}")
            if result.get("lesson_completed"):
                print("课程已到末尾。")
                current_step = None
                current_questions = []
            else:
                current_step = result.get("tutor_step")
            current_questions = _print_tutor_step(current_step)
            continue

        if lower in {"2", "quiz", "qz", "小测", "做题"}:
            question = _choose_question(current_questions)
            if not question:
                continue
            _print_bar("Quiz")
            print(question.get("stem", ""))
            for opt in question.get("options", []):
                print(f"  {opt}")
            answer = input("你的答案（A/B/C/D）> ").strip()
            if not answer:
                print("未输入答案，本次取消。")
                continue
            result = _run_with_progress(
                "小测评估",
                lambda: orchestrator.submit_answer(session_id, question, answer),
                [
                    "正在分析用户选项与正确答案。",
                    "正在生成错因分析或正答解释。",
                    "正在更新掌握度与学习画像。",
                ],
            )
            _print_assessment(result)
            continue

        if lower in {"1", "ask", "question", "提问", "问"}:
            question_text = input("请输入你的问题 > ").strip()
            question_text = _clean_text(question_text)
            if not question_text:
                print("未输入问题，本次取消。")
                continue
            qa_result = _run_with_progress(
                "中途 QA 回答生成",
                lambda: orchestrator.handle_interruption(session_id, question_text),
                [
                    "正在读取当前小节 handout / evidence。",
                    "正在判断是否需要 targeted KAG search。",
                    "正在生成证据约束的 QA 回答。",
                    "正在更新学习画像与掌握度信号。",
                ],
            )
            _print_qa_result(qa_result)
            continue

        print("没有识别这个选择。请输入 1/2/3/4/0。")
        continue


if __name__ == "__main__":
    main()
