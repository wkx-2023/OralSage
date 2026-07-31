"""Browser-safe configuration for iFLYTEK streaming speech recognition."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import format_datetime
from typing import Any
from urllib.parse import quote, urlsplit

from backend.services.env_loader import load_project_env


load_project_env()


def _env(*names: str) -> str:
    for name in names:
        value = str(os.getenv(name) or "").strip()
        if value:
            return value
    return ""


@dataclass(frozen=True)
class XfyunIatConfig:
    app_id: str
    api_key: str
    api_secret: str
    server_url: str
    language: str
    accent: str
    domain: str
    vad_eos: int

    @classmethod
    def from_env(cls) -> "XfyunIatConfig":
        try:
            vad_eos = int(_env("XF_IAT_VAD_EOS") or "10000")
        except ValueError:
            vad_eos = 10000
        return cls(
            # One iFLYTEK application may enable multiple services. Dedicated
            # IAT credentials take priority; avatar/TTS credentials are only
            # aliases for the same provider, never an alternate ASR engine.
            app_id=_env("XF_IAT_APP_ID", "XF_AVATAR_APP_ID", "XF_TTS_APP_ID"),
            api_key=_env("XF_IAT_API_KEY", "XF_AVATAR_API_KEY", "XF_TTS_API_KEY"),
            api_secret=_env("XF_IAT_API_SECRET", "XF_AVATAR_API_SECRET", "XF_TTS_API_SECRET"),
            server_url=_env("XF_IAT_SERVER_URL") or "wss://iat-api.xfyun.cn/v2/iat",
            language=_env("XF_IAT_LANGUAGE") or "zh_cn",
            accent=_env("XF_IAT_ACCENT") or "mandarin",
            domain=_env("XF_IAT_DOMAIN") or "iat",
            vad_eos=max(1000, min(vad_eos, 60000)),
        )

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.api_key and self.api_secret and self.server_url)

    def signed_url(self, now: datetime | None = None) -> str:
        if not self.configured:
            return ""
        parsed = urlsplit(self.server_url)
        if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
            return ""
        timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        date = format_datetime(timestamp, usegmt=True)
        request_path = parsed.path or "/"
        signature_origin = f"host: {parsed.netloc}\ndate: {date}\nGET {request_path} HTTP/1.1"
        signature = base64.b64encode(
            hmac.new(
                self.api_secret.encode("utf-8"),
                signature_origin.encode("utf-8"),
                hashlib.sha256,
            ).digest()
        ).decode("utf-8")
        authorization_origin = (
            f'api_key="{self.api_key}", algorithm="hmac-sha256", '
            f'headers="host date request-line", signature="{signature}"'
        )
        authorization = base64.b64encode(authorization_origin.encode("utf-8")).decode("utf-8")
        query = (
            f"authorization={quote(authorization, safe='')}"
            f"&date={quote(date, safe='')}"
            f"&host={quote(parsed.netloc, safe='')}"
        )
        separator = "&" if parsed.query else "?"
        return f"{self.server_url}{separator}{query}"

    def public_payload(self) -> dict[str, Any]:
        missing = [
            name
            for name, value in {
                "XF_IAT_APP_ID": self.app_id,
                "XF_IAT_API_KEY": self.api_key,
                "XF_IAT_API_SECRET": self.api_secret,
            }.items()
            if not value
        ]
        return {
            "provider": "xfyun_iat_websocket",
            "configured": self.configured,
            "app_id": self.app_id,
            "signed_url": self.signed_url(),
            "business": {
                "language": self.language,
                "domain": self.domain,
                "accent": self.accent,
                "dwa": "wpgs",
                "vad_eos": self.vad_eos,
            },
            "audio": {
                "format": "audio/L16;rate=16000",
                "encoding": "raw",
                "sample_rate": 16000,
            },
            "missing": missing,
            "credentials_exposed": False,
            "fallback_used": False,
        }


def get_public_iat_config() -> dict[str, Any]:
    return XfyunIatConfig.from_env().public_payload()
