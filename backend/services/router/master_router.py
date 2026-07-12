"""Master router — classifies user intent and dispatches to existing services.

This module is the ONLY new architectural layer; it does NOT modify, fix, or
duplicate any downstream learning / study-plan / literature logic.

Architecture
------------
``MasterRouter`` receives a user message + context and:

1. If ``context.plan_item_id`` is present → deterministic lookup (no LLM).
2. Otherwise → constrained LLM classification into one of four intents.
3. Dispatches to the existing downstream service and returns a unified
   ``{route, final_result, next_action}`` response.

LLM failures / invalid output ALWAYS fall back to ``clarification`` — never
to a keyword-based heuristic or an auto‑started lesson.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date
from typing import Any, Callable

from backend.services.llm_client import (
    LLMConfigurationError,
    LLMRequestError,
    chat_completion,
    is_llm_configured,
)
from backend.services.learning.study_plan import recommend_study_plan
from backend.services.literature.agent import run_literature_search

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_INTENTS: frozenset[str] = frozenset(
    {"learning", "study_plan", "literature", "clarification"}
)

INTENT_TO_TARGET: dict[str, str] = {
    "learning": "learning_orchestrator",
    "study_plan": "study_plan",
    "literature": "frontier_literature",
    "clarification": "none",
}

INTENT_TO_NEXT_ACTION: dict[str, str] = {
    "learning": "start_session",
    "study_plan": "show_recommendations",
    "literature": "show_literature",
    "clarification": "select_plan",
}

CLASSIFIER_SYSTEM_PROMPT = (
    "你是一个学习需求分类器。根据用户的消息内容，判断用户意图属于以下四类中的哪一类：\n"
    "\n"
    "- learning: 用户想学习某个知识主题、了解某个概念、开始学习某项内容\n"
    "- study_plan: 用户想获取学习计划推荐、想知道今天学什么、需要安排学习任务\n"
    "- literature: 用户想检索论文文献、查找最新研究、翻译文章、了解科研进展\n"
    "- clarification: 用户意图模糊不清，或无法判断其具体需求\n"
    "\n"
    "请严格只返回一个 JSON 对象，格式如下，不要附带任何其他文本：\n"
    '{"intent": "<learning|study_plan|literature|clarification>", "reason": "<简短中文说明>"}'
)

PLAN_ITEM_MATCHER_SYSTEM_PROMPT = (
    "你是一个学习计划匹配器。用户表达了学习意愿，下面列出了用户当前未完成的"
    "学习计划项候选列表。请根据用户消息判断最匹配哪一项。\n\n"
    "请严格只返回一个 JSON 对象，格式如下，不要附带任何其他文本：\n"
    '{"plan_item_id": "<匹配的ID或null>", "reason": "<简短中文说明>"}\n\n'
    "如果没有任何候选匹配用户的意图，plan_item_id 返回 null。"
)

# Fields exposed to the frontend in a select_plan whitelist response.
CANDIDATE_WHITELIST_FIELDS: tuple[str, ...] = (
    "plan_item_id",
    "title",
    "linked_course_title",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_classifier_messages(
    message: str,
    history: list[dict[str, Any]] | None,
) -> list[dict[str, str]]:
    """Build the message list for the intent classifier."""
    messages: list[dict[str, str]] = [
        {"role": "system", "content": CLASSIFIER_SYSTEM_PROMPT},
    ]
    if history:
        for entry in history[-6:]:  # keep context window small
            role = entry.get("role", "user")
            content = entry.get("content", "")
            if isinstance(content, str) and content.strip():
                messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": message})
    return messages


def _validate_route(parsed: dict[str, Any]) -> dict[str, Any] | None:
    """Validate a classifier JSON output. Returns the route dict or None."""
    if not isinstance(parsed, dict):
        return None
    intent = str(parsed.get("intent", "")).strip().lower()
    if intent not in VALID_INTENTS:
        return None
    reason = str(parsed.get("reason", "")).strip()
    return {
        "intent": intent,
        "target": INTENT_TO_TARGET[intent],
        "reason": reason or "无法提供具体理由。",
        "source": "llm_router",
    }


def _build_candidate_whitelist(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a field-whitelisted copy of each candidate for the frontend.

    Maps the internal ``id`` field to ``plan_item_id`` for the frontend, and
    strips all other internal fields (e.g. ``plan_date``, ``status``).
    """
    return [
        {
            "plan_item_id": item.get("id"),
            "title": item.get("title"),
            "linked_course_title": item.get("linked_course_title"),
        }
        for item in items
    ]


# ---------------------------------------------------------------------------
# MasterRouter
# ---------------------------------------------------------------------------

