"""Lightweight lesson planner — maps a user topic to a learning outline.

DEPRECATED MAIN-PATH WARNING:
The dimension-based planning flow in this module is no longer considered the
MVP Planner main path.  It may remain only as fallback/debug scaffolding until
the LLM Agent Planner is implemented.  New development must not treat the fixed
dimension order or title/objective-only LLM refinement as real Agent planning.

Strategy priority (auto-selected):
1. **retrieval_enhanced** — calls the existing KAG retrieval service, infers
   learning dimensions from evidence chunks, and builds a dimension-based
   course path.  Deprecated as the main path; fallback/debug only.
2. **kag_node_match** — falls back to direct ``kg_nodes.jsonl`` scoring when
   retrieval returns no results.
3. **fallback_outline** — generic outline when nothing matches.

Optional LLM refinement layer: if an API key is configured, the draft outline
titles and learning objectives can be refined by an LLM.  Structural fields
(node ids, kg_node_ids, source_refs, prerequisites, etc.) are never touched.
This is title/objective refinement only, not Agent Planner output.
"""

from __future__ import annotations

import copy
import json
import re
import traceback
from pathlib import Path
from typing import Any

from .kag_paths import resolve_kag_dir
from .schemas import LessonOutline, OutlineNode


# ---------------------------------------------------------------------------
# Data loaders (unchanged from previous version)
# ---------------------------------------------------------------------------


def load_kg_nodes(limit: int | None = None) -> list[dict[str, Any]]:
    """Read KAG nodes from ``kg_nodes.jsonl``, core-first."""
    kag = resolve_kag_dir()
    cores: list[dict[str, Any]] = []
    others: list[dict[str, Any]] = []

    with open(kag / "kg_nodes.jsonl", "r", encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            if limit is not None and i >= limit:
                break
            try:
                node = json.loads(line)
            except json.JSONDecodeError:
                continue
            if node.get("node_kind") == "core":
                cores.append(node)
            else:
                others.append(node)

    return cores + others


def load_kg_edges_for_nodes(
    node_ids: set[str], limit: int = 200
) -> list[dict[str, Any]]:
    """Scan ``kg_edges.jsonl`` for edges incident to any of *node_ids*."""
    if not node_ids:
        return []
    kag = resolve_kag_dir()
    edges: list[dict[str, Any]] = []
    with open(kag / "kg_edges.jsonl", "r", encoding="utf-8") as fh:
        for line in fh:
            try:
                edge = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                edge.get("source_node_id") in node_ids
                or edge.get("target_node_id") in node_ids
            ):
                edges.append(edge)
                if len(edges) >= limit:
                    break
    return edges


def load_questions_for_nodes(
    node_ids: set[str], limit: int = 20
) -> list[dict[str, Any]]:
    """Scan ``questions.jsonl`` for questions whose ``primary_node_id`` is in
    *node_ids*."""
    if not node_ids:
        return []
    kag = resolve_kag_dir()
    questions: list[dict[str, Any]] = []
    with open(kag / "questions.jsonl", "r", encoding="utf-8") as fh:
        for line in fh:
            try:
                q = json.loads(line)
            except json.JSONDecodeError:
                continue
            if q.get("primary_node_id") in node_ids:
                questions.append(q)
                if len(questions) >= limit:
                    break
    return questions


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def score_node_for_topic(node: dict[str, Any], topic: str) -> float:
    """Score a KAG node against *topic* using simple keyword rules."""
    score = 0.0
    topic_lower = topic.lower().strip()
    if not topic_lower:
        return 0.0

    canonical = (node.get("canonical_name") or "").lower()
    aliases: list[str] = [a.lower() for a in (node.get("aliases") or [])]
    subject = (node.get("subject") or "").lower()
    book_title = (node.get("book_title") or "").lower()
    chapter = (node.get("chapter") or "").lower()

    # --- exact / containment matches --------------------------------
    if topic_lower in canonical or canonical in topic_lower:
        score += 10.0

    for alias in aliases:
        if topic_lower in alias or alias in topic_lower:
            score += 8.0
            break

    # --- keyword-level: extract consecutive Chinese characters from
    #     the topic and check canonical_name -------------------------
    _chinese_keywords = _chinese_substrings(topic_lower, min_len=2)
    for kw in _chinese_keywords:
        if kw in canonical:
            score += 4.0

    # --- context fields ---------------------------------------------
    for field_text in (subject, book_title, chapter):
        if topic_lower in field_text:
            score += 2.0

    # --- quality signals --------------------------------------------
    if node.get("node_kind") == "core":
        score += 1.0

    auth = node.get("authority_level", 0)
    if isinstance(auth, (int, float)):
        score += float(auth) / 1000.0

    return score


def _chinese_substrings(text: str, min_len: int = 2) -> list[str]:
    """Extract all consecutive Chinese-character substrings."""
    cleaned = "".join(ch for ch in text if "一" <= ch <= "鿿")
    subs: list[str] = []
    n = len(cleaned)
    for length in range(n, min_len - 1, -1):
        for start in range(n - length + 1):
            subs.append(cleaned[start : start + length])
    return subs


# ---------------------------------------------------------------------------
# Node discovery
# ---------------------------------------------------------------------------


def find_relevant_nodes(
    topic: str, max_nodes: int = 6
) -> list[dict[str, Any]]:
    """Load all KAG nodes, score by *topic*, and return the top matches.

    Each returned node dict will have a ``_score`` key attached.
    """
    nodes = load_kg_nodes()
    scored = [(score_node_for_topic(n, topic), n) for n in nodes]
    scored.sort(key=lambda x: x[0], reverse=True)
    result: list[dict[str, Any]] = []
    for s, n in scored:
        if s <= 0:
            break
        n["_score"] = s
        result.append(n)
        if len(result) >= max_nodes:
            break
    return result


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


def dedupe_nodes(
    nodes: list[dict[str, Any]],
    topic: str,
    max_nodes: int = 6,
) -> list[dict[str, Any]]:
    """Remove duplicate canonical-name nodes, keep the best per name.

    Rules:
    - Keep only the first occurrence of each ``canonical_name``.
    - Prefer nodes whose ``subject`` / ``book_title`` match the topic domain.
    - Cap to *max_nodes*.
    """
    topic_lower = topic.lower().strip()
    seen_names: set[str] = set()
    result: list[dict[str, Any]] = []

    # Sort: nodes with topic-relevant subject first, then by score
    def _sort_key(n: dict) -> tuple[int, float]:
        subj = (n.get("subject") or "").lower()
        book = (n.get("book_title") or "").lower()
        domain_bonus = 0
        if topic_lower and (topic_lower in subj or topic_lower in book):
            domain_bonus = -1  # sort before (negated)
        return (domain_bonus, -(n.get("_score", 0.0)))

    for n in sorted(nodes, key=_sort_key):
        name = (n.get("canonical_name") or "").strip()
        if not name:
            continue
        if name in seen_names:
            continue
        seen_names.add(name)
        result.append(n)
        if len(result) >= max_nodes:
            break

    return result


# ---------------------------------------------------------------------------
# Retrieval integration
# ---------------------------------------------------------------------------


def collect_planning_context(
    topic: str, top_k: int = 12
) -> dict[str, Any]:
    """Call the existing KAG retrieval service and return lightweight dicts.

    Gracefully degrades on any error.
    """
    chunks: list[dict[str, Any]] = []
    warnings: list[str] = []
    ok = False

    try:
        from backend.services.retrieval import (  # type: ignore[import-not-found]
            search_knowledge_base,
        )

        raw = search_knowledge_base(topic, top_k=top_k)
        ok = True
        for ch in raw:
            text = ch.text or ""
            # compact whitespace
            excerpt = " ".join(text.split())[:240]
            chunks.append({
                "id": ch.id,
                "title": ch.title,
                "text_excerpt": excerpt,
                "source": ch.source,
                "source_type": ch.source_type,
                "path": ch.path,
                "score": round(float(ch.score), 4),
                "metadata": ch.metadata or {},
            })
    except Exception:
        warnings.append(f"search_knowledge_base failed: {traceback.format_exc()}")

    return {
        "ok": ok,
        "chunks": chunks,
        "warnings": warnings,
        "source": "search_knowledge_base",
    }


# ---------------------------------------------------------------------------
# Learning dimensions
# DEPRECATED FOR MAIN PATH:
# This fixed medical-teaching dimension list is allowed only for
# fallback_rule_based_planner/debug/smoke coverage.  The future Planner main path
# must plan ordered outline units from topic/user state/evidence_pool, with
# sequence_reason and selected KG nodes/edges/concepts/questions/chunks.
# ---------------------------------------------------------------------------


_DIMENSION_CONFIG: list[dict[str, Any]] = [
    {
        "id": "definition",
        "title": "定义与疾病本质",
        "content_type_hints": ["definition", "concept", "overview", "定义"],
        "keyword_hints": ["定义", "本质", "概念", "概述", "基本"],
    },
    {
        "id": "etiology",
        "title": "病因与危险因素",
        "content_type_hints": ["etiology", "risk_factor", "病因", "危险因素"],
        "keyword_hints": ["病因", "危险因素", "诱因", "风险因素"],
    },
    {
        "id": "pathogenesis",
        "title": "发病机制与组织破坏",
        "content_type_hints": ["pathogenesis", "mechanism", "发病机制"],
        "keyword_hints": ["机制", "为什么", "炎症", "破坏", "吸收", "进展", "途径"],
    },
    {
        "id": "clinical_feature",
        "title": "临床表现与检查要点",
        "content_type_hints": ["clinical_feature", "symptom", "diagnosis", "临床表现", "检查"],
        "keyword_hints": ["表现", "症状", "检查", "探诊", "影像", "体征", "X线"],
    },
    {
        "id": "diagnosis",
        "title": "诊断与鉴别诊断",
        "content_type_hints": ["diagnosis", "differential", "诊断", "鉴别"],
        "keyword_hints": ["诊断", "鉴别", "区别", "区分", "标准"],
    },
    {
        "id": "treatment",
        "title": "治疗原则与维护",
        "content_type_hints": ["treatment", "management", "therapy", "治疗", "药物"],
        "keyword_hints": ["治疗", "维护", "处理", "原则", "方案", "用药", "手术", "预防"],
    },
]


def _content_types_from_chunk(chunk: dict[str, Any]) -> list[str]:
    """Extract content-type values from a chunk's metadata (defensive)."""
    meta = chunk.get("metadata", {}) or {}
    ct = meta.get("content_type") or meta.get("content_types")
    if isinstance(ct, str):
        return [ct]
    if isinstance(ct, list):
        return [str(v) for v in ct]
    return []


