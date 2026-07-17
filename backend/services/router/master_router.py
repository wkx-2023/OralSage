"""Master router — classifies user intent and dispatches to existing services.

This module is the ONLY new architectural layer; it does NOT modify, fix, or
duplicate any downstream learning / study-plan / literature logic.

Architecture
------------
``MasterRouter`` receives a user message + context and:

1. If ``context.plan_item_id`` is present → deterministic lookup (no LLM).
2. Otherwise → constrained LLM classification into one of five intents.
3. Dispatches to the existing downstream service and returns a unified
   ``{route, final_result, next_action}`` response.

LLM failures / invalid output fall back to ``clarification`` except for
high-confidence literature requests, which can be routed deterministically so
the literature tool remains usable without a classifier LLM.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
from datetime import date
from typing import Any, Callable

from backend.services.llm_client import (
    LLMConfigurationError,
    LLMRequestError,
    chat_completion,
    is_llm_configured,
)
from backend.services.learning.study_plan import recommend_study_plan
from backend.services.learning.llm_policy import decide_next_action_with_llm
from backend.services.learning.planner import plan_lesson
from backend.services.learning.skills import (
    LEARNING_AGENT_ALIASES,
    LEARN_KNOWLEDGE_POINT_SKILL,
    learning_skill_catalog,
    run_learn_knowledge_point,
)
from backend.services.literature.agent import run_literature_search
from backend.services.qa.direct_qa import answer_direct_question

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_INTENTS: frozenset[str] = frozenset(
    {"learning", "study_plan", "literature", "clarification", "direct_qa"}
)

INTENT_TO_TARGET: dict[str, str] = {
    "learning": "learning_orchestrator",
    "study_plan": "study_plan",
    "literature": "frontier_literature",
    "clarification": "none",
    "direct_qa": "direct_qa_service",
}

INTENT_TO_NEXT_ACTION: dict[str, str] = {
    "learning": "start_session",
    "study_plan": "show_recommendations",
    "literature": "show_literature",
    "clarification": "select_plan",
    "direct_qa": "show_direct_answer",
}

CONVERSATIONAL_LEARNING_AGENTS: frozenset[str] = frozenset({
    "planner",
    "handout",
    "slides",
    "ppt",
    "tutor",
    "lecture_script",
    "voice",
    "qa",
    "quiz",
    "assessment",
    "continue",
})

STANDALONE_BOOTSTRAP_AGENTS: frozenset[str] = frozenset({
    "handout",
    "slides",
    "ppt",
    "lecture_script",
    "voice",
    "quiz",
})

CLASSIFIER_SYSTEM_PROMPT = (
    "你是一个学习需求分类器。根据用户的消息内容，判断用户意图属于以下五类中的哪一类：\n"
    "\n"
    "- learning: 用户想系统学习某个知识主题、了解某个概念、开始学习某项内容，"
    "需要课程结构、连续讲解或完整主题学习\n"
    "- direct_qa: 用户提出具体的口腔医学问题，希望获得直接文字回答而非系统学习课程；"
    "问题明确、可以一次回答，不需要大纲、讲义或 PPT\n"
    "- study_plan: 用户想获取学习计划推荐、想知道今天学什么、需要安排学习任务\n"
    "- literature: 用户想检索论文文献、查找最新研究、翻译文章、了解科研进展\n"
    "- clarification: 用户意图模糊不清，或无法判断其具体需求\n"
    "\n"
    "区分 learning 和 direct_qa 的关键：如果用户的问题是为了求解一个具体疑问"
    "（如鉴别诊断、治疗方法、病因机制等），属于 direct_qa；"
    "如果用户需要系统性地学习一个主题（如\"系统学习牙周炎\"），属于 learning。\n"
    "\n"
    "同时判断用户是否明确要求开始或完成自己已有的学习计划项。只有用户明确提到"
    "自己的学习计划、计划任务或要完成已有计划时，plan_item_requested 才能为 true。"
    "普通的主题学习请求不能因为系统中可能存在未完成计划而设为 true；请求制定、推荐"
    "或安排学习计划属于 study_plan，plan_item_requested 应为 false。\n"
    "\n"
    "请严格只返回一个 JSON 对象，格式如下，不要附带任何其他文本：\n"
    '{"intent": "<learning|direct_qa|study_plan|literature|clarification>", '
    '"plan_item_requested": <true|false>, "topic": "<学习主题或空字符串>", '
    '"reason": "<简短中文说明>"}'
)

CLASSIFIER_SYSTEM_PROMPT += (
    "\nFor a learning request, also decide whether it is a complete workflow or "
    "an explicit single capability request. When the user explicitly asks to "
    "only generate/rebuild a handout, slides/PPT, lecture script, voice, quiz, "
    "or asks to re-explain the current section, include: "
    '"dispatch_type": "agent" and "target_agent": one of '
    "planner|handout|slides|ppt|tutor|lecture_script|voice|qa|quiz|assessment|continue. "
    "Otherwise omit both fields and use the complete learning workflow. "
    "For an independent quiz request, set target_agent to quiz and include "
    '"topic" as the requested subject plus "question_count" as an integer from 1 to 20. '
    "Extract these fields from meaning rather than word order. For example, "
    "a request equivalent to asking for five questions about periodontitis must return "
    '{"intent":"learning","dispatch_type":"agent","target_agent":"quiz",'
    '"topic":"periodontitis","question_count":5,"plan_item_requested":false,"reason":"..."}.'
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
    plan_item_requested = parsed.get("plan_item_requested", False)
    if not isinstance(plan_item_requested, bool):
        return None
    if plan_item_requested and intent != "learning":
        return None
    reason = str(parsed.get("reason", "")).strip()
    route = {
        "intent": intent,
        "target": INTENT_TO_TARGET[intent],
        "plan_item_requested": plan_item_requested,
        "reason": reason or "无法提供具体理由。",
        "source": "llm_router",
    }
    topic = str(parsed.get("topic") or "").strip()
    if intent == "learning" and topic:
        route["topic"] = topic
    dispatch_type = str(parsed.get("dispatch_type") or "").strip().lower()
    target_agent = str(parsed.get("target_agent") or "").strip().lower()
    if dispatch_type or target_agent:
        if intent != "learning" or dispatch_type != "agent":
            return None
        target_agent = LEARNING_AGENT_ALIASES.get(target_agent, target_agent)
        if target_agent not in CONVERSATIONAL_LEARNING_AGENTS:
            return None
        route["dispatch_type"] = "agent"
        route["target_agent"] = target_agent
        if target_agent == "quiz" and parsed.get("question_count") is not None:
            question_count = _coerce_int(
                parsed.get("question_count"),
                minimum=1,
                maximum=20,
            )
            if question_count is None:
                return None
            route["question_count"] = question_count
    return route


def _normalize_history_for_direct_qa(
    history: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Keep only ``role`` + ``content`` fields with non-empty string values.

    The Direct QA service performs its own internal normalization (max 8
    turns, user/assistant roles only), so this is a lightweight pre-filter
    to satisfy the type contract.
    """
    cleaned: list[dict[str, str]] = []
    for turn in history:
        if not isinstance(turn, dict):
            continue
        role = str(turn.get("role", "")).strip()
        content = str(turn.get("content", "")).strip()
        if role and content:
            cleaned.append({"role": role, "content": content})
    return cleaned


