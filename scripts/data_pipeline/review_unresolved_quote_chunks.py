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
DEFAULT_OUT_DIR = PROJECT_ROOT / "data" / "kb" / "span_semantic_review"


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    return chunker.write_jsonl(path, rows)


def write_json(path: Path, data: dict[str, Any]) -> None:
    chunker.write_json(path, data)


def review_norm(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value or ""))
    value = re.sub(r"[\s，。；：、,.!?！？;:“”‘’\"'（）()\[\]【】《》<>·•\-—_+=/\\|]", "", value)
    return value


def char_ngrams(value: str, n: int) -> set[str]:
    value = review_norm(value)
    if len(value) < n:
        return {value} if value else set()
    return {value[index : index + n] for index in range(len(value) - n + 1)}


def coarse_similarity(quote: str, candidate: str) -> float:
    q = review_norm(quote)
    c = review_norm(candidate)
    if not q or not c:
        return 0.0
    if q in c:
        return 1.0
    n = 2 if len(q) < 24 else 3
    q_grams = char_ngrams(q, n)
    c_grams = char_ngrams(c, n)
    if not q_grams:
        return 0.0
    recall = len(q_grams & c_grams) / len(q_grams)
    fragments = chunker.quote_fragments(quote)
    fragment_hits = sum(1 for fragment in fragments if fragment and fragment in c)
    fragment_recall = fragment_hits / len(fragments) if fragments else 0.0
    return round(0.78 * recall + 0.22 * fragment_recall, 4)


def sequence_metrics(quote: str, candidate: str) -> dict[str, float]:
    q = review_norm(quote)
    c = review_norm(candidate)
    if not q or not c:
        return {"score": 0.0, "lcs_coverage": 0.0, "ngram_recall": 0.0, "longest_block_ratio": 0.0}

    matcher = difflib.SequenceMatcher(None, q, c, autojunk=False)
    blocks = matcher.get_matching_blocks()
    matched = sum(block.size for block in blocks)
    longest = max((block.size for block in blocks), default=0)
    lcs_coverage = matched / len(q)
    longest_ratio = longest / len(q)
    n = 2 if len(q) < 24 else 3
    q_grams = char_ngrams(q, n)
    c_grams = char_ngrams(c, n)
    ngram_recall = len(q_grams & c_grams) / len(q_grams) if q_grams else 0.0

    fragments = chunker.quote_fragments(quote)
    fragment_hits = sum(1 for fragment in fragments if fragment and fragment in c)
    fragment_recall = fragment_hits / len(fragments) if fragments else 0.0

    score = 0.52 * lcs_coverage + 0.32 * ngram_recall + 0.10 * longest_ratio + 0.06 * fragment_recall
    return {
        "score": round(score, 4),
        "lcs_coverage": round(lcs_coverage, 4),
        "ngram_recall": round(ngram_recall, 4),
        "longest_block_ratio": round(longest_ratio, 4),
        "fragment_recall": round(fragment_recall, 4),
    }


def decision_for_metrics(metrics: dict[str, float], quote: str) -> tuple[str, str]:
    qlen = len(review_norm(quote))
    score = metrics["score"]
    lcs = metrics["lcs_coverage"]
    ngram = metrics["ngram_recall"]
    longest = metrics["longest_block_ratio"]
    fragment = metrics.get("fragment_recall", 0.0)

    if qlen <= 16:
        if score >= 0.84 and lcs >= 0.82 and ngram >= 0.70:
            return "accepted", "reviewed_high_confidence_ocr_variant"
        return "needs_manual_review", "short_quote_low_discrimination"

    if score >= 0.78 and lcs >= 0.72 and ngram >= 0.58:
        return "accepted", "reviewed_high_confidence_ocr_variant"
    if score >= 0.70 and lcs >= 0.64 and ngram >= 0.50 and (longest >= 0.22 or fragment >= 0.25):
        return "accepted", "reviewed_medium_confidence_ocr_variant"
    return "needs_manual_review", "insufficient_textual_similarity"


