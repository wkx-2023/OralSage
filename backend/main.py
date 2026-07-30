from __future__ import annotations

import asyncio
import os
from datetime import datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any
import base64
import json
import time
from uuid import uuid4
from urllib.error import HTTPError, URLError
from urllib.request import Request as UrlRequest, urlopen

from .services.env_loader import load_project_env

load_project_env()

from fastapi import FastAPI, File, Form, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .schemas import (
    AsrRequest,
    DecisionRequest,
    EvidenceSearchRequest,
    ExtractRequest,
    GenericAgentRequest,
    GenericAgentResponse,
    ImageRequest,
    KnowledgeGraphNeighborhoodRequest,
    KnowledgeSearchRequest,
    KnowledgeViewRequest,
    LearningContinueRequest,
    LearnerAssessmentSubmitRequest,
    LearningExecuteRequest,
    LearningMemoryConsolidateRequest,
    LearningPlanCompleteRequest,
    LearningPlanCreateRequest,
    LearningPlanDeferRequest,
    LearningPlanRecommendRequest,
    LearningPlanUpdateRequest,
    LearningProfileSliceRequest,
    LearningInterruptionRequest,
    LearningRouteRequest,
    LearningStartRequest,
    LearningStepRequest,
    LearningSubmitAnswerRequest,
    StageExamGenerateRequest,
    StageExamSubmitRequest,
    LiteratureRequest,
    LiteratureTranslateRequest,
    PathologyRequest,
    ReportRequest,
    RouterChatRequest,
    StatsFilterRequest,
    StatsRequest,
    TermsRequest,
    WorkflowRequest,
)
from .services.decision import run_decision_assist, run_evidence_search
from .services.image import run_image_analysis
from .services.image.agent import image_size_limit_bytes
from .services.knowledge import get_knowledge_topics, run_knowledge_view
from .services.knowledge_search import run_knowledge_search
from .services.knowledge_graph import (
    get_graph_neighborhood,
    get_graph_node,
    get_graph_overview,
    get_graph_summary,
)
from .services.literature import run_article_translation, run_literature_search, run_terms
from .services.mcp_tools import invoke_local_mcp_tool
from .services.pathology import run_pathology_ask
from .services.record_workflow import correct_asr, extract_fields, generate_record, run_workflow
from .services.stats.dashboard import run_stats_filter, stats_options
from .services.stats import run_stats_query
from .services.learning.orchestrator import LearningOrchestrator, decide_next_action
from .services.router.master_router import MasterRouter
from .services.learning.skills import LEARNING_AGENT_ALIASES
from .services.router.jobs import RouterJobManager
from .services.learning.audio import get_audio_manifest
from .services.learning.avatar_video import (
    XfyunVideoError,
    create_avatar_video,
    get_avatar_video_record,
    refresh_avatar_video,
)
from .services.avatar_service import get_public_avatar_config
from .services.learning.ppt_pipeline import ensure_pin_and_paper_previews
from .services.learning.llm_policy import decide_next_action_with_llm
from .services.learning.learner_assessment import (
    get_learner_assessment_definition,
    submit_learner_assessment,
)
from .services.learning.profile_memory import (
    build_profile_slice,
    consolidate_l2,
    consolidate_l3,
    refresh_profile_state_from_l3,
)
from .services.learning.study_plan import (
    complete_study_plan_item,
    create_study_plan_item,
    defer_study_plan_item,
    delete_study_plan_item,
    get_draft_from_default_store,
    get_study_plan_day,
    get_study_plan_month,
    recommend_study_plan,
    update_study_plan_item,
)
from .services.learning.student_twin import build_student_twin_simulation
from .services.learning.stage_exam import (
    generate_stage_exam,
    public_stage_exam,
    submit_stage_exam,
)
from .services.clawbridge_client import ClawBridgeClient, ClawBridgeError
from .services.weixin_auth import WeixinAuthError, WeixinAuthStore, session_cookie_name
from .services.weixin_plan_bot import build_daily_plan_text, build_daily_summary_text, handle_weixin_plan_message


learning_orchestrator = LearningOrchestrator()
master_router = MasterRouter(learning_orchestrator)
router_job_manager = RouterJobManager(master_router, learning_orchestrator)
weixin_auth_store = WeixinAuthStore()

app = FastAPI(title="OralPath API Interfaces", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _sse(event: str, data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


def _stream_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }


def _elapsed(started_at: float) -> float:
    return round(time.perf_counter() - started_at, 3)


SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def _env_enabled(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _parse_push_time(name: str, default_value: str) -> datetime_time:
    raw = os.getenv(name, default_value).strip() or default_value
    try:
        hour_text, minute_text = raw.split(":", 1)
        return datetime_time(hour=max(0, min(23, int(hour_text))), minute=max(0, min(59, int(minute_text))))
    except (TypeError, ValueError):
        return datetime_time.fromisoformat(default_value)


def _seconds_until_next(target_time: datetime_time, *, now: datetime | None = None) -> float:
    current = now or datetime.now(SHANGHAI_TZ)
    target = datetime.combine(current.date(), target_time, tzinfo=SHANGHAI_TZ)
    if target <= current:
        target += timedelta(days=1)
    return max(1.0, (target - current).total_seconds())


def _progress_event(
    *,
    action: str,
    phase: str,
    message: str,
    started_at: float,
    tick: int,
) -> str:
    return _sse(
        "progress",
        {
            "ok": True,
            "module": "learning",
            "action": action,
            "phase": phase,
            "message": message,
            "elapsed_seconds": _elapsed(started_at),
            "tick": tick,
        },
    )

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = PROJECT_ROOT / "frontend"
INTERFACE_REMOVED_MESSAGE = "Interface retained; implementation removed for rebuild."
LEARNING_ROUTER_REBUILD_MESSAGE = "旧总控 agent 已停用；学习型总控 agent 待重建。"


def _stub(module: str, payload: dict[str, Any] | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=501,
        content={
            "ok": False,
            "module": module,
            "message": INTERFACE_REMOVED_MESSAGE,
            "payload": payload or {},
        },
    )


def _learning_router_stub(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "ok": True,
        "module": "learning_router_placeholder",
        "answer": LEARNING_ROUTER_REBUILD_MESSAGE,
        "message": LEARNING_ROUTER_REBUILD_MESSAGE,
        "route": {
            "mode": "placeholder",
            "intent": "learning_router_rebuild",
            "target_agent": None,
            "reason": "当前版本已将旧医疗总控从调用链中摘除，现有专业 agent 作为工具箱能力独立保留。",
        },
        "steps": [],
        "final_result": {},
        "payload": payload or {},
        "trace": {
            "route_source": "disabled",
            "selected_agent": None,
            "errors": [],
        },
    }


@app.get("/api/system/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "mode": "partial-rebuild",
        "message": "Record generation workflow is implemented locally; other modules may still be interface stubs.",
    }


@app.get("/api/avatar/config")
def avatar_config() -> JSONResponse:
    """Expose only browser-safe configuration for the real-time avatar SDK."""
    return JSONResponse({"ok": True, "module": "avatar", "result": get_public_avatar_config()})


@app.get("/api/system/capabilities")
def capabilities() -> dict[str, Any]:
    return {
        "ok": True,
        "mode": "partial-rebuild",
        "implemented_modules": [
            "asr_correction_text",
            "record_extraction",
            "record_generation",
            "record_workflow_text",
            "decision_assistant",
            "evidence_search",
            "knowledge_view",
            "course_knowledge_graph",
            "frontier_literature",
            "clinic_stats",
            "pathology_qa",
            "image_analysis",
            "learning_router_placeholder",
            "learning_agent_mvp",
            "learning_study_plan",
            "learning_student_twin",
            "learning_knowledge_tracing",
            "learning_learner_assessment",
            "learning_stage_exam",
        ],
        "modules": [
            "pathology_qa",
            "decision_assistant",
            "frontier_literature",
            "clinic_stats",
            "asr_correction",
            "record_extraction",
            "record_generation",
            "record_workflow",
            "image_analysis",
            "evidence_search",
        ],
        "endpoints": [
            "/api/pathology/ask",
            "/api/decision/assist",
            "/api/evidence/search",
            "/api/knowledge/topics",
            "/api/knowledge/view",
            "/api/knowledge-graph/summary",
            "/api/knowledge-graph/overview",
            "/api/knowledge-graph/node/{node_id}",
            "/api/knowledge-graph/neighborhood",
            "/api/literature/search",
            "/api/literature/terms",
            "/api/literature/translate",
            "/api/stats/query",
            "/api/records/asr-correct",
            "/api/records/extract",
            "/api/records/generate",
            "/api/records/workflow",
            "/api/records/transcribe-upload",
            "/api/records/audio-generate",
            "/api/records/extract-upload",
            "/api/records/workflow-upload",
            "/api/avatar/config",
            "/api/image/analyze",
            "/api/image/analyze-upload",
            "/api/learning/session/start",
            "/api/learning/session/start/stream",
            "/api/learning/session/{session_id}/state",
            "/api/learning/session/{session_id}/audio-manifest",
            "/api/learning/session/{session_id}/audio/{outline_node_id}/{filename}",
            "/api/learning/session/{session_id}/ppt-preview",
            "/api/learning/session/{session_id}/ppt-preview/{plan_hash}/{filename}",
            "/api/learning/assessment",
            "/api/learning/assessment/submit",
            "/api/learning/profile/{user_id}",
            "/api/learning/profile/slice",
            "/api/learning/memory/consolidate",
            "/api/learning/student-twin/{user_id}",
            "/api/learning/knowledge-tracing/{user_id}",
            "/api/learning/plan/{user_id}/month",
            "/api/learning/plan/{user_id}/day/{plan_date}",
            "/api/learning/plan/recommend",
            "/api/learning/plan/items",
            "/api/learning/plan/items/{item_id}/update",
            "/api/learning/plan/items/{item_id}/complete",
            "/api/learning/plan/items/{item_id}/defer",
            "/api/learning/plan/items/{item_id}",
            "/api/learning/route",
            "/api/learning/execute",
            "/api/learning/step",
            "/api/learning/interruption",
            "/api/learning/interruption/stream",
            "/api/learning/continue",
            "/api/learning/continue/stream",
            "/api/learning/answer",
            "/api/learning/answer/stream",
            "/api/learning/session/{session_id}/stage-exam",
            "/api/learning/session/{session_id}/stage-exam/submit",
            "/api/router/chat",
            "/api/router/jobs",
            "/api/router/jobs/{job_id}",
            "/api/router/jobs/{job_id}/stream",
            "/api/agents/{agent_name}/invoke",
            "/api/agents/{agent_name}/stream",
            "/api/legacy/chat",
            "/mcp/tools/{tool_name}",
            "/mcp/agents/{agent_name}/stream",
        ],
    }


@app.post("/api/pathology/ask")
async def pathology_ask(req: PathologyRequest) -> JSONResponse:
    return JSONResponse(await run_pathology_ask(req.question, req.top_k, req.allow_web))


@app.post("/api/decision/assist")
async def decision_assist(req: DecisionRequest) -> JSONResponse:
    return JSONResponse(await run_decision_assist(req.case_text, req.top_k, req.allow_web))


@app.post("/api/evidence/search")
async def evidence_search(req: EvidenceSearchRequest) -> JSONResponse:
    result = run_evidence_search(req.query, req.top_k)
    return JSONResponse(result)


@app.post("/api/knowledge/search")
async def knowledge_search(req: KnowledgeSearchRequest) -> JSONResponse:
    result = await asyncio.to_thread(run_knowledge_search, req.query, req.top_k)
    return JSONResponse(result)


@app.get("/api/knowledge/topics")
async def knowledge_topics() -> JSONResponse:
    return JSONResponse(get_knowledge_topics())


@app.post("/api/knowledge/view")
async def knowledge_view(req: KnowledgeViewRequest) -> JSONResponse:
    return JSONResponse(run_knowledge_view(req.topic_id, req.dimension_id, req.source_type, req.top_k))


@app.get("/api/knowledge-graph/summary")
async def knowledge_graph_summary() -> JSONResponse:
    result = get_graph_summary()
    return JSONResponse(result, status_code=200 if result.get("ok", False) else 404)


@app.get("/api/knowledge-graph/overview")
async def knowledge_graph_overview() -> JSONResponse:
    result = get_graph_overview()
    return JSONResponse(result, status_code=200 if result.get("ok", False) else 404)


@app.get("/api/knowledge-graph/node/{node_id:path}")
async def knowledge_graph_node(node_id: str) -> JSONResponse:
    result = get_graph_node(node_id)
    return JSONResponse(result, status_code=200 if result.get("ok", False) else 404)


@app.post("/api/knowledge-graph/neighborhood")
async def knowledge_graph_neighborhood(req: KnowledgeGraphNeighborhoodRequest) -> JSONResponse:
    result = get_graph_neighborhood(req.node_id, req.depth, req.limit)
    return JSONResponse(result, status_code=200 if result.get("ok", False) else 404)


@app.post("/api/literature/search")
async def literature_search(req: LiteratureRequest) -> JSONResponse:
    return JSONResponse(await run_literature_search(req.query, req.months, req.limit))


@app.post("/api/literature/terms")
async def literature_terms(req: TermsRequest) -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "module": "literature_terms",
            "result": run_terms(req.task),
        }
    )


