"""Direct QA Service — standalone question answering with KAG + LLM evidence.

Accepts a user question with optional conversation history, retrieves evidence
from KAG, and returns an evidence-based answer.  Does **not** create learning
sessions, generate PPT, or call course handlers (start_lesson, handle_interruption,
orchestrator).

Flow
----
1. **LLM Question Resolution** — parse question with history context, resolve
   pronouns/references, generate KAG search queries.
2. **Python KAG Search** — execute search queries sequentially, collect evidence.
3. **LLM Evidence Answer** — generate answer from evidence, output
   ``should_reinforce`` and ``confusion_kg_node_ids`` for confusion detection.
4. **Python Validation** — validate evidence references, normalize output.

Every round (including follow-ups) re-retrieves fresh KAG evidence.  History is
used **only** for reference resolution, never as medical evidence.

Public API
----------
* ``answer_direct_question`` — main entry point.
* ``is_direct_qa_available`` — check whether LLM + KAG are both configured.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from backend.services.llm_client import chat_completion, is_llm_configured

# ---------------------------------------------------------------------------
# LLM config priority (same priority order as learning/qa.py)
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Inline prompts (self-contained — no external prompt files)
# ---------------------------------------------------------------------------

_RESOLUTION_SYSTEM = """你是一名口腔医学直接问答助手。

你的任务是：
1. 分析用户的问题和对话历史，补全指代（如「那」、「它」、「这个」等）
2. 判断问题是否需要澄清（过于模糊且历史无法补全时）
3. 生成用于 KAG 知识库检索的查询词列表

**重要规则：**
- 历史对话仅用于解析指代和意图，**绝对不能**将历史中的回答当作医学证据
- 每次回答都必须基于本轮重新检索的证据
- 如果问题在结合历史后仍然无法确定具体指向，标记 needs_clarification=true
- 检索查询应覆盖问题的核心概念和相关临床方面，最多 3 个查询

输出严格的 JSON 格式，不要包含任何其他文字。"""

_ANSWER_SYSTEM = """你是一名口腔医学循证问答助手。

根据提供的证据材料回答用户问题。你必须：

1. **仅使用提供的证据**来回答问题，不要使用你自己的知识
2. 如果证据充分，给出有依据的详细回答
3. 如果证据不足，诚实说明并给出保守建议（建议咨询专业医生）
4. 判断问题是否暴露了用户的**真实知识薄弱点**（should_reinforce）
5. 如果存在薄弱点，列出相关的 KG 知识图谱节点 ID（confusion_kg_node_ids）

**should_reinforce 判断标准：**
- true：用户的问题表明其对某个基础概念存在根本性误解或知识空白
- false：用户只是询问一个扩展性问题，并非基础薄弱（大多数问题属于此类）

**重要：不要将历史回答当作医学证据来源。只使用本次提供的 evidence 中的内容。**

