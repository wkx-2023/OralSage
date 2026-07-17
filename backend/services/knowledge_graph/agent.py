from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DRAFT_DIRS = [
    PROJECT_ROOT / "data" / "core" / "knowledge-graph" / "manual-kg-drafts",
    # The bundled data archive keeps its data root under ``data/data``.
    # Keep the canonical location first while supporting that layout directly.
    PROJECT_ROOT / "data" / "data" / "core" / "knowledge-graph" / "manual-kg-drafts",
]
SOURCE_FORMAT = "oralpath-manual-kg-draft-v1"

BOOK_STYLE_BY_DOCUMENT = {
    "document.011c48d14b69bb34": {
        "title": "口腔颌面医学影像诊断学",
        "color": "#B394BF",
        "soft_color": "#F1E3F0",
    },
    "document.bada3a836e200b0e": {
        "title": "口腔颌面外科学",
        "color": "#A985B7",
        "soft_color": "#F0E2F3",
    },
    "document.fd0d62f3c358924b": {
        "title": "口腔临床药物学",
        "color": "#987CAF",
        "soft_color": "#EBE0F0",
    },
    "document.0e4388600d19c4d2": {
        "title": "口腔修复学",
        "color": "#8F79A6",
        "soft_color": "#E8DCEB",
    },
    "document.8c1ea1ece81d4b2c": {
        "title": "口腔材料学",
        "color": "#8674A0",
        "soft_color": "#E5DDEB",
    },
    "document.219248fd852423d4": {
        "title": "口腔组织病理学",
        "color": "#6F5F82",
        "soft_color": "#DED4E4",
    },
    "document.d84f54c596cd0e08": {
        "title": "口腔黏膜病学",
        "color": "#7A6B8F",
        "soft_color": "#E3D9E8",
    },
    "document.2509eddbd86404de": {
        "title": "口腔正畸学",
        "color": "#5F526E",
        "soft_color": "#D9D1DF",
    },
    "document.d6af9ce97e57b3c6": {
        "title": "口腔预防医学",
        "color": "#9D93B7",
        "soft_color": "#ECE7F3",
    },
    "document.eebd8c5722e0b28c": {
        "title": "儿童口腔医学",
        "color": "#B29CC1",
        "soft_color": "#F2E8F5",
    },
    "document.abae58f6831f4c1d": {
        "title": "牙体牙髓病学",
        "color": "#766C9D",
        "soft_color": "#E2DDED",
    },
    "document.d766e8629232a2f6": {
        "title": "牙周病学",
        "color": "#8B82AA",
        "soft_color": "#E8E3F0",
    },
    "document.f8989fec6cb8cc74": {
        "title": "口腔生物学",
        "color": "#A69ACC",
        "soft_color": "#ECE7F6",
    },
    "document.5d9280f70177ff2e": {
        "title": "口腔种植学",
        "color": "#7F789F",
        "soft_color": "#E5E1ED",
    },
}

PREDICATE_LABELS = {
    "HAS_ETIOLOGY": "病因",
    "HAS_PATHOGEN": "病原",
    "HAS_RISK_FACTOR": "危险因素",
    "HAS_SYMPTOM": "症状",
    "HAS_CLINICAL_SIGN": "体征",
    "HAS_IMAGING_FEATURE": "影像表现",
    "HAS_PATHOLOGICAL_FEATURE": "病理特征",
    "HAS_PATHOLOGICAL_PROCESS": "病理过程",
    "HAS_COMPLICATION": "并发症",
    "DIAGNOSED_BY": "诊断方法",
    "TREATED_BY": "治疗",
    "LOCATED_IN": "部位",
    "DIFFERENTIAL_FROM": "鉴别",
    "IS_SUBTYPE_OF": "分型",
}

SHARED_EDGE_LABELS = {
    "HAS_ETIOLOGY": "共享病因",
    "HAS_PATHOGEN": "共享病原",
    "HAS_RISK_FACTOR": "共享危险因素",
    "HAS_SYMPTOM": "症状重叠",
    "HAS_CLINICAL_SIGN": "体征重叠",
    "HAS_IMAGING_FEATURE": "影像相似",
    "HAS_PATHOLOGICAL_FEATURE": "病理相似",
    "HAS_PATHOLOGICAL_PROCESS": "机制相似",
    "HAS_COMPLICATION": "并发相关",
    "DIAGNOSED_BY": "诊断方法相近",
    "TREATED_BY": "治疗相近",
    "LOCATED_IN": "同部位",
    "DIFFERENTIAL_FROM": "鉴别关联",
}

