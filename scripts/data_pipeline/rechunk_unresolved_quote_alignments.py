from __future__ import annotations

import argparse
import difflib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

import preview_span_semantic_chunks as chunker


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_KG_ROOT = PROJECT_ROOT / "data" / "core" / "knowledge-graph"
DEFAULT_KB_DIR = PROJECT_ROOT / "data" / "kb" / "span_semantic"


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.open("r", encoding="utf-8") if line.strip()]


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    return chunker.write_jsonl(path, rows)


def write_json(path: Path, data: dict[str, Any]) -> None:
    chunker.write_json(path, data)


def clean_for_match(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"[\s，。；：、,.!?！？;:“”‘’\"'（）()\[\]【】《》<>·•\-—_+=/\\|]", "", value)


def char_grams(value: str, n: int) -> set[str]:
    value = clean_for_match(value)
    if not value:
        return set()
    if len(value) <= n:
        return {value}
    return {value[index : index + n] for index in range(len(value) - n + 1)}


def latin_tokens(value: str) -> set[str]:
    value = unicodedata.normalize("NFKC", str(value or "")).lower()
    return {match.group(0) for match in re.finditer(r"[a-z]+|\d+(?:\.\d+)?%?", value)}


def coarse_score(quote: str, body: str) -> float:
    q = clean_for_match(quote)
    b = clean_for_match(body)
    if not q or not b:
        return 0.0
    if q in b:
        return 1.0
    n = 2 if len(q) < 24 else 3
    qg = char_grams(q, n)
    bg = char_grams(b, n)
    gram_recall = len(qg & bg) / len(qg) if qg else 0.0
    fragments = chunker.quote_fragments(quote)
    fragment_recall = sum(1 for fragment in fragments if fragment and fragment in b) / len(fragments) if fragments else 0.0
    return round(0.84 * gram_recall + 0.16 * fragment_recall, 4)


def detailed_metrics(quote: str, body: str) -> dict[str, float]:
    q = clean_for_match(quote)
    b = clean_for_match(body)
    if not q or not b:
        return {
            "score": 0.0,
            "lcs_coverage": 0.0,
            "ngram_recall": 0.0,
            "fragment_recall": 0.0,
            "token_recall": 0.0,
            "longest_block_ratio": 0.0,
        }
    matcher = difflib.SequenceMatcher(None, q, b, autojunk=False)
    blocks = matcher.get_matching_blocks()
    matched = sum(block.size for block in blocks)
    longest = max((block.size for block in blocks), default=0)
    lcs_coverage = matched / len(q)
    longest_ratio = longest / len(q)
    n = 2 if len(q) < 24 else 3
    qg = char_grams(q, n)
    bg = char_grams(b, n)
    ngram_recall = len(qg & bg) / len(qg) if qg else 0.0
    fragments = chunker.quote_fragments(quote)
    fragment_recall = sum(1 for fragment in fragments if fragment and fragment in b) / len(fragments) if fragments else 0.0
    qt = latin_tokens(quote)
    bt = latin_tokens(body)
    token_recall = len(qt & bt) / len(qt) if qt else 1.0
    score = 0.46 * lcs_coverage + 0.34 * ngram_recall + 0.10 * fragment_recall + 0.06 * token_recall + 0.04 * longest_ratio
    return {
        "score": round(score, 4),
        "lcs_coverage": round(lcs_coverage, 4),
        "ngram_recall": round(ngram_recall, 4),
        "fragment_recall": round(fragment_recall, 4),
        "token_recall": round(token_recall, 4),
        "longest_block_ratio": round(longest_ratio, 4),
    }


def classify_match(quote: str, units: list[dict[str, Any]], metrics: dict[str, float]) -> tuple[str, str]:
    exact_alignment = chunker.locate_quote_units(units, quote)
    if exact_alignment:
        return "accepted", exact_alignment["match_status"]

    qlen = len(clean_for_match(quote))
    score = metrics["score"]
    lcs = metrics["lcs_coverage"]
    ngram = metrics["ngram_recall"]
    token = metrics["token_recall"]
    fragment = metrics["fragment_recall"]
    longest = metrics["longest_block_ratio"]

    if qlen <= 16:
        if score >= 0.84 and lcs >= 0.82 and ngram >= 0.72 and token >= 0.80:
            return "accepted", "reviewed_high_confidence_span_unit_ocr_variant"
        return "needs_manual_review", "short_quote_low_discrimination"

    if score >= 0.80 and lcs >= 0.74 and ngram >= 0.62 and token >= 0.70:
        return "accepted", "reviewed_high_confidence_span_unit_ocr_variant"
    if score >= 0.70 and lcs >= 0.62 and ngram >= 0.52 and token >= 0.55 and (fragment >= 0.25 or longest >= 0.20):
        return "accepted", "reviewed_medium_confidence_span_unit_ocr_variant"
    return "needs_manual_review", "low_confidence_span_unit_candidate"