def _normalize_learning_topic(value: Any) -> str:
    text = " ".join(str(value or "").split()).strip()
    prefixes = ("我想要学习", "我想学习", "我想要了解", "我想了解", "请学习", "学习")
    changed = True
    while text and changed:
        changed = False
        for prefix in prefixes:
            if text.startswith(prefix) and len(text) > len(prefix):
                text = text[len(prefix):].lstrip(" ：:，,。")
                changed = True
                break
    return text


def _deterministic_literature_route(message: str) -> dict[str, Any] | None:
    """Route only obvious literature-search requests without an LLM."""
    text = "".join(str(message or "").lower().split())
    if not text:
        return None

    strong_markers = (
        "文献",
        "论文",
        "pubmed",
        "elsevier",
        "scopus",
        "综述",
        "meta分析",
        "系统评价",
        "研究进展",
        "科研进展",
        "最新研究",
    )
    temporal_research_markers = (
        "近一个月",
        "近1个月",
        "近三个月",
        "近3个月",
        "近半年",
        "近6个月",
        "近一年",
        "近1年",
        "近两年",
        "近2年",
        "最近研究",
    )
    has_strong_literature_marker = any(marker in text for marker in strong_markers)
    has_temporal_research_marker = (
        any(marker in text for marker in temporal_research_markers)
        and any(marker in text for marker in ("研究", "科研", "阅读", "检索", "查找", "论文", "文献"))
    )
    if not (has_strong_literature_marker or has_temporal_research_marker):
        return None

    return {
        "intent": "literature",
        "target": INTENT_TO_TARGET["literature"],
        "plan_item_requested": False,
        "reason": "根据文献检索相关表述进入文献模式。",
        "source": "deterministic_literature_route",
    }


def _deterministic_learning_route(message: str) -> dict[str, Any] | None:
    """Route explicit learn/teach requests without relying on the LLM."""
    raw = " ".join(str(message or "").split()).strip()
    text = "".join(raw.lower().split())
    if not text:
        return None
    if any(marker in text for marker in ("学习计划", "推荐计划", "今日计划", "文献", "论文")):
        return None
    markers = (
        "我想学习",
        "我想要学习",
        "开始学习",
        "系统学习",
        "学习一下",
        "请讲解",
        "讲解一下",
        "教我",
        "我想了解",
        "我想要了解",
    )
    if not any(marker in text for marker in markers):
        return None
    topic = _normalize_learning_topic(raw)
    if not topic:
        return None
    return {
        "intent": "learning",
        "target": INTENT_TO_TARGET["learning"],
        "plan_item_requested": False,
        "reason": "根据明确学习表述进入学习模式。",
        "source": "deterministic_learning_route",
        "topic": topic,
    }


def _detect_conversational_agent(message: str) -> str | None:
    """Recognize clear single-capability commands without an LLM.

    This intentionally requires both a generation/control verb and a resource
    marker, so a factual question mentioning PPT or audio is not hijacked from
    Direct QA.
    """
    text = "".join(str(message or "").lower().split())
    if not text:
        return None

    action_markers = (
        "\u53ea\u751f\u6210", "\u751f\u6210", "\u91cd\u65b0\u751f\u6210", "\u91cd\u5efa",
        "\u505a\u4e00\u4efd", "\u51fa", "\u518d\u8bb2\u4e00\u904d", "\u91cd\u65b0\u8bb2",
        "\u7b80\u5355\u4e00\u70b9", "\u8be6\u7ec6\u4e00\u70b9", "\u7ee7\u7eed\u4e0b\u4e00\u8282",
    )
    if not any(marker in text for marker in action_markers):
        return None
    if any(marker in text for marker in ("ppt", "\u5e7b\u706f\u7247", "\u8bfe\u4ef6")):
        return "ppt"
    if any(marker in text for marker in ("\u8bed\u97f3", "\u97f3\u9891", "\u6717\u8bfb")):
        return "voice"
    if "\u8bb2\u7a3f" in text:
        return "lecture_script"
    if "\u8bb2\u4e49" in text:
        return "handout"
    if (
        any(marker in text for marker in ("\u51fa\u9898", "\u7ec3\u4e60\u9898", "\u5c0f\u6d4b", "\u6d4b\u9a8c\u9898"))
        or ("\u51fa" in text and ("\u9053\u9898" in text or "\u4e2a\u9898" in text))
        or bool(re.search(r"\u51fa\s*\d*\s*(?:\u9053|\u4e2a).{0,40}\u9898", text))
    ):
        return "quiz"
    if any(marker in text for marker in ("\u91cd\u65b0\u8bb2", "\u518d\u8bb2\u4e00\u904d", "\u7b80\u5355\u4e00\u70b9", "\u8be6\u7ec6\u4e00\u70b9")):
        return "tutor"
    if "\u7ee7\u7eed\u4e0b\u4e00\u8282" in text:
        return "continue"
    return None