@app.post("/api/literature/translate")
async def literature_translate(req: LiteratureTranslateRequest) -> JSONResponse:
    return JSONResponse(run_article_translation(req.article))


@app.post("/api/stats/query")
async def stats_query(req: StatsRequest) -> JSONResponse:
    return JSONResponse(run_stats_query(req.query, req.top_k))


@app.get("/api/stats/options")
async def stats_options_api() -> JSONResponse:
    return JSONResponse(stats_options())


@app.post("/api/stats/filter")
async def stats_filter(req: StatsFilterRequest) -> JSONResponse:
    return JSONResponse(run_stats_filter(req.model_dump()))


@app.post("/api/records/asr-correct")
async def asr_correct(req: AsrRequest) -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "module": "asr_correction",
            "result": correct_asr(req.conversation),
        }
    )


@app.post("/api/records/extract")
async def record_extract(req: ExtractRequest) -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "module": "record_extraction",
            "result": extract_fields(req.conversation),
        }
    )


@app.post("/api/records/generate")
async def record_generate(req: ReportRequest) -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "module": "record_generation",
            "result": generate_record(req.fields, req.conversation),
        }
    )


@app.post("/api/records/workflow")
async def record_workflow(req: WorkflowRequest) -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "module": "record_workflow",
            "result": run_workflow(req.conversation),
        }
    )


@app.post("/api/records/transcribe-upload")
async def record_transcribe_upload(file: UploadFile = File(...)) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "ok": False,
            "module": "record_transcribe_upload",
            "message": "Audio ASR is not configured in the local rebuild yet. Please paste transcribed text.",
            "payload": {"filename": file.filename, "content_type": file.content_type},
        },
    )


@app.post("/api/records/audio-generate")
async def record_audio_generate(
    file: UploadFile = File(...),
    query: str | None = Form(default=None),
) -> JSONResponse:
    if query:
        return JSONResponse(
            {
                "ok": True,
                "module": "record_audio_generate",
                "result": run_workflow(query),
                "payload": {"filename": file.filename, "content_type": file.content_type},
            }
        )
    return JSONResponse(
        status_code=422,
        content={
            "ok": False,
            "module": "record_audio_generate",
            "message": "Audio ASR is not configured in the local rebuild yet. Provide query/transcribed text.",
            "payload": {"filename": file.filename, "content_type": file.content_type},
        },
    )


@app.post("/api/records/extract-upload")
async def record_extract_upload(
    audio_file: UploadFile | None = File(default=None),
    image_file: UploadFile | None = File(default=None),
    conversation: str | None = Form(default=None),
) -> JSONResponse:
    if not conversation:
        return JSONResponse(
            status_code=422,
            content={
                "ok": False,
                "module": "record_extract_upload",
                "message": "Audio/image parsing is not configured yet. Provide conversation text.",
                "payload": {
                    "audio_filename": audio_file.filename if audio_file else None,
                    "image_filename": image_file.filename if image_file else None,
                },
            },
        )
    result = extract_fields(conversation)
    result["image_filename"] = image_file.filename if image_file else None
    return JSONResponse({"ok": True, "module": "record_extract_upload", "result": result})


@app.post("/api/records/workflow-upload")
async def record_workflow_upload(
    audio_file: UploadFile | None = File(default=None),
    image_file: UploadFile | None = File(default=None),
    conversation: str | None = Form(default=None),
) -> JSONResponse:
    if not conversation:
        return JSONResponse(
            status_code=422,
            content={
                "ok": False,
                "module": "record_workflow_upload",
                "message": "Audio/image parsing is not configured yet. Provide conversation text.",
                "payload": {
                    "audio_filename": audio_file.filename if audio_file else None,
                    "image_filename": image_file.filename if image_file else None,
                },
            },
        )
    result = run_workflow(conversation)
    result["image_filename"] = image_file.filename if image_file else None
    return JSONResponse({"ok": True, "module": "record_workflow_upload", "result": result})


@app.post("/api/image/analyze")
async def image_analyze(req: ImageRequest) -> JSONResponse:
    result = await run_image_analysis(
        image_base64=req.image_base64,
        query=req.query,
        allow_web=req.allow_web,
    )
    return JSONResponse(result, status_code=200 if result.get("ok", False) else 422)


@app.post("/api/image/analyze-upload")
async def image_analyze_upload(
    file: UploadFile = File(...),
    query: str | None = Form(default=None),
    allow_web: bool = Form(default=True),
) -> JSONResponse:
    content = await file.read()
    if len(content) > image_size_limit_bytes():
        limit_mb = image_size_limit_bytes() // 1024 // 1024
        return JSONResponse(
            status_code=413,
            content={
                "ok": False,
                "module": "image_analysis",
                "message": f"Image is too large. Limit is {limit_mb} MB.",
            },
        )

    image_base64 = base64.b64encode(content).decode("ascii")
    result = await run_image_analysis(
        image_base64=image_base64,
        query=query,
        allow_web=allow_web,
        filename=file.filename,
        content_type=file.content_type,
    )
    return JSONResponse(result, status_code=200 if result.get("ok", False) else 422)


# ============================================================================
# Learning Orchestrator API
# ============================================================================


def _learning_route(
    session_id: str,
    user_input: dict[str, Any] | None,
    use_llm_policy: bool,
) -> dict[str, Any]:
    """Produce a policy decision — does NOT execute.

    When *use_llm_policy* is True, the LLM policy agent is used (with
    automatic fallback to the rule-based policy on error / invalid output).
    Otherwise the orchestrator's current policy_fn is used (default:
    rule-based).
    """
    state = learning_orchestrator.get_policy_state(session_id)
    if use_llm_policy:
        decision = decide_next_action_with_llm(
            state,
            user_input,
            fallback_policy=decide_next_action,
        )
    else:
        decision = learning_orchestrator.policy_fn(state, user_input)
    return {"state": state, "decision": decision}


def _current_web_user(request: Request) -> dict[str, Any] | None:
    return weixin_auth_store.get_user_by_session(request.cookies.get(session_cookie_name()))


def _effective_learning_user_id(
    request: Request,
    requested_user_id: str | None = None,
) -> str:
    user = _current_web_user(request)
    if user:
        return str(user["user_id"])
    return str(requested_user_id or "demo").strip() or "demo"


def _ensure_learning_session_access(request: Request, session_id: str) -> dict[str, Any]:
    session = learning_orchestrator.memory.get_session(session_id)
    user = _current_web_user(request)
    if user and str(session.get("user_id") or "") != str(user["user_id"]):
        raise HTTPException(status_code=403, detail={"message": "This learning session belongs to another account."})
    return session


@app.post("/api/learning/session/start")
async def learning_session_start(req: LearningStartRequest, request: Request) -> JSONResponse:
    """Start a new learning session — no policy routing needed for creation."""
    user_id = _effective_learning_user_id(request, req.user_id)
    result = learning_orchestrator.start_lesson(
        user_id=user_id,
        topic=req.topic,
        display_name=req.display_name,
        profile_input=req.profile_input,
    )
    return JSONResponse(
        {
            "ok": True,
            "module": "learning",
            "action": "start_lesson",
            "result": result,
        }
    )


