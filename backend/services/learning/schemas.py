"""Learning domain schemas for profiles, outlines, and lesson data."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


DEFAULT_PROFILE: dict[str, Any] = {
    "level": "unknown",
    "goal": "course_learning",
    "preferred_explanation": "concept_then_case",
    "pace": "standard",
    "daily_available_minutes": 45,
    "assessment_preference": "moderate",
    "planning_support_need": "normal",
    "learner_type": "",
    "learner_type_summary": "",
    "known_weak_points": [],
    "mistake_patterns": [],
    "mastery_by_kg_node": {},
    "plan_adherence": 0.0,
    "review_need": "normal",
    "target_exam": None,
    "assessment_skipped": False,
    "last_assessed_at": None,
    "assessment_source": None,
    "topic_interests": [],
    "confidence": 0.2,
}


class LearningProfile(BaseModel):
    """User learning profile / persona."""

    level: str = Field(default="unknown", description="Current learning level")
    goal: str = Field(default="course_learning", description="Current learning goal")
    preferred_explanation: str = Field(
        default="concept_then_case",
        description="Preferred teaching style",
    )
    pace: str = Field(default="standard", description="Preferred learning pace")
    daily_available_minutes: int = Field(
        default=45,
        ge=10,
        le=240,
        description="Daily study time budget in minutes",
    )
    assessment_preference: str = Field(
        default="moderate",
        description="Tolerance for section / daily quiz frequency",
    )
    planning_support_need: str = Field(
        default="normal",
        description="Need for explicit planning support",
    )
    learner_type: str = Field(default="", description="Frontend display learner type label")
    learner_type_summary: str = Field(
        default="",
        description="Human-readable explanation for learner_type",
    )
    known_weak_points: list[str] = Field(
        default_factory=list,
        description="Known weak concepts or nodes",
    )
    mistake_patterns: list[str] = Field(
        default_factory=list,
        description="Recurring mistake patterns",
    )
    mastery_by_kg_node: dict[str, Any] = Field(
        default_factory=dict,
        description="Current mastery summary keyed by KG node id",
    )
    plan_adherence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Study-plan adherence summary",
    )
    review_need: str = Field(default="normal", description="Current review intensity need")
    target_exam: str | None = Field(default=None, description="Target exam, if any")
    assessment_skipped: bool = Field(
        default=False,
        description="Whether the initial learner assessment was skipped",
    )
    last_assessed_at: str | None = Field(
        default=None,
        description="ISO timestamp of the latest learner assessment",
    )
    assessment_source: str | None = Field(
        default=None,
        description="Assessment instrument name/version used most recently",
    )
    topic_interests: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Low-confidence topics inferred from standalone resource requests",
    )
    confidence: float = Field(
        default=0.2,
        ge=0.0,
        le=1.0,
        description="Profile confidence",
    )


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
    sequence_reason: str = Field(default="")
    kg_edge_ids: list[str] = Field(default_factory=list)
    concept_ids: list[str] = Field(default_factory=list)
    evidence_chunk_ids: list[str] = Field(default_factory=list)
    question_ids: list[str] = Field(default_factory=list)


class LessonOutline(BaseModel):
    """Full lesson outline composed of ordered nodes."""

    title: str
    nodes: list[OutlineNode]
