from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


RouteStatus = Literal[
    "exact",
    "resolved_by_llm",
    "skipped_no_kg_subject",
    "chapter_unresolved",
    "chapter_ambiguous",
    "llm_error",
]

LinkStatus = Literal[
    "matched",
    "core_ambiguous",
    "no_core_match",
    "review_required",
    "skipped_no_kg_subject",
    "chapter_unresolved",
    "chapter_ambiguous",
    "llm_error",
]

MatchType = Literal["direct_core", "support_to_core", "concept_to_core", "mixed"]
ParentLinkStrength = Literal["direct", "edge", "strong_related", "weak_related", "invalid"]


class KGSupportNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_ref: str
    type: str
    canonical_name: str
    aliases: list[str] = Field(default_factory=list)


class KGConceptNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_ref: str
    canonical_name: str
    relation: str | None = None
    related_node_refs: list[str] = Field(default_factory=list)
    reason_not_edge: str | None = None


class KGEdgeEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    span_id: str | None = None
    exact_quote: str | None = None
    support_explanation: str | None = None


class KGEdgeNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    edge_ref: str
    subject_ref: str
    predicate: str
    object_ref: str
    subject_name: str | None = None
    object_name: str | None = None
    evidence: list[KGEdgeEvidence] = Field(default_factory=list)


class KGCoreNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kg_core_uid: str
    kg_subject_name: str
    draft_id: str
    draft_file: str
    book_title: str | None = None
    chapter_title: str
    section_title: str | None = None
    client_ref: str
    type: str
    canonical_name: str
    aliases: list[str] = Field(default_factory=list)
    source_span_ids: list[str] = Field(default_factory=list)
    support_nodes: list[KGSupportNode] = Field(default_factory=list)
    concept_nodes: list[KGConceptNode] = Field(default_factory=list)
    edges: list[KGEdgeNode] = Field(default_factory=list)


class ChapterCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kg_subject_name: str
    kg_chapter_title: str
    draft_id: str
    draft_file: str
    core_count: int


class ChapterRouteDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["resolved", "ambiguous", "no_match"]
    kg_chapter_title: str | None = None
    draft_id: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str
    considered_draft_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_resolved_target(self) -> "ChapterRouteDecision":
        if self.decision == "resolved" and (not self.kg_chapter_title or not self.draft_id):
            raise ValueError("resolved chapter decision requires kg_chapter_title and draft_id")
        return self


class BridgeNodeDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_kind: Literal["core_node", "support_node", "concept_node"]
    client_ref: str
    canonical_name: str
    reason: str


class MatchedKnowledgeNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_kind: Literal["core_node", "support_node", "concept_node"]
    client_ref: str
    canonical_name: str
    parent_core_uid: str
    parent_core_name: str
    parent_core_type: str
    parent_link_strength: ParentLinkStrength
    parent_link_reason: str


class CoreMatchLLMDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["match", "ambiguous", "no_match"]
    primary_core_uid: str | None = None
    matched_core_uids: list[str] = Field(default_factory=list)
    match_type: MatchType | None = None
    matched_node: MatchedKnowledgeNode | None = None
    bridge_nodes: list[BridgeNodeDecision] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    question_evidence_terms: list[str] = Field(default_factory=list)
    kg_evidence_terms: list[str] = Field(default_factory=list)
    rejected_candidates: list[dict[str, str]] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_match_fields(self) -> "CoreMatchLLMDecision":
        if self.decision == "match":
            if not self.primary_core_uid:
                raise ValueError("match decision requires primary_core_uid")
            if not self.match_type:
                raise ValueError("match decision requires match_type")
            if self.matched_node is None:
                raise ValueError("match decision requires matched_node")
        return self


class CoreVerifierDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["accept", "reject", "uncertain"]
    reasons: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_reason(self) -> "CoreVerifierDecision":
        if self.decision in {"reject", "uncertain"} and not self.reasons:
            raise ValueError("reject/uncertain verifier decisions require reasons")
        return self


class RouteInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_status: Literal["exact_subject", "missing_kg_subject"]
    chapter_status: RouteStatus
    source_subject_name: str
    source_chapter_name: str
    kg_subject_name: str | None = None
    kg_chapter_title: str | None = None
    draft_id: str | None = None
    draft_file: str | None = None
    reason: str
    confidence: float | None = None


class KGCoreLinking(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: LinkStatus
    primary_core_uid: str | None = None
    primary_core_name: str | None = None
    primary_core_type: str | None = None
    kg_subject_name: str | None = None
    kg_chapter_title: str | None = None
    kg_section_title: str | None = None
    match_type: MatchType | None = None
    confidence: float | None = None
    route: RouteInfo
    bridge_nodes: list[BridgeNodeDecision] = Field(default_factory=list)
    matched_knowledge_node: MatchedKnowledgeNode | None = None
    matched_core_uids: list[str] = Field(default_factory=list)
    rationale: str
    verifier: CoreVerifierDecision | None = None
    question_evidence_terms: list[str] = Field(default_factory=list)
    kg_evidence_terms: list[str] = Field(default_factory=list)
    rejected_candidates: list[dict[str, str]] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class LinkedQuestionBank(BaseModel):
    model_config = ConfigDict(extra="allow")

    source: str | None = None
    generated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    total_questions: int
    records: list[dict[str, Any]]


class LinkerReviewItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_id: str
    subject_name: str
    chapter_name: str
    status: LinkStatus
    reason: str
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
