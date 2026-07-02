from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_KAG_DIR = PROJECT_ROOT / "data" / "kb" / "kag_bge_m3"
DEFAULT_MODEL_DIR = PROJECT_ROOT / "models" / "embeddings" / "bge-m3"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at {path}:{line_no}: {exc}") from exc
            if isinstance(item, dict):
                yield item


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def rel_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def load_corpus(kag_dir: Path, limit: int | None) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    doc_ids: list[str] = []
    texts: list[str] = []
    metadata: list[dict[str, Any]] = []
    for row in iter_jsonl(kag_dir / "retrieval_corpus.jsonl"):
        doc_id = str(row.get("doc_id") or "")
        body = str(row.get("text") or "").strip()
        if not doc_id or not body:
            continue
        doc_ids.append(doc_id)
        texts.append(body)
        metadata.append(
            {
                "doc_id": doc_id,
                "doc_type": row.get("doc_type"),
                "source_layer": row.get("source_layer"),
                "authority_level": row.get("authority_level"),
                "metadata": row.get("metadata") or {},
            }
        )
        if limit and len(doc_ids) >= limit:
            break
    return doc_ids, texts, metadata


def digest_ids(doc_ids: list[str]) -> str:
    digest = hashlib.sha256()
    for doc_id in doc_ids:
        digest.update(doc_id.encode("utf-8", errors="ignore"))
        digest.update(b"\0")
    return digest.hexdigest()


def load_encoder(model_dir: Path, *, max_length: int, preferred_provider: str, num_threads: int):
    onnx_path = model_dir / "onnx" / "model.onnx"
    onnx_error: Exception | None = None
    if preferred_provider in {"auto", "onnxruntime"} and onnx_path.exists():
        try:
            import onnxruntime as ort
            from transformers import AutoTokenizer

            tokenizer = AutoTokenizer.from_pretrained(str(model_dir), local_files_only=True)
            session_options = ort.SessionOptions()
            session_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            session_options.intra_op_num_threads = max(1, num_threads)
            session_options.inter_op_num_threads = 1
            session = ort.InferenceSession(
                str(onnx_path),
                sess_options=session_options,
                providers=["CPUExecutionProvider"],
            )
            input_names = {item.name for item in session.get_inputs()}
            output_names = [item.name for item in session.get_outputs()]

            def encode(texts: list[str], batch_size: int) -> np.ndarray:
                vectors: list[np.ndarray] = []
                for start in range(0, len(texts), batch_size):
                    batch = texts[start : start + batch_size]
                    encoded = tokenizer(
                        batch,
                        padding=True,
                        truncation=True,
                        max_length=max_length,
                        return_tensors="np",
                    )
                    feeds = {key: value.astype(np.int64) for key, value in encoded.items() if key in input_names}
                    outputs = session.run(output_names, feeds)
                    token_embeddings = outputs[0]
                    mask = encoded["attention_mask"].astype(np.float32)[..., None]
                    pooled = (token_embeddings * mask).sum(axis=1) / np.clip(mask.sum(axis=1), 1e-9, None)
                    pooled = pooled / np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-9, None)
                    vectors.append(pooled.astype(np.float32))
                return np.vstack(vectors).astype(np.float32)

            probe = encode(["dimension probe"], 1)
            return "onnxruntime", str(model_dir), int(probe.shape[1]), encode
        except Exception as exc:
            onnx_error = exc
            if preferred_provider == "onnxruntime":
                raise RuntimeError(f"Requested ONNX Runtime provider failed: {exc}") from exc

    if preferred_provider in {"auto", "sentence_transformers"}:
        try:
            from sentence_transformers import SentenceTransformer

            model = SentenceTransformer(str(model_dir))
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

            return "sentence_transformers", str(model_dir), dims, encode
        except Exception as exc:
            if preferred_provider == "sentence_transformers":
                raise RuntimeError(f"Requested sentence-transformers provider failed: {exc}") from exc
            raise RuntimeError(
                "BGE-M3 local encoder is not available. "
                f"ONNX error: {onnx_error}; sentence-transformers error: {exc}. "
                "No fallback embeddings were generated."
            ) from exc
    raise RuntimeError(f"Unsupported provider setting: {preferred_provider}")