def split_parts(text: str) -> list[str]:
    parts = [part.strip() for part in re.split(r"\n\s*\n+", str(text or "")) if part.strip()]
    if len(parts) <= 1:
        parts = [part.strip() for part in str(text or "").splitlines() if part.strip()]
    if len(parts) <= 1 and len(str(text or "")) > 800:
        parts = [part.strip() for part in re.split(r"(?<=[。！？；;])", str(text or "")) if part.strip()]
    return parts


def candidate_windows_from_seed(
    parts: list[str],
    seed: int,
    *,
    min_chars: int,
    target_chars: int,
    hard_max_chars: int,
) -> list[tuple[int, int, str]]:
    windows: list[tuple[int, int, str]] = []
    for start in range(max(0, seed - 2), seed + 1):
        body_parts: list[str] = []
        for end in range(start, min(len(parts), seed + 5)):
            body_parts.append(parts[end])
            body = "\n\n".join(body_parts)
            if len(body) > hard_max_chars and end > start:
                break
            if end >= seed:
                windows.append((start, end, body))
            if len(body) >= target_chars:
                break
    if len(parts[seed]) <= hard_max_chars:
        windows.append((seed, seed, parts[seed]))
    return windows


def best_window_from_text(
    quote: str,
    text: str,
    *,
    target_chars: int = 900,
    min_chars: int = 260,
    hard_max_chars: int = 1600,
) -> dict[str, Any]:
    parts = split_parts(text)
    if not parts:
        return {"text": "", "part_start": None, "part_end": None, "metrics": sequence_metrics(quote, "")}

    part_scores = [(index, coarse_similarity(quote, part)) for index, part in enumerate(parts)]
    top_seeds = [index for index, score in sorted(part_scores, key=lambda item: item[1], reverse=True)[:8] if score > 0]
    if not top_seeds:
        top_seeds = [0]

    raw_windows: list[tuple[int, int, str]] = []
    if len("\n\n".join(parts)) <= hard_max_chars:
        raw_windows.append((0, len(parts) - 1, "\n\n".join(parts)))
    for seed in top_seeds:
        raw_windows.extend(
            candidate_windows_from_seed(
                parts,
                seed,
                min_chars=min_chars,
                target_chars=target_chars,
                hard_max_chars=hard_max_chars,
            )
        )

    deduped: dict[tuple[int, int], str] = {}
    for start, end, body in raw_windows:
        deduped[(start, end)] = body

    coarse_ranked = sorted(
        [(start, end, body, coarse_similarity(quote, body)) for (start, end), body in deduped.items()],
        key=lambda item: (item[3], -abs(len(item[2]) - target_chars)),
        reverse=True,
    )[:36]

    candidates: list[tuple[int, int, str, dict[str, float]]] = [
        (start, end, body, sequence_metrics(quote, body)) for start, end, body, _ in coarse_ranked
    ]

    best = max(candidates, key=lambda item: (item[3]["score"], item[3]["lcs_coverage"], -abs(len(item[2]) - target_chars)))
    start, end, body, metrics = best

    while len(body) < min_chars and (start > 0 or end + 1 < len(parts)):
        left_text = parts[start - 1] if start > 0 else ""
        right_text = parts[end + 1] if end + 1 < len(parts) else ""
        choose_left = bool(left_text) and (not right_text or len(left_text) <= len(right_text))
        if choose_left:
            proposed = "\n\n".join([left_text, body])
            if len(proposed) <= hard_max_chars:
                start -= 1
                body = proposed
            else:
                break
        else:
            proposed = "\n\n".join([body, right_text])
            if len(proposed) <= hard_max_chars:
                end += 1
                body = proposed
            else:
                break
    return {"text": body, "part_start": start, "part_end": end, "metrics": sequence_metrics(quote, body)}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.open("r", encoding="utf-8") if line.strip()]


def kg_span_order(kg_file: str) -> list[str]:
    kg = json.loads(Path(kg_file).read_text(encoding="utf-8"))
    return [item["span_id"] for item in kg.get("located_evidence") or [] if item.get("span_id")]


