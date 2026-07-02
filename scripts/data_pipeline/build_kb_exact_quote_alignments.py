from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


DEFAULT_CANONICAL_DIR = PROJECT_ROOT / "data" / "kb" / "canonical"
DEFAULT_OUT = DEFAULT_CANONICAL_DIR / "alignments.jsonl"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def stable_id(prefix: str, *parts: Any, length: int = 24) -> str:
    payload = "\0".join(str(part or "") for part in parts)
    digest = hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()[:length]
    return f"{prefix}:{digest}"


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


def write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            fh.write("\n")
            count += 1
    return count


def normalize_text(value: str) -> str:
    value = str(value or "")
    value = re.sub(r"\s+", "", value)
    value = value.replace("，", ",").replace("。", ".").replace("；", ";").replace("：", ":")
    value = value.replace("（", "(").replace("）", ")")
    return value


def compact_quote(value: str, max_len: int = 220) -> str:
    value = " ".join(str(value or "").split())
    if len(value) <= max_len:
        return value
    return value[: max_len - 3].rstrip() + "..."


def load_nodes(canonical_dir: Path) -> dict[str, dict[str, Any]]:
    return {item["node_id"]: item for item in iter_jsonl(canonical_dir / "nodes.jsonl")}


def load_textbook_chunks(canonical_dir: Path) -> list[dict[str, Any]]:
    return [
        item
        for item in iter_jsonl(canonical_dir / "chunks.jsonl")
        if item.get("source_layer") == "textbook_evidence" and item.get("text")
    ]


def build_chunk_indexes(chunks: list[dict[str, Any]]) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    by_source_page: dict[str, list[dict[str, Any]]] = defaultdict(list)
    prepared: list[dict[str, Any]] = []
    for chunk in chunks:
        normalized = normalize_text(str(chunk.get("text") or ""))
        enriched = {**chunk, "_normalized_text": normalized}
        prepared.append(enriched)
        source = str(chunk.get("source") or "")
        page = chunk.get("page")
        by_source_page[f"{source}\0{page}"].append(enriched)
    return by_source_page, prepared


def candidate_chunks(
    evidence: dict[str, Any],
    source_node: dict[str, Any],
    target_node: dict[str, Any],
    by_source_page: dict[str, list[dict[str, Any]]],
    all_chunks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    page = evidence.get("pdf_page_index")
    if page is None:
        page = evidence.get("printed_page_number")
    source_candidates: list[dict[str, Any]] = []
    book_title = str(source_node.get("book_title") or target_node.get("book_title") or "")
    if page is not None:
        for key, rows in by_source_page.items():
            source, key_page = key.split("\0", 1)
            if str(page) != key_page:
                continue
            if book_title and book_title not in source:
                continue
            source_candidates.extend(rows)
    if source_candidates:
        return source_candidates

    subject = str(source_node.get("subject") or target_node.get("subject") or "")
    chapter = str(source_node.get("chapter") or target_node.get("chapter") or "")
    scoped = [
        chunk
        for chunk in all_chunks
        if (book_title and book_title in str(chunk.get("source") or ""))
        or (subject and subject in str(chunk.get("source") or ""))
        or (chapter and chapter and chapter in str(chunk.get("chapter") or ""))
    ]
    return scoped or all_chunks


def quote_match(quote: str, chunk: dict[str, Any]) -> bool:
    normalized_quote = normalize_text(quote)
    if len(normalized_quote) < 8:
        return False
    normalized_chunk = str(chunk.get("_normalized_text") or "")
    if normalized_quote in normalized_chunk:
        return True
    # OCR can drop punctuation or short connective phrases; require a long prefix for a conservative partial match.
    if len(normalized_quote) >= 30 and normalized_quote[:24] in normalized_chunk:
        return True
    return False


def alignment_record(
    edge: dict[str, Any],
    node_id: str,
    chunk: dict[str, Any],
    evidence: dict[str, Any],
    role: str,
) -> dict[str, Any]:
    quote = str(evidence.get("exact_quote") or "")
    alignment_id = stable_id("align", edge.get("edge_id"), node_id, chunk.get("chunk_id"), quote, role)
    return {
        "alignment_id": alignment_id,
        "from_id": node_id,
        "to_id": chunk.get("chunk_id"),
        "relation": "supports",
        "score": 1.0,
        "method_chain": ["kg_edge_exact_quote", "textbook_chunk_exact_quote"],
        "evidence_quote": compact_quote(quote),
        "verification_status": "verified_exact_quote",
        "metadata": {
            "edge_id": edge.get("edge_id"),
            "node_role": role,
            "predicate": edge.get("predicate"),
            "support_type": edge.get("support_type"),
            "importance": edge.get("importance"),
            "span_id": evidence.get("span_id"),
            "pdf_page_index": evidence.get("pdf_page_index"),
            "printed_page_number": evidence.get("printed_page_number"),
            "support_explanation": evidence.get("support_explanation"),
            "chunk_source": chunk.get("source"),
            "chunk_page": chunk.get("page"),
            "chunk_title": chunk.get("title"),
        },
    }


def build(canonical_dir: Path, out_path: Path) -> dict[str, Any]:
    nodes = load_nodes(canonical_dir)
    textbook_chunks = load_textbook_chunks(canonical_dir)
    by_source_page, prepared_chunks = build_chunk_indexes(textbook_chunks)
    alignments: list[dict[str, Any]] = []
    seen: set[str] = set()
    diagnostics: Counter[str] = Counter()

    for edge in iter_jsonl(canonical_dir / "edges.jsonl"):
        evidence_rows = edge.get("evidence") if isinstance(edge.get("evidence"), list) else []
        if not evidence_rows:
            diagnostics["edges_without_evidence"] += 1
            continue
        source_node_id = str(edge.get("source_node_id") or "")
        target_node_id = str(edge.get("target_node_id") or "")
        source_node = nodes.get(source_node_id, {})
        target_node = nodes.get(target_node_id, {})
        for evidence in evidence_rows:
            if not isinstance(evidence, dict):
                continue
            quote = str(evidence.get("exact_quote") or "")
            if not quote:
                diagnostics["evidence_without_exact_quote"] += 1
                continue
            candidates = candidate_chunks(evidence, source_node, target_node, by_source_page, prepared_chunks)
            matches = [chunk for chunk in candidates if quote_match(quote, chunk)]
            if not matches and candidates is not prepared_chunks:
                matches = [chunk for chunk in prepared_chunks if quote_match(quote, chunk)]
            if not matches:
                diagnostics["quote_not_found"] += 1
                continue
            diagnostics["quote_found"] += 1
            best = matches[0]
            for node_id, role in ((source_node_id, "edge_source"), (target_node_id, "edge_target")):
                if not node_id:
                    continue
                record = alignment_record(edge, node_id, best, evidence, role)
                if record["alignment_id"] in seen:
                    continue
                seen.add(record["alignment_id"])
                alignments.append(record)

    count = write_jsonl(out_path, alignments)
    report = {
        "generated_at": now_iso(),
        "method": "kg_edge_exact_quote_to_textbook_chunk",
        "output": out_path.as_posix(),
        "alignment_count": count,
        "diagnostics": dict(diagnostics),
    }
    report_path = out_path.with_suffix(".report.json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build verified KG-node to textbook-chunk alignments from exact KG evidence quotes.")
    parser.add_argument("--canonical-dir", type=Path, default=DEFAULT_CANONICAL_DIR)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = build(args.canonical_dir, args.out)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