TYPE_LABELS = {
    "Disease": "疾病",
    "DiseaseSubtype": "疾病亚型",
    "Etiology": "病因",
    "Pathogen": "病原",
    "RiskFactor": "危险因素",
    "Symptom": "症状",
    "ClinicalSign": "体征",
    "ImagingFeature": "影像表现",
    "PathologicalFeature": "病理特征",
    "PathologicalProcess": "病理过程",
    "DiagnosticMethod": "诊断方法",
    "Treatment": "治疗",
    "Complication": "并发症",
    "Anatomy": "解剖部位",
}

CORE_RELATION_PREDICATES = {"IS_SUBTYPE_OF", "DIFFERENTIAL_FROM"}
SUPPORT_TYPES_FOR_SHARED_EDGES = {
    "Etiology",
    "Pathogen",
    "RiskFactor",
    "Symptom",
    "ClinicalSign",
    "ImagingFeature",
    "PathologicalFeature",
    "PathologicalProcess",
    "DiagnosticMethod",
    "Treatment",
    "Complication",
    "Anatomy",
}


def _digest(value: str, size: int = 16) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:size]


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _iter_draft_files() -> list[Path]:
    seen: set[Path] = set()
    files: list[Path] = []
    for root in DRAFT_DIRS:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.kg-draft.json")):
            if path.name.lower() in {"index.json", "index.jsonl"}:
                continue
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            if resolved not in seen:
                seen.add(resolved)
                files.append(path)
    return files


def _load_drafts() -> list[dict[str, Any]]:
    drafts: list[dict[str, Any]] = []
    for path in _iter_draft_files():
        raw = _read_json(path)
        if not raw or raw.get("schema_version") != SOURCE_FORMAT:
            continue
        raw["_draft_path"] = path
        raw["_relative_path"] = _relative_display_path(path)
        drafts.append(raw)
    drafts.sort(
        key=lambda item: (
            str(item.get("source_structure", {}).get("book_title") or ""),
            _chapter_number(str(item.get("source_structure", {}).get("chapter_title") or "")),
            str(item.get("source_structure", {}).get("chapter_title") or ""),
            str(item.get("_relative_path") or ""),
        )
    )
    return drafts


def _normalize_document_id(document_id: str, path: Path | None = None) -> str:
    value = _text(document_id)
    if value:
        value = re.sub(r"\(\d+\)$", "", value)
    if value:
        return value
    if path:
        for part in path.parts:
            if part.startswith("document."):
                return re.sub(r"\(\d+\)$", "", part)
    return ""


def _book_style(document_id: str, raw_title: str) -> dict[str, str]:
    normalized_id = _normalize_document_id(document_id)
    if normalized_id in BOOK_STYLE_BY_DOCUMENT:
        return BOOK_STYLE_BY_DOCUMENT[normalized_id]
    title = _text(raw_title, "未命名教材")
    return {
        "title": title,
        "color": "#B394BF",
        "soft_color": "#F1E3F0",
    }


def _book_order(title: str) -> int:
    ordered_titles = [item["title"] for item in BOOK_STYLE_BY_DOCUMENT.values()]
    try:
        return ordered_titles.index(title)
    except ValueError:
        return len(ordered_titles)


def _relative_display_path(path: Path) -> str:
    try:
        return path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _chapter_number(title: str) -> int:
    match = re.search(r"第\s*([0-9０-９]+)\s*章", title)
    if match:
        try:
            return int(match.group(1).translate(str.maketrans("０１２３４５６７８９", "0123456789")))
        except ValueError:
            pass
    match = re.search(r"第\s*([一二三四五六七八九十百〇零两]+)\s*章", title)
    if match:
        return _chinese_number(match.group(1))
    return 9999


def _chinese_number(value: str) -> int:
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if not value:
        return 9999
    if value == "十":
        return 10
    if "十" in value:
        left, _, right = value.partition("十")
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        return tens * 10 + ones
    return digits.get(value, 9999)