class MasterRouter:
    """Classify user intent and dispatch to existing downstream services.

    Parameters
    ----------
    learning_orchestrator:
        The application-level ``LearningOrchestrator`` instance (for session
        start and plan-item lookups).
    classifier:
        Optional callable ``(message, history, context) -> dict`` that
        replaces the default LLM classifier.  Injected during tests so no
        external LLM call is made.
    literature_service:
        Optional callable replacing :func:`run_literature_search`.
    study_plan_service:
        Optional callable replacing :func:`recommend_study_plan`.
    """

    def __init__(
        self,
        learning_orchestrator: Any,
        *,
        classifier: Callable[..., dict[str, Any]] | None = None,
        literature_service: Callable[..., Any] | None = None,
        study_plan_service: Callable[..., Any] | None = None,
        plan_matcher: Callable[..., str | None] | None = None,
    ) -> None:
        self.learning_orchestrator = learning_orchestrator
        self._classifier = classifier
        self._literature_service = literature_service or run_literature_search
        self._study_plan_service = study_plan_service or recommend_study_plan
        self._plan_matcher = plan_matcher

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def route(
        self,
        message: str,
        user_id: str = "demo",
        history: list[dict[str, Any]] | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Classify *message* and dispatch to the appropriate downstream service.

        Returns a dict with ``ok``, ``route``, ``final_result``, and
        ``next_action``.
        """
        ctx = context or {}

        # --- explicit plan_item_id — deterministic, no LLM -------------------
        plan_item_id = ctx.get("plan_item_id")
        if plan_item_id:
            return await self._handle_plan_item(user_id, str(plan_item_id))

        session_id = str(ctx.get("session_id") or "").strip()
        if session_id and self._is_continue_message(message):
            return await self._handle_session_continue(user_id, session_id)

        # --- LLM classification ----------------------------------------------
        route = await self._classify(message, history, ctx)

        # --- dispatch --------------------------------------------------------
        if route["intent"] == "learning":
            return await self._handle_learning(user_id, message, route)
        elif route["intent"] == "study_plan":
            return await self._handle_study_plan(user_id, message, route)
        elif route["intent"] == "literature":
            return await self._handle_literature(message, route)
        else:
            return self._respond_clarification(message, route)

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------

    async def _classify(
        self,
        message: str,
        history: list[dict[str, Any]] | None,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Run the (injectable) classifier.

        Falls back to ``clarification`` on any failure.
        """
        if self._classifier is not None:
            try:
                result = self._classifier(message, history, context)
                if isinstance(result, dict) and result.get("intent") in VALID_INTENTS:
                    return result
            except Exception:
                logger.exception("Injected classifier failed")
            return self._clarification_route("分类器返回无效结果。")

        # --- default: LLM classifier -----------------------------------------
        if not is_llm_configured():
            return self._clarification_route("LLM 未配置，无法进行意图分类。")

        messages = _build_classifier_messages(message, history)
        try:
            raw = await asyncio.to_thread(
                chat_completion,
                messages,
                temperature=0.0,
                max_tokens=256,
                response_format={"type": "json_object"},
            )
        except (LLMConfigurationError, LLMRequestError, OSError, ValueError) as exc:
            logger.warning("LLM classifier failed: %s", exc)
            return self._clarification_route(f"意图分类失败：{exc}")

        # Parse and validate
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("LLM classifier returned invalid JSON: %s", raw[:200])
            return self._clarification_route("分类器返回了无效的 JSON。")

        validated = _validate_route(parsed)
        if validated is None:
            logger.warning("LLM classifier returned invalid route: %s", parsed)
            return self._clarification_route("分类器返回了无效的意图值。")

        return validated

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def _handle_plan_item(
        self, user_id: str, plan_item_id: str
    ) -> dict[str, Any]:
        """Deterministic plan-item lookup — NO LLM involved."""
        route_base = {
            "intent": "learning",
            "target": INTENT_TO_TARGET["learning"],
            "source": "explicit_plan_item",
        }

        # 1. Read the real plan item (raises KeyError if not found / wrong user)
        try:
            item = self.learning_orchestrator.memory.get_study_plan_item(
                plan_item_id, user_id=user_id
            )
        except KeyError:
            return {
                "ok": False,
                "route": {
                    **route_base,
                    "reason": f"未找到计划任务 {plan_item_id}，或该任务不属于当前用户。",
                },
                "final_result": None,
                "next_action": None,
                "error": f"Study plan item not found or access denied: {plan_item_id}",
            }

        # 2. Verify status
        if item.get("status") != "open":
            return {
                "ok": False,
                "route": {
                    **route_base,
                    "reason": f"计划任务「{item.get('title', plan_item_id)}」状态为 {item.get('status')}，无法开始学习。",
                },
                "final_result": None,
                "next_action": None,
                "error": f"Plan item is not open: status={item.get('status')}",
            }

        # 3. Resolve topic
        topic = (
            str(item.get("linked_course_title") or "").strip()
            or str(item.get("title") or "").strip()
        )
        if not topic:
            return {
                "ok": False,
                "route": {
                    **route_base,
                    "reason": f"计划任务「{item.get('title', plan_item_id)}」没有可解析的学习主题。",
                },
                "final_result": None,
                "next_action": None,
                "error": "Plan item has no resolvable topic.",
            }

        # 4. Start lesson
        route_base["reason"] = f"根据计划任务「{topic}」启动学习。"
        return await self._start_learning(user_id, topic, route_base)

    async def _handle_learning(
        self, user_id: str, message: str, route: dict[str, Any]
    ) -> dict[str, Any]:
        """Candidate matching against open study-plan items.

        0 candidates / list-open failure → free study
        1 candidate                          → deterministic ``_handle_plan_item``
        2+ candidates                        → LLM-constrained matching, fallback ``select_plan``
        """
        # 1. List open candidates from existing data layer
        try:
            open_items = self.learning_orchestrator.memory.list_open_study_plan_items(
                user_id
            )
        except Exception:
            logger.exception("list_open_study_plan_items failed")
            return await self._start_learning(user_id, message.strip(), route)

        # 2. Zero candidates → free study
        if not open_items:
            return await self._start_learning(user_id, message.strip(), route)

        # 3. Single candidate → deterministic dispatch
        if len(open_items) == 1:
            return await self._handle_plan_item(user_id, open_items[0]["id"])

        # 4. Multiple candidates → constrained LLM matching
        matched_id = await self._match_plan_item(message, open_items)
        if matched_id is not None:
            return await self._handle_plan_item(user_id, matched_id)

        # 5. No viable match → return select_plan with whitelist
        return self._respond_select_plan(open_items, route)

    async def _handle_study_plan(
        self, user_id: str, message: str, route: dict[str, Any]
    ) -> dict[str, Any]:
        """Recommend study plans based on user's current learning progress."""
        try:
            result = await asyncio.to_thread(
                self._study_plan_service,
                self.learning_orchestrator.memory,
                user_id=user_id,
                plan_date=date.today().isoformat(),
            )
        except Exception as exc:
            logger.exception("study_plan_service failed")
            return {
                "ok": False,
                "route": route,
                "final_result": None,
                "next_action": INTENT_TO_NEXT_ACTION[route["intent"]],
                "error": f"学习计划推荐失败：{exc}",
            }

        return {
            "ok": True,
            "route": route,
            "final_result": result,
            "next_action": INTENT_TO_NEXT_ACTION[route["intent"]],
        }

    async def _handle_literature(
        self, message: str, route: dict[str, Any]
    ) -> dict[str, Any]:
        """Call the existing literature search service."""
        query = message.strip()
        try:
            result = await self._literature_service(query)
        except Exception as exc:
            logger.exception("Literature search failed")
            return {
                "ok": False,
                "route": route,
                "final_result": None,
                "next_action": INTENT_TO_NEXT_ACTION[route["intent"]],
                "error": f"文献检索失败：{exc}",
            }

        return {
            "ok": result.get("ok", False),
            "route": route,
            "final_result": result,
            "next_action": INTENT_TO_NEXT_ACTION[route["intent"]],
        }

    async def _match_plan_item(
        self,
        message: str,
        candidates: list[dict[str, Any]],
    ) -> str | None:
        """Ask the (injectable) LLM matcher to pick one candidate by ID.

        Returns the matched ``id``, or ``None`` when no match is found, the
        LLM is unavailable, or the returned ID is not in the server-side set
        (hallucination guard).
        """
        # --- injected matcher (tests) ---
        if self._plan_matcher is not None:
            try:
                result = self._plan_matcher(message, candidates)
                if isinstance(result, str) and result.strip():
                    result = result.strip()
                    valid_ids = {c["id"] for c in candidates}
                    if result in valid_ids:
                        return result
                    logger.warning(
                        "Injected plan_matcher returned ID not in candidate set: %s",
                        result,
                    )
                return None
            except Exception:
                logger.exception("Injected plan_matcher failed")
                return None

        # --- LLM matcher ---
        if not is_llm_configured():
            return None

        # Build compact candidate list for the prompt
        candidate_lines: list[str] = []
        for c in candidates:
            cid = c.get("id", "")
            title = c.get("title", "") or c.get("linked_course_title", "") or "(无标题)"
            candidate_lines.append(f"- id: {cid}, 标题: {title}")

        matcher_messages: list[dict[str, str]] = [
            {"role": "system", "content": PLAN_ITEM_MATCHER_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"用户消息：{message}\n\n"
                    f"候选计划项列表：\n" + "\n".join(candidate_lines)
                ),
            },
        ]

        try:
            raw = await asyncio.to_thread(
                chat_completion,
                matcher_messages,
                temperature=0.0,
                max_tokens=128,
                response_format={"type": "json_object"},
            )
        except (LLMConfigurationError, LLMRequestError, OSError, ValueError) as exc:
            logger.warning("Plan-item matcher LLM call failed: %s", exc)
            return None

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Plan-item matcher returned invalid JSON: %s", raw[:200])
            return None

        if not isinstance(parsed, dict):
            return None

        llm_id = parsed.get("plan_item_id")
        if llm_id is None or not isinstance(llm_id, str) or not llm_id.strip():
            return None

        llm_id = llm_id.strip()
        valid_ids = {c["id"] for c in candidates}
        if llm_id not in valid_ids:
            logger.warning(
                "Plan-item matcher returned ID not in candidate set: %s (valid: %s)",
                llm_id,
                valid_ids,
            )
            return None

        return llm_id

    def _respond_select_plan(
        self,
        candidates: list[dict[str, Any]],
        route: dict[str, Any],
    ) -> dict[str, Any]:
        """Return a ``select_plan`` response with field-whitelisted candidates."""
        return {
            "ok": True,
            "route": route,
            "final_result": {
                "candidates": _build_candidate_whitelist(candidates),
                "message": f"找到 {len(candidates)} 个未完成的学习计划任务，请选择一项开始学习。",
            },
            "next_action": "select_plan",
        }

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    async def _start_learning(
        self, user_id: str, topic: str, route: dict[str, Any]
    ) -> dict[str, Any]:
        """Call the existing ``start_lesson`` and wrap the result."""
        try:
            result = await asyncio.to_thread(
                self.learning_orchestrator.start_lesson,
                user_id=user_id,
                topic=topic,
            )
        except Exception as exc:
            logger.exception("start_lesson failed for topic=%s", topic)
            return {
                "ok": False,
                "route": route,
                "final_result": None,
                "next_action": INTENT_TO_NEXT_ACTION[route["intent"]],
                "error": f"启动学习失败：{exc}",
            }

        return {
            "ok": True,
            "route": route,
            "final_result": result,
            "next_action": INTENT_TO_NEXT_ACTION[route["intent"]],
        }

    async def _handle_session_continue(
        self,
        user_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        route = {
            "intent": "learning",
            "target": INTENT_TO_TARGET["learning"],
            "reason": "继续当前学习 session。",
            "source": "session_context",
        }
        try:
            result = await asyncio.to_thread(
                self.learning_orchestrator.continue_to_next_section,
                session_id,
            )
        except Exception as exc:
            logger.exception("continue_to_next_section failed for session=%s", session_id)
            return {
                "ok": False,
                "route": route,
                "final_result": None,
                "next_action": "continue_session",
                "error": f"继续学习失败：{exc}",
            }

        if result.get("lesson_completed") and result.get("stage_exam"):
            exam = result.get("stage_exam") or {}
            question_count = len(exam.get("questions") or [])
            result = {
                **result,
                "answer": f"本阶段知识点已学完，已生成阶段卷，共 {question_count} 题。请在下面作答。",
            }

        return {
            "ok": True,
            "route": route,
            "final_result": result,
            "next_action": (
                "show_stage_exam"
                if result.get("lesson_completed") and result.get("stage_exam")
                else "continue_session"
            ),
        }

    @staticmethod
    def _is_continue_message(message: str) -> bool:
        text = "".join(str(message or "").lower().split())
        if not text:
            return False
        markers = (
            "继续",
            "下一节",
            "下一页",
            "下一个",
            "学完",
            "讲完",
            "完成",
            "next",
            "continue",
        )
        return any(marker in text for marker in markers)

    def _respond_clarification(
        self, message: str, route: dict[str, Any]
    ) -> dict[str, Any]:
        """Return a clarification response when intent is unclear."""
        return {
            "ok": True,
            "route": route,
            "final_result": {
                "message": f"不太确定您的意图，请更具体地描述您想做什么。您的原始消息：{message[:200]}",
            },
            "next_action": INTENT_TO_NEXT_ACTION[route["intent"]],
        }

    @staticmethod
    def _clarification_route(reason: str) -> dict[str, Any]:
        """Return a clarification route dict (used when LLM fails)."""
        return {
            "intent": "clarification",
            "target": INTENT_TO_TARGET["clarification"],
            "reason": reason,
            "source": "llm_router",
        }
