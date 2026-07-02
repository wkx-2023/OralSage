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


def review_norm(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"[\s，。；：、,.!?！？;:“”‘’\"'（）()\[\]【】《》<>·•\-—_+=/\\|]", "", value)


def grams(value: str, n: int) -> set[str]:
    value = review_norm(value)
    if not value:
        return set()
    if len(value) <= n:
        return {value}
    return {value[i : i + n] for i in range(len(value) - n + 1)}


def token_set(value: str) -> set[str]:
    value = unicodedata.normalize("NFKC", str(value or ""))
    return {m.group(0).lower() for m in re.finditer(r"[A-Za-z]+|\d+(?:\.\d+)?%?", value)}


def coarse_recall(quote: str, candidate: str) -> float:
    q = review_norm(quote)
    c = review_norm(candidate)
    if not q or not c:
        return 0.0
    if q in c:
        return 1.0
    n = 2 if len(q) < 24 else 3
    qg = grams(q, n)
    cg = grams(c, n)
    gram_recall = len(qg & cg) / len(qg) if qg else 0.0
    fragments = chunker.quote_fragments(quote)
    fragment_recall = sum(1 for frag in fragments if frag and frag in c) / len(fragments) if fragments else 0.0
    return round(0.82 * gram_recall + 0.18 * fragment_recall, 4)


def text_metrics(quote: str, candidate: str) -> dict[str, float]:
    q = review_norm(quote)
    c = review_norm(candidate)
    if not q or not c:
        return {
            "score": 0.0,
            "lcs_coverage": 0.0,
            "ngram_recall": 0.0,
            "fragment_recall": 0.0,
            "token_recall": 0.0,
            "longest_block_ratio": 0.0,
        }
    matcher = difflib.SequenceMatcher(None, q, c, autojunk=False)
    blocks = matcher.get_matching_blocks()
    matched = sum(block.size for block in blocks)
    longest = max((block.size for block in blocks), default=0)
    lcs_coverage = matched / len(q)
    longest_ratio = longest / len(q)
    n = 2 if len(q) < 24 else 3
    qg = grams(q, n)
    cg = grams(c, n)
    ngram_recall = len(qg & cg) / len(qg) if qg else 0.0
    fragments = chunker.quote_fragments(quote)
    fragment_recall = sum(1 for frag in fragments if frag and frag in c) / len(fragments) if fragments else 0.0
    qt = token_set(quote)
    ct = token_set(candidate)
    token_recall = len(qt & ct) / len(qt) if qt else 1.0
    score = 0.46 * lcs_coverage + 0.34 * ngram_recall + 0.10 * fragment_recall + 0.06 * token_recall + 0.04 * longest_ratio
    return {
        "score": round(score, 4),
        "lcs_coverage": round(lcs_coverage, 4),
        "ngram_recall": round(ngram_recall, 4),
        "fragment_recall": round(fragment_recall, 4),
        "token_recall": round(token_recall, 4),
        "longest_block_ratio": round(longest_ratio, 4),
    }


def decision(metrics: dict[str, float], quote: str) -> tuple[str, str]:
    qlen = len(review_norm(quote))
    score = metrics["score"]
    lcs = metrics["lcs_coverage"]
    ngram = metrics["ngram_recall"]
    token = metrics["token_recall"]
    longest = metrics["longest_block_ratio"]
    fragment = metrics["fragment_recall"]

    if qlen <= 16:
        if score >= 0.84 and lcs >= 0.82 and ngram >= 0.72 and token >= 0.80:
            return "accepted", "reviewed_high_confidence_ocr_variant"
        return "needs_manual_review", "short_quote_low_discrimination"

    if score >= 0.80 and lcs >= 0.74 and ngram >= 0.62 and token >= 0.70:
        return "accepted", "reviewed_high_confidence_ocr_variant"
    if score >= 0.70 and lcs >= 0.62 and ngram >= 0.52 and token >= 0.55 and (fragment >= 0.25 or longest >= 0.20):
        return "accepted", "reviewed_medium_confidence_ocr_variant"
    return "needs_manual_review", "insufficient_textual_similarity"


def split_segments(value: str) -> list[str]:
    value = str(value or "")
    parts = [p.strip() for p in re.split(r"\n\s*\n+", value) if p.strip()]
    if len(parts) <= 1:
        parts = [p.strip() for p in value.splitlines() if p.strip()]
    if len(parts) <= 1:
        parts = [p.strip() for p in re.split(r"(?<=[。！？；;])", value) if p.strip()]
    return parts or ([value.strip()] if value.strip() else [])


