from __future__ import annotations

import base64
import os
from typing import Any

from .box_parser import extract_boxes, parse_model_output
from .vision_client import VisionConfigurationError, VisionRequestError, call_vision_model


SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/bmp"}


async def run_image_analysis(
    *,
    image_base64: str,
    query: str | None = None,
    allow_web: bool = True,
    filename: str | None = None,
    content_type: str | None = None,
) -> dict[str, Any]:
    del allow_web
    mime_type, raw_base64 = _normalize_image_input(image_base64, content_type)
    validation_error = _validate_base64_image(raw_base64, mime_type)
    if validation_error:
        return {
            "ok": False,
            "module": "image_analysis",
            "message": validation_error,
            "payload": {"filename": filename, "content_type": mime_type},
        }

    try:
        raw_output = call_vision_model(image_base64=raw_base64, mime_type=mime_type, query=query)
    except VisionConfigurationError as exc:
        return {
            "ok": False,
            "module": "image_analysis",
            "message": str(exc),
            "payload": {"required_env": "VISION_LLM_API_KEY"},
        }
    except VisionRequestError as exc:
        return {
            "ok": False,
            "module": "image_analysis",
            "message": f"Vision model request failed: {exc}",
        }

    structured, report_text = parse_model_output(raw_output)
    boxes = extract_boxes(structured)
    if not report_text:
        report_text = raw_output.strip()

    return {
        "ok": True,
        "module": "image_analysis",
        "result": {
            "report_text": report_text,
            "structured": structured,
            "boxes": boxes,
            "raw_model_output": raw_output,
            "filename": filename,
            "content_type": mime_type,
        },
    }


def image_size_limit_bytes() -> int:
    value = os.getenv("VISION_MAX_IMAGE_MB", "10")
    try:
        megabytes = max(1, min(int(value), 50))
    except ValueError:
        megabytes = 10
    return megabytes * 1024 * 1024


def _normalize_image_input(image_base64: str, content_type: str | None) -> tuple[str, str]:
    value = (image_base64 or "").strip()
    if value.startswith("data:") and ";base64," in value:
        header, value = value.split(";base64,", 1)
        content_type = header.removeprefix("data:") or content_type
    return content_type or "image/png", value


def _validate_base64_image(image_base64: str, mime_type: str) -> str | None:
    if mime_type not in SUPPORTED_IMAGE_TYPES:
        return f"Unsupported image content type: {mime_type}"
    if not image_base64:
        return "image_base64 is required."
    try:
        raw = base64.b64decode(image_base64, validate=True)
    except ValueError:
        return "image_base64 is not valid base64."
    if len(raw) > image_size_limit_bytes():
        limit_mb = image_size_limit_bytes() // 1024 // 1024
        return f"Image is too large. Limit is {limit_mb} MB."
    return None
