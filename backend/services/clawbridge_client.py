"""HTTP client for the local OralSage ClawBridge service."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


class ClawBridgeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class QrStartResult:
    session_id: str
    qr_data_url: str
    message: str


@dataclass(frozen=True, slots=True)
class QrStatusResult:
    connected: bool
    status: str
    message: str
    qr_data_url: str | None = None
    weixin_account_id: str | None = None
    weixin_user_id: str | None = None
    clawbot_instance_id: str | None = None


def clawbridge_base_url() -> str:
    return os.getenv("CLAWBRIDGE_BASE_URL", "http://127.0.0.1:8787").strip().rstrip("/")


def clawbridge_internal_secret() -> str:
    return (
        os.getenv("CLAWBRIDGE_INTERNAL_SECRET")
        or os.getenv("ORALSAGE_CLAWBRIDGE_INTERNAL_SECRET")
        or os.getenv("AUTH_INTERNAL_OPENCLAW_SECRET")
        or ""
    ).strip()


def clawbridge_instance_id() -> str:
    return os.getenv("CLAWBRIDGE_INSTANCE_ID", "oralsage-clawbridge-local").strip() or "oralsage-clawbridge-local"


class ClawBridgeClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        internal_secret: str | None = None,
        instance_id: str | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        self.base_url = (base_url or clawbridge_base_url()).rstrip("/")
        self.internal_secret = clawbridge_internal_secret() if internal_secret is None else internal_secret.strip()
        self.instance_id = instance_id or clawbridge_instance_id()
        self.timeout_seconds = max(1.0, float(timeout_seconds))

    def start_qr_login(self, *, session_id: str) -> QrStartResult:
        payload = self._request(
            "POST",
            "/api/v1/weixin/qr/start",
            json_payload={"session_id": session_id},
        )
        qr_data_url = str(payload.get("qr_data_url") or "").strip()
        if not qr_data_url:
            raise ClawBridgeError("ClawBridge did not return a Weixin QR code.")
        return QrStartResult(
            session_id=str(payload.get("session_id") or session_id),
            qr_data_url=qr_data_url,
            message=str(payload.get("message") or "Scan the QR code with Weixin.").strip(),
        )

    def get_qr_status(self, *, session_id: str, timeout_ms: int = 1000) -> QrStatusResult:
        query = urllib.parse.urlencode({"session_id": session_id, "timeout_ms": max(1000, int(timeout_ms))})
        payload = self._request("GET", f"/api/v1/weixin/qr/status?{query}")
        return QrStatusResult(
            connected=bool(payload.get("connected")),
            status=str(payload.get("status") or "waiting").strip() or "waiting",
            message=str(payload.get("message") or "Waiting for the QR login to finish.").strip(),
            qr_data_url=_optional_string(payload.get("qr_data_url")),
            weixin_account_id=_optional_string(payload.get("weixin_account_id")),
            weixin_user_id=_optional_string(payload.get("weixin_user_id")),
            clawbot_instance_id=_optional_string(payload.get("clawbot_instance_id")),
        )

    def send_text(
        self,
        *,
        text: str,
        clawbot_instance_id: str | None = None,
        weixin_account_id: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/weixin/messages",
            json_payload={
                "text": text,
                "clawbot_instance_id": clawbot_instance_id,
                "weixin_account_id": weixin_account_id,
            },
        )

    def _request(self, method: str, path: str, json_payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None
        headers = {
            "X-AlertClaw-ClawBridge-Secret": self.internal_secret,
            "X-AlertClaw-ClawBridge-Instance-Id": self.instance_id,
        }
        if json_payload is not None:
            body = json.dumps(json_payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            message = _extract_error_message(raw) or f"ClawBridge request failed with status {exc.code}."
            raise ClawBridgeError(message) from exc
        except OSError as exc:
            raise ClawBridgeError(f"ClawBridge is unavailable: {exc}") from exc
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as exc:
            raise ClawBridgeError("ClawBridge returned a non-JSON response.") from exc
        if not isinstance(payload, dict):
            raise ClawBridgeError("ClawBridge returned an invalid response payload.")
        return payload


def _optional_string(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _extract_error_message(raw: str) -> str:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip()
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, dict):
            return str(detail.get("message") or detail.get("detail") or "").strip()
        return str(payload.get("message") or "").strip()
    return ""
