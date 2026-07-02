from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
from typing import Any

from .schemas import (
    ChapterCandidate,
    KGConceptNode,
    KGCoreNode,
    KGEdgeEvidence,
    KGEdgeNode,
    KGSupportNode,
)


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_str(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _node_name(ref: str, cores: dict[str, dict[str, Any]], supports: dict[str, dict[str, Any]]) -> str | None:
    node = cores.get(ref) or supports.get(ref)
    if not node:
        return None
    name = _as_str(node.get("canonical_name"))
    return name or None


def _edge_ref(draft_id: str, index: int, edge: dict[str, Any]) -> str:
    subject_ref = _as_str(edge.get("subject_ref")) or "subject"
    predicate = _as_str(edge.get("predicate")) or "predicate"
    object_ref = _as_str(edge.get("object_ref")) or "object"
    return f"{draft_id}#edge_{index:04d}_{subject_ref}_{predicate}_{object_ref}"


def _edge_evidence(edge: dict[str, Any]) -> list[KGEdgeEvidence]:
    evidence_rows: list[KGEdgeEvidence] = []
    for item in _as_list(edge.get("evidence"))[:3]:
        if not isinstance(item, dict):
            continue
        evidence_rows.append(
            KGEdgeEvidence(
                span_id=item.get("span_id"),
                exact_quote=item.get("exact_quote"),
                support_explanation=item.get("support_explanation"),
            )
        )
    return evidence_rows


def _load_draft(path: Path, root: Path) -> list[KGCoreNode]:
    data = json.loads(path.read_text(encoding="utf-8"))
    kg_subject_name = path.parent.name
    source = data.get("source_structure") or {}
    metadata = data.get("draft_metadata") or {}
    draft_id = _as_str(metadata.get("draft_id")) or path.stem.replace(".kg-draft", "")
    draft_file = path.relative_to(root).as_posix()
    book_title = _as_str(source.get("book_title")) or None
    chapter_title = _as_str(source.get("chapter_title")) or draft_id

    raw_cores = {node.get("client_ref"): node for node in _as_list(data.get("core_nodes")) if node.get("client_ref")}
    raw_supports = {node.get("client_ref"): node for node in _as_list(data.get("support_nodes")) if node.get("client_ref")}
    raw_concepts = {node.get("client_ref"): node for node in _as_list(data.get("concepts")) if node.get("client_ref")}

    support_refs_by_core: dict[str, set[str]] = defaultdict(set)
    concept_refs_by_core: dict[str, set[str]] = defaultdict(set)
    edges_by_core: dict[str, list[KGEdgeNode]] = defaultdict(list)

    for index, edge in enumerate(_as_list(data.get("edges")), start=1):
        if not isinstance(edge, dict):
            continue
        subject_ref = _as_str(edge.get("subject_ref"))
        object_ref = _as_str(edge.get("object_ref"))
        predicate = _as_str(edge.get("predicate"))
        if not subject_ref or not object_ref or not predicate:
            continue
        connected_core_refs = [ref for ref in (subject_ref, object_ref) if ref in raw_cores]
        for core_ref in connected_core_refs:
            other_ref = object_ref if core_ref == subject_ref else subject_ref
            if other_ref in raw_supports:
                support_refs_by_core[core_ref].add(other_ref)
            edge_node = KGEdgeNode(
                edge_ref=_edge_ref(draft_id, index, edge),
                subject_ref=subject_ref,
                predicate=predicate,
                object_ref=object_ref,
                subject_name=_node_name(subject_ref, raw_cores, raw_supports),
                object_name=_node_name(object_ref, raw_cores, raw_supports),
                evidence=_edge_evidence(edge),
            )
            edges_by_core[core_ref].append(edge_node)

    for concept_ref, concept in raw_concepts.items():
        related_refs = [_as_str(ref) for ref in _as_list(concept.get("related_node_refs"))]
        for related_ref in related_refs:
            if related_ref in raw_cores:
                concept_refs_by_core[related_ref].add(concept_ref)
            elif related_ref in raw_supports:
                for core_ref, support_refs in support_refs_by_core.items():
                    if related_ref in support_refs:
                        concept_refs_by_core[core_ref].add(concept_ref)

    items: list[KGCoreNode] = []
    for core_ref, core in raw_cores.items():
        core_source = core.get("source_structure") or {}
        core_chapter_title = _as_str(core_source.get("chapter_title")) or chapter_title
        section_title = _as_str(core_source.get("section_title")) or _as_str(source.get("section_title")) or None
        support_nodes: list[KGSupportNode] = []
        for support_ref in sorted(support_refs_by_core.get(core_ref, set())):
            support = raw_supports.get(support_ref) or {}
            support_nodes.append(
                KGSupportNode(
                    client_ref=support_ref,
                    type=_as_str(support.get("type")) or "Unknown",
                    canonical_name=_as_str(support.get("canonical_name")) or support_ref,
                    aliases=[_as_str(alias) for alias in _as_list(support.get("aliases")) if _as_str(alias)],
                )
            )
        concept_nodes: list[KGConceptNode] = []
        for concept_ref in sorted(concept_refs_by_core.get(core_ref, set())):
            concept = raw_concepts.get(concept_ref) or {}
            concept_nodes.append(
                KGConceptNode(
                    client_ref=concept_ref,
                    canonical_name=_as_str(concept.get("canonical_name")) or concept_ref,
                    relation=_as_str(concept.get("relation")) or None,
                    related_node_refs=[_as_str(ref) for ref in _as_list(concept.get("related_node_refs")) if _as_str(ref)],
                    reason_not_edge=_as_str(concept.get("reason_not_edge")) or None,
                )
            )
        items.append(
            KGCoreNode(
                kg_core_uid=f"kg://{kg_subject_name}/{draft_id}/{core_ref}",
                kg_subject_name=kg_subject_name,
                draft_id=draft_id,
                draft_file=draft_file,
                book_title=book_title,
                chapter_title=core_chapter_title,
                section_title=section_title,
                client_ref=core_ref,
                type=_as_str(core.get("type")) or "Unknown",
                canonical_name=_as_str(core.get("canonical_name")) or core_ref,
                aliases=[_as_str(alias) for alias in _as_list(core.get("aliases")) if _as_str(alias)],
                source_span_ids=[_as_str(span) for span in _as_list(core.get("source_span_ids")) if _as_str(span)],
                support_nodes=support_nodes,
                concept_nodes=concept_nodes,
                edges=edges_by_core.get(core_ref, [])[:80],
            )
        )
    return items


def load_kg_catalog(root: Path) -> list[KGCoreNode]:
    if not root.exists():
        return []
    items: list[KGCoreNode] = []
    for path in sorted(root.rglob("*.kg-draft.json")):
        items.extend(_load_draft(path, root))
    return items


def chapter_candidates_for_subject(catalog: list[KGCoreNode], subject_name: str) -> list[ChapterCandidate]:
    grouped: dict[tuple[str, str], list[KGCoreNode]] = defaultdict(list)
    for core in catalog:
        if core.kg_subject_name != subject_name:
            continue
        grouped[(core.draft_id, core.draft_file)].append(core)

    candidates: list[ChapterCandidate] = []
    for (draft_id, draft_file), cores in sorted(grouped.items(), key=lambda item: item[0][0]):
        chapter_title = cores[0].chapter_title
        candidates.append(
            ChapterCandidate(
                kg_subject_name=subject_name,
                kg_chapter_title=chapter_title,
                draft_id=draft_id,
                draft_file=draft_file,
                core_count=len(cores),
            )
        )
    return candidates


def compact_core_payload(cores: list[KGCoreNode]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for core in cores:
        support_payloads: list[dict[str, Any]] = []
        for support in core.support_nodes[:30]:
            support_payload = support.model_dump(mode="json")
            parent_edges = []
            for edge in core.edges:
                connected_to_core = core.client_ref in {edge.subject_ref, edge.object_ref}
                connected_to_support = support.client_ref in {edge.subject_ref, edge.object_ref}
                if not (connected_to_core and connected_to_support):
                    continue
                if edge.subject_ref == support.client_ref and edge.object_ref == core.client_ref:
                    direction = "support_to_core"
                elif edge.subject_ref == core.client_ref and edge.object_ref == support.client_ref:
                    direction = "core_to_support"
                else:
                    direction = "indirect_or_unknown"
                direction_warning = None
                if edge.predicate == "IS_SUBTYPE_OF" and direction == "core_to_support":
                    direction_warning = (
                        "edge_direction_suspect: IS_SUBTYPE_OF is usually support/subtype -> core/supertype"
                    )
                parent_edges.append(
                    {
                        "edge_ref": edge.edge_ref,
                        "subject_ref": edge.subject_ref,
                        "subject_name": edge.subject_name,
                        "predicate": edge.predicate,
                        "object_ref": edge.object_ref,
                        "object_name": edge.object_name,
                        "direction": direction,
                        "direction_warning": direction_warning,
                        "evidence": [
                            evidence.model_dump(mode="json")
                            for evidence in edge.evidence[:2]
                        ],
                    }
                )
            support_payload["parent_edges"] = parent_edges[:5]
            support_payloads.append(support_payload)
        payload.append(
            {
                "kg_core_uid": core.kg_core_uid,
                "client_ref": core.client_ref,
                "type": core.type,
                "canonical_name": core.canonical_name,
                "aliases": core.aliases[:8],
                "kg_subject_name": core.kg_subject_name,
                "chapter_title": core.chapter_title,
                "section_title": core.section_title,
                "support_nodes": support_payloads,
                "concept_nodes": [
                    concept.model_dump(mode="json")
                    for concept in core.concept_nodes[:20]
                ],
                "edges": [
                    {
                        "edge_ref": edge.edge_ref,
                        "subject_ref": edge.subject_ref,
                        "subject_name": edge.subject_name,
                        "predicate": edge.predicate,
                        "object_ref": edge.object_ref,
                        "object_name": edge.object_name,
                        "evidence": [
                            evidence.model_dump(mode="json")
                            for evidence in edge.evidence[:2]
                        ],
                    }
                    for edge in core.edges[:30]
                ],
            }
        )
    return payload