def best_window(quote: str, body: str, *, min_chars: int = 260, target_chars: int = 900, hard_max_chars: int = 1600) -> dict[str, Any]:
    parts = split_segments(body)
    if not parts:
        return {"text": "", "part_start": None, "part_end": None, "metrics": text_metrics(quote, "")}
    part_scores = sorted(
        [(idx, coarse_recall(quote, part)) for idx, part in enumerate(parts)],
        key=lambda item: item[1],
        reverse=True,
    )
    seeds = [idx for idx, score in part_scores[:6] if score > 0]
    if not seeds:
        seeds = [part_scores[0][0]]

    windows: dict[tuple[int, int], str] = {}
    for seed in seeds:
        for start in range(max(0, seed - 2), seed + 1):
            current: list[str] = []
            for end in range(start, min(len(parts), seed + 5)):
                current.append(parts[end])
                text = "\n\n".join(current)
                if len(text) > hard_max_chars and end > start:
                    break
                if end >= seed:
                    windows[(start, end)] = text
                if len(text) >= target_chars:
                    break
    if len("\n\n".join(parts)) <= hard_max_chars:
        windows[(0, len(parts) - 1)] = "\n\n".join(parts)

    ranked = sorted(
        [(start, end, text, coarse_recall(quote, text)) for (start, end), text in windows.items()],
        key=lambda item: (item[3], -abs(len(item[2]) - target_chars)),
        reverse=True,
    )[:18]
    scored = [(start, end, text, text_metrics(quote, text)) for start, end, text, _ in ranked]
    best = max(scored, key=lambda item: (item[3]["score"], item[3]["lcs_coverage"], -abs(len(item[2]) - target_chars)))
    start, end, text, metrics = best

    while len(text) < min_chars and (start > 0 or end + 1 < len(parts)):
        if end + 1 < len(parts):
            proposed = "\n\n".join([text, parts[end + 1]])
            if len(proposed) <= hard_max_chars:
                end += 1
                text = proposed
                continue
        if start > 0:
            proposed = "\n\n".join([parts[start - 1], text])
            if len(proposed) <= hard_max_chars:
                start -= 1
                text = proposed
                continue
        break
    return {"text": text, "part_start": start, "part_end": end, "metrics": text_metrics(quote, text)}


def kg_span_order(kg_file: str, cache: dict[str, list[str]]) -> list[str]:
    if kg_file not in cache:
        kg = json.loads(Path(kg_file).read_text(encoding="utf-8"))
        cache[kg_file] = [item["span_id"] for item in kg.get("located_evidence") or [] if item.get("span_id")]
    return cache[kg_file]


def candidate_sources(
    row: dict[str, Any],
    *,
    chunks_by_kg: dict[str, list[dict[str, Any]]],
    chunks_by_kg_span: dict[tuple[str, str], list[dict[str, Any]]],
    span_rows: dict[str, dict[str, Any]],
    documents: dict[str, dict[str, Any]],
    span_order_cache: dict[str, list[str]],
) -> list[dict[str, Any]]:
    kg_file = row["kg_file"]
    span_id = row.get("span_id")
    quote = row.get("exact_quote") or ""
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_chunk(chunk: dict[str, Any], scope: str) -> None:
        key = "chunk:" + chunk["chunk_id"]
        if key in seen:
            return
        seen.add(key)
        candidates.append({"type": "semantic_chunk", "scope": scope, "source": chunk, "text": chunk.get("text") or ""})

    def add_span(span: dict[str, Any], scope: str) -> None:
        key = "span:" + span["span_id"]
        if key in seen:
            return
        seen.add(key)
        doc = documents.get(span.get("document_id"), {})
        candidates.append(
            {
                "type": "span_text",
                "scope": scope,
                "source": span,
                "text": span.get("text") or "",
                "source_type": chunker.text(doc.get("source_type")),
                "source_title": chunker.text(doc.get("title")),
            }
        )

    for chunk in chunks_by_kg_span.get((kg_file, span_id), []):
        add_chunk(chunk, "declared_span_chunk")

    order = kg_span_order(kg_file, span_order_cache)
    if span_id in order:
        idx = order.index(span_id)
        for sid in order[max(0, idx - 2) : min(len(order), idx + 3)]:
            for chunk in chunks_by_kg_span.get((kg_file, sid), []):
                add_chunk(chunk, "neighbor_span_chunk")
            if sid in span_rows:
                add_span(span_rows[sid], "neighbor_span_text")

    if span_id in span_rows:
        add_span(span_rows[span_id], "declared_span_text")

    if len(candidates) < 6:
        same_kg = []
        for chunk in chunks_by_kg.get(kg_file, []):
            same_kg.append((coarse_recall(quote, chunk.get("text") or ""), chunk))
        for score, chunk in sorted(same_kg, key=lambda item: item[0], reverse=True)[:8]:
            if score > 0:
                add_chunk(chunk, "same_kg_top_chunk")

    return candidates


