from __future__ import annotations

import json
import re
from typing import Any

from backend.services.literature import run_literature_search
from backend.services.llm_client import LLMConfigurationError, LLMRequestError, chat_completion, is_llm_configured

from .evidence import EvidenceItem, collect_literature_evidence, collect_local_evidence, compact, evidence_prompt_block
from .terms import LITERATURE_TRIGGERS, normalize_pathology_query


def _needs_literature(question: str, plan: dict[str, Any], local_items: list[EvidenceItem]) -> bool:
    if plan.get("need_literature"):
        return True
    if len(local_items) < 3:
        return True
    return any(trigger.lower() in question.lower() for trigger in LITERATURE_TRIGGERS)


def _build_messages(question: str, plan: dict[str, Any], evidence: list[EvidenceItem]) -> list[dict[str, str]]:
    system = (
        "你是口腔病理知识问答助手，服务对象是口腔医生、医学生和科研人员。"
        "你只能基于用户问题、检索计划和证据池进行分析总结，不要凭空编造。"
        "每个关键医学判断后必须给出引用标记，例如[依据1]或[文献1]。"
        "证据不足时必须明确写出证据不足，不要给确定诊断、处方剂量或替代面诊结论。"
        "区分教材/指南中的稳定知识和文献中的研究性结论。"
        "引用依据只写清洗后的来源名称和证据支持点，不要输出'已压缩'、'全彩(OCR)'、学习交流/禁止商业用途等无关文本。"
        "遇到脱敏门诊病例时，只能使用摘要化、分析后的证据，不要复述原始门诊字段、编号、医生名或流水号。"
        "输出 Markdown，严格包含这些一级小节：核心结论、病理机制、典型表现、诊断与鉴别要点、临床处理原则、证据不足或争议点、引用依据、免责声明。"
    )
    user = {
        "question": question,
        "normalized_query": plan,
        "evidence": evidence_prompt_block(evidence),
        "output_requirements": [
            "核心结论用 2-4 条短要点回答用户问题。",
            "病理机制解释病因、组织学或病理生理过程。",
            "诊断与鉴别要点只给思路，不给最终诊断。",
            "临床处理原则只写原则和需补充检查，不写药物剂量。",
            "引用依据逐条说明每个证据支持了什么，使用[依据N]/[文献N]标记。",
            "引用依据不要照搬原始片段，特别是脱敏门诊病例必须写成总结分析后的依据。",
        ],
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]


def _fallback_answer(question: str, evidence: list[EvidenceItem], plan: dict[str, Any]) -> str:
    local = [item for item in evidence if item.source_type != "literature"]
    literature = [item for item in evidence if item.source_type == "literature"]
    lines = [
        "## 核心结论",
        f"- 当前问题聚焦：{compact(question, 180)}。",
    ]
    if evidence:
        markers = "、".join(item.marker for item in evidence[:4])
        lines.append(f"- 已检索到可参考证据：{markers}。以下内容仅按证据摘录做保守归纳。")
    else:
        lines.append("- 当前未检索到足够证据，不能形成可靠医学结论。")

    lines.extend(["", "## 病理机制"])
    if local:
        for item in local[:3]:
            lines.append(f"- {item.excerpt} {item.marker}")
    else:
        lines.append("- 本地知识库未返回可用机制性证据，建议补充教材、指南或文献检索。")

    lines.extend(["", "## 典型表现"])
    if len(local) > 3:
        for item in local[3:5]:
            lines.append(f"- 可参考 {item.marker} 中的相关描述：{compact(item.excerpt, 180)}")
    else:
        lines.append("- 证据不足以稳定归纳典型表现。")

    lines.extend(
        [
            "",
            "## 诊断与鉴别要点",
            "- 可围绕病损部位、病程、影像表现、组织病理结果和相关危险因素建立鉴别思路；证据不足时不应直接下最终诊断。",
            "",
            "## 临床处理原则",
            "- 建议结合面诊检查、影像资料和必要的病理检查判断；此处不提供具体处方或替代临床决策。",
            "",
            "## 证据不足或争议点",
        ]
    )
    if literature:
        lines.append("- 已补充外部文献，但文献结论需结合研究类型、样本量和适用人群审慎解读。")
    else:
        lines.append("- 未纳入外部文献补充，若问题涉及最新进展，建议开启 MCP 文献检索。")

    lines.extend(["", "## 引用依据"])
    for item in evidence:
        title = item.title or item.source
        lines.append(f"- {item.marker} {title}：{compact(item.excerpt, 180)}")

    lines.extend(
        [
            "",
            "## 免责声明",
            "以上内容仅用于口腔病理知识学习和临床资料查证参考，不替代医生面诊、影像判读、病理检查和最终诊疗决定。",
        ]
    )
    return "\n".join(lines)