@app.post("/api/learning/session/start/stream")
async def learning_session_start_stream(req: LearningStartRequest, request: Request) -> StreamingResponse:
    """Start a lesson and stream long-running progress events via SSE."""

    user_id = _effective_learning_user_id(request, req.user_id)

    async def event_stream():
        action = "start_lesson_stream"
        started_at = time.perf_counter()
        yield _sse(
            "start",
            {
                "ok": True,
                "module": "learning",
                "action": action,
                "phase": "received",
                "topic": req.topic,
                "user_id": user_id,
                "message": "已收到学习请求，开始检索知识库并规划课程。",
                "elapsed_seconds": _elapsed(started_at),
            },
        )

        start_task = asyncio.create_task(
            asyncio.to_thread(
                learning_orchestrator.start_lesson,
                user_id=user_id,
                topic=req.topic,
                display_name=req.display_name,
                profile_input=req.profile_input,
            )
        )
        start_messages = [
            "正在检索 KAG 证据并生成第一节大纲。",
            "正在为第一节绑定 evidence / KG node。",
            "正在生成第一节 handout_section，后续标题先占位。",
            "正在启动完整大纲后台补全。",
        ]
        tick = 0
        while not start_task.done():
            await asyncio.sleep(5)
            tick += 1
            yield _progress_event(
                action=action,
                phase="start_lesson",
                message=start_messages[(tick - 1) % len(start_messages)],
                started_at=started_at,
                tick=tick,
            )

        try:
            start_result = await start_task
        except Exception as exc:
            yield _sse(
                "error",
                {
                    "ok": False,
                    "module": "learning",
                    "action": action,
                    "phase": "start_lesson",
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_seconds": _elapsed(started_at),
                },
            )
            return

        session_id = start_result["session"]["id"]
        outline_nodes = start_result.get("outline", {}).get("nodes", [])
        yield _sse(
            "outline_ready",
            {
                "ok": True,
                "module": "learning",
                "action": action,
                "phase": "outline_ready",
                "session_id": session_id,
                "outline_node_count": len(outline_nodes),
                "outline": [
                    {"id": node.get("id"), "title": node.get("title")}
                    for node in outline_nodes
                ],
                "first_handout_section_ready": start_result.get("first_handout_section_ready"),
                "first_handout_audio_status": (
                    start_result.get("first_handout_audio", {}) or {}
                ).get("status"),
                "outline_status": start_result.get("outline_status", "complete"),
                "outline_completion_started": start_result.get(
                    "outline_completion_started",
                    False,
                ),
                "elapsed_seconds": _elapsed(started_at),
            },
        )

        tutor_task = asyncio.create_task(
            asyncio.to_thread(learning_orchestrator.next_step, session_id)
        )
        tutor_messages = [
            "正在基于第一节 handout 生成 Tutor 讲解。",
            "正在生成本节 quiz 与教学信号。",
            "正在写入学习事件与掌握度信号。",
        ]
        while not tutor_task.done():
            await asyncio.sleep(5)
            tick += 1
            yield _progress_event(
                action=action,
                phase="first_tutor_step",
                message=tutor_messages[(tick - 1) % len(tutor_messages)],
                started_at=started_at,
                tick=tick,
            )

        try:
            tutor_step = await tutor_task
        except Exception as exc:
            yield _sse(
                "error",
                {
                    "ok": False,
                    "module": "learning",
                    "action": action,
                    "phase": "first_tutor_step",
                    "session_id": session_id,
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_seconds": _elapsed(started_at),
                },
            )
            return

        yield _sse(
            "tutor_ready",
            {
                "ok": True,
                "module": "learning",
                "action": action,
                "phase": "tutor_ready",
                "session_id": session_id,
                "outline_node_id": tutor_step.get("outline_node_id"),
                "content_status": tutor_step.get("content_status"),
                "question_count": len(tutor_step.get("questions") or []),
                "section_audio_status": (
                    tutor_step.get("section_audio", {}) or {}
                ).get("status"),
                "elapsed_seconds": _elapsed(started_at),
            },
        )
        yield _sse(
            "complete",
            {
                "ok": True,
                "module": "learning",
                "action": action,
                "elapsed_seconds": _elapsed(started_at),
                "result": {
                    "start": start_result,
                    "tutor_step": tutor_step,
                },
            },
        )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers=_stream_headers(),
    )


@app.get("/api/learning/session/{session_id}/state")
async def learning_session_state(session_id: str, request: Request) -> JSONResponse:
    """Return policy state + session summary for a learning session."""
    session = _ensure_learning_session_access(request, session_id)
    try:
        refresh_profile_state_from_l3(
            learning_orchestrator.memory,
            session["user_id"],
        )
    except Exception:
        pass
    summary = learning_orchestrator.memory.session_summary(session_id)
    policy_state = learning_orchestrator.get_policy_state(session_id)
    return JSONResponse(
        {
            "ok": True,
            "module": "learning",
            "result": {
                "policy_state": policy_state,
                "summary": summary,
            },
        }
    )


@app.get("/api/learning/session/{session_id}/audio-manifest")
async def learning_session_audio_manifest(session_id: str, request: Request) -> JSONResponse:
    """Return the slide-level audio manifest for a learning session."""
    _ensure_learning_session_access(request, session_id)
    manifest = get_audio_manifest(
        session_id,
        resource_root=learning_orchestrator.resource_root,
    )
    return JSONResponse(
        {
            "ok": True,
            "module": "learning",
            "action": "audio_manifest",
            "result": manifest,
        }
    )


@app.get("/api/learning/session/{session_id}/avatar-video/{outline_node_id}")
async def learning_session_avatar_video(
    session_id: str,
    outline_node_id: str,
    request: Request,
) -> JSONResponse:
    """Return and refresh the digital-human video task for one lesson section."""
    _ensure_learning_session_access(request, session_id)
    result = await asyncio.to_thread(
        refresh_avatar_video,
        session_id,
        outline_node_id,
        learning_orchestrator.resource_root,
    )
    return JSONResponse({"ok": result.get("status") != "failed", "module": "learning", "action": "avatar_video", "result": result})


@app.post("/api/learning/session/{session_id}/avatar-video/{outline_node_id}")
async def learning_session_create_avatar_video(
    session_id: str,
    outline_node_id: str,
    request: Request,
) -> JSONResponse:
    """Start one asynchronous iFLYTEK video-generation task from the lecture script."""
    _ensure_learning_session_access(request, session_id)
    try:
        result = await asyncio.to_thread(
            create_avatar_video,
            session_id,
            outline_node_id,
            learning_orchestrator.resource_root,
        )
    except XfyunVideoError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "module": "learning", "action": "avatar_video_create", "result": result}, status_code=202)


@app.get("/api/learning/session/{session_id}/avatar-video/{outline_node_id}/stream")
async def learning_session_avatar_video_stream(
    session_id: str,
    outline_node_id: str,
    request: Request,
) -> StreamingResponse:
    """Proxy the short-lived provider video URL as a browser-friendly MP4 stream."""
    _ensure_learning_session_access(request, session_id)
    record = await asyncio.to_thread(
        refresh_avatar_video,
        session_id,
        outline_node_id,
        learning_orchestrator.resource_root,
        True,
    )
    video_url = str(record.get("video_url") or "")
    if record.get("status") != "completed" or not video_url:
        raise HTTPException(status_code=404, detail="Digital-human video is not ready")

    upstream_headers: dict[str, str] = {}
    range_header = request.headers.get("range", "")
    if range_header.startswith("bytes="):
        upstream_headers["Range"] = range_header
    try:
        upstream = await asyncio.to_thread(urlopen, UrlRequest(video_url, headers=upstream_headers), timeout=45)
    except HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Video provider returned HTTP {exc.code}") from exc
    except URLError as exc:
        raise HTTPException(status_code=502, detail="Video provider could not be reached") from exc

    def stream_chunks() -> Any:
        try:
            while chunk := upstream.read(64 * 1024):
                yield chunk
        finally:
            upstream.close()

    response_headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-store"}
    for name in ("Content-Length", "Content-Range"):
        value = upstream.headers.get(name)
        if value:
            response_headers[name] = value
    return StreamingResponse(
        stream_chunks(),
        status_code=getattr(upstream, "status", 200),
        media_type=upstream.headers.get_content_type() or "video/mp4",
        headers=response_headers,
    )


def _learning_session_resource_dir(session_id: str) -> Path:
    resource_root = Path(learning_orchestrator.resource_root).resolve()
    session_dir = (resource_root / session_id).resolve()
    if resource_root not in session_dir.parents:
        raise HTTPException(status_code=404, detail="Learning session resource not found")
    return session_dir


@app.get("/api/learning/session/{session_id}/slides")
async def learning_session_slides(session_id: str) -> JSONResponse:
    """Return the formal ``slides.v1`` deck used by the PPT preview chain."""
    path = _learning_session_resource_dir(session_id) / "slides" / "slides.v1.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Slides resource not found")
    try:
        deck = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Slides resource is invalid: {exc}") from exc
    return JSONResponse({"ok": True, "module": "learning", "action": "slides", "result": deck})


@app.get("/api/learning/session/{session_id}/ppt-plan")
async def learning_session_ppt_plan(session_id: str) -> JSONResponse:
    """Return the aggregated runtime Pin & Paper plan for a session."""
    path = _learning_session_resource_dir(session_id) / "slides" / "pin_and_paper_plan.v1.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Pin & Paper plan not found")
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Pin & Paper plan is invalid: {exc}") from exc
    return JSONResponse({"ok": True, "module": "learning", "action": "ppt_plan", "result": plan})


@app.get("/api/learning/session/{session_id}/ppt-preview")
async def learning_session_ppt_preview(session_id: str) -> JSONResponse:
    """Return real Pin & Paper pages rendered by the same pipeline as PPTX."""
    manifest = await asyncio.to_thread(
        ensure_pin_and_paper_previews,
        session_id=session_id,
        resource_root=learning_orchestrator.resource_root,
    )
    public_slides = []
    for slide in manifest.get("slides", []):
        plan_hash = str(slide.get("plan_hash") or manifest.get("plan_hash") or "")
        filename = str(slide.get("filename") or "")
        public_slides.append({
            key: value
            for key, value in slide.items()
            if key != "relative_path"
        } | {
            "image_url": (
                f"/api/learning/session/{session_id}/ppt-preview/"
                f"{plan_hash}/{filename}"
            ),
        })
    result = {
        **manifest,
        "slides": public_slides,
    }
    return JSONResponse({
        "ok": manifest.get("status") == "ready",
        "module": "learning",
        "action": "ppt_preview",
        "result": result,
    })


@app.get(
    "/api/learning/session/{session_id}/ppt-preview/{plan_hash}/{filename}"
)
async def learning_session_ppt_preview_image(
    session_id: str,
    plan_hash: str,
    filename: str,
) -> FileResponse:
    """Serve one immutable real-rendered PPT page."""
    session_dir = _learning_session_resource_dir(session_id)
    preview_root = (session_dir / "slides" / "rendered_ppt").resolve()
    candidate = (preview_root / plan_hash / filename).resolve()
    if preview_root not in candidate.parents:
        raise HTTPException(status_code=404, detail="PPT preview page not found")
    if candidate.suffix.lower() != ".png" or not candidate.is_file():
        raise HTTPException(status_code=404, detail="PPT preview page not found")
    return FileResponse(
        candidate,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.get("/api/learning/session/{session_id}/ppt-status")
@app.get("/api/learning/session/{session_id}/pptx-status")
async def learning_session_ppt_status(session_id: str) -> JSONResponse:
    """Return current Slides, Pin & Paper plan, and PPTX state."""
    result = learning_orchestrator.wait_for_ppt_resources(session_id, timeout_seconds=0)
    return JSONResponse({
        "ok": True,
        "module": "learning",
        "action": "ppt_status",
        "result": result,
    })


@app.get("/api/learning/session/{session_id}/pptx")
async def learning_session_pptx_file(session_id: str) -> FileResponse:
    """Download the latest partial or complete Pin & Paper PPTX."""
    session_dir = _learning_session_resource_dir(session_id)
    status_path = session_dir / "pptx_status.json"
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=404, detail="PPTX resource is not ready") from exc
    if status.get("ok") is not True or status.get("status") not in {"partial", "ready"}:
        raise HTTPException(status_code=409, detail="PPTX resource is not ready")
    candidate = (session_dir / "lesson.pptx").resolve()
    if session_dir not in candidate.parents or not candidate.is_file():
        raise HTTPException(status_code=404, detail="PPTX file not found")
    return FileResponse(
        candidate,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        filename=f"{session_id}.pptx",
    )