def _node_key(book_title: str, node_type: str, canonical_name: str) -> str:
    return f"{book_title}\0{node_type}\0{canonical_name.strip()}"


def _support_key(node_type: str, canonical_name: str) -> str:
    return f"{node_type}\0{canonical_name.strip()}"


def stable_core_identity(book_title: str, node_type: str, canonical_name: str) -> str:
    """Public, side-effect-free stable core identity helper.

    Returns a stable ``core:<hex>`` identifier derived from the canonical
    ``(book_title, node_type, canonical_name)`` tuple.  This is the single
    source of truth for stable core ID computation — every consumer that
    needs to map a stable ``core:*`` ID to raw KG nodes MUST use this
    function (or a caller that itself calls this function).

    The returned ID is a prefix of the SHA-1 hex digest, truncated to 16
    characters, of ``book_title\\0node_type\\0canonical_name``.
    """
    return f"core:{_digest(_node_key(book_title, node_type, canonical_name))}"


def canonical_book_title(document_id: str, raw_title: str) -> str:
    """Return the canonical book title for a given (*document_id*, *raw_title*).

    This is the **single source of truth** for canonical book identity — the
    same ``_book_style`` logic that the overview uses to produce its
    ``book_title`` fields on every chapter / textbook record.

    Consumers that need to map raw KG nodes (whose ``book_title`` may carry
    long-form text with editors / editions / publishers) to stable
    ``core:*`` IDs MUST call this helper to normalise the raw title before
    computing :func:`stable_core_identity`.  Duplicating the normalisation
    rule in a downstream module is forbidden — this function is the sole
    owner of the canonical title derivation.
    """
    return _book_style(document_id, raw_title)["title"]


def _core_id(book_title: str, node_type: str, canonical_name: str) -> str:
    """Private compatibility alias for :func:`stable_core_identity`."""
    return stable_core_identity(book_title, node_type, canonical_name)


def _support_id(node_type: str, canonical_name: str) -> str:
    return f"support:{_digest(_support_key(node_type, canonical_name))}"


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any, fallback: str = "") -> str:
    return str(value).strip() if value is not None and str(value).strip() else fallback


def _unique_extend(target: list[Any], values: list[Any]) -> None:
    for value in values:
        if value not in target:
            target.append(value)


def _predicate_label(predicate: str) -> str:
    return PREDICATE_LABELS.get(predicate, predicate or "相关")


def _not_ready() -> dict[str, Any]:
    roots = [str(path.relative_to(PROJECT_ROOT)) for path in DRAFT_DIRS]
    return {
        "ok": False,
        "module": "oralpath_manual_knowledge_graph",
        "message": "尚未发现 oralpath-manual-kg-draft-v1 草稿 JSON。",
        "expected_locations": roots,
    }


