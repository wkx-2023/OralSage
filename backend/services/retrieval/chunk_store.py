from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .models import EvidenceChunk


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "retrieval.yaml"
DEFAULT_KB_DIR = PROJECT_ROOT / "data" / "processed" / "knowledge_base"


def _load_config() -> dict[str, Any]:
    if not DEFAULT_CONFIG.exists():
        return {}
    with DEFAULT_CONFIG.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _configured_sources(config: dict[str, Any]) -> list[dict[str, Any]]:
    sources = config.get("sources") or []
    if isinstance(sources, list) and sources:
        return sources
    return [
        {"name": "clinical_cases", "path": "data/processed/knowledge_base/clinical_cases.jsonl", "source_type": "clinical_case"},
        {"name": "clinical_norms", "path": "data/processed/knowledge_base/clinical_norms.jsonl", "source_type": "guideline"},
        {"name": "oral_textbooks", "path": "data/processed/knowledge_base/oral_textbooks.jsonl", "source_type": "textbook"},
        {"name": "merck_manual", "path": "data/processed/knowledge_base/merck_manual.jsonl", "source_type": "general_reference"},
    ]


def _record_to_chunk(record: dict[str, Any], fallback_source: str, fallback_type: str, index: int) -> EvidenceChunk | None:
    raw_metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    metadata = dict(raw_metadata)
    text = str(
        record.get("text")
        or record.get("content")
        or record.get("chunk")
        or record.get("body")
        or ""
    ).strip()
    if not text:
        return None
    source = str(record.get("source") or record.get("source_name") or metadata.get("subject") or record.get("source_layer") or fallback_source)
    source_type = str(record.get("source_type") or metadata.get("source_type") or record.get("type") or fallback_type)
    title = str(record.get("title") or record.get("heading") or record.get("section") or metadata.get("chapter") or metadata.get("subject") or "")
    chunk_id = str(record.get("id") or record.get("chunk_id") or record.get("doc_id") or f"{fallback_source}:{index}")
    path = str(record.get("path") or record.get("file") or "")
    for key in (
        "page",
        "chapter",
        "section",
        "subsection",
        "content_type",
        "content_types",
        "entities",
        "quality_score",
        "parent_id",
        "chunk_index",
    ):
        if key in record and key not in metadata:
            metadata[key] = record.get(key)
    return EvidenceChunk(
        id=chunk_id,
        text=text,
        source=source,
        source_type=source_type,
        title=title,
        path=path,
        metadata=metadata,
    )


def _load_jsonl(path: Path, fallback_source: str, fallback_type: str) -> list[EvidenceChunk]:
    chunks: list[EvidenceChunk] = []
    if not path.exists():
        return chunks
    with path.open("r", encoding="utf-8") as fh:
        for index, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                chunk = _record_to_chunk(record, fallback_source, fallback_type, index)
                if chunk:
                    chunks.append(chunk)
    return chunks


def _load_raw_merck_fallback(limit: int = 2500) -> list[EvidenceChunk]:
    path = PROJECT_ROOT / "data" / "raw" / "merck_manual" / "msd_manual.jsonl"
    chunks: list[EvidenceChunk] = []
    if not path.exists():
        return chunks
    with path.open("r", encoding="utf-8") as fh:
        for index, line in enumerate(fh):
            if index >= limit:
                break
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            title = str(record.get("title") or "")
            fields = [
                record.get("introduction"),
                record.get("symptoms_and_signs"),
                record.get("diagnosis"),
                record.get("treatment"),
                record.get("key_points"),
            ]
            text = "\n".join(str(field).strip() for field in fields if str(field or "").strip())
            if not text:
                continue
            chunks.append(
                EvidenceChunk(
                    id=f"raw_merck:{index}",
                    text=text,
                    source="默沙东诊疗手册",
                    source_type="general_reference",
                    title=title,
                    path=str(path.relative_to(PROJECT_ROOT)),
                )
            )
    return chunks


@lru_cache(maxsize=1)
def load_chunks() -> tuple[EvidenceChunk, ...]:
    config = _load_config()
    chunks: list[EvidenceChunk] = []
    for source in _configured_sources(config):
        rel_path = str(source.get("path") or "")
        path = PROJECT_ROOT / rel_path
        chunks.extend(
            _load_jsonl(
                path,
                str(source.get("name") or path.stem),
                str(source.get("source_type") or "reference"),
            )
        )

    if not chunks:
        chunks.extend(_load_raw_merck_fallback())

    return tuple(chunks)


def reset_cache() -> None:
    load_chunks.cache_clear()
