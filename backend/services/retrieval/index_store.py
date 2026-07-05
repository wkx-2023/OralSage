from __future__ import annotations

import gzip
import hashlib
import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from .models import EvidenceChunk
from .text_features import tokenize


PROJECT_ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = PROJECT_ROOT / "configs" / "retrieval.yaml"

_OLD_KAG_PREFIX = Path("data/kb/kag_bge_m3")
_NEW_KAG_PREFIX = Path("data/data/kb/kag_bge_m3")


def _resolve_existing_path(path: Path) -> Path:
    """If *path* exists return it; otherwise try the fallback
    ``data/data/kb/kag_bge_m3`` prefix for paths under the old
    ``data/kb/kag_bge_m3`` prefix.
    """
    if path.exists():
        return path
    try:
        rel = path.relative_to(PROJECT_ROOT)
    except ValueError:
        return path
    try:
        suffix = rel.relative_to(_OLD_KAG_PREFIX)
        fallback = PROJECT_ROOT / _NEW_KAG_PREFIX / suffix
        if fallback.exists():
            return fallback
    except ValueError:
        pass
    return path


def _config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _path_from_config(*keys: str, default: str) -> Path:
    cfg = _config()
    node: Any = cfg
    for key in keys:
        node = node.get(key, {}) if isinstance(node, dict) else {}
    value = node if isinstance(node, str) else default
    return PROJECT_ROOT / value


def bm25_index_path() -> Path:
    base = _resolve_existing_path(
        _path_from_config("retrieval", "sparse_index", default="data/indexes/bm25")
    )
    default_path = base / "index.json.gz"
    if default_path.exists():
        return default_path
    manifest_path = base / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            manifest = {}
        configured_name = manifest.get("index")
        if isinstance(configured_name, str) and configured_name:
            return base / configured_name
    return default_path


def vector_index_dir() -> Path:
    return _resolve_existing_path(
        _path_from_config("retrieval", "vector_index", default="data/indexes/vector")
    )


def embedding_model_path() -> Path:
    return _path_from_config("embedding", "local_path", default="models/embeddings/bge-m3")


def _resolve_model_ref(model_ref: str) -> str | None:
    raw = str(model_ref or "").strip()
    candidates: list[Path] = []
    if raw:
        raw_path = Path(raw)
        candidates.append(raw_path)
        if not raw_path.is_absolute():
            candidates.append(PROJECT_ROOT / raw_path)

    configured = embedding_model_path()
    candidates.append(configured)

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return raw or None


@lru_cache(maxsize=1)
def load_bm25_index() -> dict[str, Any] | None:
    path = bm25_index_path()
    if not path.exists():
        return None
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache(maxsize=1)
def load_vector_index() -> tuple[dict[str, Any], np.ndarray] | None:
    base = vector_index_dir()
    manifest_path = base / "manifest.json"
    vectors_path = base / "vectors.npy"
    if not manifest_path.exists() or not vectors_path.exists():
        return None
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not manifest.get("doc_ids"):
        metadata_name = manifest.get("doc_metadata")
        metadata_path = base / str(metadata_name or "doc_metadata.json")
        if metadata_path.exists():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                metadata = {}
            doc_ids = metadata.get("doc_ids")
            if isinstance(doc_ids, list):
                manifest["doc_ids"] = doc_ids
    vectors = np.load(vectors_path, mmap_mode="r")
    return manifest, vectors


def bm25_scores_from_index(query: str, chunks_by_id: dict[str, EvidenceChunk], limit: int) -> list[tuple[float, EvidenceChunk]]:
    index = load_bm25_index()
    if not index:
        return []
    terms = list(dict.fromkeys(tokenize(query)))
    if not terms:
        return []

    doc_terms = index.get("doc_terms") or []
    if isinstance(doc_terms, list) and doc_terms and isinstance(doc_terms[0], dict):
        doc_ids = [str(item.get("doc_id") or "") for item in doc_terms]
        doc_lens = [int(item.get("length") or 0) for item in doc_terms]
    else:
        doc_ids = index.get("doc_ids") or []
        doc_lens = index.get("doc_lens") or []
    postings: dict[str, list[list[int | float]]] = index.get("postings") or {}
    df: dict[str, int] = index.get("df") or {}
    idf_values: dict[str, float] = index.get("idf") or {}
    avgdl = float(index.get("avgdl") or index.get("avg_doc_len") or 1.0)
    total_docs = int(index.get("total_docs") or len(doc_ids) or 1)
    k1 = float(index.get("k1") or 1.4)
    b = float(index.get("b") or 0.72)

    scores: dict[int, float] = {}
    for term in terms:
        term_postings = postings.get(term)
        if not term_postings:
            continue
        if term in idf_values:
            idf = float(idf_values.get(term) or 0.0)
        else:
            term_df = int(df.get(term) or len(term_postings))
            idf = math.log(1 + (total_docs - term_df + 0.5) / (term_df + 0.5))
        for doc_idx, freq in term_postings:
            idx = int(doc_idx)
            if idx >= len(doc_lens):
                continue
            dl = max(float(doc_lens[idx]), 1.0)
            tf = float(freq)
            denom = tf + k1 * (1 - b + b * dl / max(avgdl, 1.0))
            scores[idx] = scores.get(idx, 0.0) + idf * (tf * (k1 + 1)) / denom

    ranked: list[tuple[float, EvidenceChunk]] = []
    for idx, score in scores.items():
        if idx >= len(doc_ids):
            continue
        chunk = chunks_by_id.get(doc_ids[idx])
        if chunk:
            ranked.append((float(score), chunk))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[:limit]