@app.post("/api/learning/session/{session_id}/ppt/rebuild")
async def learning_session_rebuild_ppt(session_id: str) -> JSONResponse:
    """Rebuild formal Slides and enqueue Pin & Paper/PPTX generation."""
    result = learning_orchestrator.rebuild_ppt_resources(session_id)
    return JSONResponse({
        "ok": bool(result.get("started")),
        "module": "learning",
        "action": "rebuild_ppt",
        "result": result,
    })


@app.post("/api/learning/session/{session_id}/resources/retry")
async def learning_session_retry_resources(session_id: str) -> JSONResponse:
    """Resume failed or interrupted handout generation and refresh slides.v1."""
    result = learning_orchestrator.retry_failed_resources(session_id)
    return JSONResponse({
        "ok": bool(result.get("started")),
        "module": "learning",
        "action": "retry_resources",
        "result": result,
    })


def _stage_exam_evidence_map(session_id: str) -> dict[str, Any]:
    learning_orchestrator._activate_evidence_context(session_id)
    if learning_orchestrator._active_evidence_session_id != session_id:
        return {}
    return learning_orchestrator._last_outline_evidence_map or {}


@app.get("/api/learning/session/{session_id}/stage-exam")
async def learning_session_stage_exam(session_id: str) -> JSONResponse:
    """Return an existing stage exam, or generate one for this session."""
    try:
        exam = generate_stage_exam(
            learning_orchestrator.memory,
            session_id,
            resource_root=learning_orchestrator.resource_root,
            outline_evidence_map=_stage_exam_evidence_map(session_id),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse({
        "ok": True,
        "module": "learning",
        "action": "stage_exam",
        "result": public_stage_exam(
            exam,
            include_answers=exam.get("status") == "submitted",
        ),
    })


@app.post("/api/learning/session/{session_id}/stage-exam")
async def learning_session_stage_exam_generate(
    session_id: str,
    req: StageExamGenerateRequest,
) -> JSONResponse:
    """Generate or rebuild the stage exam for this session."""
    try:
        exam = generate_stage_exam(
            learning_orchestrator.memory,
            session_id,
            resource_root=learning_orchestrator.resource_root,
            outline_evidence_map=_stage_exam_evidence_map(session_id),
            force=req.force,
            questions_per_section=req.questions_per_section,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse({
        "ok": True,
        "module": "learning",
        "action": "stage_exam_generate",
        "result": public_stage_exam(exam, include_answers=False),
    })


@app.post("/api/learning/session/{session_id}/stage-exam/submit")
async def learning_session_stage_exam_submit(
    session_id: str,
    req: StageExamSubmitRequest,
) -> JSONResponse:
    """Submit and grade the session-level stage exam."""
    try:
        exam = submit_stage_exam(
            learning_orchestrator.memory,
            session_id,
            [answer.model_dump() for answer in req.answers],
            resource_root=learning_orchestrator.resource_root,
            outline_evidence_map=_stage_exam_evidence_map(session_id),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({
        "ok": True,
        "module": "learning",
        "action": "stage_exam_submit",
        "result": public_stage_exam(exam, include_answers=True),
    })


@app.get("/api/learning/session/{session_id}/audio/{outline_node_id}/{filename}")
async def learning_session_audio_file(
    session_id: str,
    outline_node_id: str,
    filename: str,
    request: Request,
) -> FileResponse:
    """Return one generated slide audio file."""
    _ensure_learning_session_access(request, session_id)
    session_dir = _learning_session_resource_dir(session_id)
    audio_root = (session_dir / "audio_files").resolve()
    candidate = (audio_root / outline_node_id / filename).resolve()
    if audio_root not in candidate.parents:
        raise HTTPException(status_code=404, detail="Audio file not found")
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Audio file not found")
    return FileResponse(candidate)


@app.get("/api/learning/assessment")
async def learning_assessment_definition() -> JSONResponse:
    """Return the stable learner-assessment question set."""
    return JSONResponse(
        {
            "ok": True,
            "module": "learning",
            "action": "learner_assessment_definition",
            "result": get_learner_assessment_definition(),
        }
    )


@app.post("/api/learning/assessment/submit")
async def learning_assessment_submit(req: LearnerAssessmentSubmitRequest, request: Request) -> JSONResponse:
    """Submit onboarding assessment answers and persist the initial profile."""
    user_id = _effective_learning_user_id(request, req.user_id)
    try:
        result = submit_learner_assessment(
            learning_orchestrator.memory,
            user_id=user_id,
            answers=[item.model_dump() for item in req.answers],
            target_exam=req.target_exam,
            skip_assessment=req.skip_assessment,
            instrument_name=req.instrument_name,
            instrument_version=req.instrument_version,
        )
    except ValueError as exc:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "module": "learning",
                "action": "learner_assessment_submit",
                "result": {"error": str(exc)},
            },
        )
    return JSONResponse(
        {
            "ok": True,
            "module": "learning",
            "action": "learner_assessment_submit",
            "result": result,
        }
    )


@app.get("/api/learning/profile/{user_id}")
async def learning_profile(user_id: str, request: Request) -> JSONResponse:
    """Return compact profile + L3 learner memory for a user."""
    user_id = _effective_learning_user_id(request, user_id)
    profile = learning_orchestrator.memory.get_or_create_profile(user_id)
    try:
        refresh_profile_state_from_l3(learning_orchestrator.memory, user_id)
    except Exception:
        pass
    learner_memory = learning_orchestrator.memory.get_learner_memory(user_id)
    summaries = learning_orchestrator.memory.list_learning_summaries(
        user_id,
        limit=50,
    )
    recent_events = learning_orchestrator.memory.list_recent_events(
        user_id,
        limit=100,
    )
    assessment_state = learning_orchestrator.memory.learner_assessment_state(
        user_id,
        profile=profile,
    )
    return JSONResponse(
        {
            "ok": True,
            "module": "learning",
            "action": "profile",
            "result": {
                "user_id": user_id,
                "profile": profile,
                "learner_memory": learner_memory,
                "learning_summaries": summaries,
                "recent_events": recent_events,
                "assessment_state": assessment_state,
            },
        }
    )


@app.post("/api/learning/profile/slice")
async def learning_profile_slice(req: LearningProfileSliceRequest, request: Request) -> JSONResponse:
    """Return a role-specific learner-memory slice."""
    user_id = _effective_learning_user_id(request, req.user_id)
    result = build_profile_slice(
        learning_orchestrator.memory,
        user_id=user_id,
        role=req.role,
        context=req.context,
        limit=req.limit,
    )
    return JSONResponse(
        {
            "ok": True,
            "module": "learning",
            "action": "profile_slice",
            "result": result,
        }
    )


@app.post("/api/learning/memory/consolidate")
async def learning_memory_consolidate(
    req: LearningMemoryConsolidateRequest,
    request: Request,
) -> JSONResponse:
    """Manually run L2 and/or L3 learner-memory consolidation."""
    layer = req.layer.lower().strip()
    user_id = _effective_learning_user_id(request, req.user_id)
    result: dict[str, Any] = {}
    if layer in {"all", "l2"}:
        result["l2"] = consolidate_l2(
            learning_orchestrator.memory,
            user_id=user_id,
            session_id=req.session_id,
            surface=req.surface,
            surfaces=req.surfaces,
            limit=req.limit,
        )
    if layer in {"all", "l3"}:
        result["l3"] = consolidate_l3(
            learning_orchestrator.memory,
            user_id=user_id,
            session_id=req.session_id,
            slot=req.slot,
            limit=req.limit,
        )
    if not result:
        result["error"] = "layer must be one of: all, l2, l3"
    return JSONResponse(
        {
            "ok": "error" not in result,
            "module": "learning",
            "action": "memory_consolidate",
            "result": result,
        }
    )


@app.get("/api/learning/student-twin/{user_id}")
async def learning_student_twin(
    user_id: str,
    request: Request,
    plan_date: str | None = None,
    available_minutes: int | None = None,
    max_bottlenecks: int = 5,
) -> JSONResponse:
    """Return a deterministic StudentTwinAgent learning-path simulation."""
    user_id = _effective_learning_user_id(request, user_id)
    try:
        result = build_student_twin_simulation(
            learning_orchestrator.memory,
            user_id=user_id,
            plan_date=plan_date,
            available_minutes=available_minutes,
            max_bottlenecks=max_bottlenecks,
        )
    except ValueError as exc:
        return JSONResponse(
            {
                "ok": False,
                "module": "learning",
                "action": "student_twin",
                "result": {"error": str(exc)},
            },
            status_code=400,
        )
    return JSONResponse(
        {
            "ok": True,
            "module": "learning",
            "action": "student_twin",
            "result": result,
        }
    )


@app.get("/api/learning/knowledge-tracing/{user_id}")
async def learning_knowledge_tracing(
    user_id: str,
    request: Request,
    limit: int = 200,
) -> JSONResponse:
    """Return BKT/DKT-compatible KG-node cognitive states for a learner."""
    user_id = _effective_learning_user_id(request, user_id)
    states = learning_orchestrator.memory.list_knowledge_trace_states(
        user_id,
        limit=limit,
    )
    return JSONResponse(
        {
            "ok": True,
            "module": "learning",
            "action": "knowledge_tracing",
            "result": {
                "user_id": user_id,
                "schema_version": "knowledge_tracing.bkt.v1",
                "model": "bkt_kg_node_v1",
                "states": states,
            },
        }
    )


def _learning_plan_response(action: str, result: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        {
            "ok": status_code < 400,
            "module": "learning",
            "action": action,
            "result": result,
        },
        status_code=status_code,
    )


def _learning_plan_error(action: str, exc: Exception, status_code: int) -> JSONResponse:
    return _learning_plan_response(
        action,
        {"error": str(exc)},
        status_code=status_code,
    )


@app.get("/api/learning/plan/{user_id}/month")
async def learning_plan_month(user_id: str, year: int, month: int, request: Request) -> JSONResponse:
    """Return study-plan items grouped by calendar day for one month."""
    user_id = _effective_learning_user_id(request, user_id)
    try:
        result = get_study_plan_month(
            learning_orchestrator.memory,
            user_id=user_id,
            year=year,
            month=month,
        )
    except ValueError as exc:
        return _learning_plan_error("study_plan_month", exc, 400)
    return _learning_plan_response("study_plan_month", result)


@app.get("/api/learning/plan/{user_id}/day/{plan_date}")
async def learning_plan_day(user_id: str, plan_date: str, request: Request) -> JSONResponse:
    """Return study-plan items for one day."""
    user_id = _effective_learning_user_id(request, user_id)
    try:
        result = get_study_plan_day(
            learning_orchestrator.memory,
            user_id=user_id,
            plan_date=plan_date,
        )
    except ValueError as exc:
        return _learning_plan_error("study_plan_day", exc, 400)
    return _learning_plan_response("study_plan_day", result)


@app.post("/api/learning/plan/recommend")
async def learning_plan_recommend(req: LearningPlanRecommendRequest, request: Request) -> JSONResponse:
    """Return preview recommendations for one study-plan day."""
    user_id = _effective_learning_user_id(request, req.user_id)
    try:
        result = recommend_study_plan(
            learning_orchestrator.memory,
            user_id=user_id,
            plan_date=req.plan_date,
            max_items=req.max_items,
            request_text=req.request_text,
        )
    except ValueError as exc:
        return _learning_plan_error("study_plan_recommend", exc, 400)
    return _learning_plan_response("study_plan_recommend", result)


@app.get("/api/learning/plan/drafts/{plan_draft_id}")
async def learning_plan_get_draft(plan_draft_id: str, user_id: str = Query(min_length=1)) -> JSONResponse:
    """Return a single plan draft owned by *user_id*, if it exists and has not expired.

    Unknown, cross-user, and expired drafts return distinct HTTP errors —
    none of them fall back to default recommendations or expose draft
    content to unauthorized callers.
    """
    try:
        record = get_draft_from_default_store(plan_draft_id, user_id)
    except KeyError as exc:
        return _learning_plan_error("study_plan_get_draft", exc, 404)
    except PermissionError as exc:
        return _learning_plan_error("study_plan_get_draft", exc, 403)
    except ValueError as exc:
        return _learning_plan_error("study_plan_get_draft", exc, 410)
    return _learning_plan_response("study_plan_get_draft", record)


@app.post("/api/learning/plan/items")
async def learning_plan_create_item(req: LearningPlanCreateRequest, request: Request) -> JSONResponse:
    """Create one study-plan to-do item."""
    user_id = _effective_learning_user_id(request, req.user_id)
    try:
        result = create_study_plan_item(
            learning_orchestrator.memory,
            user_id=user_id,
            plan_date=req.plan_date,
            title=req.title,
            notes=req.notes,
            linked_course_id=req.linked_course_id,
            linked_course_title=req.linked_course_title,
            task_type=req.task_type,
            learning_target=req.learning_target,
            kg_node_ids=req.kg_node_ids,
            learning_scope=req.learning_scope,
            dependency=req.dependency,
            practice_level=req.practice_level,
            session_id=req.session_id,
        )
    except ValueError as exc:
        return _learning_plan_error("study_plan_create_item", exc, 400)
    return _learning_plan_response("study_plan_create_item", result)


@app.post("/api/learning/plan/items/{item_id}/update")
async def learning_plan_update_item(
    item_id: str,
    req: LearningPlanUpdateRequest,
    request: Request,
) -> JSONResponse:
    """Update one study-plan item."""
    user_id = _effective_learning_user_id(request, req.user_id)
    try:
        updates = req.model_dump(exclude={"user_id"}, exclude_unset=True)
        result = update_study_plan_item(
            learning_orchestrator.memory,
            item_id=item_id,
            user_id=user_id,
            updates=updates,
        )
    except KeyError as exc:
        return _learning_plan_error("study_plan_update_item", exc, 404)
    except ValueError as exc:
        return _learning_plan_error("study_plan_update_item", exc, 400)
    return _learning_plan_response("study_plan_update_item", result)


@app.post("/api/learning/plan/items/{item_id}/complete")
async def learning_plan_complete_item(
    item_id: str,
    req: LearningPlanCompleteRequest,
    request: Request,
) -> JSONResponse:
    """Toggle completion. Completed items feed a deterministic learner-memory signal."""
    user_id = _effective_learning_user_id(request, req.user_id)
    try:
        result = complete_study_plan_item(
            learning_orchestrator.memory,
            item_id=item_id,
            user_id=user_id,
            completed=req.completed,
        )
    except KeyError as exc:
        return _learning_plan_error("study_plan_complete_item", exc, 404)
    return _learning_plan_response("study_plan_complete_item", result)


@app.post("/api/learning/plan/items/{item_id}/defer")
async def learning_plan_defer_item(
    item_id: str,
    req: LearningPlanDeferRequest,
    request: Request,
) -> JSONResponse:
    """Move a plan item to another day, defaulting to the next day."""
    user_id = _effective_learning_user_id(request, req.user_id)
    try:
        result = defer_study_plan_item(
            learning_orchestrator.memory,
            item_id=item_id,
            user_id=user_id,
            to_date=req.to_date,
        )
    except KeyError as exc:
        return _learning_plan_error("study_plan_defer_item", exc, 404)
    except ValueError as exc:
        return _learning_plan_error("study_plan_defer_item", exc, 400)
    return _learning_plan_response("study_plan_defer_item", result)


@app.delete("/api/learning/plan/items/{item_id}")
async def learning_plan_delete_item(item_id: str, request: Request, user_id: str = "demo") -> JSONResponse:
    """Delete one study-plan item."""
    user_id = _effective_learning_user_id(request, user_id)
    try:
        result = delete_study_plan_item(
            learning_orchestrator.memory,
            item_id=item_id,
            user_id=user_id,
        )
    except KeyError as exc:
        return _learning_plan_error("study_plan_delete_item", exc, 404)
    return _learning_plan_response("study_plan_delete_item", result)


def _clawbridge_secret() -> str:
    return (
        os.getenv("ORALSAGE_CLAWBRIDGE_INTERNAL_SECRET")
        or os.getenv("CLAWBRIDGE_INTERNAL_SECRET")
        or os.getenv("AUTH_INTERNAL_OPENCLAW_SECRET")
        or ""
    ).strip()


def _require_web_user(request: Request) -> dict[str, Any]:
    user = weixin_auth_store.get_user_by_session(request.cookies.get(session_cookie_name()))
    if user is None:
        raise HTTPException(status_code=401, detail={"message": "Web login is required."})
    return user


def _resolve_internal_weixin_user(
    *,
    x_newproject_internal_secret: str | None,
    x_clawbot_instance_id: str | None,
    x_legacy_clawbot_instance_id: str | None,
    x_weixin_account_id: str | None,
    x_weixin_user_id: str | None,
) -> dict[str, Any] | None:
    if not any([x_newproject_internal_secret, x_clawbot_instance_id, x_legacy_clawbot_instance_id, x_weixin_account_id, x_weixin_user_id]):
        return None
    if str(x_newproject_internal_secret or "").strip() != _clawbridge_secret():
        raise HTTPException(status_code=401, detail={"message": "Weixin internal authentication failed."})
    instance_id = str(x_clawbot_instance_id or x_legacy_clawbot_instance_id or "").strip() or None
    if not instance_id and not (x_weixin_account_id and x_weixin_user_id):
        raise HTTPException(status_code=401, detail={"message": "Weixin identity headers are missing."})
    return weixin_auth_store.get_bound_user(
        clawbot_instance_id=instance_id,
        weixin_account_id=x_weixin_account_id,
        weixin_user_id=x_weixin_user_id,
    )


def _set_weixin_auth_cookie(response: JSONResponse, token: str) -> JSONResponse:
    response.set_cookie(
        key=session_cookie_name(),
        value=token,
        httponly=True,
        samesite="lax",
        path="/",
        max_age=30 * 24 * 3600,
    )
    return response


@app.post("/api/auth/register")
@app.post("/api/v1/auth/register")
async def web_register(payload: dict[str, Any] | None = None) -> JSONResponse:
    data = payload or {}
    try:
        user, token = weixin_auth_store.register(
            username=str(data.get("username") or data.get("user_id") or ""),
            password=str(data.get("password") or ""),
            display_name=str(data.get("display_name") or data.get("username") or data.get("user_id") or ""),
        )
    except WeixinAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"message": str(exc)}) from exc
    json_response = JSONResponse({"ok": True, "module": "weixin_auth", "user": user})
    return _set_weixin_auth_cookie(json_response, token)


