from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_KAG_DIR = PROJECT_ROOT / "data" / "kb" / "kag_bge_m3"

GENERATED_FLAGS = {
    "exact_duplicate_primary",
    "exact_duplicate_suppressed",
    "boilerplate_disclaimer",
    "reference_heavy",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def stable_id(prefix: str, *parts: Any, length: int = 24) -> str:
    payload = "\0".join(str(part or "") for part in parts)
    digest = hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()[:length]
    return f"{prefix}:{digest}"


def rel_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


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


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            fh.write("\n")
            count += 1
    return count


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def normalize_text_for_dedupe(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def is_disclaimer_or_boilerplate(value: str) -> bool:
    compact = re.sub(r"\s+", "", value)
    return any(
        marker in compact
        for marker in [
            "免责声明",
            "版权所有",
            "保留所有权利",
            "Merck&Co.",
            "Rahway,NJ,USA",
        ]
    )


def is_reference_heavy(value: str) -> bool:
    if "参考文献" in value[:120]:
        return True
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if not lines:
        return False
    citation_lines = 0
    for line in lines:
        score = 0
        if re.search(r"\[\d+\]", line):
            score += 1
        if re.search(r"\bdoi\b|J\s+[A-Z][A-Za-z]+|et al\.|Clin |Periodontol|Pediatrics|N Engl J Med", line, re.I):
            score += 1
        if re.search(r"\b(19|20)\d{2}\b", line) and re.search(r"\d+\s*[:(]\s*\d+", line):
            score += 1
        if score >= 2:
            citation_lines += 1
    return citation_lines >= 3 and citation_lines / max(len(lines), 1) >= 0.35


def clean_generated_quality(chunk: dict[str, Any]) -> None:
    flags = [flag for flag in as_list(chunk.get("quality_flags")) if text(flag) and flag not in GENERATED_FLAGS]
    chunk["quality_flags"] = flags
    metadata = chunk.get("metadata") if isinstance(chunk.get("metadata"), dict) else {}
    metadata.pop("dedupe", None)
    metadata.pop("retrieval_quality", None)
    chunk["metadata"] = metadata


def add_flag(chunk: dict[str, Any], flag: str) -> None:
    flags = [text(item) for item in as_list(chunk.get("quality_flags")) if text(item)]
    if flag not in flags:
        flags.append(flag)
    chunk["quality_flags"] = flags


def duplicate_primary_score(chunk: dict[str, Any]) -> tuple[int, int, int, str]:
    role = chunk.get("chunk_role")
    layer = chunk.get("source_layer")
    role_score = {
        "canonical_textbook_chunk": 100,
        "source_chunk": 70,
        "recovered_evidence_chunk": 50,
    }.get(role, 40)
    if layer == "clinical_authority":
        role_score += 8
    if layer == "textbook_evidence":
        role_score += 6
    if is_disclaimer_or_boilerplate(text(chunk.get("text"))):
        role_score -= 50
    core_count = len(as_list(chunk.get("core_node_ids")))
    span_count = len(as_list(chunk.get("source_span_ids")))
    return (role_score, core_count, span_count, text(chunk.get("chunk_id")))


def tag_duplicates(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for chunk in chunks:
        normalized = normalize_text_for_dedupe(text(chunk.get("text")))
        if normalized:
            digest = hashlib.sha1(normalized.encode("utf-8", errors="ignore")).hexdigest()
            groups[digest].append(chunk)

    duplicate_groups = [group for group in groups.values() if len(group) > 1]
    suppressed = 0
    primary_count = 0
    for group in duplicate_groups:
        primary = max(group, key=duplicate_primary_score)
        primary_id = primary["chunk_id"]
        group_id = stable_id("dupe", primary_id, len(group))
        for chunk in group:
            metadata = chunk.get("metadata") if isinstance(chunk.get("metadata"), dict) else {}
            is_primary = chunk["chunk_id"] == primary_id
            metadata["dedupe"] = {
                "duplicate_group_id": group_id,
                "duplicate_of": None if is_primary else primary_id,
                "duplicate_group_size": len(group),
                "method": "exact_normalized_text_hash",
            }
            chunk["metadata"] = metadata
            add_flag(chunk, "exact_duplicate_primary" if is_primary else "exact_duplicate_suppressed")
            if is_primary:
                primary_count += 1
            else:
                suppressed += 1
    return {
        "duplicate_groups": len(duplicate_groups),
        "duplicate_primary_chunks": primary_count,
        "duplicate_suppressed_chunks": suppressed,
    }


def tag_low_signal_content(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    boilerplate = 0
    reference_heavy = 0
    for chunk in chunks:
        value = text(chunk.get("text"))
        if is_disclaimer_or_boilerplate(value):
            add_flag(chunk, "boilerplate_disclaimer")
            boilerplate += 1
        if is_reference_heavy(value):
            add_flag(chunk, "reference_heavy")
            reference_heavy += 1
    return {"boilerplate_disclaimer": boilerplate, "reference_heavy": reference_heavy}


def apply_retrieval_quality_metadata(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    penalty_counts: Counter[str] = Counter()
    for chunk in chunks:
        flags = set(as_list(chunk.get("quality_flags")))
        penalty = 0.0
        reasons: list[str] = []
        if "exact_duplicate_suppressed" in flags:
            penalty += 0.35
            reasons.append("exact_duplicate_suppressed")
        if "reference_heavy" in flags:
            penalty += 0.25
            reasons.append("reference_heavy")
        if "boilerplate_disclaimer" in flags:
            penalty += 0.65
            reasons.append("boilerplate_disclaimer")
        metadata = chunk.get("metadata") if isinstance(chunk.get("metadata"), dict) else {}
        metadata["retrieval_quality"] = {
            "penalty": round(penalty, 3),
            "reasons": reasons,
            "suppress_duplicate": "exact_duplicate_suppressed" in flags,
        }
        chunk["metadata"] = metadata
        if reasons:
            for reason in reasons:
                penalty_counts[reason] += 1
    return dict(penalty_counts)


def compute_core_chunk_paths(
    chunks: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    kg_edges: list[dict[str, Any]],
    concept_edges: list[dict[str, Any]],
    kg_chunk_links: list[dict[str, Any]],
) -> dict[str, set[str]]:
    core_ids = {node["node_id"] for node in nodes if node.get("node_kind") == "core"}
    edge_by_id = {edge["edge_id"]: edge for edge in kg_edges}
    core_to_chunks: dict[str, set[str]] = defaultdict(set)
    for chunk in chunks:
        for node_id in as_list(chunk.get("core_node_ids")):
            if node_id in core_ids:
                core_to_chunks[node_id].add(chunk["chunk_id"])

    concept_to_chunks: dict[str, set[str]] = defaultdict(set)
    edge_to_chunks: dict[str, set[str]] = defaultdict(set)
    for link in kg_chunk_links:
        if link.get("from_type") == "concept":
            concept_to_chunks[text(link.get("from_id"))].add(text(link.get("to_id")))
        elif link.get("from_type") == "edge":
            edge_to_chunks[text(link.get("from_id"))].add(text(link.get("to_id")))
        elif link.get("from_type") in {"node", "kg_node", "core"}:
            from_id = text(link.get("from_id"))
            if from_id in core_ids:
                core_to_chunks[from_id].add(text(link.get("to_id")))

    for edge in concept_edges:
        source = text(edge.get("source_node_id"))
        target = text(edge.get("target_node_id"))
        if source in concept_to_chunks and target in core_ids:
            core_to_chunks[target].update(concept_to_chunks[source])
        if target in concept_to_chunks and source in core_ids:
            core_to_chunks[source].update(concept_to_chunks[target])

    for edge_id, chunk_ids in edge_to_chunks.items():
        edge = edge_by_id.get(edge_id)
        if not edge:
            continue
        for node_id in [text(edge.get("source_node_id")), text(edge.get("target_node_id"))]:
            if node_id in core_ids:
                core_to_chunks[node_id].update(chunk_ids)
    return core_to_chunks


def repair_core_span_links(
    chunks: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    kg_edges: list[dict[str, Any]],
    concept_edges: list[dict[str, Any]],
    kg_chunk_links: list[dict[str, Any]],
) -> dict[str, Any]:
    chunk_by_id = {chunk["chunk_id"]: chunk for chunk in chunks}
    core_nodes = [node for node in nodes if node.get("node_kind") == "core"]
    core_paths = compute_core_chunk_paths(chunks, nodes, kg_edges, concept_edges, kg_chunk_links)
    missing_core_nodes = [node for node in core_nodes if not core_paths.get(node["node_id"])]

    span_to_chunks: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for chunk in chunks:
        if chunk.get("source_layer") != "textbook_evidence":
            continue
        for span_id in as_list(chunk.get("source_span_ids")):
            if text(span_id):
                span_to_chunks[text(span_id)].append(chunk)

    seen_links = {link["link_id"] for link in kg_chunk_links}
    existing_pairs = {(link.get("from_id"), link.get("to_id")) for link in kg_chunk_links}
    added_links: list[dict[str, Any]] = []
    updated_chunks = 0
    repaired_nodes: list[dict[str, Any]] = []
    still_missing: list[str] = []
    for node in missing_core_nodes:
        node_id = node["node_id"]
        matched_chunks: dict[str, dict[str, Any]] = {}
        for span_id in as_list(node.get("source_span_ids")):
            for chunk in span_to_chunks.get(text(span_id), []):
                matched_chunks[chunk["chunk_id"]] = chunk
        if not matched_chunks:
            still_missing.append(node_id)
            continue

        for chunk_id, chunk in sorted(matched_chunks.items()):
            core_node_ids = [text(item) for item in as_list(chunk.get("core_node_ids")) if text(item)]
            if node_id not in core_node_ids:
                core_node_ids.append(node_id)
                chunk["core_node_ids"] = core_node_ids
                updated_chunks += 1
            if (node_id, chunk_id) in existing_pairs:
                continue
            link_id = stable_id("kagrepair", node_id, chunk_id, "source_span_overlap")
            if link_id in seen_links:
                continue
            seen_links.add(link_id)
            existing_pairs.add((node_id, chunk_id))
            matched_spans = sorted(set(as_list(node.get("source_span_ids"))) & set(as_list(chunk.get("source_span_ids"))))
            added_links.append(
                {
                    "declared_span_id": matched_spans[0] if matched_spans else None,
                    "from_id": node_id,
                    "from_type": "node",
                    "kg_file": node.get("kg_file"),
                    "link_id": link_id,
                    "match_coverage": 1.0,
                    "matched_span_ids": matched_spans,
                    "metadata": {
                        "repair_reason": "core_node_source_span_overlap",
                        "source_node_name": node.get("canonical_name"),
                    },
                    "quote_id": None,
                    "relation": "supports",
                    "score": 1.0,
                    "source_stage": "kag_quality_repair",
                    "to_id": chunk_id,
                    "to_type": "chunk",
                    "unit_index_end": chunk.get("unit_index_end"),
                    "unit_index_start": chunk.get("unit_index_start"),
                    "verification_status": "verified_core_node_source_span_overlap",
                }
            )
        repaired_nodes.append(
            {
                "node_id": node_id,
                "name": node.get("canonical_name"),
                "matched_chunk_ids": sorted(matched_chunks),
            }
        )

    if added_links:
        kg_chunk_links.extend(added_links)

    # Recompute after repair.
    chunk_paths = compute_core_chunk_paths(chunks, nodes, kg_edges, concept_edges, kg_chunk_links)
    remaining = [node["node_id"] for node in core_nodes if not chunk_paths.get(node["node_id"])]
    return {
        "core_nodes_missing_before": len(missing_core_nodes),
        "core_nodes_repaired": len(repaired_nodes),
        "core_nodes_missing_after": len(remaining),
        "added_kg_chunk_links": len(added_links),
        "chunks_with_added_core_node_ids": updated_chunks,
        "repaired_nodes": repaired_nodes,
        "remaining_missing_core_node_ids": remaining,
        "stale_missing_without_span_match": still_missing,
    }


def retrieval_text_for_chunk(chunk: dict[str, Any]) -> str:
    values: list[str] = []
    for key in ["subject", "chapter", "section", "heading_path", "source_name", "source_title", "text"]:
        value = chunk.get(key)
        if isinstance(value, list):
            values.extend(text(item) for item in value if text(item))
        elif text(value):
            values.append(text(value))
    return "\n".join(values)


def update_retrieval_corpus(kag_dir: Path, chunks: list[dict[str, Any]]) -> dict[str, Any]:
    chunk_by_id = {chunk["chunk_id"]: chunk for chunk in chunks}
    updated_docs = 0
    docs: list[dict[str, Any]] = []
    for doc in iter_jsonl(kag_dir / "retrieval_corpus.jsonl"):
        chunk = chunk_by_id.get(doc.get("doc_id"))
        if chunk:
            metadata = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
            chunk_metadata = chunk.get("metadata") if isinstance(chunk.get("metadata"), dict) else {}
            dedupe = chunk_metadata.get("dedupe") if isinstance(chunk_metadata.get("dedupe"), dict) else {}
            retrieval_quality = (
                chunk_metadata.get("retrieval_quality")
                if isinstance(chunk_metadata.get("retrieval_quality"), dict)
                else {}
            )
            metadata.update(
                {
                    "chunk_role": chunk.get("chunk_role"),
                    "source_type": chunk.get("source_type"),
                    "subject": chunk.get("subject"),
                    "chapter": chunk.get("chapter"),
                    "core_node_ids": as_list(chunk.get("core_node_ids")),
                    "source_span_ids": as_list(chunk.get("source_span_ids")),
                    "quality_flags": as_list(chunk.get("quality_flags")),
                    "duplicate_group_id": dedupe.get("duplicate_group_id"),
                    "duplicate_of": dedupe.get("duplicate_of"),
                    "duplicate_group_size": dedupe.get("duplicate_group_size"),
                    "retrieval_penalty": retrieval_quality.get("penalty", 0.0),
                    "retrieval_penalty_reasons": retrieval_quality.get("reasons", []),
                    "suppress_duplicate": retrieval_quality.get("suppress_duplicate", False),
                }
            )
            doc["metadata"] = metadata
            doc["text"] = retrieval_text_for_chunk(chunk)
            updated_docs += 1
        docs.append(doc)
    write_jsonl(kag_dir / "retrieval_corpus.jsonl", docs)
    return {"retrieval_docs_updated": updated_docs, "retrieval_docs_total": len(docs)}


def write_graph_index(
    kag_dir: Path,
    nodes: list[dict[str, Any]],
    kg_edges: list[dict[str, Any]],
    concept_edges: list[dict[str, Any]],
    kg_chunk_links: list[dict[str, Any]],
    question_links: list[dict[str, Any]],
) -> dict[str, Any]:
    adjacency: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in [*kg_edges, *concept_edges]:
        source = edge.get("source_node_id")
        target = edge.get("target_node_id")
        if not source or not target:
            continue
        payload = {
            "to_id": target,
            "relation": edge.get("predicate"),
            "edge_id": edge.get("edge_id"),
            "direction": "out",
        }
        adjacency[source].append(payload)
        adjacency[target].append({**payload, "to_id": source, "direction": "in"})

    for link in kg_chunk_links:
        source = link.get("from_id")
        chunk_id = link.get("to_id")
        if source and chunk_id:
            adjacency[source].append(
                {
                    "to_id": chunk_id,
                    "relation": link.get("relation", "supports"),
                    "edge_id": link.get("link_id"),
                    "direction": "evidence",
                    "from_type": link.get("from_type"),
                    "verification_status": link.get("verification_status"),
                }
            )

    for link in question_links:
        node_id = link.get("to_id")
        question_id = link.get("from_id")
        if node_id and question_id:
            adjacency[node_id].append(
                {
                    "to_id": question_id,
                    "relation": link.get("relation"),
                    "edge_id": link.get("link_id"),
                    "direction": "assessment",
                    "original_match_status": link.get("original_match_status"),
                }
            )

    graph_dir = kag_dir / "indexes" / "graph"
    graph_dir.mkdir(parents=True, exist_ok=True)
    write_json(graph_dir / "adjacency.json", dict(adjacency))
    alias_rows: list[dict[str, Any]] = []
    for node in nodes:
        names = [node.get("canonical_name"), *as_list(node.get("aliases"))]
        for name in names:
            if text(name):
                alias_rows.append({"alias": text(name).lower(), "node_id": node["node_id"]})
    write_jsonl(graph_dir / "aliases.jsonl", alias_rows)
    manifest = {
        "status": "ready",
        "nodes": len(nodes),
        "kg_edges": len(kg_edges),
        "concept_edges": len(concept_edges),
        "kg_chunk_links": len(kg_chunk_links),
        "question_links": len(question_links),
        "adjacency_nodes": len(adjacency),
        "aliases": len(alias_rows),
        "generated_at": now_iso(),
    }
    write_json(graph_dir / "manifest.json", manifest)
    return manifest


def update_manifest(
    kag_dir: Path,
    chunks: list[dict[str, Any]],
    kg_chunk_links: list[dict[str, Any]],
    graph_manifest: dict[str, Any],
    report: dict[str, Any],
) -> None:
    manifest_path = kag_dir / "manifest.json"
    manifest = load_json(manifest_path)
    chunk_layers = Counter(chunk.get("source_layer") for chunk in chunks)
    chunk_roles = Counter(chunk.get("chunk_role") for chunk in chunks)
    manifest.setdefault("counts", {})["chunks"] = len(chunks)
    manifest["counts"]["chunks_by_layer"] = dict(sorted(chunk_layers.items()))
    manifest["counts"]["chunks_by_role"] = dict(sorted(chunk_roles.items()))
    manifest["counts"]["kg_chunk_links"] = len(kg_chunk_links)
    manifest["counts"]["kg_chunk_links_verified_or_reviewed"] = len(
        [link for link in kg_chunk_links if text(link.get("verification_status")) != "needs_review"]
    )
    manifest.setdefault("indexes", {})["graph"] = graph_manifest
    manifest.setdefault("quality_gates", {})["core_nodes_without_any_chunk_path"] = report["core_span_link_repair"][
        "core_nodes_missing_after"
    ]
    manifest["quality_gates"]["duplicate_text_groups_tagged"] = report["duplicates"]["duplicate_groups"]
    manifest["quality_gates"]["external_reference_quality_flags_tagged"] = report["low_signal_content"][
        "boilerplate_disclaimer"
    ] + report["low_signal_content"]["reference_heavy"]
    manifest["quality_gates"]["retrieval_quality_repair_status"] = "ready"
    manifest["updated_at"] = now_iso()
    write_json(manifest_path, manifest)


def repair(kag_dir: Path) -> dict[str, Any]:
    chunks = list(iter_jsonl(kag_dir / "chunks.jsonl"))
    nodes = list(iter_jsonl(kag_dir / "kg_nodes.jsonl"))
    kg_edges = list(iter_jsonl(kag_dir / "kg_edges.jsonl"))
    concept_edges = list(iter_jsonl(kag_dir / "concept_edges.jsonl"))
    kg_chunk_links = list(iter_jsonl(kag_dir / "kg_chunk_links.jsonl"))
    question_links = list(iter_jsonl(kag_dir / "question_links.jsonl"))

    for chunk in chunks:
        clean_generated_quality(chunk)

    core_repair = repair_core_span_links(chunks, nodes, kg_edges, concept_edges, kg_chunk_links)
    duplicate_report = tag_duplicates(chunks)
    low_signal_report = tag_low_signal_content(chunks)
    penalty_report = apply_retrieval_quality_metadata(chunks)

    write_jsonl(kag_dir / "chunks.jsonl", chunks)
    write_jsonl(kag_dir / "kg_chunk_links.jsonl", kg_chunk_links)
    retrieval_report = update_retrieval_corpus(kag_dir, chunks)
    graph_manifest = write_graph_index(kag_dir, nodes, kg_edges, concept_edges, kg_chunk_links, question_links)

    report = {
        "generated_at": now_iso(),
        "kag_dir": rel_path(kag_dir),
        "core_span_link_repair": core_repair,
        "duplicates": duplicate_report,
        "low_signal_content": low_signal_report,
        "retrieval_penalties": penalty_report,
        "retrieval_corpus": retrieval_report,
        "graph_index": graph_manifest,
        "counts": {
            "chunks": len(chunks),
            "kg_chunk_links": len(kg_chunk_links),
        },
    }
    write_json(kag_dir / "quality_repair_report.json", report)
    update_manifest(kag_dir, chunks, kg_chunk_links, graph_manifest, report)
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Repair and tag quality metadata for the KAG BGE-M3 KB.")
    parser.add_argument("--kag-dir", type=Path, default=DEFAULT_KAG_DIR)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = repair(args.kag_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
