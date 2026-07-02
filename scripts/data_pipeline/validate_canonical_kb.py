from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CANONICAL_DIR = PROJECT_ROOT / "data" / "kb" / "canonical"


REQUIRED_FIELDS = {
    "sources": ["source_document_id", "source_layer", "source_type", "name", "path", "authority_level", "verification_status"],
    "nodes": ["node_id", "node_kind", "node_type", "canonical_name", "source_layer", "subject", "chapter", "source_document_id", "verification_status"],
    "edges": ["edge_id", "edge_kind", "source_node_id", "target_node_id", "predicate", "source_layer", "source_document_id", "verification_status"],
    "chunks": ["chunk_id", "source_chunk_id", "source_layer", "source_type", "source_document_id", "title", "text", "authority_level", "verification_status"],
    "questions": ["question_id", "source_question_id", "source_layer", "stem", "match_status", "verification_status"],
    "question_links": ["alignment_id", "from_id", "to_id", "relation", "method_chain", "verification_status"],
    "alignments": ["alignment_id", "from_id", "to_id", "relation", "method_chain", "verification_status"],
}


ID_FIELD = {
    "sources": "source_document_id",
    "nodes": "node_id",
    "edges": "edge_id",
    "chunks": "chunk_id",
    "questions": "question_id",
    "question_links": "alignment_id",
    "alignments": "alignment_id",
}


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return
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


def is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, list) and not value:
        return False
    return False


def validate_required(name: str, rows: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    required = REQUIRED_FIELDS.get(name, [])
    for index, row in enumerate(rows, 1):
        row_id = row.get(ID_FIELD.get(name, "id"), f"row:{index}")
        for field in required:
            if field not in row or is_missing(row.get(field)):
                errors.append(f"{name}:{row_id} missing required field {field}")
                if len(errors) >= 20:
                    return errors
    return errors


def validate_unique(name: str, rows: list[dict[str, Any]]) -> list[str]:
    field = ID_FIELD[name]
    counts = Counter(str(row.get(field) or "") for row in rows)
    return [f"{name}:{item_id} duplicated {count} times" for item_id, count in counts.items() if item_id and count > 1][:20]


def validate(canonical_dir: Path) -> dict[str, Any]:
    tables = {
        name: list(iter_jsonl(canonical_dir / f"{name}.jsonl"))
        for name in ("sources", "nodes", "edges", "chunks", "questions", "question_links", "alignments")
    }
    errors: list[str] = []
    for name, rows in tables.items():
        errors.extend(validate_required(name, rows))
        errors.extend(validate_unique(name, rows))

    source_ids = {row["source_document_id"] for row in tables["sources"]}
    node_ids = {row["node_id"] for row in tables["nodes"]}
    edge_ids = {row["edge_id"] for row in tables["edges"]}
    chunk_ids = {row["chunk_id"] for row in tables["chunks"]}
    question_ids = {row["question_id"] for row in tables["questions"]}
    graph_target_ids = node_ids | edge_ids

    for edge in tables["edges"]:
        if edge.get("source_node_id") not in node_ids:
            errors.append(f"edges:{edge.get('edge_id')} source_node_id not found")
        if edge.get("target_node_id") not in node_ids:
            errors.append(f"edges:{edge.get('edge_id')} target_node_id not found")
        if edge.get("source_document_id") not in source_ids:
            errors.append(f"edges:{edge.get('edge_id')} source_document_id not found")
        if len(errors) >= 50:
            break

    for chunk in tables["chunks"]:
        if chunk.get("source_document_id") not in source_ids:
            errors.append(f"chunks:{chunk.get('chunk_id')} source_document_id not found")
        if len(errors) >= 50:
            break

    for link in tables["question_links"]:
        if link.get("from_id") not in question_ids:
            errors.append(f"question_links:{link.get('alignment_id')} from_id question not found")
        if link.get("to_id") not in graph_target_ids:
            errors.append(f"question_links:{link.get('alignment_id')} to_id graph target not found")
        if len(errors) >= 50:
            break

    for alignment in tables["alignments"]:
        if alignment.get("from_id") not in node_ids:
            errors.append(f"alignments:{alignment.get('alignment_id')} from_id node not found")
        if alignment.get("to_id") not in chunk_ids:
            errors.append(f"alignments:{alignment.get('alignment_id')} to_id chunk not found")
        if len(errors) >= 50:
            break

    report = {
        "ok": not errors,
        "counts": {name: len(rows) for name, rows in tables.items()},
        "errors": errors[:50],
    }
    report_path = canonical_dir / "validation_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate canonical KB field completeness and internal references.")
    parser.add_argument("--canonical-dir", type=Path, default=DEFAULT_CANONICAL_DIR)
    return parser.parse_args()


def main() -> None:
    report = validate(parse_args().canonical_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if not report["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
