from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from backend.services.env_loader import load_project_env

logger = logging.getLogger(__name__)


load_project_env()


DEFAULT_QWEN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_QWEN_MODEL = "qwen-plus"


class LLMConfigurationError(RuntimeError):
    pass


class LLMRequestError(RuntimeError):
    """Raised when an LLM request fails at the transport level.

    The original exception is chained via ``__cause__`` so that callers
    can inspect the underlying error class (e.g. :class:`TimeoutError`,
    :class:`urllib.error.HTTPError`) for fine-grained retry decisions.
    """


def _http_error_detail(exc: HTTPError) -> str:
    cached = getattr(exc, "_oralsage_response_body", None)
    if isinstance(cached, str):
        return cached
    try:
        detail = exc.read().decode("utf-8", errors="replace").strip()
    except Exception:
        detail = ""
    setattr(exc, "_oralsage_response_body", detail)
    return detail


def classify_llm_error(exc: Exception) -> str:
    """Classify an exception from an LLM transport failure into a stable category.

    Inspects the chained ``__cause__`` when *exc* is an
    :class:`LLMRequestError`, otherwise inspects *exc* directly.

    Returns one of these category strings:

    * ``"timeout"`` — the request exceeded its time limit
    * ``"auth"`` — HTTP 401 or 403 (do **not** blindly retry)
    * ``"unsupported"`` — HTTP 4xx whose response body mentions
      ``response_format`` / ``json_object`` (JSON mode is explicitly
      not supported by this provider)
    * ``"http_client_error"`` — HTTP 4xx without an explicit
      unsupported-format signal
    * ``"http_server_error"`` — HTTP 5xx
    * ``"network"`` — DNS / connection / reset error
    * ``"unknown"`` — fallback for anything else
    """
    from urllib.error import HTTPError, URLError

    # Unwrap LLMRequestError to inspect the transport-level cause
    cause = exc.__cause__ if isinstance(exc, LLMRequestError) else exc

    # -- timeout -----------------------------------------------------------
    if isinstance(cause, TimeoutError):
        return "timeout"

    # -- HTTP-level errors -------------------------------------------------
    if isinstance(cause, HTTPError):
        code = getattr(cause, "code", 0)
        if code in (401, 403):
            return "auth"

        # Inspect response body for explicit unsupported-format signals
        if 400 <= code < 500:
            body = _http_error_detail(cause).lower()
            if any(
                kw in body
                for kw in (
                    "response_format",
                    "json_object",
                    "not supported",
                    "unsupported",
                    "invalid parameter",
                )
            ):
                return "unsupported"
            return "http_client_error"

        if 500 <= code < 600:
            return "http_server_error"

        return "unknown"

    # -- network-level errors -----------------------------------------------
    if isinstance(cause, URLError):
        return "network"

    return "unknown"


def _http_error_message(exc: HTTPError) -> str:
    detail = _http_error_detail(exc)
    if detail:
        return f"{exc} | {detail[:500]}"
    return str(exc)


