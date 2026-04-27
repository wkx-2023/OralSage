from __future__ import annotations

from pathlib import Path
from typing import Any
import json

from fastapi import FastAPI, File, Form, Response, UploadFile
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
    LiteratureRequest,
    LiteratureTranslateRequest,
    PathologyRequest,
    ReportRequest,
    StatsRequest,
    TermsRequest,
    WorkflowRequest,
)
from .services.decision import run_decision_assist, run_evidence_search
from .services.literature import run_article_translation, run_literature_search, run_terms
from .services.record_workflow import correct_asr, extract_fields, generate_record, run_workflow


app = FastAPI(title="OralPath API Interfaces", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = PROJECT_ROOT / "frontend"
INTERFACE_REMOVED_MESSAGE = "Interface retained; implementation removed for rebuild."


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


@app.get("/api/system/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "mode": "partial-rebuild",
        "message": "Record generation workflow is implemented locally; other modules may still be interface stubs.",
    }


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
            "frontier_literature",
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
            "/api/image/analyze",
            "/api/image/analyze-upload",
            "/api/agents/{agent_name}/invoke",
            "/api/agents/{agent_name}/stream",
            "/api/legacy/chat",
            "/mcp/tools/{tool_name}",
            "/mcp/agents/{agent_name}/stream",
        ],
    }


@app.post("/api/pathology/ask")
async def pathology_ask(req: PathologyRequest) -> JSONResponse:
    return _stub("pathology_qa", req.model_dump())


@app.post("/api/decision/assist")
async def decision_assist(req: DecisionRequest) -> JSONResponse:
    return JSONResponse(await run_decision_assist(req.case_text, req.top_k, req.allow_web))


@app.post("/api/evidence/search")
async def evidence_search(req: EvidenceSearchRequest) -> JSONResponse:
    result = run_evidence_search(req.query, req.top_k)
    return JSONResponse(result)


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
    return _stub("clinic_stats", req.model_dump())


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
    payload = req.model_dump()
    payload["image_base64"] = "[removed from stub echo]" if req.image_base64 else None
    return _stub("image_analysis", payload)


@app.post("/api/image/analyze-upload")
async def image_analyze_upload(
    file: UploadFile = File(...),
    query: str | None = Form(default=None),
    allow_web: bool = Form(default=True),
) -> JSONResponse:
    return _stub(
        "image_analysis_upload",
        {
            "filename": file.filename,
            "content_type": file.content_type,
            "query": query,
            "allow_web": allow_web,
        },
    )


@app.post("/api/agents/{agent_name}/invoke", response_model=GenericAgentResponse)
async def invoke_agent(agent_name: str, req: GenericAgentRequest, response: Response) -> GenericAgentResponse:
    if agent_name in {"oral_medicine_search_assistant", "oral_frontier_learning_assistant", "frontier_literature"}:
        query = req.query or req.task or req.question or req.message or req.text or ""
        result = await run_literature_search(query, req.months, req.limit or 5)
        return GenericAgentResponse(agent=agent_name, ok=result.get("ok", True), result=result)
    if agent_name in {"oral_cases_assistant", "decision_assistant"}:
        case_text = req.case_text or req.query or req.question or req.message or req.text or ""
        result = await run_decision_assist(case_text, req.top_k or 8, True if req.allow_web is None else req.allow_web)
        return GenericAgentResponse(agent=agent_name, ok=result.get("ok", True), result=result)

    response.status_code = 501
    return GenericAgentResponse(
        agent=agent_name,
        ok=False,
        result={"message": INTERFACE_REMOVED_MESSAGE, "payload": req.model_dump()},
    )


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
    return _stub(f"mcp_tool:{tool_name}", req.model_dump())


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


if (FRONTEND_DIR / "assets").exists():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIR / "assets")), name="assets")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/{full_path:path}")
def static_fallback(full_path: str) -> FileResponse:
    candidate = FRONTEND_DIR / full_path
    if candidate.exists() and candidate.is_file():
        return FileResponse(candidate)
    return FileResponse(FRONTEND_DIR / "index.html")
