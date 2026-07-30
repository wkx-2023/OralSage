from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


class GenericAgentRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    query: str | None = None
    question: str | None = None
    message: str | None = None
    task: str | None = None
    conversation: str | None = None
    text: str | None = None
    case_text: str | None = None
    image_base64: str | None = None
    months: int | None = None
    limit: int | None = None
    top_k: int | None = None
    allow_web: bool | None = None
    fields: dict[str, Any] | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    data: dict[str, Any] = Field(default_factory=dict)


class GenericAgentResponse(BaseModel):
    agent: str
    ok: bool = True
    result: dict[str, Any]


class RouterChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    message: str = ""
    image_base64: str | None = None
    history: list[dict[str, Any]] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=lambda: {"user_id": "demo"})


class TermsRequest(BaseModel):
    task: str


class LiteratureRequest(BaseModel):
    query: str
    months: int | None = None
    limit: int = 5


class LiteratureTranslateRequest(BaseModel):
    article: dict[str, Any]


class StatsRequest(BaseModel):
    query: str
    top_k: int = 10


class StatsFilterRequest(BaseModel):
    months: list[str] = Field(default_factory=list)
    disease_groups: list[str] = Field(default_factory=list)
    diagnosis_names: list[str] = Field(default_factory=list)
    operation_names: list[str] = Field(default_factory=list)
    departments: list[str] = Field(default_factory=list)
    register_types: list[str] = Field(default_factory=list)
    view: str = "dashboard"


class AsrRequest(BaseModel):
    conversation: str


class ExtractRequest(BaseModel):
    conversation: str


class ReportRequest(BaseModel):
    fields: dict[str, Any] | None = None
    conversation: str | None = None


class WorkflowRequest(BaseModel):
    conversation: str


class PathologyRequest(BaseModel):
    question: str
    top_k: int = 8
    allow_web: bool = True


class DecisionRequest(BaseModel):
    case_text: str
    top_k: int = 8
    allow_web: bool = True


class ClinicalSpeakingStartRequest(BaseModel):
    user_id: str = "demo"
    request_id: str = Field(min_length=8, max_length=128)


class ClinicalSpeakingTurnRequest(BaseModel):
    user_id: str = "demo"
    transcript: str = Field(min_length=1, max_length=4000)
    request_id: str = Field(min_length=8, max_length=128)
    input_mode: Literal["typed", "speech"] = "typed"


class ClinicalSpeakingDiagnosisConfirmRequest(BaseModel):
    user_id: str = "demo"
    confirmed: bool
    request_id: str = Field(min_length=8, max_length=128)


class ClinicalSpeakingExamAnswerRequest(BaseModel):
    user_id: str = "demo"
    transcript: str = Field(min_length=1, max_length=6000)
    request_id: str = Field(min_length=8, max_length=128)
    input_mode: Literal["typed", "speech"] = "typed"


class EvidenceSearchRequest(BaseModel):
    query: str
    top_k: int = 10
    allow_web: bool = False


class KnowledgeSearchRequest(BaseModel):
    query: str
    top_k: int = 24


class KnowledgeViewRequest(BaseModel):
    topic_id: str = "impacted_wisdom_tooth"
    dimension_id: str = "all"
    source_type: str = "all"
    top_k: int = 4


class KnowledgeGraphNeighborhoodRequest(BaseModel):
    node_id: str
    depth: int = 1
    limit: int = 160


class ImageRequest(BaseModel):
    query: str | None = None
    image_base64: str
    allow_web: bool = True


# ============================================================================
# Learning Orchestrator API schemas
# ============================================================================


class LearningStartRequest(BaseModel):
    user_id: str = "demo"
    topic: str
    display_name: str | None = None
    profile_input: dict[str, Any] = Field(default_factory=dict)


class LearningRouteRequest(BaseModel):
    session_id: str
    user_input: dict[str, Any] | None = None
    use_llm_policy: bool = False


class LearningExecuteRequest(BaseModel):
    session_id: str
    decision: dict[str, Any]
    user_input: dict[str, Any] | None = None


class LearningStepRequest(BaseModel):
    session_id: str
    use_llm_policy: bool = False


class LearningInterruptionRequest(BaseModel):
    session_id: str
    question: str
    use_llm_policy: bool = False


class LearningContinueRequest(BaseModel):
    session_id: str
    use_llm_policy: bool = False


class LearningSubmitAnswerRequest(BaseModel):
    session_id: str
    question: dict[str, Any]
    answer: str
    use_llm_policy: bool = False


class StageExamGenerateRequest(BaseModel):
    force: bool = False
    questions_per_section: int = 1


class StageExamAnswer(BaseModel):
    question_id: str
    selected_option: str


class StageExamSubmitRequest(BaseModel):
    answers: list[StageExamAnswer] = Field(default_factory=list)


class LearningProfileSliceRequest(BaseModel):
    user_id: str
    role: str = "qa"
    context: dict[str, Any] = Field(default_factory=dict)
    limit: int = 6


class LearningMemoryConsolidateRequest(BaseModel):
    user_id: str
    session_id: str | None = None
    surface: str | None = None
    surfaces: list[str] | None = None
    slot: str | None = None
    layer: str = "all"
    limit: int = 200


class LearnerAssessmentAnswer(BaseModel):
    question_id: str
    selected_option: str


class LearnerAssessmentSubmitRequest(BaseModel):
    user_id: str = "demo"
    instrument_name: str = "learning_onboarding_v1"
    instrument_version: str = "v1"
    answers: list[LearnerAssessmentAnswer] = Field(default_factory=list)
    target_exam: str | None = None
    skip_assessment: bool = False


class LearningPlanCreateRequest(BaseModel):
    user_id: str = "demo"
    plan_date: str
    title: str
    notes: str | None = None
    linked_course_id: str | None = None
    linked_course_title: str | None = None
    task_type: str = "topic_learning"
    learning_target: str | None = None
    kg_node_ids: list[str] = Field(default_factory=list)
    learning_scope: dict[str, Any] | None = None
    dependency: str | None = None
    practice_level: str | None = None
    session_id: str | None = None


class LearningPlanUpdateRequest(BaseModel):
    user_id: str = "demo"
    plan_date: str | None = None
    title: str | None = None
    notes: str | None = None
    linked_course_id: str | None = None
    linked_course_title: str | None = None
    task_type: str | None = None
    learning_target: str | None = None
    kg_node_ids: list[str] | None = None
    learning_scope: dict[str, Any] | None = None


class LearningPlanCompleteRequest(BaseModel):
    user_id: str = "demo"
    completed: bool = True


class LearningPlanDeferRequest(BaseModel):
    user_id: str = "demo"
    to_date: str | None = None


class LearningPlanRecommendRequest(BaseModel):
    user_id: str = "demo"
    plan_date: str
    max_items: int = 4
    request_text: str | None = None
