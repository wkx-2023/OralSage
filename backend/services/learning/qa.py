"""QA Agent — interruption question handling with evidence + controlled targeted search.

Answers a student's mid-lecture question without advancing course progress.

Flow:
1. LLM QA Plan — judge evidence sufficiency, propose search_plan if needed
2. Python — execute search_plan (reuse Resource Factory helpers), fetch evidence detail
3. LLM QA Answer — generate answer from handout_section + evidence + search results
4. Python — normalize / validate

Rule-based placeholder output is retained as a safe fallback. No database writes.

Public API
----------
* ``classify_interruption_question`` — lightweight keyword classifier.
* ``answer_interruption_question`` — rule-based placeholder (fallback only).
* ``answer_question_with_evidence`` — main QA entry: handout_section + evidence + targeted search.
"""

from __future__ import annotations

import json
import re
import time
import traceback
from pathlib import Path
from typing import Any

from backend.services.llm_client import chat_completion, is_llm_configured

from .tutor import find_outline_node

# LLM config key priority for QA
_QA_API_KEY_NAMES = (
    "LEARNING_QA_LLM_API_KEY",
    "LEARNING_LLM_API_KEY",
    "QWEN_API_KEY",
    "DASHSCOPE_API_KEY",
)
_QA_BASE_URL_NAMES = (
    "LEARNING_QA_LLM_BASE_URL",
    "LEARNING_LLM_BASE_URL",
    "QWEN_BASE_URL",
    "DASHSCOPE_BASE_URL",
)
_QA_MODEL_NAMES = (
    "LEARNING_QA_LLM_MODEL",
    "LEARNING_LLM_MODEL",
    "QWEN_MODEL",
    "DASHSCOPE_MODEL",
)


# ============================================================================
# Keyword classification (retained from original)
# ============================================================================

_MECHANISM_KEYWORDS = ["为什么", "机制", "导致", "怎么会", "原因", "引起", "诱发"]
_COMPARISON_KEYWORDS = ["区别", "鉴别", "不同", "一样吗", "差异", "区分", "对比"]
_CLINICAL_KEYWORDS = ["治疗", "处理", "怎么办", "用药", "手术", "方案", "怎么治"]


def _match_any(text: str, keywords: list[str]) -> bool:
    return any(kw in text for kw in keywords)


def classify_interruption_question(question: str) -> dict[str, Any]:
    """Classify an interruption question into one of four types.

    Parameters
    ----------
    question:
        The raw question text from the student.

    Returns
    -------
    dict
        Keys: ``question_type``, ``confusion_tags``, ``trace``.
    """
    question = question.strip()

    if _match_any(question, _MECHANISM_KEYWORDS):
        question_type = "mechanism_question"
        confusion_tags = ["机制链条不完整"]
    elif _match_any(question, _COMPARISON_KEYWORDS):
        question_type = "comparison_question"
        confusion_tags = ["概念混淆"]
    elif _match_any(question, _CLINICAL_KEYWORDS):
        question_type = "clinical_action_question"
        confusion_tags = ["治疗决策不清"]
    else:
        question_type = "general_question"
        confusion_tags = ["课堂打断问题"]

    return {
        "question_type": question_type,
        "confusion_tags": confusion_tags,
        "trace": {"mode": "rule_based_question_classifier"},
    }


# ============================================================================
# Rule-based fallback QA (retained from original)
# ============================================================================


