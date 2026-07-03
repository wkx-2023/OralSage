# Learning Agent MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建 OralSage 学习 Agent 后端 MVP，使终端可以完整跑通“开始课程、生成资源、逐步讲课、中途答疑、答题评估、更新掌握度、输出总结”的闭环。

**Architecture:** 沿用当前项目的 Python service + FastAPI 风格，不引入新的 Agent 框架。新增 `backend/services/learning/` 作为学习域模块，SQLite 负责学习记忆，Resource Factory 负责课程资源派生，Master Orchestrator 负责调度 Planner、Tutor、QA、User Model 和 Assessment Engine。

**Tech Stack:** Python 3、FastAPI、Pydantic v2、SQLite 标准库、现有 `backend.services.llm_client`、现有 KAG/knowledge graph/retrieval 服务、可选 Node + PptxGenJS 生成 PPTX。

---

## 文件结构

新增文件：

```text
backend/services/learning/__init__.py
backend/services/learning/schemas.py
backend/services/learning/memory.py
backend/services/learning/user_model.py
backend/services/learning/planner.py
backend/services/learning/resource_factory.py
backend/services/learning/tutor.py
backend/services/learning/qa.py
backend/services/learning/assessment.py
backend/services/learning/orchestrator.py
backend/services/learning/prompts/planner.md
backend/services/learning/prompts/tutor.md
backend/services/learning/prompts/qa.md
backend/services/learning/prompts/user_model.md
backend/services/learning/prompts/handout.md
backend/services/learning/prompts/slides.md
scripts/learning_demo.py
scripts/smoke_test_learning.py
tests/test_learning_memory.py
tests/test_learning_orchestrator.py
tests/test_learning_resource_factory.py
```

修改文件：

```text
backend/schemas.py
backend/main.py
backend/requirements.txt
```

输出目录：

```text
data/learning/learning.sqlite3
data/learning/sessions/{session_id}/handout.md
data/learning/sessions/{session_id}/slides.json
data/learning/sessions/{session_id}/base_questions.json
data/learning/sessions/{session_id}/reading_list.json
data/learning/sessions/{session_id}/mindmap.json
data/learning/sessions/{session_id}/course.pptx
```

## 实现约束

- 不新增检索算子，Planner 只复用现有 KAG/KG/retrieval 能力，第一版可以用规则和 LLM 混合生成 outline。
- MVP 先支持单选题和判断题的确定性评估，简答题结构化评估可在后续阶段加入。
- QA 打断不能修改 `learning_sessions.current_outline_node_id`。
- 所有状态写入 SQLite，不依赖内存保存关键进度。
- Resource Factory 共享 Planner 的同一个 outline，讲义、slides、题目和 mindmap 不各自重新规划。
- PPTX 失败不能阻塞课程开始；保留 `slides.json` 并记录 partial resource event。
- 文档、注释和终端输出优先使用中文。

---

## Task 1: 学习域 Schemas

**Files:**
- Create: `backend/services/learning/__init__.py`
- Create: `backend/services/learning/schemas.py`
- Test: `tests/test_learning_memory.py`

- [ ] **Step 1: 编写 schema 导入测试**

创建 `tests/test_learning_memory.py`，先写最小导入与默认画像测试：

```python
from backend.services.learning.schemas import (
    DEFAULT_PROFILE,
    LearningProfile,
    LessonOutline,
    OutlineNode,
)


def test_default_profile_is_valid():
    profile = LearningProfile(**DEFAULT_PROFILE)

    assert profile.level == "unknown"
    assert profile.goal == "course_learning"
    assert profile.confidence == 0.2


def test_outline_schema_accepts_minimal_node():
    outline = LessonOutline(
        title="牙周炎系统学习路径",
        nodes=[
            OutlineNode(
                id="node_1",
                title="牙周炎的定义",
                kg_node_ids=[],
                learning_objectives=["说出牙周炎的基本定义"],
                prerequisites=[],
                estimated_minutes=8,
                difficulty="basic",
                source_refs=[],
            )
        ],
    )

    assert outline.nodes[0].id == "node_1"
```

- [ ] **Step 2: 运行测试确认失败**

Run:

```bash
python -m pytest tests/test_learning_memory.py -v
```

Expected: FAIL，错误包含 `No module named 'backend.services.learning'`。

- [ ] **Step 3: 创建 learning package 和 schemas**

创建 `backend/services/learning/__init__.py`：

```python
from __future__ import annotations

from .orchestrator import LearningOrchestrator

__all__ = ["LearningOrchestrator"]
```

创建 `backend/services/learning/schemas.py`：

```python
from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field


DEFAULT_PROFILE = {
    "level": "unknown",
    "goal": "course_learning",
    "preferred_explanation": "concept_then_case",
    "pace": "standard",
    "known_weak_points": [],
    "mistake_patterns": [],
    "target_exam": None,
    "confidence": 0.2,
}


class LearningProfile(BaseModel):
    level: str = "unknown"
    goal: str = "course_learning"
    preferred_explanation: str = "concept_then_case"
    pace: str = "standard"
    known_weak_points: list[str] = Field(default_factory=list)
    mistake_patterns: list[str] = Field(default_factory=list)
    target_exam: str | None = None
    confidence: float = 0.2


class SourceRef(BaseModel):
    source_id: str | None = None
    document: str | None = None
    page: int | None = None
    chunk_id: str | None = None
    quote: str | None = None


class OutlineNode(BaseModel):
    id: str
    title: str
    kg_node_ids: list[str] = Field(default_factory=list)
    learning_objectives: list[str] = Field(default_factory=list)
    prerequisites: list[str] = Field(default_factory=list)
    estimated_minutes: int = 8
    difficulty: str = "basic"
    source_refs: list[SourceRef | dict[str, Any]] = Field(default_factory=list)


class LessonOutline(BaseModel):
    title: str
    nodes: list[OutlineNode]


class AgentResult(BaseModel):
    ok: bool = True
    agent: str
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    events: list[dict[str, Any]] = Field(default_factory=list)
    next_action: dict[str, Any] = Field(default_factory=dict)
    trace: dict[str, Any] = Field(default_factory=dict)


class TutorStep(BaseModel):
    outline_node_id: str
    teaching_text: str
    question: dict[str, Any] | None = None
    expected_user_action: Literal["answer", "continue", "ask"] = "answer"
    progress: dict[str, int] = Field(default_factory=dict)


class AssessmentResult(BaseModel):
    question_id: str
    is_correct: bool
    score: float
    mistake_type: str | None = None
    affected_kg_node_ids: list[str] = Field(default_factory=list)
    mastery_delta: float = 0.0
    recommendation: dict[str, Any] = Field(default_factory=dict)
```

- [ ] **Step 4: 运行测试确认通过**

Run:

```bash
python -m pytest tests/test_learning_memory.py -v
```

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/services/learning/__init__.py backend/services/learning/schemas.py tests/test_learning_memory.py
git commit -m "feat: add learning service schemas"
```

---

## Task 2: SQLite 学习记忆层

**Files:**
- Modify: `backend/services/learning/memory.py`
- Modify: `tests/test_learning_memory.py`

- [ ] **Step 1: 编写 Memory 测试**

追加到 `tests/test_learning_memory.py`：

```python
from pathlib import Path