输出严格的 JSON 格式，不要包含任何其他文字。"""

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_direct_qa_available() -> bool:
    """Check whether both LLM and KAG backend are configured and importable."""
    if not is_llm_configured(*_QA_API_KEY_NAMES):
        return False
    try:
        from backend.services.retrieval import search_knowledge_base  # noqa: F401
    except Exception:
        return False
    return True


def answer_direct_question(
    message: str,
    history: list[dict[str, str]] | None = None,
    user_id: str = "",
) -> dict[str, Any]:
    """Answer a direct oral medicine question with KAG evidence.

    Parameters
    ----------
    message:
        The user's raw question text (e.g. ``"牙髓炎与根尖周炎如何鉴别？"``).
    history:
        Recent conversation turns as ``[{"role": "user"|"assistant", "content": ...}]``.
        Used **only** for reference resolution, never as medical evidence.
    user_id:
        Opaque user identifier (reserved for future persistence; currently
        only logged in trace).

    Returns
    -------
    dict with keys:
        ``answer``: str — the final answer text.
        ``resolved_question``: str — the question after reference resolution.
        ``conversation_context_used``: bool — whether history was used.
        ``used_evidence``: list[dict] — KAG chunks actually cited in the answer.
        ``evidence_status``: str — ``"sufficient"`` | ``"insufficient"`` | ``"none"``.
        ``should_reinforce``: bool — whether a knowledge gap was detected.
        ``confusion_kg_node_ids``: list[str] — KG node ids for confusion (validated).
        ``trace``: dict — timing, status, and diagnostic info.
    """
    t_start = time.time()
    history = _normalize_history(history or [])

    # -- Build fallback result template --
    def _unavailable_result(detail: str) -> dict[str, Any]:
        return {
            "answer": "答疑系统当前不可用，请稍后重试。",
            "resolved_question": message.strip(),
            "conversation_context_used": False,
            "used_evidence": [],
            "evidence_status": "none",
            "should_reinforce": False,
            "confusion_kg_node_ids": [],
            "trace": {
                "mode": "unavailable",
                "detail": detail,
                "total_seconds": round(time.time() - t_start, 3),
            },
        }

    # -- 0. Prerequisites --
    if not is_llm_configured(*_QA_API_KEY_NAMES):
        return _unavailable_result("no_llm_configured")

    try:
        from backend.services.retrieval import search_knowledge_base
    except Exception as exc:
        return _unavailable_result(f"kag_import_failed: {exc}")

    message = message.strip()
    if not message:
        return _unavailable_result("empty_message")

    # =====================================================================
    # Step 1: LLM Question Resolution + Search Planning
    # =====================================================================

    resolution_user = json.dumps({
        "question": message,
        "history": history,
    }, ensure_ascii=False, indent=2)

    t_resolve_start = time.time()
    try:
        resolution_raw = chat_completion(
            [
                {"role": "system", "content": _RESOLUTION_SYSTEM},
                {"role": "user", "content": resolution_user},
            ],
            temperature=0.2,
            max_tokens=1500,
            api_key_names=_QA_API_KEY_NAMES,
            base_url_names=_QA_BASE_URL_NAMES,
            model_names=_QA_MODEL_NAMES,
            response_format={"type": "json_object"},
        )
    except Exception as exc:
        result = _unavailable_result(f"resolution_llm_failed: {exc}")
        return result
    resolve_seconds = round(time.time() - t_resolve_start, 3)

    resolution = _parse_json_output(resolution_raw)
    if resolution is None:
        result = _unavailable_result("resolution_json_parse_failed")
        result["trace"]["resolution_raw_head"] = resolution_raw[:200] if resolution_raw else ""
        return result

    # -- Extract resolution fields --
    resolved_question = str(resolution.get("resolved_question", message)).strip() or message.strip()
    conversation_context_used = bool(resolution.get("conversation_context_used", False))
    needs_clarification = bool(resolution.get("needs_clarification", False))
    clarification_message = str(resolution.get("clarification_message", "")).strip()

    search_queries: list[dict] = resolution.get("search_queries", [])
    if not isinstance(search_queries, list):
        search_queries = []

    # -- Handle clarification needed --
    if needs_clarification:
        return {
            "answer": clarification_message or "抱歉，我不太确定您具体想问什么，能否再详细描述一下您的问题？",
            "resolved_question": resolved_question,
            "conversation_context_used": conversation_context_used,
            "used_evidence": [],
            "evidence_status": "none",
            "should_reinforce": False,
            "confusion_kg_node_ids": [],
            "trace": {
                "mode": "clarification_needed",
                "uses_llm": True,
                "resolve_seconds": resolve_seconds,
                "total_seconds": round(time.time() - t_start, 3),
            },
        }

    # =====================================================================
    # Step 2: Python KAG Search (sequential, no parallel retrieval)
    # =====================================================================

    t_search_start = time.time()
    all_search_results: list[dict] = []
    all_chunk_ids: set[str] = set()
    all_core_node_ids: set[str] = set()

    # Use resolved_question as a fallback query if no search_queries provided
    queries_to_run = _sanitize_search_queries(search_queries)
    if not queries_to_run:
        queries_to_run = [{"query": resolved_question, "purpose": "核心问题检索"}]

    for sq in queries_to_run[:3]:
        query_text = sq.get("query", "").strip()
        if not query_text:
            continue
        try:
            chunks = search_knowledge_base(query_text, top_k=5)
        except Exception:
            chunks = []

        chunk_dicts: list[dict] = []
        for ch in chunks:
            cid = ch.id or ""
            if cid and not cid.startswith("chunk:"):
                cid = f"chunk:{cid}"
            chunk_dicts.append({
                "chunk_id": cid,
                "title": ch.title or "",
                "source": ch.source or "",
                "source_type": ch.source_type or "",
                "score": round(float(ch.score), 4) if ch.score else 0.0,
                "text_excerpt": (ch.text or "")[:800],
            })
            if cid:
                all_chunk_ids.add(cid)
            # Collect core_node_ids from chunk metadata for KG-ID whitelist
            core_ids = ch.metadata.get("core_node_ids", [])
            if isinstance(core_ids, list):
                for nid in core_ids:
                    nid_str = str(nid).strip() if nid else ""
                    if nid_str:
                        all_core_node_ids.add(nid_str)

        all_search_results.append({
            "query": query_text,
            "purpose": sq.get("purpose", ""),
            "results": chunk_dicts,
        })

    search_seconds = round(time.time() - t_search_start, 3)

    # =====================================================================
    # Step 3: LLM Evidence Answer + Confusion Detection
    # =====================================================================

    # Flatten evidence for LLM consumption
    flat_evidence: list[dict] = []
    for sr in all_search_results:
        for r in sr.get("results", []):
            if r.get("chunk_id") not in {e.get("chunk_id") for e in flat_evidence}:
                flat_evidence.append(r)

    answer_user = json.dumps({
        "question": message,
        "resolved_question": resolved_question,
        "evidence": flat_evidence,
        "allowed_kg_node_ids": sorted(all_core_node_ids),
        "history_context_note": (
            "以下历史对话仅用于理解问题上下文，不能作为医学证据使用。"
            if conversation_context_used else ""
        ),
    }, ensure_ascii=False, indent=2)

    t_answer_start = time.time()
    try:
        answer_raw = chat_completion(
            [
                {"role": "system", "content": _ANSWER_SYSTEM},
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
        result = _unavailable_result(f"answer_llm_failed: {exc}")
        result["trace"]["resolve_seconds"] = resolve_seconds
        result["trace"]["search_seconds"] = search_seconds
        result["trace"]["search_result_count"] = len(all_chunk_ids)
        return result
    answer_seconds = round(time.time() - t_answer_start, 3)

    answer_parsed = _parse_json_output(answer_raw)
    if answer_parsed is None:
        result = _unavailable_result("answer_json_parse_failed")
        result["trace"]["answer_raw_head"] = answer_raw[:200] if answer_raw else ""
        result["trace"]["resolve_seconds"] = resolve_seconds
        result["trace"]["search_seconds"] = search_seconds
        result["trace"]["search_result_count"] = len(all_chunk_ids)
        return result

    # -- Extract answer fields --
    answer_text = str(answer_parsed.get("answer", "")).strip()
    if not answer_text:
        result = _unavailable_result("answer_empty")
        result["trace"]["resolve_seconds"] = resolve_seconds
        result["trace"]["search_seconds"] = search_seconds
        result["trace"]["search_result_count"] = len(all_chunk_ids)
        return result

    evidence_status = str(answer_parsed.get("evidence_status", "insufficient")).strip()
    should_reinforce = bool(answer_parsed.get("should_reinforce", False))
    raw_confusion_kg_ids = answer_parsed.get("confusion_kg_node_ids", [])
    raw_used_evidence = answer_parsed.get("used_evidence", [])

    # =====================================================================
    # Step 4: Python Validation & Normalization
    # =====================================================================

    # Validate used_evidence — only allow chunk_ids from actual KAG results
    used_evidence = _normalize_used_evidence(raw_used_evidence, all_chunk_ids)

    # Validate confusion_kg_node_ids — must be non-empty strings AND
    # belong to the current round's allowed core_node_ids from evidence chunks.
    raw_ids = _normalize_str_list(raw_confusion_kg_ids)
    confusion_kg_node_ids = [nid for nid in raw_ids if nid in all_core_node_ids]

    # Sanity: if no evidence found but LLM claims sufficient, downgrade
    if len(all_chunk_ids) == 0 and evidence_status == "sufficient":
        evidence_status = "insufficient"

    # Sanity: if should_reinforce but no KG ids, keep the signal but note
    if should_reinforce and not confusion_kg_node_ids:
        # LLM says reinforce but gave no KG ids — still respect the signal
        # but trace the inconsistency
        pass

    # =====================================================================
    # Build final result
    # =====================================================================

    total_seconds = round(time.time() - t_start, 3)

    result: dict[str, Any] = {
        "answer": answer_text,
        "resolved_question": resolved_question,
        "conversation_context_used": conversation_context_used,
        "used_evidence": used_evidence,
        "evidence_status": evidence_status,
        "should_reinforce": should_reinforce,
        "confusion_kg_node_ids": confusion_kg_node_ids,
        "trace": {
            "mode": "direct_qa_evidence",
            "uses_llm": True,
            "uses_kag": True,
            "resolve_seconds": resolve_seconds,
            "search_seconds": search_seconds,
            "answer_seconds": answer_seconds,
            "total_seconds": total_seconds,
            "search_queries_count": len(queries_to_run),
            "search_result_count": len(all_chunk_ids),
            "search_result_chunk_ids": sorted(all_chunk_ids),
            "history_turns": len(history),
            "history_used": conversation_context_used,
            "user_id": user_id,
        },
    }

    # =====================================================================
    # Step 5: Persist to learning profile (best-effort, never blocks answer)
    # =====================================================================
    if (
        user_id
        and result["should_reinforce"] is True
        and result["confusion_kg_node_ids"]
    ):
        _persist_qa_result(result, user_id)

    return result


# ============================================================================
# Internal helpers
# ============================================================================


def _normalize_history(raw: list[dict[str, str]]) -> list[dict[str, str]]:
    """Keep only role + content fields; max 8 turns."""
    cleaned: list[dict[str, str]] = []
    for turn in raw[-8:]:
        if not isinstance(turn, dict):
            continue
        role = str(turn.get("role", "")).strip()
        content = str(turn.get("content", "")).strip()
        if role in ("user", "assistant") and content:
            cleaned.append({"role": role, "content": content})
    return cleaned


def _sanitize_search_queries(raw: list[dict]) -> list[dict]:
    """Sanitize search queries: max 3, each needs query + purpose."""
    if not raw:
        return []
    cleaned: list[dict] = []
    for sq in raw[:3]:
        if not isinstance(sq, dict):
            continue
        query_text = (sq.get("query") or "").strip()
        purpose = (sq.get("purpose") or "").strip()
        if not query_text:
            continue
        cleaned.append({
            "query": query_text[:200],
            "purpose": purpose[:200],
        })
    return cleaned


def _normalize_used_evidence(raw: list[dict], allowed_chunk_ids: set[str]) -> list[dict]:
    """Normalize used_evidence, filtering to only allowed chunk ids."""
    if not isinstance(raw, list):
        return []
    result: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        chunk_id = str(item.get("chunk_id", "")).strip()
        if not chunk_id or chunk_id in seen:
            continue
        # Only allow chunk_ids that came from actual KAG results
        if chunk_id not in allowed_chunk_ids:
            continue
        seen.add(chunk_id)
        result.append({
            "chunk_id": chunk_id,
            "source": str(item.get("source", ""))[:200],
            "why_used": str(item.get("why_used", ""))[:200],
        })
    return result


def _normalize_str_list(value: Any) -> list[str]:
    """Normalize to a list of non-empty, deduplicated strings."""
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    result: list[str] = []
    for item in value:
        text = str(item).strip() if item else ""
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


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


def _persist_qa_result(result: dict[str, Any], user_id: str) -> None:
    """Attempt to persist a Direct QA result via the persistence adapter.

    Only called when pre-conditions are already met (user_id non-empty,
    *should_reinforce* is ``True``, valid *confusion_kg_node_ids* exist).
    Failures are recorded in ``result["trace"]`` and **never** propagate —
    the answer is always preserved.

    Parameters
    ----------
    result:
        The fully-built Direct QA result dict (mutated in-place to add
        persistence trace entries).
    user_id:
        Non-empty user identifier.
    """
    try:
        from backend.services.qa.direct_qa_persistence import \
            update_profile_from_qa  # noqa: F811
        from backend.services.learning.memory import LearningMemory
    except Exception as exc:
        result["trace"]["persistence"] = f"failed: import error - {exc}"
        return

    memory = None
    try:
        memory = LearningMemory()
        profile = memory.get_or_create_profile(user_id)

        qa_payload: dict[str, Any] = {
            "should_reinforce": result["should_reinforce"],
            "confusion_kg_node_ids": result["confusion_kg_node_ids"],
            "question": result.get("resolved_question", ""),
            "resolved_question": result["resolved_question"],
            "used_evidence": result["used_evidence"],
        }

        source_ref = f"dqa-{int(time.time())}"

        persistence_result = update_profile_from_qa(
            memory=memory,
            user_id=user_id,
            profile=profile,
            qa_result=qa_payload,
            source_ref=source_ref,
        )

        result["trace"]["persistence"] = persistence_result["trace"]
    except Exception as exc:
        result["trace"]["persistence"] = f"failed: {exc}"
    finally:
        if memory is not None:
            try:
                memory._conn.close()
            except Exception:
                pass