def text_from_units(units: list[dict[str, Any]]) -> str:
    return chunker.chunk_text_from_units(units)


def unique_values(units: list[dict[str, Any]], key: str) -> list[Any]:
    values: list[Any] = []
    for unit in units:
        value = unit.get(key)
        if value not in values:
            values.append(value)
    return values


def heading_path_for_units(units: list[dict[str, Any]]) -> list[str]:
    if not units:
        return []
    return chunker.chunk_heading_path(units)


def major_heading(unit: dict[str, Any]) -> bool:
    if unit.get("unit_type") != "heading":
        return False
    value = chunker.text(unit.get("text"))
    if value.startswith("【"):
        return False
    return bool(re.match(r"^第[一二三四五六七八九十百千万\d]+[章节]", value) or re.match(r"^[一二三四五六七八九十]+[、.．]", value))


def make_window(
    units: list[dict[str, Any]],
    seed_position: int,
    *,
    min_chars: int,
    target_chars: int,
    hard_max_chars: int,
) -> list[dict[str, Any]]:
    start = seed_position
    end = seed_position

    while start > 0:
        prev = units[start - 1]
        if major_heading(prev) and len(text_from_units(units[start : end + 1])) >= min_chars:
            break
        proposed = units[start - 1 : end + 1]
        if len(text_from_units(proposed)) > hard_max_chars:
            break
        start -= 1
        if prev.get("unit_type") == "heading":
            break

    while end + 1 < len(units) and len(text_from_units(units[start : end + 1])) < target_chars:
        nxt = units[end + 1]
        if major_heading(nxt) and len(text_from_units(units[start : end + 1])) >= min_chars:
            break
        proposed = units[start : end + 2]
        if len(text_from_units(proposed)) > hard_max_chars:
            break
        end += 1

    while len(text_from_units(units[start : end + 1])) < min_chars and start > 0:
        proposed = units[start - 1 : end + 1]
        if len(text_from_units(proposed)) > hard_max_chars:
            break
        start -= 1

    return units[start : end + 1]


def kg_span_order(kg_file: str, cache: dict[str, list[str]]) -> list[str]:
    if kg_file not in cache:
        kg = json.loads(Path(kg_file).read_text(encoding="utf-8"))
        cache[kg_file] = [item["span_id"] for item in kg.get("located_evidence") or [] if item.get("span_id")]
    return cache[kg_file]


def candidate_unit_positions(
    quote: str,
    row: dict[str, Any],
    *,
    units_by_kg: dict[str, list[dict[str, Any]]],
    positions_by_kg_span: dict[tuple[str, str], list[int]],
    span_order_cache: dict[str, list[str]],
) -> list[tuple[int, str, float]]:
    kg_file = row["kg_file"]
    span_id = row.get("span_id")
    units = units_by_kg.get(kg_file, [])
    positions: list[tuple[int, str]] = []
    seen: set[int] = set()

    def add_positions(items: Iterable[int], scope: str) -> None:
        for pos in items:
            if pos not in seen:
                seen.add(pos)
                positions.append((pos, scope))

    add_positions(positions_by_kg_span.get((kg_file, span_id), []), "declared_span_units")
    order = kg_span_order(kg_file, span_order_cache)
    if span_id in order:
        index = order.index(span_id)
        for sid in order[max(0, index - 2) : min(len(order), index + 3)]:
            add_positions(positions_by_kg_span.get((kg_file, sid), []), "neighbor_span_units")

    scored = [(pos, scope, coarse_score(quote, units[pos].get("text") or "")) for pos, scope in positions]
    scored = [item for item in scored if item[2] > 0]
    if len(scored) < 4:
        same_kg = [(idx, "same_kg_units", coarse_score(quote, unit.get("text") or "")) for idx, unit in enumerate(units)]
        scored.extend([item for item in same_kg if item[2] > 0])

    best_by_pos: dict[int, tuple[int, str, float]] = {}
    for pos, scope, score in scored:
        old = best_by_pos.get(pos)
        if not old or score > old[2]:
            best_by_pos[pos] = (pos, scope, score)
    return sorted(best_by_pos.values(), key=lambda item: item[2], reverse=True)[:12]