@app.post("/api/auth/web-login")
@app.post("/api/auth/login")
@app.post("/api/v1/auth/login")
async def web_login(payload: dict[str, Any] | None = None) -> JSONResponse:
    data = payload or {}
    try:
        user, token = weixin_auth_store.login(
            username=str(data.get("username") or data.get("user_id") or ""),
            password=str(data.get("password") or ""),
        )
    except WeixinAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"message": str(exc)}) from exc
    json_response = JSONResponse({"ok": True, "module": "weixin_auth", "user": user})
    return _set_weixin_auth_cookie(json_response, token)


@app.post("/api/auth/logout")
@app.post("/api/v1/auth/logout")
async def web_logout(request: Request) -> JSONResponse:
    weixin_auth_store.logout(request.cookies.get(session_cookie_name()))
    response = JSONResponse({"ok": True})
    response.delete_cookie(session_cookie_name(), path="/")
    return response


@app.get("/api/auth/me")
@app.get("/api/v1/auth/me")
async def web_me(request: Request) -> JSONResponse:
    user = weixin_auth_store.get_user_by_session(request.cookies.get(session_cookie_name()))
    return JSONResponse({"ok": True, "user": user, "authenticated": user is not None})


@app.post("/api/auth/weixin/qr/start")
async def weixin_qr_start(request: Request) -> JSONResponse:
    user = _require_web_user(request)
    session_id = uuid4().hex
    try:
        result = ClawBridgeClient().start_qr_login(session_id=session_id)
    except ClawBridgeError as exc:
        raise HTTPException(status_code=503, detail={"message": str(exc)}) from exc
    session = weixin_auth_store.create_bind_session(
        user_id=str(user["user_id"]),
        session_id=result.session_id,
        qr_data_url=result.qr_data_url,
        message=result.message,
    )
    return JSONResponse(
        {
            "session_id": session["session_id"],
            "status": session["status"],
            "qr_data_url": session["qr_data_url"],
            "expires_at": session["expires_at"],
            "message": session["message"],
        }
    )