from backend.services.learning.memory import LearningMemory


def test_memory_creates_profile_session_event_and_summary(tmp_path: Path):
    db_path = tmp_path / "learning.sqlite3"
    memory = LearningMemory(db_path)

    profile = memory.get_or_create_profile("demo")
    session = memory.create_session(user_id="demo", topic="牙周炎")
    memory.record_event(
        session_id=session["id"],
        user_id="demo",
        event_type="lesson_started",
        outline_node_id=None,
        payload={"topic": "牙周炎"},
    )

    summary = memory.session_summary(session["id"])

    assert profile["level"] == "unknown"
    assert summary["session"]["topic"] == "牙周炎"
    assert summary["events"][0]["event_type"] == "lesson_started"


def test_memory_keeps_current_node_stable_during_qa(tmp_path: Path):
    memory = LearningMemory(tmp_path / "learning.sqlite3")
    session = memory.create_session(user_id="demo", topic="牙周炎")
    memory.save_outline(
        session_id=session["id"],
        outline={"title": "牙周炎", "nodes": [{"id": "node_1", "title": "定义"}]},
        mindmap={"root": {"name": "牙周炎"}},
        source_trace={},
    )
    memory.set_current_outline_node(session["id"], "node_1")
    before = memory.get_session(session["id"])["current_outline_node_id"]

    memory.record_event(
        session_id=session["id"],
        user_id="demo",
        event_type="user_interruption_question",
        outline_node_id=before,
        payload={"question": "为什么会骨吸收？"},
    )

    after = memory.get_session(session["id"])["current_outline_node_id"]
    assert before == after == "node_1"
```

- [ ] **Step 2: 运行测试确认失败**

Run:

```bash
python -m pytest tests/test_learning_memory.py -v
```

Expected: FAIL，错误包含 `No module named 'backend.services.learning.memory'`。

- [ ] **Step 3: 实现 SQLite memory**

创建 `backend/services/learning/memory.py`，实现以下 public 方法：

```python
from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import sqlite3
import time
import uuid

from .schemas import DEFAULT_PROFILE


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "learning" / "learning.sqlite3"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def _loads(text: str | None, default: Any) -> Any:
    if not text:
        return default
    return json.loads(text)


