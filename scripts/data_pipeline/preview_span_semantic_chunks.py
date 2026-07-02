from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_KG_ROOT = PROJECT_ROOT / "data" / "core" / "knowledge-graph"
DEFAULT_OUT_DIR = PROJECT_ROOT / "data" / "kb" / "chunk_preview"


CHINESE_NUMERAL = "一二三四五六七八九十百千万"
BRACKET_HEADING_RE = re.compile(r"^(【[^】]{1,24}】)(.*)$")
CHAPTER_RE = re.compile(rf"^第[{CHINESE_NUMERAL}\d]+章\s*(.*)$")
SECTION_RE = re.compile(rf"^第[{CHINESE_NUMERAL}\d]+节\s*(.*)$")
ENUM_HEADING_RE = re.compile(rf"^[{CHINESE_NUMERAL}]+[、.．]\s*\S+")
PAREN_HEADING_RE = re.compile(rf"^[（(][{CHINESE_NUMERAL}\d]+[）)]\s*\S+")
NUMBERED_HEADING_RE = re.compile(r"^\d+[.．、]\s*\S+")
NUMBERED_ITEM_RE = re.compile(r"^\d+[.．、]\s*\S+")
IGNORABLE_MATCH_CHARS = set(" \t\r\n，。；：、,.!?！？;:“”‘’\"'（）()[]【】《》<>")
REFERENCE_CONTINUATION_RE = re.compile(r"^[&(]?\d{0,3}[)]?\s*[:：]\s*\d+[-－]\d+[.]?$")

NOISE_MARKERS = [
    "本资料仅用于学习交流使用",
    "获得更多医学资料",
    "版权所有",
    "图书在版编目",
    "CIP",
    "ISBN",
    "参考文献",
    "主要参考文献",
    "复习题",
    "思考题",
    "课后思考题",
]

SHORT_HEADING_SUFFIXES = (
    "导言",
    "概述",
    "总论",
    "原则",
    "分类",
    "病因",
    "病理",
    "病因病理",
    "临床表现",
    "诊断",
    "鉴别诊断",
    "诊断及鉴别诊断",
    "治疗",
    "预防",
    "并发症",
    "炎症",
    "疾病",
)


def text(value: Any) -> str:
    return str(value or "").strip()


def normalize_width(value: str) -> str:
    return unicodedata.normalize("NFKC", text(value))


def norm(value: str) -> str:
    return "".join(normalize_width(value).split())


def match_norm(value: str) -> str:
    return "".join(ch for ch in normalize_width(value) if ch not in IGNORABLE_MATCH_CHARS and not ch.isspace())


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


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            fh.write("\n")
            count += 1
    return count


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def chapter_aliases(chapter_title: str) -> set[str]:
    title = text(chapter_title)
    aliases = {norm(title)} if title else set()
    match = CHAPTER_RE.match(title)
    if match:
        aliases.add(norm(title[: title.find(match.group(1))].strip()) if match.group(1) else norm(title))
        aliases.add(norm(match.group(1)))
    return {item for item in aliases if item}


def is_noise_line(line: str) -> bool:
    value = text(line)
    if not value:
        return False
    if norm(value) in {"学习笔记", "习笔记", "笔记", "学习"}:
        return True
    if any(marker in value for marker in NOISE_MARKERS):
        return True
    if len(norm(value)) <= 1:
        return True
    if re.fullmatch(r"\d{1,4}", value):
        return True
    if value in {"学", "习", "笔", "记"}:
        return True
    if re.match(r"^(文档|画廊|图片)[:：]?[A-Z]{1,3}\d", value):
        return True
    return False


def is_reference_line(line: str) -> bool:
    value = text(line)
    if not value:
        return False
    if REFERENCE_CONTINUATION_RE.match(value):
        return True
    if re.match(r"^\d+\s*[.．]", value):
        has_year = bool(re.search(r"(?:19|20)\d{2}", value))
        has_citation_marker = bool(
            re.search(
                r"(出版社|杂志|学报|临床|Journal|Laryngoscope|Surg|Pathology|Tumours|Press|IARC|et\s*al|etal|Lyon|北京|Company|Saunders|Hamilton|Decker|J\s|Med|Dent|Oral|Maxillofac|Endod)",
                value,
                re.IGNORECASE,
            )
        )
        return has_year and has_citation_marker
    if re.match(r"^&?[A-Za-z]", value):
        return bool(
            re.search(
                r"(Genetics|Tumours|Pathology|Press|Laryngoscope|Surg|Journal|retrospec|population|classification|Handb|Neurol|ed\.|Saunders|Company|Decker|Endod|Dent|Oral|Maxillofac)",
                value,
                re.IGNORECASE,
            )
        )
    return False