@lru_cache(maxsize=1)
def _manual_kg_data() -> dict[str, Any]:
    drafts = _load_drafts()
    cards: dict[str, dict[str, Any]] = {}
    nodes: dict[str, dict[str, Any]] = {}
    support_catalog: dict[str, dict[str, Any]] = {}
    chapters: dict[str, dict[str, Any]] = {}
    textbooks: dict[str, dict[str, Any]] = {}
    local_core_maps: dict[str, dict[str, str]] = {}
    local_support_maps: dict[str, dict[str, dict[str, Any]]] = {}
    explicit_edges: list[dict[str, Any]] = []
    explicit_pair_index: dict[tuple[str, str], dict[str, Any]] = {}

    for draft_index, draft in enumerate(drafts):
        source = draft.get("source_structure") if isinstance(draft.get("source_structure"), dict) else {}
        draft_path_obj = draft.get("_draft_path") if isinstance(draft.get("_draft_path"), Path) else None
        raw_book_title = _text(source.get("book_title"), "未命名教材")
        raw_document_id = _text(source.get("document_id"), f"document-{draft_index + 1}")
        document_id = _normalize_document_id(raw_document_id, draft_path_obj)
        book_style = _book_style(document_id, raw_book_title)
        book_title = book_style["title"]
        version_id = _text(source.get("version_id"), "")
        chapter_title = _text(source.get("chapter_title"), "未命名章节")
        section_title = _text(source.get("section_title"), "")
        chapter_no = _chapter_number(chapter_title)
        chapter_key_source = f"{book_title}\0{document_id}\0{chapter_title}"
        chapter_key = f"chapter:{_digest(chapter_key_source)}"
        draft_key = f"draft:{_digest(str(draft.get('_relative_path') or draft_index))}"
        draft_file = _text(draft.get("draft_metadata", {}).get("draft_file"), Path(str(draft.get("_relative_path", ""))).name)
        draft_path = _text(draft.get("_relative_path"), draft_file)

        textbook = textbooks.setdefault(
            book_title,
            {
                "id": f"textbook:{_digest(book_title)}",
                "title": book_title,
                "document_ids": [],
                "chapter_keys": [],
                "core_ids": [],
                "color": book_style["color"],
                "soft_color": book_style["soft_color"],
                "raw_titles": [],
            },
        )
        _unique_extend(textbook["document_ids"], [document_id])
        _unique_extend(textbook["chapter_keys"], [chapter_key])
        _unique_extend(textbook["raw_titles"], [raw_book_title])

        chapter = chapters.setdefault(
            chapter_key,
            {
                "key": chapter_key,
                "book_title": book_title,
                "document_id": document_id,
                "version_id": version_id,
                "number": chapter_no,
                "title": chapter_title,
                "section_count": 0,
                "source_sections": [],
                "draft_files": [],
                "core_ids": [],
                "cores": [],
            },
        )
        if section_title and section_title not in chapter["source_sections"]:
            chapter["source_sections"].append(section_title)
            chapter["section_count"] = len(chapter["source_sections"])
        _unique_extend(chapter["draft_files"], [draft_path])

        core_ref_map: dict[str, str] = {}
        support_ref_map: dict[str, dict[str, Any]] = {}
        local_core_maps[draft_key] = core_ref_map
        local_support_maps[draft_key] = support_ref_map

        for support in _as_list(draft.get("support_nodes")):
            if not isinstance(support, dict):
                continue
            ref = _text(support.get("client_ref"))
            name = _text(support.get("canonical_name"))
            node_type = _text(support.get("type"), "Concept")
            if not ref or not name:
                continue
            sid = _support_id(node_type, name)
            support_ref_map[ref] = {
                "id": sid,
                "client_ref": ref,
                "type": node_type,
                "type_label": TYPE_LABELS.get(node_type, node_type),
                "name": name,
                "canonical_name": name,
                "aliases": _as_list(support.get("aliases")),
                "source_span_ids": _as_list(support.get("source_span_ids")),
            }
            existing = support_catalog.setdefault(sid, support_ref_map[ref].copy())
            _unique_extend(existing["aliases"], _as_list(support.get("aliases")))
            _unique_extend(existing["source_span_ids"], _as_list(support.get("source_span_ids")))

        for core in _as_list(draft.get("core_nodes")):
            if not isinstance(core, dict):
                continue
            ref = _text(core.get("client_ref"))
            name = _text(core.get("canonical_name"))
            node_type = _text(core.get("type"), "Core")
            if not ref or not name:
                continue
            cid = _core_id(book_title, node_type, name)
            core_ref_map[ref] = cid
            core_source = core.get("source_structure") if isinstance(core.get("source_structure"), dict) else {}
            core_chapter_title = _text(core_source.get("chapter_title"), chapter_title)
            core_section_title = _text(core_source.get("section_title"), section_title)
            card = cards.setdefault(
                cid,
                {
                    "id": cid,
                    "client_ref": ref,
                    "name": name,
                    "canonical_name": name,
                    "type": node_type,
                    "type_label": TYPE_LABELS.get(node_type, node_type),
                    "aliases": [],
                    "book_title": book_title,
                    "book_color": book_style["color"],
                    "book_soft_color": book_style["soft_color"],
                    "document_ids": [],
                    "version_ids": [],
                    "chapter_sources": [],
                    "chapter_keys": [],
                    "section_titles": [],
                    "source_span_ids": [],
                    "source_structures": [],
                    "draft_files": [],
                    "support_relations": [],
                    "support_by_predicate": {},
                    "core_relations": [],
                    "concepts": [],
                    "dropped_items": [],
                    "review_summaries": [],
                },
            )
            _unique_extend(card["aliases"], _as_list(core.get("aliases")))
            _unique_extend(card["document_ids"], [document_id])
            if version_id:
                _unique_extend(card["version_ids"], [version_id])
            _unique_extend(card["chapter_keys"], [chapter_key])
            _unique_extend(card["section_titles"], [core_section_title] if core_section_title else [])
            _unique_extend(card["source_span_ids"], _as_list(core.get("source_span_ids")))
            _unique_extend(card["draft_files"], [draft_path])
            _unique_extend(
                card["chapter_sources"],
                [
                    {
                        "key": chapter_key,
                        "number": chapter_no,
                        "title": core_chapter_title,
                        "book_title": book_title,
                        "book_color": book_style["color"],
                    }
                ],
            )
            _unique_extend(
                card["source_structures"],
                [
                    {
                        "book_title": book_title,
                        "book_color": book_style["color"],
                        "document_id": document_id,
                        "version_id": version_id,
                        "chapter_title": core_chapter_title,
                        "section_title": core_section_title,
                        "draft_file": draft_path,
                    }
                ],
            )
            if cid not in chapter["core_ids"]:
                chapter["core_ids"].append(cid)
                chapter["cores"].append(
                    {
                        "id": cid,
                        "client_ref": ref,
                        "name": name,
                        "canonical_name": name,
                        "type": node_type,
                        "type_label": TYPE_LABELS.get(node_type, node_type),
                        "book_title": book_title,
                        "book_color": book_style["color"],
                    }
                )
            _unique_extend(textbook["core_ids"], [cid])

            nodes[cid] = {
                "id": cid,
                "type": "core",
                "label": name,
                "group": TYPE_LABELS.get(node_type, node_type),
                "properties": card,
            }

        chapter_concepts = _normalize_concepts(draft.get("concepts"))
        chapter_dropped = _normalize_dropped(draft.get("dropped_items"))
        review_summary = draft.get("review_summary") if isinstance(draft.get("review_summary"), dict) else {}
        for cid in core_ref_map.values():
            card = cards.get(cid)
            if not card:
                continue
            _unique_extend(card["concepts"], chapter_concepts)
            _unique_extend(card["dropped_items"], chapter_dropped)
            if review_summary:
                _unique_extend(card["review_summaries"], [review_summary])

        for edge in _as_list(draft.get("edges")):
            if not isinstance(edge, dict):
                continue
            subject_ref = _text(edge.get("subject_ref"))
            object_ref = _text(edge.get("object_ref"))
            predicate = _text(edge.get("predicate"), "RELATED_TO")
            source_id = core_ref_map.get(subject_ref)
            target_core_id = core_ref_map.get(object_ref)
            support = support_ref_map.get(object_ref)

            if source_id and target_core_id:
                edge_row = _core_edge_row(source_id, target_core_id, predicate, edge)
                explicit_edges.append(edge_row)
                explicit_pair_index[_pair_key(source_id, target_core_id)] = edge_row
                _append_core_relation(cards, source_id, target_core_id, predicate, edge, direction="outgoing")
                _append_core_relation(cards, target_core_id, source_id, predicate, edge, direction="incoming")
                continue

            if source_id and support:
                _append_support_relation(cards, source_id, predicate, support, edge)

    shared_edges = _shared_support_edges(cards, explicit_pair_index)
    all_edges = explicit_edges + shared_edges
    outgoing, incoming = _edge_indexes(all_edges)
    sorted_chapters = sorted(chapters.values(), key=lambda item: (_book_order(item["book_title"]), item["number"], item["title"]))
    sorted_nodes = sorted(nodes.values(), key=lambda item: (_book_order(item["properties"].get("book_title", "")), item["label"]))

    return {
        "draft_count": len(drafts),
        "textbooks": sorted(textbooks.values(), key=lambda item: _book_order(item["title"])),
        "chapters": sorted_chapters,
        "nodes": sorted_nodes,
        "edges": all_edges,
        "cards": cards,
        "by_id": nodes,
        "outgoing": outgoing,
        "incoming": incoming,
        "support_catalog": support_catalog,
    }