def candidate_chunks_for_quote(
    unresolved: dict[str, Any],
    *,
    chunks_by_kg: dict[str, list[dict[str, Any]]],
    chunks_by_kg_span: dict[tuple[str, str], list[dict[str, Any]]],
    span_rows: dict[str, dict[str, Any]],
    spans_by_doc: dict[str, list[dict[str, Any]]],
    span_order_cache: dict[str, list[str]],
) -> list[dict[str, Any]]:
    kg_file = unresolved["kg_file"]
    span_id = unresolved.get("span_id")
    candidates: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    def add_chunk(chunk: dict[str, Any], scope: str) -> None:
        chunk_id = chunk["chunk_id"]
        if chunk_id in seen_ids:
            return
        seen_ids.add(chunk_id)
        candidates.append({"candidate_type": "semantic_chunk", "candidate_scope": scope, "chunk": chunk})

    for chunk in chunks_by_kg_span.get((kg_file, span_id), []):
        add_chunk(chunk, "declared_span_chunk")

    span_order = span_order_cache.setdefault(kg_file, kg_span_order(kg_file))
    if span_id in span_order:
        index = span_order.index(span_id)
        neighbor_span_ids = span_order[max(0, index - 2) : min(len(span_order), index + 3)]
        for neighbor_span_id in neighbor_span_ids:
            for chunk in chunks_by_kg_span.get((kg_file, neighbor_span_id), []):
                add_chunk(chunk, "kg_neighbor_span_chunk")

    if len(candidates) < 10:
        quote = unresolved.get("exact_quote") or ""
        kg_candidates = []
        for chunk in chunks_by_kg.get(kg_file, []):
            if chunk["chunk_id"] in seen_ids:
                continue
            kg_candidates.append((coarse_similarity(quote, chunk.get("text") or ""), chunk))
        for _, chunk in sorted(kg_candidates, key=lambda item: item[0], reverse=True)[:8]:
            add_chunk(chunk, "same_kg_chunk")

    span = span_rows.get(span_id)
    if span:
        direct_span_ids = [span_id]
        document_spans = spans_by_doc.get(span.get("document_id"), [])
        page = span.get("pdf_page_index")
        if page is not None:
            for row in document_spans:
                other_page = row.get("pdf_page_index")
                if other_page is not None and abs(int(other_page) - int(page)) <= 2 and row["span_id"] not in direct_span_ids:
                    direct_span_ids.append(row["span_id"])
        for direct_span_id in direct_span_ids[:7]:
            direct = span_rows.get(direct_span_id)
            if direct:
                candidates.append({"candidate_type": "span_direct", "candidate_scope": "document_neighbor_span", "span": direct})

    return candidates