@app.get("/api/auth/weixin/qr/status")
async def weixin_qr_status(request: Request, session_id: str = Query(min_length=1)) -> JSONResponse:
    user = _require_web_user(request)
    session = weixin_auth_store.get_bind_session(session_id)
    if not session or session.get("user_id") != user["user_id"]:
        raise HTTPException(status_code=404, detail={"message": "The requested Weixin QR session was not found."})
    if session.get("status") == "bound":
        return JSONResponse(
            {
                "session_id": session_id,
                "status": "bound",
                "bound": True,
                "qr_data_url": session.get("qr_data_url"),
                "expires_at": session.get("expires_at"),
                "message": session.get("message"),
                "binding": weixin_auth_store.get_bind_status_for_user(str(user["user_id"])),
            }
        )
    try:
        status = await asyncio.to_thread(
            ClawBridgeClient(timeout_seconds=8).get_qr_status,
            session_id=session_id,
            timeout_ms=5000,
        )
    except ClawBridgeError as exc:
        updated = weixin_auth_store.update_bind_session(session_id, {"status": "error", "message": str(exc)})
        return JSONResponse(
            {
                "session_id": session_id,
                "status": "error",
                "bound": False,
                "qr_data_url": (updated or session).get("qr_data_url"),
                "expires_at": (updated or session).get("expires_at"),
                "message": str(exc),
                "binding": weixin_auth_store.get_bind_status_for_user(str(user["user_id"])),
            }
        )

    if not status.connected:
        next_status = status.status if status.status in {"waiting", "expired", "error"} else "waiting"
        updated = weixin_auth_store.update_bind_session(
            session_id,
            {
                "status": next_status,
                "message": status.message,
                **({"qr_data_url": status.qr_data_url} if status.qr_data_url else {}),
            },
        )
        return JSONResponse(
            {
                "session_id": session_id,
                "status": next_status,
                "bound": False,
                "qr_data_url": (updated or session).get("qr_data_url"),
                "expires_at": (updated or session).get("expires_at"),
                "message": status.message,
                "binding": weixin_auth_store.get_bind_status_for_user(str(user["user_id"])),
            }
        )

    binding = weixin_auth_store.bind_weixin_identity(
        user_id=str(user["user_id"]),
        weixin_account_id=status.weixin_account_id or "",
        weixin_user_id=status.weixin_user_id or "",
        clawbot_instance_id=status.clawbot_instance_id,
    )
    updated = weixin_auth_store.update_bind_session(
        session_id,
        {
            "status": "bound",
            "message": status.message,
            "weixin_account_id": status.weixin_account_id,
            "weixin_user_id": status.weixin_user_id,
            "clawbot_instance_id": binding.get("clawbot_instance_id"),
            "binding_revision": binding.get("binding_revision"),
        },
    )
    return JSONResponse(
        {
            "session_id": session_id,
            "status": "bound",
            "bound": True,
            "qr_data_url": (updated or session).get("qr_data_url"),
            "expires_at": (updated or session).get("expires_at"),
            "message": status.message,
            "binding": binding,
        }
    )


@app.get("/api/auth/weixin/bind-status")
async def weixin_bind_status(
    request: Request,
    x_newproject_internal_secret: str | None = Header(default=None, alias="X-NewProject-Internal-Secret"),
    x_clawbot_instance_id: str | None = Header(default=None, alias="X-AlertClaw-ClawBot-Instance-Id"),
    x_legacy_clawbot_instance_id: str | None = Header(default=None, alias="X-OpenSensus-ClawBot-Instance-Id"),
    x_weixin_account_id: str | None = Header(default=None, alias="X-Weixin-Account-Id"),
    x_weixin_user_id: str | None = Header(default=None, alias="X-Weixin-User-Id"),
) -> JSONResponse:
    internal_user = _resolve_internal_weixin_user(
        x_newproject_internal_secret=x_newproject_internal_secret,
        x_clawbot_instance_id=x_clawbot_instance_id,
        x_legacy_clawbot_instance_id=x_legacy_clawbot_instance_id,
        x_weixin_account_id=x_weixin_account_id,
        x_weixin_user_id=x_weixin_user_id,
    )
    if internal_user is not None:
        return JSONResponse(weixin_auth_store.get_bind_status_for_user(str(internal_user["user_id"])))
    user = _require_web_user(request)
    return JSONResponse(weixin_auth_store.get_bind_status_for_user(str(user["user_id"])))


@app.delete("/api/auth/weixin/bind")
async def weixin_unbind(
    request: Request,
    x_newproject_internal_secret: str | None = Header(default=None, alias="X-NewProject-Internal-Secret"),
    x_clawbot_instance_id: str | None = Header(default=None, alias="X-AlertClaw-ClawBot-Instance-Id"),
    x_legacy_clawbot_instance_id: str | None = Header(default=None, alias="X-OpenSensus-ClawBot-Instance-Id"),
    x_weixin_account_id: str | None = Header(default=None, alias="X-Weixin-Account-Id"),
    x_weixin_user_id: str | None = Header(default=None, alias="X-Weixin-User-Id"),
) -> JSONResponse:
    if any([x_newproject_internal_secret, x_clawbot_instance_id, x_legacy_clawbot_instance_id, x_weixin_account_id, x_weixin_user_id]):
        if str(x_newproject_internal_secret or "").strip() != _clawbridge_secret():
            raise HTTPException(status_code=401, detail={"message": "Weixin internal authentication failed."})
        result = weixin_auth_store.unbind_identity(
            clawbot_instance_id=str(x_clawbot_instance_id or x_legacy_clawbot_instance_id or "").strip() or None,
            weixin_account_id=x_weixin_account_id,
            weixin_user_id=x_weixin_user_id,
        )
        return JSONResponse(result)
    user = _require_web_user(request)
    return JSONResponse(weixin_auth_store.unbind_user(str(user["user_id"])))


@app.get("/api/v1/internal/clawbridge/bootstrap")
async def clawbridge_bootstrap(
    x_clawbridge_secret: str | None = Header(default=None, alias="X-AlertClaw-ClawBridge-Secret"),
    x_clawbridge_instance_id: str | None = Header(default=None, alias="X-AlertClaw-ClawBridge-Instance-Id"),
) -> JSONResponse:
    if str(x_clawbridge_secret or "").strip() != _clawbridge_secret():
        raise HTTPException(status_code=401, detail={"message": "ClawBridge internal authentication failed."})
    return JSONResponse(
        {
            "service": "OralSage",
            "version": "0.1.0",
            "bridge_name": "OralSage ClawBridge",
            "bridge_instance_id": x_clawbridge_instance_id,
            "backend_base_url": os.getenv("CLAWBRIDGE_BACKEND_BASE_URL", "http://127.0.0.1:8000"),
            "headers": {
                "clawbridge_secret": "X-AlertClaw-ClawBridge-Secret",
                "clawbridge_instance_id": "X-AlertClaw-ClawBridge-Instance-Id",
                "clawbot_instance_id": "X-AlertClaw-ClawBot-Instance-Id",
                "weixin_account_id": "X-Weixin-Account-Id",
                "weixin_user_id": "X-Weixin-User-Id",
            },
            "endpoints": {
                "bind_status": "/api/auth/weixin/bind-status",
                "unbind": "/api/auth/weixin/bind",
                "internal_chatbox_messages": "/api/v1/internal/clawbridge/chatbox/messages",
                "weixin_qr_start": "/api/v1/weixin/qr/start",
                "weixin_qr_status": "/api/v1/weixin/qr/status",
                "weixin_account_status": "/api/v1/weixin/accounts/{account_id}",
            },
        }
    )


@app.post("/api/v1/internal/clawbridge/chatbox/messages")
async def clawbridge_chatbox_messages(
    payload: dict[str, Any],
    x_clawbridge_secret: str | None = Header(default=None, alias="X-AlertClaw-ClawBridge-Secret"),
) -> JSONResponse:
    if str(x_clawbridge_secret or "").strip() != _clawbridge_secret():
        raise HTTPException(status_code=401, detail={"message": "ClawBridge internal authentication failed."})
    user = weixin_auth_store.get_bound_user(
        clawbot_instance_id=str(payload.get("clawbot_instance_id") or "").strip() or None,
        weixin_account_id=str(payload.get("weixin_account_id") or "").strip() or None,
        weixin_user_id=str(payload.get("weixin_user_id") or "").strip() or None,
    )
    if user is None:
        raise HTTPException(status_code=404, detail={"error_code": "auth_weixin_not_bound", "message": "The Weixin sender is not bound to an OralSage account."})
    result = handle_weixin_plan_message(
        learning_orchestrator.memory,
        user_id=str(user["user_id"]),
        text=str(payload.get("text") or ""),
    )
    return JSONResponse(
        {
            "thread_id": f"weixin-plan-{user['user_id']}",
            "reply_text": result.get("reply_text", ""),
            "reply_attachments": result.get("reply_attachments", []),
            "reply_actions": result.get("reply_actions", []),
            "pending_action": None,
            "last_event_candidates": [],
            "last_report_query": None,
        }
    )


def _build_weixin_plan_text(action: str, *, user_id: str, plan_date: str | None = None) -> str:
    if action == "summary":
        return build_daily_summary_text(learning_orchestrator.memory, user_id=user_id, plan_date=plan_date)
    return build_daily_plan_text(learning_orchestrator.memory, user_id=user_id, plan_date=plan_date)


def _push_weixin_plan_to_binding(
    binding: dict[str, Any],
    *,
    action: str,
    plan_date: str | None = None,
) -> dict[str, Any]:
    user_id = str(binding.get("user_id") or "").strip()
    if not user_id:
        raise ClawBridgeError("Weixin binding does not include a user_id.")
    text = _build_weixin_plan_text(action, user_id=user_id, plan_date=plan_date)
    sent = ClawBridgeClient().send_text(
        text=text,
        clawbot_instance_id=binding.get("clawbot_instance_id"),
        weixin_account_id=binding.get("weixin_account_id"),
    )
    return {
        "sent": sent,
        "text": text,
        "user_id": user_id,
    }


def _push_weixin_plan_to_all_bound(*, action: str, plan_date: str | None = None) -> dict[str, Any]:
    bindings = weixin_auth_store.list_bound_users()
    sent: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for binding in bindings:
        try:
            sent.append(_push_weixin_plan_to_binding(binding, action=action, plan_date=plan_date))
        except Exception as exc:
            failed.append(
                {
                    "user_id": binding.get("user_id"),
                    "message": str(exc),
                    "error_type": type(exc).__name__,
                }
            )
    return {
        "action": action,
        "total": len(bindings),
        "sent_count": len(sent),
        "failed_count": len(failed),
        "sent": sent,
        "failed": failed,
    }


@app.post("/api/weixin/plan/push-daily")
async def weixin_push_daily_plan(request: Request, payload: dict[str, Any] | None = None) -> JSONResponse:
    user = _require_web_user(request)
    data = payload or {}
    plan_date = str(data.get("plan_date") or "").strip() or None
    binding = weixin_auth_store.get_bind_status_for_user(str(user["user_id"]))
    if not binding.get("bound"):
        raise HTTPException(status_code=409, detail={"message": "Current account is not connected to Weixin ClawBot."})
    try:
        result = _push_weixin_plan_to_binding(binding, action="daily", plan_date=plan_date)
    except ClawBridgeError as exc:
        raise HTTPException(status_code=503, detail={"message": str(exc)}) from exc
    return JSONResponse({"ok": True, "module": "weixin_plan", "action": "push_daily", **result})


@app.post("/api/weixin/plan/push-summary")
async def weixin_push_daily_summary(request: Request, payload: dict[str, Any] | None = None) -> JSONResponse:
    user = _require_web_user(request)
    data = payload or {}
    plan_date = str(data.get("plan_date") or "").strip() or None
    binding = weixin_auth_store.get_bind_status_for_user(str(user["user_id"]))
    if not binding.get("bound"):
        raise HTTPException(status_code=409, detail={"message": "Current account is not connected to Weixin ClawBot."})
    try:
        result = _push_weixin_plan_to_binding(binding, action="summary", plan_date=plan_date)
    except ClawBridgeError as exc:
        raise HTTPException(status_code=503, detail={"message": str(exc)}) from exc
    return JSONResponse({"ok": True, "module": "weixin_plan", "action": "push_summary", **result})


