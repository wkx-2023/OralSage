from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .schemas import KGCoreCatalogItem, KGEdgeSummary, KGSupportSummary


def _support_index(support_nodes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {node.get("client_ref", ""): node for node in support_nodes if node.get("client_ref")}


def _core_index(core_nodes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {node.get("client_ref", ""): node for node in core_nodes if node.get("client_ref")}


def _node_name(ref: str, supports: dict[str, dict[str, Any]], cores: dict[str, dict[str, Any]]) -> tuple[str | None, str | None]:
    node = supports.get(ref) or cores.get(ref)
    if not node:
        return None, None
    return node.get("canonical_name"), node.get("type")


def _edge_ref(draft_file: Path, index: int, edge: dict[str, Any]) -> str:
    subject = edge.get("subject_ref", "subject")
    predicate = edge.get("predicate", "predicate")
    obj = edge.get("object_ref", "object")
    return f"{draft_file.as_posix()}#edge_{index:04d}_{subject}_{predicate}_{obj}"


def _load_draft(path: Path, root: Path) -> list[KGCoreCatalogItem]:
    data = json.loads(path.read_text(encoding="utf-8"))
    source = data.get("source_structure") or {}
    document_id = source.get("document_id") or path.parent.name
    version_id = source.get("version_id") or None
    book_title = source.get("book_title") or None
    chapter_title = source.get("chapter_title") or None
    draft_file = path.relative_to(root).as_posix()
    uid_draft_file = draft_file
    prefix = f"{document_id}/"
    if uid_draft_file.startswith(prefix):
        uid_draft_file = uid_draft_file[len(prefix) :]

    cores = _core_index(data.get("core_nodes") or [])
    supports = _support_index(data.get("support_nodes") or [])
    edges_by_core: dict[str, list[KGEdgeSummary]] = {}
    support_refs_by_core: dict[str, set[str]] = {}

    for index, edge in enumerate(data.get("edges") or [], start=1):
        subject_ref = edge.get("subject_ref")
        object_ref = edge.get("object_ref")
        if not subject_ref or subject_ref not in cores or not object_ref:
            continue
        object_name, object_type = _node_name(object_ref, supports, cores)
        summary = KGEdgeSummary(
            edge_ref=_edge_ref(path.relative_to(root), index, edge),
            predicate=str(edge.get("predicate") or ""),
            object_ref=object_ref,
            object_name=object_name,
            object_type=object_type,
        )
        edges_by_core.setdefault(subject_ref, []).append(summary)
        support_refs_by_core.setdefault(subject_ref, set()).add(object_ref)

    items: list[KGCoreCatalogItem] = []
    for core_ref, core in cores.items():
        core_source = core.get("source_structure") or {}
        section_title = core_source.get("section_title") or source.get("section_title") or None
        core_chapter = core_source.get("chapter_title") or chapter_title
        support_summaries: list[KGSupportSummary] = []
        for support_ref in sorted(support_refs_by_core.get(core_ref, set())):
            support = supports.get(support_ref)
            if not support:
                continue
            support_summaries.append(
                KGSupportSummary(
                    client_ref=support_ref,
                    type=str(support.get("type") or ""),
                    canonical_name=str(support.get("canonical_name") or ""),
                    aliases=list(support.get("aliases") or []),
                )
            )
        items.append(
            KGCoreCatalogItem(
                kg_core_uid=f"{document_id}/{uid_draft_file}/{core_ref}",
                draft_file=draft_file,
                document_id=document_id,
                version_id=version_id,
                book_title=book_title,
                chapter_title=core_chapter,
                section_title=section_title,
                client_ref=core_ref,
                type=str(core.get("type") or ""),
                canonical_name=str(core.get("canonical_name") or ""),
                aliases=list(core.get("aliases") or []),
                source_span_ids=list(core.get("source_span_ids") or []),
                supports=support_summaries,
                edges=edges_by_core.get(core_ref, []),
            )
        )
    return items


def load_kg_core_catalog(root: Path) -> list[KGCoreCatalogItem]:
    if not root.exists():
        return []
    catalog: list[KGCoreCatalogItem] = []
    for path in sorted(root.rglob("*.kg-draft.json")):
        catalog.extend(_load_draft(path, root))
    return catalog


def core_catalog_payload(items: list[KGCoreCatalogItem]) -> list[dict[str, Any]]:
    return [
        {
            "kg_core_uid": item.kg_core_uid,
            "client_ref": item.client_ref,
            "type": item.type,
            "canonical_name": item.canonical_name,
            "aliases": item.aliases,
            "book_title": item.book_title,
            "chapter_title": item.chapter_title,
            "section_title": item.section_title,
            "supports": [support.model_dump(mode="json") for support in item.supports[:40]],
            "edges": [edge.model_dump(mode="json") for edge in item.edges[:60]],
        }
        for item in items
    ]
