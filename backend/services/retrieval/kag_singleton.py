from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from .models import EvidenceChunk


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_KAG_DIRS = (
    PROJECT_ROOT / "data" / "kb" / "kag_bge_m3",
    PROJECT_ROOT / "data" / "data" / "kb" / "kag_bge_m3",
)
DEFAULT_EMBEDDING_MODEL_DIR = PROJECT_ROOT / "models" / "embeddings" / "bge-m3"
DEFAULT_RERANKER_MODEL_DIR = PROJECT_ROOT / "models" / "rerankers" / "bge-reranker-v2-m3"

DEFAULT_DENSE_K = 30
DEFAULT_SPARSE_K = 30
DEFAULT_ALIAS_K = 12
DEFAULT_PPR_K = 50
DEFAULT_RERANK_K = 20


def _env_int(name: str, default: int, *, minimum: int = 1, maximum: int | None = None) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    value = max(minimum, value)
    if maximum is not None:
        value = min(value, maximum)
    return value


def resolve_kag_dir() -> Path:
    configured = os.getenv("ORALSAGE_KAG_DIR")
    candidates = [Path(configured)] if configured else []
    candidates.extend(DEFAULT_KAG_DIRS)
    required = ("manifest.json", "chunks.jsonl", "retrieval_corpus.jsonl", "kg_nodes.jsonl")
    checked: list[str] = []
    for candidate in candidates:
        candidate = candidate.expanduser().resolve()
        missing = [name for name in required if not (candidate / name).is_file()]
        if not missing:
            return candidate
        checked.append(f"{candidate} missing {', '.join(missing)}")
    raise FileNotFoundError("No valid KAG directory found: " + "; ".join(checked))


def _resolve_model_dir(env_name: str, default: Path, label: str) -> Path:
    value = os.getenv(env_name)
    path = Path(value).expanduser().resolve() if value else default
    if not path.exists():
        raise FileNotFoundError(f"{label} model directory does not exist: {path}")
    return path


def _load_solver_class():
    try:
        from scripts.data_pipeline.kag_solver import KAGSolver, SolverConfig
    except Exception as exc:  # pragma: no cover - import error is surfaced to caller.
        raise RuntimeError(f"KAGSolver import failed: {exc}") from exc
    return KAGSolver, SolverConfig


@lru_cache(maxsize=1)
def get_kag_solver():
    KAGSolver, SolverConfig = _load_solver_class()
    config = SolverConfig(
        kag_dir=resolve_kag_dir(),
        embedding_model_dir=_resolve_model_dir(
            "ORALSAGE_BGE_M3_DIR",
            DEFAULT_EMBEDDING_MODEL_DIR,
            "BGE-M3",
        ),
        reranker_model_dir=_resolve_model_dir(
            "ORALSAGE_RERANKER_DIR",
            DEFAULT_RERANKER_MODEL_DIR,
            "bge-reranker-v2-m3",
        ),
        enable_dense=True,
        require_reranker=True,
        dense_num_threads=_env_int("ORALSAGE_KAG_DENSE_THREADS", 4, minimum=1, maximum=16),
        reranker_max_length=_env_int("ORALSAGE_KAG_RERANKER_MAX_LENGTH", 512, minimum=128, maximum=2048),
        ppr_top_k=_env_int("ORALSAGE_KAG_PPR_K", DEFAULT_PPR_K, minimum=5, maximum=200),
    )
    return KAGSolver(config)


def reset_kag_solver_cache() -> None:
    get_kag_solver.cache_clear()


def kag_solver_status() -> dict[str, Any]:
    try:
        solver = get_kag_solver()
    except Exception as exc:
        return {
            "status": "unavailable",
            "error": f"{type(exc).__name__}: {exc}",
            "backend": "kag_solver_singleton",
        }
    return {
        "status": "ready",
        "backend": "kag_solver_singleton",
        "kag_dir": str(solver.kag_dir),
        "dense_enabled": bool(solver.config.enable_dense),
        "dense_provider": getattr(solver.encoder, "provider", None),
        "reranker": "bge-reranker-v2-m3",
        "default_dense_k": DEFAULT_DENSE_K,
        "default_sparse_k": DEFAULT_SPARSE_K,
        "default_ppr_k": DEFAULT_PPR_K,
        "default_rerank_k": _env_int("ORALSAGE_KAG_RERANK_K", DEFAULT_RERANK_K, minimum=1, maximum=30),
    }


