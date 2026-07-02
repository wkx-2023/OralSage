from __future__ import annotations

import argparse
import gzip
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

import jieba
import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_KAG_DIR = PROJECT_ROOT / "data" / "kb" / "kag_bge_m3"
DEFAULT_MODEL_DIR = PROJECT_ROOT / "models" / "embeddings" / "bge-m3"
TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[\u4e00-\u9fff]+")


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


def text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def tokenize(value: str) -> list[str]:
    tokens: list[str] = []
    for item in TOKEN_RE.findall(value.lower()):
        if re.fullmatch(r"[\u4e00-\u9fff]+", item):
            tokens.extend(token for token in jieba.cut(item) if token.strip())
            if len(item) >= 2:
                tokens.extend(item[index : index + 2] for index in range(len(item) - 1))
        else:
            tokens.append(item)
    return [token for token in tokens if token.strip()]


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_sparse_index(kag_dir: Path) -> dict[str, Any]:
    path = kag_dir / "indexes" / "sparse" / "inverted_index.json.gz"
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def load_rows(path: Path, id_field: str) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for row in iter_jsonl(path):
        row_id = text(row.get(id_field))
        if row_id:
            rows[row_id] = row
    return rows


def load_retrieval_corpus(kag_dir: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    docs = list(iter_jsonl(kag_dir / "retrieval_corpus.jsonl"))
    id_to_index = {doc["doc_id"]: index for index, doc in enumerate(docs)}
    return docs, id_to_index


def load_dense_manifest(kag_dir: Path) -> dict[str, Any]:
    path = kag_dir / "indexes" / "dense_bge_m3" / "manifest.json"
    if not path.exists():
        return {"status": "missing"}
    return load_json(path)


def load_dense_encoder(model_dir: Path, *, max_length: int, num_threads: int):
    onnx_path = model_dir / "onnx" / "model.onnx"
    pooling_config_path = model_dir / "1_Pooling" / "config.json"
    if not onnx_path.exists():
        raise FileNotFoundError(f"BGE-M3 ONNX model is missing: {onnx_path}")
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
    pooling_config = load_json(pooling_config_path) if pooling_config_path.exists() else {}
    use_cls_pooling = bool(pooling_config.get("pooling_mode_cls_token", True))

    def encode(query: str) -> np.ndarray:
        encoded = tokenizer(
            [query],
            padding=True,
            truncation=True,
            max_length=max_length,
            return_tensors="np",
        )
        feeds = {key: value.astype(np.int64) for key, value in encoded.items() if key in input_names}
        outputs = session.run(output_names, feeds)
        token_embeddings = outputs[0]
        if use_cls_pooling:
            pooled = token_embeddings[:, 0, :]
        else:
            mask = encoded["attention_mask"].astype(np.float32)[..., None]
            pooled = (token_embeddings * mask).sum(axis=1) / np.clip(mask.sum(axis=1), 1e-9, None)
        pooled = pooled.astype(np.float32)
        pooled = pooled / np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-9, None)
        return pooled.astype(np.float32)

    return encode


def dense_search(
    query: str,
    kag_dir: Path,
    docs: list[dict[str, Any]],
    *,
    top_k: int,
    model_dir: Path,
    max_length: int,
    num_threads: int,
    doc_types: set[str] | None = None,
    source_layers: set[str] | None = None,
) -> list[dict[str, Any]]:
    dense_dir = kag_dir / "indexes" / "dense_bge_m3"
    manifest = load_dense_manifest(kag_dir)
    if manifest.get("status") != "ready":
        return []
    try:
        import faiss
    except Exception:
        return []
    index_path = dense_dir / str((manifest.get("faiss") or {}).get("path") or "faiss.index")
    if not index_path.exists():
        return []
    index = faiss.read_index(str(index_path))
    encode = load_dense_encoder(model_dir, max_length=max_length, num_threads=num_threads)
    query_vector = encode(query)
    search_k = min(max(top_k * 4, top_k), len(docs))
    scores, indices = index.search(query_vector, search_k)
    results: list[dict[str, Any]] = []
    for score, doc_index in zip(scores[0], indices[0]):
        if doc_index < 0:
            continue
        doc = docs[int(doc_index)]
        if doc_types and doc.get("doc_type") not in doc_types:
            continue
        if source_layers and doc.get("source_layer") not in source_layers:
            continue
        results.append(
            {
                "doc_id": doc["doc_id"],
                "doc_type": doc.get("doc_type"),
                "source_layer": doc.get("source_layer"),
                "score": round(float(score), 6),
                "match_stage": "dense_bge_m3",
            }
        )
        if len(results) >= top_k:
            break
    return results