def classify_line(line: str, *, chapter_title: str = "") -> str:
    value = text(line)
    if not value:
        return "blank"
    if norm(value) in chapter_aliases(chapter_title):
        return "page_header"
    if is_noise_line(value):
        return "noise"
    if re.match(r"^(图|表)\s*\d+[-－]\d+", value):
        return "caption"
    if BRACKET_HEADING_RE.match(value):
        return "clinical_heading"
    if CHAPTER_RE.match(value) or SECTION_RE.match(value):
        return "heading"
    if ENUM_HEADING_RE.match(value):
        return "heading"
    if PAREN_HEADING_RE.match(value):
        tail = re.sub(rf"^[（(][{CHINESE_NUMERAL}\d]+[）)]\s*", "", value).strip()
        if tail and not re.match(r"^[:：\d]", tail) and re.search(r"[\u4e00-\u9fff]", tail):
            return "heading"
    if NUMBERED_HEADING_RE.match(value) and len(value) <= 14:
        return "heading"
    if NUMBERED_ITEM_RE.match(value):
        return "numbered_item"
    if len(value) <= 18 and not re.search(r"[，。；：,.!?！？]", value):
        if value in SHORT_HEADING_SUFFIXES or value.endswith(SHORT_HEADING_SUFFIXES):
            return "heading"
    return "paragraph"


def merge_line(previous: str, current: str) -> str:
    previous = previous.rstrip()
    current = current.strip()
    if not previous:
        return current
    if re.search(r"[A-Za-z0-9,.;:，。；：、）)]$", previous) and re.match(r"^[A-Za-z0-9(（]", current):
        return previous + " " + current
    return previous + current


def update_heading_path(base_path: list[str], current_path: list[str], heading: str) -> list[str]:
    heading = text(heading)
    chapter = base_path[0] if base_path else ""
    if not heading:
        return list(current_path)
    if CHAPTER_RE.match(heading):
        return [chapter or heading]
    if SECTION_RE.match(heading):
        return [item for item in [chapter, heading] if item]
    if BRACKET_HEADING_RE.match(heading):
        trunk = [chapter] if chapter else []
        for item in current_path[1:]:
            if not BRACKET_HEADING_RE.match(text(item)):
                trunk.append(item)
        return [*trunk, heading] if heading not in trunk else trunk
    if ENUM_HEADING_RE.match(heading) or PAREN_HEADING_RE.match(heading):
        section = next((item for item in reversed(current_path[1:]) if SECTION_RE.match(text(item))), "")
        return [item for item in [chapter, section, heading] if item]
    return [item for item in [chapter, heading] if item]