def _deterministic_agent_route(message: str) -> dict[str, Any] | None:
    target_agent = _detect_conversational_agent(message)
    if not target_agent:
        return None
    return {
        "intent": "learning",
        "target": target_agent,
        "target_agent": target_agent,
        "dispatch_type": "agent",
        "plan_item_requested": False,
        "reason": "Explicit single-capability request recognized from the conversation.",
        "source": "deterministic_agent_route",
    }


def _agent_dispatch_answer(target: str, next_action: str) -> str:
    labels = {
        "handout": "\u8bb2\u4e49\u751f\u6210",
        "slides": "\u5e7b\u706f\u7247\u751f\u6210",
        "ppt": "PPT \u751f\u6210",
        "tutor": "\u8bfe\u7a0b\u8bb2\u89e3",
        "lecture_script": "\u8bb2\u7a3f\u751f\u6210",
        "voice": "\u8bed\u97f3\u751f\u6210",
        "qa": "\u5f53\u524d\u8bfe\u7a0b\u7b54\u7591",
        "quiz": "\u51fa\u9898",
        "assessment": "\u7b54\u6848\u8bc4\u4f30",
        "continue": "\u8fdb\u5165\u4e0b\u4e00\u8282",
    }
    label = labels.get(target, target)
    suffix = "\u5df2\u5b8c\u6210" if next_action in {"continue_session", "show_stage_exam"} else "\u5df2\u5f00\u59cb\u5904\u7406"
    return f"\u5df2\u8c03\u5ea6 {label}\uff0c{suffix}\u3002"


def _extract_standalone_topic(message: str, target: str) -> str:
    """Extract a topic from a short resource-generation command."""
    text = " ".join(str(message or "").split()).strip()
    if not text:
        return ""
    patterns = [
        r"\u8bf7", r"\u5e2e\u6211", r"\u7ed9\u6211", r"\u53ea", r"\u751f\u6210", r"\u91cd\u65b0\u751f\u6210",
        r"\u91cd\u5efa", r"\u5236\u4f5c", r"\u505a\u4e00\u4efd", r"\u5f53\u524d\u8bfe\u7a0b", r"\u5f53\u524d",
        r"PPT", r"ppt", r"\u5e7b\u706f\u7247", r"\u8bfe\u4ef6", r"\u8bb2\u4e49", r"\u8bb2\u7a3f",
        r"\u8bed\u97f3", r"\u97f3\u9891", r"\u6717\u8bfb", r"\u7ec3\u4e60\u9898", r"\u6d4b\u9a8c\u9898",
    ]
    for pattern in patterns:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    if target == "quiz":
        text = re.sub(r"\u51fa\s*\d*\s*(?:\u9053|\u4e2a)?\s*\u9898", "", text)
        text = re.sub(r"^\u51fa\s*\d*\s*(?:\u9053|\u4e2a)\s*", "", text)
        text = re.sub(r"(?:\u7684)?\u9898$", "", text)
        text = re.sub(r"\u51fa\u9898|\u5c0f\u6d4b", "", text)
        text = re.sub(r"^\u9488\u5bf9\s*", "", text)
    return _normalize_learning_topic(text.strip(" \uff0c\u3002\uff1a:,-"))


def _requested_quiz_question_count(value: Any) -> int:
    direct_count = _coerce_int(value, minimum=1, maximum=20)
    if direct_count is not None:
        return direct_count
    match = re.search(r"(\d{1,2})\s*(?:\u9053|\u4e2a)(?:.{0,40}?\u9898)", str(value or ""))
    if not match:
        return 5
    return max(1, min(int(match.group(1)), 20))


def _deterministic_route(message: str) -> dict[str, Any] | None:
    return (
        _deterministic_literature_route(message)
        or _deterministic_agent_route(message)
        or _deterministic_learning_route(message)
    )


def _coerce_int(value: Any, *, minimum: int, maximum: int) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return max(minimum, min(number, maximum))


