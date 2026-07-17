"""Persistent-in-process jobs for the master chat experience.

The browser may leave the chat page while a route is still running.  A job
keeps ownership of that work on the server and exposes replayable progress
events so the UI can reconnect without starting the request again.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from uuid import uuid4

from backend.services.learning.audio import get_audio_manifest
from backend.services.learning.lecture_script import get_lecture_script


class _BufferedTextEmitter:
    def __init__(self, emit: Callable[[str], None]) -> None:
        self._emit = emit
        self._pending = ""
        self._emitted = ""
        self._guard = threading.RLock()

    @property
    def emitted_text(self) -> str:
        with self._guard:
            return self._emitted

    def push(self, delta: str) -> None:
        if not delta:
            return
        chunks: list[str] = []
        with self._guard:
            self._pending += str(delta)
            while True:
                boundary = _stream_chunk_boundary(self._pending)
                if boundary <= 0:
                    break
                chunk = self._pending[:boundary]
                self._pending = self._pending[boundary:]
                self._emitted += chunk
                chunks.append(chunk)
        for chunk in chunks:
            self._emit(chunk)

    def flush(self) -> None:
        with self._guard:
            chunk = self._pending
            self._pending = ""
            if chunk:
                self._emitted += chunk
        if chunk:
            self._emit(chunk)


def _stream_chunk_boundary(value: str) -> int:
    for index, char in enumerate(value):
        if index >= 11 and char in "。！？!?；;\n":
            return index + 1
    return 32 if len(value) >= 32 else 0


class RouterJobManager:
    def __init__(
        self,
        master_router: Any,
        learning_orchestrator: Any,
        *,
        ttl_seconds: float = 6 * 60 * 60,
    ) -> None:
        self.master_router = master_router
        self.learning_orchestrator = learning_orchestrator
        self.ttl_seconds = ttl_seconds
        self._jobs: dict[str, dict[str, Any]] = {}
        self._guard = threading.RLock()

    def create(
        self,
        *,
        message: str,
        user_id: str,
        history: list[dict[str, Any]] | None,
        context: dict[str, Any] | None,
    ) -> dict[str, Any]:
        self._cleanup()
        job_id = f"router_{uuid4().hex}"
        job = {
            "job_id": job_id,
            "status": "queued",
            "created_at": time.time(),
            "updated_at": time.time(),
            "events": [],
            "task": None,
        }
        with self._guard:
            self._jobs[job_id] = job
        self._emit(job_id, "queued", "请求已进入总控队列。")
        task = asyncio.create_task(
            self._run(
                job_id,
                message=message,
                user_id=user_id,
                history=history,
                context=context,
            )
        )
        with self._guard:
            self._jobs[job_id]["task"] = task
        return self.snapshot(job_id)

    def snapshot(self, job_id: str, *, after: int = 0) -> dict[str, Any]:
        with self._guard:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            events = [event for event in job["events"] if event["id"] > after]
            return {
                "job_id": job_id,
                "status": job["status"],
                "done": job["status"] in {"complete", "failed"},
                "created_at": job["created_at"],
                "updated_at": job["updated_at"],
                "last_event_id": job["events"][-1]["id"] if job["events"] else 0,
                "events": events,
            }

    def _emit(
        self,
        job_id: str,
        event_type: str,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        with self._guard:
            job = self._jobs[job_id]
            event_id = len(job["events"]) + 1
            job["events"].append(
                {
                    "id": event_id,
                    "type": event_type,
                    "message": message,
                    "data": data or {},
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            job["updated_at"] = time.time()
            if event_type == "complete":
                job["status"] = "complete"
            elif event_type == "error":
                job["status"] = "failed"
            elif job["status"] == "queued":
                job["status"] = "running"

    async def _run(
        self,
        job_id: str,
        *,
        message: str,
        user_id: str,
        history: list[dict[str, Any]] | None,
        context: dict[str, Any] | None,
    ) -> None:
        text_stream = _BufferedTextEmitter(
            lambda delta: self._emit(
                job_id,
                "content_delta",
                "正在生成正文。",
                {"channel": "teaching_text", "delta": delta},
            )
        )
        try:
            self._emit(job_id, "routing", "正在识别需求并选择工作流。")
            route_result = await self._await_with_progress(
                job_id,
                asyncio.create_task(
                    self.master_router.route(
                        message=message,
                        user_id=user_id,
                        history=history,
                        context=context,
                        stream_callback=text_stream.push,
                    )
                ),
                (
                    "正在读取对话上下文。",
                    "正在确认目标 Agent 与执行边界。",
                    "下游工作流正在准备结果。",
                ),
            )

            route = route_result.get("route") or {}
            final_result = route_result.get("final_result") or {}
            session_id = str((final_result.get("session") or {}).get("id") or "")
            standalone_generation = bool(final_result.get("standalone_generation"))
            if not session_id and route.get("source") in {
                "session_context",
                "learning_session_policy",
                "explicit_agent_dispatch",
            }:
                session_id = str((context or {}).get("session_id") or "").strip()
            self._emit(
                job_id,
                "route_ready",
                _route_message(route.get("intent")),
                {
                    "route": route,
                    "next_action": route_result.get("next_action"),
                    "session_id": None if standalone_generation else (session_id or None),
                },
            )

            learning_step: dict[str, Any] | None = None
            resource_status: dict[str, Any] | None = None
            if route_result.get("ok") and route.get("intent") == "learning" and session_id:
                already_advanced = route_result.get("next_action") in {
                    "continue_session",
                    "show_stage_exam",
                } or isinstance(final_result.get("tutor_step"), dict)
                if route.get("source") == "explicit_agent_dispatch":
                    target = str(route.get("target") or "")
                    if target in {"tutor", "lecture_script"} and isinstance(final_result, dict):
                        learning_step = {**final_result, "session_id": session_id}
                    elif target == "quiz" and isinstance(final_result, dict):
                        learning_step = {
                            "session_id": session_id,
                            "stage_exam": final_result.get("stage_exam") or final_result,
                        }
                elif route.get("source") == "session_context" or already_advanced:
                    nested_step = final_result.get("tutor_step")
                    learning_step = dict(nested_step) if isinstance(nested_step, dict) else {}
                    learning_step.update(
                        {
                            "session_id": session_id,
                            "lesson_completed": final_result.get("lesson_completed", False),
                            "stage_exam": final_result.get("stage_exam"),
                        }
                    )
                elif route.get("source") == "learning_session_policy":
                    learning_step = dict(final_result)
                    learning_step.setdefault("session_id", session_id)
                else:
                    self._emit(job_id, "learning", "课程大纲已就绪，正在生成本节讲稿。")
                    learning_step = await self._await_with_progress(
                        job_id,
                        asyncio.create_task(
                            asyncio.to_thread(
                                self.learning_orchestrator.next_step,
                                session_id,
                                stream_callback=text_stream.push,
                            )
                        ),
                        (
                            "正在读取本节讲义与教材证据。",
                            "Tutor 正在组织讲稿与互动题。",
                            "正在同步准备本节语音和演示文稿。",
                        ),
                    )
                try:
                    ppt_status = await asyncio.to_thread(
                        self.learning_orchestrator.wait_for_ppt_resources,
                        session_id,
                        timeout_seconds=0,
                    )
                    audio_status = await asyncio.to_thread(
                        get_audio_manifest,
                        session_id,
                        getattr(self.learning_orchestrator, "resource_root", None),
                    )
                    resource_status = {
                        "ppt": ppt_status,
                        "audio_manifest": audio_status,
                    }
                except Exception as exc:  # resources must not hide the lecture
                    resource_status = {
                        "status": "pending",
                        "error": f"{type(exc).__name__}: {exc}",
                    }

            text_stream.flush()
            final_stream_text = str((learning_step or {}).get("teaching_text") or "").strip()
            if (
                text_stream.emitted_text
                and final_stream_text
                and text_stream.emitted_text.strip() != final_stream_text
            ):
                self._emit(
                    job_id,
                    "content_replace",
                    "正文已完成校验。",
                    {"channel": "teaching_text", "text": final_stream_text},
                )

            self._emit(
                job_id,
                "complete",
                "结果已生成。",
                {
                    "router_result": _compact_router_result(route_result),
                    "learning_step": _compact_learning_step(
                        learning_step,
                        resource_root=getattr(
                            self.learning_orchestrator,
                            "resource_root",
                            None,
                        ),
                    ),
                    "resource_status": resource_status,
                },
            )
        except Exception as exc:
            self._emit(
                job_id,
                "error",
                "请求处理失败。",
                {"error": f"{type(exc).__name__}: {exc}"},
            )

    async def _await_with_progress(
        self,
        job_id: str,
        task: Awaitable[Any],
        messages: tuple[str, ...],
    ) -> Any:
        future = asyncio.ensure_future(task)
        tick = 0
        while True:
            done, _ = await asyncio.wait({future}, timeout=2.0)
            if done:
                return future.result()
            self._emit(
                job_id,
                "progress",
                messages[tick % len(messages)],
                {"tick": tick + 1},
            )
            tick += 1

    def _cleanup(self) -> None:
        cutoff = time.time() - self.ttl_seconds
        with self._guard:
            expired = [
                job_id
                for job_id, job in self._jobs.items()
                if job["updated_at"] < cutoff
                and job["status"] in {"complete", "failed"}
            ]
            for job_id in expired:
                self._jobs.pop(job_id, None)


def _route_message(intent: Any) -> str:
    return {
        "learning": "已进入学习模式。",
        "study_plan": "已进入计划模式。",
        "literature": "已进入文献模式。",
        "clarification": "需要补充需求信息。",
        "direct_qa": "已进入直接问答模式。",
    }.get(str(intent or ""), "总控已完成路由。")


def _compact_router_result(result: dict[str, Any]) -> dict[str, Any]:
    route = result.get("route") or {}
    final = result.get("final_result")
    if route.get("intent") != "learning" or not isinstance(final, dict):
        return result
    if result.get("next_action") == "select_plan" or final.get("candidates"):
        return result
    if route.get("source") == "explicit_agent_dispatch":
        return result
    if route.get("source") == "learning_session_policy":
        return result

    session = final.get("session") or {}
    outline = final.get("outline") or {}
    nodes = outline.get("nodes") or []
    compact_final = {
        "session": {
            "id": session.get("id"),
            "topic": session.get("topic"),
            "user_id": session.get("user_id"),
        },
        "outline": {
            "title": outline.get("title"),
            "nodes": [
                {"id": node.get("id"), "title": node.get("title")}
                for node in nodes
                if isinstance(node, dict)
            ],
        },
        "mindmap": final.get("mindmap"),
        "current_outline_node_id": final.get("current_outline_node_id"),
        "outline_status": final.get("outline_status"),
        "stage_exam": final.get("stage_exam"),
        "lesson_completed": final.get("lesson_completed", False),
    }
    return {
        "ok": result.get("ok", True),
        "route": route,
        "final_result": compact_final,
        "next_action": result.get("next_action"),
        "error": result.get("error"),
    }


def _compact_learning_step(
    step: dict[str, Any] | None,
    *,
    resource_root: Any = None,
) -> dict[str, Any] | None:
    if not isinstance(step, dict):
        return None
    session_id = step.get("session_id")
    outline_node_id = step.get("outline_node_id") or step.get("current_outline_node_id")
    return {
        "session_id": session_id,
        "outline_node_id": outline_node_id,
        "teaching_text": step.get("teaching_text"),
        "lecture_slides": _compact_lecture_slides(
            session_id=session_id,
            outline_node_id=outline_node_id,
            resource_root=resource_root,
            section_audio=step.get("section_audio"),
        ),
        "questions": step.get("questions") or [],
        "content_status": step.get("content_status"),
        "content_note": step.get("content_note"),
        "section_audio": step.get("section_audio"),
        "progress": step.get("progress"),
        "lesson_completed": step.get("lesson_completed", False),
        "stage_exam": step.get("stage_exam"),
    }


def _compact_lecture_slides(
    *,
    session_id: Any,
    outline_node_id: Any,
    resource_root: Any,
    section_audio: Any = None,
) -> list[dict[str, Any]]:
    sid = str(session_id or "").strip()
    node_id = str(outline_node_id or "").strip()
    if not sid or not node_id:
        return []

    script: dict[str, Any] = {}
    try:
        script = get_lecture_script(sid, node_id, resource_root)
    except Exception:
        script = {}

    slides = []
    if isinstance(script, dict) and _is_tutor_lecture_script(script):
        slides = script.get("slides")
    if not isinstance(slides, list) or not slides:
        audio_slides = (
            section_audio.get("slides")
            if isinstance(section_audio, dict)
            else []
        )
        if isinstance(audio_slides, list):
            slides = [
                slide for slide in audio_slides
                if isinstance(slide, dict) and _is_tutor_lecture_slide(slide)
            ]
    if not isinstance(slides, list):
        return []

    compact: list[dict[str, Any]] = []
    for index, slide in enumerate(slides, start=1):
        if not isinstance(slide, dict):
            continue
        text = str(slide.get("narration") or slide.get("text") or "").strip()
        if not text:
            continue
        compact.append({
            "slide_id": slide.get("slide_id") or f"slide_{index:03d}",
            "index": slide.get("index") or index,
            "kind": slide.get("kind") or "lecture",
            "title": slide.get("title") or f"讲稿 {index}",
            "text": text,
            "text_hash": slide.get("text_hash") or slide.get("script_hash"),
            "source_path": slide.get("source_path"),
        })
    return compact


def _is_tutor_lecture_script(script: dict[str, Any]) -> bool:
    if script.get("source") == "tutor_step.teaching_text":
        return True
    slides = script.get("slides")
    return isinstance(slides, list) and any(
        isinstance(slide, dict) and _is_tutor_lecture_slide(slide)
        for slide in slides
    )


def _is_tutor_lecture_slide(slide: dict[str, Any]) -> bool:
    return (
        slide.get("kind") == "tutor_teaching_text"
        or slide.get("source_path") == "tutor_step.teaching_text"
    )