def score_candidate(unresolved: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    quote = unresolved.get("exact_quote") or ""
    if candidate["candidate_type"] == "semantic_chunk":
        chunk = candidate["chunk"]
        window = best_window_from_text(quote, chunk.get("text") or "")
        source = {
            "base_chunk_id": chunk.get("chunk_id"),
            "kg_file": chunk.get("kg_file"),
            "document_id": chunk.get("document_id"),
            "source_type": chunk.get("source_type"),
            "source_title": chunk.get("source_title"),
            "source_span_ids": chunk.get("source_span_ids") or [],
            "heading_path": chunk.get("heading_path") or [],
            "page_start": chunk.get("page_start"),
            "page_end": chunk.get("page_end"),
            "pdf_page_start": chunk.get("pdf_page_start"),
            "pdf_page_end": chunk.get("pdf_page_end"),
            "printed_page_start": chunk.get("printed_page_start"),
            "printed_page_end": chunk.get("printed_page_end"),
            "source_structure": chunk.get("source_structure") or {},
        }
    else:
        span = candidate["span"]
        window = best_window_from_text(quote, span.get("text") or "")
        source = {
            "base_chunk_id": "",
            "kg_file": unresolved.get("kg_file"),
            "document_id": span.get("document_id"),
            "source_type": "",
            "source_title": "",
            "source_span_ids": [span.get("span_id")],
            "heading_path": [],
            "page_start": span.get("page"),
            "page_end": span.get("page"),
            "pdf_page_start": span.get("pdf_page_index"),
            "pdf_page_end": span.get("pdf_page_index"),
            "printed_page_start": span.get("printed_page_number"),
            "printed_page_end": span.get("printed_page_number"),
            "source_structure": {},
        }

    decision, status = decision_for_metrics(window["metrics"], quote)
    return {
        **source,
        "candidate_type": candidate["candidate_type"],
        "candidate_scope": candidate["candidate_scope"],
        "window_text": window["text"],
        "part_start": window["part_start"],
        "part_end": window["part_end"],
        "review_decision": decision,
        "review_status": status,
        "review_metrics": window["metrics"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Smart review unresolved KG quote alignments and create supplemental chunks.")
    parser.add_argument("--kg-root", type=Path, default=DEFAULT_KG_ROOT)
    parser.add_argument("--kb-dir", type=Path, default=DEFAULT_KB_DIR)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()

    unresolved = load_jsonl(args.kb_dir / "unresolved_quote_alignments.jsonl")
    chunks = load_jsonl(args.kb_dir / "semantic_chunks.jsonl")
    span_rows = {row["span_id"]: row for row in chunker.iter_jsonl(args.kg_root / "evidence" / "spans.jsonl")}
    documents = {row["document_id"]: row for row in chunker.iter_jsonl(args.kg_root / "evidence" / "documents.jsonl")}

    chunks_by_kg: dict[str, list[dict[str, Any]]] = defaultdict(list)
    chunks_by_kg_span: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for chunk in chunks:
        kg_file = chunk["kg_file"]
        chunks_by_kg[kg_file].append(chunk)
        for span_id in chunk.get("source_span_ids") or []:
            chunks_by_kg_span[(kg_file, span_id)].append(chunk)

    spans_by_doc: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in span_rows.values():
        spans_by_doc[row.get("document_id")].append(row)
    for rows in spans_by_doc.values():
        rows.sort(key=lambda row: (row.get("pdf_page_index") is None, row.get("pdf_page_index") or 0, row.get("span_id") or ""))

    span_order_cache: dict[str, list[str]] = {}
    review_chunks: list[dict[str, Any]] = []
    review_links: list[dict[str, Any]] = []
    review_records: list[dict[str, Any]] = []
    manual_review: list[dict[str, Any]] = []

    for index, item in enumerate(unresolved, 1):
        candidates = candidate_chunks_for_quote(
            item,
            chunks_by_kg=chunks_by_kg,
            chunks_by_kg_span=chunks_by_kg_span,
            span_rows=span_rows,
            spans_by_doc=spans_by_doc,
            span_order_cache=span_order_cache,
        )
        scored = [score_candidate(item, candidate) for candidate in candidates]
        scored.sort(
            key=lambda row: (
                row["review_decision"] == "accepted",
                row["review_metrics"]["score"],
                row["review_metrics"]["lcs_coverage"],
                row["review_metrics"]["ngram_recall"],
            ),
            reverse=True,
        )
        best = scored[0] if scored else None
        if not best:
            record = {**item, "review_decision": "needs_manual_review", "review_status": "no_candidate_chunk_or_span"}
            manual_review.append(record)
            review_records.append(record)
            continue

        record = {
            **item,
            "review_decision": best["review_decision"],
            "review_status": best["review_status"],
            "review_metrics": best["review_metrics"],
            "candidate_type": best["candidate_type"],
            "candidate_scope": best["candidate_scope"],
            "base_chunk_id": best.get("base_chunk_id"),
            "source_span_ids": best.get("source_span_ids") or [],
            "heading_path": best.get("heading_path") or [],
            "text_preview": best["window_text"][:240],
        }
        review_records.append(record)

        if best["review_decision"] != "accepted":
            manual_review.append(record)
            continue

        document = documents.get(best.get("document_id"), {})
        source_title = best.get("source_title") or chunker.text(document.get("title"))
        source_type = best.get("source_type") or chunker.text(document.get("source_type"))
        chunk_id = chunker.stable_id("reviewchunk", item["quote_id"], best.get("base_chunk_id"), best["window_text"])
        related_edge_refs = [item["source_id"]] if item.get("source_kind") == "edge" and item.get("source_id") else []
        related_node_refs = [item["source_id"]] if item.get("source_kind") == "concept" and item.get("source_id") else []
        review_chunk = {
            "chunk_id": chunk_id,
            "chunk_type": "smart_review_quote_chunk",
            "base_chunk_id": best.get("base_chunk_id"),
            "document_id": best.get("document_id"),
            "source_type": source_type,
            "source_title": source_title,
            "source_span_ids": best.get("source_span_ids") or [],
            "kg_file": item.get("kg_file"),
            "source_structure": best.get("source_structure") or {},
            "heading_path": best.get("heading_path") or [],
            "page_start": best.get("page_start"),
            "page_end": best.get("page_end"),
            "pdf_page_start": best.get("pdf_page_start"),
            "pdf_page_end": best.get("pdf_page_end"),
            "printed_page_start": best.get("printed_page_start"),
            "printed_page_end": best.get("printed_page_end"),
            "text": best["window_text"],
            "evidence_quote_ids": [item["quote_id"]],
            "related_edge_refs": related_edge_refs,
            "related_node_refs": related_node_refs,
            "quality_flags": [],
            "review": {
                "method": "ocr_variant_textual_review",
                "decision": best["review_decision"],
                "status": best["review_status"],
                "metrics": best["review_metrics"],
                "candidate_type": best["candidate_type"],
                "candidate_scope": best["candidate_scope"],
                "part_start": best.get("part_start"),
                "part_end": best.get("part_end"),
            },
        }
        review_chunks.append(review_chunk)
        review_links.append(
            {
                "link_id": chunker.stable_id("reviewkgchunklink", item["quote_id"], chunk_id),
                "kg_file": item.get("kg_file"),
                "from_type": item.get("source_kind"),
                "from_id": item.get("source_id"),
                "to_type": "smart_review_chunk",
                "to_id": chunk_id,
                "base_chunk_id": best.get("base_chunk_id"),
                "quote_id": item["quote_id"],
                "span_id": item.get("span_id"),
                "declared_span_id": item.get("span_id"),
                "matched_span_ids": best.get("source_span_ids") or [],
                "verification_status": best["review_status"],
                "review_decision": best["review_decision"],
                **best["review_metrics"],
            }
        )

        if index % 100 == 0:
            print(f"reviewed {index}/{len(unresolved)}")

    decision_counts = Counter(row.get("review_decision") for row in review_records)
    status_counts = Counter(row.get("review_status") for row in review_records)
    scope_counts = Counter(row.get("candidate_scope") for row in review_records)
    chunk_lengths = [len(row["text"]) for row in review_chunks]
    report = {
        "input_unresolved_quote_alignments": len(unresolved),
        "review_records": len(review_records),
        "accepted_review_chunks": len(review_chunks),
        "accepted_review_links": len(review_links),
        "manual_review_needed": len(manual_review),
        "decision_counts": dict(sorted(decision_counts.items())),
        "status_counts": dict(sorted(status_counts.items())),
        "candidate_scope_counts": dict(sorted(scope_counts.items())),
        "review_chunk_length": {
            "min": min(chunk_lengths) if chunk_lengths else 0,
            "max": max(chunk_lengths) if chunk_lengths else 0,
            "avg": round(sum(chunk_lengths) / max(len(chunk_lengths), 1), 2),
        },
        "method": {
            "accepted_statuses": [
                "reviewed_high_confidence_ocr_variant",
                "reviewed_medium_confidence_ocr_variant",
            ],
            "note": "These links are smart-reviewed OCR/textual-variant links, not exact quote verification.",
        },
    }

    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(args.out_dir / "smart_review_chunks.jsonl", review_chunks)
    write_jsonl(args.out_dir / "smart_review_links.jsonl", review_links)
    write_jsonl(args.out_dir / "smart_review_records.jsonl", review_records)
    write_jsonl(args.out_dir / "manual_review_needed.jsonl", manual_review)
    write_json(args.out_dir / "smart_review_report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