def _hash_query_vector(query: str, dims: int) -> np.ndarray:
    vector = np.zeros(dims, dtype=np.float32)
    for token in tokenize(query):
        digest = hashlib.blake2b(token.encode("utf-8", errors="ignore"), digest_size=8).digest()
        value = int.from_bytes(digest, "little", signed=False)
        bucket = value % dims
        sign = 1.0 if (value >> 8) % 2 == 0 else -1.0
        vector[bucket] += sign
    norm = np.linalg.norm(vector)
    if norm > 0:
        vector /= norm
    return vector


@lru_cache(maxsize=1)
def _sentence_transformer_model(model_ref: str):
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(model_ref)


def _query_vector(query: str, manifest: dict[str, Any]) -> np.ndarray | None:
    dims = int(manifest.get("dims") or 384)
    provider = manifest.get("provider")
    if provider == "onnxruntime":
        model_ref = _resolve_model_ref(str(manifest.get("model_ref") or ""))
        if not model_ref:
            return None
        try:
            import onnxruntime as ort
            from transformers import AutoTokenizer

            tokenizer = AutoTokenizer.from_pretrained(model_ref, local_files_only=True)
            session = ort.InferenceSession(str(Path(model_ref) / "onnx" / "model.onnx"), providers=["CPUExecutionProvider"])
            encoded = tokenizer([query], padding=True, truncation=True, max_length=512, return_tensors="np")
            encoded = {key: value.astype(np.int64) for key, value in encoded.items()}
            input_names = {item.name for item in session.get_inputs()}
            feeds = {key: value for key, value in encoded.items() if key in input_names}
            output = session.run(None, feeds)[0]
            mask = encoded["attention_mask"].astype(np.float32)[..., None]
            pooled = (output * mask).sum(axis=1) / np.clip(mask.sum(axis=1), 1e-9, None)
            pooled = pooled / np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-9, None)
            return np.asarray(pooled[0], dtype=np.float32)
        except Exception:
            return None
    if provider == "sentence_transformers":
        model_ref = _resolve_model_ref(str(manifest.get("model_ref") or ""))
        if not model_ref:
            return None
        try:
            model = _sentence_transformer_model(model_ref)
            vector = model.encode([query], normalize_embeddings=True, show_progress_bar=False)[0]
            return np.asarray(vector, dtype=np.float32)
        except Exception:
            return None
    return _hash_query_vector(query, dims)


def vector_scores_from_index(query: str, chunks_by_id: dict[str, EvidenceChunk], limit: int) -> list[tuple[float, EvidenceChunk]]:
    loaded = load_vector_index()
    if not loaded:
        return []
    manifest, vectors = loaded
    query_vec = _query_vector(query, manifest)
    if query_vec is None:
        return []
    doc_ids: list[str] = manifest.get("doc_ids") or []
    if not doc_ids:
        return []
    scores = np.asarray(vectors @ query_vec, dtype=np.float32)
    if scores.size == 0:
        return []
    top_n = min(max(int(limit or 1), 1), scores.size)
    top_idx = np.argpartition(-scores, top_n - 1)[:top_n]
    ordered = top_idx[np.argsort(-scores[top_idx])]
    ranked: list[tuple[float, EvidenceChunk]] = []
    for idx in ordered:
        doc_id = doc_ids[int(idx)]
        chunk = chunks_by_id.get(doc_id)
        if chunk:
            ranked.append((float(scores[int(idx)]), chunk))
    return ranked


def reset_index_caches() -> None:
    load_bm25_index.cache_clear()
    load_vector_index.cache_clear()
    _sentence_transformer_model.cache_clear()
