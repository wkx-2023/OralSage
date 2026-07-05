"""Resource Factory — generates learning resource files from a Planner outline.

Produces six resource files under ``data/learning/sessions/{session_id}/``:

* ``handout.md``          — Chinese markdown lecture handout (skeleton)
* ``slides.json``         — slide deck (title + one content slide per node)
* ``base_questions.json`` — MVP single-choice questions (one per node)
* ``reading_list.json``   — reading list derived from node source_refs
* ``mindmap.json``        — mindmap passed through from the planner
* ``pptx_status.json``    — PPTX generation status (placeholder in MVP)

No LLM.  No Tutor / QA / Assessment.  No video.  No new dependencies.
"""

from __future__ import annotations

import json
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any


# ============================================================================
# Public entry point
# ============================================================================


def generate_resources(
    session_id: str,
    topic: str,
    outline: dict[str, Any],
    mindmap: dict[str, Any],
    output_root: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Generate all learning resource files and write them to disk.

    Parameters
    ----------
    session_id:
        Session identifier used as the sub-directory name.
    topic:
        The lesson topic (e.g. ``"牙周炎"``).
    outline:
        A ``LessonOutline``-compatible dict as returned by :func:`plan_lesson`.
    mindmap:
        A ``markmap_json``-format mindmap dict.
    output_root:
        Root directory for session output.  Defaults to
        ``data/learning/sessions`` (relative to CWD).

    Returns
    -------
    list[dict]
        One entry per generated resource, each with keys:
        ``resource_type``, ``resource_path``, ``resource_json``, ``status``.
    """
    root = Path(output_root) if output_root else Path("data/learning/sessions")
    session_dir = root / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    resources: list[dict[str, Any]] = []

    # -- handout.md ---------------------------------------------------------
    handout_md = build_handout(topic, outline)
    handout_path = session_dir / "handout.md"
    handout_path.write_text(handout_md, encoding="utf-8")
    resources.append({
        "resource_type": "handout_md",
        "resource_path": str(handout_path),
        "resource_json": {},
        "status": "ok",
    })

    # -- slides.json --------------------------------------------------------
    slides = build_slides(topic, outline)
    slides_path = session_dir / "slides.json"
    slides_path.write_text(
        json.dumps(slides, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    resources.append({
        "resource_type": "slides_json",
        "resource_path": str(slides_path),
        "resource_json": slides,
        "status": "ok",
    })

    # -- base_questions.json ------------------------------------------------
    questions = build_base_questions(topic, outline)
    q_path = session_dir / "base_questions.json"
    q_path.write_text(
        json.dumps(questions, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    resources.append({
        "resource_type": "base_questions_json",
        "resource_path": str(q_path),
        "resource_json": questions,
        "status": "ok",
    })

    # -- reading_list.json --------------------------------------------------
    reading = build_reading_list(topic, outline)
    r_path = session_dir / "reading_list.json"
    r_path.write_text(
        json.dumps(reading, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    resources.append({
        "resource_type": "reading_list_json",
        "resource_path": str(r_path),
        "resource_json": reading,
        "status": "ok",
    })

    # -- mindmap.json -------------------------------------------------------
    mm_path = session_dir / "mindmap.json"
    mm_path.write_text(
        json.dumps(mindmap, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    resources.append({
        "resource_type": "mindmap_json",
        "resource_path": str(mm_path),
        "resource_json": mindmap,
        "status": "ok",
    })

    # -- pptx_status.json ---------------------------------------------------
    pptx = build_pptx_status(slides_path)
    pptx_path = session_dir / "pptx_status.json"
    pptx_path.write_text(
        json.dumps(pptx, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    resources.append({
        "resource_type": "pptx_status_json",
        "resource_path": str(pptx_path),
        "resource_json": pptx,
        "status": "ok",
    })

    return resources


# ============================================================================
# Resource builders
# ============================================================================


def build_handout(topic: str, outline: dict[str, Any]) -> str:
    """Generate a Chinese markdown lecture handout (skeleton).

    Does **not** invent medical content — only renders the outline structure.
    """
    lines: list[str] = []
    title = outline.get("title", f"{topic}系统学习路径")
    nodes: list[dict[str, Any]] = outline.get("nodes", [])

    # Title
    lines.append(f"# {title}")
    lines.append("")

    # 课程目标
    lines.append("## 课程目标")
    lines.append("")
    for node in nodes:
        for obj in node.get("learning_objectives", []):
            lines.append(f"- {obj}")
    lines.append("")

    # 学习路径
    lines.append("## 学习路径")
    lines.append("")

    for i, node in enumerate(nodes, start=1):
        node_title = node.get("title", f"节点{i}")
        lines.append(f"### {i}. {node_title}")
        lines.append("")

        # 学习目标
        objectives: list[str] = node.get("learning_objectives", [])
        if objectives:
            lines.append("**学习目标：**")
            lines.append("")
            for obj in objectives:
                lines.append(f"- {obj}")
            lines.append("")

        # 证据来源
        lines.append("**证据来源：**")
        lines.append("")
        source_refs: list[dict[str, Any]] = node.get("source_refs", [])
        if source_refs:
            for ref in source_refs:
                src = ref.get("source", "")
                ref_title = ref.get("title", "")
                stype = ref.get("source_type", "")
                path = ref.get("path", "")
                lines.append(f"- **来源**: {src}  ")
                if ref_title:
                    lines.append(f"  **标题**: {ref_title}  ")
                if stype:
                    lines.append(f"  **类型**: {stype}  ")
                if path:
                    lines.append(f"  **路径**: {path}  ")
                lines.append("")
        else:
            lines.append("暂无明确证据来源")
            lines.append("")

        # Estimated time
        est = node.get("estimated_minutes", 0)
        if est:
            lines.append(f"*预计学习时间：{est} 分钟*")
            lines.append("")

    # Footer
    lines.append("---")
    lines.append("")
    lines.append("*本讲义由 OralSage Resource Factory 自动生成，为课程讲义草稿，不是完整医学教材。*")

    return "\n".join(lines)


def build_slides(topic: str, outline: dict[str, Any]) -> dict[str, Any]:
    """Build a ``slides.json`` dict from the outline.

    One title slide + one content slide per outline node.
    """
    outline_title = outline.get("title", f"{topic}系统学习路径")
    nodes: list[dict[str, Any]] = outline.get("nodes", [])

    slides: list[dict[str, Any]] = []

    # Title slide
    slides.append({
        "type": "title",
        "title": outline_title,
        "subtitle": "OralSage 个性化学习课程",
    })

    # Content slides — one per node
    for node in nodes:
        node_title = node.get("title", "")
        objectives: list[str] = node.get("learning_objectives", [])
        source_refs: list[dict[str, Any]] = node.get("source_refs", [])

        # bullets from learning_objectives, max 4
        bullets = objectives[:4]

        # speaker_notes — one Chinese sentence
        speaker_notes = (
            f"本页围绕{node_title}展开，"
            f"课堂讲解时结合用户掌握情况补充案例和小测。"
        )

        slides.append({
            "type": "content",
            "title": node_title,
            "bullets": bullets,
            "speaker_notes": speaker_notes,
            "source_refs": source_refs,
        })

    return {
        "title": outline_title,
        "slides": slides,
    }


def build_base_questions(topic: str, outline: dict[str, Any]) -> dict[str, Any]:
    """Generate MVP single-choice questions (one per outline node).

    These are **not** real exam questions — they are lightweight checks
    for the subsequent Assessment module to validate the flow.
    """
    nodes: list[dict[str, Any]] = outline.get("nodes", [])

    questions: list[dict[str, Any]] = []
    for i, node in enumerate(nodes, start=1):
        node_id = node.get("id", f"node_{i}")
        node_title = node.get("title", "")
        kg_node_ids: list[str] = node.get("kg_node_ids", [])
        difficulty = node.get("difficulty", "basic")
        source_refs: list[dict[str, Any]] = node.get("source_refs", [])

        stem = "关于“{}”，本节最重要的学习任务是？".format(node_title)

        question = {
            "id": f"q_{i}",
            "type": "single_choice",
            "outline_node_id": node_id,
            "kg_node_ids": kg_node_ids,
            "stem": stem,
            "options": [
                "A. 只记住名词，不理解关系",
                "B. 理解核心概念、证据来源和临床意义",
                "C. 跳过基础知识直接治疗",
                "D. 只看题目答案",
            ],
            "answer": "B",
            "explanation": f"本节目标是理解核心概念、证据来源和临床意义。",
            "difficulty": difficulty,
            "source_refs": source_refs,
        }
        questions.append(question)

    return {"questions": questions}


def build_reading_list(topic: str, outline: dict[str, Any]) -> dict[str, Any]:
    """Build a reading list from the source_refs on each outline node."""
    nodes: list[dict[str, Any]] = outline.get("nodes", [])
    items: list[dict[str, Any]] = []

    for node in nodes:
        node_id = node.get("id", "")
        node_title = node.get("title", "")
        source_refs: list[dict[str, Any]] = node.get("source_refs", [])

        items.append({
            "outline_node_id": node_id,
            "title": node_title,
            "source_refs": source_refs,
            "note": "建议学习本节前后对照查看。",
        })

    return {"items": items}


def build_pptx_status(slides_path: Path) -> dict[str, Any]:
    """Return a placeholder PPTX status — no real PPTX generation in MVP.

    Parameters
    ----------
    slides_path:
        Path to the ``slides.json`` that would be the source for PPTX export.
    """
    return {
        "ok": False,
        "status": "not_implemented",
        "message": (
            "MVP 当前先生成 slides.json，"
            "PPTX 导出将在后续接入 PptxGenJS。"
        ),
        "slides_path": str(slides_path),
    }


# ============================================================================
# Handout Section Agent — per-node handout section generation
# ============================================================================

# LLM config key priority for handout section generation
_HANDOUT_API_KEY_NAMES = (
    "LEARNING_HANDOUT_LLM_API_KEY",
    "LEARNING_RESOURCE_LLM_API_KEY",
    "LEARNING_LLM_API_KEY",
    "QWEN_API_KEY",
    "DASHSCOPE_API_KEY",
)
_HANDOUT_BASE_URL_NAMES = (
    "LEARNING_HANDOUT_LLM_BASE_URL",
    "LEARNING_RESOURCE_LLM_BASE_URL",
    "LEARNING_LLM_BASE_URL",
    "QWEN_BASE_URL",
    "DASHSCOPE_BASE_URL",
)
_HANDOUT_MODEL_NAMES = (
    "LEARNING_HANDOUT_LLM_MODEL",
    "LEARNING_RESOURCE_LLM_MODEL",
    "LEARNING_LLM_MODEL",
    "QWEN_MODEL",
    "DASHSCOPE_MODEL",
)


def get_evidence_detail(
    node_evidence: dict,
    requested_chunk_ids: list[str],
    requested_kg_node_ids: list[str],
    requested_kg_edge_ids: list[str],
    requested_question_ids: list[str],
    evidence_pool: dict | None = None,
    max_chars_per_chunk: int = 800,
) -> dict:
    """Read-only helper: fetch evidence detail from *node_evidence*.

    Only retrieves content that already exists in *node_evidence* (or
    *evidence_pool* as fallback).  Does NOT judge, supplement, or generate
    content.

    Parameters
    ----------
    node_evidence:
        The per-node evidence bundle from ``outline_evidence_map[node_id]``.
    requested_*_ids:
        The ids the LLM requested in Round 1.
    evidence_pool:
        Optional global evidence pool for fallback lookups.
    max_chars_per_chunk:
        Max characters per chunk text (default 800).

    Returns
    -------
    dict with keys: ``chunks``, ``kg_nodes``, ``kg_edges``, ``questions``.
    Each value is a list of lightweight dicts with fuller content.
    """
    # --- index helpers from node_evidence ---
    node_chunks: dict[str, dict] = {
        ch.get("chunk_id", ""): ch
        for ch in node_evidence.get("evidence_chunks", [])
    }
    node_kg_nodes: dict[str, dict] = {
        n.get("node_id", ""): n
        for n in node_evidence.get("kg_nodes", [])
    }
    node_edges: dict[str, dict] = {
        e.get("edge_id", ""): e
        for e in node_evidence.get("kg_edges", [])
    }
    node_questions: dict[str, dict] = {
        q.get("question_id", ""): q
        for q in node_evidence.get("questions", [])
    }

    # Fallback indexes from evidence_pool
    pool_chunks: dict[str, dict] = {}
    pool_kg_nodes: dict[str, dict] = {}
    pool_edges: dict[str, dict] = {}
    pool_questions: dict[str, dict] = {}
    if evidence_pool:
        pool_chunks = {
            ch.get("chunk_id", ""): ch
            for ch in evidence_pool.get("evidence_chunks", [])
        }
        pool_kg_nodes = {
            n.get("node_id", ""): n
            for n in evidence_pool.get("kg_nodes", [])
        }
        pool_edges = {
            e.get("edge_id", ""): e
            for e in evidence_pool.get("kg_edges", [])
        }
        pool_questions = {
            q.get("question_id", ""): q
            for q in evidence_pool.get("questions", [])
        }

    # --- chunks ---
    chunks: list[dict] = []
    for cid in requested_chunk_ids:
        ch = node_chunks.get(cid) or pool_chunks.get(cid)
        if ch:
            text = (ch.get("text_excerpt", "") or "")[:max_chars_per_chunk]
            chunks.append({
                "chunk_id": cid,
                "title": ch.get("title", ""),
                "source": ch.get("source", ""),
                "source_type": ch.get("source_type", ""),
                "score": ch.get("score", 0.0),
                "text": text,
            })

    # --- kg_nodes ---
    kg_nodes: list[dict] = []
    for nid in requested_kg_node_ids:
        n = node_kg_nodes.get(nid) or pool_kg_nodes.get(nid)
        if n:
            kg_nodes.append({
                "node_id": nid,
                "canonical_name": n.get("canonical_name", ""),
                "aliases": n.get("aliases", []),
                "node_type": n.get("node_type", ""),
                "node_kind": n.get("node_kind", ""),
                "subject": n.get("subject", ""),
                "chapter": n.get("chapter", ""),
                "section": n.get("section", ""),
            })

    # --- kg_edges ---
    kg_edges: list[dict] = []
    for eid in requested_kg_edge_ids:
        e = node_edges.get(eid) or pool_edges.get(eid)
        if e:
            kg_edges.append({
                "edge_id": eid,
                "source_node_id": e.get("source_node_id", ""),
                "target_node_id": e.get("target_node_id", ""),
                "relation": e.get("relation", ""),
                "edge_type": e.get("edge_type", ""),
                "weight": e.get("weight"),
                "confidence": e.get("confidence"),
            })

    # --- questions ---
    questions: list[dict] = []
    for qid in requested_question_ids:
        q = node_questions.get(qid) or pool_questions.get(qid)
        if q:
            questions.append({
                "question_id": qid,
                "type": q.get("type", ""),
                "stem": q.get("stem", ""),
                "options": q.get("options", []),
                "answer": q.get("answer", ""),
                "explanation": q.get("explanation", ""),
                "primary_node_id": q.get("primary_node_id", ""),
            })

    return {
        "chunks": chunks,
        "kg_nodes": kg_nodes,
        "kg_edges": kg_edges,
        "questions": questions,
    }


# ---------------------------------------------------------------------------
# targeted evidence search
# ---------------------------------------------------------------------------


def run_targeted_evidence_search(
    topic: str,
    outline_node: dict,
    search_queries: list[dict],
    top_k_per_query: int = 3,
) -> list[dict]:
    """Execute targeted KAG searches based on LLM-proposed queries.

    Each query in *search_queries* should have ``"query"`` and ``"purpose"``.
    Returns a list of per-query result bundles.  Does NOT judge content or
    select evidence for the LLM.

    Parameters
    ----------
    topic:
        The lesson topic (used only for logging context).
    outline_node:
        The outline node dict (used only for logging context).
    search_queries:
        List of dicts, each with ``"query"`` and ``"purpose"``.
    top_k_per_query:
        Max results per query (default 3).

    Returns
    -------
    list[dict] — one entry per query:
        {"query": str, "purpose": str, "results": [chunk dicts]}
    """
    if not search_queries:
        return []

    try:
        from backend.services.retrieval import search_knowledge_base
    except Exception:
        # retrieval not available — return empty gracefully
        return [
            {"query": sq.get("query", ""), "purpose": sq.get("purpose", ""), "results": []}
            for sq in search_queries
        ]

    results: list[dict] = []
    for sq in search_queries:
        query_text = (sq.get("query") or "").strip()
        purpose = (sq.get("purpose") or "").strip()
        if not query_text:
            continue
        try:
            raw_chunks = search_knowledge_base(query_text, top_k=top_k_per_query)
        except Exception:
            raw_chunks = []

        chunk_results: list[dict] = []
        for ch in raw_chunks:
            text = ch.text or ""
            excerpt = " ".join(text.split())[:800]
            # Normalize chunk id: ensure consistent "chunk:" prefix
            cid = ch.id or ""
            if cid and not cid.startswith("chunk:"):
                cid = f"chunk:{cid}"
            chunk_results.append({
                "chunk_id": cid,
                "title": ch.title or "",
                "source": ch.source or "",
                "source_type": ch.source_type or "",
                "score": round(float(ch.score), 4) if ch.score else 0.0,
                "text_excerpt": excerpt,
            })

        results.append({
            "query": query_text,
            "purpose": purpose,
            "results": chunk_results,
        })

    return results


# ---------------------------------------------------------------------------
# normalize / validate for handout section output
# ---------------------------------------------------------------------------


def _collect_allowed_evidence_sets(
    node_evidence: dict,
    extra_chunk_ids: set[str] | None = None,
) -> dict[str, set[str]]:
    """Collect evidence ids by type — each field gets its own allowed set.

    Returns a dict with typed allowed sets:
        kg_node_ids, kg_edge_ids, concept_ids, question_ids,
        evidence_chunk_ids, evidence_refs.

    Targeted search results are ONLY added to ``evidence_chunk_ids`` and
    ``evidence_refs`` — they MUST NOT leak into kg_node/edge/concept/question.
    """
    # --- per-type sets from node_evidence ---
    kg_node_ids: set[str] = set(node_evidence.get("kg_node_ids", []))
    kg_edge_ids: set[str] = set()
    concept_ids: set[str] = set()
    question_ids: set[str] = set()
    evidence_chunk_ids: set[str] = set()

    for n in node_evidence.get("kg_nodes", []):
        nid = n.get("node_id", "")
        if nid:
            kg_node_ids.add(nid)

    for e in node_evidence.get("kg_edges", []):
        eid = e.get("edge_id", "")
        if eid:
            kg_edge_ids.add(eid)

    for c in node_evidence.get("concepts", []):
        cid = c.get("node_id", "")
        if cid:
            concept_ids.add(cid)

    for q in node_evidence.get("questions", []):
        qid = q.get("question_id", "")
        if qid:
            question_ids.add(qid)

    for ch in node_evidence.get("evidence_chunks", []):
        cid = ch.get("chunk_id", "")
        if cid:
            evidence_chunk_ids.add(cid)

    # --- extra chunk ids from targeted search ---
    ts_chunk_ids: set[str] = set(extra_chunk_ids) if extra_chunk_ids else set()
    evidence_chunk_ids |= ts_chunk_ids

    # --- evidence_refs = all node evidence ids + targeted search chunk ids ---
    evidence_refs: set[str] = (
        kg_node_ids | kg_edge_ids | concept_ids | question_ids | evidence_chunk_ids
    )

    return {
        "kg_node_ids": kg_node_ids,
        "kg_edge_ids": kg_edge_ids,
        "concept_ids": concept_ids,
        "question_ids": question_ids,
        "evidence_chunk_ids": evidence_chunk_ids,
        "evidence_refs": evidence_refs,
    }


def normalize_handout_section_output(raw: Any) -> dict:
    """Normalize LLM handout section output — format fixes only, no content generation.

    Allowed:
    - Strip markdown code fences
    - Field name normalisation (case / minor variants)
    - String trim
    - Single string → single-element list for key_points / common_confusions

    Forbidden:
    - Adding missing key_points, explanations, evidence_refs, clinical_example
    - Generating content from titles
    """
    if isinstance(raw, str):
        import re as _re

        text = raw.strip()
        # Strip markdown code fences
        fence_match = _re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, _re.DOTALL)
        if not fence_match:
            fence_match = _re.search(r"```(?:json)?\s*\n?(.*)$", text, _re.DOTALL)
        if fence_match:
            text = fence_match.group(1).strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            raise ValueError(f"Cannot parse handout section output as JSON: text_head={text[:200]!r}")
    elif isinstance(raw, dict):
        parsed = raw
    else:
        raise ValueError(f"Unexpected handout section output type: {type(raw)}")

    if not isinstance(parsed, dict):
        raise ValueError(f"Expected dict, got {type(parsed)}")

    out: dict[str, Any] = {}

    # --- outline_node_id ---
    out["outline_node_id"] = str(parsed.get("outline_node_id", "")).strip()

    # --- title ---
    out["title"] = str(parsed.get("title", "")).strip()

    # --- section_status (trim only; validate() enforces the allowed set) ---
    status = str(parsed.get("section_status", "")).strip().lower()
    out["section_status"] = status  # preserve as-is, even if empty or invalid

    # --- evidence_status (trim only; validate() enforces the allowed set) ---
    ev_status = str(parsed.get("evidence_status", "")).strip().lower()
    out["evidence_status"] = ev_status  # preserve as-is, even if empty or invalid

    # --- error ---
    out["error"] = str(parsed.get("error", "")).strip() or None

    # --- used_evidence ---
    ue = parsed.get("used_evidence")
    if isinstance(ue, dict):
        out["used_evidence"] = {
            "kg_node_ids": _normalize_str_list(ue.get("kg_node_ids", [])),
            "kg_edge_ids": _normalize_str_list(ue.get("kg_edge_ids", [])),
            "concept_ids": _normalize_str_list(ue.get("concept_ids", [])),
            "question_ids": _normalize_str_list(ue.get("question_ids", [])),
            "evidence_chunk_ids": _normalize_str_list(ue.get("evidence_chunk_ids", [])),
        }
    else:
        out["used_evidence"] = {
            "kg_node_ids": [],
            "kg_edge_ids": [],
            "concept_ids": [],
            "question_ids": [],
            "evidence_chunk_ids": [],
        }

    # --- content ---
    raw_content = parsed.get("content")
    if not isinstance(raw_content, dict):
        raw_content = {}

    out["content"] = {
        "overview": str(raw_content.get("overview", "")).strip(),
        "key_points": _normalize_key_points(raw_content.get("key_points")),
        "clinical_example": str(raw_content.get("clinical_example", "")).strip(),
        "common_confusions": _normalize_common_confusions(raw_content.get("common_confusions")),
        "transition_to_next": str(raw_content.get("transition_to_next", "")).strip(),
    }

    # --- missing_evidence ---
    me = parsed.get("missing_evidence", [])
    if isinstance(me, list):
        out["missing_evidence"] = [str(x).strip() for x in me if str(x).strip()]
    else:
        out["missing_evidence"] = []

    return out


def _normalize_key_points(raw: Any) -> list[dict]:
    """Normalize key_points: ensure list of dicts with heading/explanation/evidence_refs."""
    if not isinstance(raw, list):
        return []
    result: list[dict] = []
    for kp in raw:
        if not isinstance(kp, dict):
            continue
        heading = str(kp.get("heading", "")).strip()
        explanation = str(kp.get("explanation", "")).strip()
        refs = kp.get("evidence_refs", [])
        if isinstance(refs, list):
            refs = [str(r).strip() for r in refs if str(r).strip()]
        else:
            refs = []
        # Keep even if heading/explanation empty — validate will flag it
        result.append({
            "heading": heading,
            "explanation": explanation,
            "evidence_refs": refs,
        })
    return result


def _normalize_common_confusions(raw: Any) -> list[dict]:
    """Normalize common_confusions: ensure list of dicts."""
    if not isinstance(raw, list):
        return []
    result: list[dict] = []
    for cc in raw:
        if not isinstance(cc, dict):
            continue
        confusion = str(cc.get("confusion", "")).strip()
        clarification = str(cc.get("clarification", "")).strip()
        refs = cc.get("evidence_refs", [])
        if isinstance(refs, list):
            refs = [str(r).strip() for r in refs if str(r).strip()]
        else:
            refs = []
        result.append({
            "confusion": confusion,
            "clarification": clarification,
            "evidence_refs": refs,
        })
    return result


def _normalize_str_list(value: Any) -> list[str]:
    """Normalize a value to a list of non-empty strings."""
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def validate_handout_section_output(
    section: dict,
    node_evidence: dict,
    extra_chunk_ids: set[str] | None = None,
) -> list[str]:
    """Validate a normalized handout section against hard constraints.

    Returns a list of validation error strings (empty = valid).
    Does NOT modify the section — only checks it.

    Parameters
    ----------
    section:
        The normalized section dict.
    node_evidence:
        The per-node evidence bundle.
    extra_chunk_ids:
        Additional chunk ids from targeted search. These are ONLY allowed
        in ``evidence_chunk_ids`` and ``evidence_refs`` fields, NOT in
        kg_node_ids / kg_edge_ids / concept_ids / question_ids.
    """
    errors: list[str] = []

    node_id = node_evidence.get("outline_node_id", "")

    # --- outline_node_id ---
    if section.get("outline_node_id") != node_id:
        errors.append(
            f"outline_node_id mismatch: got {section.get('outline_node_id')!r}, "
            f"expected {node_id!r}"
        )

    # --- section_status ---
    if section.get("section_status") not in ("llm_generated", "failed"):
        errors.append(
            f"invalid section_status: {section.get('section_status')!r}"
        )

    # --- evidence_status ---
    if section.get("evidence_status") not in ("sufficient", "insufficient", "not_generated"):
        errors.append(
            f"invalid evidence_status: {section.get('evidence_status')!r}"
        )

    content = section.get("content", {})
    key_points = content.get("key_points", [])

    # --- key_points must be a list ---
    if not isinstance(key_points, list):
        errors.append("key_points is not a list")
        key_points = []

    # --- if sufficient, at least 2 key_points ---
    if section.get("evidence_status") == "sufficient":
        if len(key_points) < 2:
            errors.append(
                f"evidence_status=sufficient but only {len(key_points)} key_point(s); "
                f"need at least 2"
            )

    # --- each key_point must have heading / explanation / evidence_refs ---
    for i, kp in enumerate(key_points):
        if not kp.get("heading"):
            errors.append(f"key_point[{i}] has empty heading")
        if not kp.get("explanation"):
            errors.append(f"key_point[{i}] has empty explanation")
        refs = kp.get("evidence_refs", [])
        if not isinstance(refs, list) or len(refs) == 0:
            errors.append(f"key_point[{i}] has no evidence_refs")

    # --- typed allowed sets ---
    allowed = _collect_allowed_evidence_sets(node_evidence, extra_chunk_ids=extra_chunk_ids)

    # --- evidence_refs (content body) must be in allowed["evidence_refs"] ---
    ref_allowed = allowed["evidence_refs"]
    for i, kp in enumerate(key_points):
        for ref in kp.get("evidence_refs", []):
            if ref not in ref_allowed:
                errors.append(
                    f"key_point[{i}] references evidence id {ref!r} "
                    f"not in allowed evidence_refs"
                )

    for i, cc in enumerate(content.get("common_confusions", [])):
        for ref in cc.get("evidence_refs", []):
            if ref not in ref_allowed:
                errors.append(
                    f"common_confusions[{i}] references evidence id {ref!r} "
                    f"not in allowed evidence_refs"
                )

    # --- used_evidence: type-specific checks ---
    ue = section.get("used_evidence", {})

    # kg_node_ids: only from node_evidence kg_nodes
    for eid in ue.get("kg_node_ids", []):
        if eid not in allowed["kg_node_ids"]:
            errors.append(
                f"used_evidence.kg_node_ids references id {eid!r} "
                f"not in allowed kg_node_ids (targeted search chunks are NOT valid here)"
            )

    # kg_edge_ids: only from node_evidence kg_edges
    for eid in ue.get("kg_edge_ids", []):
        if eid not in allowed["kg_edge_ids"]:
            errors.append(
                f"used_evidence.kg_edge_ids references id {eid!r} "
                f"not in allowed kg_edge_ids (targeted search chunks are NOT valid here)"
            )

    # concept_ids: only from node_evidence concepts
    for eid in ue.get("concept_ids", []):
        if eid not in allowed["concept_ids"]:
            errors.append(
                f"used_evidence.concept_ids references id {eid!r} "
                f"not in allowed concept_ids (targeted search chunks are NOT valid here)"
            )

    # question_ids: only from node_evidence questions
    for eid in ue.get("question_ids", []):
        if eid not in allowed["question_ids"]:
            errors.append(
                f"used_evidence.question_ids references id {eid!r} "
                f"not in allowed question_ids (targeted search chunks are NOT valid here)"
            )

    # evidence_chunk_ids: from node_evidence chunks + targeted search chunks
    for eid in ue.get("evidence_chunk_ids", []):
        if eid not in allowed["evidence_chunk_ids"]:
            errors.append(
                f"used_evidence.evidence_chunk_ids references id {eid!r} "
                f"not in allowed evidence_chunk_ids"
            )

    return errors


# ---------------------------------------------------------------------------
# generate_handout_section — main two-round agent flow
# ---------------------------------------------------------------------------


def _capture_raw_diag(raw: str, label: str = "") -> dict:
    """Capture diagnostic info from a raw LLM response string.

    Returns a dict with head, tail, length, and basic classification.
    Does NOT modify the response — read-only diagnostics.
    """
    text = raw.strip() if isinstance(raw, str) else str(raw)
    text_len = len(text)
    return {
        f"{label}raw_response_len": text_len,
        f"{label}raw_response_head": text[:300] if text_len > 0 else "(empty)",
        f"{label}raw_response_tail": text[-200:] if text_len > 200 else (text if text_len > 0 else "(empty)"),
        f"{label}is_empty": text_len == 0,
        f"{label}starts_with_brace": text.startswith("{") if text_len > 0 else False,
        f"{label}ends_with_brace": text.endswith("}") if text_len > 0 else False,
    }


def _failed_section_dict(
    node_id: str,
    title: str,
    error: str,
    trace: dict | None = None,
) -> dict:
    """Build a complete failed-section dict with all required fields."""
    sec: dict[str, Any] = {
        "outline_node_id": node_id,
        "title": title,
        "section_status": "failed",
        "evidence_status": "not_generated",
        "error": error,
        "used_evidence": {
            "kg_node_ids": [],
            "kg_edge_ids": [],
            "concept_ids": [],
            "question_ids": [],
            "evidence_chunk_ids": [],
        },
        "content": {
            "overview": "",
            "key_points": [],
            "clinical_example": "",
            "common_confusions": [],
            "transition_to_next": "",
        },
        "missing_evidence": [],
    }
    if trace:
        sec["_trace"] = trace
    return sec


def generate_handout_section(
    outline_node: dict,
    node_evidence: dict,
    evidence_pool: dict,
    user_profile: dict | None = None,
    previous_node_title: str | None = None,
    next_node_title: str | None = None,
) -> dict:
    """Generate a single handout section for one outline node.

    Two-round LLM flow:
    1. **Evidence Planning** — LLM reviews evidence index, decides what to fetch.
    2. **Python fetch** — read-only helper gets evidence detail.
    3. **Write Section** — LLM writes the section with evidence refs.

    Parameters
    ----------
    outline_node:
        An outline node dict from ``outline["nodes"][i]``.
    node_evidence:
        The per-node evidence bundle from
        ``outline_evidence_map[outline_node["id"]]``.
    evidence_pool:
        The global evidence pool for fallback detail lookups.
    user_profile:
        Optional user profile dict.
    previous_node_title:
        Title of the previous outline node (for transition writing).
    next_node_title:
        Title of the next outline node (for transition writing).

    Returns
    -------
    A structured handout section dict (see task spec for shape).
    """
    t_start = time.time()
    node_id = outline_node.get("id", node_evidence.get("outline_node_id", ""))
    node_title = outline_node.get("title", node_evidence.get("outline_node_title", ""))

    # --- 0. check LLM availability ---
    try:
        from backend.services.llm_client import chat_completion, is_llm_configured
    except Exception:
        return _failed_section_dict(node_id, node_title, "llm_client_import_failed")

    if not is_llm_configured(*_HANDOUT_API_KEY_NAMES):
        return _failed_section_dict(node_id, node_title, "no_llm_api_key_configured")

    _prompt_dir = Path(__file__).resolve().parent / "prompts"

    # =====================================================================
    # Round 1: Evidence Planning
    # =====================================================================

    # --- build evidence index summary ---
    evidence_index = _build_evidence_index_summary(node_evidence)

    plan_system = _load_prompt(_prompt_dir, "handout_section_plan.md")

    plan_user = json.dumps({
        "outline_node": {
            "id": outline_node.get("id", ""),
            "title": outline_node.get("title", ""),
            "learning_objectives": outline_node.get("learning_objectives", []),
            "sequence_reason": outline_node.get("sequence_reason", ""),
        },
        "previous_node_title": previous_node_title,
        "next_node_title": next_node_title,
        "evidence_index": evidence_index,
    }, ensure_ascii=False, indent=2)

    # --- call LLM Round 1 ---
    t_round1_start = time.time()
    round1_retry_used = False
    try:
        plan_raw = chat_completion(
            [
                {"role": "system", "content": plan_system},
                {"role": "user", "content": plan_user},
            ],
            temperature=0.2,
            max_tokens=3000,
            api_key_names=_HANDOUT_API_KEY_NAMES,
            base_url_names=_HANDOUT_BASE_URL_NAMES,
            model_names=_HANDOUT_MODEL_NAMES,
            response_format={"type": "json_object"},
        )
    except Exception as exc:
        return _failed_section_dict(node_id, node_title, f"round1_llm_call_failed: {str(exc)[:300]}")
    round1_seconds = round(time.time() - t_round1_start, 3)

    # --- parse Round 1 output ---
    plan = _parse_plan_output(plan_raw)

    # --- retry once if parse failed ---
    round1_retry_error: str | None = None
    if plan is None:
        round1_retry_used = True
        try:
            plan_raw_retry = chat_completion(
                [
                    {"role": "system", "content": plan_system},
                    {"role": "user", "content": plan_user},
                ],
                temperature=0.2,
                max_tokens=3000,
                api_key_names=_HANDOUT_API_KEY_NAMES,
                base_url_names=_HANDOUT_BASE_URL_NAMES,
                model_names=_HANDOUT_MODEL_NAMES,
                response_format={"type": "json_object"},
            )
            plan = _parse_plan_output(plan_raw_retry)
            if plan is not None:
                plan_raw = plan_raw_retry  # use retry response for diagnostics
        except Exception as exc:
            round1_retry_error = f"{type(exc).__name__}: {str(exc)[:300]}"

    if plan is None:
        diag = _capture_raw_diag(plan_raw, "round1_")
        diag["round1_used_json_mode"] = True
        diag["round1_retry_attempted"] = True
        diag["round1_retry_succeeded"] = False
        diag["round1_seconds"] = round1_seconds
        if round1_retry_error:
            diag["round1_retry_error"] = round1_retry_error
        # Classify failure mode
        if diag["round1_is_empty"]:
            diag["round1_failure_mode"] = "llm_empty_response"
        elif diag["round1_starts_with_brace"] and not diag["round1_ends_with_brace"]:
            diag["round1_failure_mode"] = "likely_truncated"
        elif not diag["round1_starts_with_brace"]:
            diag["round1_failure_mode"] = "non_json_response"
        else:
            diag["round1_failure_mode"] = "schema_mismatch_or_parse_error"
        return _failed_section_dict(
            node_id, node_title,
            f"round1_json_parse_failed: "
            f"mode={diag['round1_failure_mode']}, "
            f"raw_len={diag['round1_raw_response_len']}, "
            f"is_empty={diag['round1_is_empty']}, "
            f"starts_brace={diag['round1_starts_with_brace']}, "
            f"ends_brace={diag['round1_ends_with_brace']}",
            trace=diag,
        )

    # --- sanitize invalid ids (non-fatal: remove + warn, don't fail the section) ---
    plan, plan_warnings = sanitize_handout_evidence_plan(plan, node_evidence)

    # --- validate caps and targeted_search_queries format (fatal) ---
    plan_errors = _validate_plan_requested_ids(plan, node_evidence)
    if plan_errors:
        return _failed_section_dict(node_id, node_title, f"round1_validation_failed: {'; '.join(plan_errors[:5])}")

    # =====================================================================
    # Python: fetch evidence detail
    # =====================================================================
    evidence_detail = get_evidence_detail(
        node_evidence=node_evidence,
        requested_chunk_ids=plan.get("requested_chunk_ids", []),
        requested_kg_node_ids=plan.get("requested_kg_node_ids", []),
        requested_kg_edge_ids=plan.get("requested_kg_edge_ids", []),
        requested_question_ids=plan.get("requested_question_ids", []),
        evidence_pool=evidence_pool,
    )

    # =====================================================================
    # Python: execute targeted evidence search (if LLM requested it)
    # =====================================================================
    targeted_search_queries: list[dict] = plan.get("targeted_search_queries", [])
    if not isinstance(targeted_search_queries, list):
        targeted_search_queries = []

    targeted_search_results: list[dict] = []
    extra_chunk_ids: set[str] = set()
    targeted_search_query_timings: list[dict] = []

    t_search_start = time.time()
    if targeted_search_queries:
        topic_str = outline_node.get("title", evidence_pool.get("topic", ""))
        for sq in targeted_search_queries:
            q_text = (sq.get("query") or "").strip()
            if not q_text:
                continue
            t_q_start = time.time()
            single_result = run_targeted_evidence_search(
                topic=topic_str,
                outline_node=outline_node,
                search_queries=[sq],
                top_k_per_query=3,
            )
            t_q_elapsed = round(time.time() - t_q_start, 3)
            result_count = 0
            if single_result:
                targeted_search_results.extend(single_result)
                for r in single_result[0].get("results", []):
                    cid = r.get("chunk_id", "")
                    if cid:
                        extra_chunk_ids.add(cid)
                        result_count += 1
            targeted_search_query_timings.append({
                "query": q_text[:80],
                "seconds": t_q_elapsed,
                "result_count": result_count,
            })
    targeted_search_seconds = round(time.time() - t_search_start, 3)
    targeted_search_query_count = len(targeted_search_query_timings)

    # =====================================================================
    # Round 2: Write Section
    # =====================================================================

    write_system = _load_prompt(_prompt_dir, "handout_section_write.md")

    write_user = json.dumps({
        "outline_node": {
            "id": outline_node.get("id", ""),
            "title": outline_node.get("title", ""),
            "learning_objectives": outline_node.get("learning_objectives", []),
            "sequence_reason": outline_node.get("sequence_reason", ""),
        },
        "previous_node_title": previous_node_title,
        "next_node_title": next_node_title,
        "evidence_index": evidence_index,
        "evidence_detail": evidence_detail,
        "targeted_search_results": targeted_search_results,
        "section_outline_from_plan": plan.get("section_outline", []),
        "evidence_questions_from_plan": plan.get("evidence_questions", []),
    }, ensure_ascii=False, indent=2)

    # --- call LLM Round 2 ---
    t_round2_start = time.time()
    round2_retry_used = False
    try:
        write_raw = chat_completion(
            [
                {"role": "system", "content": write_system},
                {"role": "user", "content": write_user},
            ],
            temperature=0.3,
            max_tokens=12000,
            api_key_names=_HANDOUT_API_KEY_NAMES,
            base_url_names=_HANDOUT_BASE_URL_NAMES,
            model_names=_HANDOUT_MODEL_NAMES,
            response_format={"type": "json_object"},
        )
    except Exception as exc:
        return _failed_section_dict(node_id, node_title, f"round2_llm_call_failed: {str(exc)[:300]}")
    round2_seconds = round(time.time() - t_round2_start, 3)

    # --- normalize ---
    round2_normalize_error: str | None = None
    try:
        section = normalize_handout_section_output(write_raw)
    except ValueError as exc:
        round2_normalize_error = str(exc)[:300]

    # --- retry once if normalize failed ---
    round2_retry_error: str | None = None
    if round2_normalize_error is not None:
        round2_retry_used = True
        try:
            write_raw_retry = chat_completion(
                [
                    {"role": "system", "content": write_system},
                    {"role": "user", "content": write_user},
                ],
                temperature=0.3,
                max_tokens=12000,
                api_key_names=_HANDOUT_API_KEY_NAMES,
                base_url_names=_HANDOUT_BASE_URL_NAMES,
                model_names=_HANDOUT_MODEL_NAMES,
                response_format={"type": "json_object"},
            )
            section = normalize_handout_section_output(write_raw_retry)
            round2_normalize_error = None  # retry succeeded
            write_raw = write_raw_retry  # use retry response for diagnostics
        except ValueError:
            # normalize retry also failed — capture nothing extra, normalize_error already set
            pass
        except Exception as exc:
            round2_retry_error = f"{type(exc).__name__}: {str(exc)[:300]}"

    if round2_normalize_error is not None:
        diag = _capture_raw_diag(write_raw, "round2_")
        diag["round2_used_json_mode"] = True
        diag["round2_retry_attempted"] = True
        diag["round2_retry_succeeded"] = False
        diag["round2_seconds"] = round2_seconds
        diag["round2_normalize_error"] = round2_normalize_error
        if round2_retry_error:
            diag["round2_retry_error"] = round2_retry_error

        # Classify failure mode
        failure_mode: str
        if diag["round2_is_empty"]:
            failure_mode = "llm_empty_response"
        elif diag["round2_starts_with_brace"] and not diag["round2_ends_with_brace"]:
            failure_mode = "likely_truncated"
        elif not diag["round2_starts_with_brace"]:
            failure_mode = "non_json_response"
        else:
            failure_mode = "schema_mismatch_or_parse_error"
        diag["round2_failure_mode"] = failure_mode

        return _failed_section_dict(
            node_id, node_title,
            f"normalize_failed: "
            f"mode={failure_mode}, "
            f"raw_len={diag['round2_raw_response_len']}, "
            f"is_empty={diag['round2_is_empty']}, "
            f"starts_brace={diag['round2_starts_with_brace']}, "
            f"ends_brace={diag['round2_ends_with_brace']}, "
            f"error_head={round2_normalize_error[:150]}",
            trace=diag,
        )

    # --- validate (type-specific: extra_chunk_ids only for evidence_chunk_ids / evidence_refs) ---
    t_validate_start = time.time()
    validation_errors = validate_handout_section_output(
        section, node_evidence, extra_chunk_ids=extra_chunk_ids or None
    )
    validate_seconds = round(time.time() - t_validate_start, 3)

    if validation_errors:
        trace_fail = {
            "plan_warnings": plan_warnings,
            "targeted_search_used": len(targeted_search_results) > 0,
            "targeted_search_queries": [
                {"query": sq.get("query", ""), "purpose": sq.get("purpose", "")}
                for sq in targeted_search_queries
            ],
            "targeted_search_result_count": len(list(extra_chunk_ids)),
            "targeted_search_result_chunk_ids": list(extra_chunk_ids),
            "timings": {
                "round1_plan_seconds": round1_seconds,
                "targeted_search_seconds": targeted_search_seconds,
                "targeted_search_query_count": targeted_search_query_count,
                "targeted_search_query_timings": targeted_search_query_timings,
                "round2_write_seconds": round2_seconds,
                "validate_seconds": validate_seconds,
            },
        }
        return _failed_section_dict(
            node_id, node_title,
            f"validation_failed: {'; '.join(validation_errors[:5])}",
            trace=trace_fail,
        )

    # --- attach generation trace ---
    total_seconds = round(time.time() - t_start, 3)
    targeted_search_result_chunk_ids: list[str] = list(extra_chunk_ids)
    targeted_search_result_count = len(targeted_search_result_chunk_ids)

    section["_trace"] = {
        "used_json_mode": True,
        "round1_retry_used": round1_retry_used,
        "round2_retry_used": round2_retry_used,
        "plan_warnings": plan_warnings,
        "targeted_search_used": len(targeted_search_results) > 0,
        "targeted_search_queries": [
            {"query": sq.get("query", ""), "purpose": sq.get("purpose", "")}
            for sq in targeted_search_queries
        ],
        "targeted_search_result_count": targeted_search_result_count,
        "targeted_search_result_chunk_ids": targeted_search_result_chunk_ids,
        "timings": {
            "round1_plan_seconds": round1_seconds,
            "targeted_search_seconds": targeted_search_seconds,
            "targeted_search_query_count": targeted_search_query_count,
            "targeted_search_query_timings": targeted_search_query_timings,
            "round2_write_seconds": round2_seconds,
            "validate_seconds": validate_seconds,
            "total_section_seconds": total_seconds,
        },
    }

    return section


# ---------------------------------------------------------------------------
# Internal helpers for generate_handout_section
# ---------------------------------------------------------------------------


def _build_evidence_index_summary(node_evidence: dict) -> dict:
    """Build a lightweight evidence index summary for LLM consumption."""
    index: dict[str, Any] = {
        "outline_node_id": node_evidence.get("outline_node_id", ""),
        "outline_node_title": node_evidence.get("outline_node_title", ""),
        "sequence_reason": node_evidence.get("sequence_reason", ""),
    }

    # kg_node_ids
    index["kg_node_ids"] = node_evidence.get("kg_node_ids", [])

    # kg_nodes (lightweight: id + name only)
    index["kg_nodes"] = [
        {"node_id": n.get("node_id", ""), "canonical_name": n.get("canonical_name", "")}
        for n in node_evidence.get("kg_nodes", [])
    ]

    # kg_edges (lightweight: id + relation)
    index["kg_edges"] = [
        {
            "edge_id": e.get("edge_id", ""),
            "relation": e.get("relation", ""),
            "source_node_id": e.get("source_node_id", ""),
            "target_node_id": e.get("target_node_id", ""),
        }
        for e in node_evidence.get("kg_edges", [])
    ]

    # concepts (lightweight)
    index["concepts"] = [
        {"node_id": c.get("node_id", ""), "name": c.get("name", "")}
        for c in node_evidence.get("concepts", [])
    ]

    # questions (lightweight: id + stem prefix)
    index["questions"] = [
        {
            "question_id": q.get("question_id", ""),
            "type": q.get("type", ""),
            "stem": (q.get("stem", "") or "")[:120],
        }
        for q in node_evidence.get("questions", [])
    ]

    # evidence_chunks (lightweight: id + title + short excerpt)
    index["evidence_chunks"] = [
        {
            "chunk_id": ch.get("chunk_id", ""),
            "title": ch.get("title", ""),
            "source_type": ch.get("source_type", ""),
            "text_excerpt": (ch.get("text_excerpt", "") or "")[:200],
        }
        for ch in node_evidence.get("evidence_chunks", [])
    ]

    # counts
    index["counts"] = {
        "kg_nodes": len(node_evidence.get("kg_nodes", [])),
        "kg_edges": len(node_evidence.get("kg_edges", [])),
        "concepts": len(node_evidence.get("concepts", [])),
        "questions": len(node_evidence.get("questions", [])),
        "evidence_chunks": len(node_evidence.get("evidence_chunks", [])),
    }

    return index


def _load_prompt(prompt_dir: Path, filename: str) -> str:
    """Load a prompt file, or return a fallback."""
    prompt_path = prompt_dir / filename
    if prompt_path.exists():
        return prompt_path.read_text(encoding="utf-8")
    return "你是一名口腔医学讲义编写助手。请基于提供的证据材料，输出严格JSON格式的讲义section。"


def _parse_plan_output(raw: str) -> dict | None:
    """Parse the Round 1 planning LLM output into a dict, or None on failure."""
    import re as _re

    text = raw.strip()
    fence_match = _re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, _re.DOTALL)
    if not fence_match:
        fence_match = _re.search(r"```(?:json)?\s*\n?(.*)$", text, _re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def sanitize_handout_evidence_plan(
    plan: dict,
    node_evidence: dict,
) -> tuple[dict, list[str]]:
    """Remove non-existent or wrong-type evidence ids from a Round 1 plan.

    Each requested-id field is checked against its **type-specific** allowed
    set (e.g. ``requested_chunk_ids`` against ``evidence_chunk_ids``).  An
    id that exists in the evidence pool but belongs to the wrong type (e.g.
    a chunk id in ``requested_kg_node_ids``) is removed and warned.

    Caps and targeted_search_queries format are NOT checked here —
    :func:`_validate_plan_requested_ids` still handles those as fatal.

    Returns ``(cleaned_plan, warnings)``.  *cleaned_plan* is a deep copy;
    the original *plan* is never modified.
    """
    import copy

    cleaned = copy.deepcopy(plan)
    warnings: list[str] = []

    allowed_sets = _collect_allowed_evidence_sets(node_evidence)

    # field -> allowed set key mapping
    field_allowed_map = {
        "requested_chunk_ids": "evidence_chunk_ids",
        "requested_kg_node_ids": "kg_node_ids",
        "requested_kg_edge_ids": "kg_edge_ids",
        "requested_question_ids": "question_ids",
    }

    for field, allowed_key in field_allowed_map.items():
        raw = cleaned.get(field, [])
        if not isinstance(raw, list):
            continue
        allowed = allowed_sets[allowed_key]
        kept: list[str] = []
        for rid in raw:
            rid_str = str(rid).strip()
            if rid_str in allowed:
                kept.append(rid_str)
            else:
                warnings.append(
                    f"{field}: removed invalid or wrong-type id {rid_str!r}"
                )
        cleaned[field] = kept

    return cleaned, warnings


def _validate_plan_requested_ids(plan: dict, node_evidence: dict) -> list[str]:
    """Validate that all requested ids in *plan* exist in *node_evidence*.

    Also checks caps: max 4 chunks, 6 edges, 3 questions, and validates
    targeted_search_queries format.
    """
    errors: list[str] = []

    allowed_sets = _collect_allowed_evidence_sets(node_evidence)

    # --- chunk cap ---
    req_chunks = plan.get("requested_chunk_ids", [])
    if isinstance(req_chunks, list) and len(req_chunks) > 4:
        errors.append(f"requested {len(req_chunks)} chunks, max 4 allowed")

    # --- edge cap ---
    req_edges = plan.get("requested_kg_edge_ids", [])
    if isinstance(req_edges, list) and len(req_edges) > 6:
        errors.append(f"requested {len(req_edges)} edges, max 6 allowed")

    # --- question cap ---
    req_qs = plan.get("requested_question_ids", [])
    if isinstance(req_qs, list) and len(req_qs) > 3:
        errors.append(f"requested {len(req_qs)} questions, max 3 allowed")

    # --- id existence checks (type-specific) ---
    chunk_allowed = allowed_sets["evidence_chunk_ids"]
    for cid in (req_chunks if isinstance(req_chunks, list) else []):
        if cid not in chunk_allowed:
            errors.append(f"requested_chunk_id {cid!r} not in allowed evidence_chunk_ids")

    kg_node_allowed = allowed_sets["kg_node_ids"]
    for nid in (plan.get("requested_kg_node_ids", []) if isinstance(plan.get("requested_kg_node_ids"), list) else []):
        if nid not in kg_node_allowed:
            errors.append(f"requested_kg_node_id {nid!r} not in allowed kg_node_ids")

    kg_edge_allowed = allowed_sets["kg_edge_ids"]
    for eid in (req_edges if isinstance(req_edges, list) else []):
        if eid not in kg_edge_allowed:
            errors.append(f"requested_kg_edge_id {eid!r} not in allowed kg_edge_ids")

    question_allowed = allowed_sets["question_ids"]
    for qid in (req_qs if isinstance(req_qs, list) else []):
        if qid not in question_allowed:
            errors.append(f"requested_question_id {qid!r} not in allowed question_ids")

    # --- targeted_search_queries validation ---
    tqs = plan.get("targeted_search_queries", [])
    if not isinstance(tqs, list):
        tqs = []

    if len(tqs) > 3:
        errors.append(f"targeted_search_queries: {len(tqs)} queries, max 3 allowed")

    for i, tq in enumerate(tqs):
        if not isinstance(tq, dict):
            errors.append(f"targeted_search_queries[{i}] is not a dict")
            continue
        query_text = (tq.get("query") or "").strip()
        purpose = (tq.get("purpose") or "").strip()
        if not query_text:
            errors.append(f"targeted_search_queries[{i}] has empty query")
        if not purpose:
            errors.append(f"targeted_search_queries[{i}] has empty purpose")
        if query_text and len(query_text) > 80:
            errors.append(
                f"targeted_search_queries[{i}] query is {len(query_text)} chars, max 80 allowed"
            )

    return errors


# ============================================================================
# Multi-section Handout Builder
# ============================================================================


def generate_handout(
    topic: str,
    user_profile: dict | None = None,
    max_workers: int = 2,
    max_sections: int | None = None,
) -> dict:
    """Generate a complete multi-section handout for *topic*.

    Calls Planner to get the outline, then concurrently generates one
    handout section per outline node via :func:`generate_handout_section`.

    Parameters
    ----------
    topic:
        The lesson topic (e.g. ``"牙周炎"``).
    user_profile:
        Optional user profile dict.
    max_workers:
        Max concurrent section-generation workers (default 2).
    max_sections:
        If set, only generate the first *max_sections* nodes (for
        smoke/demo, does not alter the outline itself).

    Returns
    -------
    A complete handout dict with sections, source_index, and
    generation_summary.
    """
    t_start = time.time()
    user_profile = user_profile or {}

    # --- 1. Planner ---
    t_planner_start = time.time()
    try:
        from backend.services.learning.planner import plan_lesson
    except Exception:
        return {
            "topic": topic,
            "handout_status": "failed",
            "outline": None,
            "sections": [],
            "source_index": {},
            "generation_summary": {},
            "warnings": ["planner_import_failed"],
        }

    result = plan_lesson(topic, user_profile=user_profile)
    planner_seconds = round(time.time() - t_planner_start, 3)

    planning_trace = result.get("planning_trace", {})
    if not planning_trace.get("is_agent_planned"):
        return {
            "topic": topic,
            "handout_status": "failed",
            "outline": result.get("outline"),
            "sections": [],
            "source_index": {},
            "generation_summary": {
                "planner_status": planning_trace.get("strategy", "unknown"),
                "planner_seconds": planner_seconds,
                "is_agent_planned": False,
            },
            "warnings": [
                f"Planner not on Agent success path: "
                f"{planning_trace.get('fallback_reason', 'unknown')}"
            ],
        }

    outline = result["outline"]
    outline_evidence_map = result["outline_evidence_map"]
    evidence_pool = result["evidence_pool"]

    outline_nodes = outline.get("nodes", [])
    if max_sections is not None and max_sections > 0:
        outline_nodes = outline_nodes[:max_sections]

    total_nodes = len(outline_nodes)

    # --- 2. build per-node context (previous/next titles) ---
    def _prev_next(node_id: str, node_list: list[dict]) -> tuple[str | None, str | None]:
        idx = next((i for i, n in enumerate(node_list) if n.get("id") == node_id), -1)
        prev_title = node_list[idx - 1]["title"] if idx > 0 else None
        next_title = node_list[idx + 1]["title"] if idx >= 0 and idx + 1 < len(node_list) else None
        return prev_title, next_title

    # --- 3. generate sections concurrently ---
    t_sections_start = time.time()

    # Results collected in order via dict keyed by node_id
    section_results: dict[str, dict] = {}

    def _generate_one(node: dict) -> tuple[str, dict]:
        nid = node.get("id", "")
        ne = outline_evidence_map.get(nid, {})
        prev_title, next_title = _prev_next(nid, outline_nodes)
        try:
            sec = generate_handout_section(
                outline_node=node,
                node_evidence=ne,
                evidence_pool=evidence_pool,
                user_profile=user_profile,
                previous_node_title=prev_title,
                next_node_title=next_title,
            )
        except Exception:
            sec = _failed_section_dict(
                nid, node.get("title", ""),
                f"exception: {traceback.format_exc()[-300:]}",
            )
        return (nid, sec)

    workers = max(1, min(max_workers, total_nodes))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_generate_one, node): node for node in outline_nodes}
        for future in as_completed(futures):
            try:
                nid, sec = future.result()
                section_results[nid] = sec
            except Exception:
                node = futures[future]
                nid = node.get("id", "")
                section_results[nid] = _failed_section_dict(
                    nid, node.get("title", ""), "future_result_failed",
                )

    sections_wall_seconds = round(time.time() - t_sections_start, 3)

    # --- 4. order sections by outline ---
    sections: list[dict] = []
    for node in outline_nodes:
        nid = node.get("id", "")
        sec = section_results.get(nid)
        if sec is None:
            sec = _failed_section_dict(
                nid, node.get("title", ""), "section_missing_from_results",
            )
        sections.append(sec)

    # --- 5. source_index ---
    source_index: dict[str, set[str]] = {
        "evidence_chunk_ids": set(),
        "kg_node_ids": set(),
        "kg_edge_ids": set(),
        "concept_ids": set(),
        "question_ids": set(),
    }
    for sec in sections:
        ue = sec.get("used_evidence", {})
        if isinstance(ue, dict):
            for key in source_index:
                vals = ue.get(key, [])
                if isinstance(vals, list):
                    source_index[key].update(str(v) for v in vals if v)

    source_index_serializable: dict[str, list[str]] = {
        k: sorted(v) for k, v in source_index.items()
    }

    # --- 6. generation_summary ---
    llm_generated_count = sum(
        1 for s in sections if s.get("section_status") == "llm_generated"
    )
    failed_count = sum(
        1 for s in sections if s.get("section_status") == "failed"
    )
    sufficient_count = sum(
        1 for s in sections if s.get("evidence_status") == "sufficient"
    )
    insufficient_count = sum(
        1 for s in sections if s.get("evidence_status") == "insufficient"
    )
    targeted_search_used_count = sum(
        1 for s in sections
        if (s.get("_trace") or {}).get("targeted_search_used")
    )

    total_seconds = round(time.time() - t_start, 3)

    # Determine handout_status
    if failed_count == 0:
        handout_status = "generated"
    elif failed_count == total_nodes:
        handout_status = "failed"
    else:
        handout_status = "partial"

    generation_summary = {
        "planner_status": "llm_agent_planner",
        "is_agent_planned": True,
        "section_count": total_nodes,
        "llm_generated_count": llm_generated_count,
        "failed_count": failed_count,
        "sufficient_count": sufficient_count,
        "insufficient_count": insufficient_count,
        "targeted_search_used_count": targeted_search_used_count,
        "total_seconds": total_seconds,
        "planner_seconds": planner_seconds,
        "sections_wall_seconds": sections_wall_seconds,
        "max_workers": workers,
    }

    # --- 7. warnings ---
    warnings: list[str] = []
    for sec in sections:
        if sec.get("section_status") == "failed":
            warnings.append(
                f"{sec.get('outline_node_id', '?')} failed: "
                f"{sec.get('error', 'unknown')[:200]}"
            )

    return {
        "topic": topic,
        "handout_status": handout_status,
        "outline": outline,
        "sections": sections,
        "source_index": source_index_serializable,
        "generation_summary": generation_summary,
        "warnings": warnings,
    }