def source_range(units: list[dict[str, Any]], key: str) -> tuple[Any, Any]:
    values = [unit.get(key) for unit in units if unit.get(key) is not None]
    return (min(values), max(values)) if values else (None, None)


def chunk_from_units(row: dict[str, Any], window_units: list[dict[str, Any]], decision: str, status: str, metrics: dict[str, float], scope: str) -> dict[str, Any]:
    body = text_from_units(window_units)
    source_span_ids = unique_values(window_units, "span_id")
    page_start, page_end = source_range(window_units, "page")
    pdf_page_start, pdf_page_end = source_range(window_units, "pdf_page_index")
    printed_page_start, printed_page_end = source_range(window_units, "printed_page_number")
    document_ids = [unit.get("document_id") for unit in window_units if unit.get("document_id")]
    document_id = Counter(document_ids).most_common(1)[0][0] if document_ids else ""
    source_structure = window_units[0].get("source_structure") or {}
    related_edge_refs = [row["source_id"]] if row.get("source_kind") == "edge" and row.get("source_id") else []
    related_node_refs = [row["source_id"]] if row.get("source_kind") == "concept" and row.get("source_id") else []
    quality_flags: list[str] = []
    if decision != "accepted":
        quality_flags.append("needs_manual_review")
    if len(body) > 1800:
        quality_flags.append("over_hard_max_due_to_unit_window")
    chunk_id = chunker.stable_id("unresolvedrechunk", row["quote_id"], source_span_ids, body)
    return {
        "chunk_id": chunk_id,
        "chunk_type": "unresolved_quote_span_derived_chunk",
        "kg_file": row["kg_file"],
        "document_id": document_id,
        "source_structure": source_structure,
        "source_span_ids": source_span_ids,
        "declared_span_id": row.get("span_id"),
        "page_start": page_start,
        "page_end": page_end,
        "pdf_page_start": pdf_page_start,
        "pdf_page_end": pdf_page_end,
        "printed_page_start": printed_page_start,
        "printed_page_end": printed_page_end,
        "heading_path": heading_path_for_units(window_units),
        "text": body,
        "unit_ids": [unit["unit_id"] for unit in window_units],
        "unit_index_start": min(int(unit["unit_index"]) for unit in window_units),
        "unit_index_end": max(int(unit["unit_index"]) for unit in window_units),
        "evidence_quote_ids": [row["quote_id"]],
        "related_edge_refs": related_edge_refs,
        "related_node_refs": related_node_refs,
        "quality_flags": quality_flags,
        "rechunk_match": {
            "source_input": "unresolved_quote_alignments.jsonl",
            "method": "paragraph_units_seed_window",
            "decision": decision,
            "status": status,
            "candidate_scope": scope,
            "metrics": metrics,
            "span_derived": True,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Re-chunk unresolved quote alignments using existing span-derived paragraph units.")
    parser.add_argument("--kg-root", type=Path, default=DEFAULT_KG_ROOT)
    parser.add_argument("--kb-dir", type=Path, default=DEFAULT_KB_DIR)
    parser.add_argument("--input", type=Path, default=None)
    parser.add_argument("--min-chars", type=int, default=260)
    parser.add_argument("--target-chars", type=int, default=900)
    parser.add_argument("--hard-max-chars", type=int, default=1800)
    args = parser.parse_args()

    input_path = args.input or args.kb_dir / "unresolved_quote_alignments.jsonl"
    unresolved = load_jsonl(input_path)
    paragraph_units = load_jsonl(args.kb_dir / "paragraph_units.jsonl")

    units_by_kg: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for unit in paragraph_units:
        units_by_kg[unit["kg_file"]].append(unit)
    for units in units_by_kg.values():
        units.sort(key=lambda item: int(item["unit_index"]))

    positions_by_kg_span: dict[tuple[str, str], list[int]] = defaultdict(list)
    for kg_file, units in units_by_kg.items():
        for pos, unit in enumerate(units):
            positions_by_kg_span[(kg_file, unit.get("span_id"))].append(pos)

    span_order_cache: dict[str, list[str]] = {}
    rechunked_chunks: list[dict[str, Any]] = []
    rechunked_links: list[dict[str, Any]] = []
    review_records: list[dict[str, Any]] = []

    for idx, row in enumerate(unresolved, 1):
        quote = row.get("exact_quote") or ""
        kg_file = row["kg_file"]
        units = units_by_kg.get(kg_file, [])
        candidates = candidate_unit_positions(
            quote,
            row,
            units_by_kg=units_by_kg,
            positions_by_kg_span=positions_by_kg_span,
            span_order_cache=span_order_cache,
        )
        scored_windows: list[tuple[list[dict[str, Any]], str, dict[str, float], float]] = []
        for pos, scope, seed_score in candidates:
            window_units = make_window(
                units,
                pos,
                min_chars=args.min_chars,
                target_chars=args.target_chars,
                hard_max_chars=args.hard_max_chars,
            )
            metrics = detailed_metrics(quote, text_from_units(window_units))
            scored_windows.append((window_units, scope, metrics, seed_score))

        if not scored_windows and units:
            window_units = make_window(units, 0, min_chars=args.min_chars, target_chars=args.target_chars, hard_max_chars=args.hard_max_chars)
            metrics = detailed_metrics(quote, text_from_units(window_units))
            scored_windows.append((window_units, "fallback_first_units", metrics, 0.0))

        if not scored_windows:
            record = {
                **row,
                "rechunk_decision": "needs_manual_review",
                "rechunk_status": "no_span_derived_units_found",
                "span_derived": False,
            }
            review_records.append(record)
            continue

        window_units, scope, metrics, seed_score = max(
            scored_windows,
            key=lambda item: (item[2]["score"], item[2]["lcs_coverage"], item[2]["ngram_recall"], item[3]),
        )
        decision, status = classify_match(quote, window_units, metrics)
        chunk = chunk_from_units(row, window_units, decision, status, metrics, scope)
        rechunked_chunks.append(chunk)
        link = {
            "link_id": chunker.stable_id("unresolvedrechunklink", row["quote_id"], chunk["chunk_id"]),
            "kg_file": row["kg_file"],
            "from_type": row.get("source_kind"),
            "from_id": row.get("source_id"),
            "to_type": "unresolved_quote_span_derived_chunk",
            "to_id": chunk["chunk_id"],
            "quote_id": row["quote_id"],
            "declared_span_id": row.get("span_id"),
            "matched_span_ids": chunk["source_span_ids"],
            "unit_index_start": chunk["unit_index_start"],
            "unit_index_end": chunk["unit_index_end"],
            "rechunk_decision": decision,
            "verification_status": status,
            "candidate_scope": scope,
            **metrics,
        }
        rechunked_links.append(link)
        review_records.append(
            {
                **row,
                "rechunk_decision": decision,
                "rechunk_status": status,
                "chunk_id": chunk["chunk_id"],
                "source_span_ids": chunk["source_span_ids"],
                "unit_index_start": chunk["unit_index_start"],
                "unit_index_end": chunk["unit_index_end"],
                "candidate_scope": scope,
                "metrics": metrics,
                "span_derived": True,
                "chunk_preview": chunk["text"][:260],
            }
        )
        if idx % 100 == 0:
            print(f"rechunked {idx}/{len(unresolved)}")

    accepted = [row for row in review_records if row.get("rechunk_decision") == "accepted"]
    manual = [row for row in review_records if row.get("rechunk_decision") != "accepted"]
    lengths = [len(chunk["text"]) for chunk in rechunked_chunks]
    report = {
        "input_file": str(input_path),
        "input_unresolved_quote_alignments": len(unresolved),
        "rechunked_span_derived_chunks": len(rechunked_chunks),
        "rechunked_links": len(rechunked_links),
        "accepted": len(accepted),
        "needs_manual_review": len(manual),
        "all_chunks_span_derived": all(chunk.get("unit_ids") and chunk.get("source_span_ids") for chunk in rechunked_chunks),
        "decision_counts": dict(sorted(Counter(row.get("rechunk_decision") for row in review_records).items())),
        "status_counts": dict(sorted(Counter(row.get("rechunk_status") for row in review_records).items())),
        "candidate_scope_counts": dict(sorted(Counter(row.get("candidate_scope") for row in review_records).items())),
        "chunk_length": {
            "min": min(lengths) if lengths else 0,
            "max": max(lengths) if lengths else 0,
            "avg": round(sum(lengths) / max(len(lengths), 1), 2),
        },
        "note": "Every output chunk is rebuilt from data/kb/span_semantic/paragraph_units.jsonl and preserves unit_ids/source_span_ids.",
    }

    write_jsonl(args.kb_dir / "unresolved_rechunked_span_chunks.jsonl", rechunked_chunks)
    write_jsonl(args.kb_dir / "unresolved_rechunked_span_links.jsonl", rechunked_links)
    write_jsonl(args.kb_dir / "unresolved_quote_alignments.rechunked.jsonl", review_records)
    write_jsonl(args.kb_dir / "unresolved_quote_alignments.rechunked_manual_review.jsonl", manual)
    write_json(args.kb_dir / "unresolved_rechunk_report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