def infer_learning_dimensions(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group evidence chunks into learning dimensions.

    Deprecated for Planner main path.  This is a fallback/debug grouping helper,
    not Agent planning.

    Each dimension gets at most 3 chunks.  Only dimensions with at least one
    chunk are returned, in the fixed order (definition → treatment).
    """
    if not chunks:
        return []

    dims: dict[str, dict[str, Any]] = {}
    for dc in _DIMENSION_CONFIG:
        dims[dc["id"]] = {**dc, "chunks": []}

    for chunk in chunks:
        cts = _content_types_from_chunk(chunk)
        matched = False
        for dc in _DIMENSION_CONFIG:
            dim_id = dc["id"]
            if len(dims[dim_id]["chunks"]) >= 3:
                continue
            # Try content_type match first
            for ct_val in cts:
                ct_lower = ct_val.lower()
                for hint in dc["content_type_hints"]:
                    if hint.lower() in ct_lower:
                        dims[dim_id]["chunks"].append(chunk)
                        matched = True
                        break
                if matched:
                    break
            # If no content_type match, try keyword match in title/excerpt
            if not matched:
                text = (chunk.get("title", "") + " " + chunk.get("text_excerpt", "")).lower()
                for kw in dc["keyword_hints"]:
                    if kw in text:
                        dims[dim_id]["chunks"].append(chunk)
                        matched = True
                        break
            if matched:
                break

    # Return only dimensions that have chunks, preserving order
    result: list[dict[str, Any]] = []
    for dc in _DIMENSION_CONFIG:
        entry = dims[dc["id"]]
        if entry["chunks"]:
            result.append(entry)
    return result


# ---------------------------------------------------------------------------
# Dimension-based outline builder
# DEPRECATED FOR MAIN PATH:
# Builds an outline from fixed dimensions and fixed order.  Do not feed this as
# the required skeleton for the future LLM Agent Planner; use it only when
# explicitly returning fallback_rule_based_planner or for smoke/debug traces.
# ---------------------------------------------------------------------------


def _extract_core_node_ids(chunks: list[dict], max_ids: int = 5) -> list[str]:
    """Extract and deduplicate core_node_ids from chunk metadata."""
    ids: list[str] = []
    seen: set[str] = set()
    for ch in chunks:
        meta = ch.get("metadata", {}) or {}
        raw = meta.get("core_node_ids") or []
        if isinstance(raw, list):
            for rid in raw:
                sid = str(rid)
                if sid and sid not in seen:
                    seen.add(sid)
                    ids.append(sid)
                    if len(ids) >= max_ids:
                        return ids
    return ids


def build_outline_from_dimensions(
    topic: str,
    dimensions: list[dict[str, Any]],
    relevant_nodes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a LessonOutline-compatible dict from learning dimensions.

    Deprecated for Planner main path; fallback/debug only.
    """
    # Index relevant nodes by node_id for fallback kg_node_ids
    node_by_id: dict[str, dict] = {n.get("node_id", ""): n for n in relevant_nodes}

    outline_nodes: list[dict[str, Any]] = []
    for i, dim in enumerate(dimensions):
        kg_node_ids = _extract_core_node_ids(dim["chunks"], max_ids=5)
        # If no core_node_ids found, supplement from relevant_nodes
        if not kg_node_ids and relevant_nodes:
            start = i % len(relevant_nodes)
            kg_node_ids = [relevant_nodes[start].get("node_id", "")]
        if not kg_node_ids:
            kg_node_ids = []

        # Build source_refs from first 2 chunks
        source_refs: list[dict] = []
        for ch in dim["chunks"][:2]:
            source_refs.append({
                "chunk_id": ch.get("id", ""),
                "source": ch.get("source", ""),
                "source_type": ch.get("source_type", ""),
                "title": ch.get("title", ""),
                "path": ch.get("path", ""),
                "score": ch.get("score", 0.0),
            })

        onode: dict[str, Any] = {
            "id": f"node_{i + 1}",
            "title": dim["title"],
            "kg_node_ids": kg_node_ids,
            "learning_objectives": [f"掌握{topic}的{dim['title']}"],
            "prerequisites": [] if i == 0 else [f"node_{i}"],
            "estimated_minutes": 8,
            "difficulty": "basic" if i < 2 else "intermediate",
            "source_refs": source_refs,
        }
        # Validate
        OutlineNode(**onode)
        outline_nodes.append(onode)

    outline = {
        "title": f"{topic}系统学习路径",
        "nodes": outline_nodes,
    }
    LessonOutline(**outline)
    return outline


# ---------------------------------------------------------------------------
# Outline builders (existing)
# ---------------------------------------------------------------------------


def build_fallback_outline(
    topic: str, max_nodes: int = 5
) -> dict[str, Any]:
    """Return a generic lesson outline when nothing matches."""
    titles = _FALLBACK_NODE_TITLES[:max_nodes]
    nodes: list[dict[str, Any]] = []
    for i, title in enumerate(titles):
        nodes.append({
            "id": f"node_{i + 1}",
            "title": title,
            "kg_node_ids": [],
            "learning_objectives": [f"掌握{title}的核心知识点"],
            "prerequisites": [] if i == 0 else [f"node_{i}"],
            "estimated_minutes": 8,
            "difficulty": "basic" if i < 2 else "intermediate",
            "source_refs": [],
        })
    return {
        "title": f"{topic}系统学习路径",
        "nodes": nodes,
    }


_FALLBACK_NODE_TITLES = [
    "定义与疾病本质",
    "病因和发病机制",
    "临床表现与检查",
    "鉴别诊断",
    "治疗原则与复习总结",
]


def build_outline_from_nodes(
    topic: str,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    questions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a :class:`LessonOutline`-compatible dict from matched KAG nodes."""
    q_by_node: dict[str, list[dict]] = {}
    for q in questions:
        pid = q.get("primary_node_id", "")
        q_by_node.setdefault(pid, []).append(q)

    outline_nodes: list[dict[str, Any]] = []
    for i, node in enumerate(nodes):
        node_id = node.get("node_id", "")
        canonical = node.get("canonical_name", f"节点{i + 1}")
        subject = node.get("subject", "")
        book_title = node.get("book_title", "")
        chapter = node.get("chapter", "")
        section = node.get("section", "")
        chapter_title = ""
        section_title = ""
        if isinstance(node.get("source_structure"), dict):
            ss = node["source_structure"]
            chapter_title = ss.get("chapter_title", "")
            section_title = ss.get("section_title", "")

        learning_objectives = [
            f"理解{canonical}的核心概念及其在{subject}中的位置"
        ]

        qs = q_by_node.get(node_id, [])
        onode: dict[str, Any] = {
            "id": f"node_{i + 1}",
            "title": canonical,
            "kg_node_ids": [node_id],
            "learning_objectives": learning_objectives,
            "prerequisites": [] if i == 0 else [f"node_{i}"],
            "estimated_minutes": 8,
            "difficulty": "basic" if i < 2 else "intermediate",
            "source_refs": [
                {
                    "kg_node_id": node_id,
                    "subject": subject,
                    "book_title": book_title,
                    "chapter": chapter or chapter_title,
                    "section": section or section_title,
                }
            ],
        }
        if qs:
            onode["question_ids"] = [q.get("question_id") for q in qs]

        OutlineNode(**onode)
        outline_nodes.append(onode)

    outline = {
        "title": f"{topic}系统学习路径",
        "nodes": outline_nodes,
    }
    LessonOutline(**outline)
    return outline


# ---------------------------------------------------------------------------
# Mindmap
# ---------------------------------------------------------------------------


def build_mindmap(outline: dict[str, Any]) -> dict[str, Any]:
    """Derive a ``markmap_json``-format mindmap from an outline dict."""
    children = [
        {"name": node["title"], "id": node["id"]}
        for node in outline.get("nodes", [])
    ]
    return {
        "format": "markmap_json",
        "root": {
            "name": outline["title"],
            "children": children,
        },
    }


# ---------------------------------------------------------------------------
# Optional LLM outline refinement
# ---------------------------------------------------------------------------

_LLM_API_KEY_NAMES = (
    "LEARNING_PLANNER_LLM_API_KEY",
    "PLANNER_LLM_API_KEY",
    "LEARNING_LLM_API_KEY",
    "QWEN_API_KEY",
    "DASHSCOPE_API_KEY",
)
_LLM_BASE_URL_NAMES = (
    "LEARNING_PLANNER_LLM_BASE_URL",
    "PLANNER_LLM_BASE_URL",
    "LEARNING_LLM_BASE_URL",
    "QWEN_BASE_URL",
    "DASHSCOPE_BASE_URL",
)
_LLM_MODEL_NAMES = (
    "LEARNING_PLANNER_LLM_MODEL",
    "PLANNER_LLM_MODEL",
    "LEARNING_LLM_MODEL",
    "QWEN_MODEL",
    "DASHSCOPE_MODEL",
)


def _try_salvage_json(raw: str) -> dict[str, Any] | None:
    """Try to salvage truncated JSON by auto-closing brackets/braces.

    Returns the parsed dict, or None if salvage fails.
    """
    text = raw.strip()
    # Count open/close braces and brackets
    open_braces = text.count("{") - text.count("}")
    open_brackets = text.count("[") - text.count("]")

    # Check if we're inside a string (odd number of unescaped quotes)
    suffix = ""
    in_string = False
    i = 0
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == '"':
            in_string = not in_string
        i += 1
    if in_string:
        suffix += '"'

    if open_braces > 0 or open_brackets > 0 or in_string:
        suffix += "]" * open_brackets
        suffix += "}" * open_braces
        if suffix:
            text += suffix
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return None
    return None


def refine_outline_with_llm(
    topic: str,
    draft_outline: dict[str, Any],
    planning_context: dict[str, Any],
    matched_nodes: list[dict[str, Any]],
    user_profile: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Optionally refine outline titles and learning objectives via LLM.

    Deprecated as a Planner main path.  This function preserves a pre-existing
    fixed outline and asks the LLM to edit titles/objectives only; it must not be
    described as Agent Planner sequence generation.

    Parameters
    ----------
    topic:
        The lesson topic.
    draft_outline:
        The draft outline dict (from ``build_outline_from_dimensions`` etc.).
    planning_context:
        Result from :func:`collect_planning_context`.
    matched_nodes:
        Relevant KG nodes.
    user_profile:
        Optional user profile dict.

    Returns
    -------
    (outline, trace)
        ``outline`` is the refined outline (or *draft_outline* on fallback).
        ``trace`` carries refinement status metadata.
    """
    # --- draft snapshot (lightweight) --------------------------------------
    draft_nodes = draft_outline.get("nodes", [])
    draft_snapshot = [
        {
            "id": n["id"],
            "title": n.get("title", ""),
            "learning_objectives": list(n.get("learning_objectives", [])),
        }
        for n in draft_nodes
    ]

    trace: dict[str, Any] = {
        "outline_refinement": "skipped_no_llm",
        "outline_refinement_model": None,
        "outline_refinement_error": None,
        "outline_refinement_change_count": 0,
        "outline_refinement_changes": [],
        "draft_outline_nodes": draft_snapshot,
        "refined_outline_nodes": draft_snapshot,
        # -- diagnostic fields (populated on LLM path) --
        "outline_refinement_prompt_chars": 0,
        "outline_refinement_system_prompt_chars": 0,
        "outline_refinement_user_prompt_chars": 0,
        "outline_refinement_raw_response_chars": 0,
        "outline_refinement_raw_response_head": None,
        "outline_refinement_raw_response_tail": None,
        "outline_refinement_json_error": None,
        "outline_refinement_json_error_pos": None,
        "outline_refinement_json_error_lineno": None,
        "outline_refinement_json_error_colno": None,
        "outline_refinement_salvage_attempted": False,
        "outline_refinement_salvage_succeeded": False,
    }

    # --- 1. check LLM availability ----------------------------------------
    try:
        from backend.services.llm_client import chat_completion, is_llm_configured
    except Exception:
        return draft_outline, trace

    if not is_llm_configured(*_LLM_API_KEY_NAMES):
        return draft_outline, trace

    # --- 2. build prompts -------------------------------------------------
    draft_nodes = draft_outline.get("nodes", [])
    draft_for_llm = [
        {
            "id": n["id"],
            "title": n.get("title", ""),
            "learning_objectives": n.get("learning_objectives", []),
            "source_refs": n.get("source_refs", []),
        }
        for n in draft_nodes
    ]

    # light evidence from planning_context chunks
    light_chunks = []
    for ch in planning_context.get("chunks", [])[:16]:
        light_chunks.append({
            "id": ch.get("id", ""),
            "title": ch.get("title", ""),
            "text_excerpt": ch.get("text_excerpt", ""),
            "source_type": ch.get("source_type", ""),
            "score": ch.get("score", 0.0),
        })

    # light nodes
    light_nodes = []
    for n in matched_nodes[:10]:
        light_nodes.append({
            "node_id": n.get("node_id", ""),
            "canonical_name": n.get("canonical_name", ""),
            "aliases": n.get("aliases", []),
            "subject": n.get("subject", ""),
            "chapter": n.get("chapter", ""),
            "node_type": n.get("node_type", ""),
        })

    # Load the prompt template
    _prompt_dir = Path(__file__).resolve().parent / "prompts"
    prompt_path = _prompt_dir / "planner_refine.md"
    if prompt_path.exists():
        system_prompt = prompt_path.read_text(encoding="utf-8")
    else:
        system_prompt = (
            "你是一名口腔医学学习路径规划助手。"
            "只精修节点标题和学习目标，不改变结构字段，不编造证据。"
            "输出严格 JSON。"
        )

    user_prompt = json.dumps({
        "topic": topic,
        "user_profile": user_profile or {},
        "draft_nodes": draft_for_llm,
        "evidence_chunks": light_chunks,
        "related_kg_nodes": light_nodes,
    }, ensure_ascii=False, indent=2)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    trace["outline_refinement_system_prompt_chars"] = len(system_prompt)
    trace["outline_refinement_user_prompt_chars"] = len(user_prompt)
    trace["outline_refinement_prompt_chars"] = len(system_prompt) + len(user_prompt)

    # --- 3. call LLM ------------------------------------------------------
    try:
        raw = chat_completion(
            messages,
            temperature=0.2,
            max_tokens=3200,
            api_key_names=_LLM_API_KEY_NAMES,
            base_url_names=_LLM_BASE_URL_NAMES,
            model_names=_LLM_MODEL_NAMES,
        )
        trace["outline_refinement_model"] = None  # model is internal
    except Exception as exc:
        trace["outline_refinement"] = "failed_exception"
        trace["outline_refinement_error"] = str(exc)[:500]
        return draft_outline, trace

    # Record raw response diagnostics
    trace["outline_refinement_raw_response_chars"] = len(raw)
    trace["outline_refinement_raw_response_head"] = raw[:800]
    trace["outline_refinement_raw_response_tail"] = raw[-800:]

    # --- 4. parse JSON ----------------------------------------------------
    # Strip markdown code fences (including unclosed ones)
    raw = raw.strip()
    # Try closed fence first
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", raw, re.DOTALL)
    if not fence_match:
        # Try unclosed fence: ```json\n{...}  (no closing ```)
        fence_match = re.search(r"```(?:json)?\s*\n?(.*)$", raw, re.DOTALL)
    if fence_match:
        raw = fence_match.group(1).strip()

    # Try to salvage truncated JSON by adding missing closing brackets
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        # Record JSON error diagnostics
        trace["outline_refinement_json_error"] = str(exc)[:500]
        trace["outline_refinement_json_error_pos"] = exc.pos
        trace["outline_refinement_json_error_lineno"] = exc.lineno
        trace["outline_refinement_json_error_colno"] = exc.colno

        # Attempt salvage
        trace["outline_refinement_salvage_attempted"] = True
        salvaged = _try_salvage_json(raw)
        if salvaged is not None:
            trace["outline_refinement_salvage_succeeded"] = True
            parsed = salvaged
        else:
            trace["outline_refinement_salvage_succeeded"] = False
            trace["outline_refinement"] = "failed_invalid_json"
            trace["outline_refinement_error"] = str(exc)[:500]
            return draft_outline, trace

    refined_nodes = parsed.get("nodes") if isinstance(parsed, dict) else None
    if not isinstance(refined_nodes, list):
        trace["outline_refinement"] = "failed_invalid_json"
        trace["outline_refinement_error"] = "output has no 'nodes' array"
        return draft_outline, trace

    # --- 5. validate ------------------------------------------------------
    if len(refined_nodes) != len(draft_nodes):
        trace["outline_refinement"] = "failed_schema_mismatch"
        trace["outline_refinement_error"] = (
            f"node count mismatch: got {len(refined_nodes)}, "
            f"expected {len(draft_nodes)}"
        )
        return draft_outline, trace

    for i, (rn, dn) in enumerate(zip(refined_nodes, draft_nodes)):
        if rn.get("id") != dn["id"]:
            trace["outline_refinement"] = "failed_schema_mismatch"
            trace["outline_refinement_error"] = (
                f"node id mismatch at position {i}: "
                f"got {rn.get('id')!r}, expected {dn['id']!r}"
            )
            return draft_outline, trace
        if not isinstance(rn.get("title"), str) or not rn["title"].strip():
            trace["outline_refinement"] = "failed_schema_mismatch"
            trace["outline_refinement_error"] = (
                f"empty or missing title at node {dn['id']}"
            )
            return draft_outline, trace
        if not isinstance(rn.get("learning_objectives"), list) or not rn["learning_objectives"]:
            trace["outline_refinement"] = "failed_schema_mismatch"
            trace["outline_refinement_error"] = (
                f"empty or missing learning_objectives at node {dn['id']}"
            )
            return draft_outline, trace

    # --- 6. merge: deep copy draft, only replace title & objectives -------
    refined_outline = copy.deepcopy(draft_outline)
    changes: list[dict[str, Any]] = []
    refined_snapshot: list[dict[str, Any]] = []

    for i, rn in enumerate(refined_nodes):
        dn = draft_nodes[i]
        new_title = rn["title"].strip()
        new_objectives = [str(o).strip() for o in rn["learning_objectives"]]

        refined_outline["nodes"][i]["title"] = new_title
        refined_outline["nodes"][i]["learning_objectives"] = new_objectives

        # Build refined snapshot
        refined_snapshot.append({
            "id": dn["id"],
            "title": new_title,
            "learning_objectives": list(new_objectives),
        })

        # Build change record
        change: dict[str, Any] = {
            "id": dn["id"],
            "before_title": dn.get("title", ""),
            "after_title": new_title,
            "before_objectives": list(dn.get("learning_objectives", [])),
            "after_objectives": list(new_objectives),
            "refinement_reason": rn.get("refinement_reason", ""),
            "evidence_chunk_ids": rn.get("evidence_chunk_ids", []),
        }
        changes.append(change)

    trace["outline_refinement_changes"] = changes
    trace["outline_refinement_change_count"] = len(changes)
    trace["refined_outline_nodes"] = refined_snapshot

    # --- 7. final schema validation ---------------------------------------
    try:
        LessonOutline(**refined_outline)
    except Exception as exc:
        trace["outline_refinement"] = "failed_schema_mismatch"
        trace["outline_refinement_error"] = f"LessonOutline validation failed: {exc}"
        return draft_outline, trace

    trace["outline_refinement"] = "llm_refined"
    return refined_outline, trace


# ---------------------------------------------------------------------------
# Evidence pool
# ---------------------------------------------------------------------------


def build_planning_evidence_pool(
    topic: str,
    planning_context: dict[str, Any],
    relevant_nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    questions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a lightweight evidence pool from retrieval results.

    Returns a dict with kg_nodes, kg_edges, concepts, evidence_chunks,
    questions, and counts — all as lightweight summaries suitable for
    downstream consumers and outline_evidence_map binding.
    """

    # --- 1. kg_nodes (lightweight summaries) -------------------------------
    kg_nodes: list[dict[str, Any]] = []
    for n in relevant_nodes:
        kg_nodes.append({
            "node_id": n.get("node_id", ""),
            "canonical_name": n.get("canonical_name", ""),
            "aliases": (n.get("aliases") or [])[:5],
            "node_type": n.get("node_type", ""),
            "node_kind": n.get("node_kind", ""),
            "subject": n.get("subject", ""),
            "chapter": n.get("chapter", ""),
            "section": n.get("section", ""),
            "score": n.get("_score", 0.0),
        })

    # --- 2. kg_edges (lightweight summaries) -------------------------------
    kg_edges: list[dict[str, Any]] = []
    for i, edge in enumerate(edges):
        relation = (
            edge.get("relation")
            or edge.get("predicate")
            or edge.get("edge_type")
            or edge.get("type")
            or ""
        )
        kg_edges.append({
            "edge_id": f"edge_{i}",
            "source_node_id": edge.get("source_node_id", ""),
            "target_node_id": edge.get("target_node_id", ""),
            "relation": relation,
            "edge_type": edge.get("edge_type", ""),
            "weight": edge.get("weight"),
            "confidence": edge.get("confidence"),
        })

    # --- 3. concepts (derived from KG nodes) -------------------------------
    concepts: list[dict[str, Any]] = []
    for n in relevant_nodes:
        canonical_name = n.get("canonical_name", "")
        if not canonical_name:
            continue
        concepts.append({
            "name": canonical_name,
            "node_id": n.get("node_id", ""),
            "aliases": (n.get("aliases") or [])[:5],
            "node_type": n.get("node_type", ""),
        })

    # --- 4. evidence_chunks (lightweight text excerpts) --------------------
    evidence_chunks: list[dict[str, Any]] = []
    for ch in planning_context.get("chunks", []):
        text_excerpt = ch.get("text_excerpt", "") or ""
        evidence_chunks.append({
            "chunk_id": ch.get("id", ""),
            "title": ch.get("title", ""),
            "source": ch.get("source", ""),
            "source_type": ch.get("source_type", ""),
            "path": ch.get("path", ""),
            "score": ch.get("score", 0.0),
            "text_excerpt": text_excerpt[:500],
            "metadata": {
                "core_node_ids": (ch.get("metadata", {}) or {}).get("core_node_ids", []),
                "content_type": (ch.get("metadata", {}) or {}).get("content_type", ""),
            },
        })

    # --- 5. questions (lightweight summaries) ------------------------------
    light_questions: list[dict[str, Any]] = []
    for q in questions:
        light_questions.append({
            "question_id": q.get("question_id") or q.get("id") or "",
            "type": q.get("type", ""),
            "stem": (q.get("stem") or "")[:300],
            "options": q.get("options", []),
            "answer": q.get("answer", ""),
            "primary_node_id": q.get("primary_node_id", ""),
        })

    # --- 6. counts ---------------------------------------------------------
    counts = {
        "kg_nodes": len(kg_nodes),
        "kg_edges": len(kg_edges),
        "concepts": len(concepts),
        "evidence_chunks": len(evidence_chunks),
        "questions": len(light_questions),
    }

    return {
        "topic": topic,
        "kg_nodes": kg_nodes,
        "kg_edges": kg_edges,
        "concepts": concepts,
        "evidence_chunks": evidence_chunks,
        "questions": light_questions,
        "counts": counts,
    }


# ---------------------------------------------------------------------------
# Outline evidence map
# ---------------------------------------------------------------------------


def build_outline_evidence_map(
    outline: dict[str, Any],
    evidence_pool: dict[str, Any],
) -> dict[str, Any]:
    """Bind evidence pool entries to each outline node.

    For every node in *outline*, looks up matching KG nodes, edges,
    concepts, questions, evidence chunks, and source refs from the
    *evidence_pool*, producing a per-node evidence bundle.
    """

    pool_kg_nodes = evidence_pool.get("kg_nodes", [])
    pool_kg_edges = evidence_pool.get("kg_edges", [])
    pool_concepts = evidence_pool.get("concepts", [])
    pool_questions = evidence_pool.get("questions", [])
    pool_evidence_chunks = evidence_pool.get("evidence_chunks", [])

    # Index helpers
    pool_node_ids: set[str] = {n["node_id"] for n in pool_kg_nodes if n["node_id"]}
    pool_concept_by_node: dict[str, dict] = {
        c["node_id"]: c for c in pool_concepts if c["node_id"]
    }
    pool_question_by_id: dict[str, dict] = {
        q["question_id"]: q for q in pool_questions if q["question_id"]
    }
    pool_chunk_by_id: dict[str, dict] = {
        ch["chunk_id"]: ch for ch in pool_evidence_chunks if ch["chunk_id"]
    }

    outline_nodes = outline.get("nodes", [])
    evidence_map: dict[str, Any] = {}

    for node in outline_nodes:
        node_id = node.get("id", "")
        node_kg_ids: set[str] = set(node.get("kg_node_ids", []))
        source_refs: list[dict] = node.get("source_refs", [])
        source_chunk_ids: set[str] = {
            ref["chunk_id"] for ref in source_refs if ref.get("chunk_id")
        }
        node_question_ids: set[str] = set(node.get("question_ids", []))

        # --- 1. kg_nodes ---------------------------------------------------
        matched_kg_nodes: list[dict] = [
            n for n in pool_kg_nodes if n["node_id"] in node_kg_ids
        ]

        # --- 2. kg_edges ---------------------------------------------------
        matched_kg_edges: list[dict] = [
            e
            for e in pool_kg_edges
            if e["source_node_id"] in node_kg_ids
            or e["target_node_id"] in node_kg_ids
        ]

        # --- 3. concepts ---------------------------------------------------
        matched_concepts: list[dict] = [
            pool_concept_by_node[nid]
            for nid in node_kg_ids
            if nid in pool_concept_by_node
        ]

        # --- 4. questions --------------------------------------------------
        matched_questions: list[dict] = []
        resolved_question_ids: list[str] = []

        if node_question_ids:
            # Match by explicit question_ids on the node
            for qid in node_question_ids:
                q = pool_question_by_id.get(qid)
                if q:
                    matched_questions.append(q)
                    resolved_question_ids.append(qid)
        else:
            # Fallback: match questions whose primary_node_id is in node_kg_ids
            for q in pool_questions:
                pid = q.get("primary_node_id", "")
                if pid and pid in node_kg_ids:
                    matched_questions.append(q)
                    resolved_question_ids.append(q["question_id"])

        # --- 5. evidence_chunks --------------------------------------------
        matched_chunks: list[dict] = []

        # Priority 1: match by source_chunk_ids
        for cid in source_chunk_ids:
            ch = pool_chunk_by_id.get(cid)
            if ch:
                matched_chunks.append(ch)

        # Priority 2: match by core_node_ids intersection with node_kg_ids
        if not matched_chunks:
            for ch in pool_evidence_chunks:
                core_ids = set(ch.get("metadata", {}).get("core_node_ids", []))
                if core_ids & node_kg_ids:
                    matched_chunks.append(ch)

        # Priority 3: fallback — use source_refs as light info
        if not matched_chunks:
            for ref in source_refs:
                matched_chunks.append({
                    "chunk_id": ref.get("chunk_id", ""),
                    "title": ref.get("title", ""),
                    "source": ref.get("source", ""),
                    "source_type": ref.get("source_type", ""),
                    "path": ref.get("path", ""),
                    "score": ref.get("score", 0.0),
                    "text_excerpt": "",
                    "metadata": {},
                })

        # --- 6. retrieval_trace --------------------------------------------
        retrieval_trace: dict[str, Any] = {
            "kg_node_count": len(matched_kg_nodes),
            "kg_edge_count": len(matched_kg_edges),
            "concept_count": len(matched_concepts),
            "question_count": len(matched_questions),
            "evidence_chunk_count": len(matched_chunks),
            "has_text_excerpt": any(
                ch.get("text_excerpt") for ch in matched_chunks
            ),
        }

        # --- 7. assemble node entry ----------------------------------------
        evidence_map[node_id] = {
            "outline_node_id": node_id,
            "outline_node_title": node.get("title", ""),
            "sequence_reason": node.get("sequence_reason", ""),
            "kg_node_ids": list(node_kg_ids),
            "kg_nodes": matched_kg_nodes,
            "kg_edges": matched_kg_edges,
            "concepts": matched_concepts,
            "question_ids": resolved_question_ids,
            "questions": matched_questions,
            "source_refs": source_refs,
            "evidence_chunks": matched_chunks,
            "retrieval_trace": retrieval_trace,
        }

    return evidence_map


# ---------------------------------------------------------------------------
# LLM Agent Planner (NEW main path)
# ---------------------------------------------------------------------------


def _build_light_evidence_pool_for_prompt(
    evidence_pool: dict[str, Any],
) -> dict[str, Any]:
    """Return a compact evidence pool for planner prompts."""
    # Light versions with truncated text_excerpt for LLM context budget
    # Cap evidence chunks at top 12, with heavily truncated excerpts
    light_chunks: list[dict[str, Any]] = []
    for ch in evidence_pool.get("evidence_chunks", [])[:12]:
        light_chunks.append({
            "chunk_id": ch.get("chunk_id", ""),
            "title": ch.get("title", ""),
            "source_type": ch.get("source_type", ""),
            "score": ch.get("score", 0.0),
            "text_excerpt": (ch.get("text_excerpt", "") or "")[:120],
        })

    # Cap edges at top 30 — too many edges overwhelm the LLM
    light_edges: list[dict[str, Any]] = []
    for e in evidence_pool.get("kg_edges", [])[:30]:
        light_edges.append({
            "edge_id": e.get("edge_id", ""),
            "source_node_id": e.get("source_node_id", ""),
            "target_node_id": e.get("target_node_id", ""),
            "relation": e.get("relation", ""),
        })

    # Cap questions
    light_questions: list[dict[str, Any]] = []
    for q in evidence_pool.get("questions", [])[:10]:
        light_questions.append({
            "question_id": q.get("question_id", ""),
            "type": q.get("type", ""),
            "stem": (q.get("stem", "") or "")[:150],
            "primary_node_id": q.get("primary_node_id", ""),
        })

    return {
        "kg_nodes": evidence_pool.get("kg_nodes", []),
        "kg_edges": light_edges,
        "concepts": evidence_pool.get("concepts", []),
        "questions": light_questions,
        "evidence_chunks": light_chunks,
        "counts": evidence_pool.get("counts", {}),
    }


def _build_agent_planner_user_prompt(
    topic: str,
    user_profile: dict[str, Any],
    evidence_pool: dict[str, Any],
) -> str:
    """Build the user-prompt JSON for the LLM Agent Planner.

    Formats the evidence pool into a concise JSON that the LLM can digest,
    with truncated excerpts and capped list sizes to stay within context limits.
    """
    payload = {
        "topic": topic,
        "user_profile": user_profile,
        "evidence_pool": _build_light_evidence_pool_for_prompt(evidence_pool),
    }

    return json.dumps(payload, ensure_ascii=False, indent=2)


def normalize_agent_planner_output(raw: Any) -> dict[str, Any]:
    """Normalize an LLM Agent Planner raw output into a canonical plan dict.

    Allowed normalizations (format/name fixes only — no content generation):
    - outline / items / sections  ->  nodes
    - node_id  ->  id
    - objectives / goals  ->  learning_objectives
    - string objective  ->  single-element list
    - strip markdown code fences
    - clean whitespace
    - ignore extra fields

    Returns the normalized dict, or raises ValueError if completely
    unparseable.
    """
    if isinstance(raw, str):
        text = raw.strip()
        # Strip markdown code fences
        fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if not fence_match:
            fence_match = re.search(r"```(?:json)?\s*\n?(.*)$", text, re.DOTALL)
        if fence_match:
            text = fence_match.group(1).strip()
        # Try to salvage truncated JSON
        json_error: str | None = None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            json_error = (
                f"pos={exc.pos} line={exc.lineno} col={exc.colno} msg={exc.msg}"
            )
            salvaged = _try_salvage_json(text)
            if salvaged is not None:
                parsed = salvaged
            else:
                raise ValueError(
                    f"Cannot parse LLM output as JSON: "
                    f"json_error=({json_error}) text_head={text[:300]!r} "
                    f"text_tail={text[-200:]!r}"
                )
    elif isinstance(raw, dict):
        parsed = raw
    else:
        raise ValueError(f"Unexpected LLM output type: {type(raw)}")

    if not isinstance(parsed, dict):
        raise ValueError(f"Expected dict from LLM, got {type(parsed)}")

    plan: dict[str, Any] = {}

    # --- title ---
    plan["title"] = str(parsed.get("title", "")).strip()

    # --- sequence_strategy ---
    plan["sequence_strategy"] = str(parsed.get("sequence_strategy", "")).strip()

    # --- nodes ---
    raw_nodes = parsed.get("nodes")
    if raw_nodes is None:
        raw_nodes = parsed.get("outline") or parsed.get("items") or parsed.get("sections") or []

    if not isinstance(raw_nodes, list):
        raw_nodes = []

    normalized_nodes: list[dict[str, Any]] = []
    for i, rn in enumerate(raw_nodes):
        if not isinstance(rn, dict):
            continue
        nn: dict[str, Any] = {}

        # id (must come from LLM; empty string → validate will flag it)
        nid = rn.get("id") or rn.get("node_id") or ""
        nn["id"] = str(nid).strip()

        # title
        title = rn.get("title") or rn.get("name") or ""
        nn["title"] = str(title).strip()

        # learning_objectives
        raw_obj = rn.get("learning_objectives") or rn.get("objectives") or rn.get("goals") or []
        if isinstance(raw_obj, str):
            raw_obj = [raw_obj]
        if not isinstance(raw_obj, list):
            raw_obj = []
        nn["learning_objectives"] = [str(o).strip() for o in raw_obj if str(o).strip()]

        # sequence_reason
        nn["sequence_reason"] = str(rn.get("sequence_reason", "")).strip()

        # evidence references
        nn["kg_node_ids"] = _normalize_str_list(rn.get("kg_node_ids", []))
        nn["kg_edge_ids"] = _normalize_str_list(rn.get("kg_edge_ids", []))
        nn["concept_ids"] = _normalize_str_list(rn.get("concept_ids", []))
        nn["question_ids"] = _normalize_str_list(rn.get("question_ids", []))
        nn["evidence_chunk_ids"] = _normalize_str_list(rn.get("evidence_chunk_ids", []))

        # difficulty
        diff = str(rn.get("difficulty", "basic")).strip().lower()
        if diff not in ("basic", "intermediate", "advanced"):
            diff = "basic"
        nn["difficulty"] = diff

        # prerequisites
        prereqs = rn.get("prerequisites", [])
        nn["prerequisites"] = _normalize_str_list(prereqs if isinstance(prereqs, list) else [])

        # estimated_minutes (keep if LLM provided, else default)
        est = rn.get("estimated_minutes", 8)
        nn["estimated_minutes"] = int(est) if isinstance(est, (int, float)) else 8

        normalized_nodes.append(nn)

    plan["nodes"] = normalized_nodes
    return plan


def repair_non_content_planner_fields(plan: dict[str, Any]) -> list[str]:
    """Repair sequencing metadata without inventing medical content."""
    warnings: list[str] = []
    nodes = plan.get("nodes", [])
    if not isinstance(nodes, list):
        return warnings
    for index, node in enumerate(nodes):
        if not isinstance(node, dict) or node.get("sequence_reason"):
            continue
        title = str(node.get("title") or f"node_{index + 1}").strip()
        node["sequence_reason"] = (
            f"从「{title}」开始建立本课程的基础框架。"
            if index == 0
            else f"在前一节基础上继续学习「{title}」。"
        )
        warnings.append(
            f"node[{index}] sequence_reason was empty and was deterministically repaired"
        )
    return warnings


def _normalize_str_list(value: Any) -> list[str]:
    """Normalize a value to a list of non-empty strings."""
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def validate_agent_planner_output(
    plan: dict[str, Any],
    evidence_pool: dict[str, Any],
) -> list[str]:
    """Validate a normalized agent planner output against hard constraints.

    Returns a list of validation error strings (empty list = valid).
    Does NOT modify the plan — only checks it.
    """
    errors: list[str] = []

    # --- title ---
    if not plan.get("title"):
        errors.append("plan title is empty")

    # --- nodes ---
    nodes = plan.get("nodes", [])
    if not isinstance(nodes, list) or len(nodes) == 0:
        errors.append("plan has no nodes")
        return errors

    if len(nodes) < 2:
        errors.append(f"plan has only {len(nodes)} node(s); expected at least 2")

    # Build index sets from evidence_pool for id validation
    pool_node_ids: set[str] = {
        n["node_id"] for n in evidence_pool.get("kg_nodes", []) if n.get("node_id")
    }
    pool_edge_ids: set[str] = {
        e["edge_id"] for e in evidence_pool.get("kg_edges", []) if e.get("edge_id")
    }
    pool_concept_ids: set[str] = {
        c["node_id"] for c in evidence_pool.get("concepts", []) if c.get("node_id")
    }
    pool_question_ids: set[str] = {
        q["question_id"] for q in evidence_pool.get("questions", []) if q.get("question_id")
    }
    pool_chunk_ids: set[str] = {
        ch["chunk_id"] for ch in evidence_pool.get("evidence_chunks", []) if ch.get("chunk_id")
    }

    node_ids_seen: set[str] = set()

    for i, node in enumerate(nodes):
        nid = node.get("id", "")

        # id
        if not nid:
            errors.append(f"node[{i}] has empty id")
        elif nid in node_ids_seen:
            errors.append(f"node[{i}] duplicate id: {nid!r}")
        else:
            node_ids_seen.add(nid)

        # title
        if not node.get("title"):
            errors.append(f"node[{i}] ({nid}) title is empty")

        # learning_objectives
        objectives = node.get("learning_objectives", [])
        if not isinstance(objectives, list) or len(objectives) < 1:
            errors.append(f"node[{i}] ({nid}) has no learning_objectives")

        # sequence_reason
        if not node.get("sequence_reason"):
            errors.append(f"node[{i}] ({nid}) sequence_reason is empty")

        # evidence: must have BOTH kg_node_ids AND evidence_chunk_ids
        has_kg = bool(node.get("kg_node_ids"))
        has_chunks = bool(node.get("evidence_chunk_ids"))

        if not has_kg:
            errors.append(
                f"node[{i}] ({nid}) has no kg_node_ids"
            )
        if not has_chunks:
            errors.append(
                f"node[{i}] ({nid}) has no evidence_chunk_ids"
            )

        # Validate referenced ids exist in evidence_pool
        for kg_nid in node.get("kg_node_ids", []):
            if kg_nid not in pool_node_ids:
                errors.append(
                    f"node[{i}] ({nid}) references unknown kg_node_id: {kg_nid!r}"
                )

        for eid in node.get("kg_edge_ids", []):
            if eid not in pool_edge_ids:
                errors.append(
                    f"node[{i}] ({nid}) references unknown kg_edge_id: {eid!r}"
                )

        for cid in node.get("concept_ids", []):
            if cid not in pool_concept_ids and cid not in pool_node_ids:
                errors.append(
                    f"node[{i}] ({nid}) references unknown concept_id: {cid!r}"
                )

        for qid in node.get("question_ids", []):
            if qid not in pool_question_ids:
                errors.append(
                    f"node[{i}] ({nid}) references unknown question_id: {qid!r}"
                )

        for cid in node.get("evidence_chunk_ids", []):
            if cid not in pool_chunk_ids:
                errors.append(
                    f"node[{i}] ({nid}) references unknown evidence_chunk_id: {cid!r}"
                )

    return errors


def _build_agent_outline_evidence_map(
    agent_plan: dict[str, Any],
    evidence_pool: dict[str, Any],
) -> dict[str, Any]:
    """Build outline_evidence_map from an LLM Agent Planner output.

    Unlike the fallback version which matches by node_kg_ids intersection,
    this version uses the LLM's explicit edge / chunk / question selections
    to construct the per-node evidence bundle.
    """

    pool_kg_nodes = evidence_pool.get("kg_nodes", [])
    pool_kg_edges = evidence_pool.get("kg_edges", [])
    pool_concepts = evidence_pool.get("concepts", [])
    pool_questions = evidence_pool.get("questions", [])
    pool_evidence_chunks = evidence_pool.get("evidence_chunks", [])

    # Index helpers
    pool_kg_node_by_id: dict[str, dict] = {
        n["node_id"]: n for n in pool_kg_nodes if n["node_id"]
    }
    pool_edge_by_id: dict[str, dict] = {
        e["edge_id"]: e for e in pool_kg_edges if e.get("edge_id")
    }
    pool_concept_by_node: dict[str, dict] = {
        c["node_id"]: c for c in pool_concepts if c["node_id"]
    }
    pool_question_by_id: dict[str, dict] = {
        q["question_id"]: q for q in pool_questions if q["question_id"]
    }
    pool_chunk_by_id: dict[str, dict] = {
        ch["chunk_id"]: ch for ch in pool_evidence_chunks if ch["chunk_id"]
    }

    evidence_map: dict[str, Any] = {}

    for node in agent_plan.get("nodes", []):
        node_id = node.get("id", "")
        node_kg_ids: set[str] = set(node.get("kg_node_ids", []))
        node_edge_ids: set[str] = set(node.get("kg_edge_ids", []))
        node_concept_ids: set[str] = set(node.get("concept_ids", []))
        node_question_ids: set[str] = set(node.get("question_ids", []))
        node_chunk_ids: set[str] = set(node.get("evidence_chunk_ids", []))

        # --- kg_nodes: by LLM-selected node_ids ---
        matched_kg_nodes: list[dict] = [
            pool_kg_node_by_id[nid]
            for nid in node_kg_ids
            if nid in pool_kg_node_by_id
        ]

        # --- kg_edges: by LLM-selected edge_ids ---
        matched_kg_edges: list[dict] = [
            pool_edge_by_id[eid]
            for eid in node_edge_ids
            if eid in pool_edge_by_id
        ]

        # --- concepts: by LLM-selected concept_ids (or kg_node_ids fallback) ---
        matched_concepts: list[dict] = []
        concept_id_set = node_concept_ids if node_concept_ids else node_kg_ids
        for cid in concept_id_set:
            c = pool_concept_by_node.get(cid)
            if c:
                matched_concepts.append(c)

        # --- questions: by LLM-selected question_ids ---
        matched_questions: list[dict] = []
        resolved_question_ids: list[str] = []
        for qid in node_question_ids:
            q = pool_question_by_id.get(qid)
            if q:
                matched_questions.append(q)
                resolved_question_ids.append(qid)

        # --- evidence_chunks: by LLM-selected chunk_ids ---
        matched_chunks: list[dict] = []
        for cid in node_chunk_ids:
            ch = pool_chunk_by_id.get(cid)
            if ch:
                matched_chunks.append(ch)

        # --- source_refs: derived from evidence_chunk_ids ---
        source_refs: list[dict] = []
        for ch in matched_chunks:
            source_refs.append({
                "chunk_id": ch.get("chunk_id", ""),
                "title": ch.get("title", ""),
                "source": ch.get("source", ""),
                "source_type": ch.get("source_type", ""),
                "path": ch.get("path", ""),
                "score": ch.get("score", 0.0),
            })

        # --- retrieval_trace ---
        retrieval_trace: dict[str, Any] = {
            "kg_node_count": len(matched_kg_nodes),
            "kg_edge_count": len(matched_kg_edges),
            "concept_count": len(matched_concepts),
            "question_count": len(matched_questions),
            "evidence_chunk_count": len(matched_chunks),
            "has_text_excerpt": any(
                ch.get("text_excerpt") for ch in matched_chunks
            ),
        }

        evidence_map[node_id] = {
            "outline_node_id": node_id,
            "outline_node_title": node.get("title", ""),
            "sequence_reason": node.get("sequence_reason", ""),
            "kg_node_ids": list(node_kg_ids),
            "kg_nodes": matched_kg_nodes,
            "kg_edges": matched_kg_edges,
            "concepts": matched_concepts,
            "question_ids": resolved_question_ids,
            "questions": matched_questions,
            "source_refs": source_refs,
            "evidence_chunks": matched_chunks,
            "retrieval_trace": retrieval_trace,
        }

    return evidence_map


def plan_lesson_agent(
    topic: str,
    user_profile: dict[str, Any],
    evidence_pool: dict[str, Any],
    planning_context: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any]]:
    """Run the LLM Agent Planner as the primary path.

    Returns ``(outline, outline_evidence_map, planning_trace)``.
    On failure, ``outline`` and ``outline_evidence_map`` are None and
    *planning_trace* explains the failure.
    """
    trace: dict[str, Any] = {
        "strategy": "llm_agent_planner",
        "is_agent_planned": False,
        "fallback_used": False,
        "sequence_strategy": "",
        "evidence_pool_counts": evidence_pool.get("counts", {}),
        "json_mode_used": False,
        "normalization_warnings": [],
        "validation_errors": [],
        # diagnostics
        "agent_planner_prompt_chars": 0,
        "agent_planner_raw_response_chars": 0,
        "agent_planner_json_error": None,
    }

    # --- 1. check LLM availability ------------------------------------------
    try:
        from backend.services.llm_client import chat_completion, is_llm_configured
    except Exception:
        trace["fallback_used"] = True
        trace["fallback_reason"] = "llm_client_import_failed"
        return None, None, trace

    _agent_api_keys = (
        "LEARNING_PLANNER_LLM_API_KEY",
        "PLANNER_LLM_API_KEY",
        "LEARNING_LLM_API_KEY",
        "QWEN_API_KEY",
        "DASHSCOPE_API_KEY",
    )
    _agent_base_urls = (
        "LEARNING_PLANNER_LLM_BASE_URL",
        "PLANNER_LLM_BASE_URL",
        "LEARNING_LLM_BASE_URL",
        "QWEN_BASE_URL",
        "DASHSCOPE_BASE_URL",
    )
    _agent_models = (
        "LEARNING_PLANNER_LLM_MODEL",
        "PLANNER_LLM_MODEL",
        "LEARNING_LLM_MODEL",
        "QWEN_MODEL",
        "DASHSCOPE_MODEL",
    )

    if not is_llm_configured(*_agent_api_keys):
        trace["fallback_used"] = True
        trace["fallback_reason"] = "no_llm_api_key_configured"
        return None, None, trace

    # --- 2. build prompts ---------------------------------------------------
    user_prompt = _build_agent_planner_user_prompt(topic, user_profile, evidence_pool)

    _prompt_dir = Path(__file__).resolve().parent / "prompts"
    agent_prompt_path = _prompt_dir / "planner_agent.md"
    if agent_prompt_path.exists():
        system_prompt = agent_prompt_path.read_text(encoding="utf-8")
    else:
        system_prompt = (
            "你是口腔医学学习路径规划助手。"
            "基于topic、user_profile和evidence_pool自主规划学习单元和顺序。"
            "输出严格JSON。"
        )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    trace["agent_planner_prompt_chars"] = len(system_prompt) + len(user_prompt)

    # --- 3. call LLM --------------------------------------------------------
    try:
        raw = chat_completion(
            messages,
            temperature=0.3,
            max_tokens=8000,
            api_key_names=_agent_api_keys,
            base_url_names=_agent_base_urls,
            model_names=_agent_models,
            response_format={"type": "json_object"},
        )
        trace["json_mode_used"] = True
    except Exception as exc:
        trace["fallback_used"] = True
        trace["fallback_reason"] = f"llm_call_failed: {str(exc)[:200]}"
        return None, None, trace

    trace["agent_planner_raw_response_chars"] = len(raw)
    trace["agent_planner_raw_response_head"] = raw[:500]
    trace["agent_planner_raw_response_tail"] = raw[-500:]

    # --- 4. normalize -------------------------------------------------------
    norm_warnings: list[str] = []
    try:
        plan = normalize_agent_planner_output(raw)
    except ValueError as exc:
        trace["fallback_used"] = True
        exc_msg = str(exc)
        # Detect truncation: Unterminated string OR tail doesn't end with }
        is_truncated = (
            "Unterminated string" in exc_msg
            or not raw.rstrip().endswith("}")
        )
        if is_truncated:
            trace["fallback_reason"] = (
                f"llm_output_truncated: {exc_msg[:180]}"
            )
        else:
            trace["fallback_reason"] = (
                f"normalization_failed: {exc_msg[:180]}"
            )
        trace["agent_planner_json_error"] = exc_msg[:500]
        return None, None, trace

    norm_warnings.extend(repair_non_content_planner_fields(plan))

    # --- 5. validate --------------------------------------------------------
    validation_errors = validate_agent_planner_output(plan, evidence_pool)
    trace["validation_errors"] = validation_errors

    if validation_errors:
        # Any validation error → reject the plan, do not use
        trace["fallback_used"] = True
        trace["fallback_reason"] = (
            f"validation_failed: {len(validation_errors)} error(s); "
            f"first: {validation_errors[0][:200]}"
        )
        return None, None, trace

    # --- 5b. coverage_warnings (non-fatal) ---------------------------------
    coverage_warnings: list[str] = []
    for node in plan.get("nodes", []):
        nid = node.get("id", "?")
        if not node.get("kg_edge_ids"):
            coverage_warnings.append(f"{nid} has no kg_edge_ids")
        if not node.get("concept_ids"):
            coverage_warnings.append(f"{nid} has no concept_ids")
        if not node.get("question_ids"):
            coverage_warnings.append(f"{nid} has no question_ids")
    trace["coverage_warnings"] = coverage_warnings

    trace["normalization_warnings"] = norm_warnings

    # --- 6. build outline dict from agent plan ------------------------------
    outline_nodes: list[dict[str, Any]] = []
    for node in plan.get("nodes", []):
        onode: dict[str, Any] = {
            "id": node["id"],
            "title": node["title"],
            "kg_node_ids": node.get("kg_node_ids", []),
            "learning_objectives": node.get("learning_objectives", []),
            "prerequisites": node.get("prerequisites", []),
            "estimated_minutes": node.get("estimated_minutes", 8),
            "difficulty": node.get("difficulty", "basic"),
            "source_refs": [],  # populated by evidence map builder
            "sequence_reason": node.get("sequence_reason", ""),
            # Preserve LLM-selected evidence index ids in outline nodes
            "kg_edge_ids": node.get("kg_edge_ids", []),
            "concept_ids": node.get("concept_ids", []),
            "evidence_chunk_ids": node.get("evidence_chunk_ids", []),
            "question_ids": node.get("question_ids", []),
        }

        # Validate with schema
        try:
            OutlineNode(**onode)
        except Exception:
            pass  # Will be caught by LessonOutline validation below

        outline_nodes.append(onode)

    outline = {
        "title": plan.get("title", f"{topic}系统学习路径"),
        "nodes": outline_nodes,
    }

    try:
        LessonOutline(**outline)
    except Exception as exc:
        trace["fallback_used"] = True
        trace["fallback_reason"] = f"outline_schema_validation_failed: {exc}"
        return None, None, trace

    # --- 7. build outline_evidence_map --------------------------------------
    outline_evidence_map = _build_agent_outline_evidence_map(plan, evidence_pool)

    # --- 8. finalize trace --------------------------------------------------
    trace["is_agent_planned"] = True
    trace["sequence_strategy"] = plan.get("sequence_strategy", "")
    trace["fallback_used"] = False

    # Compute outline_evidence_map_counts
    oem_nodes = len(outline_evidence_map)
    oem_kg = sum(len(e["kg_nodes"]) for e in outline_evidence_map.values())
    oem_edges = sum(len(e["kg_edges"]) for e in outline_evidence_map.values())
    oem_concepts = sum(len(e["concepts"]) for e in outline_evidence_map.values())
    oem_questions = sum(len(e["questions"]) for e in outline_evidence_map.values())
    oem_chunks = sum(len(e["evidence_chunks"]) for e in outline_evidence_map.values())
    trace["outline_evidence_map_counts"] = {
        "outline_nodes": oem_nodes,
        "total_kg_nodes_bound": oem_kg,
        "total_kg_edges_bound": oem_edges,
        "total_concepts_bound": oem_concepts,
        "total_questions_bound": oem_questions,
        "total_evidence_chunks_bound": oem_chunks,
    }

    return outline, outline_evidence_map, trace


def _agent_llm_settings() -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    return (
        (
            "LEARNING_PLANNER_LLM_API_KEY",
            "PLANNER_LLM_API_KEY",
            "LEARNING_LLM_API_KEY",
            "QWEN_API_KEY",
            "DASHSCOPE_API_KEY",
        ),
        (
            "LEARNING_PLANNER_LLM_BASE_URL",
            "PLANNER_LLM_BASE_URL",
            "LEARNING_LLM_BASE_URL",
            "QWEN_BASE_URL",
            "DASHSCOPE_BASE_URL",
        ),
        (
            "LEARNING_PLANNER_LLM_MODEL",
            "PLANNER_LLM_MODEL",
            "LEARNING_LLM_MODEL",
            "QWEN_MODEL",
            "DASHSCOPE_MODEL",
        ),
    )


def _empty_refinement_trace(reason: str) -> dict[str, Any]:
    return {
        "outline_refinement": reason,
        "outline_refinement_model": None,
        "outline_refinement_error": None,
        "outline_refinement_change_count": 0,
        "outline_refinement_changes": [],
    }


def _outline_evidence_counts(
    outline_evidence_map: dict[str, Any],
) -> dict[str, int]:
    return {
        "outline_nodes": len(outline_evidence_map),
        "total_kg_nodes_bound": sum(
            len(entry.get("kg_nodes", []))
            for entry in outline_evidence_map.values()
        ),
        "total_kg_edges_bound": sum(
            len(entry.get("kg_edges", []))
            for entry in outline_evidence_map.values()
        ),
        "total_concepts_bound": sum(
            len(entry.get("concepts", []))
            for entry in outline_evidence_map.values()
        ),
        "total_questions_bound": sum(
            len(entry.get("questions", []))
            for entry in outline_evidence_map.values()
        ),
        "total_evidence_chunks_bound": sum(
            len(entry.get("evidence_chunks", []))
            for entry in outline_evidence_map.values()
        ),
    }


def _build_outline_from_agent_plan(
    plan: dict[str, Any],
    topic: str,
) -> dict[str, Any]:
    outline_nodes: list[dict[str, Any]] = []
    for node in plan.get("nodes", []):
        outline_nodes.append({
            "id": node["id"],
            "title": node["title"],
            "kg_node_ids": node.get("kg_node_ids", []),
            "learning_objectives": node.get("learning_objectives", []),
            "prerequisites": node.get("prerequisites", []),
            "estimated_minutes": node.get("estimated_minutes", 8),
            "difficulty": node.get("difficulty", "basic"),
            "source_refs": [],
            "sequence_reason": node.get("sequence_reason", ""),
            "kg_edge_ids": node.get("kg_edge_ids", []),
            "concept_ids": node.get("concept_ids", []),
            "evidence_chunk_ids": node.get("evidence_chunk_ids", []),
            "question_ids": node.get("question_ids", []),
        })

    outline = {
        "title": plan.get("title", f"{topic}系统学习路径"),
        "nodes": outline_nodes,
    }
    LessonOutline(**outline)
    return outline


def _validate_progressive_initial_output(
    plan: dict[str, Any],
    evidence_pool: dict[str, Any],
    max_nodes: int,
) -> list[str]:
    """Validate first-section-fast planner output.

    Only node_1 must be fully evidence-backed. Remaining nodes are allowed
    to be title-only placeholders for background completion.
    """
    errors: list[str] = []
    if not plan.get("title"):
        errors.append("plan title is empty")

    nodes = plan.get("nodes", [])
    if not isinstance(nodes, list) or not nodes:
        return errors + ["plan has no nodes"]

    if len(nodes) > max_nodes:
        errors.append(f"plan has {len(nodes)} nodes; max is {max_nodes}")

    pool_node_ids: set[str] = {
        n["node_id"] for n in evidence_pool.get("kg_nodes", []) if n.get("node_id")
    }
    pool_edge_ids: set[str] = {
        e["edge_id"] for e in evidence_pool.get("kg_edges", []) if e.get("edge_id")
    }
    pool_concept_ids: set[str] = {
        c["node_id"] for c in evidence_pool.get("concepts", []) if c.get("node_id")
    }
    pool_question_ids: set[str] = {
        q["question_id"] for q in evidence_pool.get("questions", []) if q.get("question_id")
    }
    pool_chunk_ids: set[str] = {
        ch["chunk_id"] for ch in evidence_pool.get("evidence_chunks", []) if ch.get("chunk_id")
    }

    first = nodes[0]
    if not first.get("id"):
        errors.append("first node has empty id")
    if not first.get("title"):
        errors.append("first node title is empty")
    if not first.get("learning_objectives"):
        errors.append("first node has no learning_objectives")
    if not first.get("sequence_reason"):
        errors.append("first node sequence_reason is empty")
    if not first.get("kg_node_ids"):
        errors.append("first node has no kg_node_ids")
    if not first.get("evidence_chunk_ids"):
        errors.append("first node has no evidence_chunk_ids")

    for nid in first.get("kg_node_ids", []):
        if nid not in pool_node_ids:
            errors.append(f"first node references unknown kg_node_id: {nid!r}")
    for eid in first.get("kg_edge_ids", []):
        if eid not in pool_edge_ids:
            errors.append(f"first node references unknown kg_edge_id: {eid!r}")
    for cid in first.get("concept_ids", []):
        if cid not in pool_concept_ids:
            errors.append(f"first node references unknown concept_id: {cid!r}")
    for qid in first.get("question_ids", []):
        if qid not in pool_question_ids:
            errors.append(f"first node references unknown question_id: {qid!r}")
    for cid in first.get("evidence_chunk_ids", []):
        if cid not in pool_chunk_ids:
            errors.append(f"first node references unknown evidence_chunk_id: {cid!r}")

    seen_ids: set[str] = set()
    for i, node in enumerate(nodes[:max_nodes]):
        nid = node.get("id", "")
        if not nid:
            errors.append(f"node[{i}] has empty id")
        elif nid in seen_ids:
            errors.append(f"node[{i}] duplicate id: {nid!r}")
        seen_ids.add(nid)
        if not node.get("title"):
            errors.append(f"node[{i}] ({nid}) title is empty")

    return errors


def _build_progressive_initial_outline(
    plan: dict[str, Any],
    topic: str,
    max_nodes: int,
) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []
    raw_nodes = plan.get("nodes", [])[:max_nodes]
    for i, node in enumerate(raw_nodes):
        if i == 0:
            nodes.append({
                "id": node["id"],
                "title": node["title"],
                "kg_node_ids": node.get("kg_node_ids", []),
                "learning_objectives": node.get("learning_objectives", []),
                "prerequisites": node.get("prerequisites", []),
                "estimated_minutes": node.get("estimated_minutes", 8),
                "difficulty": node.get("difficulty", "basic"),
                "source_refs": [],
                "sequence_reason": node.get("sequence_reason", ""),
                "kg_edge_ids": node.get("kg_edge_ids", []),
                "concept_ids": node.get("concept_ids", []),
                "evidence_chunk_ids": node.get("evidence_chunk_ids", []),
                "question_ids": node.get("question_ids", []),
                "planning_status": "ready",
            })
            continue

        prerequisites = node.get("prerequisites", [])
        if not prerequisites and nodes:
            prerequisites = [nodes[-1]["id"]]
        nodes.append({
            "id": node["id"],
            "title": node["title"],
            "kg_node_ids": [],
            "learning_objectives": [],
            "prerequisites": prerequisites,
            "estimated_minutes": node.get("estimated_minutes", 8),
            "difficulty": node.get("difficulty", "basic"),
            "source_refs": [],
            "sequence_reason": "pending_background_planning",
            "kg_edge_ids": [],
            "concept_ids": [],
            "evidence_chunk_ids": [],
            "question_ids": [],
            "planning_status": "title_only_pending",
        })

    outline = {
        "title": plan.get("title", f"{topic}系统学习路径"),
        "nodes": nodes,
        "planning_status": "initial_ready",
        "sequence_strategy": plan.get("sequence_strategy", ""),
    }
    LessonOutline(**outline)
    return outline


def _build_progressive_initial_user_prompt(
    topic: str,
    user_profile: dict[str, Any],
    evidence_pool: dict[str, Any],
    max_nodes: int,
) -> str:
    payload = {
        "topic": topic,
        "user_profile": user_profile,
        "max_nodes": max_nodes,
        "task": {
            "mode": "progressive_initial_outline",
            "requirements": [
                "Choose the best first section to teach immediately. It should usually be a definition, nature, boundary, or basic property section when evidence supports that.",
                "Return a complete evidence-backed node_1 with objectives, sequence_reason, and evidence ids.",
                "For later sections, return only id, title, difficulty, prerequisites, and estimated_minutes. Do not fill objectives or evidence for later sections.",
                "Use only ids from evidence_pool. If evidence is weak, create fewer later title placeholders.",
            ],
            "output_shape": {
                "title": "course title",
                "sequence_strategy": "short overall logic",
                "nodes": [
                    "node_1 complete",
                    "node_2 title only",
                    "node_3 title only",
                ],
            },
        },
        "evidence_pool": _build_light_evidence_pool_for_prompt(evidence_pool),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def plan_lesson_initial_agent(
    topic: str,
    user_profile: dict[str, Any],
    evidence_pool: dict[str, Any],
    planning_context: dict[str, Any],
    max_nodes: int = 6,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any]]:
    """Plan only the first teachable section plus later title placeholders."""
    trace: dict[str, Any] = {
        "strategy": "progressive_llm_agent_planner",
        "is_agent_planned": False,
        "fallback_used": False,
        "sequence_strategy": "",
        "evidence_pool_counts": evidence_pool.get("counts", {}),
        "json_mode_used": False,
        "normalization_warnings": [],
        "validation_errors": [],
        "agent_planner_prompt_chars": 0,
        "agent_planner_raw_response_chars": 0,
        "agent_planner_json_error": None,
        "planning_context_source": planning_context.get("source", ""),
    }

    try:
        from backend.services.llm_client import chat_completion, is_llm_configured
    except Exception:
        trace["fallback_used"] = True
        trace["fallback_reason"] = "llm_client_import_failed"
        return None, None, trace

    api_keys, base_urls, model_names = _agent_llm_settings()
    if not is_llm_configured(*api_keys):
        trace["fallback_used"] = True
        trace["fallback_reason"] = "no_llm_api_key_configured"
        return None, None, trace

    system_prompt = (
        "You are OralSage's progressive lesson planner. "
        "The user must start learning as soon as possible. "
        "Return strict compact JSON only. "
        "Plan node_1 completely with evidence ids. "
        "For all later nodes, return title-only placeholders. "
        "Do not invent evidence ids."
    )
    user_prompt = _build_progressive_initial_user_prompt(
        topic=topic,
        user_profile=user_profile,
        evidence_pool=evidence_pool,
        max_nodes=max_nodes,
    )
    trace["agent_planner_prompt_chars"] = len(system_prompt) + len(user_prompt)

    try:
        raw = chat_completion(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.25,
            max_tokens=2600,
            api_key_names=api_keys,
            base_url_names=base_urls,
            model_names=model_names,
            response_format={"type": "json_object"},
        )
        trace["json_mode_used"] = True
    except Exception as exc:
        trace["fallback_used"] = True
        trace["fallback_reason"] = f"llm_call_failed: {str(exc)[:200]}"
        return None, None, trace

    trace["agent_planner_raw_response_chars"] = len(raw)
    trace["agent_planner_raw_response_head"] = raw[:500]
    trace["agent_planner_raw_response_tail"] = raw[-500:]

    try:
        plan = normalize_agent_planner_output(raw)
    except ValueError as exc:
        trace["fallback_used"] = True
        exc_msg = str(exc)
        trace["fallback_reason"] = f"normalization_failed: {exc_msg[:180]}"
        trace["agent_planner_json_error"] = exc_msg[:500]
        return None, None, trace

    trace["normalization_warnings"].extend(
        repair_non_content_planner_fields(plan)
    )
    validation_errors = _validate_progressive_initial_output(
        plan,
        evidence_pool,
        max_nodes=max_nodes,
    )
    trace["validation_errors"] = validation_errors
    if validation_errors:
        trace["fallback_used"] = True
        trace["fallback_reason"] = (
            f"validation_failed: {len(validation_errors)} error(s); "
            f"first: {validation_errors[0][:200]}"
        )
        return None, None, trace

    try:
        outline = _build_progressive_initial_outline(plan, topic, max_nodes)
    except Exception as exc:
        trace["fallback_used"] = True
        trace["fallback_reason"] = f"outline_schema_validation_failed: {exc}"
        return None, None, trace

    first_plan = {
        "title": plan.get("title", outline.get("title", "")),
        "sequence_strategy": plan.get("sequence_strategy", ""),
        "nodes": [plan.get("nodes", [])[0]],
    }
    outline_evidence_map = _build_agent_outline_evidence_map(
        first_plan,
        evidence_pool,
    )

    trace["is_agent_planned"] = True
    trace["fallback_used"] = False
    trace["sequence_strategy"] = plan.get("sequence_strategy", "")
    trace["outline_evidence_map_counts"] = _outline_evidence_counts(
        outline_evidence_map
    )
    trace["initial_outline_node_count"] = len(outline.get("nodes", []))
    trace["title_only_node_count"] = max(0, len(outline.get("nodes", [])) - 1)

    return outline, outline_evidence_map, trace


def _build_progressive_completion_user_prompt(
    topic: str,
    user_profile: dict[str, Any],
    evidence_pool: dict[str, Any],
    initial_outline: dict[str, Any],
) -> str:
    nodes = initial_outline.get("nodes", [])
    first_node = copy.deepcopy(nodes[0]) if nodes else {}
    title_skeleton = [
        {
            "id": n.get("id", ""),
            "title": n.get("title", ""),
            "prerequisites": n.get("prerequisites", []),
        }
        for n in nodes[1:]
    ]
    payload = {
        "topic": topic,
        "user_profile": user_profile,
        "task": {
            "mode": "complete_progressive_outline",
            "locked_first_node": first_node,
            "later_title_skeleton": title_skeleton,
            "requirements": [
                "Keep locked_first_node as the first section; do not change its id or title.",
                "Complete the remaining outline nodes with learning_objectives, sequence_reason, prerequisites, and evidence ids.",
                "Use the later_title_skeleton as guidance, but you may rename, merge, drop, or add later nodes when evidence and teaching sequence require it.",
                "Every returned node must have at least one kg_node_id and one evidence_chunk_id from evidence_pool.",
                "Use only ids present in evidence_pool.",
            ],
        },
        "evidence_pool": _build_light_evidence_pool_for_prompt(evidence_pool),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _merge_locked_first_node(
    plan: dict[str, Any],
    initial_outline: dict[str, Any],
) -> dict[str, Any]:
    initial_nodes = initial_outline.get("nodes", [])
    if not initial_nodes:
        return plan

    first_node = copy.deepcopy(initial_nodes[0])
    first_id = first_node.get("id", "")
    merged_nodes = [first_node]
    for node in plan.get("nodes", []):
        if node.get("id") == first_id:
            continue
        merged_nodes.append(node)

    return {
        "title": plan.get("title") or initial_outline.get("title", ""),
        "sequence_strategy": plan.get("sequence_strategy", ""),
        "nodes": merged_nodes,
    }


def plan_lesson_completion_agent(
    topic: str,
    user_profile: dict[str, Any],
    evidence_pool: dict[str, Any],
    planning_context: dict[str, Any],
    initial_outline: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any]]:
    """Complete a progressive outline in the background."""
    trace: dict[str, Any] = {
        "strategy": "progressive_llm_agent_planner_completed",
        "is_agent_planned": False,
        "fallback_used": False,
        "sequence_strategy": "",
        "evidence_pool_counts": evidence_pool.get("counts", {}),
        "json_mode_used": False,
        "normalization_warnings": [],
        "validation_errors": [],
        "agent_planner_prompt_chars": 0,
        "agent_planner_raw_response_chars": 0,
        "agent_planner_json_error": None,
        "planning_context_source": planning_context.get("source", ""),
    }

    try:
        from backend.services.llm_client import chat_completion, is_llm_configured
    except Exception:
        trace["fallback_used"] = True
        trace["fallback_reason"] = "llm_client_import_failed"
        return None, None, trace

    api_keys, base_urls, model_names = _agent_llm_settings()
    if not is_llm_configured(*api_keys):
        trace["fallback_used"] = True
        trace["fallback_reason"] = "no_llm_api_key_configured"
        return None, None, trace

    system_prompt = (
        "You are OralSage's background lesson planner. "
        "The first section is already being taught, so keep it locked. "
        "Complete the rest of the course outline with strict evidence-backed JSON only. "
        "Do not invent ids."
    )
    user_prompt = _build_progressive_completion_user_prompt(
        topic=topic,
        user_profile=user_profile,
        evidence_pool=evidence_pool,
        initial_outline=initial_outline,
    )
    trace["agent_planner_prompt_chars"] = len(system_prompt) + len(user_prompt)

    try:
        raw = chat_completion(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=8000,
            api_key_names=api_keys,
            base_url_names=base_urls,
            model_names=model_names,
            response_format={"type": "json_object"},
        )
        trace["json_mode_used"] = True
    except Exception as exc:
        trace["fallback_used"] = True
        trace["fallback_reason"] = f"llm_call_failed: {str(exc)[:200]}"
        return None, None, trace

    trace["agent_planner_raw_response_chars"] = len(raw)
    trace["agent_planner_raw_response_head"] = raw[:500]
    trace["agent_planner_raw_response_tail"] = raw[-500:]

    try:
        plan = normalize_agent_planner_output(raw)
    except ValueError as exc:
        trace["fallback_used"] = True
        exc_msg = str(exc)
        trace["fallback_reason"] = f"normalization_failed: {exc_msg[:180]}"
        trace["agent_planner_json_error"] = exc_msg[:500]
        return None, None, trace

    trace["normalization_warnings"].extend(
        repair_non_content_planner_fields(plan)
    )
    plan = _merge_locked_first_node(plan, initial_outline)
    validation_errors = validate_agent_planner_output(plan, evidence_pool)
    trace["validation_errors"] = validation_errors
    if validation_errors:
        trace["fallback_used"] = True
        trace["fallback_reason"] = (
            f"validation_failed: {len(validation_errors)} error(s); "
            f"first: {validation_errors[0][:200]}"
        )
        return None, None, trace

    try:
        outline = _build_outline_from_agent_plan(plan, topic)
    except Exception as exc:
        trace["fallback_used"] = True
        trace["fallback_reason"] = f"outline_schema_validation_failed: {exc}"
        return None, None, trace

    outline["planning_status"] = "complete"
    outline["sequence_strategy"] = plan.get("sequence_strategy", "")
    outline_evidence_map = _build_agent_outline_evidence_map(plan, evidence_pool)

    trace["is_agent_planned"] = True
    trace["fallback_used"] = False
    trace["sequence_strategy"] = plan.get("sequence_strategy", "")
    trace["outline_evidence_map_counts"] = _outline_evidence_counts(
        outline_evidence_map
    )

    return outline, outline_evidence_map, trace


def _merge_first_node_into_outline(
    initial_outline: dict[str, Any],
    outline: dict[str, Any],
) -> dict[str, Any]:
    initial_nodes = initial_outline.get("nodes", [])
    outline_nodes = outline.get("nodes", [])
    if not initial_nodes or not outline_nodes:
        return outline

    first_node = copy.deepcopy(initial_nodes[0])
    first_id = first_node.get("id", "")
    rest_nodes = [n for n in outline_nodes if n.get("id") != first_id]
    merged = copy.deepcopy(outline)
    merged["nodes"] = [first_node] + rest_nodes
    merged["planning_status"] = "complete"
    return merged


def complete_progressive_lesson_plan(
    initial_plan_result: dict[str, Any],
) -> dict[str, Any]:
    """Complete a progressive plan after the first section has started."""
    topic = initial_plan_result.get("topic", "").strip() or "口腔医学主题"
    initial_outline = initial_plan_result.get("outline", {})
    user_profile = (
        initial_plan_result.get("source_trace", {}).get("user_profile", {})
        or {}
    )
    evidence_pool = initial_plan_result.get("evidence_pool", {})
    planning_context = initial_plan_result.get("planning_context", {})
    relevant_nodes = initial_plan_result.get("matched_nodes", [])
    edges = initial_plan_result.get("related_edges", [])
    questions = initial_plan_result.get("linked_questions", [])

    completion_outline, completion_evidence_map, completion_trace = (
        plan_lesson_completion_agent(
            topic=topic,
            user_profile=user_profile,
            evidence_pool=evidence_pool,
            planning_context=planning_context,
            initial_outline=initial_outline,
        )
    )

    if completion_outline is not None and completion_evidence_map is not None:
        outline = completion_outline
        outline_evidence_map = completion_evidence_map
        planning_trace = completion_trace
        strategy = "progressive_llm_agent_planner_completed"
        refinement_trace = _empty_refinement_trace(
            "skipped_progressive_completion_active"
        )
    else:
        full_outline, full_evidence_map, full_trace = plan_lesson_agent(
            topic=topic,
            user_profile=user_profile,
            evidence_pool=evidence_pool,
            planning_context=planning_context,
        )
        if full_outline is not None and full_evidence_map is not None:
            outline = _merge_first_node_into_outline(initial_outline, full_outline)
            outline_evidence_map = full_evidence_map
            first_id = (initial_outline.get("nodes") or [{}])[0].get("id", "")
            initial_map = initial_plan_result.get("outline_evidence_map", {})
            if first_id and first_id in initial_map:
                outline_evidence_map[first_id] = initial_map[first_id]
            planning_trace = full_trace
            planning_trace["strategy"] = "llm_agent_planner_background_fallback"
            planning_trace["progressive_completion_fallback_reason"] = (
                completion_trace.get("fallback_reason", "completion_failed")
            )
            strategy = "llm_agent_planner_background_fallback"
            refinement_trace = _empty_refinement_trace(
                "skipped_background_full_agent_active"
            )
        else:
            fallback_reason = completion_trace.get(
                "fallback_reason",
                "progressive_completion_failed",
            )
            dimensions = infer_learning_dimensions(planning_context.get("chunks", []))
            if dimensions:
                outline = build_outline_from_dimensions(topic, dimensions, relevant_nodes)
                strategy = "retrieval_enhanced_background_fallback"
            elif relevant_nodes:
                outline = build_outline_from_nodes(topic, relevant_nodes, edges, questions)
                strategy = "kag_node_match_background_fallback"
            else:
                outline = build_fallback_outline(topic)
                strategy = "fallback_outline_background_fallback"

            outline = _merge_first_node_into_outline(initial_outline, outline)
            outline_evidence_map = build_outline_evidence_map(
                outline=outline,
                evidence_pool=evidence_pool,
            )
            first_id = (initial_outline.get("nodes") or [{}])[0].get("id", "")
            initial_map = initial_plan_result.get("outline_evidence_map", {})
            if first_id and first_id in initial_map:
                outline_evidence_map[first_id] = initial_map[first_id]
            planning_trace = {
                "strategy": strategy,
                "is_agent_planned": False,
                "fallback_used": True,
                "fallback_reason": fallback_reason,
                "progressive_completion_trace": completion_trace,
                "full_agent_fallback_trace": full_trace,
                "evidence_pool_counts": evidence_pool.get("counts", {}),
                "outline_evidence_map_counts": _outline_evidence_counts(
                    outline_evidence_map
                ),
                "coverage_warnings": [],
                "sequence_strategy": "",
            }
            refinement_trace = _empty_refinement_trace(
                "skipped_background_rule_fallback"
            )

    outline["planning_status"] = "complete"
    mindmap = build_mindmap(outline)
    kag_dir = resolve_kag_dir()
    progressive_planning = {
        "enabled": True,
        "completion_required": False,
        "completed": True,
    }

    return {
        "ok": True,
        "topic": topic,
        "outline": outline,
        "mindmap": mindmap,
        "matched_nodes": relevant_nodes,
        "related_edges": edges,
        "linked_questions": questions,
        "outline_refinement_trace": refinement_trace,
        "evidence_pool": evidence_pool,
        "outline_evidence_map": outline_evidence_map,
        "planning_context": planning_context,
        "progressive_planning": progressive_planning,
        "planning_trace": planning_trace,
        "source_trace": {
            "strategy": strategy,
            "planning_context_source": planning_context.get("source", ""),
            "planning_context_ok": planning_context.get("ok", False),
            "planning_context_warning_count": len(planning_context.get("warnings", [])),
            "evidence_chunk_count": len(planning_context.get("chunks", [])),
            "dimension_count": len(
                infer_learning_dimensions(planning_context.get("chunks", []))
            ),
            "matched_node_count": len(relevant_nodes),
            "edge_count": len(edges),
            "question_count": len(questions),
            "kag_dir": str(kag_dir),
            "user_profile": user_profile,
            "outline_refinement": refinement_trace["outline_refinement"],
            "outline_refinement_model": refinement_trace["outline_refinement_model"],
            "outline_refinement_error": refinement_trace["outline_refinement_error"],
            "outline_refinement_change_count": refinement_trace["outline_refinement_change_count"],
            "evidence_pool_counts": evidence_pool.get("counts", {}),
            "outline_evidence_node_count": len(outline_evidence_map),
            "progressive_planning_completed": True,
        },
    }


# ---------------------------------------------------------------------------
# Scope resource preflight (shared by planner and study-plan persistence)
# ---------------------------------------------------------------------------


# Stable reason codes returned by :func:`preflight_scope_resources`.
_PREFLIGHT_OK = "ok"
_PREFLIGHT_NO_KG_NODES = "no_kg_nodes_usable"
_PREFLIGHT_NO_EVIDENCE_CHUNKS = "no_evidence_chunks_for_scope"
_PREFLIGHT_NO_KG_AND_NO_CHUNKS = "no_kg_nodes_and_no_evidence_chunks"
_PREFLIGHT_EMPTY_SCOPE = "empty_scope"
_PREFLIGHT_UNAVAILABLE = "resource_check_unavailable"
_PREFLIGHT_NOT_VERIFIED = "not_verified"


def _resolve_stable_core_to_raw_nodes(
    kg_node_ids: list[str],
    *,
    document_id: str | None = None,
    chapter_title: str | None = None,
    book_title: str | None = None,
) -> dict[str, Any]:
    """Map stable ``core:*`` IDs to raw ``kg://`` node records.

    Uses the **same** identity rule as the KG producer
    (:func:`backend.services.knowledge_graph.agent._core_id`):
    ``core:<sha1(book_title\\0node_type\\0canonical_name)[:16]>``.

    When *document_id*, *chapter_title*, or *book_title* constraints are
    provided, only raw nodes matching all constraints are returned.  This
    prevents a stable core that appears in multiple chapters from pulling
    in cross-chapter nodes.

    Raw ``kg://`` IDs (those not starting with ``core:``) are passed
    through directly — they are looked up in the KG by their literal
    ``node_id`` and returned when found (subject to the same
    document/chapter filters).

    Parameters
    ----------
    kg_node_ids:
        The list of node IDs to resolve — typically a mix of stable
        ``core:<hex>`` IDs and possibly raw ``kg://...`` IDs.
    document_id:
        Optional document ID constraint.  When provided, only raw nodes
        whose ``metadata.document_id`` matches are included.
    chapter_title:
        Optional chapter title constraint.  When provided, only raw nodes
        with a matching ``chapter`` field are included.  Matching is
        bidirectional: *chapter_title* contains the node's chapter OR
        vice-versa.
    book_title:
        Optional book title constraint.  When provided, only raw nodes
        whose ``book_title`` field matches are included.

    Returns
    -------
    dict
        Keys:

        * ``by_stable`` — ``{stable_id: [raw_node_dict, ...]}``
        * ``raw_node_ids`` — deduplicated list of resolved raw ``node_id``
          strings (both mapped from stable IDs and pass-through raw IDs)
        * ``raw_nodes`` — deduplicated list of resolved raw node dicts
        * ``stable_ids_checked`` — the ``core:*`` subset of *kg_node_ids*
        * ``raw_ids_checked`` — the raw ``kg://`` subset of *kg_node_ids*
        * ``stable_id_matches`` — ``{stable_id: [raw_node_id, ...]}``
    """
    from backend.services.knowledge_graph.agent import (  # noqa: PLC0415
        canonical_book_title,
        stable_core_identity,
    )

    # ------------------------------------------------------------------
    # 1. Separate stable vs raw IDs — reject unknown formats
    # ------------------------------------------------------------------
    stable_ids: list[str] = []
    raw_ids: list[str] = []
    for nid in kg_node_ids:
        nid = str(nid).strip()
        if not nid:
            continue
        if nid.startswith("core:"):
            stable_ids.append(nid)
        elif nid.startswith("kg://"):
            raw_ids.append(nid)
        # Unknown formats are silently dropped — they cannot map to
        # anything because only core:* and kg:// are valid identifiers.

    # Fast-path: no IDs at all
    if not stable_ids and not raw_ids:
        return {
            "by_stable": {},
            "raw_node_ids": [],
            "raw_nodes": [],
            "stable_ids_checked": [],
            "raw_ids_checked": [],
            "stable_id_matches": {},
        }

    # ------------------------------------------------------------------
    # 2. Load KG and index
    # ------------------------------------------------------------------
    try:
        all_nodes = load_kg_nodes()
    except Exception:
        all_nodes = []

    raw_id_index: dict[str, dict[str, Any]] = {}
    for n in all_nodes:
        nid = str(n.get("node_id", "")).strip()
        if nid:
            raw_id_index[nid] = n

    # ------------------------------------------------------------------
    # 3. Build stable→raw mapping using KG producer identity rule
    # ------------------------------------------------------------------
    stable_to_raw: dict[str, list[dict[str, Any]]] = {}
    stable_id_matches: dict[str, list[str]] = {}

    for node in all_nodes:
        bt_raw = str(node.get("book_title", "")).strip()
        nt = str(node.get("node_type", "")).strip()
        cn = str(node.get("canonical_name", "")).strip()
        node_raw_id = str(node.get("node_id", "")).strip()

        if not bt_raw or not nt or not cn or not node_raw_id:
            continue

        # ── Resolve canonical book title via KG producer helper ──
        #     The overview computes stable core IDs from the canonical
        #     short title (e.g. "牙周病学").  Raw nodes may carry a
        #     long-form title (e.g. "牙周病学（孟焕新主编，第5版，
        #     人民卫生出版社）").  We MUST normalise through the same
        #     producer-owned helper so the hashes match.
        meta = node.get("metadata")
        node_doc = (
            str(meta.get("document_id", "")).strip()
            if isinstance(meta, dict) else ""
        )
        doc_for_canonical = document_id or node_doc
        bt_canonical = canonical_book_title(doc_for_canonical, bt_raw)

        # ── Document constraint ────────────────────────────────────
        if document_id:
            if node_doc and node_doc != document_id:
                continue

        # ── Chapter constraint (bidirectional) ─────────────────────
        if chapter_title:
            node_ch = str(node.get("chapter", "")).strip()
            if node_ch and chapter_title and (
                chapter_title not in node_ch and node_ch not in chapter_title
            ):
                continue

        # ── Book title constraint (canonical → canonical) ──────────
        if book_title:
            if bt_canonical != book_title:
                continue

        # ── Compute stable ID via KG producer identity ───────────
        #     Use the CANONICAL title so the hash matches the
        #     overview's stable core IDs.
        stable_id = stable_core_identity(bt_canonical, nt, cn)

        stable_to_raw.setdefault(stable_id, []).append(node)
        stable_id_matches.setdefault(stable_id, []).append(node_raw_id)

    # ------------------------------------------------------------------
    # 4. Resolve and collect
    # ------------------------------------------------------------------
    raw_nodes: list[dict[str, Any]] = []
    raw_node_ids_result: list[str] = []
    seen_raw_ids: set[str] = set()

    for sid in stable_ids:
        for node in stable_to_raw.get(sid, []):
            nid = str(node.get("node_id", "")).strip()
            if nid and nid not in seen_raw_ids:
                seen_raw_ids.add(nid)
                raw_nodes.append(node)
                raw_node_ids_result.append(nid)

    for rid in raw_ids:
        node = raw_id_index.get(rid)
        if node and rid not in seen_raw_ids:
            # ── Apply document/chapter/book constraints to raw IDs too ──
            if document_id:
                meta = node.get("metadata")
                if isinstance(meta, dict):
                    node_doc = str(meta.get("document_id", "")).strip()
                    if node_doc and node_doc != document_id:
                        continue
            if chapter_title:
                node_ch = str(node.get("chapter", "")).strip()
                if node_ch and chapter_title and (
                    chapter_title not in node_ch and node_ch not in chapter_title
                ):
                    continue
            if book_title:
                # Normalise through the same producer helper so the
                # constraint compares canonical→canonical.
                node_bt_raw = str(node.get("book_title", "")).strip()
                if node_bt_raw:
                    node_meta = node.get("metadata")
                    node_doc_raw = (
                        str(node_meta.get("document_id", "")).strip()
                        if isinstance(node_meta, dict) else ""
                    )
                    node_bt_canonical = canonical_book_title(
                        document_id or node_doc_raw, node_bt_raw,
                    )
                    if node_bt_canonical != book_title:
                        continue
            seen_raw_ids.add(rid)
            raw_nodes.append(node)
            raw_node_ids_result.append(rid)

    return {
        "by_stable": stable_to_raw,
        "raw_node_ids": raw_node_ids_result,
        "raw_nodes": raw_nodes,
        "stable_ids_checked": stable_ids,
        "raw_ids_checked": raw_ids,
        "stable_id_matches": stable_id_matches,
    }


def preflight_scope_resources(
    learning_scope: dict[str, Any],
    *,
    check_kg_nodes: bool = True,
    check_evidence_chunks: bool = True,
    evidence_chunks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Check whether *learning_scope* has usable resources — pure, no LLM.

    Uses the **same** KG node availability and document/chapter evidence
    chunk filtering rules as :func:`plan_lesson`.  Does **not** start a
    lesson, create a session, or call any LLM.

    Parameters
    ----------
    learning_scope:
        The formal ``learning_scope`` dict as persisted on a plan draft
        item.  Must carry at least one of ``kg_node_ids``,
        ``document_id``, or ``chapter_key``.
    check_kg_nodes:
        When ``True`` (default), loads ``kg_nodes.jsonl`` and verifies that
        at least one ``kg_node_id`` in *learning_scope* exists as a real
        KG node.
    check_evidence_chunks:
        When ``True`` (default), checks whether the scope's
        ``document_id`` / ``chapter_key`` can produce at least one
        evidence chunk.  When *evidence_chunks* is provided, filters
        it in-process; otherwise calls ``collect_planning_context``
        (which hits the retrieval service — this is NOT an LLM call).
    evidence_chunks:
        Optional pre-retrieved list of evidence chunk dicts (the
        ``"chunks"`` value from :func:`collect_planning_context`).
        When provided AND *check_evidence_chunks* is ``True``, the
        function filters these inline and skips the retrieval call.

    Returns
    -------
    dict
        A structured result with keys:

        * ``usable`` — ``True`` when at least one real resource exists
        * ``reason_code`` — stable machine-readable code (see module-level
          ``_PREFLIGHT_*`` constants)
        * ``kg_node_hit_count`` — how many scope KG node IDs exist in the
          real KG
        * ``kg_node_ids_checked`` — the canonical list of KG node IDs
          checked (from *learning_scope*)
        * ``kg_node_ids_usable`` — the subset that actually exist in the KG
        * ``evidence_chunk_count`` — number of matching evidence chunks
          (0 when *check_evidence_chunks* is ``False``)
        * ``scope_diagnostics`` — dict with ``has_kg_node_ids``,
          ``has_document_id``, ``has_chapter_key``, ``title``,
          ``book_title``, ``document_id``, ``chapter_key``,
          ``checks_performed``
        * ``error_message`` — human-readable explanation when
          ``usable`` is ``False``; ``None`` otherwise
    """
    # ------------------------------------------------------------------
    # 1. Parse scope fields (same rules as plan_lesson lines 2652-2666)
    # ------------------------------------------------------------------
    scope_kg_node_ids: list[str] = []
    scope_document_id: str | None = None
    scope_chapter: str | None = None
    scope_title: str = ""
    scope_book_title: str = ""

    if learning_scope and isinstance(learning_scope, dict):
        raw_ids = learning_scope.get("kg_node_ids")
        if raw_ids and isinstance(raw_ids, list):
            scope_kg_node_ids = [
                str(sid).strip() for sid in raw_ids if str(sid).strip()
            ]
        did = str(learning_scope.get("document_id", "")).strip()
        if did:
            scope_document_id = did
        ch = str(
            learning_scope.get("chapter_key", "")
            or learning_scope.get("chapter_title", "")
        ).strip()
        if ch:
            scope_chapter = ch
        scope_title = str(learning_scope.get("chapter_title", "")).strip()
        scope_book_title = str(learning_scope.get("book_title", "")).strip()

    # ------------------------------------------------------------------
    # 2. Empty scope short-circuit
    # ------------------------------------------------------------------
    has_kg_ids = bool(scope_kg_node_ids)
    has_doc = bool(scope_document_id)
    has_ch = bool(scope_chapter)

    if not has_kg_ids and not has_doc and not has_ch:
        return {
            "usable": False,
            "reason_code": _PREFLIGHT_EMPTY_SCOPE,
            "kg_node_hit_count": 0,
            "kg_node_ids_checked": [],
            "kg_node_ids_usable": [],
            "evidence_chunk_count": 0,
            "scope_diagnostics": {
                "has_kg_node_ids": False,
                "has_document_id": False,
                "has_chapter_key": False,
                "title": scope_title,
                "book_title": scope_book_title,
                "document_id": scope_document_id or "",
                "chapter_key": scope_chapter or "",
                "checks_performed": [],
            },
            "error_message": (
                "The learning scope is empty — it has no kg_node_ids, "
                "document_id, or chapter_key.  A usable scope must carry "
                "at least one resource identifier."
            ),
        }

    # ------------------------------------------------------------------
    # 3. KG node availability check (reads kg_nodes.jsonl)
    # ------------------------------------------------------------------
    kg_node_hit_count = 0
    kg_node_ids_usable: list[str] = []
    checks_performed: list[str] = []
    # Track whether each check was *conclusive* or ran in a degraded
    # environment (empty KG, retrieval unavailable, etc.).
    kg_check_conclusive: bool = False
    evidence_check_conclusive: bool = False

    if check_kg_nodes and has_kg_ids:
        checks_performed.append("kg_nodes")
        try:
            mapping = _resolve_stable_core_to_raw_nodes(
                scope_kg_node_ids,
                document_id=scope_document_id,
                chapter_title=scope_title or scope_chapter,
                book_title=scope_book_title,
            )
            if mapping["raw_node_ids"]:
                kg_check_conclusive = True
                kg_node_hit_count = len(mapping["raw_node_ids"])
                # Return the original requested IDs that resolved successfully
                # — preserving stable core:* format (not replacing with kg://).
                kg_node_ids_usable = [
                    sid for sid in scope_kg_node_ids
                    if sid in mapping["stable_id_matches"]
                ]
                # Also include any raw pass-through IDs that resolved
                for rid in mapping["raw_ids_checked"]:
                    if rid not in kg_node_ids_usable:
                        kg_node_ids_usable.append(rid)
            elif load_kg_nodes():
                # KG file exists and was readable, but no match
                kg_check_conclusive = True
                # kg_node_hit_count stays 0, usable stays empty
        except Exception:
            # File missing / unreadable → check is inconclusive
            pass

    # ------------------------------------------------------------------
    # 4. Evidence chunk check
    # ------------------------------------------------------------------
    evidence_chunk_count = 0
    evidence_retrieval_ok: bool | None = None

    if check_evidence_chunks and (has_doc or has_ch):
        checks_performed.append("evidence_chunks")
        if evidence_chunks is not None:
            # Filter pre-supplied chunks inline (same rules as plan_lesson
            # lines 2672-2687).  Pre-supplied chunks are always conclusive
            # — either they match or they don't.
            evidence_check_conclusive = True
            chunks_to_check = list(evidence_chunks)
        else:
            # Call retrieval — this is NOT an LLM call; it hits the
            # search_knowledge_base vector index.  Use the chapter title
            # or book title as the search topic.
            _topic = scope_title or scope_book_title or "口腔医学"
            _ctx = collect_planning_context(_topic, top_k=8)
            evidence_retrieval_ok = _ctx.get("ok", False)
            chunks_to_check = list(_ctx.get("chunks", []))
            # The check is conclusive only when retrieval succeeded.
            # If retrieval failed / returned zero chunks because the
            # service is unavailable, we cannot verify evidence chunks.
            evidence_check_conclusive = bool(evidence_retrieval_ok)

        for ch in chunks_to_check:
            if has_doc:
                ch_doc_id = str(
                    ch.get("path", "")
                    or ch.get("metadata", {}).get("document_id", "")
                )
                if scope_document_id and ch_doc_id != scope_document_id:
                    continue
            if has_ch:
                ch_chapter = str(ch.get("metadata", {}).get("chapter", ""))
                if scope_chapter and (
                    scope_chapter not in ch_chapter
                    and ch_chapter not in scope_chapter
                ):
                    continue
            evidence_chunk_count += 1

    # ------------------------------------------------------------------
    # 5. Usability determination (FAIL-CLOSED)
    # ------------------------------------------------------------------
    # A scope is usable ONLY when at least one conclusive check PASSED.
    # When ALL checks are inconclusive (degraded environment — KG file
    # missing/empty, retrieval unavailable, all checks disabled) the
    # scope is NOT usable.  "Cannot prove valid" ≠ valid.
    has_kg = kg_node_hit_count > 0
    has_evidence = evidence_chunk_count > 0
    kg_failed_conclusively = kg_check_conclusive and not has_kg
    evidence_failed_conclusively = evidence_check_conclusive and not has_evidence
    any_conclusive_pass = (
        (kg_check_conclusive and has_kg)
        or (evidence_check_conclusive and has_evidence)
    )
    all_inconclusive = not kg_check_conclusive and not evidence_check_conclusive

    if any_conclusive_pass:
        return {
            "usable": True,
            "reason_code": _PREFLIGHT_OK,
            "kg_node_hit_count": kg_node_hit_count,
            "kg_node_ids_checked": scope_kg_node_ids,
            "kg_node_ids_usable": kg_node_ids_usable,
            "evidence_chunk_count": evidence_chunk_count,
            "scope_diagnostics": {
                "has_kg_node_ids": has_kg_ids,
                "has_document_id": has_doc,
                "has_chapter_key": has_ch,
                "title": scope_title,
                "book_title": scope_book_title,
                "document_id": scope_document_id or "",
                "chapter_key": scope_chapter or "",
                "checks_performed": checks_performed,
            },
            "error_message": None,
        }

    # --- NOT usable — build a specific error message. -------------------
    # 5a. No checks were enabled at all — FAIL-CLOSED.
    #     A scope that has NOT been verified by ANY resource check is NOT
    #     usable.  The upstream scope-resolution pipeline may have validated
    #     chapter records, but that is a different gate than execution-time
    #     resource availability.  Every persistence path must prove that KG
    #     nodes and/or evidence chunks are actually usable NOW.
    if not checks_performed:
        return {
            "usable": False,
            "reason_code": _PREFLIGHT_NOT_VERIFIED,
            "kg_node_hit_count": 0,
            "kg_node_ids_checked": scope_kg_node_ids,
            "kg_node_ids_usable": [],
            "evidence_chunk_count": 0,
            "scope_diagnostics": {
                "has_kg_node_ids": has_kg_ids,
                "has_document_id": has_doc,
                "has_chapter_key": has_ch,
                "title": scope_title,
                "book_title": scope_book_title,
                "document_id": scope_document_id or "",
                "chapter_key": scope_chapter or "",
                "checks_performed": checks_performed,
                "note": (
                    "No resource checks were performed — scope cannot be "
                    "verified as usable."
                ),
            },
            "error_message": (
                f"Learning scope \"{scope_title or scope_book_title}\" "
                f"was not verified — no resource checks were performed.  "
                f"The scope cannot be executed until resources are verified."
            ),
        }

    # 5b. All checks inconclusive → resource check unavailable
    if all_inconclusive:
        unavailable_details: list[str] = []
        if check_kg_nodes and has_kg_ids and not kg_check_conclusive:
            unavailable_details.append(
                "KG nodes could not be verified (KG file missing, empty, "
                "or unreadable)"
            )
        if check_evidence_chunks and (has_doc or has_ch) and not evidence_check_conclusive:
            unavailable_details.append(
                "evidence chunks could not be verified (retrieval "
                "service unavailable or returned no data)"
            )
        if not unavailable_details and not checks_performed:
            unavailable_details.append(
                "no resource checks were performed (all checks disabled)"
            )
        detail = "; ".join(unavailable_details) if unavailable_details else (
            "resource verification could not be completed"
        )
        return {
            "usable": False,
            "reason_code": _PREFLIGHT_UNAVAILABLE,
            "kg_node_hit_count": 0,
            "kg_node_ids_checked": scope_kg_node_ids,
            "kg_node_ids_usable": [],
            "evidence_chunk_count": 0,
            "scope_diagnostics": {
                "has_kg_node_ids": has_kg_ids,
                "has_document_id": has_doc,
                "has_chapter_key": has_ch,
                "title": scope_title,
                "book_title": scope_book_title,
                "document_id": scope_document_id or "",
                "chapter_key": scope_chapter or "",
                "checks_performed": checks_performed,
                "note": f"Resource check unavailable: {detail}",
            },
            "error_message": (
                f"Learning scope \"{scope_title or scope_book_title}\" "
                f"could not be verified — {detail}.  "
                f"The scope cannot be executed until resources are available."
            ),
        }

    # 5c. At least one conclusive check FAILED → specific reason
    if kg_failed_conclusively and evidence_failed_conclusively:
        reason = _PREFLIGHT_NO_KG_AND_NO_CHUNKS
        doc_info = f"document_id={scope_document_id!r}" if has_doc else ""
        ch_info = f"chapter={scope_chapter!r}" if has_ch else ""
        filter_desc = " / ".join(p for p in (doc_info, ch_info) if p)
        msg = (
            f"Learning scope \"{scope_title or scope_book_title}\" has "
            f"no usable KG nodes and no matching evidence chunks for "
            f"the specified filters ({filter_desc}).  The scope cannot "
            f"be executed."
        )
    elif kg_failed_conclusively:
        reason = _PREFLIGHT_NO_KG_NODES
        msg = (
            f"Learning scope \"{scope_title or scope_book_title}\" has "
            f"{len(scope_kg_node_ids)} KG node ID(s) but none exist in "
            f"the knowledge graph.  The scope cannot be executed."
        )
    else:
        reason = _PREFLIGHT_NO_EVIDENCE_CHUNKS
        doc_info = f"document_id={scope_document_id!r}" if has_doc else ""
        ch_info = f"chapter={scope_chapter!r}" if has_ch else ""
        filter_desc = " / ".join(p for p in (doc_info, ch_info) if p)
        msg = (
            f"Learning scope \"{scope_title or scope_book_title}\" has "
            f"no matching evidence chunks for the specified filters "
            f"({filter_desc})."
        )

    return {
        "usable": False,
        "reason_code": reason,
        "kg_node_hit_count": kg_node_hit_count,
        "kg_node_ids_checked": scope_kg_node_ids,
        "kg_node_ids_usable": kg_node_ids_usable,
        "evidence_chunk_count": evidence_chunk_count,
        "scope_diagnostics": {
            "has_kg_node_ids": has_kg_ids,
            "has_document_id": has_doc,
            "has_chapter_key": has_ch,
            "title": scope_title,
            "book_title": scope_book_title,
            "document_id": scope_document_id or "",
            "chapter_key": scope_chapter or "",
            "checks_performed": checks_performed,
        },
        "error_message": msg,
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def plan_lesson(
    topic: str,
    user_profile: dict[str, Any] | None = None,
    max_nodes: int = 6,
    learning_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate a lesson outline and mindmap for *topic*.

    Strategy: progressive_llm_agent_planner (first full node + later titles)
    > retrieval_enhanced fallback > kag_node_match fallback > fallback_outline.

    The synchronous path is intentionally progressive: it prepares enough
    outline/evidence for the first section to start teaching immediately. The
    orchestrator completes the remaining evidence-backed outline in the
    background while the first section is being taught.

    When *learning_scope* is provided it constrains KG node selection and
    evidence chunk retrieval to the specified document/chapter/KG nodes.
    If the scope yields zero usable resources, ``ok=False`` is returned
    with a clear ``scope_error`` — the caller must NOT fall back to global
    (unscoped) planning.
    """
    topic = topic.strip() or "口腔医学主题"
    user_profile = user_profile or {}

    # --- pre: resolve scope constraints -----------------------------------
    scope_kg_node_ids: set[str] | None = None
    scope_document_id: str | None = None
    scope_chapter: str | None = None
    if learning_scope and isinstance(learning_scope, dict) and learning_scope:
        raw_ids = learning_scope.get("kg_node_ids")
        if raw_ids and isinstance(raw_ids, list):
            scope_kg_node_ids = {str(sid).strip() for sid in raw_ids if str(sid).strip()}
            if not scope_kg_node_ids:
                scope_kg_node_ids = None
        did = str(learning_scope.get("document_id", "")).strip()
        if did:
            scope_document_id = did
        ch = str(learning_scope.get("chapter_key", "") or learning_scope.get("chapter_title", "")).strip()
        if ch:
            scope_chapter = ch

    # --- 1. contextual retrieval -----------------------------------------
    planning_context = collect_planning_context(topic, top_k=16)

    # --- 1a. apply scope document_id / chapter filter to evidence chunks --
    if scope_document_id or scope_chapter:
        filtered_chunks: list[dict[str, Any]] = []
        for ch in planning_context.get("chunks", []):
            if scope_document_id:
                ch_doc_id = str(ch.get("path", "") or ch.get("metadata", {}).get("document_id", ""))
                if ch_doc_id != scope_document_id:
                    continue
            if scope_chapter:
                ch_chapter = str(ch.get("metadata", {}).get("chapter", ""))
                if scope_chapter not in ch_chapter and ch_chapter not in scope_chapter:
                    continue
            filtered_chunks.append(ch)
        planning_context["chunks"] = filtered_chunks
        planning_context["scope_filtered"] = True
        planning_context["scope_document_id"] = scope_document_id
        planning_context["scope_chapter"] = scope_chapter

    # --- 2. find & dedupe KG nodes ---------------------------------------
    raw_nodes = find_relevant_nodes(topic, max_nodes=max_nodes * 2)
    relevant_nodes = dedupe_nodes(raw_nodes, topic, max_nodes=max_nodes)

    # --- 2b. scope preflight: unified resource check with preflight_scope_resources
    #     plan_lesson and the plan-draft preflight MUST consume the SAME
    #     preflight result — no dual-rule (existence vs topic top-N).
    if learning_scope and isinstance(learning_scope, dict) and learning_scope:
        scope_preflight = preflight_scope_resources(
            learning_scope,
            check_kg_nodes=True,     # use the SAME KG existence check
            check_evidence_chunks=True,
            evidence_chunks=planning_context.get("chunks", []),
        )

        if not scope_preflight["usable"]:
            return {
                "ok": False,
                "topic": topic,
                "error": "scope_no_resources",
                "scope_error": scope_preflight["error_message"] or (
                    "The learning scope has no usable resources — planning "
                    "cannot proceed. The caller must NOT fall back to global "
                    "(unscoped) planning."
                ),
                "scope_preflight": scope_preflight,
                "learning_scope": learning_scope,
                "outline": {"nodes": []},
                "mindmap": {},
                "matched_nodes": [],
                "related_edges": [],
                "linked_questions": [],
                "evidence_pool": {"counts": {}},
                "outline_evidence_map": {},
                "planning_context": planning_context,
                "progressive_planning": {"enabled": False, "completion_required": False},
                "planning_trace": {"strategy": "scope_preflight_failed"},
                "source_trace": {
                    "strategy": "scope_preflight_failed",
                    "planning_context_source": planning_context.get("source", ""),
                    "planning_context_ok": planning_context.get("ok", False),
                    "evidence_chunk_count": len(planning_context.get("chunks", [])),
                    "matched_node_count": 0,
                    "progressive_planning": {"enabled": False, "completion_required": False},
                },
            }

        # --- 2a. apply scope kg_node_ids using preflight's verified IDs ----
        #     Ensure every scope KG node that exists in the real KG is
        #     included in relevant_nodes — not just topic-scored top-N.
        #     Uses the SAME mapping helper as preflight to resolve stable
        #     core:* IDs to raw kg:// nodes.
        scope_usable_kg_ids: set[str] = set(scope_preflight["kg_node_ids_usable"])
        if scope_kg_node_ids is not None:
            try:
                _scope_chapter_title = (
                    str(learning_scope.get("chapter_title", "")).strip()
                    if isinstance(learning_scope, dict) else ""
                )
                _scope_book_title = (
                    str(learning_scope.get("book_title", "")).strip()
                    if isinstance(learning_scope, dict) else ""
                )
                mapping = _resolve_stable_core_to_raw_nodes(
                    list(scope_kg_node_ids),
                    document_id=scope_document_id,
                    chapter_title=_scope_chapter_title or scope_chapter,
                    book_title=_scope_book_title,
                )
                scope_direct_nodes = list(mapping["raw_nodes"])
                # Merge: scope-direct nodes first, then topic-scored nodes
                # that also fall within the scope (by raw node_id match).
                existing_ids = {n.get("node_id") for n in scope_direct_nodes}
                resolved_raw_ids = set(mapping["raw_node_ids"])
                for n in relevant_nodes:
                    if n.get("node_id", "") in resolved_raw_ids and n.get("node_id") not in existing_ids:
                        scope_direct_nodes.append(n)
                        existing_ids.add(n.get("node_id"))
                relevant_nodes = scope_direct_nodes
            except Exception:
                # KG unavailable — scope_preflight already caught this above.
                # Fall back to topic-scored filter as best-effort.
                relevant_nodes = [
                    n for n in relevant_nodes
                    if n.get("node_id", "") in scope_kg_node_ids
                ]

    # --- 3. edges & questions (always based on relevant_nodes) -----------
    if relevant_nodes:
        node_ids = {n["node_id"] for n in relevant_nodes}
        edges = load_kg_edges_for_nodes(node_ids)
        questions = load_questions_for_nodes(node_ids)
    else:
        edges = []
        questions = []

    # --- 4. build evidence pool (shared by agent & fallback paths) -------
    evidence_pool = build_planning_evidence_pool(
        topic=topic,
        planning_context=planning_context,
        relevant_nodes=relevant_nodes,
        edges=edges,
        questions=questions,
    )

    # --- 5. try progressive Agent Planner first --------------------------
    agent_outline, agent_evidence_map, agent_trace = plan_lesson_initial_agent(
        topic=topic,
        user_profile=user_profile,
        evidence_pool=evidence_pool,
        planning_context=planning_context,
        max_nodes=max_nodes,
    )

    if agent_outline is not None and agent_evidence_map is not None:
        # Agent Planner succeeded — this is the main path
        outline = agent_outline
        outline_evidence_map = agent_evidence_map
        planning_trace = agent_trace
        strategy = "progressive_llm_agent_planner"
        refinement_trace: dict[str, Any] = _empty_refinement_trace(
            "skipped_progressive_planner_active"
        )
        progressive_planning: dict[str, Any] = {
            "enabled": True,
            "completion_required": True,
            "first_node_id": (
                outline.get("nodes", [{}])[0].get("id", "")
                if outline.get("nodes")
                else ""
            ),
            "initial_strategy": strategy,
            "title_only_node_count": max(0, len(outline.get("nodes", [])) - 1),
        }
    else:
        # --- 6. FALLBACK: old dimension-based path -----------------------
        fallback_reason = agent_trace.get("fallback_reason", "agent_planner_failed")
        dimensions = infer_learning_dimensions(planning_context["chunks"])

        if dimensions:
            outline = build_outline_from_dimensions(topic, dimensions, relevant_nodes)
            strategy = "retrieval_enhanced"
        elif relevant_nodes:
            outline = build_outline_from_nodes(topic, relevant_nodes, edges, questions)
            strategy = "kag_node_match"
        else:
            outline = build_fallback_outline(topic, max_nodes=max_nodes)
            strategy = "fallback_outline"

        # Keep synchronous fallback fast; background completion can refine later.
        refinement_trace = _empty_refinement_trace(
            "skipped_fast_progressive_fallback"
        )

        # Build evidence map using fallback matching logic
        outline_evidence_map = build_outline_evidence_map(
            outline=outline,
            evidence_pool=evidence_pool,
        )

        # Compute OEM counts for fallback trace
        _oem_total_nodes = len(outline_evidence_map)
        _oem_total_kg_nodes = sum(
            len(entry["kg_nodes"]) for entry in outline_evidence_map.values()
        )
        _oem_total_kg_edges = sum(
            len(entry["kg_edges"]) for entry in outline_evidence_map.values()
        )
        _oem_total_concepts = sum(
            len(entry["concepts"]) for entry in outline_evidence_map.values()
        )
        _oem_total_questions = sum(
            len(entry["questions"]) for entry in outline_evidence_map.values()
        )
        _oem_total_chunks = sum(
            len(entry["evidence_chunks"]) for entry in outline_evidence_map.values()
        )
        oem_counts = {
            "outline_nodes": _oem_total_nodes,
            "total_kg_nodes_bound": _oem_total_kg_nodes,
            "total_kg_edges_bound": _oem_total_kg_edges,
            "total_concepts_bound": _oem_total_concepts,
            "total_questions_bound": _oem_total_questions,
            "total_evidence_chunks_bound": _oem_total_chunks,
        }

        planning_trace = {
            "strategy": strategy,
            "is_agent_planned": False,
            "fallback_used": True,
            "fallback_reason": fallback_reason,
            "evidence_pool_counts": evidence_pool["counts"],
            "outline_evidence_map_counts": oem_counts,
            # Preserve agent planner diagnostics for audit
            "agent_planner_prompt_chars": agent_trace.get(
                "agent_planner_prompt_chars", 0
            ),
            "agent_planner_raw_response_chars": agent_trace.get(
                "agent_planner_raw_response_chars", 0
            ),
            "agent_planner_raw_response_head": agent_trace.get(
                "agent_planner_raw_response_head"
            ),
            "agent_planner_raw_response_tail": agent_trace.get(
                "agent_planner_raw_response_tail"
            ),
            "agent_planner_json_error": agent_trace.get(
                "agent_planner_json_error"
            ),
            "json_mode_used": agent_trace.get("json_mode_used", False),
            "validation_errors": agent_trace.get("validation_errors", []),
            "coverage_warnings": [],
            "sequence_strategy": "",
        }
        progressive_planning = {
            "enabled": False,
            "completion_required": False,
            "fallback_reason": fallback_reason,
        }

    # --- 7. mindmap ------------------------------------------------------
    mindmap = build_mindmap(outline)
    kag_dir = resolve_kag_dir()

    return {
        "ok": True,
        "topic": topic,
        "outline": outline,
        "mindmap": mindmap,
        "matched_nodes": relevant_nodes,
        "related_edges": edges,
        "linked_questions": questions,
        "outline_refinement_trace": refinement_trace,
        "evidence_pool": evidence_pool,
        "outline_evidence_map": outline_evidence_map,
        "planning_context": planning_context,
        "progressive_planning": progressive_planning,
        "planning_trace": planning_trace,
        "source_trace": {
            "strategy": strategy,
            "planning_context_source": planning_context["source"],
            "planning_context_ok": planning_context["ok"],
            "planning_context_warning_count": len(planning_context["warnings"]),
            "evidence_chunk_count": len(planning_context["chunks"]),
            "dimension_count": len(
                infer_learning_dimensions(planning_context["chunks"])
            ),
            "matched_node_count": len(relevant_nodes),
            "edge_count": len(edges),
            "question_count": len(questions),
            "kag_dir": str(kag_dir),
            "user_profile": user_profile,
            "outline_refinement": refinement_trace["outline_refinement"],
            "outline_refinement_model": refinement_trace["outline_refinement_model"],
            "outline_refinement_error": refinement_trace["outline_refinement_error"],
            "outline_refinement_change_count": refinement_trace["outline_refinement_change_count"],
            "evidence_pool_counts": evidence_pool["counts"],
            "outline_evidence_node_count": len(outline_evidence_map),
            "progressive_planning": progressive_planning,
        },
    }
