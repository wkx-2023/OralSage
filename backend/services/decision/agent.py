from __future__ import annotations

import json
import re
from typing import Any

from backend.services.llm_client import LLMConfigurationError, LLMRequestError, chat_completion, is_llm_configured
from backend.services.literature import run_literature_search
from backend.services.retrieval import EvidenceChunk, search_knowledge_base


REQUIRED_CLINICAL_POINTS = {
    "年龄/性别": ("年龄", "性别", "男", "女"),
    "主诉": ("主诉", "疼", "痛", "肿", "敏感", "不适", "松动", "出血"),
    "现病史": ("病史", "天", "周", "月", "年", "冷热", "自发痛", "夜间痛", "咬合痛"),
    "口内检查": ("检查", "叩诊", "探诊", "松动", "牙周袋", "龋", "牙髓活力"),
    "影像资料": ("影像", "x线", "X线", "CBCT", "根尖片", "全景片"),
    "禁忌证/风险": ("过敏", "妊娠", "抗凝", "糖尿病", "高血压", "心脏", "免疫", "双膦酸盐"),
}


def _compact(text: str, limit: int = 900) -> str:
    value = " ".join((text or "").split())
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def _missing_fields(case_text: str) -> list[str]:
    missing: list[str] = []
    for label, hints in REQUIRED_CLINICAL_POINTS.items():
        if not any(hint in case_text for hint in hints):
            missing.append(label)
    return missing


def _build_query(case_text: str) -> str:
    cleaned = re.sub(r"\s+", " ", case_text).strip()
    anchors = []
    for keyword in (
        "阻生", "第三磨牙", "智齿", "牙髓炎", "根尖周炎", "龋", "牙周炎", "种植",
        "颞下颌", "黏膜", "拔除", "根管", "CBCT", "下牙槽神经", "冷热敏感",
    ):
        if keyword in cleaned:
            anchors.append(keyword)
    if anchors:
        return " ".join(dict.fromkeys(anchors + ["诊疗指南", "适应证", "禁忌证", "治疗方案"]))
    return f"{cleaned[:160]} 口腔 临床决策 鉴别诊断 检查 治疗 指南"


def _format_evidence_for_prompt(chunks: list[EvidenceChunk]) -> str:
    if not chunks:
        return "未检索到可用本地证据。"
    lines: list[str] = []
    for idx, chunk in enumerate(chunks, 1):
        title = f"《{chunk.source}》"
        if chunk.title:
            title += f" - {chunk.title}"
        lines.append(
            f"[{idx}] 来源：{title}\n"
            f"类型：{chunk.source_type}\n"
            f"摘录：{chunk.excerpt(520)}"
        )
    return "\n\n".join(lines)


def _citation_dicts(chunks: list[EvidenceChunk]) -> list[dict[str, Any]]:
    citations = []
    for idx, chunk in enumerate(chunks, 1):
        item = chunk.to_dict()
        item["marker"] = f"[依据{idx}]"
        citations.append(item)
    return citations


def _fallback_answer(case_text: str, chunks: list[EvidenceChunk], missing: list[str], web_articles: list[dict[str, Any]]) -> str:
    lines = [
        "## 一、病例信息完整性核查",
    ]
    if missing:
        lines.append("以下信息建议补充后再做正式临床判断：" + "、".join(missing) + "。")
    else:
        lines.append("病例核心信息较完整，但仍需由接诊医生结合现场检查确认。")

    lines.extend(
        [
            "",
            "## 二、初步问题归纳",
            f"当前病例摘要：{_compact(case_text, 420)}",
            "",
            "## 三、鉴别诊断思路",
        ]
    )

    if chunks:
        for idx, chunk in enumerate(chunks[:3], 1):
            lines.append(f"- 需结合 {chunk.source} 的相关内容判断：{chunk.excerpt(160)} [依据{idx}]")
    else:
        lines.append("- 当前本地知识库未检索到足够依据，不能给出具体诊疗方向。")

    lines.extend(
        [
            "",
            "## 四、检查建议",
            "- 建议补全疼痛性质、诱因、持续时间、叩诊/探诊/松动度、牙髓活力及必要影像资料，再综合判断。",
            "- 如涉及拔除、种植、根尖外科或与神经管邻近的病变，应根据实际情况考虑 CBCT 等影像评估。",
            "",
            "## 五、治疗参考",
            "- 在证据不足或信息缺失时，不应直接给出确定治疗指令。可先围绕首要诊断假设进行检查验证，再比较保守治疗、牙体牙髓治疗、牙周治疗、外科处理或修复方案的适应证与风险。",
            "- 如存在系统病史、用药史、过敏史、妊娠、抗凝/抗骨吸收药物使用等风险因素，应先完成禁忌证核查。",
            "",
            "## 六、安全边界",
            "以上内容仅作为口腔临床决策支持，不替代医生面诊、影像判读和最终诊疗决定；未提供具体处方剂量。",
        ]
    )
    if web_articles:
        lines.extend(["", "## 七、联网文献补充"])
        for article in web_articles[:3]:
            title = article.get("title") or "未提供标题"
            link = article.get("link") or "未提供链接"
            lines.append(f"- {title}：{link}")
    return "\n".join(lines)