def _normalize_concepts(values: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in _as_list(values):
        if isinstance(item, dict):
            name = _text(item.get("canonical_name") or item.get("name"))
            if not name:
                continue
            rows.append(
                {
                    "client_ref": _text(item.get("client_ref")),
                    "name": name,
                    "canonical_name": name,
                    "reason_not_edge": _text(item.get("reason_not_edge")),
                    "evidence": _as_list(item.get("evidence")),
                }
            )
        elif item:
            rows.append({"name": str(item), "canonical_name": str(item), "reason_not_edge": "", "evidence": []})
    return rows


def _normalize_dropped(values: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in _as_list(values):
        if isinstance(item, dict):
            candidate = _text(item.get("candidate") or item.get("name"))
            if candidate:
                rows.append(
                    {
                        "candidate": candidate,
                        "reason": _text(item.get("reason")),
                        "quote": _text(item.get("quote")),
                    }
                )
        elif item:
            rows.append({"candidate": str(item), "reason": "", "quote": ""})
    return rows


def _pair_key(source: str, target: str) -> tuple[str, str]:
    return tuple(sorted((source, target)))


def _core_edge_row(source_id: str, target_id: str, predicate: str, edge: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "source": source_id,
        "target": target_id,
        "predicate": predicate,
        "evidence": edge.get("evidence", []),
        "importance": edge.get("importance"),
    }
    return {
        "id": f"edge:{_digest(json.dumps(payload, ensure_ascii=False, sort_keys=True))}",
        "source": source_id,
        "target": target_id,
        "type": predicate,
        "dimension": predicate,
        "label": _predicate_label(predicate),
        "properties": {
            "kind": "explicit_core_relation",
            "predicate": predicate,
            "support_type": edge.get("support_type"),
            "importance": edge.get("importance"),
            "evidence": _as_list(edge.get("evidence")),
        },
    }


def _append_core_relation(
    cards: dict[str, dict[str, Any]],
    source_id: str,
    target_id: str,
    predicate: str,
    edge: dict[str, Any],
    direction: str,
) -> None:
    source_card = cards.get(source_id)
    target_card = cards.get(target_id)
    if not source_card or not target_card:
        return
    source_card["core_relations"].append(
        {
            "node_id": target_id,
            "name": target_card.get("name"),
            "type": target_card.get("type"),
            "type_label": target_card.get("type_label"),
            "predicate": predicate,
            "label": _predicate_label(predicate),
            "direction": direction,
            "evidence": _as_list(edge.get("evidence")),
            "importance": edge.get("importance"),
        }
    )


def _append_support_relation(
    cards: dict[str, dict[str, Any]],
    source_id: str,
    predicate: str,
    support: dict[str, Any],
    edge: dict[str, Any],
) -> None:
    card = cards.get(source_id)
    if not card:
        return
    relation = {
        "support_id": support["id"],
        "client_ref": support.get("client_ref"),
        "name": support.get("name"),
        "canonical_name": support.get("canonical_name"),
        "type": support.get("type"),
        "type_label": support.get("type_label"),
        "predicate": predicate,
        "label": _predicate_label(predicate),
        "support_type": edge.get("support_type"),
        "importance": edge.get("importance"),
        "evidence": _as_list(edge.get("evidence")),
    }
    card["support_relations"].append(relation)
    card["support_by_predicate"].setdefault(predicate, []).append(relation)


def _shared_support_edges(
    cards: dict[str, dict[str, Any]],
    explicit_pair_index: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    by_support: dict[str, list[dict[str, Any]]] = {}
    for core_id, card in cards.items():
        for relation in card.get("support_relations", []):
            support_type = relation.get("type")
            if support_type not in SUPPORT_TYPES_FOR_SHARED_EDGES:
                continue
            key = str(relation.get("support_id") or "")
            if not key:
                continue
            by_support.setdefault(key, []).append({"core_id": core_id, "relation": relation})

    pair_payloads: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for rows in by_support.values():
        unique_rows = []
        seen_core_ids: set[str] = set()
        for row in rows:
            if row["core_id"] in seen_core_ids:
                continue
            seen_core_ids.add(row["core_id"])
            unique_rows.append(row)
        if len(unique_rows) < 2:
            continue
        for i, left in enumerate(unique_rows):
            for right in unique_rows[i + 1 :]:
                pair = _pair_key(left["core_id"], right["core_id"])
                pair_payloads.setdefault(pair, []).append(
                    {
                        "support_id": left["relation"].get("support_id"),
                        "name": left["relation"].get("name"),
                        "type": left["relation"].get("type"),
                        "type_label": left["relation"].get("type_label"),
                        "predicate": left["relation"].get("predicate"),
                        "label": left["relation"].get("label"),
                    }
                )

    shared_edges: list[dict[str, Any]] = []
    for pair, supports in pair_payloads.items():
        ranked = _rank_shared_supports(supports)
        if pair in explicit_pair_index:
            explicit_pair_index[pair]["properties"]["shared_supports"] = ranked[:8]
            continue
        source_id, target_id = pair
        primary = ranked[0] if ranked else {}
        predicate = str(primary.get("predicate") or "SHARES_SUPPORT")
        edge_type = f"SHARES_{predicate.removeprefix('HAS_')}" if predicate.startswith("HAS_") else "SHARES_SUPPORT"
        edge_id = f"shared:{_digest(source_id + target_id + json.dumps(ranked, ensure_ascii=False, sort_keys=True))}"
        shared_edges.append(
            {
                "id": edge_id,
                "source": source_id,
                "target": target_id,
                "type": edge_type,
                "dimension": predicate,
                "label": SHARED_EDGE_LABELS.get(predicate, "共享知识点"),
                "properties": {
                    "kind": "derived_shared_support",
                    "predicate": predicate,
                    "shared_supports": ranked[:8],
                    "reason": "两个核心知识点引用了同一 support 节点。",
                },
            }
        )
    return shared_edges


def _rank_shared_supports(supports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    priority = {
        "HAS_ETIOLOGY": 1,
        "HAS_PATHOGEN": 2,
        "HAS_RISK_FACTOR": 3,
        "HAS_SYMPTOM": 4,
        "HAS_CLINICAL_SIGN": 5,
        "HAS_IMAGING_FEATURE": 6,
        "HAS_PATHOLOGICAL_FEATURE": 7,
        "HAS_COMPLICATION": 8,
        "DIAGNOSED_BY": 9,
        "TREATED_BY": 10,
        "LOCATED_IN": 11,
        "DIFFERENTIAL_FROM": 12,
    }
    deduped: dict[str, dict[str, Any]] = {}
    for support in supports:
        key = str(support.get("support_id") or support.get("name"))
        if key and key not in deduped:
            deduped[key] = support
    return sorted(deduped.values(), key=lambda item: (priority.get(str(item.get("predicate")), 99), str(item.get("name") or "")))


def _edge_indexes(edges: list[dict[str, Any]]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    outgoing: dict[str, list[dict[str, Any]]] = {}
    incoming: dict[str, list[dict[str, Any]]] = {}
    for edge in edges:
        outgoing.setdefault(str(edge.get("source")), []).append(edge)
        incoming.setdefault(str(edge.get("target")), []).append(edge)
    return outgoing, incoming


def get_graph_summary() -> dict[str, Any]:
    data = _manual_kg_data()
    if not data["nodes"]:
        return _not_ready()
    return {
        "ok": True,
        "module": "oralpath_manual_knowledge_graph_summary",
        "source_format": SOURCE_FORMAT,
        "summary": {
            "draft_count": data["draft_count"],
            "textbook_count": len(data["textbooks"]),
            "chapter_count": len(data["chapters"]),
            "core_count": len(data["nodes"]),
            "core_edge_count": len(data["edges"]),
        },
    }


def get_graph_overview() -> dict[str, Any]:
    data = _manual_kg_data()
    if not data["nodes"]:
        return _not_ready()
    first_textbook = data["textbooks"][0] if data["textbooks"] else {"id": "", "title": "口腔医学教材"}
    return {
        "ok": True,
        "module": "oralpath_manual_knowledge_graph_overview",
        "source_format": SOURCE_FORMAT,
        "summary": {
            "draft_count": data["draft_count"],
            "textbook_count": len(data["textbooks"]),
            "chapter_count": len(data["chapters"]),
            "core_count": len(data["nodes"]),
            "core_edge_count": len(data["edges"]),
        },
        "textbook": first_textbook,
        "textbooks": data["textbooks"],
        "chapters": data["chapters"],
        "overview": {"nodes": data["nodes"], "edges": data["edges"]},
    }


def get_graph_node(node_id: str) -> dict[str, Any]:
    data = _manual_kg_data()
    node = data["by_id"].get(node_id)
    if not node:
        return {"ok": False, "module": "oralpath_manual_knowledge_graph_node", "message": f"Core not found: {node_id}"}
    outgoing = data["outgoing"].get(node_id, [])
    incoming = data["incoming"].get(node_id, [])
    return {
        "ok": True,
        "module": "oralpath_manual_knowledge_graph_node",
        "node": node,
        "card": data["cards"].get(node_id),
        "outgoing": outgoing[:80],
        "incoming": incoming[:80],
        "related_nodes": _related_nodes(data, outgoing[:80] + incoming[:80], node_id),
    }


def get_graph_neighborhood(node_id: str, depth: int = 1, limit: int = 120) -> dict[str, Any]:
    data = _manual_kg_data()
    if node_id not in data["by_id"]:
        return {"ok": False, "module": "oralpath_manual_knowledge_graph_neighborhood", "message": f"Core not found: {node_id}"}

    max_depth = max(1, min(int(depth or 1), 2))
    max_items = max(20, min(int(limit or 120), 260))
    seen_nodes = {node_id}
    frontier = {node_id}
    selected_edges: list[dict[str, Any]] = []
    seen_edges: set[str] = set()

    for _ in range(max_depth):
        next_frontier: set[str] = set()
        candidate_edges: list[dict[str, Any]] = []
        for current in frontier:
            candidate_edges.extend(data["outgoing"].get(current, []))
            candidate_edges.extend(data["incoming"].get(current, []))
        candidate_edges.sort(key=_relation_rank, reverse=True)
        for edge in candidate_edges:
            if len(selected_edges) >= max_items:
                break
            edge_id = str(edge.get("id"))
            if edge_id in seen_edges:
                continue
            source = str(edge.get("source"))
            target = str(edge.get("target"))
            if source not in data["by_id"] or target not in data["by_id"]:
                continue
            seen_edges.add(edge_id)
            selected_edges.append(edge)
            for adjacent in (source, target):
                if adjacent not in seen_nodes:
                    seen_nodes.add(adjacent)
                    next_frontier.add(adjacent)
        frontier = next_frontier
        if len(selected_edges) >= max_items:
            break

    return {
        "ok": True,
        "module": "oralpath_manual_knowledge_graph_neighborhood",
        "center": data["by_id"][node_id],
        "card": data["cards"].get(node_id),
        "nodes": [data["by_id"][item] for item in seen_nodes if item in data["by_id"]],
        "edges": selected_edges,
        "outgoing": [edge for edge in selected_edges if str(edge.get("source")) == node_id],
        "incoming": [edge for edge in selected_edges if str(edge.get("target")) == node_id],
        "related_nodes": _related_nodes(data, selected_edges, node_id),
    }


def _relation_rank(edge: dict[str, Any]) -> int:
    relation_type = str(edge.get("type") or "")
    if relation_type == "IS_SUBTYPE_OF":
        return 20
    if relation_type == "DIFFERENTIAL_FROM":
        return 18
    if relation_type.startswith("SHARES_ETIOLOGY") or relation_type.startswith("SHARES_PATHOGEN"):
        return 15
    if relation_type.startswith("SHARES_SYMPTOM") or relation_type.startswith("SHARES_CLINICAL_SIGN"):
        return 12
    if relation_type.startswith("SHARES_IMAGING") or relation_type.startswith("SHARES_PATHOLOGICAL"):
        return 11
    if relation_type.startswith("SHARES_"):
        return 9
    return 4


def _related_nodes(data: dict[str, Any], edges: list[dict[str, Any]], center_id: str) -> list[dict[str, Any]]:
    related: dict[str, dict[str, Any]] = {}
    for edge in edges:
        for key in ("source", "target"):
            node_id = str(edge.get(key))
            if node_id != center_id and node_id in data["by_id"]:
                related[node_id] = data["by_id"][node_id]
    return list(related.values())
