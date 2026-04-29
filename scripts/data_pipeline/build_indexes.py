from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import gzip
import hashlib
import json
import shutil
from pathlib import Path
import sys
from typing import Any

import numpy as np
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.services.retrieval.chunk_store import load_chunks, reset_cache
from backend.services.retrieval.text_features import tokenize


CONFIG_PATH = PROJECT_ROOT / "configs" / "retrieval.yaml"


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def configured_path(config: dict[str, Any], section: str, key: str, default: str) -> Path:
    value = ((config.get(section) or {}).get(key) or default)
    return PROJECT_ROOT / str(value)


def document_text(chunk) -> str:
    metadata = chunk.metadata or {}
    pieces = [
        chunk.source,
        chunk.title,
        metadata.get("chapter"),
        metadata.get("section"),
        " ".join(str(item) for item in metadata.get("entities") or [] if item),
        chunk.text,
    ]
    return "\n".join(str(piece) for piece in pieces if piece)


def build_bm25(config: dict[str, Any], max_doc_freq_ratio: float = 0.72) -> dict[str, Any]:
    chunks = load_chunks()
    doc_ids: list[str] = []
    doc_lens: list[int] = []
    postings_map: dict[str, list[tuple[int, int]]] = defaultdict(list)
    df: Counter[str] = Counter()

    for chunk in chunks:
        counts = Counter(tokenize(document_text(chunk)))
        if not counts:
            continue
        doc_idx = len(doc_ids)
        doc_ids.append(chunk.id)
        doc_lens.append(sum(counts.values()))
        for term, freq in counts.items():
            df[term] += 1
            postings_map[term].append((doc_idx, int(freq)))

    total_docs = len(doc_ids)
    max_df = max(1, int(total_docs * max_doc_freq_ratio))
    postings = {
        term: values
        for term, values in postings_map.items()
        if 1 <= df[term] <= max_df
    }
    filtered_df = {term: int(df[term]) for term in postings}
    avgdl = sum(doc_lens) / max(total_docs, 1)

    index = {
        "version": 1,
        "provider": "local_bm25",
        "k1": 1.4,
        "b": 0.72,
        "total_docs": total_docs,
        "avgdl": avgdl,
        "doc_ids": doc_ids,
        "doc_lens": doc_lens,
        "df": filtered_df,
        "postings": postings,
    }

    out_dir = configured_path(config, "retrieval", "sparse_index", "data/indexes/bm25")
    out_dir.mkdir(parents=True, exist_ok=True)
    with gzip.open(out_dir / "index.json.gz", "wt", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False)
    (out_dir / "manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "provider": "local_bm25",
                "total_docs": total_docs,
                "terms": len(postings),
                "path": "index.json.gz",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return {"docs": total_docs, "terms": len(postings), "path": str(out_dir / "index.json.gz")}


def stable_hash_vector(text: str, dims: int) -> np.ndarray:
    vector = np.zeros(dims, dtype=np.float32)
    for token in tokenize(text):
        digest = hashlib.blake2b(token.encode("utf-8", errors="ignore"), digest_size=8).digest()
        value = int.from_bytes(digest, "little", signed=False)
        bucket = value % dims
        sign = 1.0 if (value >> 8) % 2 == 0 else -1.0
        vector[bucket] += sign
    norm = np.linalg.norm(vector)
    if norm > 0:
        vector /= norm
    return vector


def resolve_sentence_transformer(config: dict[str, Any], allow_download: bool) -> tuple[Any | None, str | None]:
    embedding = config.get("embedding") or {}
    local_path = PROJECT_ROOT / str(embedding.get("local_path") or "")
    model_name = str(embedding.get("model_name") or "BAAI/bge-m3")
    model_ref = str(local_path) if local_path.exists() and any(local_path.iterdir()) else None
    if not model_ref and allow_download:
        model_ref = model_name
    if not model_ref:
        return None, None
    try:
        from sentence_transformers import SentenceTransformer

        return SentenceTransformer(model_ref), model_ref
    except Exception:
        return None, None


def resolve_onnx_model(config: dict[str, Any]) -> tuple[Any | None, Any | None, str | None]:
    embedding = config.get("embedding") or {}
    local_path = PROJECT_ROOT / str(embedding.get("local_path") or "")
    onnx_path = local_path / "onnx" / "model.onnx"
    if not onnx_path.exists():
        return None, None, None
    try:
        import onnxruntime as ort
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(str(local_path), local_files_only=True)
        session = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
        return tokenizer, session, str(local_path)
    except Exception:
        return None, None, None


def encode_onnx(tokenizer: Any, session: Any, texts: list[str], batch_size: int, max_length: int = 512) -> np.ndarray:
    vectors: list[np.ndarray] = []
    input_names = {item.name for item in session.get_inputs()}
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        encoded = tokenizer(
            batch,
            padding=True,
            truncation=True,
            max_length=max_length,
            return_tensors="np",
        )
        encoded = {key: value.astype(np.int64) for key, value in encoded.items()}
        feeds = {key: value for key, value in encoded.items() if key in input_names}
        output = session.run(None, feeds)[0]
        mask = encoded["attention_mask"].astype(np.float32)[..., None]
        pooled = (output * mask).sum(axis=1) / np.clip(mask.sum(axis=1), 1e-9, None)
        pooled = pooled / np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-9, None)
        vectors.append(pooled.astype(np.float32))
        if start and start % max(batch_size * 100, 1) == 0:
            print(f"encoded {start}/{len(texts)}")
    return np.vstack(vectors).astype(np.float32)


