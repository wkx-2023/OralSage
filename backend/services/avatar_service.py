"""Public configuration for the iFLYTEK real-time avatar web client.

The browser may receive an application identifier and the selected avatar/voice,
but never receives an API secret. The provider SDK is loaded at runtime because
iFLYTEK supplies the distribution URL with the tenant grant.
"""

from __future__ import annotations

import os
import base64
import hashlib
import hmac
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from email.utils import format_datetime

from backend.services.env_loader import load_project_env


load_project_env()


def _env(*names: str) -> str:
    for name in names:
        value = str(os.getenv(name) or "").strip()
        if value:
            return value
    return ""


def _enabled() -> bool:
    return _env("XF_AVATAR_ENABLED").lower() not in {"0", "false", "no", "off", "disabled"}


@dataclass(frozen=True)
class XfyunAvatarConfig:
    enabled: bool
    app_id: str
    scene_id: str
    server_url: str
    avatar_id: str
    vcn: str
    protocol: str
    width: int
    height: int
    sdk_url: str
    api_key: str
    api_secret: str
    action_greet: str
    action_think: str
    action_cheer: str

    @classmethod
    def from_env(cls) -> "XfyunAvatarConfig":
        return cls(
            enabled=_enabled(),
            app_id=_env("XF_AVATAR_APP_ID", "XF_TTS_APP_ID"),
            scene_id=_env("XF_AVATAR_SCENE_ID"),
            server_url=_env("XF_AVATAR_SERVER_URL") or "wss://avatar.cn-huadong-1.xf-yun.com/v1/interact",
            avatar_id=_env("XF_AVATAR_ID") or "138805001",
            vcn=_env("XF_AVATAR_VCN", "XF_TTS_VCN") or "x4_lingxiaoqi_oral",
            protocol=_env("XF_AVATAR_PROTOCOL") or "xrtc",
            width=int(_env("XF_AVATAR_WIDTH") or "720"),
            height=int(_env("XF_AVATAR_HEIGHT") or "1280"),
            sdk_url=_env("XF_AVATAR_SDK_URL"),
            api_key=_env("XF_AVATAR_API_KEY"),
            api_secret=_env("XF_AVATAR_API_SECRET"),
            action_greet=_env("XF_AVATAR_ACTION_GREET"),
            action_think=_env("XF_AVATAR_ACTION_THINK"),
            action_cheer=_env("XF_AVATAR_ACTION_CHEER"),
        )

    def signed_url(self, now: datetime | None = None) -> str:
        """Build the short-lived Web API authorization URL without exposing secrets."""
        if not (self.server_url and self.api_key and self.api_secret):
            return ""
        parsed = urlsplit(self.server_url)
        if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
            return ""
        request_path = parsed.path or "/"
        if parsed.query:
            request_path = f"{request_path}?{parsed.query}"
        timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        date = format_datetime(timestamp, usegmt=True)
        signature_origin = f"host: {parsed.netloc}\ndate: {date}\nGET {request_path} HTTP/1.1"
        signature = base64.b64encode(
            hmac.new(self.api_secret.encode("utf-8"), signature_origin.encode("utf-8"), hashlib.sha256).digest()
        ).decode("utf-8")
        authorization_origin = (
            f'api_key="{self.api_key}", algorithm="hmac-sha256", '
            'headers="host date request-line", '
            f'signature="{signature}"'
        )
        authorization = base64.b64encode(authorization_origin.encode("utf-8")).decode("utf-8")
        # Match the provider SDK's Web API handshake exactly. Its client builds
        # this query without percent-encoding the base64 authorization value.
        query = f"authorization={authorization}&date={date}&host={parsed.netloc}"
        return f"{self.server_url}{'&' if parsed.query else '?'}{query}"

    def sdk_is_available(self) -> bool:
        """Verify locally-hosted ESM bundles before advertising the avatar as ready."""
        if self.sdk_url.startswith(("https://", "http://")):
            return True
        if not self.sdk_url.startswith("/assets/"):
            return False
        asset_path = Path(__file__).resolve().parents[2] / "frontend" / self.sdk_url.lstrip("/")
        return asset_path.is_file()

    def public_payload(self) -> dict[str, Any]:
        signed_url = self.signed_url()
        sdk_available = self.sdk_is_available()
        configured = bool(self.enabled and self.app_id and self.scene_id and sdk_available and signed_url)
        missing = [
            name
            for name, value in {
                "XF_AVATAR_APP_ID": self.app_id,
                "XF_AVATAR_SCENE_ID": self.scene_id,
                "XF_AVATAR_SDK_URL": self.sdk_url,
                "XF_AVATAR_API_KEY": self.api_key,
                "XF_AVATAR_API_SECRET": self.api_secret,
            }.items()
            if not value
        ]
        if self.sdk_url and not sdk_available:
            missing.append("avatar SDK package at XF_AVATAR_SDK_URL")
        return {
            "provider": "xfyun_avatar_web_sdk",
            "enabled": self.enabled,
            "configured": configured,
            "app_id": self.app_id,
            "scene_id": self.scene_id,
            "avatar_id": self.avatar_id,
            "vcn": self.vcn,
            "protocol": self.protocol,
            "width": self.width,
            "height": self.height,
            "sdk_url": self.sdk_url,
            "sdk_available": sdk_available,
            "signed_url": signed_url,
            "actions": {
                "greet": self.action_greet,
                "think": self.action_think,
                "cheer": self.action_cheer,
            },
            "missing": missing if self.enabled else [],
            "credentials_exposed": False,
        }


def get_public_avatar_config() -> dict[str, Any]:
    return XfyunAvatarConfig.from_env().public_payload()