@app.post("/api/learning/route")
async def learning_route(req: LearningRouteRequest, request: Request) -> JSONResponse:
    """Policy-only route — returns decision, does NOT execute.

    Set ``use_llm_policy=true`` to use the LLM policy agent (with automatic
    fallback to rule-based on error).
    """
    _ensure_learning_session_access(request, req.session_id)
    result = _learning_route(req.session_id, req.user_input, req.use_llm_policy)
    return JSONResponse(
        {
            "ok": True,
            "module": "learning",
            "action": "route",
            "result": result,
        }
    )


@app.post("/api/learning/execute")
async def learning_execute(req: LearningExecuteRequest, request: Request) -> JSONResponse:
    """Execute a policy decision — does NOT re-route."""
    _ensure_learning_session_access(request, req.session_id)
    result = learning_orchestrator.execute_policy_action(
        req.session_id, req.decision, req.user_input
    )
    return JSONResponse(
        {
            "ok": True,
            "module": "learning",
            "action": "execute",
            "result": result,
        }
    )


@app.post("/api/learning/step")
async def learning_step(req: LearningStepRequest, request: Request) -> JSONResponse:
    """Convenience: route(None) → execute (Tutor step).

    Does NOT call next_step directly — always goes through policy.
    Set ``use_llm_policy=true`` to use the LLM policy agent.
    """
    _ensure_learning_session_access(request, req.session_id)
    route_result = _learning_route(req.session_id, None, req.use_llm_policy)
    decision = route_result["decision"]
    execution = learning_orchestrator.execute_policy_action(
        req.session_id, decision, None
    )
    return JSONResponse(
        {
            "ok": True,
            "module": "learning",
            "action": "step",
            "result": {
                "state": route_result["state"],
                "decision": decision,
                "execution": execution,
            },
        }
    )


@app.post("/api/learning/interruption")
async def learning_interruption(req: LearningInterruptionRequest, request: Request) -> JSONResponse:
    """Convenience: route(interruption) → execute (QA).

    Does NOT call handle_interruption directly — always goes through policy.
    Set ``use_llm_policy=true`` to use the LLM policy agent.
    """
    _ensure_learning_session_access(request, req.session_id)
    user_input = {"type": "interruption_question", "text": req.question}
    route_result = _learning_route(req.session_id, user_input, req.use_llm_policy)
    decision = route_result["decision"]
    execution = learning_orchestrator.execute_policy_action(
        req.session_id, decision, user_input
    )
    return JSONResponse(
        {
            "ok": True,
            "module": "learning",
            "action": "interruption",
            "result": {
                "state": route_result["state"],
                "decision": decision,
                "execution": execution,
            },
        }
    )


@app.post("/api/learning/interruption/stream")
async def learning_interruption_stream(req: LearningInterruptionRequest, request: Request) -> StreamingResponse:
    """Stream QA progress events while preserving the route -> execute path."""

    _ensure_learning_session_access(request, req.session_id)

    async def event_stream():
        action = "interruption_stream"
        started_at = time.perf_counter()
        user_input = {"type": "interruption_question", "text": req.question}
        yield _sse(
            "start",
            {
                "ok": True,
                "module": "learning",
                "action": action,
                "phase": "received",
                "session_id": req.session_id,
                "message": "已收到中途提问，开始路由到 QA。",
                "elapsed_seconds": _elapsed(started_at),
            },
        )
        try:
            route_result = _learning_route(req.session_id, user_input, req.use_llm_policy)
            decision = route_result["decision"]
        except Exception as exc:
            yield _sse(
                "error",
                {
                    "ok": False,
                    "module": "learning",
                    "action": action,
                    "phase": "route",
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_seconds": _elapsed(started_at),
                },
            )
            return

        yield _sse(
            "route_ready",
            {
                "ok": True,
                "module": "learning",
                "action": action,
                "phase": "route_ready",
                "session_id": req.session_id,
                "decision": decision,
                "elapsed_seconds": _elapsed(started_at),
            },
        )
        exec_task = asyncio.create_task(
            asyncio.to_thread(
                learning_orchestrator.execute_policy_action,
                req.session_id,
                decision,
                user_input,
            )
        )
        messages = [
            "正在读取当前小节 handout / evidence。",
            "正在判断是否需要 targeted KAG search。",
            "正在生成证据约束的 QA 回答。",
            "正在更新学习画像与掌握度信号。",
        ]
        tick = 0
        while not exec_task.done():
            await asyncio.sleep(5)
            tick += 1
            yield _progress_event(
                action=action,
                phase="execute_qa",
                message=messages[(tick - 1) % len(messages)],
                started_at=started_at,
                tick=tick,
            )

        try:
            execution = await exec_task
        except Exception as exc:
            yield _sse(
                "error",
                {
                    "ok": False,
                    "module": "learning",
                    "action": action,
                    "phase": "execute_qa",
                    "session_id": req.session_id,
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_seconds": _elapsed(started_at),
                },
            )
            return

        yield _sse(
            "complete",
            {
                "ok": True,
                "module": "learning",
                "action": action,
                "elapsed_seconds": _elapsed(started_at),
                "result": {
                    "state": route_result["state"],
                    "decision": decision,
                    "execution": execution,
                },
            },
        )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers=_stream_headers(),
    )


@app.post("/api/learning/continue")
async def learning_continue(req: LearningContinueRequest, request: Request) -> JSONResponse:
    """Convenience: route(continue) -> execute (advance + next Tutor step)."""
    _ensure_learning_session_access(request, req.session_id)
    user_input = {"type": "continue"}
    route_result = _learning_route(req.session_id, user_input, req.use_llm_policy)
    decision = route_result["decision"]
    execution = learning_orchestrator.execute_policy_action(
        req.session_id, decision, user_input
    )
    return JSONResponse(
        {
            "ok": True,
            "module": "learning",
            "action": "continue",
            "result": {
                "state": route_result["state"],
                "decision": decision,
                "execution": execution,
            },
        }
    )


@app.post("/api/learning/continue/stream")
async def learning_continue_stream(req: LearningContinueRequest, request: Request) -> StreamingResponse:
    """Stream continue progress events while preserving the route -> execute path."""

    _ensure_learning_session_access(request, req.session_id)

    async def event_stream():
        action = "continue_stream"
        started_at = time.perf_counter()
        user_input = {"type": "continue"}
        yield _sse(
            "start",
            {
                "ok": True,
                "module": "learning",
                "action": action,
                "phase": "received",
                "session_id": req.session_id,
                "message": "已收到继续请求，开始进入下一节。",
                "elapsed_seconds": _elapsed(started_at),
            },
        )
        try:
            route_result = _learning_route(req.session_id, user_input, req.use_llm_policy)
            decision = route_result["decision"]
        except Exception as exc:
            yield _sse(
                "error",
                {
                    "ok": False,
                    "module": "learning",
                    "action": action,
                    "phase": "route",
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_seconds": _elapsed(started_at),
                },
            )
            return

        yield _sse(
            "route_ready",
            {
                "ok": True,
                "module": "learning",
                "action": action,
                "phase": "route_ready",
                "session_id": req.session_id,
                "decision": decision,
                "elapsed_seconds": _elapsed(started_at),
            },
        )
        exec_task = asyncio.create_task(
            asyncio.to_thread(
                learning_orchestrator.execute_policy_action,
                req.session_id,
                decision,
                user_input,
            )
        )
        messages = [
            "正在检查下一节 handout/tutor cache。",
            "如果后台已生成，将直接返回缓存讲解。",
            "如果缓存未就绪，正在等待或同步生成下一节。",
            "正在写入学习进度与事件。",
        ]
        tick = 0
        while not exec_task.done():
            await asyncio.sleep(5)
            tick += 1
            yield _progress_event(
                action=action,
                phase="execute_continue",
                message=messages[(tick - 1) % len(messages)],
                started_at=started_at,
                tick=tick,
            )

        try:
            execution = await exec_task
        except Exception as exc:
            yield _sse(
                "error",
                {
                    "ok": False,
                    "module": "learning",
                    "action": action,
                    "phase": "execute_continue",
                    "session_id": req.session_id,
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_seconds": _elapsed(started_at),
                },
            )
            return

        yield _sse(
            "complete",
            {
                "ok": True,
                "module": "learning",
                "action": action,
                "elapsed_seconds": _elapsed(started_at),
                "result": {
                    "state": route_result["state"],
                    "decision": decision,
                    "execution": execution,
                },
            },
        )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers=_stream_headers(),
    )


@app.post("/api/learning/answer")
async def learning_answer(req: LearningSubmitAnswerRequest, request: Request) -> JSONResponse:
    """Convenience: route(answer_submission) → execute (Assessment).

    Does NOT call submit_answer directly — always goes through policy.
    Set ``use_llm_policy=true`` to use the LLM policy agent.
    """
    _ensure_learning_session_access(request, req.session_id)
    user_input = {
        "type": "answer_submission",
        "question": req.question,
        "answer": req.answer,
    }
    route_result = _learning_route(req.session_id, user_input, req.use_llm_policy)
    decision = route_result["decision"]
    execution = learning_orchestrator.execute_policy_action(
        req.session_id, decision, user_input
    )
    return JSONResponse(
        {
            "ok": True,
            "module": "learning",
            "action": "answer",
            "result": {
                "state": route_result["state"],
                "decision": decision,
                "execution": execution,
            },
        }
    )