def split_span_to_units(
    span: dict[str, Any],
    *,
    kg_file: str,
    base_heading_path: list[str],
    matched_heading: str = "",
) -> list[dict[str, Any]]:
    chapter_title = base_heading_path[0] if base_heading_path else ""
    lines = [line.strip() for line in text(span.get("text")).splitlines()]
    units: list[dict[str, Any]] = []
    current_lines: list[str] = []
    current_type = "paragraph"
    heading_path = [item for item in base_heading_path if item]

    def add_unit(unit_type: str, body: str) -> None:
        body = text(body)
        if not body:
            return
        unit_id = stable_id("unit", kg_file, span.get("span_id"), len(units), body)
        units.append(
            {
                "unit_id": unit_id,
                "document_id": span.get("document_id"),
                "span_id": span.get("span_id"),
                "page": span.get("page"),
                "pdf_page_index": span.get("pdf_page_index"),
                "printed_page_number": span.get("printed_page_number"),
                "unit_index_in_span": len(units),
                "unit_type": unit_type,
                "heading_path": list(heading_path),
                "span_heading_hint": matched_heading,
                "text": body,
            }
        )

    def flush() -> None:
        nonlocal current_lines, current_type
        body = "\n".join(line for line in current_lines if line).strip()
        if body:
            add_unit(current_type, body)
        current_lines = []
        current_type = "paragraph"

    for raw_line in lines:
        line = text(raw_line)
        line_type = classify_line(line, chapter_title=chapter_title)

        if line_type == "blank":
            flush()
            continue
        if line_type in {"page_header", "noise"}:
            flush()
            continue
        if line_type == "caption":
            flush()
            add_unit("caption", line)
            continue
        if line_type == "clinical_heading":
            flush()
            match = BRACKET_HEADING_RE.match(line)
            assert match is not None
            heading_label, rest = text(match.group(1)), text(match.group(2))
            heading_path = update_heading_path(base_heading_path, heading_path, heading_label)
            add_unit("heading", heading_label)
            if rest:
                current_type = "paragraph"
                current_lines = [rest]
            continue
        if line_type == "heading":
            flush()
            heading_path = update_heading_path(base_heading_path, heading_path, line)
            add_unit("heading", line)
            continue
        if line_type == "numbered_item":
            flush()
            current_type = "paragraph"
            current_lines = [line]
            continue

        if current_lines:
            current_lines[-1] = merge_line(current_lines[-1], line)
        else:
            current_type = "paragraph"
            current_lines = [line]

    flush()
    return units