def _env(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def _timeout_seconds() -> int:
    value = _env("LLM_REQUEST_TIMEOUT_SECONDS", "QWEN_TIMEOUT_SECONDS", "DASHSCOPE_TIMEOUT_SECONDS")
    if not value:
        return 180
    try:
        return max(30, min(int(value), 300))
    except ValueError:
        return 180


def _json_probe_timeout_seconds() -> int:
    """Short timeout for JSON-mode compatibility probes.

    When a provider does not support ``response_format``, the request
    may hang until the full transport timeout.  A probe timeout that is
    much shorter than the default avoids minutes-long stalls while still
    giving the provider enough time to respond to a simple request.

    Configurable via ``LLM_JSON_PROBE_TIMEOUT_SECONDS`` (default 15 s,
    clamped to [5, 60]).
    """
    value = _env("LLM_JSON_PROBE_TIMEOUT_SECONDS")
    if not value:
        return 15
    try:
        return max(5, min(int(value), 60))
    except ValueError:
        return 15


def is_llm_configured(*api_key_names: str) -> bool:
    names = api_key_names or ("DECISION_LLM_API_KEY", "QWEN_API_KEY", "DASHSCOPE_API_KEY")
    return bool(_env(*names))


def chat_completion(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.15,
    max_tokens: int = 1800,
    api_key_names: tuple[str, ...] = ("DECISION_LLM_API_KEY", "QWEN_API_KEY", "DASHSCOPE_API_KEY"),
    base_url_names: tuple[str, ...] = ("DECISION_LLM_BASE_URL", "QWEN_BASE_URL", "DASHSCOPE_BASE_URL"),
    model_names: tuple[str, ...] = ("DECISION_LLM_MODEL", "QWEN_MODEL", "DASHSCOPE_MODEL"),
    response_format: dict[str, Any] | None = None,
    stream_callback: Callable[[str], None] | None = None,
    timeout: int | None = None,
) -> str:
    api_key = _env(*api_key_names)
    if not api_key:
        raise LLMConfigurationError(f"None of these API keys is configured: {', '.join(api_key_names)}")

    base_url = _env(*base_url_names) or DEFAULT_QWEN_BASE_URL
    model = _env(*model_names) or DEFAULT_QWEN_MODEL
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if response_format is not None:
        payload["response_format"] = response_format
    if stream_callback is not None:
        payload["stream"] = True

    effective_timeout = timeout if timeout is not None else _timeout_seconds()

    def _request_once(request_payload: dict[str, Any]) -> dict[str, Any] | str:
        request = Request(
            f"{base_url.rstrip('/')}/chat/completions",
            data=json.dumps(request_payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "OralPath-Rebuild/0.1",
            },
            method="POST",
        )
        with urlopen(request, timeout=effective_timeout) as response:
            if stream_callback is not None and _is_event_stream(response):
                return _read_streaming_content(response, stream_callback)
            return json.loads(response.read().decode("utf-8", errors="replace"))

    try:
        data = _request_once(payload)
    except HTTPError as exc:
        if exc.code == 400 and response_format is not None and "response_format" in payload:
            retry_payload = dict(payload)
            retry_payload.pop("response_format", None)
            try:
                data = _request_once(retry_payload)
            except (HTTPError, URLError, TimeoutError) as retry_exc:
                if isinstance(retry_exc, HTTPError):
                    raise LLMRequestError(_http_error_message(retry_exc)) from retry_exc
                raise LLMRequestError(str(retry_exc)) from retry_exc
        else:
            raise LLMRequestError(_http_error_message(exc)) from exc
    except (URLError, TimeoutError) as exc:
        raise LLMRequestError(str(exc)) from exc

    if isinstance(data, str):
        return data

    try:
        choice0 = data["choices"][0]
        msg = choice0["message"]
        content = msg.get("content")
        finish_reason = choice0.get("finish_reason") or msg.get("finish_reason")
        if content is None:
            logger.warning(
                "LLM response content is None. finish_reason=%s, "
                "model=%s, response_keys=%s, msg_keys=%s",
                finish_reason,
                model,
                sorted(data.keys()) if isinstance(data, dict) else type(data).__name__,
                sorted(msg.keys()) if isinstance(msg, dict) else type(msg).__name__,
            )
            return ""
        if not isinstance(content, str):
            logger.warning(
                "LLM response content is not a string: type=%s, value=%r",
                type(content).__name__,
                content,
            )
            content = str(content)
        content = content.strip()
        if not content:
            logger.warning(
                "LLM response content is empty string. finish_reason=%s, "
                "model=%s, choice_keys=%s",
                finish_reason,
                model,
                sorted(choice0.keys()) if isinstance(choice0, dict) else type(choice0).__name__,
            )
        if stream_callback is not None and content:
            stream_callback(content)
        return content
    except (KeyError, IndexError, TypeError) as exc:
        logger.warning(
            "Unexpected LLM response shape: %s",
            json.dumps(data, ensure_ascii=False, default=str)[:1000],
        )
        raise LLMRequestError(f"Unexpected LLM response shape: {data}") from exc


def _is_event_stream(response: Any) -> bool:
    headers = getattr(response, "headers", None)
    if headers is None:
        return False
    content_type = str(headers.get("Content-Type", "")).lower()
    return "text/event-stream" in content_type


def _read_streaming_content(
    response: Any,
    stream_callback: Callable[[str], None],
) -> str:
    chunks: list[str] = []
    for raw_line in response:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
            delta = event["choices"][0].get("delta", {}).get("content", "")
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise LLMRequestError(f"Unexpected streaming LLM response: {payload[:300]}") from exc
        if isinstance(delta, list):
            delta = "".join(
                str(item.get("text") or "")
                for item in delta
                if isinstance(item, dict)
            )
        if not isinstance(delta, str) or not delta:
            continue
        chunks.append(delta)
        stream_callback(delta)
    content = "".join(chunks).strip()
    if not content:
        raise LLMRequestError("Streaming LLM response contained no text content")
    return content