@app.post("/api/learning/answer/stream")
async def learning_answer_stream(req: LearningSubmitAnswerRequest, request: Request) -> StreamingResponse:
    """Stream assessment progress events while preserving route -> execute."""

    _ensure_learning_session_access(request, req.session_id)

    async def event_stream():
        action = "answer_stream"
        started_at = time.perf_counter()
        user_input = {
            "type": "answer_submission",
            "question": req.question,
            "answer": req.answer,
        }
        yield _sse(
            "start",
            {
                "ok": True,
                "module": "learning",
                "action": action,
                "phase": "received",
                "session_id": req.session_id,
                "message": "已收到小测答案，开始评估。",
                "elapsed_seconds": _elapsed(started_at),
            },
        )
        try:
            route_result = _learning_route(req.session_id, user_input, req.use_llm_policy)
            decision = route_result["decision"]
        except Exception as exc:
            yield _sse(
                "error",
                {
                    "ok": False,
                    "module": "learning",
                    "action": action,
                    "phase": "route",
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_seconds": _elapsed(started_at),
                },
            )
            return

        yield _sse(
            "route_ready",
            {
                "ok": True,
                "module": "learning",
                "action": action,
                "phase": "route_ready",
                "session_id": req.session_id,
                "decision": decision,
                "elapsed_seconds": _elapsed(started_at),
            },
        )
        exec_task = asyncio.create_task(
            asyncio.to_thread(
                learning_orchestrator.execute_policy_action,
                req.session_id,
                decision,
                user_input,
            )
        )
        messages = [
            "正在分析用户选项与正确答案。",
            "正在生成错因分析或正答解释。",
            "正在关联 KG node 掌握度信号。",
            "正在更新学习画像。",
        ]
        tick = 0
        while not exec_task.done():
            await asyncio.sleep(5)
            tick += 1
            yield _progress_event(
                action=action,
                phase="execute_assessment",
                message=messages[(tick - 1) % len(messages)],
                started_at=started_at,
                tick=tick,
            )

        try:
            execution = await exec_task
        except Exception as exc:
            yield _sse(
                "error",
                {
                    "ok": False,
                    "module": "learning",
                    "action": action,
                    "phase": "execute_assessment",
                    "session_id": req.session_id,
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_seconds": _elapsed(started_at),
                },
            )
            return

        yield _sse(
            "complete",
            {
                "ok": True,
                "module": "learning",
                "action": action,
                "elapsed_seconds": _elapsed(started_at),
                "result": {
                    "state": route_result["state"],
                    "decision": decision,
                    "execution": execution,
                },
            },
        )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers=_stream_headers(),
    )


@app.post("/api/router/chat")
async def router_chat(req: RouterChatRequest) -> JSONResponse:
    result = await master_router.route(
        message=req.message,
        user_id=req.context.get("user_id", "demo"),
        history=req.history,
        context=req.context,
    )
    return JSONResponse(result)


@app.post("/api/router/jobs", status_code=202)
async def create_router_job(req: RouterChatRequest) -> JSONResponse:
    """Start a replayable master-router job that survives page navigation."""
    result = router_job_manager.create(
        message=req.message,
        user_id=req.context.get("user_id", "demo"),
        history=req.history,
        context=req.context,
    )
    return JSONResponse(result, status_code=202)


@app.get("/api/router/jobs/{job_id}")
async def get_router_job(job_id: str, after: int = Query(default=0, ge=0)) -> JSONResponse:
    try:
        result = router_job_manager.snapshot(job_id, after=after)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Router job not found") from exc
    return JSONResponse(result)


@app.get("/api/router/jobs/{job_id}/stream")
async def stream_router_job(
    job_id: str,
    after: int = Query(default=0, ge=0),
) -> StreamingResponse:
    """Replay job events and continue streaming until the job is terminal."""
    try:
        router_job_manager.snapshot(job_id, after=after)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Router job not found") from exc

    async def event_stream():
        cursor = after
        idle_ticks = 0
        while True:
            snapshot = router_job_manager.snapshot(job_id, after=cursor)
            for event in snapshot["events"]:
                cursor = max(cursor, int(event["id"]))
                payload = json.dumps(event, ensure_ascii=False, default=str)
                yield f"id: {event['id']}\nevent: router_job\ndata: {payload}\n\n"
                idle_ticks = 0
            if snapshot["done"] and cursor >= snapshot["last_event_id"]:
                return
            await asyncio.sleep(0.5)
            idle_ticks += 1
            if idle_ticks >= 20:
                yield ": keep-alive\n\n"
                idle_ticks = 0

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers=_stream_headers(),
    )


@app.post("/api/agents/{agent_name}/invoke", response_model=GenericAgentResponse)
async def invoke_agent(agent_name: str, req: GenericAgentRequest, response: Response) -> GenericAgentResponse:
    if agent_name in {"oral_master_router_agent", "master_router", "oral_master"}:
        result = await master_router.route(
            message=req.message or req.query or req.question or req.task or req.text or "",
            user_id=req.context.get("user_id", "demo"),
            history=[],
            context=req.context,
        )
        return GenericAgentResponse(agent=agent_name, ok=result.get("ok", True), result=result)
    if agent_name in LEARNING_AGENT_ALIASES or agent_name in {"study_plan", "literature", "direct_qa"}:
        context = {**req.data, **req.context}
        if req.question and "question" not in context:
            context["question"] = req.question
        result = await master_router.dispatch_agent(
            agent_name,
            message=req.message or req.query or req.task or req.text or req.question or "",
            user_id=str(context.get("user_id") or "demo"),
            history=context.get("history") if isinstance(context.get("history"), list) else [],
            context=context,
        )
        response.status_code = 200 if result.get("ok", False) else 422
        return GenericAgentResponse(agent=agent_name, ok=result.get("ok", False), result=result)
    if agent_name in {"oral_pathology_assistant", "pathology_qa", "oral_pathology"}:
        question = req.question or req.query or req.task or req.message or req.text or ""
        result = await run_pathology_ask(question, req.top_k or 8, True if req.allow_web is None else req.allow_web)
        return GenericAgentResponse(agent=agent_name, ok=result.get("ok", True), result=result)
    if agent_name in {"oral_medicine_search_assistant", "oral_frontier_learning_assistant", "frontier_literature"}:
        query = req.query or req.task or req.question or req.message or req.text or ""
        result = await run_literature_search(query, req.months, req.limit or 5, include_chat_translations=True)
        return GenericAgentResponse(agent=agent_name, ok=result.get("ok", True), result=result)
    if agent_name in {"oral_cases_assistant", "decision_assistant"}:
        case_text = req.case_text or req.query or req.question or req.message or req.text or ""
        result = await run_decision_assist(case_text, req.top_k or 8, True if req.allow_web is None else req.allow_web)
        return GenericAgentResponse(agent=agent_name, ok=result.get("ok", True), result=result)
    if agent_name in {"clinic_case_stats_assistant", "clinic_stats"}:
        query = req.query or req.task or req.question or req.message or req.text or ""
        result = run_stats_query(query, req.top_k or 10)
        return GenericAgentResponse(agent=agent_name, ok=result.get("ok", True), result=result)
    if agent_name in {"oral_disease_image_recognition_assistant", "oral_image_assistant", "image_analysis"}:
        image_base64 = req.image_base64 or req.data.get("image_base64") or ""
        if not image_base64:
            response.status_code = 422
            return GenericAgentResponse(
                agent=agent_name,
                ok=False,
                result={"message": "image_base64 is required for image analysis."},
            )
        query = req.query or req.question or req.task or req.message or req.text or ""
        result = await run_image_analysis(
            image_base64=image_base64,
            query=query,
            allow_web=True if req.allow_web is None else req.allow_web,
        )
        response.status_code = 200 if result.get("ok", False) else 422
        return GenericAgentResponse(agent=agent_name, ok=result.get("ok", False), result=result)

    response.status_code = 501
    return GenericAgentResponse(
        agent=agent_name,
        ok=False,
        result={"message": INTERFACE_REMOVED_MESSAGE, "payload": req.model_dump()},
    )


@app.get("/api/learning/capabilities")
async def learning_capabilities() -> JSONResponse:
    """List the registered learning workflow and individually dispatchable agents."""
    return JSONResponse({"ok": True, "capabilities": master_router.capability_catalog()})


@app.post("/api/agents/{agent_name}/stream")
async def invoke_agent_stream(agent_name: str, req: GenericAgentRequest) -> StreamingResponse:
    async def event_stream():
        event = {
            "type": "interface_removed",
            "agent": agent_name,
            "message": INTERFACE_REMOVED_MESSAGE,
            "payload": req.model_dump(),
        }
        yield f"event: interface_removed\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_stream(), status_code=501, media_type="text/event-stream")


@app.post("/api/legacy/chat", response_model=GenericAgentResponse)
async def legacy_chat(req: GenericAgentRequest, response: Response) -> GenericAgentResponse:
    response.status_code = 501
    return GenericAgentResponse(
        agent="legacy_chat",
        ok=False,
        result={"message": INTERFACE_REMOVED_MESSAGE, "payload": req.model_dump()},
    )


@app.post("/mcp/tools/{tool_name}")
async def invoke_mcp_tool(tool_name: str, req: GenericAgentRequest) -> JSONResponse:
    result = await invoke_local_mcp_tool(tool_name, req)
    return JSONResponse(result, status_code=200 if result.get("ok", False) else 404)


@app.post("/mcp/agents/{agent_name}/stream")
async def stream_mcp_agent(agent_name: str, req: GenericAgentRequest) -> StreamingResponse:
    async def event_stream():
        event = {
            "type": "interface_removed",
            "agent": agent_name,
            "message": INTERFACE_REMOVED_MESSAGE,
            "payload": req.model_dump(),
        }
        yield f"event: interface_removed\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_stream(), status_code=501, media_type="text/event-stream")


async def _weixin_auto_push_job(*, action: str, env_name: str, default_time: str) -> None:
    while True:
        target_time = _parse_push_time(env_name, default_time)
        sleep_seconds = _seconds_until_next(target_time)
        await asyncio.sleep(sleep_seconds)
        plan_date = datetime.now(SHANGHAI_TZ).date().isoformat()
        try:
            result = await asyncio.to_thread(_push_weixin_plan_to_all_bound, action=action, plan_date=plan_date)
            print(
                f"[weixin-auto-push] action={action} date={plan_date} "
                f"sent={result['sent_count']} failed={result['failed_count']} total={result['total']}"
            )
        except Exception as exc:
            print(f"[weixin-auto-push] action={action} failed: {type(exc).__name__}: {exc}")
        await asyncio.sleep(60)


@app.on_event("startup")
async def _start_weixin_auto_push_jobs() -> None:
    if not _env_enabled("ORALSAGE_WEIXIN_AUTO_PUSH_ENABLED", True):
        app.state.weixin_auto_push_tasks = []
        return
    app.state.weixin_auto_push_tasks = [
        asyncio.create_task(
            _weixin_auto_push_job(
                action="daily",
                env_name="ORALSAGE_WEIXIN_DAILY_PUSH_TIME",
                default_time="08:00",
            )
        ),
        asyncio.create_task(
            _weixin_auto_push_job(
                action="summary",
                env_name="ORALSAGE_WEIXIN_SUMMARY_PUSH_TIME",
                default_time="21:30",
            )
        ),
    ]


@app.on_event("shutdown")
async def _stop_weixin_auto_push_jobs() -> None:
    tasks = list(getattr(app.state, "weixin_auto_push_tasks", []) or [])
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


if (FRONTEND_DIR / "assets").exists():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIR / "assets")), name="assets")


def _frontend_file_response(path: Path) -> FileResponse:
    return FileResponse(
        path,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/")
def index() -> FileResponse:
    return _frontend_file_response(FRONTEND_DIR / "index.html")


@app.get("/{full_path:path}")
def static_fallback(full_path: str) -> FileResponse:
    candidate = FRONTEND_DIR / full_path
    if candidate.exists() and candidate.is_file():
        return _frontend_file_response(candidate)
    return _frontend_file_response(FRONTEND_DIR / "index.html")
