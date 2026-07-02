from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import preview_span_semantic_chunks as chunker


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_KG_ROOT = PROJECT_ROOT / "data" / "core" / "knowledge-graph"
DEFAULT_KB_DIR = PROJECT_ROOT / "data" / "kb" / "span_semantic"
DEFAULT_OUT_DIR = PROJECT_ROOT / "data" / "kb" / "span_semantic_review"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [row for row in chunker.iter_jsonl(path)]


def load_span_rows(kg_root: Path) -> dict[str, dict[str, Any]]:
    return {row["span_id"]: row for row in chunker.iter_jsonl(kg_root / "evidence" / "spans.jsonl")}


def unit_window(units: list[dict[str, Any]], center_index: int, *, target_chars: int, hard_max_chars: int) -> list[dict[str, Any]]:
    if not units:
        return []
    by_index = {int(unit["unit_index"]): pos for pos, unit in enumerate(units)}
    if center_index not in by_index:
        pos = min(range(len(units)), key=lambda item: abs(int(units[item]["unit_index"]) - center_index))
    else:
        pos = by_index[center_index]
    start = end = pos

    def current_text() -> str:
        return chunker.chunk_text_from_units(units[start : end + 1])

    while len(current_text()) < target_chars and (start > 0 or end < len(units) - 1):
        left_len = len(units[start - 1]["text"]) if start > 0 else 10**9
        right_len = len(units[end + 1]["text"]) if end < len(units) - 1 else 10**9
        if left_len <= right_len and start > 0:
            candidate_start, candidate_end = start - 1, end
        elif end < len(units) - 1:
            candidate_start, candidate_end = start, end + 1
        else:
            break
        candidate_text = chunker.chunk_text_from_units(units[candidate_start : candidate_end + 1])
        if len(candidate_text) > hard_max_chars and len(current_text()) >= 300:
            break
        start, end = candidate_start, candidate_end
    return units[start : end + 1]