def _build_messages(case_text: str, chunks: list[EvidenceChunk], missing: list[str], web_articles: list[dict[str, Any]]) -> list[dict[str, str]]:
    article_payload = [
        {
            "title": article.get("title"),
            "summary": article.get("summary"),
            "journal": article.get("journal"),
            "publish_date": article.get("publish_date"),
            "link": article.get("link"),
        }
        for article in web_articles[:5]
    ]
    system = (
        "你是口腔病例智能决策支持助手，服务对象是口腔医生和医学生。"
        "你只能基于输入病例、本地证据和联网文献字段进行推理，禁止编造指南、出处、数值、药物剂量或确定诊断。"
        "输出口吻必须是'建议/需考虑/可进一步评估'，不能替代医生做最终诊断。"
        "每条关键诊疗建议后都要引用证据标记，例如[依据1]。"
        "不要开具具体处方剂量。"
    )
    user = {
        "case_text": case_text,
        "missing_fields": missing,
        "local_evidence": _format_evidence_for_prompt(chunks),
        "web_articles": article_payload,
        "required_output": [
            "一、病例信息完整性核查",
            "二、核心问题归纳",
            "三、鉴别诊断：2-3个方向，列支持点和不支持点",
            "四、下一步检查建议",
            "五、治疗参考：首选/备选方案、适应证、风险",
            "六、禁忌证与安全核查",
            "七、引用依据汇总",
            "八、免责声明",
        ],
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]


def _safety_notes(answer: str) -> list[str]:
    notes: list[str] = []
    if not re.search(r"\[依据\d+\]", answer):
        notes.append("生成内容未检测到标准依据标记，前端应提示人工复核。")
    if re.search(r"\b\d+(\.\d+)?\s*(mg|g|ml|片|粒|次/日|bid|tid|qd)\b", answer, re.IGNORECASE):
        notes.append("生成内容疑似包含具体处方剂量，需人工复核后使用。")
    if "仅供" not in answer and "不替代" not in answer:
        notes.append("生成内容未检测到明确免责声明。")
    return notes


def _ensure_disclaimer(answer: str) -> str:
    if "不替代" in answer or "仅供" in answer:
        return answer
    footer = (
        "\n\n## 免责声明\n"
        "以上内容仅作为口腔临床决策支持和资料查证参考，不替代医生面诊、影像判读、病理检查及最终诊疗决定。"
    )
    return answer.rstrip() + footer


async def run_decision_assist(case_text: str, top_k: int = 8, allow_web: bool = True) -> dict[str, Any]:
    case_text = (case_text or "").strip()
    if not case_text:
        return {
            "ok": False,
            "module": "decision_assistant",
            "message": "case_text is required",
            "answer": "",
            "citations": [],
            "missing_fields": [],
            "safety_notes": ["病例文本为空。"],
        }

    missing = _missing_fields(case_text)
    query = _build_query(case_text)
    chunks = search_knowledge_base(query, top_k=top_k)

    web_articles: list[dict[str, Any]] = []
    web_errors: list[str] = []
    if allow_web and len(chunks) < 3:
        try:
            literature = await run_literature_search(query, months=12, limit=3)
            web_articles = literature.get("articles") or []
            web_errors = literature.get("errors") or []
        except Exception as exc:
            web_errors = [str(exc)]

    answer_source = "fallback"
    try:
        if is_llm_configured():
            answer = chat_completion(_build_messages(case_text, chunks, missing, web_articles))
            answer_source = "qwen"
        else:
            answer = _fallback_answer(case_text, chunks, missing, web_articles)
    except (LLMConfigurationError, LLMRequestError, RuntimeError) as exc:
        answer = _fallback_answer(case_text, chunks, missing, web_articles)
        web_errors.append(f"LLM fallback used: {exc}")

    answer = _ensure_disclaimer(answer)

    return {
        "ok": True,
        "module": "decision_assistant",
        "query": query,
        "answer": answer,
        "answer_source": answer_source,
        "citations": _citation_dicts(chunks),
        "missing_fields": missing,
        "safety_notes": _safety_notes(answer),
        "web_articles": web_articles,
        "errors": web_errors,
    }


def run_evidence_search(query: str, top_k: int = 10) -> dict[str, Any]:
    query = (query or "").strip()
    if not query:
        return {"ok": False, "module": "evidence_search", "message": "query is required", "results": []}
    chunks = search_knowledge_base(query, top_k=top_k)
    return {
        "ok": True,
        "module": "evidence_search",
        "query": query,
        "count": len(chunks),
        "results": _citation_dicts(chunks),
    }
