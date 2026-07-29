"""iFLYTEK digital-human video generation for persisted lecture scripts."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from backend.services.env_loader import load_project_env
from backend.services.learning.lecture_script import get_lecture_script


load_project_env()

_PROMPT_PREFIX = "请准确讲解以下口腔医学内容，不编造诊疗建议："


def _env(name: str) -> str:
    return str(os.getenv(name) or "").strip()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class XfyunVideoConfig:
    app_id: str
    api_key: str
    api_secret: str
    create_url: str
    query_url: str
    timeout_seconds: int

    @classmethod
    def from_env(cls) -> "XfyunVideoConfig":
        return cls(
            app_id=_env("XF_VIDEO_APP_ID"),
            api_key=_env("XF_VIDEO_API_KEY"),
            api_secret=_env("XF_VIDEO_API_SECRET"),
            create_url=_env("XF_VIDEO_CREATE_URL") or "https://vms.cn-huadong-1.xf-yun.com/v1/private/video/generate",
            query_url=_env("XF_VIDEO_QUERY_URL") or "https://vms.cn-huadong-1.xf-yun.com/v1/private/video/query",
            timeout_seconds=max(5, int(_env("XF_VIDEO_TIMEOUT_SECONDS") or "45")),
        )

    def configured(self) -> bool:
        return bool(self.app_id and self.api_key and self.api_secret)


class XfyunVideoError(RuntimeError):
    pass


def _signed_url(url: str, config: XfyunVideoConfig) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise XfyunVideoError("Invalid iFLYTEK video API URL")
    date = format_datetime(datetime.now(timezone.utc), usegmt=True)
    path = parsed.path or "/"
    signature_origin = f"host: {parsed.netloc}\ndate: {date}\nPOST {path} HTTP/1.1"
    signature = base64.b64encode(
        hmac.new(config.api_secret.encode("utf-8"), signature_origin.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")
    authorization_origin = (
        f'api_key="{config.api_key}", algorithm="hmac-sha256", '
        'headers="host date request-line", '
        f'signature="{signature}"'
    )
    authorization = base64.b64encode(authorization_origin.encode("utf-8")).decode("utf-8")
    separator = "&" if parsed.query else "?"
    return f"{url}{separator}{urlencode({'authorization': authorization, 'date': date, 'host': parsed.netloc})}"


def _post(url: str, payload: dict[str, Any], config: XfyunVideoConfig) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        _signed_url(url, config),
        data=body,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=config.timeout_seconds) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise XfyunVideoError(f"iFLYTEK video HTTP {exc.code}: {raw[:240]}") from exc
    except URLError as exc:
        raise XfyunVideoError(f"iFLYTEK video connection failed: {exc.reason}") from exc
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise XfyunVideoError("iFLYTEK video returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise XfyunVideoError("iFLYTEK video returned an invalid response")
    header = result.get("header") if isinstance(result.get("header"), dict) else {}
    if header.get("code") not in (None, 0, "0"):
        raise XfyunVideoError(str(header.get("message") or "iFLYTEK video request failed"))
    return result


def _record_path(session_id: str, outline_node_id: str, resource_root: str | Path) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", str(outline_node_id or "")):
        raise XfyunVideoError("Invalid lesson section identifier")
    root = Path(resource_root).resolve()
    session_dir = (root / session_id).resolve()
    if root not in session_dir.parents:
        raise XfyunVideoError("Invalid learning session path")
    return session_dir / "avatar_videos" / f"{outline_node_id}.json"


def _write_record(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
        os.replace(temp_name, path)
    finally:
        Path(temp_name).unlink(missing_ok=True)


def get_avatar_video_record(session_id: str, outline_node_id: str, resource_root: str | Path) -> dict[str, Any]:
    config = XfyunVideoConfig.from_env()
    path = _record_path(session_id, outline_node_id, resource_root)
    if not path.exists():
        return {
            "status": "not_created" if config.configured() else "not_configured",
            "configured": config.configured(),
            "session_id": session_id,
            "outline_node_id": outline_node_id,
        }
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"status": "failed", "configured": config.configured(), "error": "video_record_read_failed"}
    return {**record, "configured": config.configured()}


def _script_prompt(script: dict[str, Any]) -> tuple[str, int]:
    narration = "\n".join(
        str(slide.get("narration") or slide.get("text") or "").strip()
        for slide in script.get("slides", [])
        if isinstance(slide, dict)
    ).strip()
    if not narration:
        raise XfyunVideoError("The lecture script has no narratable text")
    # The provider accepts at most 300 characters for both prompt and output.
    # Preserve the teaching instruction and trim at a natural sentence boundary
    # so a long section can still create a valid, concise recap video.
    source_limit = 300 - len(_PROMPT_PREFIX.encode("utf-8"))
    source_parts: list[str] = []
    source_bytes = 0
    for char in narration:
        char_bytes = len(char.encode("utf-8"))
        if source_bytes + char_bytes > source_limit:
            break
        source_parts.append(char)
        source_bytes += char_bytes
    source = "".join(source_parts)
    boundary = max(source.rfind(mark) for mark in "。！？；")
    if boundary >= max(20, len(source) // 2):
        source = source[:boundary + 1]
    prompt = f"{_PROMPT_PREFIX}{source}"
    return prompt, min(300, max(50, len(narration)))


def _payload_url(payload: dict[str, Any], field: str) -> str:
    """Read either documented URL fields or the provider's nested Base64 form."""
    value: Any = payload.get(field) or payload.get(f"{field}_url")
    if isinstance(value, dict):
        value = value.get(field) or value.get("url") or value.get(f"{field}_url")
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    if candidate.startswith(("https://", "http://")):
        return candidate
    try:
        decoded = base64.b64decode(candidate, validate=True).decode("utf-8").strip()
    except (ValueError, UnicodeDecodeError):
        return ""
    return decoded if decoded.startswith(("https://", "http://")) else ""