def load_aliases(kag_dir: Path) -> dict[str, list[str]]:
    alias_to_nodes: dict[str, list[str]] = defaultdict(list)
    for row in iter_jsonl(kag_dir / "indexes" / "graph" / "aliases.jsonl"):
        alias = text(row.get("alias")).lower()
        node_id = text(row.get("node_id"))
        if alias and node_id and node_id not in alias_to_nodes[alias]:
            alias_to_nodes[alias].append(node_id)
    return dict(alias_to_nodes)


def bm25_search(
    query: str,
    sparse_index: dict[str, Any],
    docs: list[dict[str, Any]],
    *,
    top_k: int,
    doc_types: set[str] | None = None,
    source_layers: set[str] | None = None,
) -> list[dict[str, Any]]:
    query_terms = Counter(tokenize(query))
    if not query_terms:
        return []
    doc_terms = sparse_index["doc_terms"]
    avg_len = float(sparse_index.get("avg_doc_len") or 1.0)
    idf = sparse_index["idf"]
    postings = sparse_index["postings"]
    k1 = 1.5
    b = 0.75
    scores: dict[int, float] = defaultdict(float)
    for term, qtf in query_terms.items():
        term_idf = float(idf.get(term) or 0.0)
        if term_idf <= 0:
            continue
        for doc_index, tf in postings.get(term, []):
            doc = docs[doc_index]
            if doc_types and doc.get("doc_type") not in doc_types:
                continue
            if source_layers and doc.get("source_layer") not in source_layers:
                continue
            doc_len = max(float(doc_terms[doc_index].get("length") or 1.0), 1.0)
            denom = tf + k1 * (1 - b + b * doc_len / avg_len)
            scores[doc_index] += qtf * term_idf * (tf * (k1 + 1)) / denom
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:top_k]
    return [
        {
            "doc_id": docs[index]["doc_id"],
            "doc_type": docs[index].get("doc_type"),
            "source_layer": docs[index].get("source_layer"),
            "score": round(score, 6),
            "match_stage": "sparse_bm25",
        }
        for index, score in ranked
    ]


def normalize_scores(group: list[dict[str, Any]], *, stage: str, weight: float) -> list[dict[str, Any]]:
    if not group:
        return []
    values = [float(item.get("score") or 0.0) for item in group]
    min_score = min(values)
    max_score = max(values)
    span = max(max_score - min_score, 1e-9)
    normalized: list[dict[str, Any]] = []
    for item in group:
        score = float(item.get("score") or 0.0)
        if stage == "alias_seed":
            norm = min(score / 3.0, 1.0)
        elif len(group) == 1:
            norm = 1.0
        else:
            norm = (score - min_score) / span
        normalized.append({**item, "raw_score": round(score, 6), "score": round(norm * weight, 6)})
    return normalized


def alias_seed_nodes(query: str, alias_to_nodes: dict[str, list[str]], *, top_k: int) -> list[dict[str, Any]]:
    query_lc = query.lower()
    seeds: list[dict[str, Any]] = []
    seen: set[str] = set()
    for alias, node_ids in alias_to_nodes.items():
        if not alias:
            continue
        if alias in query_lc or query_lc in alias:
            score = len(alias) / max(len(query_lc), len(alias), 1)
            for node_id in node_ids:
                if node_id in seen:
                    continue
                seen.add(node_id)
                seeds.append(
                    {
                        "doc_id": node_id,
                        "doc_type": "kg_node",
                        "source_layer": "textbook_kg",
                        "score": round(2.0 + score, 6),
                        "match_stage": "alias_seed",
                        "matched_alias": alias,
                    }
                )
    return sorted(seeds, key=lambda item: item["score"], reverse=True)[:top_k]