def load_progress(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None


def progress_matches(progress: dict[str, Any] | None, provider: str, model_ref: str, dims: int, doc_ids: list[str]) -> bool:
    return bool(
        progress
        and progress.get("provider") == provider
        and progress.get("model_ref") == model_ref
        and int(progress.get("dims") or 0) == dims
        and int(progress.get("total_docs") or 0) == len(doc_ids)
        and progress.get("doc_ids_digest") == digest_ids(doc_ids)
    )


def build_faiss_index(vectors: np.ndarray, out_dir: Path) -> dict[str, Any]:
    try:
        import faiss
    except Exception as exc:
        return {"status": "not_ready", "reason": f"faiss import failed: {exc}"}

    index = faiss.IndexFlatIP(vectors.shape[1])
    index.add(vectors)
    faiss.write_index(index, str(out_dir / "faiss.index"))
    return {"status": "ready", "type": "IndexFlatIP", "path": "faiss.index", "vectors": int(index.ntotal)}


def update_kag_manifest(kag_dir: Path, vector_manifest: dict[str, Any]) -> None:
    manifest_path = kag_dir / "manifest.json"
    if not manifest_path.exists():
        return
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.setdefault("indexes", {})["dense_vector"] = vector_manifest
    status = vector_manifest.get("status")
    manifest.setdefault("quality_gates", {})["bge_m3_vector_status"] = status
    manifest["updated_at"] = now_iso()
    write_json(manifest_path, manifest)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build BGE-M3 dense vectors for the KAG retrieval corpus.")
    parser.add_argument("--kag-dir", type=Path, default=DEFAULT_KAG_DIR)
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--max-length", type=int, default=512)
    parser.add_argument("--provider", choices=["auto", "onnxruntime", "sentence_transformers"], default="auto")
    parser.add_argument("--num-threads", type=int, default=max(1, min(os.cpu_count() or 1, 4)))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--max-batches", type=int, default=None)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    out_dir = args.kag_dir / "indexes" / "dense_bge_m3"
    progress_dir = out_dir / "build_progress"
    out_dir.mkdir(parents=True, exist_ok=True)
    progress_dir.mkdir(parents=True, exist_ok=True)

    doc_ids, texts, metadata = load_corpus(args.kag_dir, args.limit)
    provider, model_ref, dims, encode = load_encoder(
        args.model_dir,
        max_length=args.max_length,
        preferred_provider=args.provider,
        num_threads=args.num_threads,
    )
    progress_path = progress_dir / "progress.json"
    memmap_path = progress_dir / "vectors.float32.memmap"

    if args.force:
        for path in [progress_path, memmap_path, out_dir / "vectors.npy", out_dir / "manifest.json", out_dir / "faiss.index"]:
            if path.exists():
                path.unlink()

    progress = load_progress(progress_path)
    if not progress_matches(progress, provider, model_ref, dims, doc_ids):
        progress = {
            "version": 1,
            "provider": provider,
            "model_ref": model_ref,
            "dims": dims,
            "total_docs": len(doc_ids),
            "doc_ids_digest": digest_ids(doc_ids),
            "completed": 0,
            "batch_size": args.batch_size,
            "max_length": args.max_length,
            "started_at": now_iso(),
        }
        write_json(progress_path, progress)
        vectors = np.memmap(memmap_path, dtype="float32", mode="w+", shape=(len(doc_ids), dims))
        vectors.flush()
    else:
        vectors = np.memmap(memmap_path, dtype="float32", mode="r+", shape=(len(doc_ids), dims))

    completed = min(max(int(progress.get("completed") or 0), 0), len(doc_ids))
    batches_done = 0
    for start in range(completed, len(doc_ids), args.batch_size):
        end = min(start + args.batch_size, len(doc_ids))
        vectors[start:end] = encode(texts[start:end], args.batch_size)
        vectors.flush()
        progress["completed"] = end
        progress["updated_at"] = now_iso()
        write_json(progress_path, progress)
        print(f"bge-m3 vector progress {end}/{len(doc_ids)} ({provider})")
        batches_done += 1
        if args.max_batches and batches_done >= args.max_batches and end < len(doc_ids):
            partial_manifest = {
                "status": "partial",
                "provider": provider,
                "model_ref": rel_path(Path(model_ref)),
                "dims": dims,
                "total_docs": len(doc_ids),
                "completed": end,
                "sample_limit": args.limit,
                "progress": rel_path(progress_path),
                "generated_at": now_iso(),
            }
            write_json(out_dir / "manifest.json", partial_manifest)
            if args.limit is None:
                update_kag_manifest(args.kag_dir, partial_manifest)
            print(json.dumps(partial_manifest, ensure_ascii=False, indent=2, sort_keys=True))
            return

    final_vectors = np.asarray(vectors, dtype=np.float32)
    np.save(out_dir / "vectors.npy", final_vectors)
    write_json(out_dir / "doc_metadata.json", {"doc_ids": doc_ids, "metadata": metadata})
    faiss_manifest = build_faiss_index(final_vectors, out_dir)
    manifest = {
        "status": "sample_ready" if args.limit is not None else "ready",
        "provider": provider,
        "model_ref": rel_path(Path(model_ref)),
        "model": "BAAI/bge-m3",
        "dims": dims,
        "total_docs": len(doc_ids),
        "sample_limit": args.limit,
        "doc_ids_digest": digest_ids(doc_ids),
        "normalize": True,
        "max_length": args.max_length,
        "vectors": "vectors.npy",
        "doc_metadata": "doc_metadata.json",
        "faiss": faiss_manifest,
        "generated_at": now_iso(),
    }
    write_json(out_dir / "manifest.json", manifest)
    progress["completed"] = len(doc_ids)
    progress["finalized"] = True
    progress["updated_at"] = now_iso()
    write_json(progress_path, progress)
    if args.limit is None:
        update_kag_manifest(args.kag_dir, manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