def _extract_requested_limit(message: str) -> int | None:
    text = str(message or "")
    patterns = (
        r"(?:前|top\s*)(\d{1,2})\s*(?:篇|条|个)?",
        r"(\d{1,2})\s*(?:篇|条)\s*(?:文献|论文|研究)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return _coerce_int(match.group(1), minimum=1, maximum=30)
    return None


def _literature_options_from_context(
    message: str,
    context: dict[str, Any] | None,
) -> dict[str, Any]:
    ctx = context or {}
    fields = ctx.get("fields") if isinstance(ctx.get("fields"), dict) else {}
    data = ctx.get("data") if isinstance(ctx.get("data"), dict) else {}

    def first_value(*names: str) -> Any:
        for source in (ctx, fields, data):
            for name in names:
                if name in source and source.get(name) is not None:
                    return source.get(name)
        return None

    months = _coerce_int(first_value("months", "month_window"), minimum=1, maximum=120)
    limit = _coerce_int(first_value("limit", "top_k"), minimum=1, maximum=30)
    if limit is None:
        limit = _extract_requested_limit(message)

    return {
        "months": months,
        "limit": limit or 5,
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
        direct_qa_service: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.learning_orchestrator = learning_orchestrator
        self._classifier = classifier
        self._literature_service = literature_service or run_literature_search
        self._study_plan_service = study_plan_service or recommend_study_plan
        self._plan_matcher = plan_matcher
        self._direct_qa_service = direct_qa_service or answer_direct_question

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def route(
        self,
        message: str,
        user_id: str = "demo",
        history: list[dict[str, Any]] | None = None,
        context: dict[str, Any] | None = None,
        stream_callback: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Classify *message* and dispatch to the appropriate downstream service.

        Returns a dict with ``ok``, ``route``, ``final_result``, and
        ``next_action``.
        """
        ctx = context or {}

        # An explicit target is a controlled escape hatch for clients that
        # already know which registered capability they need. It never
        # resolves arbitrary object methods from user input.
        requested_agent = str(ctx.get("target_agent") or "").strip().lower()
        if requested_agent:
            return await self.dispatch_agent(
                requested_agent,
                message=message,
                user_id=user_id,
                history=history,
                context=ctx,
            )

        # --- explicit plan_item_id — deterministic, no LLM -------------------
        plan_item_id = ctx.get("plan_item_id")
        if plan_item_id:
            return await self._handle_plan_item(user_id, str(plan_item_id))

        recommendation_id = str(ctx.get("recommendation_id") or "").strip()
        if recommendation_id:
            return await self._handle_recommendation(
                user_id,
                recommendation_id,
                str(ctx.get("plan_date") or "").strip(),
            )

        session_id = str(ctx.get("session_id") or "").strip()
        conversational_agent = _detect_conversational_agent(message)
        if session_id and conversational_agent:
            standalone_topic = (
                _extract_standalone_topic(message, conversational_agent)
                if conversational_agent in STANDALONE_BOOTSTRAP_AGENTS
                else ""
            )
            if standalone_topic:
                return await self.dispatch_agent(
                    conversational_agent,
                    message=message,
                    user_id=user_id,
                    history=history,
                    context={**ctx, "session_id": "", "topic": standalone_topic},
                )
            return await self.dispatch_agent(
                conversational_agent,
                message=message,
                user_id=user_id,
                history=history,
                context={**ctx, "session_id": session_id},
            )
        if session_id and self._is_continue_message(message):
            return await self._handle_session_continue(
                user_id,
                session_id,
                stream_callback=stream_callback,
            )
        if session_id:
            return await self._handle_session_input(
                user_id,
                session_id,
                message,
                stream_callback=stream_callback,
            )

        # A resource request with an explicit topic is always standalone.
        # This guard prevents a partially structured classifier response from
        # turning "generate <topic> handout" into a full learning course.
        if conversational_agent in STANDALONE_BOOTSTRAP_AGENTS:
            standalone_topic = _extract_standalone_topic(message, conversational_agent)
            if standalone_topic:
                return await self.dispatch_agent(
                    conversational_agent,
                    message=message,
                    user_id=user_id,
                    history=history,
                    context={**ctx, "session_id": "", "topic": standalone_topic},
                )

        # --- LLM classification ----------------------------------------------
        route = await self._classify(message, history, ctx)

        # --- dispatch --------------------------------------------------------
        if route["intent"] == "learning":
            return await self._handle_learning(user_id, message, route, ctx)
        elif route["intent"] == "direct_qa":
            return await self._handle_direct_qa(message, user_id, route, history)
        elif route["intent"] == "study_plan":
            return await self._handle_study_plan(user_id, message, route, ctx)
        elif route["intent"] == "literature":
            return await self._handle_literature(message, route, ctx)
        else:
            return self._respond_clarification(message, route)

    async def dispatch_agent(
        self,
        agent_name: str,
        *,
        message: str = "",
        user_id: str = "demo",
        history: list[dict[str, Any]] | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Dispatch one registered workflow or leaf capability."""
        ctx = context or {}
        normalized = str(agent_name or "").strip().lower()
        target = LEARNING_AGENT_ALIASES.get(normalized, normalized)
        route = {
            "intent": "learning",
            "target": target,
            "source": "explicit_agent_dispatch",
            "reason": f"Explicitly dispatched registered capability: {target}",
        }

        if target in {"study_plan", "literature", "direct_qa"}:
            if target == "study_plan":
                return await self._handle_study_plan(user_id, message, {**route, "intent": "study_plan"}, ctx)
            if target == "literature":
                return await self._handle_literature(message, {**route, "intent": "literature"}, ctx)
            return await self._handle_direct_qa(message, user_id, {**route, "intent": "direct_qa"}, history)

        if target == LEARN_KNOWLEDGE_POINT_SKILL:
            topic = _normalize_learning_topic(ctx.get("topic") or message)
            if not topic:
                return self._dispatch_error(route, "A topic is required for learn_knowledge_point.")
            try:
                result = await asyncio.to_thread(
                    run_learn_knowledge_point,
                    self.learning_orchestrator,
                    user_id=user_id,
                    topic=topic,
                    profile_input=ctx.get("profile_input") if isinstance(ctx.get("profile_input"), dict) else None,
                    learning_scope=ctx.get("learning_scope") if isinstance(ctx.get("learning_scope"), dict) else None,
                    deliver_first_section=bool(ctx.get("deliver_first_section", False)),
                )
            except Exception as exc:
                logger.exception("learning skill failed for topic=%s", topic)
                return self._dispatch_error(route, f"Learning skill failed: {exc}")
            return {"ok": True, "route": route, "final_result": result, "next_action": "start_session"}

        if target == "planner":
            topic = _normalize_learning_topic(ctx.get("topic") or message)
            if not topic:
                return self._dispatch_error(route, "A topic is required for planner.")
            profile = ctx.get("profile_input") if isinstance(ctx.get("profile_input"), dict) else self.learning_orchestrator.memory.get_or_create_profile(user_id)
            try:
                result = await asyncio.to_thread(plan_lesson, topic, user_profile=profile)
            except Exception as exc:
                logger.exception("planner dispatch failed for topic=%s", topic)
                return self._dispatch_error(route, f"Planner failed: {exc}")
            return {"ok": bool(result.get("ok", False)), "route": route, "final_result": result, "next_action": "review_outline"}

        session_id = str(ctx.get("session_id") or "").strip()
        leaf_agents = {"handout", "slides", "ppt", "tutor", "lecture_script", "voice", "qa", "quiz", "assessment", "continue"}
        if target not in leaf_agents:
            return self._dispatch_error(route, f"Unknown registered capability: {agent_name}")
        if not session_id:
            if target in STANDALONE_BOOTSTRAP_AGENTS:
                topic = _normalize_learning_topic(ctx.get("topic") or _extract_standalone_topic(message, target))
                if topic:
                    try:
                        bootstrap = await asyncio.to_thread(
                            run_learn_knowledge_point,
                            self.learning_orchestrator,
                            user_id=user_id,
                            topic=topic,
                            session_mode="standalone_generation",
                        )
                    except Exception as exc:
                        logger.exception("standalone %s bootstrap failed for topic=%s", target, topic)
                        return self._dispatch_error(route, f"Standalone {target} setup failed: {exc}")
                    standalone_session = bootstrap.get("session") if isinstance(bootstrap, dict) else {}
                    standalone_session_id = str((standalone_session or {}).get("id") or "")
                    if not standalone_session_id:
                        return self._dispatch_error(route, "Standalone generation did not create a session.")
                    nested = await self.dispatch_agent(
                        target,
                        message=message,
                        user_id=user_id,
                        history=history,
                        context={
                            **ctx,
                            "session_id": standalone_session_id,
                            "standalone_topic": topic,
                            "quiz_question_count": _requested_quiz_question_count(message),
                        },
                    )
                    if not nested.get("ok"):
                        return nested
                    operation = nested.get("final_result") or {}
                    final_result = {
                        "session": standalone_session,
                        "standalone_generation": True,
                        "profile_impact": bootstrap.get("knowledge_profile_write"),
                        "operation": operation,
                        "answer": operation.get("answer") if isinstance(operation, dict) else "",
                    }
                    if target == "quiz":
                        final_result["stage_exam"] = operation
                    return {
                        "ok": True,
                        "route": {
                            **route,
                            "execution_mode": "standalone_generation",
                            "topic": topic,
                        },
                        "final_result": final_result,
                        "next_action": nested.get("next_action"),
                    }
            return {
                "ok": True,
                "route": {
                    "intent": "clarification",
                    "target": "none",
                    "source": "missing_learning_session",
                    "requested_target_agent": target,
                    "reason": f"{target} \u9700\u8981\u5f53\u524d\u5b66\u4e60\u4f1a\u8bdd\u4e0a\u4e0b\u6587\u3002",
                },
                "final_result": {
                    "message": "\u8fd9\u4e2a\u8bf7\u6c42\u9700\u8981\u5f53\u524d\u5b66\u4e60\u8bfe\u7a0b\u3002"
                    "\u8bf7\u5148\u8bf4\u201c\u7cfb\u7edf\u5b66\u4e60<\u77e5\u8bc6\u70b9>\u201d\u521b\u5efa\u8bfe\u7a0b\uff0c"
                    "\u518d\u5728\u8be5\u8bfe\u7a0b\u4e2d\u8bf7\u6c42\u8fd9\u9879\u80fd\u529b\u3002",
                },
                "next_action": "start_session",
            }
        try:
            session = self.learning_orchestrator.memory.get_session(session_id)
        except KeyError:
            return self._dispatch_error(route, f"Learning session not found: {session_id}")
        if str(session.get("user_id") or "") != user_id:
            return self._dispatch_error(route, "Learning session access denied.")

        try:
            if target == "handout":
                result = await asyncio.to_thread(self.learning_orchestrator.retry_failed_resources, session_id)
                handout_section = self.learning_orchestrator.get_first_handout_section_for_interaction(session_id)
                if handout_section:
                    result = {**result, "handout_section": handout_section}
                    next_action = "show_resource"
                else:
                    next_action = "wait_for_resources"
            elif target in {"slides", "ppt"}:
                result = await asyncio.to_thread(self.learning_orchestrator.rebuild_ppt_resources, session_id)
                next_action = "wait_for_resources"
            elif target == "tutor":
                result = await asyncio.to_thread(self.learning_orchestrator.next_step, session_id)
                next_action = "continue_session"
            elif target == "lecture_script":
                result = await asyncio.to_thread(
                    self.learning_orchestrator.generate_current_section_lecture_script,
                    session_id,
                )
                next_action = "show_resource"
            elif target == "voice":
                result = await asyncio.to_thread(
                    self.learning_orchestrator.generate_current_section_voice,
                    session_id,
                )
                next_action = "wait_for_resources"
            elif target == "quiz":
                standalone_topic = str(ctx.get("standalone_topic") or "").strip()
                if standalone_topic:
                    result = await asyncio.to_thread(
                        self.learning_orchestrator.generate_topic_quiz_for_interaction,
                        session_id,
                        topic=standalone_topic,
                        question_count=_requested_quiz_question_count(
                            ctx.get("quiz_question_count")
                        ),
                    )
                else:
                    result = await asyncio.to_thread(
                        self.learning_orchestrator.ensure_stage_exam_for_interaction,
                        session_id,
                    )
                next_action = "show_stage_exam"
            elif target == "qa":
                question = str(ctx.get("question") or message).strip()
                if not question:
                    return self._dispatch_error(route, "A question is required for qa.")
                result = await asyncio.to_thread(self.learning_orchestrator.handle_interruption, session_id, question)
                next_action = "continue_session"
            elif target == "assessment":
                question = ctx.get("question")
                answer = str(ctx.get("answer") or "").strip()
                if not isinstance(question, dict) or not answer:
                    return self._dispatch_error(route, "assessment requires context.question (object) and context.answer.")
                result = await asyncio.to_thread(self.learning_orchestrator.submit_answer, session_id, question, answer)
                next_action = "continue_session"
            else:
                result = await asyncio.to_thread(self.learning_orchestrator.continue_to_next_section, session_id)
                next_action = "continue_session"
        except Exception as exc:
            logger.exception("registered agent dispatch failed: %s", target)
            return self._dispatch_error(route, f"{target} failed: {exc}")

        if target == "quiz" and (
            result.get("status") != "ready" or not result.get("questions")
        ):
            return self._dispatch_error(
                route,
                str(result.get("error") or "No usable quiz questions were generated."),
            )

        if isinstance(result, dict):
            result = {
                **result,
                "answer": _agent_dispatch_answer(target, next_action),
            }
        return {"ok": True, "route": route, "final_result": result, "next_action": next_action}

    @staticmethod
    def _dispatch_error(route: dict[str, Any], error: str) -> dict[str, Any]:
        return {"ok": False, "route": route, "final_result": None, "next_action": None, "error": error}

    @staticmethod
    def capability_catalog() -> list[dict[str, Any]]:
        return [
            *learning_skill_catalog(),
            {"name": "study_plan", "kind": "agent", "description": "Generate a study-plan recommendation."},
            {"name": "literature", "kind": "agent", "description": "Search and summarize literature."},
            {"name": "direct_qa", "kind": "agent", "description": "Answer a direct medical learning question."},
        ]

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
                validated = _validate_route(result)
                if validated is not None:
                    return validated
            except Exception:
                logger.exception("Injected classifier failed")
            return self._clarification_route("分类器返回无效结果。")

        # --- default: LLM classifier -----------------------------------------
        fallback_route = _deterministic_route(message)
        if not is_llm_configured():
            return fallback_route or self._clarification_route("LLM 未配置，无法进行意图分类。")

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
            return fallback_route or self._clarification_route(f"意图分类失败：{exc}")

        # Parse and validate
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("LLM classifier returned invalid JSON: %s", raw[:200])
            return fallback_route or self._clarification_route("分类器返回了无效的 JSON。")

        validated = _validate_route(parsed)
        if validated is None:
            logger.warning("LLM classifier returned invalid route: %s", parsed)
            return fallback_route or self._clarification_route("分类器返回了无效的意图值。")
        if (
            fallback_route is not None
            and fallback_route.get("dispatch_type") == "agent"
            and validated.get("dispatch_type") != "agent"
        ):
            logger.info("Using deterministic explicit-agent route over generic LLM learning route.")
            return fallback_route
        if fallback_route is not None and validated["intent"] == "clarification":
            logger.info(
                "LLM classified an obvious request as clarification; using deterministic route."
            )
            return fallback_route

        return validated

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def _handle_plan_item(
        self, user_id: str, plan_item_id: str
    ) -> dict[str, Any]:
        """Deterministic plan-item lookup — NO LLM involved."""
        # 1. Read the real plan item (raises KeyError if not found / wrong user)
        try:
            item = self.learning_orchestrator.memory.get_study_plan_item(
                plan_item_id, user_id=user_id
            )
        except KeyError:
            return {
                "ok": False,
                "route": {
                    "intent": "learning",
                    "target": INTENT_TO_TARGET["learning"],
                    "source": "explicit_plan_item",
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
                    "intent": "learning",
                    "target": INTENT_TO_TARGET["learning"],
                    "source": "explicit_plan_item",
                    "reason": f"计划任务「{item.get('title', plan_item_id)}」状态为 {item.get('status')}，无法开始学习。",
                },
                "final_result": None,
                "next_action": None,
                "error": f"Plan item is not open: status={item.get('status')}",
            }

        return await self._execute_learning_task(
            user_id,
            item,
            source="explicit_plan_item",
        )

    async def _handle_recommendation(
        self,
        user_id: str,
        recommendation_id: str,
        plan_date: str,
    ) -> dict[str, Any]:
        """Resolve a recommendation server-side before executing it."""
        try:
            target_date = date.fromisoformat(plan_date).isoformat() if plan_date else date.today().isoformat()
        except ValueError:
            target_date = date.today().isoformat()
        route = {
            "intent": "learning",
            "target": INTENT_TO_TARGET["learning"],
            "source": "explicit_recommendation",
        }
        try:
            result = await asyncio.to_thread(
                self._study_plan_service,
                self.learning_orchestrator.memory,
                user_id=user_id,
                plan_date=target_date,
            )
        except Exception as exc:
            logger.exception("study_plan_service failed while resolving recommendation")
            return {
                "ok": False,
                "route": {**route, "reason": "无法重新读取这条学习推荐。"},
                "final_result": None,
                "next_action": None,
                "error": f"学习推荐解析失败：{exc}",
            }

        recommendation = next(
            (
                item for item in result.get("recommendations", [])
                if str(item.get("recommendation_id") or "") == recommendation_id
            ),
            None,
        )
        if recommendation is None:
            return {
                "ok": False,
                "route": {**route, "reason": "这条推荐已过期，请重新生成学习计划。"},
                "final_result": None,
                "next_action": "show_recommendations",
                "error": "Study recommendation not found or expired.",
            }
        return await self._execute_learning_task(
            user_id,
            recommendation,
            source="explicit_recommendation",
        )

    async def _execute_learning_task(
        self,
        user_id: str,
        item: dict[str, Any],
        *,
        source: str,
    ) -> dict[str, Any]:
        task_type = str(item.get("task_type") or item.get("category") or "topic_learning").strip()
        title = str(item.get("title") or "").strip()
        learning_target = _normalize_learning_topic(item.get("learning_target"))
        linked_course_title = _normalize_learning_topic(item.get("linked_course_title"))
        linked_course_id = str(item.get("linked_course_id") or "").strip()

        if task_type == "continue_lesson" and linked_course_id:
            # "开始学习" for a continue_lesson plan item must restore the
            # *current* section lecture WITHOUT advancing to the next
            # section.  We call next_step() instead of
            # continue_to_next_section() so the user sees the node they
            # left off at — not the one after it.  Regular session
            # "continue" (context.session_id + continue message) still
            # goes through _handle_session_continue and advances.
            route = {
                "intent": "learning",
                "target": INTENT_TO_TARGET["learning"],
                "source": source,
                "reason": f"根据计划任务「{title or linked_course_title}」恢复当前课程讲稿。",
            }
            try:
                step_result = await asyncio.to_thread(
                    self.learning_orchestrator.next_step,
                    linked_course_id,
                )
            except Exception as exc:
                logger.exception(
                    "next_step failed for session=%s (continue_lesson plan item)",
                    linked_course_id,
                )
                return {
                    "ok": False,
                    "route": {
                        **route,
                        "reason": f"根据计划任务「{title or linked_course_title}」恢复课程讲稿失败。",
                    },
                    "final_result": None,
                    "next_action": None,
                    "error": f"继续课程失败：{exc}",
                }
            return {
                "ok": True,
                "route": route,
                "final_result": {
                    "session": {"id": linked_course_id},
                    "tutor_step": step_result,
                    "lesson_completed": False,
                },
                "next_action": "continue_session",
            }

        focused_types = {"review_weakness", "review_mastery", "review_summary", "practice"}
        if task_type in focused_types and learning_target:
            topic = learning_target
            if linked_course_title and linked_course_title not in learning_target:
                topic = f"{linked_course_title}：{learning_target}"
        else:
            topic = learning_target or linked_course_title or _normalize_learning_topic(title)

        # --- formal learning_scope from item (empty dict → not passed) ---
        learning_scope_raw = item.get("learning_scope")
        learning_scope: dict[str, Any] | None = (
            learning_scope_raw
            if isinstance(learning_scope_raw, dict) and learning_scope_raw
            else None
        )

        route = {
            "intent": "learning",
            "target": INTENT_TO_TARGET["learning"],
            "source": source,
            "reason": f"根据计划任务「{title or topic}」启动针对性学习。",
            "task_type": task_type,
        }
        if not topic:
            return {
                "ok": False,
                "route": {**route, "reason": f"计划任务「{title}」没有可解析的学习主题。"},
                "final_result": None,
                "next_action": None,
                "error": "Plan item has no resolvable topic.",
            }
        return await self._start_learning(user_id, topic, route, learning_scope=learning_scope)

    async def _handle_learning(
        self,
        user_id: str,
        message: str,
        route: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Start free study or match an explicitly requested study-plan item.

        Ordinary learning requests never inspect unfinished plan items. Candidate
        lookup is entered only when the intent classifier explicitly sets
        ``plan_item_requested=true``.
        """
        if route.get("dispatch_type") == "agent":
            target_agent = str(route.get("target_agent") or "").strip()
            if target_agent:
                dispatch_context = dict(context or {})
                if route.get("topic"):
                    dispatch_context["topic"] = route["topic"]
                if route.get("question_count") is not None:
                    dispatch_context["quiz_question_count"] = route["question_count"]
                dispatched = await self.dispatch_agent(
                    target_agent,
                    message=message,
                    user_id=user_id,
                    context=dispatch_context,
                )
                dispatched_route = dispatched.get("route") if isinstance(dispatched, dict) else None
                if isinstance(dispatched_route, dict):
                    dispatched["route"] = {
                        **dispatched_route,
                        "source": route.get("source", dispatched_route.get("source")),
                        "reason": route.get("reason", dispatched_route.get("reason")),
                    }
                return dispatched

        if route.get("plan_item_requested") is not True:
            topic = _normalize_learning_topic(route.get("topic") or message)
            return await self._start_learning(user_id, topic, route)

        # 1. The LLM identified an explicit request to use an existing plan.
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
        self, user_id: str, message: str, route: dict[str, Any], context: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Recommend study plans based on user's current learning progress.

        When *context* carries a ``parent_plan_draft_id`` it is forwarded
        to the study-plan service so that the new draft records lineage.
        """
        ctx = context or {}
        parent_plan_draft_id: str | None = (
            str(ctx["parent_plan_draft_id"]).strip()
            if isinstance(ctx.get("parent_plan_draft_id"), str)
            and ctx["parent_plan_draft_id"].strip()
            else None
        )
        try:
            kwargs: dict[str, Any] = dict(
                memory=self.learning_orchestrator.memory,
                user_id=user_id,
                plan_date=date.today().isoformat(),
                request_text=message,
            )
            if parent_plan_draft_id is not None:
                kwargs["parent_plan_draft_id"] = parent_plan_draft_id
            result = await asyncio.to_thread(self._study_plan_service, **kwargs)
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
        self, message: str, route: dict[str, Any], context: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Call the existing literature search service."""
        query = message.strip()
        options = _literature_options_from_context(query, context)
        try:
            parameters = inspect.signature(self._literature_service).parameters.values()
            accepts_args = any(param.kind is inspect.Parameter.VAR_POSITIONAL for param in parameters)
            accepts_kwargs = any(param.kind is inspect.Parameter.VAR_KEYWORD for param in parameters)
            positional_count = sum(
                param.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
                for param in parameters
            )
            supports_translations = accepts_kwargs or any(
                param.name == "include_chat_translations" for param in parameters
            )
            if supports_translations:
                result = await self._literature_service(
                    query,
                    options["months"],
                    options["limit"],
                    include_chat_translations=True,
                )
            elif accepts_args or positional_count >= 3:
                result = await self._literature_service(
                    query,
                    options["months"],
                    options["limit"],
                )
            else:
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

    async def _handle_direct_qa(
        self,
        message: str,
        user_id: str,
        route: dict[str, Any],
        history: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        """Call the Direct QA service — no session, PPT, or outline created.

        Passes the raw *message*, real conversation *history*, and *user_id*
        to :func:`answer_direct_question`.  The history is only used for
        reference resolution; answers are never treated as medical evidence.
        """
        normalized_history = _normalize_history_for_direct_qa(history or [])
        try:
            result = await asyncio.to_thread(
                self._direct_qa_service,
                message=message,
                history=normalized_history,
                user_id=user_id,
            )
        except Exception as exc:
            logger.exception("Direct QA service failed")
            return {
                "ok": False,
                "route": route,
                "final_result": {
                    "answer": "答疑系统当前不可用，请稍后重试。",
                    "error": str(exc),
                },
                "next_action": INTENT_TO_NEXT_ACTION[route["intent"]],
                "error": f"直接问答失败：{exc}",
            }

        return {
            "ok": True,
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
        self,
        user_id: str,
        topic: str,
        route: dict[str, Any],
        *,
        learning_scope: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call the existing ``start_lesson`` and wrap the result.

        When *learning_scope* is a non-empty dict (from a formal plan item),
        it is forwarded to ``start_lesson`` as optional internal context.
        An empty / missing scope is never passed so the old topic path is
        preserved.
        """
        start_kwargs: dict[str, Any] = {"user_id": user_id, "topic": topic}
        if learning_scope:
            start_kwargs["learning_scope"] = learning_scope
        try:
            result = await asyncio.to_thread(
                run_learn_knowledge_point,
                self.learning_orchestrator,
                **start_kwargs,
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
        stream_callback: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        route = {
            "intent": "learning",
            "target": INTENT_TO_TARGET["learning"],
            "reason": "继续当前学习 session。",
            "source": "session_context",
        }
        try:
            continue_kwargs = (
                {"stream_callback": stream_callback}
                if stream_callback is not None
                else {}
            )
            result = await asyncio.to_thread(
                self.learning_orchestrator.continue_to_next_section,
                session_id,
                **continue_kwargs,
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
        result = self._attach_session_learning_context(result, session_id)

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

    async def _handle_session_input(
        self,
        user_id: str,
        session_id: str,
        message: str,
        stream_callback: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Let the learning policy LLM route natural language inside a session."""
        try:
            session = self.learning_orchestrator.memory.get_session(session_id)
        except KeyError:
            return {
                "ok": False,
                "route": {
                    "intent": "learning",
                    "target": "learning_orchestrator",
                    "source": "learning_session_policy",
                    "reason": "学习 session 不存在。",
                },
                "final_result": None,
                "next_action": None,
                "error": f"Learning session not found: {session_id}",
            }
        if str(session.get("user_id") or "") != user_id:
            return {
                "ok": False,
                "route": {
                    "intent": "learning",
                    "target": "learning_orchestrator",
                    "source": "learning_session_policy",
                    "reason": "当前用户无权访问该学习 session。",
                },
                "final_result": None,
                "next_action": None,
                "error": "Learning session access denied.",
            }

        state = self.learning_orchestrator.get_policy_state(session_id)
        raw_input = {"type": "raw_text", "raw_text": message}
        decision = await asyncio.to_thread(
            decide_next_action_with_llm,
            state,
            raw_input,
            fallback_policy=self.learning_orchestrator.policy_fn,
        )
        action = str(decision.get("action") or "")
        route = {
            "intent": "learning",
            "target": decision.get("target_agent", "learning_orchestrator"),
            "source": "learning_session_policy",
            "reason": decision.get("reason", "学习策略已完成调度。"),
            "policy_action": action,
        }

        if action == "unknown_input":
            return {
                "ok": True,
                "route": route,
                "final_result": {
                    "message": "请说明你希望继续下一节、针对本节提问，还是进行本节自测。",
                    "session_id": session_id,
                },
                "next_action": "clarify_learning_action",
            }

        if action == "assess_answer":
            return {
                "ok": True,
                "route": route,
                "final_result": {
                    "message": "请在对应的自测题中提交答案，以便关联题目并完成评估。",
                    "session_id": session_id,
                },
                "next_action": "select_quiz_answer",
            }

        execution_input: dict[str, Any] | None = raw_input
        if action == "answer_interruption":
            execution_input = {"type": "interruption_question", "text": message}
        elif action == "continue_to_next_section":
            execution_input = {"type": "continue"}
        elif action == "teach_current_node":
            execution_input = None

        execution_kwargs = (
            {"stream_callback": stream_callback}
            if stream_callback is not None
            else {}
        )
        execution = await asyncio.to_thread(
            self.learning_orchestrator.execute_policy_action,
            session_id,
            decision,
            execution_input,
            **execution_kwargs,
        )
        final_result = execution.get("result") or {
            "message": execution.get("error") or "学习动作未执行。",
            "session_id": session_id,
        }
        if isinstance(final_result, dict):
            final_result = self._attach_session_learning_context(final_result, session_id)
        return {
            "ok": bool(execution.get("executed")) or bool(execution.get("terminal")),
            "route": route,
            "final_result": final_result,
            "next_action": action,
            "error": execution.get("error"),
        }

    def _attach_session_learning_context(
        self,
        result: dict[str, Any],
        session_id: str,
    ) -> dict[str, Any]:
        """Attach lightweight outline/mindmap context for chat rendering."""
        if not isinstance(result, dict):
            return result
        enriched = dict(result)
        try:
            session = self.learning_orchestrator.memory.get_session(session_id)
        except Exception:
            session = {"id": session_id}
        session_payload = dict(enriched.get("session") or {})
        session_payload.setdefault("id", session.get("id") or session_id)
        if session.get("topic"):
            session_payload.setdefault("topic", session.get("topic"))
        if session.get("user_id"):
            session_payload.setdefault("user_id", session.get("user_id"))
        enriched["session"] = session_payload

        try:
            latest = self.learning_orchestrator.memory.latest_outline(session_id)
        except Exception:
            latest = None
        if isinstance(latest, dict):
            outline = latest.get("outline")
            if isinstance(outline, dict) and "outline" not in enriched:
                nodes = outline.get("nodes") if isinstance(outline.get("nodes"), list) else []
                enriched["outline"] = {
                    "title": outline.get("title"),
                    "nodes": [
                        {"id": node.get("id"), "title": node.get("title")}
                        for node in nodes
                        if isinstance(node, dict)
                    ],
                }
            mindmap = latest.get("mindmap")
            if isinstance(mindmap, dict) and "mindmap" not in enriched:
                enriched["mindmap"] = mindmap
        return enriched

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
