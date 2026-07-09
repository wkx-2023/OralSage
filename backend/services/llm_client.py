from __future__ import annotations

import json
import os
from typing import Any
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

    request = Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "OralPath-Rebuild/0.1",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=_timeout_seconds()) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
    except (HTTPError, URLError, TimeoutError) as exc:
        raise LLMRequestError(str(exc)) from exc

    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMRequestError(f"Unexpected LLM response shape: {data}") from exc