def _strip_emoji(text: str) -> str:
    return re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]+", "", text or "")


def _ensure_disclaimer(answer: str) -> str:
    if "不替代" in answer or "仅用于" in answer:
        return answer
    return (
        answer.rstrip()
        + "\n\n## 免责声明\n以上内容仅用于口腔病理知识学习和临床资料查证参考，不替代医生面诊、影像判读、病理检查和最终诊疗决定。"
    )


def _safety_notes(answer: str) -> list[str]:
    notes: list[str] = []
    if not re.search(r"\[(依据|文献)\d+\]", answer or ""):
        notes.append("生成内容未检测到标准引用标记，建议人工复核。")
    if re.search(r"\b\d+(\.\d+)?\s*(mg|g|ml|bid|tid|qd|iu)\b", answer or "", re.IGNORECASE):
        notes.append("生成内容疑似包含具体处方剂量，需人工复核后使用。")
    if re.search(r"(确诊为|明确诊断为|一定是|必须立即)", answer or ""):
        notes.append("生成内容包含较强诊断措辞，需结合面诊和检查复核。")
    if "不替代" not in (answer or "") and "仅用于" not in (answer or ""):
        notes.append("生成内容未检测到免责声明。")
    return notes


async def run_pathology_ask(question: str, top_k: int = 8, allow_web: bool = True) -> dict[str, Any]:
    question = (question or "").strip()
    if not question:
        return {
            "ok": False,
            "module": "pathology_qa",
            "message": "question is required",
            "answer": "",
            "citations": [],
            "mcp_articles": [],
            "normalized_query": {},
            "safety_notes": ["问题为空。"],
            "errors": [],
        }

    errors: list[str] = []
    plan, plan_errors = normalize_pathology_query(question)
    errors.extend(plan_errors)

    local_query = str(plan.get("local_query_zh") or question)
    local_items, retrieval_meta = collect_local_evidence(local_query, top_k)

    articles: list[dict[str, Any]] = []
    literature_items: list[EvidenceItem] = []
    mcp_used = False
    if allow_web and _needs_literature(question, plan, local_items):
        mcp_used = True
        try:
            literature_query = str(plan.get("literature_query_en") or question)
            literature = await run_literature_search(
                literature_query,
                months=int(plan.get("recommended_months") or 60),
                limit=5,
            )
            articles = literature.get("articles") or []
            errors.extend(literature.get("errors") or [])
            literature_items = collect_literature_evidence(articles, 5)
        except Exception as exc:
            errors.append(f"MCP literature fallback failed: {exc}")

    evidence = local_items + literature_items
    answer_source = "fallback"
    try:
        if is_llm_configured("PATHOLOGY_LLM_API_KEY", "QWEN_API_KEY", "DASHSCOPE_API_KEY"):
            answer = chat_completion(
                _build_messages(question, plan, evidence[:12]),
                temperature=0.12,
                max_tokens=2600,
                api_key_names=("PATHOLOGY_LLM_API_KEY", "QWEN_API_KEY", "DASHSCOPE_API_KEY"),
                base_url_names=("PATHOLOGY_LLM_BASE_URL", "QWEN_BASE_URL", "DASHSCOPE_BASE_URL"),
                model_names=("PATHOLOGY_LLM_MODEL", "QWEN_MODEL", "DASHSCOPE_MODEL"),
            )
            answer_source = "llm"
        else:
            answer = _fallback_answer(question, evidence, plan)
    except (LLMConfigurationError, LLMRequestError, RuntimeError) as exc:
        errors.append(f"LLM answer fallback used: {exc}")
        answer = _fallback_answer(question, evidence, plan)

    answer = _ensure_disclaimer(_strip_emoji(answer))

    return {
        "ok": True,
        "module": "pathology_qa",
        "question": question,
        "answer": answer,
        "answer_source": answer_source,
        "normalized_query": plan,
        "local_query": local_query,
        "mcp_used": mcp_used,
        "retrieval": retrieval_meta,
        "citations": [item.to_dict() for item in evidence],
        "mcp_articles": articles,
        "safety_notes": _safety_notes(answer),
        "errors": errors,
    }
