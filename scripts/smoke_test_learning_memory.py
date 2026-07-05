"""Smoke test for the learning memory layer.

Run from the project root::

    python scripts/smoke_test_learning_memory.py

Expected output ends with ``learning memory smoke test passed``.
"""

import shutil
import sys
from pathlib import Path

# Make sure we can import the learning package even when the script is
# invoked from the project root.
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from backend.services.learning.memory import LearningMemory

TEST_DIR = Path("data/learning_smoke")
DB_PATH = TEST_DIR / "learning.sqlite3"


def main() -> None:
    # 1. Clean start
    if TEST_DIR.exists():
        shutil.rmtree(TEST_DIR)

    # 2. Create memory
    mem = LearningMemory(db_path=DB_PATH)
    print(f"SQLite 路径: {DB_PATH.resolve()}")

    # 3. Create user
    mem.ensure_user("demo", "Demo User")
    print("用户: demo 已创建")

    # 4. Default profile
    profile = mem.get_or_create_profile("demo")
    print(f"用户画像: {profile}")

    # 5. Create session
    session = mem.create_session("demo", "牙周炎")
    sid = session["id"]
    print(f"课程 session: {sid}")

    # 6. Save minimal outline
    outline = {
        "title": "牙周炎系统学习路径",
        "nodes": [
            {
                "id": "node_1",
                "title": "牙周炎的定义与疾病本质",
                "kg_node_ids": [],
                "learning_objectives": ["理解牙周炎和牙龈炎的核心区别"],
                "prerequisites": [],
                "estimated_minutes": 8,
                "difficulty": "basic",
                "source_refs": [],
            },
            {
                "id": "node_2",
                "title": "牙周炎的病因和关键机制",
                "kg_node_ids": [],
                "learning_objectives": ["掌握牙周炎的主要致病因素"],
                "prerequisites": ["node_1"],
                "estimated_minutes": 10,
                "difficulty": "basic",
                "source_refs": [],
            },
        ],
    }
    mem.save_outline(sid, outline, mindmap={}, source_trace={})

    # 7. Set current node
    mem.set_current_outline_node(sid, "node_1")
    session_before = mem.get_session(sid)
    current_before = session_before["current_outline_node_id"]
    print(f"打断前 current_node: {current_before}")

    # 8. Record lesson_started event
    mem.record_event(
        session_id=sid,
        user_id="demo",
        event_type="lesson_started",
        outline_node_id="node_1",
        payload={"action": "start"},
    )

    # 9. Record interruption question — must NOT change current_outline_node_id
    mem.record_event(
        session_id=sid,
        user_id="demo",
        event_type="user_interruption_question",
        outline_node_id=current_before,  # record which node the question occurred in, but do NOT advance progress
        payload={
            "question": "为什么牙周炎会导致牙槽骨吸收？",
            "advance_progress": False,
        },
    )

    session_after = mem.get_session(sid)
    current_after = session_after["current_outline_node_id"]
    print(f"打断后 current_node: {current_after}")

    # 10. Summary
    summary = mem.session_summary(sid)

    # 11. Assertions
    assert profile["level"] == "unknown", f"Expected level='unknown', got {profile['level']!r}"
    assert session["topic"] == "牙周炎", f"Expected topic='牙周炎', got {session['topic']!r}"
    assert current_after == "node_1", (
        f"current_outline_node_id changed from {current_before!r} to {current_after!r}"
    )
    assert len(summary["events"]) >= 2, f"Expected >= 2 events, got {len(summary['events'])}"

    latest = summary["outline"]
    assert latest is not None, "Expected outline in summary"
    assert latest["outline"]["title"] == "牙周炎系统学习路径", (
        f"Unexpected outline title: {latest['outline']['title']!r}"
    )

    # 12. Final output
    if current_after == current_before:
        print("结果: 课程进度未推进")
    print("learning memory smoke test passed")


if __name__ == "__main__":
    main()