def source_metadata(candidate: dict[str, Any], documents: dict[str, dict[str, Any]]) -> dict[str, Any]:
    source = candidate["source"]
    if candidate["type"] == "semantic_chunk":
        return {
            "base_chunk_id": source.get("chunk_id"),
            "document_id": source.get("document_id"),
            "source_type": source.get("source_type"),
            "source_title": source.get("source_title"),
            "source_span_ids": source.get("source_span_ids") or [],
            "heading_path": source.get("heading_path") or [],
            "page_start": source.get("page_start"),
            "page_end": source.get("page_end"),
            "pdf_page_start": source.get("pdf_page_start"),
            "pdf_page_end": source.get("pdf_page_end"),
            "printed_page_start": source.get("printed_page_start"),
            "printed_page_end": source.get("printed_page_end"),
            "source_structure": source.get("source_structure") or {},
        }
    doc = documents.get(source.get("document_id"), {})
    return {
        "base_chunk_id": "",
        "document_id": source.get("document_id"),
        "source_type": chunker.text(doc.get("source_type")),
        "source_title": chunker.text(doc.get("title")),
        "source_span_ids": [source.get("span_id")],
        "heading_path": [],
        "page_start": source.get("page"),
        "page_end": source.get("page"),
        "pdf_page_start": source.get("pdf_page_index"),
        "pdf_page_end": source.get("pdf_page_index"),
        "printed_page_start": source.get("printed_page_number"),
        "printed_page_end": source.get("printed_page_number"),
        "source_structure": {},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Directly review unresolved_quote_alignments.jsonl and cut supplemental chunks.")
    parser.add_argument("--kg-root", type=Path, default=DEFAULT_KG_ROOT)
    parser.add_argument("--kb-dir", type=Path, default=DEFAULT_KB_DIR)
    parser.add_argument("--input", type=Path, default=None)
    args = parser.parse_args()

    input_path = args.input or args.kb_dir / "unresolved_quote_alignments.jsonl"
    unresolved = load_jsonl(input_path)
    chunks = load_jsonl(args.kb_dir / "semantic_chunks.jsonl")
    span_rows = {row["span_id"]: row for row in chunker.iter_jsonl(args.kg_root / "evidence" / "spans.jsonl")}
    documents = {row["document_id"]: row for row in chunker.iter_jsonl(args.kg_root / "evidence" / "documents.jsonl")}

    chunks_by_kg: dict[str, list[dict[str, Any]]] = defaultdict(list)
    chunks_by_kg_span: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for chunk in chunks:
        chunks_by_kg[chunk["kg_file"]].append(chunk)
        for sid in chunk.get("source_span_ids") or []:
            chunks_by_kg_span[(chunk["kg_file"], sid)].append(chunk)

    reviewed: list[dict[str, Any]] = []
    supplemental_chunks: list[dict[str, Any]] = []
    supplemental_links: list[dict[str, Any]] = []
    manual: list[dict[str, Any]] = []
    span_order_cache: dict[str, list[str]] = {}

    for index, row in enumerate(unresolved, 1):
        candidates = candidate_sources(
            row,
            chunks_by_kg=chunks_by_kg,
            chunks_by_kg_span=chunks_by_kg_span,
            span_rows=span_rows,
            documents=documents,
            span_order_cache=span_order_cache,
        )
        scored: list[dict[str, Any]] = []
        for candidate in candidates:
            window = best_window(row.get("exact_quote") or "", candidate["text"])
            metrics = window["metrics"]
            scored.append({"candidate": candidate, "window": window, "metrics": metrics})
        scored.sort(key=lambda item: (item["metrics"]["score"], item["metrics"]["lcs_coverage"], item["metrics"]["ngram_recall"]), reverse=True)

        if not scored:
            item = {**row, "review_decision": "needs_manual_review", "review_status": "no_candidate_found"}
            reviewed.append(item)
            manual.append(item)
            continue

        best = scored[0]
        dec, status = decision(best["metrics"], row.get("exact_quote") or "")
        meta = source_metadata(best["candidate"], documents)
        review_row = {
            **row,
            "review_decision": dec,
            "review_status": status,
            "review_metrics": best["metrics"],
            "candidate_type": best["candidate"]["type"],
            "candidate_scope": best["candidate"]["scope"],
            "base_chunk_id": meta["base_chunk_id"],
            "source_span_ids": meta["source_span_ids"],
            "heading_path": meta["heading_path"],
            "review_chunk_preview": best["window"]["text"][:260],
        }
        reviewed.append(review_row)

        if dec != "accepted":
            manual.append(review_row)
            continue

        chunk_id = chunker.stable_id("reviewchunk", row["quote_id"], meta["base_chunk_id"], best["window"]["text"])
        related_edge_refs = [row["source_id"]] if row.get("source_kind") == "edge" and row.get("source_id") else []
        related_node_refs = [row["source_id"]] if row.get("source_kind") == "concept" and row.get("source_id") else []
        supplemental_chunks.append(
            {
                "chunk_id": chunk_id,
                "chunk_type": "smart_review_quote_chunk",
                "base_chunk_id": meta["base_chunk_id"],
                "document_id": meta["document_id"],
                "source_type": meta["source_type"],
                "source_title": meta["source_title"],
                "source_span_ids": meta["source_span_ids"],
                "kg_file": row["kg_file"],
                "source_structure": meta["source_structure"],
                "heading_path": meta["heading_path"],
                "page_start": meta["page_start"],
                "page_end": meta["page_end"],
                "pdf_page_start": meta["pdf_page_start"],
                "pdf_page_end": meta["pdf_page_end"],
                "printed_page_start": meta["printed_page_start"],
                "printed_page_end": meta["printed_page_end"],
                "text": best["window"]["text"],
                "evidence_quote_ids": [row["quote_id"]],
                "related_edge_refs": related_edge_refs,
                "related_node_refs": related_node_refs,
                "quality_flags": [],
                "review": {
                    "input_file": str(input_path),
                    "method": "direct_unresolved_quote_review",
                    "decision": dec,
                    "status": status,
                    "metrics": best["metrics"],
                    "candidate_type": best["candidate"]["type"],
                    "candidate_scope": best["candidate"]["scope"],
                    "part_start": best["window"]["part_start"],
                    "part_end": best["window"]["part_end"],
                },
            }
        )
        supplemental_links.append(
            {
                "link_id": chunker.stable_id("reviewkgchunklink", row["quote_id"], chunk_id),
                "kg_file": row["kg_file"],
                "from_type": row.get("source_kind"),
                "from_id": row.get("source_id"),
                "to_type": "smart_review_chunk",
                "to_id": chunk_id,
                "base_chunk_id": meta["base_chunk_id"],
                "quote_id": row["quote_id"],
                "span_id": row.get("span_id"),
                "declared_span_id": row.get("span_id"),
                "matched_span_ids": meta["source_span_ids"],
                "verification_status": status,
                "review_decision": dec,
                **best["metrics"],
            }
        )
        if index % 100 == 0:
            print(f"reviewed {index}/{len(unresolved)}")

    report = {
        "input_file": str(input_path),
        "input_unresolved_quote_alignments": len(unresolved),
        "reviewed_records": len(reviewed),
        "accepted_supplemental_chunks": len(supplemental_chunks),
        "accepted_supplemental_links": len(supplemental_links),
        "manual_review_needed": len(manual),
        "decision_counts": dict(sorted(Counter(row["review_decision"] for row in reviewed).items())),
        "status_counts": dict(sorted(Counter(row["review_status"] for row in reviewed).items())),
        "candidate_scope_counts": dict(sorted(Counter(row.get("candidate_scope", "") for row in reviewed).items())),
        "chunk_length": {
            "min": min((len(row["text"]) for row in supplemental_chunks), default=0),
            "max": max((len(row["text"]) for row in supplemental_chunks), default=0),
            "avg": round(sum(len(row["text"]) for row in supplemental_chunks) / max(len(supplemental_chunks), 1), 2),
        },
        "note": "accepted links are smart-reviewed OCR/textual-variant links, not exact quote verification.",
    }

    write_jsonl(args.kb_dir / "unresolved_quote_alignments.reviewed.jsonl", reviewed)
    write_jsonl(args.kb_dir / "unresolved_quote_alignments.manual_review_needed.jsonl", manual)
    write_jsonl(args.kb_dir / "reviewed_supplemental_chunks.jsonl", supplemental_chunks)
    write_jsonl(args.kb_dir / "reviewed_supplemental_links.jsonl", supplemental_links)
    write_json(args.kb_dir / "unresolved_quote_review_report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
