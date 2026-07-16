from __future__ import annotations

import json
import os
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from backend.services.env_loader import load_project_env


load_project_env()


DEFAULT_QWEN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_QWEN_MODEL = "qwen-plus"


class LLMConfigurationError(RuntimeError):
    pass


class LLMRequestError(RuntimeError):
    pass


def _http_error_message(exc: HTTPError) -> str:
    detail = ""
    try:
        detail = exc.read().decode("utf-8", errors="replace").strip()
    except Exception:
        detail = ""
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
        with urlopen(request, timeout=_timeout_seconds()) as response:
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
        content = data["choices"][0]["message"]["content"].strip()
        if stream_callback is not None and content:
            stream_callback(content)
        return content
    except (KeyError, IndexError, TypeError) as exc:
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