def _chunk_to_evidence_chunk(item: dict[str, Any], solver: Any, retrieval_trace: dict[str, Any]) -> EvidenceChunk:
    chunk_id = str(item.get("doc_id") or item.get("chunk_id") or "")
    row = solver.chunks.get(chunk_id, {}) if chunk_id else {}
    row_metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}

    metadata: dict[str, Any] = dict(row_metadata)
    for key in (
        "core_node_ids",
        "source_span_ids",
        "heading_path",
        "chunk_role",
        "quality_flags",
        "domain_alignments",
    ):
        value = item.get(key) if key in item else row.get(key)
        if value not in (None, "", []):
            metadata[key] = value
    metadata["kag"] = {
        "doc_type": item.get("doc_type"),
        "source_layer": item.get("source_layer") or row.get("source_layer"),
        "matched_by": item.get("matched_by") or [],
        "rerank_score": item.get("rerank_score"),
        "graph_score": item.get("graph_score"),
        "retrieval_trace": retrieval_trace,
    }
    metadata["source_layer"] = item.get("source_layer") or row.get("source_layer")
    metadata["subject"] = item.get("subject") or row.get("subject")
    metadata["chapter"] = item.get("chapter") or row.get("chapter")
    metadata["page_start"] = row.get("page_start")
    metadata["page_end"] = row.get("page_end")

    text = str(row.get("text") or item.get("preview") or "").strip()
    source_type = str(item.get("source_type") or row.get("source_type") or "kag_chunk")
    source = str(
        row.get("subject")
        or item.get("subject")
        or row.get("source_title")
        or item.get("source_layer")
        or "KAG"
    )
    title = str(
        item.get("title")
        or row.get("source_title")
        or " / ".join(str(x) for x in (row.get("heading_path") or []) if str(x).strip())
        or chunk_id
    )
    path = str(row.get("source_title") or row.get("document_id") or "")

    return EvidenceChunk(
        id=chunk_id,
        text=text,
        source=source,
        source_type=source_type,
        title=title,
        path=path,
        score=float(item.get("rerank_score") or item.get("graph_score") or 0.0),
        metadata=metadata,
    )


def search_knowledge_base(
    query: str,
    top_k: int = 8,
    source_types: set[str] | None = None,
    *,
    task_type: str = "learning_retrieval",
    rerank_k: int | None = None,
) -> list[EvidenceChunk]:
    """Search the KAG knowledge base through a process-wide singleton solver.

    This is the backend-facing compatibility adapter. It keeps the teammate
    learning flow's ``search_knowledge_base -> list[EvidenceChunk]`` contract,
    while using the formal KAG chain underneath.
    """
    query = (query or "").strip()
    if not query:
        return []

    solver = get_kag_solver()
    payload = solver.query(
        query,
        task_type=task_type,
        top_k=max(1, int(top_k or 8)),
        dense_k=_env_int("ORALSAGE_KAG_DENSE_K", DEFAULT_DENSE_K, minimum=1, maximum=120),
        sparse_k=_env_int("ORALSAGE_KAG_SPARSE_K", DEFAULT_SPARSE_K, minimum=1, maximum=120),
        alias_k=_env_int("ORALSAGE_KAG_ALIAS_K", DEFAULT_ALIAS_K, minimum=1, maximum=40),
        ppr_k=_env_int("ORALSAGE_KAG_PPR_K", DEFAULT_PPR_K, minimum=5, maximum=200),
        rerank_k=rerank_k
        if rerank_k is not None
        else _env_int("ORALSAGE_KAG_RERANK_K", DEFAULT_RERANK_K, minimum=1, maximum=30),
        doc_types={"chunk"},
    )

    trace = payload.get("retrieval_trace") if isinstance(payload.get("retrieval_trace"), dict) else {}
    chunks = [
        _chunk_to_evidence_chunk(item, solver, trace)
        for item in payload.get("evidence_chunks", [])
        if isinstance(item, dict)
    ]
    if source_types:
        chunks = [chunk for chunk in chunks if chunk.source_type in source_types]
    return chunks[: max(1, int(top_k or 8))]