class LearningMemory:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    display_name TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_profiles (
                    user_id TEXT PRIMARY KEY,
                    profile_json TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS learning_sessions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_outline_node_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lesson_outlines (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    outline_json TEXT NOT NULL,
                    mindmap_json TEXT NOT NULL,
                    source_trace_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS generated_resources (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    resource_type TEXT NOT NULL,
                    resource_path TEXT,
                    resource_json TEXT NOT NULL,
                    source_trace_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lesson_events (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    outline_node_id TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mastery_records (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    kg_node_id TEXT,
                    concept_name TEXT NOT NULL,
                    mastery_score REAL NOT NULL,
                    confidence REAL NOT NULL,
                    evidence_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(user_id, session_id, concept_name)
                );
                CREATE TABLE IF NOT EXISTS answer_records (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    question_id TEXT NOT NULL,
                    outline_node_id TEXT,
                    user_answer TEXT NOT NULL,
                    grading_json TEXT NOT NULL,
                    is_correct INTEGER NOT NULL,
                    score REAL NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def ensure_user(self, user_id: str, display_name: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO users (id, display_name, created_at) VALUES (?, ?, ?)",
                (user_id, display_name or user_id, _now()),
            )

    def get_or_create_profile(self, user_id: str, display_name: str | None = None) -> dict[str, Any]:
        self.ensure_user(user_id, display_name)
        with self._connect() as conn:
            row = conn.execute("SELECT profile_json FROM user_profiles WHERE user_id = ?", (user_id,)).fetchone()
            if row:
                return _loads(row["profile_json"], dict(DEFAULT_PROFILE))
            profile = dict(DEFAULT_PROFILE)
            conn.execute(
                "INSERT INTO user_profiles (user_id, profile_json, confidence, updated_at) VALUES (?, ?, ?, ?)",
                (user_id, _json(profile), profile["confidence"], _now()),
            )
            return profile

    def save_profile(self, user_id: str, profile: dict[str, Any]) -> dict[str, Any]:
        self.ensure_user(user_id)
        confidence = float(profile.get("confidence", 0.2))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO user_profiles (user_id, profile_json, confidence, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    profile_json = excluded.profile_json,
                    confidence = excluded.confidence,
                    updated_at = excluded.updated_at
                """,
                (user_id, _json(profile), confidence, _now()),
            )
        return profile

    def create_session(self, user_id: str, topic: str, mode: str = "course_learning") -> dict[str, Any]:
        self.ensure_user(user_id)
        session_id = f"lesson_{uuid.uuid4().hex[:12]}"
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO learning_sessions
                (id, user_id, topic, mode, status, current_outline_node_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (session_id, user_id, topic, mode, "active", None, now, now),
            )
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM learning_sessions WHERE id = ?", (session_id,)).fetchone()
        if not row:
            raise KeyError(f"learning session not found: {session_id}")
        return dict(row)

    def set_current_outline_node(self, session_id: str, node_id: str | None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE learning_sessions SET current_outline_node_id = ?, updated_at = ? WHERE id = ?",
                (node_id, _now(), session_id),
            )

    def save_outline(self, session_id: str, outline: dict[str, Any], mindmap: dict[str, Any], source_trace: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO lesson_outlines (id, session_id, outline_json, mindmap_json, source_trace_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (uuid.uuid4().hex, session_id, _json(outline), _json(mindmap), _json(source_trace), _now()),
            )

    def latest_outline(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT outline_json, mindmap_json, source_trace_json FROM lesson_outlines WHERE session_id = ? ORDER BY created_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "outline": _loads(row["outline_json"], {}),
            "mindmap": _loads(row["mindmap_json"], {}),
            "source_trace": _loads(row["source_trace_json"], {}),
        }

    def record_event(self, session_id: str, user_id: str, event_type: str, outline_node_id: str | None, payload: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO lesson_events (id, session_id, user_id, event_type, outline_node_id, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (uuid.uuid4().hex, session_id, user_id, event_type, outline_node_id, _json(payload), _now()),
            )

    def session_summary(self, session_id: str) -> dict[str, Any]:
        session = self.get_session(session_id)
        with self._connect() as conn:
            events = [
                {**dict(row), "payload": _loads(row["payload_json"], {})}
                for row in conn.execute(
                    "SELECT * FROM lesson_events WHERE session_id = ? ORDER BY created_at ASC",
                    (session_id,),
                ).fetchall()
            ]
            mastery = [dict(row) for row in conn.execute("SELECT * FROM mastery_records WHERE session_id = ?", (session_id,)).fetchall()]
        return {"session": session, "events": events, "mastery": mastery, "outline": self.latest_outline(session_id)}
```

- [ ] **Step 4: 运行测试确认通过**

Run:

```bash
python -m pytest tests/test_learning_memory.py -v
```

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/services/learning/memory.py tests/test_learning_memory.py
git commit -m "feat: add sqlite learning memory"
```

---

## Task 3: User Model 与 Planner 最小路径

**Files:**
- Create: `backend/services/learning/user_model.py`
- Create: `backend/services/learning/planner.py`
- Create: `backend/services/learning/prompts/user_model.md`
- Create: `backend/services/learning/prompts/planner.md`
- Test: `tests/test_learning_orchestrator.py`

- [ ] **Step 1: 编写 User Model 和 Planner 测试**

创建 `tests/test_learning_orchestrator.py`：

```python
from backend.services.learning.planner import plan_lesson
from backend.services.learning.user_model import diagnose_profile


def test_diagnose_profile_normalizes_quick_answers():
    profile = diagnose_profile(
        {
            "level": "本科高年级",
            "goal": "期末考试",
            "preferred_explanation": "病例带入",
            "pace": "标准",
        }
    )

    assert profile["level"] == "本科高年级"
    assert profile["goal"] == "期末考试"
    assert profile["preferred_explanation"] == "病例带入"
    assert profile["confidence"] >= 0.6


def test_plan_lesson_returns_outline_and_mindmap():
    result = plan_lesson(topic="牙周炎", user_profile={"level": "unknown"})

    outline = result["outline"]
    assert outline["title"]
    assert len(outline["nodes"]) >= 3
    assert outline["nodes"][0]["id"] == "node_1"
    assert result["mindmap"]["root"]["name"] == outline["title"]
```

- [ ] **Step 2: 运行测试确认失败**

Run:

```bash
python -m pytest tests/test_learning_orchestrator.py -v
```

Expected: FAIL，错误包含 missing module 或 missing function。

- [ ] **Step 3: 实现 User Model**

创建 `backend/services/learning/user_model.py`：

```python
from __future__ import annotations

from typing import Any

from .schemas import DEFAULT_PROFILE, LearningProfile


def diagnose_profile(answers: dict[str, Any] | None = None) -> dict[str, Any]:
    answers = answers or {}
    profile = dict(DEFAULT_PROFILE)
    for key in ("level", "goal", "preferred_explanation", "pace", "target_exam"):
        value = answers.get(key)
        if value:
            profile[key] = str(value)
    profile["known_weak_points"] = list(answers.get("known_weak_points") or [])
    profile["mistake_patterns"] = list(answers.get("mistake_patterns") or [])
    profile["confidence"] = 0.65 if answers else 0.2
    return LearningProfile(**profile).model_dump()


def update_profile_from_event(profile: dict[str, Any], event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    updated = LearningProfile(**{**DEFAULT_PROFILE, **profile}).model_dump()
    if event_type == "user_interruption_question":
        tags = payload.get("confusion_tags") or []
        weak_points = set(updated["known_weak_points"])
        weak_points.update(str(tag) for tag in tags)
        updated["known_weak_points"] = sorted(weak_points)
    if event_type == "assessment_completed":
        mistake_type = payload.get("mistake_type")
        if mistake_type:
            patterns = set(updated["mistake_patterns"])
            patterns.add(str(mistake_type))
            updated["mistake_patterns"] = sorted(patterns)
    updated["confidence"] = min(0.95, max(float(updated.get("confidence") or 0.2), 0.35))
    return updated
```

创建 `backend/services/learning/prompts/user_model.md`：

```markdown
# 用户画像标准化

你负责把快速诊断答案标准化为学习画像 JSON。MVP 中代码优先使用规则标准化；当需要 LLM 辅助时，必须只输出 JSON。
```

- [ ] **Step 4: 实现 Planner 最小 outline**

创建 `backend/services/learning/planner.py`：

```python
from __future__ import annotations

from typing import Any


def _default_nodes(topic: str) -> list[dict[str, Any]]:
    titles = [
        f"{topic}的定义与疾病本质",
        f"{topic}的病因和关键机制",
        f"{topic}的临床表现与检查要点",
        f"{topic}的鉴别诊断",
        f"{topic}的治疗原则与复习总结",
    ]
    return [
        {
            "id": f"node_{index}",
            "title": title,
            "kg_node_ids": [],
            "learning_objectives": [f"理解{title}"],
            "prerequisites": [] if index == 1 else [f"node_{index - 1}"],
            "estimated_minutes": 8,
            "difficulty": "basic" if index <= 2 else "intermediate",
            "source_refs": [],
        }
        for index, title in enumerate(titles, start=1)
    ]


def plan_lesson(topic: str, user_profile: dict[str, Any] | None = None, max_nodes: int = 5) -> dict[str, Any]:
    clean_topic = (topic or "").strip() or "口腔医学主题"
    nodes = _default_nodes(clean_topic)[:max_nodes]
    outline = {"title": f"{clean_topic}系统学习路径", "nodes": nodes}
    mindmap = {
        "format": "markmap_json",
        "root": {
            "name": outline["title"],
            "children": [{"name": node["title"], "id": node["id"]} for node in nodes],
        },
    }
    return {
        "outline": outline,
        "mindmap": mindmap,
        "source_trace": {
            "strategy": "kag_preferred_minimal_planner",
            "warnings": ["MVP planner uses deterministic outline when KAG context is unavailable."],
            "user_profile": user_profile or {},
        },
    }
```

创建 `backend/services/learning/prompts/planner.md`：

```markdown
# 学习路径规划

根据用户主题、用户画像、掌握情况和 KAG 证据生成课程 outline。输出必须是 JSON，不要输出 Markdown 正文。
```

- [ ] **Step 5: 运行测试确认通过**

Run:

```bash
python -m pytest tests/test_learning_orchestrator.py -v
```

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add backend/services/learning/user_model.py backend/services/learning/planner.py backend/services/learning/prompts/user_model.md backend/services/learning/prompts/planner.md tests/test_learning_orchestrator.py
git commit -m "feat: add learning profile and planner"
```

---

## Task 4: Resource Factory 生成讲义、slides、题目、阅读和 mindmap

**Files:**
- Create: `backend/services/learning/resource_factory.py`
- Create: `backend/services/learning/prompts/handout.md`
- Create: `backend/services/learning/prompts/slides.md`
- Test: `tests/test_learning_resource_factory.py`

- [ ] **Step 1: 编写 Resource Factory 测试**

创建 `tests/test_learning_resource_factory.py`：

```python
from pathlib import Path

from backend.services.learning.planner import plan_lesson
from backend.services.learning.resource_factory import generate_resources


def test_generate_resources_writes_expected_files(tmp_path: Path):
    plan = plan_lesson("牙周炎", {})
    resources = generate_resources(
        session_id="lesson_demo",
        topic="牙周炎",
        outline=plan["outline"],
        mindmap=plan["mindmap"],
        output_root=tmp_path,
    )

    resource_types = {item["resource_type"] for item in resources}
    assert {"handout_md", "slides_json", "base_questions_json", "reading_list_json", "mindmap_json"}.issubset(resource_types)
    assert (tmp_path / "lesson_demo" / "handout.md").exists()
    assert (tmp_path / "lesson_demo" / "slides.json").exists()
    assert (tmp_path / "lesson_demo" / "base_questions.json").exists()
```

- [ ] **Step 2: 运行测试确认失败**

Run:

```bash
python -m pytest tests/test_learning_resource_factory.py -v
```

Expected: FAIL，错误包含 missing `resource_factory`。

- [ ] **Step 3: 实现资源生成**

创建 `backend/services/learning/resource_factory.py`，实现：

```python
from __future__ import annotations

from pathlib import Path
from typing import Any
import json


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "data" / "learning" / "sessions"


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _build_handout(topic: str, outline: dict[str, Any]) -> str:
    lines = [f"# {outline.get('title') or topic}", ""]
    for node in outline.get("nodes", []):
        lines.extend(
            [
                f"## {node.get('title')}",
                "",
                "学习目标：",
                *[f"- {item}" for item in node.get("learning_objectives", [])],
                "",
                "课堂讲解将在 Tutor Agent 中按该节点展开。",
                "",
            ]
        )
    return "\n".join(lines).strip() + "\n"


def _build_slides(topic: str, outline: dict[str, Any]) -> dict[str, Any]:
    slides = [{"type": "title", "title": outline.get("title") or topic, "subtitle": "OralSage 个性化课程草稿"}]
    for node in outline.get("nodes", []):
        slides.append(
            {
                "type": "content",
                "title": node.get("title"),
                "bullets": node.get("learning_objectives", [])[:4] or ["理解本节核心概念"],
                "speaker_notes": f"围绕 {node.get('title')} 展开讲解，并根据用户掌握情况插入小测。",
                "source_refs": node.get("source_refs", []),
            }
        )
    return {"title": outline.get("title") or topic, "slides": slides}


def _build_questions(outline: dict[str, Any]) -> dict[str, Any]:
    questions = []
    for index, node in enumerate(outline.get("nodes", []), start=1):
        questions.append(
            {
                "id": f"q_{index}",
                "type": "single_choice",
                "outline_node_id": node.get("id"),
                "kg_node_ids": node.get("kg_node_ids", []),
                "stem": f"关于“{node.get('title')}”，下列哪项最符合本节学习目标？",
                "options": ["A. 只需记忆名词", "B. 需要理解核心概念和关系", "C. 与临床判断无关", "D. 不需要复习"],
                "answer": "B",
                "explanation": "本题用于检查是否理解本节核心概念和关系。",
                "difficulty": node.get("difficulty", "basic"),
                "source_refs": node.get("source_refs", []),
            }
        )
    return {"questions": questions}


def _build_reading_list(outline: dict[str, Any]) -> dict[str, Any]:
    items = []
    for node in outline.get("nodes", []):
        items.append(
            {
                "outline_node_id": node.get("id"),
                "title": f"{node.get('title')}：教材与权威资料复习",
                "source_refs": node.get("source_refs", []),
                "note": "MVP 阶段优先回溯 KAG source_refs；无 source_refs 时保留占位记录供后续检索补全。",
            }
        )
    return {"items": items}


def generate_resources(
    *,
    session_id: str,
    topic: str,
    outline: dict[str, Any],
    mindmap: dict[str, Any],
    output_root: str | Path = DEFAULT_OUTPUT_ROOT,
) -> list[dict[str, Any]]:
    session_dir = Path(output_root) / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    handout_path = session_dir / "handout.md"
    slides_path = session_dir / "slides.json"
    questions_path = session_dir / "base_questions.json"
    reading_path = session_dir / "reading_list.json"
    mindmap_path = session_dir / "mindmap.json"

    handout_path.write_text(_build_handout(topic, outline), encoding="utf-8")
    _write_json(slides_path, _build_slides(topic, outline))
    _write_json(questions_path, _build_questions(outline))
    _write_json(reading_path, _build_reading_list(outline))
    _write_json(mindmap_path, mindmap)

    return [
        {"resource_type": "handout_md", "resource_path": str(handout_path), "resource_json": {}},
        {"resource_type": "slides_json", "resource_path": str(slides_path), "resource_json": {}},
        {"resource_type": "base_questions_json", "resource_path": str(questions_path), "resource_json": {}},
        {"resource_type": "reading_list_json", "resource_path": str(reading_path), "resource_json": {}},
        {"resource_type": "mindmap_json", "resource_path": str(mindmap_path), "resource_json": mindmap},
    ]
```

创建 prompt 文件：

```markdown
# handout.md
根据课程 outline 生成专业、清晰、可追溯的中文讲义。MVP 可由规则模板生成，后续再接 LLM 精修。
```

```markdown
# slides.md
根据课程 outline 生成 slides JSON。每页包含 type、title、bullets、speaker_notes 和 source_refs。
```

- [ ] **Step 4: 运行测试确认通过**

Run:

```bash
python -m pytest tests/test_learning_resource_factory.py -v
```

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/services/learning/resource_factory.py backend/services/learning/prompts/handout.md backend/services/learning/prompts/slides.md tests/test_learning_resource_factory.py
git commit -m "feat: add learning resource factory"
```

---

## Task 5: Tutor、QA 与 Assessment Engine

**Files:**
- Create: `backend/services/learning/tutor.py`
- Create: `backend/services/learning/qa.py`
- Create: `backend/services/learning/assessment.py`
- Create: `backend/services/learning/prompts/tutor.md`
- Create: `backend/services/learning/prompts/qa.md`
- Modify: `tests/test_learning_orchestrator.py`

- [ ] **Step 1: 编写 Tutor/QA/Assessment 测试**

追加到 `tests/test_learning_orchestrator.py`：

```python
from backend.services.learning.assessment import assess_answer
from backend.services.learning.qa import answer_interruption
from backend.services.learning.tutor import build_tutor_step


def test_tutor_step_uses_current_outline_node():
    plan = plan_lesson("牙周炎", {})
    step = build_tutor_step(plan["outline"], current_node_id="node_1", mastery={})

    assert step["outline_node_id"] == "node_1"
    assert "牙周炎" in step["teaching_text"]
    assert step["question"]["outline_node_id"] == "node_1"


def test_qa_answer_marks_no_progress_advance():
    plan = plan_lesson("牙周炎", {})
    result = answer_interruption(
        question="为什么会牙槽骨吸收？",
        outline=plan["outline"],
        current_node_id="node_2",
        user_profile={},
    )

    assert result["related_outline_node_id"] == "node_2"
    assert result["should_tutor_reinforce_later"] is True
    assert result["advance_progress"] is False


def test_assess_single_choice_answer():
    question = {
        "id": "q_1",
        "type": "single_choice",
        "answer": "B",
        "outline_node_id": "node_1",
        "kg_node_ids": [],
    }

    result = assess_answer(question=question, user_answer="A")

    assert result["question_id"] == "q_1"
    assert result["is_correct"] is False
    assert result["mastery_delta"] < 0
    assert result["recommendation"]["action"] == "reinforce"
```

- [ ] **Step 2: 运行测试确认失败**

Run:

```bash
python -m pytest tests/test_learning_orchestrator.py -v
```

Expected: FAIL，错误包含 missing module。

- [ ] **Step 3: 实现 Tutor**

创建 `backend/services/learning/tutor.py`：

```python
from __future__ import annotations

from typing import Any


def _find_node(outline: dict[str, Any], node_id: str | None) -> tuple[int, dict[str, Any]]:
    nodes = outline.get("nodes") or []
    for index, node in enumerate(nodes):
        if node.get("id") == node_id:
            return index, node
    if nodes:
        return 0, nodes[0]
    return 0, {"id": "node_1", "title": outline.get("title") or "当前主题", "learning_objectives": []}


def build_tutor_step(outline: dict[str, Any], current_node_id: str | None, mastery: dict[str, Any] | None = None) -> dict[str, Any]:
    index, node = _find_node(outline, current_node_id)
    nodes = outline.get("nodes") or [node]
    title = str(node.get("title") or "当前知识点")
    objectives = node.get("learning_objectives") or [f"理解{title}"]
    question = {
        "id": f"q_inline_{node.get('id')}",
        "type": "single_choice",
        "outline_node_id": node.get("id"),
        "stem": f"关于“{title}”，本节最重要的是？",
        "options": ["A. 跳过机制", "B. 理解核心概念和关系", "C. 只看结论", "D. 不需要练习"],
        "answer": "B",
        "explanation": "本节目标是理解核心概念和关系，并能用于后续判断。",
        "kg_node_ids": node.get("kg_node_ids", []),
    }
    return {
        "outline_node_id": node.get("id"),
        "teaching_text": f"现在讲“{title}”。我们先抓住目标：{objectives[0]}。接下来会用一个小题检查你是否真正理解。",
        "question": question,
        "expected_user_action": "answer",
        "progress": {"current_index": index + 1, "total": len(nodes)},
        "mastery": mastery or {},
    }
```

- [ ] **Step 4: 实现 QA**

创建 `backend/services/learning/qa.py`：

```python
from __future__ import annotations

from typing import Any


def answer_interruption(question: str, outline: dict[str, Any], current_node_id: str | None, user_profile: dict[str, Any] | None = None) -> dict[str, Any]:
    current_node = None
    for node in outline.get("nodes", []):
        if node.get("id") == current_node_id:
            current_node = node
            break
    title = current_node.get("title") if current_node else outline.get("title", "当前课程")
    return {
        "answer": f"这个问题和“{title}”有关。MVP 阶段先给出教学型回答：{question} 可以从概念、机制和临床意义三层理解；后续 Tutor 会在原课程进度处继续，不会因为这次答疑跳步。",
        "related_outline_node_id": current_node_id,
        "related_kg_node_ids": current_node.get("kg_node_ids", []) if current_node else [],
        "confusion_tags": ["课堂打断问题"],
        "should_tutor_reinforce_later": True,
        "advance_progress": False,
        "source_refs": current_node.get("source_refs", []) if current_node else [],
        "user_profile": user_profile or {},
    }
```

- [ ] **Step 5: 实现 Assessment**

创建 `backend/services/learning/assessment.py`：

```python
from __future__ import annotations

from typing import Any


def assess_answer(question: dict[str, Any], user_answer: str) -> dict[str, Any]:
    expected = str(question.get("answer") or "").strip().upper()
    actual = str(user_answer or "").strip().upper()
    is_correct = bool(expected) and actual == expected
    mastery_delta = 0.08 if is_correct else -0.05
    action = "continue" if is_correct else "reinforce"
    return {
        "question_id": question.get("id"),
        "is_correct": is_correct,
        "score": 1.0 if is_correct else 0.0,
        "mistake_type": None if is_correct else "概念混淆",
        "affected_kg_node_ids": question.get("kg_node_ids", []),
        "mastery_delta": mastery_delta,
        "recommendation": {
            "action": action,
            "reason": "回答正确，可以继续。" if is_correct else "回答不正确，需要补讲或加练。",
        },
        "explanation": question.get("explanation", ""),
    }
```

创建 `backend/services/learning/prompts/tutor.md` 和 `backend/services/learning/prompts/qa.md`：

```markdown
# Tutor Agent

按当前 outline node 生成中文课堂讲解、小测和反馈。不要修改课程进度，进度由 Orchestrator 写入 Learning Memory。
```

```markdown
# QA Agent

回答课堂打断问题。回答后输出困惑标签和相关节点，不能推进 current_outline_node_id。
```

- [ ] **Step 6: 运行测试确认通过**

Run:

```bash
python -m pytest tests/test_learning_orchestrator.py -v
```

Expected: PASS。

- [ ] **Step 7: 提交**

```bash
git add backend/services/learning/tutor.py backend/services/learning/qa.py backend/services/learning/assessment.py backend/services/learning/prompts/tutor.md backend/services/learning/prompts/qa.md tests/test_learning_orchestrator.py
git commit -m "feat: add tutor qa and assessment engine"
```

---

## Task 6: Orchestrator 串起 start/ask/answer/next/summary

**Files:**
- Create: `backend/services/learning/orchestrator.py`
- Modify: `backend/services/learning/memory.py`
- Modify: `tests/test_learning_orchestrator.py`

- [ ] **Step 1: 编写 Orchestrator 端到端测试**

追加到 `tests/test_learning_orchestrator.py`：

```python
from pathlib import Path

from backend.services.learning.orchestrator import LearningOrchestrator


def test_orchestrator_start_ask_answer_next_flow(tmp_path: Path):
    orchestrator = LearningOrchestrator(db_path=tmp_path / "learning.sqlite3", resource_root=tmp_path / "sessions")

    started = orchestrator.start_session(user_id="demo", topic="牙周炎")
    session_id = started["session"]["id"]
    current_node = started["session"]["current_outline_node_id"]

    asked = orchestrator.ask(session_id=session_id, question="为什么会牙槽骨吸收？")
    after_ask = orchestrator.summary(session_id)["session"]["current_outline_node_id"]

    answered = orchestrator.answer(session_id=session_id, question=started["tutor_step"]["question"], user_answer="A")
    next_step = orchestrator.next(session_id=session_id)

    assert current_node == "node_1"
    assert asked["qa"]["advance_progress"] is False
    assert after_ask == current_node
    assert answered["assessment"]["is_correct"] is False
    assert next_step["session"]["current_outline_node_id"] in {"node_1", "node_2"}
```

- [ ] **Step 2: 运行测试确认失败**

Run:

```bash
python -m pytest tests/test_learning_orchestrator.py::test_orchestrator_start_ask_answer_next_flow -v
```

Expected: FAIL，错误包含 missing `orchestrator` 或 missing memory methods。

- [ ] **Step 3: 补充 memory 资源与答题写入方法**

在 `backend/services/learning/memory.py` 中新增：

```python
    def save_resource(self, session_id: str, resource_type: str, resource_path: str | None, resource_json: dict[str, Any], source_trace: dict[str, Any] | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO generated_resources (id, session_id, resource_type, resource_path, resource_json, source_trace_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (uuid.uuid4().hex, session_id, resource_type, resource_path, _json(resource_json), _json(source_trace or {}), _now()),
            )

    def list_resources(self, session_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM generated_resources WHERE session_id = ? ORDER BY created_at ASC", (session_id,)).fetchall()
        return [{**dict(row), "resource_json": _loads(row["resource_json"], {})} for row in rows]

    def save_answer(self, session_id: str, user_id: str, question_id: str, outline_node_id: str | None, user_answer: str, grading: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO answer_records (id, session_id, user_id, question_id, outline_node_id, user_answer, grading_json, is_correct, score, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    session_id,
                    user_id,
                    question_id,
                    outline_node_id,
                    user_answer,
                    _json(grading),
                    1 if grading.get("is_correct") else 0,
                    float(grading.get("score") or 0),
                    _now(),
                ),
            )

    def update_mastery(self, user_id: str, session_id: str, concept_name: str, kg_node_id: str | None, delta: float, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT mastery_score FROM mastery_records WHERE user_id = ? AND session_id = ? AND concept_name = ?",
                (user_id, session_id, concept_name),
            ).fetchone()
            current = float(row["mastery_score"]) if row else 0.5
            updated = min(1.0, max(0.0, current + delta))
            conn.execute(
                """
                INSERT INTO mastery_records (id, user_id, session_id, kg_node_id, concept_name, mastery_score, confidence, evidence_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, session_id, concept_name) DO UPDATE SET
                    mastery_score = excluded.mastery_score,
                    confidence = excluded.confidence,
                    evidence_json = excluded.evidence_json,
                    updated_at = excluded.updated_at
                """,
                (uuid.uuid4().hex, user_id, session_id, kg_node_id, concept_name, updated, 0.6, _json(evidence or {}), _now()),
            )
        return {"concept_name": concept_name, "mastery_score": updated}
```

- [ ] **Step 4: 实现 Orchestrator**

创建 `backend/services/learning/orchestrator.py`：

```python
from __future__ import annotations

from pathlib import Path
from typing import Any

from .assessment import assess_answer
from .memory import LearningMemory
from .planner import plan_lesson
from .qa import answer_interruption
from .resource_factory import generate_resources
from .tutor import build_tutor_step


class LearningOrchestrator:
    def __init__(self, db_path: str | Path | None = None, resource_root: str | Path | None = None) -> None:
        self.memory = LearningMemory(db_path) if db_path else LearningMemory()
        self.resource_root = Path(resource_root) if resource_root else None

    def start_session(self, user_id: str, topic: str, profile_answers: dict[str, Any] | None = None) -> dict[str, Any]:
        profile = self.memory.get_or_create_profile(user_id)
        session = self.memory.create_session(user_id=user_id, topic=topic)
        plan = plan_lesson(topic=topic, user_profile=profile)
        self.memory.save_outline(session["id"], plan["outline"], plan["mindmap"], plan["source_trace"])
        first_node_id = plan["outline"]["nodes"][0]["id"] if plan["outline"]["nodes"] else None
        self.memory.set_current_outline_node(session["id"], first_node_id)
        resources = generate_resources(
            session_id=session["id"],
            topic=topic,
            outline=plan["outline"],
            mindmap=plan["mindmap"],
            output_root=self.resource_root if self.resource_root else None,
        )
        for item in resources:
            self.memory.save_resource(session["id"], item["resource_type"], item.get("resource_path"), item.get("resource_json") or {}, plan["source_trace"])
        self.memory.record_event(session["id"], user_id, "lesson_started", first_node_id, {"topic": topic})
        tutor_step = build_tutor_step(plan["outline"], first_node_id, mastery={})
        self.memory.record_event(session["id"], user_id, "tutor_step_presented", first_node_id, tutor_step)
        return {
            "ok": True,
            "session": self.memory.get_session(session["id"]),
            "profile": profile,
            "outline": plan["outline"],
            "resources": self.memory.list_resources(session["id"]),
            "tutor_step": tutor_step,
        }

    def ask(self, session_id: str, question: str) -> dict[str, Any]:
        session = self.memory.get_session(session_id)
        outline_pack = self.memory.latest_outline(session_id) or {"outline": {"nodes": []}}
        current_node_id = session["current_outline_node_id"]
        self.memory.record_event(session_id, session["user_id"], "user_interruption_question", current_node_id, {"question": question})
        qa = answer_interruption(question, outline_pack["outline"], current_node_id, user_profile={})
        self.memory.record_event(session_id, session["user_id"], "qa_answered", current_node_id, qa)
        return {"ok": True, "session": self.memory.get_session(session_id), "qa": qa}

    def answer(self, session_id: str, question: dict[str, Any], user_answer: str) -> dict[str, Any]:
        session = self.memory.get_session(session_id)
        grading = assess_answer(question, user_answer)
        self.memory.save_answer(session_id, session["user_id"], grading["question_id"], question.get("outline_node_id"), user_answer, grading)
        concept_name = question.get("outline_node_id") or question.get("id") or "current_concept"
        mastery = self.memory.update_mastery(
            user_id=session["user_id"],
            session_id=session_id,
            concept_name=concept_name,
            kg_node_id=(question.get("kg_node_ids") or [None])[0],
            delta=float(grading.get("mastery_delta") or 0),
            evidence={"question_id": question.get("id"), "grading": grading},
        )
        self.memory.record_event(session_id, session["user_id"], "assessment_completed", question.get("outline_node_id"), grading)
        return {"ok": True, "assessment": grading, "mastery": mastery, "session": self.memory.get_session(session_id)}

    def next(self, session_id: str) -> dict[str, Any]:
        session = self.memory.get_session(session_id)
        outline_pack = self.memory.latest_outline(session_id)
        if not outline_pack:
            raise KeyError(f"outline not found for session: {session_id}")
        nodes = outline_pack["outline"].get("nodes", [])
        current = session["current_outline_node_id"]
        current_index = next((i for i, node in enumerate(nodes) if node.get("id") == current), 0)
        next_index = min(current_index + 1, len(nodes) - 1) if nodes else 0
        next_node_id = nodes[next_index]["id"] if nodes else current
        self.memory.set_current_outline_node(session_id, next_node_id)
        tutor_step = build_tutor_step(outline_pack["outline"], next_node_id, mastery={})
        self.memory.record_event(session_id, session["user_id"], "tutor_step_presented", next_node_id, tutor_step)
        return {"ok": True, "session": self.memory.get_session(session_id), "tutor_step": tutor_step}

    def resources(self, session_id: str) -> dict[str, Any]:
        return {"ok": True, "resources": self.memory.list_resources(session_id)}

    def summary(self, session_id: str) -> dict[str, Any]:
        return self.memory.session_summary(session_id)
```

- [ ] **Step 5: 修正 Resource Factory 对 `output_root=None` 的处理**

在 `generate_resources` 开头使用：

```python
    root = Path(output_root) if output_root is not None else DEFAULT_OUTPUT_ROOT
    session_dir = root / session_id
```

替换原来的：

```python
    session_dir = Path(output_root) / session_id
```

- [ ] **Step 6: 运行 orchestrator 测试**

Run:

```bash
python -m pytest tests/test_learning_orchestrator.py -v
```

Expected: PASS。

- [ ] **Step 7: 提交**

```bash
git add backend/services/learning/orchestrator.py backend/services/learning/memory.py backend/services/learning/resource_factory.py tests/test_learning_orchestrator.py
git commit -m "feat: orchestrate learning session flow"
```

---

## Task 7: FastAPI learning endpoints

**Files:**
- Modify: `backend/schemas.py`
- Modify: `backend/main.py`

- [ ] **Step 1: 在 schemas.py 增加请求模型**

在 `backend/schemas.py` 中追加：

```python
class LearningProfileDiagnoseRequest(BaseModel):
    user_id: str
    answers: dict[str, Any] = Field(default_factory=dict)


class LearningSessionStartRequest(BaseModel):
    user_id: str
    topic: str
    profile_mode: str = "use_existing_or_default"
    profile_answers: dict[str, Any] = Field(default_factory=dict)


class LearningAskRequest(BaseModel):
    question: str


class LearningAnswerRequest(BaseModel):
    question: dict[str, Any]
    answer: str


class LearningNextRequest(BaseModel):
    action: str = "continue"
```

- [ ] **Step 2: 在 main.py 导入模型和 Orchestrator**

在 `.schemas import` 列表中加入：

```python
    LearningAnswerRequest,
    LearningAskRequest,
    LearningNextRequest,
    LearningProfileDiagnoseRequest,
    LearningSessionStartRequest,
```

在 service imports 加入：

```python
from .services.learning.orchestrator import LearningOrchestrator
from .services.learning.user_model import diagnose_profile
```

在 app 创建后加入：

```python
learning_orchestrator = LearningOrchestrator()
```

- [ ] **Step 3: 在 capabilities 加入 learning 模块和 endpoints**

在 `implemented_modules` 加入：

```python
            "learning_agent_mvp",
```

在 `endpoints` 加入：

```python
            "/api/learning/profile/diagnose",
            "/api/learning/session/start",
            "/api/learning/session/{session_id}/next",
            "/api/learning/session/{session_id}/ask",
            "/api/learning/session/{session_id}/answer",
            "/api/learning/session/{session_id}/summary",
            "/api/learning/session/{session_id}/resources",
```

- [ ] **Step 4: 增加 learning API**

在 `/api/router/chat` 附近加入：

```python
@app.post("/api/learning/profile/diagnose")
async def learning_profile_diagnose(req: LearningProfileDiagnoseRequest) -> JSONResponse:
    profile = diagnose_profile(req.answers)
    learning_orchestrator.memory.save_profile(req.user_id, profile)
    return JSONResponse({"ok": True, "profile": profile})


@app.post("/api/learning/session/start")
async def learning_session_start(req: LearningSessionStartRequest) -> JSONResponse:
    result = learning_orchestrator.start_session(req.user_id, req.topic, req.profile_answers)
    return JSONResponse(result)


@app.post("/api/learning/session/{session_id}/next")
async def learning_session_next(session_id: str, req: LearningNextRequest) -> JSONResponse:
    result = learning_orchestrator.next(session_id)
    return JSONResponse(result)


@app.post("/api/learning/session/{session_id}/ask")
async def learning_session_ask(session_id: str, req: LearningAskRequest) -> JSONResponse:
    result = learning_orchestrator.ask(session_id, req.question)
    return JSONResponse(result)


@app.post("/api/learning/session/{session_id}/answer")
async def learning_session_answer(session_id: str, req: LearningAnswerRequest) -> JSONResponse:
    result = learning_orchestrator.answer(session_id, req.question, req.answer)
    return JSONResponse(result)


@app.get("/api/learning/session/{session_id}/summary")
async def learning_session_summary(session_id: str) -> JSONResponse:
    return JSONResponse({"ok": True, "summary": learning_orchestrator.summary(session_id)})


@app.get("/api/learning/session/{session_id}/resources")
async def learning_session_resources(session_id: str) -> JSONResponse:
    return JSONResponse(learning_orchestrator.resources(session_id))
```

- [ ] **Step 5: 运行导入检查**

Run:

```bash
python -m compileall backend
```

Expected: PASS，无 syntax error。

- [ ] **Step 6: 提交**

```bash
git add backend/schemas.py backend/main.py
git commit -m "feat: expose learning agent api"
```

---

## Task 8: CLI Demo 与 Smoke Test

**Files:**
- Create: `scripts/learning_demo.py`
- Create: `scripts/smoke_test_learning.py`

- [ ] **Step 1: 创建 smoke test**

创建 `scripts/smoke_test_learning.py`：

```python
from __future__ import annotations

from pathlib import Path
import shutil
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.services.learning.orchestrator import LearningOrchestrator


def main() -> None:
    root = PROJECT_ROOT / "data" / "learning_smoke"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)

    orchestrator = LearningOrchestrator(db_path=root / "learning.sqlite3", resource_root=root / "sessions")
    started = orchestrator.start_session(user_id="demo", topic="牙周炎")
    session_id = started["session"]["id"]
    current = started["session"]["current_outline_node_id"]

    asked = orchestrator.ask(session_id, "为什么牙周炎会导致牙槽骨吸收？")
    assert asked["session"]["current_outline_node_id"] == current

    question = started["tutor_step"]["question"]
    answered = orchestrator.answer(session_id, question, "A")
    assert answered["assessment"]["is_correct"] is False

    next_step = orchestrator.next(session_id)
    assert next_step["tutor_step"]["outline_node_id"]

    resources = orchestrator.resources(session_id)["resources"]
    assert any(item["resource_type"] == "handout_md" for item in resources)

    summary = orchestrator.summary(session_id)
    assert summary["events"]
    print("learning smoke test passed")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 创建 CLI Demo**

创建 `scripts/learning_demo.py`：

```python
from __future__ import annotations

from pathlib import Path
import argparse
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.services.learning.orchestrator import LearningOrchestrator


def print_step(step: dict) -> None:
    print("\n[讲课]")
    print(step.get("teaching_text", ""))
    question = step.get("question")
    if question:
        print("\n[小测]")
        print(question.get("stem", ""))
        for option in question.get("options", []):
            print(option)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", default="demo")
    parser.add_argument("--topic", default="牙周炎")
    args = parser.parse_args()

    orchestrator = LearningOrchestrator()
    active_session_id: str | None = None
    active_question: dict | None = None

    print("OralSage 学习 Agent CLI")
    print("命令：/start /next /ask 问题 /answer A /resources /summary /quit")

    while True:
        raw = input("\n> ").strip()
        if raw == "/quit":
            break
        if raw == "/start":
            result = orchestrator.start_session(args.user, args.topic)
            active_session_id = result["session"]["id"]
            active_question = result["tutor_step"].get("question")
            print(f"已开始课程：{args.topic}，session_id={active_session_id}")
            print_step(result["tutor_step"])
            continue
        if raw == "/next":
            if not active_session_id:
                print("请先运行 /start")
                continue
            result = orchestrator.next(active_session_id)
            active_question = result["tutor_step"].get("question")
            print_step(result["tutor_step"])
            continue
        if raw.startswith("/ask "):
            if not active_session_id:
                print("请先运行 /start")
                continue
            result = orchestrator.ask(active_session_id, raw[len("/ask "):].strip())
            print("\n[答疑]")
            print(result["qa"]["answer"])
            print("课程进度未推进。")
            continue
        if raw.startswith("/answer "):
            if not active_session_id or not active_question:
                print("当前没有可回答的问题。")
                continue
            result = orchestrator.answer(active_session_id, active_question, raw[len("/answer "):].strip())
            print("\n[评估]")
            print(result["assessment"])
            continue
        if raw == "/resources":
            if not active_session_id:
                print("请先运行 /start")
                continue
            result = orchestrator.resources(active_session_id)
            for item in result["resources"]:
                print(f"{item['resource_type']}: {item.get('resource_path')}")
            continue
        if raw == "/summary":
            if not active_session_id:
                print("请先运行 /start")
                continue
            result = orchestrator.summary(active_session_id)
            print(result)
            continue
        print("未知命令。可用命令：/start /next /ask 问题 /answer A /resources /summary /quit")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: 运行 smoke test**

Run:

```bash
python scripts/smoke_test_learning.py
```

Expected:

```text
learning smoke test passed
```

- [ ] **Step 4: 手动跑 CLI 一轮**

Run:

```bash
python scripts/learning_demo.py --user demo --topic 牙周炎
```

输入：

```text
/start
/ask 为什么会牙槽骨吸收？
/answer A
/next
/resources
/summary
/quit
```

Expected:

- `/ask` 后输出“课程进度未推进”。
- `/answer A` 后输出错误评估。
- `/resources` 能看到 `handout.md`、`slides.json`、`base_questions.json` 等路径。

- [ ] **Step 5: 提交**

```bash
git add scripts/learning_demo.py scripts/smoke_test_learning.py
git commit -m "feat: add learning agent cli demo"
```

---

## Task 9: 可选 PPTX 导出器

**Files:**
- Modify: `backend/services/learning/resource_factory.py`
- Modify: `backend/requirements.txt`

说明：MVP 核心链路不依赖 PPTX 成功。若当前环境没有 Node/PptxGenJS，可先跳过本任务，保留 `slides.json` 作为 PPT 中间格式。跳过时需要在最终说明中明确“PPTX 导出器未启用，但 slides JSON 已生成”。

- [ ] **Step 1: 检查 Node 是否可用**

Run:

```bash
node --version
```

Expected: 输出 Node 版本。

- [ ] **Step 2: 初始化最小 Node 依赖**

如果仓库没有 `package.json`，运行：

```bash
npm init -y
npm install pptxgenjs
```

Expected: 生成 `package.json`、`package-lock.json`，安装 `pptxgenjs`。

- [ ] **Step 3: 添加 PPTX 失败不阻塞的导出函数**

在 `resource_factory.py` 中加入：

```python
def try_export_pptx(slides_path: Path, pptx_path: Path) -> dict[str, Any]:
    return {
        "ok": False,
        "resource_type": "pptx",
        "resource_path": str(pptx_path),
        "message": "PPTX exporter is reserved for PptxGenJS integration; slides.json is available.",
        "slides_path": str(slides_path),
    }
```

并在 `generate_resources` 中追加：

```python
    pptx_path = session_dir / "course.pptx"
    pptx_result = try_export_pptx(slides_path, pptx_path)
```

返回列表追加：

```python
        {"resource_type": "pptx", "resource_path": str(pptx_path), "resource_json": pptx_result},
```

- [ ] **Step 4: 更新 Resource Factory 测试**

在 `tests/test_learning_resource_factory.py` 中加入断言：

```python
    assert "pptx" in resource_types
```

- [ ] **Step 5: 运行测试**

Run:

```bash
python -m pytest tests/test_learning_resource_factory.py -v
python scripts/smoke_test_learning.py
```

Expected: PASS，smoke test 通过。

- [ ] **Step 6: 提交**

```bash
git add backend/services/learning/resource_factory.py tests/test_learning_resource_factory.py package.json package-lock.json
git commit -m "feat: add learning pptx export placeholder"
```

---

## Task 10: 最终验证与文档收口

**Files:**
- Modify: `docs/superpowers/specs/2026-07-03-learning-agent-architecture-design.md`
- Modify: `docs/superpowers/plans/2026-07-03-learning-agent-mvp-implementation.md`

- [ ] **Step 1: 运行全部学习测试**

Run:

```bash
python -m pytest tests/test_learning_memory.py tests/test_learning_orchestrator.py tests/test_learning_resource_factory.py -v
python scripts/smoke_test_learning.py
python -m compileall backend scripts
```

Expected:

- 所有 pytest 通过。
- smoke test 输出 `learning smoke test passed`。
- compileall 无 syntax error。

- [ ] **Step 2: 检查工作区变更**

Run:

```bash
git status --short
```

Expected: 只包含本计划涉及的 learning、tests、scripts、API schema/main 和可选 package 文件。

- [ ] **Step 3: 人工验收 CLI**

Run:

```bash
python scripts/learning_demo.py --user demo --topic 牙周炎
```

输入：

```text
/start
/ask 为什么牙周炎会导致牙槽骨吸收？
/answer A
/next
/resources
/summary
/quit
```

Expected:

- 能开始课程。
- 能生成资源路径。
- 中途答疑不会推进当前节点。
- 答题会产生 assessment。
- summary 中能看到事件流和 mastery。

- [ ] **Step 4: 更新文档状态**

在本计划文档末尾追加：

```markdown
## 实施结果

- 后端 learning service 已实现。
- CLI smoke test 已通过。
- 中途答疑不推进课程进度。
- 资源生成包含 handout、slides、base questions、reading list、mindmap，以及 PPTX 占位或导出结果。
```

- [ ] **Step 5: 提交最终收口**

```bash
git add docs/superpowers/plans/2026-07-03-learning-agent-mvp-implementation.md docs/superpowers/specs/2026-07-03-learning-agent-architecture-design.md
git commit -m "docs: update learning agent implementation status"
```

## Self-Review

Spec coverage:

- Agent 边界：Task 3、Task 5、Task 6 覆盖。
- SQLite 学习记忆：Task 2、Task 6 覆盖。
- Resource Factory：Task 4、Task 9 覆盖。
- 打断答疑不推进进度：Task 2、Task 5、Task 6、Task 8 覆盖。
- Assessment Engine：Task 5、Task 6 覆盖。
- API/CLI：Task 7、Task 8 覆盖。
- 后端/终端 MVP：Task 8、Task 10 覆盖。

Placeholder scan:

- 本计划没有使用 TBD、TODO 或“稍后实现”作为任务内容。
- PPTX 作为可选任务有明确跳过条件和保底输出，不影响 MVP 主链路。

Type consistency:

- `LearningOrchestrator.start_session/ask/answer/next/resources/summary` 在 API、CLI、smoke test 中保持一致。
- `LearningMemory` 的 session、outline、resource、event、answer、mastery 方法在 orchestrator 中一致调用。
- `TutorStep`、`AssessmentResult` 字段与测试和 CLI 使用保持一致。

