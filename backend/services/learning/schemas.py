"""Learning domain schemas — data structures for user profiles, outlines, and lessons."""

from typing import Any

from pydantic import BaseModel, Field

DEFAULT_PROFILE: dict[str, Any] = {
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
    """User learning profile / persona."""

    level: str = Field(default="unknown", description="当前学习水平")
    goal: str = Field(default="course_learning", description="学习目标")
    preferred_explanation: str = Field(
        default="concept_then_case",
        description="偏好的讲解方式",
    )
    pace: str = Field(default="standard", description="学习节奏")
    known_weak_points: list[str] = Field(
        default_factory=list, description="已知薄弱点"
    )
    mistake_patterns: list[str] = Field(
        default_factory=list, description="错题类型模式"
    )
    target_exam: str | None = Field(default=None, description="目标考试")
    confidence: float = Field(default=0.2, ge=0.0, le=1.0, description="画像置信度")


class OutlineNode(BaseModel):
    """A single node / step in a lesson outline."""

    id: str
    title: str
    kg_node_ids: list[str] = Field(default_factory=list)
    learning_objectives: list[str] = Field(default_factory=list)
    prerequisites: list[str] = Field(default_factory=list)
    estimated_minutes: int = Field(default=10, ge=1)
    difficulty: str = Field(default="basic")
    source_refs: list[dict[str, Any]] = Field(default_factory=list)
    # Agent Planner evidence index fields (id-level references only)
    sequence_reason: str = Field(default="")
    kg_edge_ids: list[str] = Field(default_factory=list)
    concept_ids: list[str] = Field(default_factory=list)
    evidence_chunk_ids: list[str] = Field(default_factory=list)
    question_ids: list[str] = Field(default_factory=list)


class LessonOutline(BaseModel):
    """Full lesson outline composed of ordered nodes."""

    title: str
    nodes: list[OutlineNode]
