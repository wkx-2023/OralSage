from __future__ import annotations

from typing import Any

from backend.agents.record_extraction import extract_record_fields
from backend.agents.record_fields import RecordFields
from backend.agents.record_generation import render_record_markdown
from backend.agents.record_llm_extraction import (
    LLMConfigurationError,
    LLMRequestError,
    configured as record_llm_configured,
    extract_record_fields_with_llm,
)
from backend.agents.voice_correction import correct_conversation


def _extract_fields_best_effort(corrected: str) -> tuple[RecordFields, dict[str, Any]]:
    if record_llm_configured():
        try:
            return extract_record_fields_with_llm(corrected)
        except (LLMConfigurationError, LLMRequestError, ValueError, TypeError) as exc:
            fields = extract_record_fields(corrected)
            return fields, {"extractor": "rules_fallback", "fallback_reason": str(exc)}
    fields = extract_record_fields(corrected)
    return fields, {"extractor": "rules", "fallback_reason": "record LLM is not configured"}


def correct_asr(conversation: str) -> dict[str, Any]:
    corrected = correct_conversation(conversation)
    return {
        "original_conversation": conversation,
        "corrected_conversation": corrected,
    }


def extract_fields(conversation: str) -> dict[str, Any]:
    corrected = correct_conversation(conversation)
    fields, meta = _extract_fields_best_effort(corrected)
    return {
        "corrected_conversation": corrected,
        "fields": fields.to_chinese(),
        "fields_normalized": fields.to_frontend(),
        "meta": meta,
    }


def generate_record(fields_payload: dict[str, Any] | None = None, conversation: str | None = None) -> dict[str, Any]:
    corrected = ""
    if fields_payload:
        fields = RecordFields.from_mapping(fields_payload)
        meta = {"extractor": "provided_fields"}
    else:
        corrected = correct_conversation(conversation or "")
        fields, meta = _extract_fields_best_effort(corrected)
    markdown = render_record_markdown(fields)
    return {
        "corrected_conversation": corrected,
        "fields": fields.to_chinese(),
        "fields_normalized": fields.to_frontend(),
        "markdown": markdown,
        "meta": meta,
    }


def run_workflow(conversation: str) -> dict[str, Any]:
    corrected = correct_conversation(conversation)
    fields, meta = _extract_fields_best_effort(corrected)
    markdown = render_record_markdown(fields)
    return {
        "original_conversation": conversation,
        "corrected_conversation": corrected,
        "fields": fields.to_chinese(),
        "fields_normalized": fields.to_frontend(),
        "markdown": markdown,
        "meta": meta,
    }