def extract_quote_records(kg: dict[str, Any], kg_file: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for edge_idx, edge in enumerate(kg.get("edges") or []):
        edge_id = stable_id(
            "edge",
            kg_file,
            edge_idx,
            edge.get("subject_ref"),
            edge.get("predicate"),
            edge.get("object_ref"),
        )
        for ev_idx, evidence in enumerate(edge.get("evidence") or []):
            quote = text(evidence.get("exact_quote"))
            if not quote:
                continue
            records.append(
                {
                    "quote_id": stable_id("quote", kg_file, "edge", edge_idx, ev_idx, quote),
                    "source_kind": "edge",
                    "source_id": edge_id,
                    "edge_index": edge_idx,
                    "evidence_index": ev_idx,
                    "span_id": evidence.get("span_id"),
                    "exact_quote": quote,
                    "predicate": edge.get("predicate"),
                    "subject_ref": edge.get("subject_ref"),
                    "object_ref": edge.get("object_ref"),
                }
            )
    for concept_idx, concept in enumerate(kg.get("concepts") or []):
        concept_id = stable_id("concept", kg_file, concept_idx, concept.get("canonical_name"))
        for ev_idx, evidence in enumerate(concept.get("evidence") or []):
            quote = text(evidence.get("exact_quote"))
            if not quote:
                continue
            records.append(
                {
                    "quote_id": stable_id("quote", kg_file, "concept", concept_idx, ev_idx, quote),
                    "source_kind": "concept",
                    "source_id": concept_id,
                    "concept_index": concept_idx,
                    "evidence_index": ev_idx,
                    "span_id": evidence.get("span_id"),
                    "exact_quote": quote,
                    "concept_name": concept.get("canonical_name"),
                    "related_node_refs": concept.get("related_node_refs") or [],
                }
            )
    return records


def normalized_body_for_units(units: list[dict[str, Any]]) -> tuple[str, list[int]]:
    parts: list[str] = []
    char_to_unit: list[int] = []
    for unit in units:
        unit_index = int(unit["unit_index"])
        unit_norm = match_norm(unit.get("text") or "")
        if not unit_norm:
            continue
        parts.append(unit_norm)
        char_to_unit.extend([unit_index] * len(unit_norm))
    return "".join(parts), char_to_unit


def unique_span_ids_for_units(units: list[dict[str, Any]]) -> list[str]:
    span_ids: list[str] = []
    for unit in units:
        span_id = text(unit.get("span_id"))
        if span_id and span_id not in span_ids:
            span_ids.append(span_id)
    return span_ids


def aligned_units(units: list[dict[str, Any]], alignment: dict[str, Any]) -> list[dict[str, Any]]:
    if not units:
        return []
    start = int(alignment["unit_start"])
    end = int(alignment["unit_end"])
    return [unit for unit in units if start <= int(unit["unit_index"]) <= end]


def quote_fragments(normalized_quote: str) -> list[str]:
    cleaned = re.sub(r"(?:\.{3,}|…+)", "，", normalize_width(normalized_quote))
    fragments = re.split(r"[，,。.;；:：、!?！？]+", cleaned)
    return [match_norm(fragment) for fragment in fragments if len(match_norm(fragment)) >= 4]


def locate_by_fragments(
    normalized_body: str,
    char_to_unit: list[int],
    quote: str,
    *,
    status: str,
) -> dict[str, Any] | None:
    fragments = quote_fragments(quote)
    if not fragments:
        return None

    search_from = 0
    matches: list[dict[str, Any]] = []
    for fragment in fragments:
        start = normalized_body.find(fragment, search_from)
        if start < 0:
            continue
        end = start + len(fragment) - 1
        matches.append({"fragment": fragment, "start": start, "end": end})
        search_from = end + 1

    matched_chars = sum(len(item["fragment"]) for item in matches)
    total_chars = sum(len(item) for item in fragments)
    coverage = matched_chars / total_chars if total_chars else 0.0
    if len(matches) < 2 and not (len(matches) == 1 and coverage >= 0.75 and len(matches[0]["fragment"]) >= 8):
        return None
    if coverage < 0.45:
        return None

    first = matches[0]["start"]
    last = matches[-1]["end"]
    return {
        "unit_start": char_to_unit[first],
        "unit_end": char_to_unit[last],
        "match_status": status,
        "match_coverage": round(coverage, 4),
        "matched_fragment_count": len(matches),
        "total_fragment_count": len(fragments),
        "matched_fragments": [item["fragment"] for item in matches[:8]],
    }


def locate_quote_units(units: list[dict[str, Any]], quote: str) -> dict[str, Any] | None:
    raw_quote = normalize_width(quote)
    normalized_quote = match_norm(quote)
    if not normalized_quote:
        return None
    normalized_body, char_to_unit = normalized_body_for_units(units)
    if not normalized_body or not char_to_unit:
        return None

    start = normalized_body.find(normalized_quote)
    if start >= 0:
        end = start + len(normalized_quote) - 1
        return {
            "unit_start": char_to_unit[start],
            "unit_end": char_to_unit[end],
            "match_status": "verified_exact_quote",
            "match_coverage": 1.0,
            "matched_fragment_count": 1,
            "total_fragment_count": 1,
            "matched_fragments": [normalized_quote[:120]],
        }

    if re.search(r"(?:\.{3,}|…+)", raw_quote):
        return locate_by_fragments(
            normalized_body,
            char_to_unit,
            raw_quote,
            status="verified_ordered_ellipsis_fragments",
        )

    return locate_by_fragments(
        normalized_body,
        char_to_unit,
        raw_quote,
        status="verified_ordered_exact_fragments",
    )


def neighbor_span_ids(span_ids: list[str], span_id: str) -> list[str]:
    if span_id not in span_ids:
        return []
    index = span_ids.index(span_id)
    candidates = []
    for candidate_index in (index - 1, index + 1):
        if 0 <= candidate_index < len(span_ids):
            candidates.append(span_ids[candidate_index])
    return candidates


def quote_alignment_windows(
    units_by_span: dict[str, list[dict[str, Any]]],
    span_order: list[str],
    span_id: str,
) -> list[tuple[str, list[dict[str, Any]], str]]:
    windows: list[tuple[str, list[dict[str, Any]], str]] = []
    declared_units = units_by_span.get(span_id) or []
    if declared_units:
        windows.append(("declared_span", declared_units, span_id))
    if span_id in span_order:
        index = span_order.index(span_id)
        if index + 1 < len(span_order):
            next_span_id = span_order[index + 1]
            next_units = units_by_span.get(next_span_id) or []
            if declared_units and next_units:
                windows.append(("declared_plus_next_span", [*declared_units, *next_units], f"{span_id}|{next_span_id}"))
        if index - 1 >= 0:
            previous_span_id = span_order[index - 1]
            previous_units = units_by_span.get(previous_span_id) or []
            if previous_units and declared_units:
                windows.append(
                    ("previous_plus_declared_span", [*previous_units, *declared_units], f"{previous_span_id}|{span_id}")
                )
        for candidate_span_id in neighbor_span_ids(span_order, span_id):
            candidate_units = units_by_span.get(candidate_span_id) or []
            if candidate_units:
                windows.append(("adjacent_span", candidate_units, candidate_span_id))
    return windows


def locate_quotes_in_units(
    units_by_span: dict[str, list[dict[str, Any]]],
    quote_records: list[dict[str, Any]],
    *,
    span_order: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    located: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for quote in quote_records:
        span_id = text(quote.get("span_id"))
        for scope, candidate_units, window_id in quote_alignment_windows(units_by_span, span_order, span_id):
            alignment = locate_quote_units(candidate_units, quote["exact_quote"])
            if alignment:
                alignment["matched_span_ids"] = unique_span_ids_for_units(aligned_units(candidate_units, alignment))
                alignment["span_match_scope"] = scope
                alignment["span_match_window"] = window_id
                alignment["declared_span_id"] = span_id
                located.append({**quote, **alignment})
                break
        else:
            missing.append(quote)
    return located, missing


def merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not ranges:
        return []
    ranges = sorted((min(start, end), max(start, end)) for start, end in ranges)
    out = [ranges[0]]
    for start, end in ranges[1:]:
        last_start, last_end = out[-1]
        if start <= last_end:
            out[-1] = (last_start, max(last_end, end))
        else:
            out.append((start, end))
    return out


def build_segments(
    units: list[dict[str, Any]],
    located_quotes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    locked_ranges = merge_ranges([(int(q["unit_start"]), int(q["unit_end"])) for q in located_quotes])
    locked_by_start = {start: end for start, end in locked_ranges}
    locked_inside = {index for start, end in locked_ranges for index in range(start, end + 1)}

    segments: list[dict[str, Any]] = []
    index = 0
    while index < len(units):
        if index in locked_by_start:
            end = locked_by_start[index]
            segments.append({"unit_start": index, "unit_end": end, "units": units[index : end + 1], "locked": True})
            index = end + 1
            continue
        if index in locked_inside:
            index += 1
            continue
        segments.append({"unit_start": index, "unit_end": index, "units": [units[index]], "locked": False})
        index += 1
    return segments


def segment_text(segment: dict[str, Any]) -> str:
    return "\n".join(unit["text"] for unit in segment["units"] if unit.get("text")).strip()


def chunk_text_from_units(units: list[dict[str, Any]]) -> str:
    return "\n\n".join(unit["text"] for unit in units if unit.get("text")).strip()


def chunk_heading_path(units: list[dict[str, Any]]) -> list[str]:
    paths = [tuple(unit.get("heading_path") or []) for unit in units if unit.get("heading_path")]
    if not paths:
        return []
    counts = Counter(paths)
    best = max(paths, key=lambda path: (len(path), counts[path]))
    return list(best)


def is_major_heading_segment(segment: dict[str, Any]) -> bool:
    units = segment.get("units") or []
    if not units or units[0].get("unit_type") != "heading":
        return False
    heading = text(units[0].get("text"))
    if BRACKET_HEADING_RE.match(heading):
        return False
    return bool(SECTION_RE.match(heading) or ENUM_HEADING_RE.match(heading) or CHAPTER_RE.match(heading))


def is_low_value_chunk_text(body: str) -> bool:
    value = text(body)
    if len(value) < 80:
        return True
    if is_reference_line(value):
        return True
    parts = [part.strip() for part in re.split(r"\n+", value) if part.strip()]
    if parts:
        reference_parts = sum(1 for part in parts if is_reference_line(part))
        if reference_parts / len(parts) >= 0.6:
            return True
    compact = norm(value)
    if re.search(r"(参考文献|主要参考文献)$", compact):
        return True
    if len(parts) <= 3 and re.search(r"(出版社|杂志|Journal|Laryngoscope|Surg|Endod|Dent|Oral|Maxillofac)", value, re.IGNORECASE):
        if not re.search(r"(治疗|诊断|病因|临床|表现|机制|分类|原则)", value):
            return True
    return False


def build_chunks_for_units(
    units: list[dict[str, Any]],
    located_quotes: list[dict[str, Any]],
    *,
    kg_file: str,
    documents: dict[str, dict[str, Any]],
    target_chars: int,
    min_chars: int,
    soft_max_chars: int,
    hard_max_chars: int,
) -> list[dict[str, Any]]:
    quotes_by_unit: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for quote in located_quotes:
        for index in range(int(quote["unit_start"]), int(quote["unit_end"]) + 1):
            quotes_by_unit[index].append(quote)

    segments = build_segments(units, located_quotes)
    chunks: list[dict[str, Any]] = []
    current_units: list[dict[str, Any]] = []

    def current_len() -> int:
        return len(chunk_text_from_units(current_units))

    def flush(reason: str) -> None:
        nonlocal current_units
        if not current_units:
            return
        body = chunk_text_from_units(current_units)
        if not body:
            current_units = []
            return
        unit_indices = [int(unit["unit_index"]) for unit in current_units]
        chunk_quotes: list[dict[str, Any]] = []
        seen_quote_ids: set[str] = set()
        for unit_index in unit_indices:
            for quote in quotes_by_unit.get(unit_index, []):
                if quote["quote_id"] not in seen_quote_ids:
                    seen_quote_ids.add(quote["quote_id"])
                    chunk_quotes.append(quote)

        source_span_ids = []
        for unit in current_units:
            span_id = unit.get("span_id")
            if span_id and span_id not in source_span_ids:
                source_span_ids.append(span_id)

        document_ids = [unit.get("document_id") for unit in current_units if unit.get("document_id")]
        document_id = Counter(document_ids).most_common(1)[0][0] if document_ids else ""
        document = documents.get(document_id, {})
        related_edge_refs = sorted({q["source_id"] for q in chunk_quotes if q.get("source_kind") == "edge"})
        related_node_refs = sorted(
            {
                ref
                for quote in chunk_quotes
                for ref in [quote.get("subject_ref"), quote.get("object_ref"), *(quote.get("related_node_refs") or [])]
                if ref
            }
        )
        quality_flags: list[str] = []
        if len(body) > hard_max_chars:
            quality_flags.append("over_hard_max_due_to_locked_quote_or_long_unit")
        if all(unit.get("unit_type") in {"noise", "caption"} for unit in current_units) and not chunk_quotes:
            quality_flags.append("noise_or_caption_only")
        if not chunk_quotes and is_low_value_chunk_text(body):
            quality_flags.append("low_value_no_quote")

        chunk_id = stable_id("chunk", kg_file, source_span_ids, len(chunks), body)
        chunks.append(
            {
                "chunk_id": chunk_id,
                "chunk_type": "semantic_evidence_chunk_preview",
                "document_id": document_id,
                "source_type": text(document.get("source_type")),
                "source_title": text(document.get("title")),
                "source_span_ids": source_span_ids,
                "page_start": min((unit.get("page") for unit in current_units if unit.get("page") is not None), default=None),
                "page_end": max((unit.get("page") for unit in current_units if unit.get("page") is not None), default=None),
                "pdf_page_start": min(
                    (unit.get("pdf_page_index") for unit in current_units if unit.get("pdf_page_index") is not None),
                    default=None,
                ),
                "pdf_page_end": max(
                    (unit.get("pdf_page_index") for unit in current_units if unit.get("pdf_page_index") is not None),
                    default=None,
                ),
                "printed_page_start": min(
                    (unit.get("printed_page_number") for unit in current_units if unit.get("printed_page_number") is not None),
                    default=None,
                ),
                "printed_page_end": max(
                    (unit.get("printed_page_number") for unit in current_units if unit.get("printed_page_number") is not None),
                    default=None,
                ),
                "heading_path": chunk_heading_path(current_units),
                "text": body,
                "unit_ids": [unit["unit_id"] for unit in current_units],
                "unit_index_start": min(unit_indices),
                "unit_index_end": max(unit_indices),
                "related_node_refs": related_node_refs,
                "related_edge_refs": related_edge_refs,
                "evidence_quote_ids": [quote["quote_id"] for quote in chunk_quotes],
                "quote_match_statuses": sorted({quote["match_status"] for quote in chunk_quotes}),
                "quality_flags": quality_flags,
                "segmentation": {
                    "method": "span_units_quote_locked_semantic_merge",
                    "flush_reason": reason,
                    "target_chars": target_chars,
                    "min_chars": min_chars,
                    "soft_max_chars": soft_max_chars,
                    "hard_max_chars": hard_max_chars,
                },
            }
        )
        current_units = []

    for segment in segments:
        segment_units = segment["units"]
        if not segment_units:
            continue
        segment_has_quote = any(quotes_by_unit.get(int(unit["unit_index"])) for unit in segment_units)
        if all(unit.get("unit_type") in {"noise", "caption"} for unit in segment_units) and not segment_has_quote:
            continue

        proposed_units = [*current_units, *segment_units]
        proposed_len = len(chunk_text_from_units(proposed_units))
        segment_len = len(segment_text(segment))

        if current_units and is_major_heading_segment(segment) and current_len() >= min_chars:
            flush("major_heading_boundary")
        elif current_units and proposed_len > soft_max_chars and current_len() >= min_chars:
            flush("soft_max_boundary")
        elif current_units and proposed_len > hard_max_chars:
            flush("hard_max_boundary")

        current_units.extend(segment_units)
        if current_len() >= target_chars and not segment.get("locked") and current_len() >= min_chars:
            flush("target_reached")
        elif segment_len > hard_max_chars:
            flush("long_atomic_segment")

    flush("chapter_end")
    return [
        chunk
        for chunk in chunks
        if "noise_or_caption_only" not in chunk.get("quality_flags", [])
        and "low_value_no_quote" not in chunk.get("quality_flags", [])
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview KG quote-locked semantic chunking from evidence spans.")
    parser.add_argument("--kg-file", type=Path, default=None)
    parser.add_argument("--kg-file-env", default="", help="Read KG file path from this environment variable.")
    parser.add_argument("--kg-root", type=Path, default=DEFAULT_KG_ROOT)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--target-chars", type=int, default=900)
    parser.add_argument("--min-chars", type=int, default=300)
    parser.add_argument("--soft-max-chars", type=int, default=1400)
    parser.add_argument("--hard-max-chars", type=int, default=1800)
    args = parser.parse_args()

    kg_file_value = os.environ.get(args.kg_file_env) if args.kg_file_env else None
    if not kg_file_value and not args.kg_file:
        raise ValueError("Provide --kg-file or --kg-file-env.")
    kg_file = Path(kg_file_value or args.kg_file).resolve()
    kg = json.loads(kg_file.read_text(encoding="utf-8"))
    kg_key = str(kg_file)
    documents = {row["document_id"]: row for row in iter_jsonl(args.kg_root / "evidence" / "documents.jsonl")}
    span_rows = {row["span_id"]: row for row in iter_jsonl(args.kg_root / "evidence" / "spans.jsonl")}

    source_structure = kg.get("source_structure") or {}
    base_heading_path = [text(source_structure.get("chapter_title"))]
    located = [item for item in kg.get("located_evidence") or [] if item.get("span_id")]
    span_ids = [item["span_id"] for item in located]
    matched_heading_by_span = {item["span_id"]: text(item.get("matched_heading")) for item in located}
    quote_records = extract_quote_records(kg, kg_key)
    missing_spans = [span_id for span_id in span_ids if span_id not in span_rows]

    all_units: list[dict[str, Any]] = []
    for span_id in span_ids:
        span = span_rows.get(span_id)
        if not span:
            continue
        all_units.extend(
            split_span_to_units(
                span,
                kg_file=kg_key,
                base_heading_path=base_heading_path,
                matched_heading=matched_heading_by_span.get(span_id, ""),
            )
        )

    for index, unit in enumerate(all_units):
        unit["unit_index"] = index

    units_by_span: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for unit in all_units:
        units_by_span[text(unit.get("span_id"))].append(unit)

    located_quotes, missing_quotes = locate_quotes_in_units(units_by_span, quote_records, span_order=span_ids)
    chunks = build_chunks_for_units(
        all_units,
        located_quotes,
        kg_file=kg_key,
        documents=documents,
        target_chars=args.target_chars,
        min_chars=args.min_chars,
        soft_max_chars=args.soft_max_chars,
        hard_max_chars=args.hard_max_chars,
    )

    all_links: list[dict[str, Any]] = []
    quote_to_chunk: dict[str, str] = {}
    located_by_id = {quote["quote_id"]: quote for quote in located_quotes}
    for chunk in chunks:
        for quote_id in chunk.get("evidence_quote_ids") or []:
            quote = located_by_id.get(quote_id)
            if not quote or quote_id in quote_to_chunk:
                continue
            quote_to_chunk[quote_id] = chunk["chunk_id"]
            all_links.append(
                {
                    "link_id": stable_id("kgchunklink", quote_id, chunk["chunk_id"]),
                    "from_type": quote["source_kind"],
                    "from_id": quote["source_id"],
                    "to_type": "chunk",
                    "to_id": chunk["chunk_id"],
                    "span_id": quote.get("span_id"),
                    "declared_span_id": quote.get("declared_span_id", quote.get("span_id")),
                    "matched_span_ids": quote.get("matched_span_ids") or [],
                    "span_match_scope": quote.get("span_match_scope"),
                    "quote_id": quote_id,
                    "verification_status": quote["match_status"],
                    "match_coverage": quote.get("match_coverage"),
                    "matched_fragment_count": quote.get("matched_fragment_count"),
                    "total_fragment_count": quote.get("total_fragment_count"),
                }
            )

    chunk_lengths = [len(chunk["text"]) for chunk in chunks]
    over_hard = [chunk["chunk_id"] for chunk in chunks if len(chunk["text"]) > args.hard_max_chars]
    quote_ids = [quote["quote_id"] for quote in quote_records]
    broken_quote_ids = [quote_id for quote_id in quote_ids if quote_id not in quote_to_chunk]
    status_counts = Counter(quote["match_status"] for quote in located_quotes)
    scope_counts = Counter(quote.get("span_match_scope") for quote in located_quotes)
    report = {
        "ok": not missing_spans and not missing_quotes and not broken_quote_ids and not over_hard,
        "kg_file": str(kg_file),
        "source_structure": source_structure,
        "counts": {
            "located_spans": len(span_ids),
            "paragraph_units": len(all_units),
            "semantic_chunks": len(chunks),
            "quote_records": len(quote_records),
            "kg_chunk_links": len(all_links),
            "missing_spans": len(missing_spans),
            "missing_quote_alignments": len(missing_quotes),
            "broken_quote_to_chunk": len(broken_quote_ids),
            "over_hard_max_chunks": len(over_hard),
        },
        "quote_alignment_status_counts": dict(sorted(status_counts.items())),
        "quote_alignment_scope_counts": dict(sorted(scope_counts.items())),
        "chunk_length": {
            "min": min(chunk_lengths) if chunk_lengths else 0,
            "max": max(chunk_lengths) if chunk_lengths else 0,
            "avg": round(sum(chunk_lengths) / max(len(chunk_lengths), 1), 2),
        },
        "quality_gates": {
            "all_located_spans_found": not missing_spans,
            "all_quotes_aligned_to_units": not missing_quotes,
            "all_aligned_quotes_linked_to_chunks": not broken_quote_ids,
            "no_chunk_over_hard_max": not over_hard,
        },
        "failures": {
            "missing_spans": missing_spans[:20],
            "missing_quote_alignments": [
                {"quote_id": quote["quote_id"], "span_id": quote.get("span_id"), "quote": quote["exact_quote"][:160]}
                for quote in missing_quotes[:20]
            ],
            "broken_quote_ids": broken_quote_ids[:20],
            "over_hard_max_chunks": over_hard[:20],
        },
    }

    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(args.out_dir / "paragraph_units.jsonl", all_units)
    write_jsonl(args.out_dir / "semantic_chunks.jsonl", chunks)
    write_jsonl(args.out_dir / "kg_chunk_links.jsonl", all_links)
    write_json(args.out_dir / "validation_report.json", report)
    print(
        json.dumps(
            {"ok": report["ok"], "out_dir": str(args.out_dir), **report["counts"], "chunk_length": report["chunk_length"]},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