def _source_excerpt(session_id: str, outline_node_id: str, resource_root: str | Path) -> str:
    try:
        prompt, _ = _script_prompt(get_lecture_script(session_id, outline_node_id, resource_root))
    except XfyunVideoError:
        return ""
    return prompt.removeprefix(_PROMPT_PREFIX)


def create_avatar_video(session_id: str, outline_node_id: str, resource_root: str | Path) -> dict[str, Any]:
    config = XfyunVideoConfig.from_env()
    if not config.configured():
        raise XfyunVideoError("XF_VIDEO_APP_ID, XF_VIDEO_API_KEY and XF_VIDEO_API_SECRET must be configured")
    existing = get_avatar_video_record(session_id, outline_node_id, resource_root)
    if existing.get("status") in {"created", "processing", "completed"}:
        return existing
    script = get_lecture_script(session_id, outline_node_id, resource_root)
    prompt, word_count = _script_prompt(script)
    result = _post(config.create_url, {
        "header": {"app_id": config.app_id},
        "parameter": {"avatar": {"prompt": prompt, "word_count": word_count}},
    }, config)
    header = result.get("header") or {}
    task_id = str(header.get("task_id") or "").strip()
    if not task_id:
        raise XfyunVideoError("iFLYTEK video response did not include task_id")
    record = {
        "status": "created",
        "session_id": session_id,
        "outline_node_id": outline_node_id,
        "task_id": task_id,
        "source_script_updated_at": script.get("updated_at"),
        "source_script_slide_count": script.get("slide_count"),
        "created_at": _now(),
        "updated_at": _now(),
        "error": None,
    }
    _write_record(_record_path(session_id, outline_node_id, resource_root), record)
    return {**record, "configured": True}


def refresh_avatar_video(
    session_id: str,
    outline_node_id: str,
    resource_root: str | Path,
    refresh_completed: bool = False,
) -> dict[str, Any]:
    record = get_avatar_video_record(session_id, outline_node_id, resource_root)
    if record.get("status") not in {"created", "processing"} and not (
        refresh_completed and record.get("status") == "completed"
    ):
        return record
    config = XfyunVideoConfig.from_env()
    if not config.configured():
        return {**record, "status": "not_configured"}
    try:
        result = _post(config.query_url, {"header": {"app_id": config.app_id, "task_id": record["task_id"]}}, config)
        header = result.get("header") or {}
        task_status = str(header.get("task_status") or "")
        payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
        completed = task_status in {"3", "4"}
        next_record = {
            **record,
            "status": "completed" if completed else "processing",
            "provider_status": task_status,
            "updated_at": _now(),
            "error": None,
        }
        if completed:
            next_record.update({
                "video_url": _payload_url(payload, "video"),
                "audio_url": _payload_url(payload, "audio"),
                "text": str(payload.get("text") or payload.get("expanded_text") or "").strip()
                or _source_excerpt(session_id, outline_node_id, resource_root),
            })
        _write_record(_record_path(session_id, outline_node_id, resource_root), next_record)
        return {**next_record, "configured": True}
    except XfyunVideoError as exc:
        failed = {**record, "status": "failed", "error": str(exc), "updated_at": _now()}
        _write_record(_record_path(session_id, outline_node_id, resource_root), failed)
        return {**failed, "configured": True}