def doc_ids_digest(doc_ids: list[str]) -> str:
    digest = hashlib.sha256()
    for doc_id in doc_ids:
        digest.update(doc_id.encode("utf-8", errors="ignore"))
        digest.update(b"\0")
    return digest.hexdigest()


def onnx_dims(config: dict[str, Any], default: int = 1024) -> int:
    embedding = config.get("embedding") or {}
    local_path = PROJECT_ROOT / str(embedding.get("local_path") or "")
    config_path = local_path / "config.json"
    if not config_path.exists():
        return default
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        return int(data.get("hidden_size") or default)
    except Exception:
        return default


def write_final_vector_index(
    out_dir: Path,
    vectors: np.ndarray,
    provider: str,
    model_ref: str | None,
    doc_ids: list[str],
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    np.save(out_dir / "vectors.npy", np.asarray(vectors, dtype=np.float32))
    manifest = {
        "version": 1,
        "provider": provider,
        "model_ref": model_ref,
        "dims": int(vectors.shape[1]),
        "total_docs": len(doc_ids),
        "doc_ids": doc_ids,
        "doc_ids_digest": doc_ids_digest(doc_ids),
        "normalize": True,
        "vectors": "vectors.npy",
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"docs": len(doc_ids), "dims": int(vectors.shape[1]), "provider": provider, "path": str(out_dir / "vectors.npy")}


def load_progress(progress_path: Path) -> dict[str, Any] | None:
    if not progress_path.exists():
        return None
    try:
        data = json.loads(progress_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def progress_matches(progress: dict[str, Any] | None, provider: str, model_ref: str | None, dims: int, doc_ids: list[str]) -> bool:
    if not progress:
        return False
    return (
        progress.get("provider") == provider
        and progress.get("model_ref") == model_ref
        and int(progress.get("dims") or 0) == int(dims)
        and int(progress.get("total_docs") or 0) == len(doc_ids)
        and progress.get("doc_ids_digest") == doc_ids_digest(doc_ids)
    )


def vector_encoder(
    config: dict[str, Any],
    allow_download: bool,
    hash_dims: int,
) -> tuple[str, str | None, int, Any]:
    tokenizer, onnx_session, onnx_ref = resolve_onnx_model(config)
    if onnx_session is not None:
        dims = onnx_dims(config)

        def encode(texts: list[str], batch_size: int) -> np.ndarray:
            return encode_onnx(tokenizer, onnx_session, texts, batch_size)

        return "onnxruntime", onnx_ref, dims, encode

    model, model_ref = resolve_sentence_transformer(config, allow_download)
    if model is not None:
        probe = model.encode(["dimension probe"], normalize_embeddings=True, show_progress_bar=False)
        dims = int(np.asarray(probe).shape[1])

        def encode(texts: list[str], batch_size: int) -> np.ndarray:
            return np.asarray(
                model.encode(
                    texts,
                    batch_size=batch_size,
                    normalize_embeddings=True,
                    show_progress_bar=False,
                ),
                dtype=np.float32,
            )

        return "sentence_transformers", model_ref, dims, encode

    def encode(texts: list[str], batch_size: int) -> np.ndarray:
        return np.vstack([stable_hash_vector(text, hash_dims) for text in texts]).astype(np.float32)

    return "stable_hashing", "stable_hash_token_vector", hash_dims, encode


def build_vector(
    config: dict[str, Any],
    dims: int,
    batch_size: int,
    allow_download: bool,
    *,
    force_vector: bool = False,
    vector_index_override: Path | None = None,
    limit_docs: int | None = None,
    max_batches: int | None = None,
) -> dict[str, Any]:
    chunks = load_chunks()
    if limit_docs:
        chunks = chunks[: max(1, int(limit_docs))]
    texts = [document_text(chunk) for chunk in chunks]
    doc_ids = [chunk.id for chunk in chunks]
    provider, model_ref, vector_dims, encode = vector_encoder(config, allow_download, dims)

    out_dir = vector_index_override or configured_path(config, "retrieval", "vector_index", "data/indexes/vector")
    progress_dir = out_dir / "build_progress"
    progress_path = progress_dir / "progress.json"
    memmap_path = progress_dir / "vectors.float32.memmap"

    if force_vector and progress_dir.exists():
        shutil.rmtree(progress_dir)

    progress = load_progress(progress_path)
    if not progress_matches(progress, provider, model_ref, vector_dims, doc_ids):
        if progress_dir.exists():
            shutil.rmtree(progress_dir)
        progress_dir.mkdir(parents=True, exist_ok=True)
        progress = {
            "version": 1,
            "provider": provider,
            "model_ref": model_ref,
            "dims": vector_dims,
            "total_docs": len(doc_ids),
            "doc_ids_digest": doc_ids_digest(doc_ids),
            "completed": 0,
            "batch_size": batch_size,
        }
        progress_path.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")
        vectors = np.memmap(memmap_path, dtype="float32", mode="w+", shape=(len(doc_ids), vector_dims))
        vectors.flush()
    else:
        progress_dir.mkdir(parents=True, exist_ok=True)
        vectors = np.memmap(memmap_path, dtype="float32", mode="r+", shape=(len(doc_ids), vector_dims))

    completed = int(progress.get("completed") or 0)
    completed = min(max(completed, 0), len(doc_ids))
    batches_done = 0
    for start in range(completed, len(doc_ids), batch_size):
        end = min(start + batch_size, len(doc_ids))
        batch_vectors = encode(texts[start:end], batch_size)
        if batch_vectors.shape != (end - start, vector_dims):
            raise RuntimeError(f"Unexpected vector shape {batch_vectors.shape}; expected {(end - start, vector_dims)}")
        vectors[start:end] = batch_vectors
        vectors.flush()
        progress["completed"] = end
        progress["batch_size"] = batch_size
        progress_path.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")
        batches_done += 1
        print(f"vector progress {end}/{len(doc_ids)} ({provider})")
        if max_batches and batches_done >= max_batches and end < len(doc_ids):
            return {
                "docs": len(doc_ids),
                "dims": vector_dims,
                "provider": provider,
                "path": str(memmap_path),
                "completed": end,
                "resumable": True,
            }

    final_vectors = np.asarray(vectors, dtype=np.float32)
    result = write_final_vector_index(out_dir, final_vectors, provider, model_ref, doc_ids)
    progress["completed"] = len(doc_ids)
    progress["finalized"] = True
    progress_path.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")
    return result | {"completed": len(doc_ids), "resumable": True}


def main() -> None:
    parser = argparse.ArgumentParser(description="Build local BM25 and vector indexes for OralPath retrieval.")
    parser.add_argument("--skip-bm25", action="store_true")
    parser.add_argument("--skip-vector", action="store_true")
    parser.add_argument("--hash-dims", type=int, default=512, help="Fallback hashing-vector dimensions.")
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--allow-download", action="store_true", help="Allow downloading embedding model if local path is missing.")
    parser.add_argument("--force-vector", action="store_true", help="Discard vector build progress and rebuild from the first document.")
    parser.add_argument("--vector-index", type=Path, default=None, help="Override vector index output directory.")
    parser.add_argument("--limit-docs", type=int, default=None, help="Build only the first N docs. Intended for testing.")
    parser.add_argument("--max-batches", type=int, default=None, help="Stop after N vector batches. Intended for resume testing.")
    args = parser.parse_args()

    reset_cache()
    config = load_config()
    result: dict[str, Any] = {}
    if not args.skip_bm25:
        result["bm25"] = build_bm25(config)
    if not args.skip_vector:
        vector_override = PROJECT_ROOT / args.vector_index if args.vector_index and not args.vector_index.is_absolute() else args.vector_index
        result["vector"] = build_vector(
            config,
            args.hash_dims,
            args.batch_size,
            args.allow_download,
            force_vector=args.force_vector,
            vector_index_override=vector_override,
            limit_docs=args.limit_docs,
            max_batches=args.max_batches,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