def answer_interruption_question(
    question: str,
    outline: dict[str, Any],
    current_node_id: str | None,
    user_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Produce a structured QA result for a mid-lecture interruption (placeholder).

    This is the **rule-based fallback**.  It does **not** call an LLM and does
    **not** advance course progress.  Use :func:`answer_question_with_evidence`
    for the real QA path.
    """
    _index, node = find_outline_node(outline, current_node_id)
    node_id = node.get("id", "")
    node_title = node.get("title", "")
    kg_node_ids: list[str] = node.get("kg_node_ids", [])

    classification = classify_interruption_question(question)
    question_type: str = classification["question_type"]
    confusion_tags: list[str] = classification["confusion_tags"]

    type_label = {
        "mechanism_question": "机制类",
        "comparison_question": "比较/鉴别类",
        "clinical_action_question": "临床处理类",
        "general_question": "一般性",
    }.get(question_type, "一般性")

    answer = (
        f"你问的是当前小节「{node_title}」中的问题。\n\n"
        f"这个问题会被记录为 {type_label} 问题（{question_type}），"
        f"课程主线暂不推进，后续会在本节强化。\n\n"
        f"当前回答为规则模板占位，真实答疑后续由 LLM/KAG 生成。"
    )

    return {
        "qa_status": "fallback_rule_based",
        "question": question,
        "answer": answer,
        "question_scope": "in_current_section",
        "evidence_status": "insufficient",
        "targeted_search_used": False,
        "search_plan": [],
        "used_evidence": [],
        "related_kg_node_ids": kg_node_ids,
        "confusion_kg_node_ids": [],
        "should_reinforce": True,
        "advance_progress": False,
        "question_type": question_type,
        "confusion_tags": confusion_tags,
        "related_outline_node_id": node_id,
        "related_outline_node_title": node_title,
        "content_status": "placeholder",
        "content_note": "规则模板占位，仅用于验证流程；真实答疑由 LLM 生成。",
        "trace": {
            "mode": "fallback_rule_based",
            "uses_llm": False,
            "uses_handout_section": False,
            "uses_current_evidence": False,
            "targeted_search_used": False,
            "fallback_rule_based": True,
        },
    }


# ============================================================================
# Main QA entry: handout_section + evidence + controlled targeted search
# ============================================================================


def answer_question_with_evidence(
    question: str,
    topic: str,
    current_outline_node: dict[str, Any] | None = None,
    current_handout_section: dict[str, Any] | None = None,
    node_evidence: dict[str, Any] | None = None,
    evidence_pool: dict[str, Any] | None = None,
    user_profile: dict[str, Any] | None = None,
    outline: dict[str, Any] | None = None,
    current_node_id: str | None = None,
    recent_qa_context: list[dict[str, Any]] | None = None,
    wrong_answer_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Answer student question with handout_section + evidence + controlled targeted search.

    This is the **main QA entry point**.  It follows a 3-step flow:
    1. LLM QA Plan — judge evidence sufficiency, propose search_plan if needed
    2. Python — execute search_plan (reuse Resource Factory helpers)
    3. LLM QA Answer — generate answer from handout + evidence + search results

    Falls back to :func:`answer_interruption_question` when LLM is unavailable
    or all steps fail.

    Parameters
    ----------
    question:
        The student's raw question text.
    topic:
        The lesson topic (e.g. ``"牙周炎"``).
    current_outline_node:
        The current outline node dict.
    current_handout_section:
        The current handout section content dict (from Resource Factory).
    node_evidence:
        The per-node evidence bundle from ``outline_evidence_map[node_id]``.
    evidence_pool:
        Optional global evidence pool for fallback lookups.
    user_profile:
        Optional user profile dict.
    outline:
        Lesson outline dict (used for fallback when handout/evidence not available).
    current_node_id:
        The outline node id being taught (used for fallback).
    recent_qa_context:
        Recent QA turns from the same outline node only.  Used for follow-up
        questions like "你刚才为什么这么说？"; must not contain other sections.

    Returns
    -------
    dict with keys: qa_status, question, answer, question_scope, evidence_status,
    targeted_search_used, search_plan, used_evidence, related_kg_node_ids,
    confusion_kg_node_ids, should_reinforce, advance_progress, trace.
    """
    t_start = time.time()

    # --- Determine fallback base ---
    _fallback_base = None

    def _get_fallback() -> dict[str, Any]:
        nonlocal _fallback_base
        if _fallback_base is None:
            if outline is not None and current_node_id is not None:
                _fallback_base = answer_interruption_question(
                    question=question,
                    outline=outline,
                    current_node_id=current_node_id,
                    user_profile=user_profile,
                )
            else:
                _fallback_base = {
                    "qa_status": "failed",
                    "question": question,
                    "answer": "答疑系统当前不可用。",
                    "question_scope": "in_current_section",
                    "evidence_status": "insufficient",
                    "targeted_search_used": False,
                    "search_plan": [],
                    "used_evidence": [],
                    "related_kg_node_ids": [],
                    "confusion_kg_node_ids": [],
                    "should_reinforce": True,
                    "advance_progress": False,
                    "trace": {
                        "mode": "qa_failed",
                        "uses_llm": False,
                        "uses_handout_section": False,
                        "uses_current_evidence": False,
                        "targeted_search_used": False,
                        "fallback_rule_based": True,
                    },
                }
        return _fallback_base

    # --- 0. Check prerequisites ---
    if not is_llm_configured(*_QA_API_KEY_NAMES):
        result = _get_fallback()
        result["trace"]["qa_status_detail"] = "no_llm_configured"
        return result

    node_evidence = node_evidence or {}
    current_handout_section = current_handout_section or {}
    current_outline_node = current_outline_node or {}
    recent_qa_context = recent_qa_context or []

    # --- Determine handout validity ---
    # Only llm_generated with non-empty key_points counts as valid handout.
    _has_valid_handout = _check_handout_valid(current_handout_section)
    _degraded_without_handout = not _has_valid_handout

    _prompt_dir = Path(__file__).resolve().parent / "prompts"

    # =====================================================================
    # Step 1: LLM QA Plan — judge evidence sufficiency + search_plan
    # =====================================================================

    plan_system = _load_prompt(_prompt_dir, "qa_plan.md")

    # Build current_evidence_summary (lightweight index for LLM consumption)
    evidence_summary = _build_evidence_index_summary(node_evidence)

    # Build handout section summary for plan step (exclude full explanations to save tokens)
    handout_summary_for_plan = _build_handout_summary(current_handout_section)

    plan_user = json.dumps({
        "question": question,
        "topic": topic,
        "current_outline_node": {
            "id": current_outline_node.get("id", ""),
            "title": current_outline_node.get("title", ""),
            "learning_objectives": current_outline_node.get("learning_objectives", []),
            "sequence_reason": current_outline_node.get("sequence_reason", ""),
        },
        "current_handout_section": handout_summary_for_plan,
        "current_evidence_summary": evidence_summary,
        "recent_qa_context": recent_qa_context,
        "wrong_answer_context": wrong_answer_context,
        "user_profile": user_profile or {},
    }, ensure_ascii=False, indent=2)

    t_plan_start = time.time()
    try:
        plan_raw = chat_completion(
            [
                {"role": "system", "content": plan_system},
                {"role": "user", "content": plan_user},
            ],
            temperature=0.2,
            max_tokens=2000,
            api_key_names=_QA_API_KEY_NAMES,
            base_url_names=_QA_BASE_URL_NAMES,
            model_names=_QA_MODEL_NAMES,
            response_format={"type": "json_object"},
        )
    except Exception as exc:
        result = _get_fallback()
        result["trace"]["qa_status_detail"] = f"plan_llm_call_failed: {str(exc)[:200]}"
        return result
    plan_seconds = round(time.time() - t_plan_start, 3)

    # --- Parse plan ---
    plan = _parse_json_output(plan_raw)
    if plan is None:
        result = _get_fallback()
        result["trace"]["qa_status_detail"] = "plan_json_parse_failed"
        result["trace"]["plan_raw_head"] = plan_raw[:200] if plan_raw else ""
        return result

    # --- Extract plan fields ---
    question_scope = str(plan.get("question_scope", "in_current_section")).strip()
    evidence_sufficient = bool(plan.get("evidence_sufficient", True))
    evidence_gap = str(plan.get("evidence_gap", "")).strip()
    search_plan: list[dict] = plan.get("search_plan", [])
    if not isinstance(search_plan, list):
        search_plan = []
    expected_post_search = str(plan.get("expected_evidence_status_after_search", "sufficient")).strip()
    should_reinforce = bool(plan.get("should_reinforce", True))
    related_kg_ids_from_plan = _normalize_str_list(plan.get("related_kg_node_ids_from_context", []))
    confusion_kg_ids_from_plan = _normalize_str_list(plan.get("confusion_kg_node_ids", []))

    # --- Validate and sanitize search_plan ---
    search_plan = _sanitize_search_plan(search_plan)

    # =====================================================================
    # Step 2: Python — fetch evidence detail + execute targeted search
    # =====================================================================

    # 2a. Fetch current node evidence detail (reuse Resource Factory helper)
    from backend.services.learning.resource_factory import get_evidence_detail

    evidence_detail = get_evidence_detail(
        node_evidence=node_evidence,
        requested_chunk_ids=node_evidence.get("evidence_chunk_ids", []) or [
            ch.get("chunk_id", "") for ch in node_evidence.get("evidence_chunks", [])
        ],
        requested_kg_node_ids=node_evidence.get("kg_node_ids", []),
        requested_kg_edge_ids=node_evidence.get("kg_edge_ids", []) if node_evidence.get("kg_edge_ids") else [],
        requested_question_ids=[],
        evidence_pool=evidence_pool,
        max_chars_per_chunk=1200,
    )

    # 2b. Execute targeted search (reuse Resource Factory helper)
    from backend.services.learning.resource_factory import run_targeted_evidence_search

    targeted_search_used = len(search_plan) > 0
    search_results: list[dict] = []
    extra_chunk_ids: set[str] = set()

    t_search_start = time.time()
    if search_plan:
        # Build minimal outline_node for search context
        search_node = {
            "id": current_outline_node.get("id", ""),
            "title": current_outline_node.get("title", ""),
        }
        for sq in search_plan:
            q_text = (sq.get("query") or "").strip()
            if not q_text:
                continue
            single_result = run_targeted_evidence_search(
                topic=topic,
                outline_node=search_node,
                search_queries=[{
                    "query": q_text,
                    "purpose": sq.get("purpose", ""),
                }],
                top_k_per_query=3,
            )
            if single_result:
                search_results.extend(single_result)
                for r in single_result[0].get("results", []):
                    cid = r.get("chunk_id", "")
                    if cid:
                        extra_chunk_ids.add(cid)
    search_seconds = round(time.time() - t_search_start, 3)

    # =====================================================================
    # Step 3: LLM QA Answer — generate answer from all evidence
    # =====================================================================

    answer_system = _load_prompt(_prompt_dir, "qa_answer.md")

    # Build full handout section content for answer generation
    handout_content_for_answer = current_handout_section.get("content", current_handout_section)

    answer_user = json.dumps({
        "question": question,
        "topic": topic,
        "current_outline_node": {
            "id": current_outline_node.get("id", ""),
            "title": current_outline_node.get("title", ""),
            "learning_objectives": current_outline_node.get("learning_objectives", []),
            "sequence_reason": current_outline_node.get("sequence_reason", ""),
        },
        "current_handout_section": handout_content_for_answer,
        "qa_plan": {
            "question_scope": question_scope,
            "evidence_sufficient": evidence_sufficient,
            "evidence_gap": evidence_gap,
            "search_plan": search_plan,
            "expected_evidence_status_after_search": expected_post_search,
        },
        "current_evidence_detail": evidence_detail,
        "search_results": search_results,
        "recent_qa_context": recent_qa_context,
        "wrong_answer_context": wrong_answer_context,
        "user_profile": user_profile or {},
    }, ensure_ascii=False, indent=2)

    t_answer_start = time.time()
    try:
        answer_raw = chat_completion(
            [
                {"role": "system", "content": answer_system},
                {"role": "user", "content": answer_user},
            ],
            temperature=0.3,
            max_tokens=3000,
            api_key_names=_QA_API_KEY_NAMES,
            base_url_names=_QA_BASE_URL_NAMES,
            model_names=_QA_MODEL_NAMES,
            response_format={"type": "json_object"},
        )
    except Exception as exc:
        result = _get_fallback()
        result["trace"]["qa_status_detail"] = f"answer_llm_call_failed: {str(exc)[:200]}"
        return result
    answer_seconds = round(time.time() - t_answer_start, 3)

    # --- Parse answer ---
    answer_parsed = _parse_json_output(answer_raw)
    if answer_parsed is None:
        result = _get_fallback()
        result["trace"]["qa_status_detail"] = "answer_json_parse_failed"
        result["trace"]["answer_raw_head"] = answer_raw[:200] if answer_raw else ""
        return result

    # --- Extract and normalize answer fields ---
    answer_text = str(answer_parsed.get("answer", "")).strip()
    if not answer_text:
        result = _get_fallback()
        result["trace"]["qa_status_detail"] = "answer_empty"
        return result

    answer_scope = str(answer_parsed.get("question_scope", question_scope)).strip()
    evidence_status = str(answer_parsed.get("evidence_status", expected_post_search)).strip()
    ts_used = bool(answer_parsed.get("targeted_search_used", targeted_search_used))

    # --- Normalize used_evidence ---
    raw_used_evidence = answer_parsed.get("used_evidence", [])
    if not isinstance(raw_used_evidence, list):
        raw_used_evidence = []
    used_evidence = _normalize_used_evidence(raw_used_evidence)

    # --- Collect evidence ids ---
    answer_related_kg_ids = _normalize_str_list(answer_parsed.get("related_kg_node_ids", []))
    answer_confusion_kg_ids = _normalize_str_list(answer_parsed.get("confusion_kg_node_ids", []))

    # Merge with plan-level ids
    all_related_kg_ids = _dedup_preserve_order(
        answer_related_kg_ids + related_kg_ids_from_plan
    )
    all_confusion_kg_ids = _dedup_preserve_order(
        answer_confusion_kg_ids + confusion_kg_ids_from_plan
    )

    # =====================================================================
    # Step 4: Build final result
    # =====================================================================

    total_seconds = round(time.time() - t_start, 3)

    # Guard evidence_status: if degraded without handout and no search results,
    # evidence_status cannot be "sufficient"
    _final_evidence_status = evidence_status
    if _degraded_without_handout and not ts_used and _final_evidence_status == "sufficient":
        _final_evidence_status = "partially_supported"

    result = {
        "qa_status": "llm_generated",
        "question": question,
        "answer": answer_text,
        "question_scope": answer_scope,
        "evidence_status": _final_evidence_status,
        "targeted_search_used": ts_used,
        "degraded_without_handout": _degraded_without_handout,
        "search_plan": search_plan,
        "used_evidence": used_evidence,
        "related_kg_node_ids": all_related_kg_ids,
        "confusion_kg_node_ids": all_confusion_kg_ids,
        "should_reinforce": should_reinforce,
        "advance_progress": False,
        "trace": {
            "mode": "llm_qa_evidence_search",
            "uses_llm": True,
            "uses_handout_section": _has_valid_handout,
            "uses_current_evidence": True,
            "targeted_search_used": ts_used,
            "fallback_rule_based": False,
            "degraded_without_handout": _degraded_without_handout,
            "plan_seconds": plan_seconds,
            "search_seconds": search_seconds,
            "answer_seconds": answer_seconds,
            "total_seconds": total_seconds,
            "search_result_count": len(extra_chunk_ids),
            "search_result_chunk_ids": sorted(extra_chunk_ids),
            "recent_qa_context_count": len(recent_qa_context),
            "qa_status_detail": "llm_generated",
        },
    }

    return result


# ============================================================================
# Internal helpers
# ============================================================================


def _check_handout_valid(handout_section: dict) -> bool:
    """Return True only if handout_section is llm_generated with real content.

    A valid handout must have:
    - section_status == "llm_generated"
    - content.key_points is a non-empty list
    """
    if not handout_section or not isinstance(handout_section, dict):
        return False
    if handout_section.get("section_status") != "llm_generated":
        return False
    content = handout_section.get("content", {})
    if not isinstance(content, dict):
        return False
    key_points = content.get("key_points", [])
    if not isinstance(key_points, list) or len(key_points) == 0:
        return False
    return True


def _build_evidence_index_summary(node_evidence: dict) -> dict:
    """Build a lightweight evidence index summary for LLM consumption.

    Same pattern as Resource Factory's _build_evidence_index_summary,
    adapted for QA context.
    """
    index: dict[str, Any] = {
        "outline_node_id": node_evidence.get("outline_node_id", ""),
        "outline_node_title": node_evidence.get("outline_node_title", ""),
    }

    index["kg_node_ids"] = node_evidence.get("kg_node_ids", [])

    index["kg_nodes"] = [
        {"node_id": n.get("node_id", ""), "canonical_name": n.get("canonical_name", "")}
        for n in node_evidence.get("kg_nodes", [])
    ]

    index["kg_edges"] = [
        {
            "edge_id": e.get("edge_id", ""),
            "relation": e.get("relation", ""),
            "source_node_id": e.get("source_node_id", ""),
            "target_node_id": e.get("target_node_id", ""),
        }
        for e in node_evidence.get("kg_edges", [])
    ]

    index["evidence_chunks"] = [
        {
            "chunk_id": ch.get("chunk_id", ""),
            "title": ch.get("title", ""),
            "source_type": ch.get("source_type", ""),
            "text_excerpt": (ch.get("text_excerpt", "") or "")[:200],
        }
        for ch in node_evidence.get("evidence_chunks", [])
    ]

    index["counts"] = {
        "kg_nodes": len(node_evidence.get("kg_nodes", [])),
        "kg_edges": len(node_evidence.get("kg_edges", [])),
        "evidence_chunks": len(node_evidence.get("evidence_chunks", [])),
    }

    return index


def _build_handout_summary(handout_section: dict) -> dict:
    """Build a lightweight handout summary (headings only) for the plan step.

    The full handout content is provided in the answer step.
    This saves tokens in the plan step while still giving the LLM
    enough context to judge evidence sufficiency.
    """
    if not handout_section:
        return {}

    content = handout_section.get("content", handout_section)
    if not isinstance(content, dict):
        return {"note": "handout_section content not available as dict"}

    key_points = content.get("key_points", [])
    if not isinstance(key_points, list):
        key_points = []

    return {
        "title": handout_section.get("title", ""),
        "overview": str(content.get("overview", ""))[:500],
        "key_points_headings": [
            kp.get("heading", "") for kp in key_points if isinstance(kp, dict)
        ],
        "key_points_count": len(key_points),
        "has_clinical_example": bool(content.get("clinical_example", "")),
        "has_common_confusions": bool(content.get("common_confusions", [])),
        "common_confusions_headings": [
            cc.get("confusion", "") for cc in content.get("common_confusions", [])
            if isinstance(cc, dict)
        ],
    }


def _sanitize_search_plan(search_plan: list[dict]) -> list[dict]:
    """Sanitize and validate the search_plan from LLM output.

    - Max 3 queries
    - Each query must have non-empty query and purpose
    - query text capped at 80 chars
    """
    if not search_plan:
        return []

    cleaned: list[dict] = []
    for sq in search_plan[:3]:  # cap at 3
        if not isinstance(sq, dict):
            continue
        query_text = (sq.get("query") or "").strip()
        purpose = (sq.get("purpose") or "").strip()
        if not query_text or not purpose:
            continue
        cleaned.append({
            "query": query_text[:80],
            "purpose": purpose,
            "expected_evidence": str(sq.get("expected_evidence", "")).strip()[:200],
            "relation_to_context": str(sq.get("relation_to_context", "")).strip()[:200],
        })

    return cleaned


def _normalize_used_evidence(raw: list[dict]) -> list[dict]:
    """Normalize used_evidence list from LLM output."""
    result: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        chunk_id = str(item.get("chunk_id", "")).strip()
        if not chunk_id:
            continue
        result.append({
            "chunk_id": chunk_id,
            "source": str(item.get("source", "")).strip()[:200],
            "why_used": str(item.get("why_used", "")).strip()[:200],
        })
    return result


def _normalize_str_list(value: Any) -> list[str]:
    """Normalize a value to a list of non-empty strings."""
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def _dedup_preserve_order(items: list[str]) -> list[str]:
    """Deduplicate a list while preserving order."""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _load_prompt(prompt_dir: Path, filename: str) -> str:
    """Load a prompt file, or return a minimal fallback."""
    prompt_path = prompt_dir / filename
    if prompt_path.exists():
        return prompt_path.read_text(encoding="utf-8")
    return "你是一名口腔医学学习 QA 助手。请基于提供的证据材料输出严格 JSON 格式的答疑结果。"


def _parse_json_output(raw: str) -> dict | None:
    """Parse LLM JSON output, handling markdown code fences."""
    text = raw.strip()
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if not fence_match:
        fence_match = re.search(r"```(?:json)?\s*\n?(.*)$", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed
