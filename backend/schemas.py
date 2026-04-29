from __future__ import annotations

from typing import Any
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
    context: dict[str, Any] = Field(default_factory=dict)


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


class EvidenceSearchRequest(BaseModel):
    query: str
    top_k: int = 10
    allow_web: bool = False


class ImageRequest(BaseModel):
    query: str | None = None
    image_base64: str
    allow_web: bool = True
