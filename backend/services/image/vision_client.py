from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from backend.services.env_loader import load_project_env

from .prompts import SYSTEM_PROMPT, build_user_prompt


load_project_env()

DEFAULT_VISION_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_VISION_MODEL = "qwen2.5-vl-7b-instruct"


class VisionConfigurationError(RuntimeError):
    pass


class VisionRequestError(RuntimeError):
    pass


def _env(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def _timeout_seconds() -> int:
    value = _env("VISION_LLM_TIMEOUT_SECONDS", "LLM_REQUEST_TIMEOUT_SECONDS", "QWEN_TIMEOUT_SECONDS")
    if not value:
        return 180
    try:
        return max(30, min(int(value), 300))
    except ValueError:
        return 180


def call_vision_model(
    *,
    image_base64: str,
    mime_type: str,
    query: str | None = None,
    max_tokens: int = 1800,
) -> str:
    api_key = _env("VISION_LLM_API_KEY", "QWEN_API_KEY", "DASHSCOPE_API_KEY")
    if not api_key:
        raise VisionConfigurationError("VISION_LLM_API_KEY is not configured.")

    base_url = _env("VISION_LLM_BASE_URL", "QWEN_BASE_URL", "DASHSCOPE_BASE_URL") or DEFAULT_VISION_BASE_URL
    model = _env("VISION_LLM_MODEL", "QWEN_VISION_MODEL") or DEFAULT_VISION_MODEL
    data_url = image_base64 if image_base64.startswith("data:") else f"data:{mime_type};base64,{image_base64}"

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": build_user_prompt(query)},
                ],
            },
        ],
        "temperature": 0.1,
        "max_tokens": max_tokens,
    }

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
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise VisionRequestError(f"{exc.code} {exc.reason}: {body}") from exc
    except (URLError, TimeoutError) as exc:
        raise VisionRequestError(str(exc)) from exc

    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise VisionRequestError(f"Unexpected vision model response shape: {data}") from exc
