"""Slide-level audio generation for learning handout sections.

This module turns a generated ``handout_section`` into deterministic
"lecture slides" and, when configured, synthesizes one audio file per slide
with iFLYTEK Spark ultra-realistic TTS.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import formatdate
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

from backend.services.env_loader import load_project_env


load_project_env()


DEFAULT_AUDIO_ROOT = Path("data/learning/sessions")
DEFAULT_TTS_URL = "wss://cbm01.cn-huabei-1.xf-yun.com/v1/private/mcd9m97e6"
DEFAULT_TTS_VOICE = "x5_lingxiaoxuan_flow"
DEFAULT_TTS_ENCODING = "lame"


class AudioConfigurationError(RuntimeError):
    pass


class AudioSynthesisError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def _env_int(name: str, default: int, min_value: int, max_value: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(min_value, min(value, max_value))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _session_dir(session_id: str, resource_root: str | Path | None = None) -> Path:
    root = Path(resource_root) if resource_root else DEFAULT_AUDIO_ROOT
    return root / session_id


def _manifest_path(session_id: str, resource_root: str | Path | None = None) -> Path:
    return _session_dir(session_id, resource_root) / "audio_manifest.json"


def _audio_dir(session_id: str, resource_root: str | Path | None = None) -> Path:
    return _session_dir(session_id, resource_root) / "audio_files"


def _audio_ext(encoding: str) -> str:
    normalized = (encoding or DEFAULT_TTS_ENCODING).strip().lower()
    if normalized == "lame":
        return "mp3"
    if normalized == "raw":
        return "pcm"
    return normalized.replace("-", "_")


def _relpath(path: Path, base: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.as_posix()


def _clean_tts_text(text: str) -> str:
    text = str(text or "")
    text = text.replace("\t", " ")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[`*_>#\[\]{}|]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _short_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def build_handout_audio_slides(handout_section: dict[str, Any]) -> list[dict[str, Any]]:
    """Build deterministic audio slide records from a handout section."""
    content = handout_section.get("content", {}) or {}
    section_title = str(handout_section.get("title") or "").strip()
    slides: list[dict[str, Any]] = []

    def _add(kind: str, title: str, text: str, source_path: str) -> None:
        cleaned = _clean_tts_text(text)
        if not cleaned:
            return
        index = len(slides) + 1
        slides.append({
            "slide_id": f"slide_{index:03d}",
            "index": index,
            "kind": kind,
            "title": title,
            "text": cleaned,
            "text_chars": len(cleaned),
            "text_hash": _short_hash(cleaned),
            "source_path": source_path,
        })

    overview = content.get("overview", "")
    _add("overview", f"{section_title}：概述" if section_title else "概述", overview, "content.overview")

    key_points = content.get("key_points", [])
    if isinstance(key_points, list):
        for idx, kp in enumerate(key_points, start=1):
            if not isinstance(kp, dict):
                continue
            heading = str(kp.get("heading") or f"要点{idx}").strip()
            explanation = str(kp.get("explanation") or "").strip()
            text = f"要点{idx}：{heading}。{explanation}"
            slide_index = len(slides) + 1
            _add("key_point", heading, text, f"content.key_points[{idx - 1}]")
            if len(slides) == slide_index:
                refs = kp.get("evidence_refs", [])
                if isinstance(refs, list):
                    slides[-1]["evidence_refs"] = [str(ref) for ref in refs if str(ref)]

    clinical_example = content.get("clinical_example", "")
    _add("clinical_example", "临床例子", clinical_example, "content.clinical_example")

    confusions = content.get("common_confusions", [])
    if isinstance(confusions, list):
        for idx, item in enumerate(confusions, start=1):
            if not isinstance(item, dict):
                continue
            confusion = str(item.get("confusion") or "").strip()
            clarification = str(item.get("clarification") or "").strip()
            title = confusion or f"易混点{idx}"
            text = f"常见误区：{confusion}。正确理解：{clarification}"
            slide_index = len(slides) + 1
            _add("common_confusion", title, text, f"content.common_confusions[{idx - 1}]")
            if len(slides) == slide_index:
                refs = item.get("evidence_refs", [])
                if isinstance(refs, list):
                    slides[-1]["evidence_refs"] = [str(ref) for ref in refs if str(ref)]

    transition = content.get("transition_to_next", "")
    _add("transition", "过渡到下一节", transition, "content.transition_to_next")

    return slides


@dataclass(frozen=True)
class XfyunTTSConfig:
    url: str
    app_id: str
    api_key: str | None
    api_secret: str | None
    api_password: str | None
    voice: str
    encoding: str
    sample_rate: int
    channels: int
    bit_depth: int
    speed: int
    volume: int
    pitch: int
    oral_level: str
    spark_assist: int
    remain: int
    timeout_seconds: int

    @classmethod
    def from_env(cls) -> "XfyunTTSConfig":
        return cls(
            url=_env("XF_TTS_URL", "XF_SPARKOS_TTS_URL") or DEFAULT_TTS_URL,
            app_id=_env("XF_TTS_APP_ID", "XF_SPEECH_APP_ID", "XF_SPARKOS_APP_ID") or "",
            api_key=_env("XF_TTS_API_KEY", "XF_SPEECH_API_KEY", "XF_SPARKOS_API_KEY"),
            api_secret=_env("XF_TTS_API_SECRET", "XF_SPEECH_API_SECRET", "XF_SPARKOS_API_SECRET"),
            api_password=_env("XF_TTS_API_PASSWORD", "XF_SPARKOS_API_PASSWORD"),
            voice=_env("XF_TTS_VCN", "XF_SPARKOS_TTS_VCN") or DEFAULT_TTS_VOICE,
            encoding=(_env("XF_TTS_ENCODING", "XF_SPARKOS_TTS_ENCODING") or DEFAULT_TTS_ENCODING).lower(),
            sample_rate=_env_int("XF_TTS_SAMPLE_RATE", 24000, 8000, 24000),
            channels=_env_int("XF_TTS_CHANNELS", 1, 1, 1),
            bit_depth=_env_int("XF_TTS_BIT_DEPTH", 16, 16, 16),
            speed=_env_int("XF_TTS_SPEED", 50, 0, 100),
            volume=_env_int("XF_TTS_VOLUME", 50, 0, 100),
            pitch=_env_int("XF_TTS_PITCH", 50, 0, 100),
            oral_level=_env("XF_TTS_ORAL_LEVEL") or "mid",
            spark_assist=_env_int("XF_TTS_SPARK_ASSIST", 0, 0, 1),
            remain=_env_int("XF_TTS_REMAIN", 1, 0, 1),
            timeout_seconds=_env_int("XF_TTS_TIMEOUT_SECONDS", 90, 10, 300),
        )

    def configured(self) -> bool:
        if not self.app_id:
            return False
        if self.api_password:
            return True
        return bool(self.api_key and self.api_secret)

    def provider_meta(self) -> dict[str, Any]:
        return {
            "name": "xfyun_super_smart_tts",
            "url": self.url,
            "voice": self.voice,
            "encoding": self.encoding,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "bit_depth": self.bit_depth,
            "speed": self.speed,
            "volume": self.volume,
            "pitch": self.pitch,
        }


class XfyunSmartTTSClient:
    """Synchronous WebSocket client for iFLYTEK ultra-realistic TTS."""

    def __init__(self, config: XfyunTTSConfig | None = None) -> None:
        self.config = config or XfyunTTSConfig.from_env()

    def synthesize(self, text: str) -> bytes:
        if not self.config.configured():
            raise AudioConfigurationError("iFLYTEK TTS credentials are not configured")
        cleaned = _clean_tts_text(text)
        if not cleaned:
            return b""

        try:
            import websocket  # type: ignore
        except ImportError as exc:
            raise AudioConfigurationError(
                "websocket-client is required for iFLYTEK TTS"
            ) from exc

        url = self._connect_url()
        headers = []
        if self.config.api_password:
            headers.append(f"x-api-key: {self.config.api_password}")

        ws = websocket.create_connection(
            url,
            timeout=self.config.timeout_seconds,
            header=headers,
        )
        frames: list[tuple[int, bytes]] = []
        try:
            ws.send(json.dumps(self._request_body(cleaned), ensure_ascii=False))
            while True:
                raw = ws.recv()
                if not raw:
                    continue
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise AudioSynthesisError(f"invalid websocket message: {raw[:120]!r}") from exc

                header = message.get("header", {}) or {}
                code = int(header.get("code", 0) or 0)
                if code != 0:
                    raise AudioSynthesisError(
                        f"xfyun_tts_error code={code} message={header.get('message', '')}"
                    )

                audio = (message.get("payload", {}) or {}).get("audio", {}) or {}
                encoded = audio.get("audio")
                if encoded:
                    try:
                        frames.append((int(audio.get("seq", len(frames))), base64.b64decode(encoded)))
                    except Exception as exc:  # noqa: BLE001
                        raise AudioSynthesisError("invalid base64 audio frame") from exc

                if header.get("status") == 2 or audio.get("status") == 2:
                    break
        finally:
            try:
                ws.close()
            except Exception:
                pass

        frames.sort(key=lambda item: item[0])
        return b"".join(chunk for _, chunk in frames)

    def _connect_url(self) -> str:
        if self.config.api_password:
            return self.config.url
        if not self.config.api_key or not self.config.api_secret:
            raise AudioConfigurationError("api_key/api_secret missing")

        parsed = urlparse(self.config.url)
        host = parsed.netloc
        path = parsed.path or "/"
        date = formatdate(timeval=None, localtime=False, usegmt=True)
        signature_origin = f"host: {host}\ndate: {date}\nGET {path} HTTP/1.1"
        signature = hmac.new(
            self.config.api_secret.encode("utf-8"),
            signature_origin.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        signature_base64 = base64.b64encode(signature).decode("utf-8")
        authorization_origin = (
            f'api_key="{self.config.api_key}", algorithm="hmac-sha256", '
            f'headers="host date request-line", signature="{signature_base64}"'
        )
        authorization = base64.b64encode(
            authorization_origin.encode("utf-8")
        ).decode("utf-8")
        query = urlencode({
            "host": host,
            "date": date,
            "authorization": authorization,
        })
        separator = "&" if parsed.query else "?"
        return f"{self.config.url}{separator}{query}"

    def _request_body(self, text: str) -> dict[str, Any]:
        text_b64 = base64.b64encode(text.encode("utf-8")).decode("utf-8")
        return {
            "header": {
                "app_id": self.config.app_id,
                "status": 2,
            },
            "parameter": {
                "oral": {
                    "oral_level": self.config.oral_level,
                    "spark_assist": self.config.spark_assist,
                    "remain": self.config.remain,
                },
                "tts": {
                    "vcn": self.config.voice,
                    "speed": self.config.speed,
                    "volume": self.config.volume,
                    "pitch": self.config.pitch,
                    "bgs": 0,
                    "reg": 0,
                    "rdn": 0,
                    "rhy": 0,
                    "audio": {
                        "encoding": self.config.encoding,
                        "sample_rate": self.config.sample_rate,
                        "channels": self.config.channels,
                        "bit_depth": self.config.bit_depth,
                        "frame_size": 0,
                    },
                },
            },
            "payload": {
                "text": {
                    "encoding": "utf8",
                    "compress": "raw",
                    "format": "plain",
                    "status": 2,
                    "seq": 0,
                    "text": text_b64,
                },
            },
        }


def get_audio_manifest(
    session_id: str,
    resource_root: str | Path | None = None,
) -> dict[str, Any]:
    path = _manifest_path(session_id, resource_root)
    if not path.exists():
        return {
            "schema_version": "learning.audio_manifest.v1",
            "session_id": session_id,
            "status": "missing",
            "sections": [],
        }
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        return {
            "schema_version": "learning.audio_manifest.v1",
            "session_id": session_id,
            "status": "missing",
            "error": f"read_or_parse_error: {exc}",
            "sections": [],
        }
    return data if isinstance(data, dict) else {
        "schema_version": "learning.audio_manifest.v1",
        "session_id": session_id,
        "status": "missing",
        "error": "manifest_not_a_dict",
        "sections": [],
    }


def get_section_audio(
    session_id: str,
    outline_node_id: str,
    resource_root: str | Path | None = None,
) -> dict[str, Any] | None:
    manifest = get_audio_manifest(session_id, resource_root)
    for section in manifest.get("sections", []) or []:
        if isinstance(section, dict) and section.get("outline_node_id") == outline_node_id:
            return section
    return None


def ensure_handout_audio(
    *,
    session_id: str,
    outline_node_id: str,
    handout_section: dict[str, Any],
    source: str,
    resource_root: str | Path | None = None,
) -> dict[str, Any]:
    """Ensure slide-level audio manifest/files for one handout section."""
    config = XfyunTTSConfig.from_env()
    client = XfyunSmartTTSClient(config)
    session_dir = _session_dir(session_id, resource_root)
    audio_root = _audio_dir(session_id, resource_root)
    section_audio_dir = audio_root / outline_node_id
    manifest_path = _manifest_path(session_id, resource_root)
    encoding = config.encoding
    ext = _audio_ext(encoding)
    provider = config.provider_meta()

    slides = build_handout_audio_slides(handout_section)
    manifest = _load_or_init_manifest(
        session_id=session_id,
        session_dir=session_dir,
        audio_root=audio_root,
        provider=provider,
        resource_root=resource_root,
    )

    existing = _section_by_id(manifest, outline_node_id)
    existing_by_hash: dict[str, dict[str, Any]] = {}
    if existing:
        for slide in existing.get("slides", []) or []:
            if isinstance(slide, dict) and slide.get("text_hash"):
                existing_by_hash[str(slide["text_hash"])] = slide

    tts_enabled = _env_bool("LEARNING_AUDIO_TTS_ENABLED", True)
    configured = config.configured()
    section_audio_dir.mkdir(parents=True, exist_ok=True)

    slide_records: list[dict[str, Any]] = []
    for slide in slides:
        filename = f"{slide['slide_id']}.{ext}"
        audio_path = section_audio_dir / filename
        rel_audio = _relpath(audio_path, session_dir)
        old = existing_by_hash.get(slide["text_hash"])
        if old and old.get("status") == "ready":
            old_path = session_dir / str(old.get("audio_file", ""))
            if old_path.exists():
                slide_records.append({**slide, **_copy_audio_fields(old)})
                continue

        base_record = {
            **slide,
            "audio_file": rel_audio,
            "audio_path": str(audio_path),
            "format": ext,
            "encoding": encoding,
            "provider": provider["name"],
        }

        if not tts_enabled:
            slide_records.append({
                **base_record,
                "status": "skipped",
                "error": "LEARNING_AUDIO_TTS_ENABLED=false",
            })
            continue
        if not configured:
            slide_records.append({
                **base_record,
                "status": "skipped",
                "error": "xfyun_tts_not_configured",
            })
            continue

        try:
            audio_bytes = client.synthesize(slide["text"])
            if not audio_bytes:
                raise AudioSynthesisError("empty_audio")
            tmp = audio_path.with_suffix(audio_path.suffix + ".tmp")
            tmp.write_bytes(audio_bytes)
            os.replace(str(tmp), str(audio_path))
            slide_records.append({
                **base_record,
                "status": "ready",
                "error": None,
                "byte_size": len(audio_bytes),
            })
        except Exception as exc:  # noqa: BLE001 - audio must not block handout
            slide_records.append({
                **base_record,
                "status": "failed",
                "error": str(exc)[:500],
            })

    section_record = {
        "outline_node_id": outline_node_id,
        "title": handout_section.get("title", ""),
        "source": source,
        "status": _rollup_status(slide_records),
        "slide_count": len(slide_records),
        "audio_count": sum(1 for slide in slide_records if slide.get("status") == "ready"),
        "updated_at": _now(),
        "slides": slide_records,
    }
    _upsert_section(manifest, section_record)
    manifest["status"] = _rollup_status(manifest.get("sections", []))
    manifest["updated_at"] = _now()
    manifest["provider"] = provider
    _write_manifest(manifest_path, manifest)
    return section_record


def _load_or_init_manifest(
    *,
    session_id: str,
    session_dir: Path,
    audio_root: Path,
    provider: dict[str, Any],
    resource_root: str | Path | None,
) -> dict[str, Any]:
    manifest = get_audio_manifest(session_id, resource_root)
    if manifest.get("status") == "missing":
        return {
            "schema_version": "learning.audio_manifest.v1",
            "session_id": session_id,
            "status": "pending",
            "created_at": _now(),
            "updated_at": _now(),
            "audio_dir": _relpath(audio_root, session_dir),
            "provider": provider,
            "sections": [],
        }
    manifest.setdefault("schema_version", "learning.audio_manifest.v1")
    manifest.setdefault("session_id", session_id)
    manifest.setdefault("sections", [])
    manifest["audio_dir"] = _relpath(audio_root, session_dir)
    manifest["provider"] = provider
    return manifest


def _section_by_id(manifest: dict[str, Any], outline_node_id: str) -> dict[str, Any] | None:
    for section in manifest.get("sections", []) or []:
        if isinstance(section, dict) and section.get("outline_node_id") == outline_node_id:
            return section
    return None


def _copy_audio_fields(record: dict[str, Any]) -> dict[str, Any]:
    keys = {
        "audio_file",
        "audio_path",
        "format",
        "encoding",
        "provider",
        "status",
        "error",
        "byte_size",
    }
    return {key: record.get(key) for key in keys if key in record}


def _upsert_section(manifest: dict[str, Any], section_record: dict[str, Any]) -> None:
    sections = manifest.setdefault("sections", [])
    if not isinstance(sections, list):
        manifest["sections"] = sections = []
    for idx, section in enumerate(sections):
        if isinstance(section, dict) and section.get("outline_node_id") == section_record["outline_node_id"]:
            sections[idx] = section_record
            return
    sections.append(section_record)


def _rollup_status(records: list[dict[str, Any]]) -> str:
    if not records:
        return "empty"
    statuses = [str(record.get("status", "")) for record in records if isinstance(record, dict)]
    if statuses and all(status == "ready" for status in statuses):
        return "ready"
    if statuses and all(status == "skipped" for status in statuses):
        return "skipped"
    if any(status == "partial" for status in statuses):
        return "partial"
    if any(status == "ready" for status in statuses):
        return "partial"
    if any(status == "failed" for status in statuses):
        return "failed"
    if any(status == "skipped" for status in statuses):
        return "skipped"
    return "unknown"


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(str(tmp), str(path))
