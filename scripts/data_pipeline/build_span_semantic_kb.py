from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

import preview_span_semantic_chunks as chunker


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_KG_ROOT = PROJECT_ROOT / "data" / "core" / "knowledge-graph"
DEFAULT_OUT_DIR = PROJECT_ROOT / "data" / "kb" / "span_semantic"


def iter_kg_files(kg_root: Path) -> Iterable[Path]:
    yield from sorted((kg_root / "manual-kg-drafts").rglob("*.kg-draft.json"))


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    return chunker.write_jsonl(path, rows)


def write_json(path: Path, data: dict[str, Any]) -> None:
    chunker.write_json(path, data)


def build_one_kg(
    kg_file: Path,
    *,
    kg_root: Path,
    documents: dict[str, dict[str, Any]],
    span_rows: dict[str, dict[str, Any]],
    target_chars: int,
    min_chars: int,
    soft_max_chars: int,
    hard_max_chars: int,
) -> dict[str, Any]:
    kg = json.loads(kg_file.read_text(encoding="utf-8"))
    kg_key = str(kg_file.resolve())
    source_structure = kg.get("source_structure") or {}
    base_heading_path = [chunker.text(source_structure.get("chapter_title"))]
    located = [item for item in kg.get("located_evidence") or [] if item.get("span_id")]
    span_ids = [item["span_id"] for item in located]
    matched_heading_by_span = {item["span_id"]: chunker.text(item.get("matched_heading")) for item in located}
    quote_records = chunker.extract_quote_records(kg, kg_key)
    missing_spans = [span_id for span_id in span_ids if span_id not in span_rows]

    all_units: list[dict[str, Any]] = []
    for span_id in span_ids:
        span = span_rows.get(span_id)
        if not span:
            continue
        units = chunker.split_span_to_units(
            span,
            kg_file=kg_key,
            base_heading_path=base_heading_path,
            matched_heading=matched_heading_by_span.get(span_id, ""),
        )
        for unit in units:
            unit["kg_file"] = kg_key
            unit["source_structure"] = source_structure
        all_units.extend(units)

    for index, unit in enumerate(all_units):
        unit["unit_index"] = index

    units_by_span: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for unit in all_units:
        units_by_span[chunker.text(unit.get("span_id"))].append(unit)

    located_quotes, missing_quotes = chunker.locate_quotes_in_units(units_by_span, quote_records, span_order=span_ids)
    chunks = chunker.build_chunks_for_units(
        all_units,
        located_quotes,
        kg_file=kg_key,
        documents=documents,
        target_chars=target_chars,
        min_chars=min_chars,
        soft_max_chars=soft_max_chars,
        hard_max_chars=hard_max_chars,
    )

    for chunk in chunks:
        chunk["kg_file"] = kg_key
        chunk["source_structure"] = source_structure

    links: list[dict[str, Any]] = []
    quote_to_chunk: dict[str, str] = {}
    located_by_id = {quote["quote_id"]: quote for quote in located_quotes}
    for chunk in chunks:
        for quote_id in chunk.get("evidence_quote_ids") or []:
            quote = located_by_id.get(quote_id)
            if not quote or quote_id in quote_to_chunk:
                continue
            quote_to_chunk[quote_id] = chunk["chunk_id"]
            links.append(
                {
                    "link_id": chunker.stable_id("kgchunklink", quote_id, chunk["chunk_id"]),
                    "kg_file": kg_key,
                    "from_type": quote["source_kind"],
                    "from_id": quote["source_id"],
                    "to_type": "chunk",
                    "to_id": chunk["chunk_id"],
                    "span_id": quote.get("span_id"),
                    "declared_span_id": quote.get("declared_span_id", quote.get("span_id")),
                    "matched_span_ids": quote.get("matched_span_ids") or [],
                    "span_match_scope": quote.get("span_match_scope"),
                    "span_match_window": quote.get("span_match_window"),
                    "quote_id": quote_id,
                    "verification_status": quote["match_status"],
                    "match_coverage": quote.get("match_coverage"),
                    "matched_fragment_count": quote.get("matched_fragment_count"),
                    "total_fragment_count": quote.get("total_fragment_count"),
                }
            )

    unresolved = []
    for quote in missing_quotes:
        unresolved.append(
            {
                "kg_file": kg_key,
                "quote_id": quote["quote_id"],
                "source_kind": quote.get("source_kind"),
                "source_id": quote.get("source_id"),
                "span_id": quote.get("span_id"),
                "exact_quote": quote.get("exact_quote"),
                "reason": "not_verified_in_declared_or_adjacent_spans",
            }
        )

    chunk_lengths = [len(chunk["text"]) for chunk in chunks]
    over_hard = [chunk["chunk_id"] for chunk in chunks if len(chunk["text"]) > hard_max_chars]
    broken_quote_ids = [quote["quote_id"] for quote in quote_records if quote["quote_id"] not in quote_to_chunk]
    return {
        "kg_file": kg_key,
        "source_structure": source_structure,
        "units": all_units,
        "chunks": chunks,
        "links": links,
        "unresolved": unresolved,
        "summary": {
            "kg_file": kg_key,
            "book_title": source_structure.get("book_title"),
            "chapter_title": source_structure.get("chapter_title"),
            "theme_id": source_structure.get("theme_id"),
            "located_spans": len(span_ids),
            "paragraph_units": len(all_units),
            "semantic_chunks": len(chunks),
            "quote_records": len(quote_records),
            "kg_chunk_links": len(links),
            "missing_spans": len(missing_spans),
            "missing_quote_alignments": len(unresolved),
            "broken_quote_to_chunk": len(broken_quote_ids),
            "over_hard_max_chunks": len(over_hard),
            "chunk_length_min": min(chunk_lengths) if chunk_lengths else 0,
            "chunk_length_max": max(chunk_lengths) if chunk_lengths else 0,
            "chunk_length_avg": round(sum(chunk_lengths) / max(len(chunk_lengths), 1), 2),
            "quote_alignment_status_counts": dict(Counter(quote["match_status"] for quote in located_quotes)),
            "quote_alignment_scope_counts": dict(Counter(quote.get("span_match_scope") for quote in located_quotes)),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build full KG-derived semantic chunks from evidence spans.")
    parser.add_argument("--kg-root", type=Path, default=DEFAULT_KG_ROOT)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--target-chars", type=int, default=900)
    parser.add_argument("--min-chars", type=int, default=300)
    parser.add_argument("--soft-max-chars", type=int, default=1400)
    parser.add_argument("--hard-max-chars", type=int, default=1800)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    documents = {row["document_id"]: row for row in chunker.iter_jsonl(args.kg_root / "evidence" / "documents.jsonl")}
    span_rows = {row["span_id"]: row for row in chunker.iter_jsonl(args.kg_root / "evidence" / "spans.jsonl")}
    kg_files = list(iter_kg_files(args.kg_root))
    if args.limit:
        kg_files = kg_files[: args.limit]

    all_units: list[dict[str, Any]] = []
    all_chunks: list[dict[str, Any]] = []
    all_links: list[dict[str, Any]] = []
    all_unresolved: list[dict[str, Any]] = []
    kg_summaries: list[dict[str, Any]] = []

    for index, kg_file in enumerate(kg_files, 1):
        result = build_one_kg(
            kg_file,
            kg_root=args.kg_root,
            documents=documents,
            span_rows=span_rows,
            target_chars=args.target_chars,
            min_chars=args.min_chars,
            soft_max_chars=args.soft_max_chars,
            hard_max_chars=args.hard_max_chars,
        )
        all_units.extend(result["units"])
        all_chunks.extend(result["chunks"])
        all_links.extend(result["links"])
        all_unresolved.extend(result["unresolved"])
        kg_summaries.append(result["summary"])
        if index % 25 == 0 or index == len(kg_files):
            print(f"processed {index}/{len(kg_files)}")

    chunk_lengths = [len(chunk["text"]) for chunk in all_chunks]
    status_counter: Counter[str] = Counter()
    scope_counter: Counter[str] = Counter()
    for summary in kg_summaries:
        status_counter.update(summary.get("quote_alignment_status_counts") or {})
        scope_counter.update(summary.get("quote_alignment_scope_counts") or {})

    report = {
        "ok": not all_unresolved and not any(summary["missing_spans"] for summary in kg_summaries),
        "kg_root": str(args.kg_root),
        "out_dir": str(args.out_dir),
        "parameters": {
            "target_chars": args.target_chars,
            "min_chars": args.min_chars,
            "soft_max_chars": args.soft_max_chars,
            "hard_max_chars": args.hard_max_chars,
        },
        "counts": {
            "kg_files": len(kg_files),
            "paragraph_units": len(all_units),
            "semantic_chunks": len(all_chunks),
            "kg_chunk_links": len(all_links),
            "unresolved_quote_alignments": len(all_unresolved),
            "missing_spans": sum(summary["missing_spans"] for summary in kg_summaries),
            "over_hard_max_chunks": sum(summary["over_hard_max_chunks"] for summary in kg_summaries),
            "quote_records": sum(summary["quote_records"] for summary in kg_summaries),
        },
        "chunk_length": {
            "min": min(chunk_lengths) if chunk_lengths else 0,
            "max": max(chunk_lengths) if chunk_lengths else 0,
            "avg": round(sum(chunk_lengths) / max(len(chunk_lengths), 1), 2),
        },
        "quote_alignment_status_counts": dict(sorted(status_counter.items())),
        "quote_alignment_scope_counts": dict(sorted(scope_counter.items())),
        "top_unresolved_by_kg": sorted(
            [
                {
                    "kg_file": summary["kg_file"],
                    "book_title": summary.get("book_title"),
                    "chapter_title": summary.get("chapter_title"),
                    "missing_quote_alignments": summary["missing_quote_alignments"],
                    "quote_records": summary["quote_records"],
                }
                for summary in kg_summaries
                if summary["missing_quote_alignments"]
            ],
            key=lambda row: row["missing_quote_alignments"],
            reverse=True,
        )[:30],
    }

    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(args.out_dir / "paragraph_units.jsonl", all_units)
    write_jsonl(args.out_dir / "semantic_chunks.jsonl", all_chunks)
    write_jsonl(args.out_dir / "kg_chunk_links.jsonl", all_links)
    write_jsonl(args.out_dir / "unresolved_quote_alignments.jsonl", all_unresolved)
    write_jsonl(args.out_dir / "kg_file_summaries.jsonl", kg_summaries)
    write_json(args.out_dir / "build_report.json", report)
    print(json.dumps({"ok": report["ok"], **report["counts"], "chunk_length": report["chunk_length"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