def graph_expand(
    seed_node_ids: list[str],
    adjacency: dict[str, list[dict[str, Any]]],
    *,
    max_hops: int,
    top_k: int,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    frontier = [(node_id, 0, 1.6) for node_id in seed_node_ids]
    visited_nodes = set(seed_node_ids)
    while frontier:
        node_id, depth, base_score = frontier.pop(0)
        if depth >= max_hops:
            continue
        for edge in adjacency.get(node_id, []):
            target = text(edge.get("to_id"))
            relation = text(edge.get("relation"))
            if not target:
                continue
            key = (target, relation)
            if key not in seen:
                seen.add(key)
                direction = edge.get("direction")
                bonus = 0.35 if direction == "evidence" else 0.15 if direction == "assessment" else 0.05
                results.append(
                    {
                        "doc_id": target,
                        "doc_type": "chunk" if target.startswith("chunk:") or target.startswith("manual") or target.startswith("unresolved") else "question" if target.startswith("question:") else "kg_node",
                        "source_layer": None,
                        "score": round(base_score - depth * 0.2 + bonus, 6),
                        "match_stage": "graph_expand",
                        "relation": relation,
                        "from_node_id": node_id,
                        "edge_id": edge.get("edge_id"),
                        "verification_status": edge.get("verification_status"),
                    }
                )
            if target.startswith("kg://") and target not in visited_nodes:
                visited_nodes.add(target)
                frontier.append((target, depth + 1, base_score - 0.25))
    return sorted(results, key=lambda item: item["score"], reverse=True)[:top_k]


def doc_penalty(doc: dict[str, Any] | None) -> tuple[float, list[str]]:
    metadata = doc.get("metadata") if isinstance(doc, dict) and isinstance(doc.get("metadata"), dict) else {}
    penalty = float(metadata.get("retrieval_penalty") or 0.0)
    reasons = [text(item) for item in metadata.get("retrieval_penalty_reasons") or [] if text(item)]
    return penalty, reasons


def layer_bonus(doc: dict[str, Any] | None) -> float:
    if not isinstance(doc, dict):
        return 0.0
    doc_type = doc.get("doc_type")
    source_layer = doc.get("source_layer")
    metadata = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
    if doc_type == "kg_node":
        return 0.12
    if source_layer == "textbook_evidence":
        bonus = 0.1
        if metadata.get("chunk_role") == "canonical_textbook_chunk":
            bonus += 0.08
        return bonus
    if source_layer == "clinical_authority":
        return 0.08
    if source_layer == "assessment":
        return -0.02
    if source_layer == "external_reference":
        return -0.06
    return 0.0


def merge_results(*groups: list[dict[str, Any]], top_k: int, docs_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for group in groups:
        for item in group:
            doc_id = text(item.get("doc_id"))
            if not doc_id:
                continue
            existing = merged.get(doc_id)
            if not existing:
                merged[doc_id] = dict(item)
                merged[doc_id]["stages"] = [item.get("match_stage")]
                continue
            existing["score"] = round(float(existing.get("score") or 0) + float(item.get("score") or 0), 6)
            existing.setdefault("stages", []).append(item.get("match_stage"))
            for key in ["relation", "from_node_id", "edge_id", "verification_status", "matched_alias"]:
                if key in item and key not in existing:
                    existing[key] = item[key]
    reranked: list[dict[str, Any]] = []
    for doc_id, item in merged.items():
        doc = docs_by_id.get(doc_id)
        penalty, penalty_reasons = doc_penalty(doc)
        bonus = layer_bonus(doc)
        stage_bonus = 0.04 * max(len(set(item.get("stages") or [])) - 1, 0)
        final_score = max(float(item.get("score") or 0.0) + bonus + stage_bonus - penalty, 0.0)
        item["score"] = round(final_score, 6)
        item["rerank"] = {
            "layer_bonus": round(bonus, 3),
            "multi_stage_bonus": round(stage_bonus, 3),
            "quality_penalty": round(penalty, 3),
            "quality_penalty_reasons": penalty_reasons,
        }
        reranked.append(item)
    return sorted(reranked, key=lambda item: item.get("score") or 0, reverse=True)[:top_k]


def hydrate_result(
    result: dict[str, Any],
    *,
    chunks: dict[str, dict[str, Any]],
    nodes: dict[str, dict[str, Any]],
    questions: dict[str, dict[str, Any]],
    docs_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    doc_id = result["doc_id"]
    row = chunks.get(doc_id) or nodes.get(doc_id) or questions.get(doc_id) or docs_by_id.get(doc_id) or {}
    preview = text(row.get("text") or row.get("stem") or row.get("canonical_name"))
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    return {
        **result,
        "title": text(row.get("canonical_name") or row.get("source_title") or row.get("stem") or doc_id)[:160],
        "source_layer": result.get("source_layer") or row.get("source_layer"),
        "subject": row.get("subject") or metadata.get("subject"),
        "chapter": row.get("chapter") or metadata.get("chapter"),
        "source_type": row.get("source_type") or metadata.get("source_type"),
        "chunk_role": row.get("chunk_role") or metadata.get("chunk_role"),
        "source_span_ids": row.get("source_span_ids") or metadata.get("source_span_ids") or [],
        "core_node_ids": row.get("core_node_ids") or metadata.get("core_node_ids") or [],
        "quality_flags": row.get("quality_flags") or metadata.get("quality_flags") or [],
        "preview": preview[:360],
    }


def parse_csv_set(value: str) -> set[str] | None:
    if not value:
        return None
    return {item.strip() for item in value.split(",") if item.strip()}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Query the KAG BGE-M3 knowledge base using hybrid dense + graph + sparse retrieval.")
    parser.add_argument("--kag-dir", type=Path, default=DEFAULT_KAG_DIR)
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--query", required=True)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--dense-k", type=int, default=30)
    parser.add_argument("--sparse-k", type=int, default=30)
    parser.add_argument("--graph-k", type=int, default=30)
    parser.add_argument("--alias-k", type=int, default=8)
    parser.add_argument("--max-hops", type=int, default=2)
    parser.add_argument("--dense-max-length", type=int, default=512)
    parser.add_argument("--dense-num-threads", type=int, default=4)
    parser.add_argument("--no-dense", action="store_true")
    parser.add_argument("--doc-types", default="", help="Comma-separated doc types: chunk,kg_node,question")
    parser.add_argument("--source-layers", default="", help="Comma-separated source layers.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    docs, _ = load_retrieval_corpus(args.kag_dir)
    docs_by_id = {row["doc_id"]: row for row in docs}
    sparse_index = load_sparse_index(args.kag_dir)
    alias_to_nodes = load_aliases(args.kag_dir)
    adjacency = load_json(args.kag_dir / "indexes" / "graph" / "adjacency.json")

    doc_types = parse_csv_set(args.doc_types)
    source_layers = parse_csv_set(args.source_layers)
    aliases = alias_seed_nodes(args.query, alias_to_nodes, top_k=args.alias_k)
    dense = []
    if not args.no_dense:
        dense = dense_search(
            args.query,
            args.kag_dir,
            docs,
            top_k=args.dense_k,
            model_dir=args.model_dir,
            max_length=args.dense_max_length,
            num_threads=args.dense_num_threads,
            doc_types=doc_types,
            source_layers=source_layers,
        )
    graph = graph_expand(
        [item["doc_id"] for item in aliases],
        adjacency,
        max_hops=args.max_hops,
        top_k=args.graph_k,
    )
    sparse = bm25_search(
        args.query,
        sparse_index,
        docs,
        top_k=args.sparse_k,
        doc_types=doc_types,
        source_layers=source_layers,
    )
    merged = merge_results(
        normalize_scores(aliases, stage="alias_seed", weight=1.4),
        normalize_scores(graph, stage="graph_expand", weight=1.2),
        normalize_scores(sparse, stage="sparse_bm25", weight=1.0),
        normalize_scores(dense, stage="dense_bge_m3", weight=1.1),
        top_k=args.top_k,
        docs_by_id=docs_by_id,
    )

    chunks = load_rows(args.kag_dir / "chunks.jsonl", "chunk_id")
    nodes = load_rows(args.kag_dir / "kg_nodes.jsonl", "node_id")
    questions = load_rows(args.kag_dir / "questions.jsonl", "question_id")
    hydrated = [
        hydrate_result(result, chunks=chunks, nodes=nodes, questions=questions, docs_by_id=docs_by_id)
        for result in merged
    ]
    payload = {
        "query": args.query,
        "retrieval": {
            "dense_vector": "used" if dense else "not_used",
            "dense_candidates": len(dense),
            "sparse_candidates": len(sparse),
            "alias_seed_count": len(aliases),
            "graph_candidates": len(graph),
        },
        "results": hydrated,
    }
    output = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.buffer.write(output.encode("utf-8", errors="replace"))
    sys.stdout.buffer.write(b"\n")


if __name__ == "__main__":
    main()
