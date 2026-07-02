from __future__ import annotations

import argparse
import hashlib
import gzip
import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_KAG_DIR = PROJECT_ROOT / "data" / "kb" / "kag_bge_m3"


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


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def count_jsonl(path: Path) -> int:
    return sum(1 for _ in iter_jsonl(path))


def validate(kag_dir: Path) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    required = [
        "manifest.json",
        "chunks.jsonl",
        "kg_nodes.jsonl",
        "kg_edges.jsonl",
        "concept_edges.jsonl",
        "kg_chunk_links.jsonl",
        "questions.jsonl",
        "question_links.jsonl",
        "retrieval_corpus.jsonl",
        "indexes/graph/adjacency.json",
        "indexes/graph/aliases.jsonl",
        "indexes/sparse/inverted_index.json.gz",
        "indexes/sparse/manifest.json",
    ]
    for rel in required:
        if not (kag_dir / rel).exists():
            errors.append(f"missing required artifact: {rel}")

    manifest = load_json(kag_dir / "manifest.json") if (kag_dir / "manifest.json").exists() else {}
    counts = manifest.get("counts") or {}
    actual_counts = {}
    for name in ["chunks", "kg_nodes", "kg_edges", "concept_edges", "kg_chunk_links", "questions", "question_links", "retrieval_corpus"]:
        path = kag_dir / f"{name}.jsonl"
        if path.exists():
            actual_counts[name] = count_jsonl(path)
            expected_name = name
            if expected_name == "retrieval_corpus":
                expected = counts.get("retrieval_docs")
            else:
                expected = counts.get(name)
            if expected is not None and int(expected) != actual_counts[name]:
                errors.append(f"count mismatch for {name}: manifest={expected}, actual={actual_counts[name]}")

    chunk_ids: set[str] = set()
    node_ids: set[str] = set()
    edge_ids: set[str] = set()
    core_node_ids: set[str] = set()
    question_ids: set[str] = set()
    doc_ids: list[str] = []
    doc_id_set: set[str] = set()
    textbook_missing_span = 0
    textbook_missing_unit = 0
    chunk_layers: Counter[str] = Counter()
    chunk_roles: Counter[str] = Counter()
    quality_flags: Counter[str] = Counter()
    chunk_core_node_ids: dict[str, set[str]] = {}
    for row in iter_jsonl(kag_dir / "chunks.jsonl"):
        chunk_id = row.get("chunk_id")
        if chunk_id in chunk_ids:
            errors.append(f"duplicate chunk_id: {chunk_id}")
        chunk_ids.add(chunk_id)
        chunk_layers.update([row.get("source_layer")])
        chunk_roles.update([row.get("chunk_role")])
        quality_flags.update(row.get("quality_flags") or [])
        chunk_core_node_ids[chunk_id] = set(row.get("core_node_ids") or [])
        if row.get("source_layer") == "textbook_evidence":
            if not row.get("source_span_ids"):
                textbook_missing_span += 1
            if not row.get("unit_ids"):
                textbook_missing_unit += 1
    if textbook_missing_span:
        errors.append(f"textbook chunks missing source_span_ids: {textbook_missing_span}")
    if textbook_missing_unit:
        errors.append(f"textbook chunks missing unit_ids: {textbook_missing_unit}")

    for row in iter_jsonl(kag_dir / "kg_nodes.jsonl"):
        node_id = row.get("node_id")
        if node_id in node_ids:
            errors.append(f"duplicate node_id: {node_id}")
        node_ids.add(node_id)
        if row.get("node_kind") == "core":
            core_node_ids.add(node_id)
    kg_edges = list(iter_jsonl(kag_dir / "kg_edges.jsonl"))
    for row in kg_edges:
        edge_id = row.get("edge_id")
        if edge_id in edge_ids:
            errors.append(f"duplicate kg edge_id: {edge_id}")
        edge_ids.add(edge_id)
        if row.get("source_node_id") not in node_ids:
            errors.append(f"kg edge has missing source node: {edge_id}")
        if row.get("target_node_id") not in node_ids:
            errors.append(f"kg edge has missing target node: {edge_id}")
    concept_edges = list(iter_jsonl(kag_dir / "concept_edges.jsonl"))
    for row in concept_edges:
        if row.get("source_node_id") not in node_ids:
            errors.append(f"concept edge has missing source node: {row.get('edge_id')}")
        if row.get("target_node_id") not in node_ids:
            errors.append(f"concept edge has missing target node: {row.get('edge_id')}")
    for row in iter_jsonl(kag_dir / "questions.jsonl"):
        question_id = row.get("question_id")
        if question_id in question_ids:
            errors.append(f"duplicate question_id: {question_id}")
        question_ids.add(question_id)

    bad_kg_links = 0
    link_statuses: Counter[str] = Counter()
    core_to_chunks: dict[str, set[str]] = {node_id: set() for node_id in core_node_ids}
    concept_to_chunks: dict[str, set[str]] = {}
    edge_to_chunks: dict[str, set[str]] = {}
    for row in iter_jsonl(kag_dir / "kg_chunk_links.jsonl"):
        link_statuses.update([row.get("verification_status")])
        if row.get("to_id") not in chunk_ids:
            bad_kg_links += 1
        from_id = row.get("from_id")
        if row.get("from_type") in {"concept", "node", "kg_node", "core"} and from_id not in node_ids:
            bad_kg_links += 1
        if row.get("from_type") == "edge" and from_id not in edge_ids:
            bad_kg_links += 1
        if row.get("to_id") in chunk_ids:
            if row.get("from_type") in {"node", "kg_node", "core"} and from_id in core_node_ids:
                core_to_chunks.setdefault(from_id, set()).add(row.get("to_id"))
            if row.get("from_type") == "concept":
                concept_to_chunks.setdefault(from_id, set()).add(row.get("to_id"))
            if row.get("from_type") == "edge":
                edge_to_chunks.setdefault(from_id, set()).add(row.get("to_id"))
    if bad_kg_links:
        errors.append(f"kg_chunk_links with unresolved endpoints: {bad_kg_links}")

    for chunk_id, node_ids_for_chunk in chunk_core_node_ids.items():
        for node_id in node_ids_for_chunk:
            if node_id in core_node_ids:
                core_to_chunks.setdefault(node_id, set()).add(chunk_id)
    for edge in concept_edges:
        source = edge.get("source_node_id")
        target = edge.get("target_node_id")
        if source in concept_to_chunks and target in core_node_ids:
            core_to_chunks.setdefault(target, set()).update(concept_to_chunks[source])
        if target in concept_to_chunks and source in core_node_ids:
            core_to_chunks.setdefault(source, set()).update(concept_to_chunks[target])
    for edge in kg_edges:
        chunks_for_edge = edge_to_chunks.get(edge.get("edge_id"), set())
        if not chunks_for_edge:
            continue
        for node_id in [edge.get("source_node_id"), edge.get("target_node_id")]:
            if node_id in core_node_ids:
                core_to_chunks.setdefault(node_id, set()).update(chunks_for_edge)
    core_nodes_without_chunks = sorted(node_id for node_id in core_node_ids if not core_to_chunks.get(node_id))
    if core_nodes_without_chunks:
        warnings.append(f"core nodes without any chunk path: {len(core_nodes_without_chunks)}")

    bad_question_links = 0
    weak_or_matched = 0
    for row in iter_jsonl(kag_dir / "question_links.jsonl"):
        if row.get("from_id") not in question_ids or row.get("to_id") not in node_ids:
            bad_question_links += 1
        if row.get("original_match_status") in {"matched", "weak_match"}:
            weak_or_matched += 1
    if bad_question_links:
        errors.append(f"question_links with unresolved endpoints: {bad_question_links}")
    if weak_or_matched == 0:
        warnings.append("no matched/weak_match question links found")

    graph_manifest = load_json(kag_dir / "indexes" / "graph" / "manifest.json")
    sparse_manifest = load_json(kag_dir / "indexes" / "sparse" / "manifest.json")
    for row in iter_jsonl(kag_dir / "retrieval_corpus.jsonl"):
        doc_id = row.get("doc_id")
        if doc_id in doc_id_set:
            errors.append(f"duplicate retrieval doc_id: {doc_id}")
        doc_id_set.add(doc_id)
        doc_ids.append(doc_id)
    with gzip.open(kag_dir / "indexes" / "sparse" / "inverted_index.json.gz", "rt", encoding="utf-8") as fh:
        sparse_index = json.load(fh)
    if sparse_index.get("total_docs") != actual_counts.get("retrieval_corpus"):
        errors.append("sparse total_docs does not match retrieval_corpus")
    if sparse_manifest.get("status") != "ready":
        errors.append("sparse index is not ready")
    if graph_manifest.get("status") != "ready":
        errors.append("graph index is not ready")

    vector_status = ((manifest.get("indexes") or {}).get("dense_vector") or {}).get("status")
    if vector_status != "ready":
        warnings.append(f"dense BGE-M3 vector index is {vector_status}")
    dense_report: dict[str, Any] = {"status": vector_status}
    dense_dir = kag_dir / "indexes" / "dense_bge_m3"
    dense_manifest_path = dense_dir / "manifest.json"
    dense_metadata_path = dense_dir / "doc_metadata.json"
    if vector_status == "ready":
        if not dense_manifest_path.exists() or not dense_metadata_path.exists():
            errors.append("dense vector manifest/doc_metadata is missing")
        else:
            dense_manifest = load_json(dense_manifest_path)
            dense_metadata = load_json(dense_metadata_path)
            dense_doc_ids = dense_metadata.get("doc_ids") or []
            digest = hashlib.sha256()
            for doc_id in doc_ids:
                digest.update(str(doc_id).encode("utf-8", errors="ignore"))
                digest.update(b"\0")
            dense_report = {
                "status": dense_manifest.get("status"),
                "total_docs": dense_manifest.get("total_docs"),
                "dims": dense_manifest.get("dims"),
                "doc_metadata_count": len(dense_doc_ids),
                "doc_id_order_matches_retrieval_corpus": dense_doc_ids == doc_ids,
                "doc_ids_digest_matches": digest.hexdigest() == dense_manifest.get("doc_ids_digest"),
                "faiss_status": (dense_manifest.get("faiss") or {}).get("status"),
            }
            if dense_manifest.get("status") != "ready":
                errors.append("dense index manifest is not ready")
            if dense_doc_ids != doc_ids:
                errors.append("dense doc_metadata doc_ids do not match retrieval_corpus order")
            if dense_manifest.get("total_docs") != len(doc_ids):
                errors.append("dense total_docs does not match retrieval_corpus")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "actual_counts": actual_counts,
        "chunk_layers": dict(sorted(chunk_layers.items())),
        "chunk_roles": dict(sorted(chunk_roles.items())),
        "quality_flags": dict(sorted(quality_flags.items())),
        "kg_chunk_link_statuses": dict(sorted(link_statuses.items())),
        "core_node_coverage": {
            "core_nodes": len(core_node_ids),
            "core_nodes_with_any_chunk_path": len(core_node_ids) - len(core_nodes_without_chunks),
            "core_nodes_without_any_chunk_path": len(core_nodes_without_chunks),
            "sample_missing": core_nodes_without_chunks[:20],
        },
        "graph_index": graph_manifest,
        "sparse_index": {
            "status": sparse_manifest.get("status"),
            "terms": sparse_manifest.get("terms"),
            "total_docs": sparse_manifest.get("total_docs"),
        },
        "dense_vector_status": vector_status,
        "dense_index": dense_report,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate KAG BGE-M3 KB artifacts.")
    parser.add_argument("--kag-dir", type=Path, default=DEFAULT_KAG_DIR)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = validate(args.kag_dir)
    write_json(args.kag_dir / "validation_report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