def make_span_context_window(
    units_by_span: dict[str, list[dict[str, Any]]],
    span_order: list[str],
    span_id: str,
    *,
    target_chars: int,
    hard_max_chars: int,
) -> tuple[list[dict[str, Any]], str]:
    windows = chunker.quote_alignment_windows(units_by_span, span_order, span_id)
    if not windows:
        return [], "missing_span_units"
    scope, units, _window_id = windows[0]
    text_len = len(chunker.chunk_text_from_units(units))
    if text_len <= hard_max_chars:
        return units, scope
    center = int(units[len(units) // 2]["unit_index"])
    return unit_window(units, center, target_chars=target_chars, hard_max_chars=hard_max_chars), f"{scope}_trimmed"


def best_fragment_hits(units: list[dict[str, Any]], quote: str) -> dict[str, Any]:
    body, char_to_unit = chunker.normalized_body_for_units(units)
    fragments = chunker.quote_fragments(quote)
    hits = []
    matched_chars = 0
    for fragment in fragments:
        pos = body.find(fragment)
        if pos >= 0:
            end = pos + len(fragment) - 1
            matched_chars += len(fragment)
            hits.append(
                {
                    "fragment": fragment,
                    "unit_start": char_to_unit[pos] if char_to_unit else None,
                    "unit_end": char_to_unit[end] if char_to_unit else None,
                }
            )
    total_chars = sum(len(fragment) for fragment in fragments)
    coverage = matched_chars / total_chars if total_chars else 0.0
    return {
        "coverage": round(coverage, 4),
        "matched_fragment_count": len(hits),
        "total_fragment_count": len(fragments),
        "matched_fragments": hits[:8],
        "unmatched_fragments": [fragment for fragment in fragments if all(hit["fragment"] != fragment for hit in hits)][:8],
    }


def classify_review_reason(quote: str, units: list[dict[str, Any]], hit_info: dict[str, Any]) -> str:
    if not units:
        return "missing_span_context"
    coverage = float(hit_info.get("coverage") or 0)
    matched = int(hit_info.get("matched_fragment_count") or 0)
    total = int(hit_info.get("total_fragment_count") or 0)
    quote_norm = chunker.match_norm(quote)
    body = chunker.match_norm(chunker.chunk_text_from_units(units))
    if coverage >= 0.4 or matched >= 2:
        return "partial_exact_fragments_need_manual_review"
    if any(token in quote for token in ["α", "β", "γ", "IgG4", "99m", "℃", "μ", "→", "±", "Ⅰ", "Ⅱ", "Ⅲ", "Ⅳ"]):
        return "symbol_or_subscript_ocr_variation"
    latin_quote = re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", quote)
    if latin_quote and any(token.lower() in body.lower() for token in latin_quote):
        return "latin_term_or_parenthesis_variation"
    if total <= 1 and len(quote_norm) <= 24:
        return "short_quote_insufficient_exact_anchor"
    return "no_exact_fragment_anchor_in_context"


def build_units_for_kg(
    kg_file: Path,
    *,
    span_rows: dict[str, dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], list[str], dict[str, Any]]:
    kg = json.loads(kg_file.read_text(encoding="utf-8"))
    kg_key = str(kg_file.resolve())
    source_structure = kg.get("source_structure") or {}
    base_heading_path = [chunker.text(source_structure.get("chapter_title"))]
    located = [item for item in kg.get("located_evidence") or [] if item.get("span_id")]
    span_ids = [item["span_id"] for item in located]
    matched_heading_by_span = {item["span_id"]: chunker.text(item.get("matched_heading")) for item in located}

    all_units: list[dict[str, Any]] = []
    for span_id in span_ids:
        span = span_rows.get(span_id)
        if not span:
            continue
        all_units.extend(
            chunker.split_span_to_units(
                span,
                kg_file=kg_key,
                base_heading_path=base_heading_path,
                matched_heading=matched_heading_by_span.get(span_id, ""),
            )
        )
    for index, unit in enumerate(all_units):
        unit["unit_index"] = index
        unit["kg_file"] = kg_key
        unit["source_structure"] = source_structure

    units_by_span: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for unit in all_units:
        units_by_span[chunker.text(unit.get("span_id"))].append(unit)
    return units_by_span, span_ids, source_structure


def main() -> None:
    parser = argparse.ArgumentParser(description="Build manual-review chunks for unresolved KG exact quotes.")
    parser.add_argument("--kg-root", type=Path, default=DEFAULT_KG_ROOT)
    parser.add_argument("--kb-dir", type=Path, default=DEFAULT_KB_DIR)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--target-chars", type=int, default=900)
    parser.add_argument("--hard-max-chars", type=int, default=1800)
    args = parser.parse_args()

    unresolved = read_jsonl(args.kb_dir / "unresolved_quote_alignments.jsonl")
    span_rows = load_span_rows(args.kg_root)

    chunks_by_span: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for chunk in chunker.iter_jsonl(args.kb_dir / "semantic_chunks.jsonl"):
        for span_id in chunk.get("source_span_ids") or []:
            chunks_by_span[span_id].append(
                {
                    "chunk_id": chunk.get("chunk_id"),
                    "heading_path": chunk.get("heading_path"),
                    "page_start": chunk.get("page_start"),
                    "page_end": chunk.get("page_end"),
                    "printed_page_start": chunk.get("printed_page_start"),
                    "printed_page_end": chunk.get("printed_page_end"),
                }
            )

    by_kg: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in unresolved:
        by_kg[row["kg_file"]].append(row)

    review_chunks: list[dict[str, Any]] = []
    review_index: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    reason_counter: Counter[str] = Counter()

    for kg_file_value, rows in sorted(by_kg.items()):
        kg_file = Path(kg_file_value)
        units_by_span, span_order, source_structure = build_units_for_kg(kg_file, span_rows=span_rows)
        kg_reason_counter: Counter[str] = Counter()
        for row in rows:
            span_id = row.get("span_id")
            units, scope = make_span_context_window(
                units_by_span,
                span_order,
                span_id,
                target_chars=args.target_chars,
                hard_max_chars=args.hard_max_chars,
            )
            hit_info = best_fragment_hits(units, row.get("exact_quote") or "")
            review_reason = classify_review_reason(row.get("exact_quote") or "", units, hit_info)
            reason_counter[review_reason] += 1
            kg_reason_counter[review_reason] += 1
            body = chunker.chunk_text_from_units(units)
            source_span_ids = chunker.unique_span_ids_for_units(units)
            unit_indices = [int(unit["unit_index"]) for unit in units] if units else []
            review_chunk_id = chunker.stable_id("reviewchunk", row["quote_id"], span_id, body)
            review_chunk = {
                "review_chunk_id": review_chunk_id,
                "chunk_type": "unresolved_quote_review_chunk",
                "kg_file": kg_file_value,
                "source_structure": source_structure,
                "quote_id": row.get("quote_id"),
                "source_kind": row.get("source_kind"),
                "source_id": row.get("source_id"),
                "declared_span_id": span_id,
                "source_span_ids": source_span_ids,
                "context_scope": scope,
                "heading_path": chunker.chunk_heading_path(units),
                "text": body,
                "unit_index_start": min(unit_indices) if unit_indices else None,
                "unit_index_end": max(unit_indices) if unit_indices else None,
                "printed_page_start": min(
                    (unit.get("printed_page_number") for unit in units if unit.get("printed_page_number") is not None),
                    default=None,
                ),
                "printed_page_end": max(
                    (unit.get("printed_page_number") for unit in units if unit.get("printed_page_number") is not None),
                    default=None,
                ),
                "exact_quote": row.get("exact_quote"),
                "review_status": "needs_manual_or_strict_rule_review",
                "review_reason": review_reason,
                "fragment_hit_info": hit_info,
                "main_chunk_candidates": chunks_by_span.get(span_id, [])[:10],
                "quality_flags": [
                    flag
                    for flag, enabled in [
                        ("context_empty", not body),
                        ("over_hard_max_review_context", len(body) > args.hard_max_chars),
                        ("declared_span_not_in_main_chunks", span_id not in chunks_by_span),
                    ]
                    if enabled
                ],
            }
            review_chunks.append(review_chunk)
            review_index.append(
                {
                    "review_chunk_id": review_chunk_id,
                    "quote_id": row.get("quote_id"),
                    "kg_file": kg_file_value,
                    "book_title": source_structure.get("book_title"),
                    "chapter_title": source_structure.get("chapter_title"),
                    "declared_span_id": span_id,
                    "review_reason": review_reason,
                    "fragment_coverage": hit_info.get("coverage"),
                    "matched_fragment_count": hit_info.get("matched_fragment_count"),
                    "total_fragment_count": hit_info.get("total_fragment_count"),
                    "main_chunk_candidate_count": len(chunks_by_span.get(span_id, [])),
                    "review_chunk_chars": len(body),
                }
            )
        summaries.append(
            {
                "kg_file": kg_file_value,
                "book_title": source_structure.get("book_title"),
                "chapter_title": source_structure.get("chapter_title"),
                "review_chunks": len(rows),
                "review_reason_counts": dict(sorted(kg_reason_counter.items())),
            }
        )

    lengths = [len(row.get("text") or "") for row in review_chunks]
    report = {
        "ok": len(review_chunks) == len(unresolved),
        "source_unresolved_file": str(args.kb_dir / "unresolved_quote_alignments.jsonl"),
        "out_dir": str(args.out_dir),
        "counts": {
            "unresolved_quotes": len(unresolved),
            "review_chunks": len(review_chunks),
            "kg_files": len(by_kg),
            "declared_spans": len({row.get("span_id") for row in unresolved}),
            "context_empty": sum(1 for row in review_chunks if "context_empty" in row.get("quality_flags", [])),
            "declared_span_not_in_main_chunks": sum(
                1 for row in review_chunks if "declared_span_not_in_main_chunks" in row.get("quality_flags", [])
            ),
            "over_hard_max_review_context": sum(
                1 for row in review_chunks if "over_hard_max_review_context" in row.get("quality_flags", [])
            ),
        },
        "review_chunk_length": {
            "min": min(lengths) if lengths else 0,
            "max": max(lengths) if lengths else 0,
            "avg": round(sum(lengths) / max(len(lengths), 1), 2),
        },
        "review_reason_counts": dict(sorted(reason_counter.items())),
        "top_kg_by_review_chunks": sorted(
            summaries,
            key=lambda row: row["review_chunks"],
            reverse=True,
        )[:30],
    }

    args.out_dir.mkdir(parents=True, exist_ok=True)
    chunker.write_jsonl(args.out_dir / "review_chunks.jsonl", review_chunks)
    chunker.write_jsonl(args.out_dir / "review_index.jsonl", review_index)
    chunker.write_jsonl(args.out_dir / "review_kg_summaries.jsonl", summaries)
    chunker.write_json(args.out_dir / "review_report.json", report)
    print(json.dumps({"ok": report["ok"], **report["counts"], "review_chunk_length": report["review_chunk_length"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
