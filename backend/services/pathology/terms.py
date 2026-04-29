from __future__ import annotations

import json
import re
from typing import Any

from backend.services.literature.terms import resolve_terms
from backend.services.llm_client import chat_completion, is_llm_configured


MAX_MONTH_WINDOW = 120

PATHOLOGY_TERM_RULES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("阻生牙", "阻生齿", "智齿", "第三磨牙"), ("impacted tooth", "impacted third molar")),
    (("牙源性囊肿", "颌骨囊肿", "根尖囊肿", "含牙囊肿"), ("odontogenic cyst", "dentigerous cyst", "radicular cyst")),
    (("根尖周炎", "根尖周病变", "根尖病变"), ("apical periodontitis", "periapical lesion")),
    (("牙髓炎", "牙髓病"), ("pulpitis", "dental pulp disease")),
    (("牙周炎", "牙周病"), ("periodontitis", "periodontal disease")),
    (("口腔鳞癌", "鳞状细胞癌", "口腔癌"), ("oral squamous cell carcinoma", "oral cancer")),
    (("白斑", "口腔白斑"), ("oral leukoplakia",)),
    (("扁平苔藓", "口腔扁平苔藓"), ("oral lichen planus",)),
    (("黏膜病", "口腔黏膜病"), ("oral mucosal disease",)),
    (("成釉细胞瘤",), ("ameloblastoma",)),
    (("牙瘤",), ("odontoma",)),
    (("龋病", "龋齿"), ("dental caries", "tooth decay")),
)

FOCUS_RULES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("机制", "原理", "病因", "为什么", "发病"), ("pathogenesis", "etiology")),
    (("表现", "症状", "特征", "病理改变"), ("clinical features", "pathological features")),
    (("鉴别", "区别", "诊断"), ("differential diagnosis", "diagnosis")),
    (("治疗", "处理", "原则", "管理"), ("management", "treatment principles")),
    (("风险", "危险", "癌变", "预后"), ("risk factors", "prognosis")),
    (("最新", "近年", "研究", "文献", "循证", "进展", "指南更新"), ("recent evidence", "research progress")),
)

LITERATURE_TRIGGERS = (
    "最新",
    "近年",
    "最近",
    "研究",
    "文献",
    "循证",
    "进展",
    "指南更新",
    "meta",
    "review",
    "pubmed",
    "elsevier",
)


def _json_from_text(text: str) -> dict[str, Any]:
    value = (text or "").strip()
    if value.startswith("```"):
        value = value.strip("`").removeprefix("json").strip()
    start = value.find("{")
    end = value.rfind("}")
    if start >= 0 and end > start:
        value = value[start : end + 1]
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else {}


def _unique(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if item and item not in result:
            result.append(item)
    return result


def _quote_group(terms: list[str]) -> str:
    if not terms:
        return ""
    return "(" + " OR ".join(f'"{term}"' for term in terms) + ")"


def _safe_months(value: Any, default: int = 60) -> int:
    try:
        months = int(value)
    except (TypeError, ValueError):
        months = default
    return max(1, min(months, MAX_MONTH_WINDOW))


def fallback_normalize_pathology_query(question: str) -> dict[str, Any]:
    question = (question or "").strip()
    entities_zh: list[str] = []
    entities_en: list[str] = []
    focus: list[str] = []

    for triggers, terms in PATHOLOGY_TERM_RULES:
        if any(trigger.lower() in question.lower() for trigger in triggers):
            entities_zh.append(triggers[0])
            entities_en.extend(terms)

    for triggers, terms in FOCUS_RULES:
        if any(trigger.lower() in question.lower() for trigger in triggers):
            focus.extend(terms)

    literature_terms = resolve_terms(question).to_dict()
    if not entities_en:
        entities_en.extend(str(term) for term in literature_terms.get("english_terms") or [])
    if not focus:
        focus.extend(["pathogenesis", "clinical features", "diagnosis"])

    local_query = " ".join(_unique(entities_zh + re.findall(r"[\u4e00-\u9fff]{2,8}", question))) or question
    disease_group = _quote_group(_unique(entities_en)[:5])
    focus_group = _quote_group(_unique(focus)[:5])
    english_query = " AND ".join(part for part in (disease_group, focus_group) if part)
    if not english_query:
        english_query = str(literature_terms.get("recommended_keywords") or question)

    need_literature = any(trigger.lower() in question.lower() for trigger in LITERATURE_TRIGGERS)

    return {
        "original_question": question,
        "question_type": "pathology_knowledge",
        "entities_zh": _unique(entities_zh),
        "entities_en": _unique(entities_en),
        "focus": _unique(focus),
        "local_query_zh": local_query,
        "literature_query_en": english_query,
        "need_literature": need_literature,
        "recommended_months": _safe_months(literature_terms.get("recommended_months"), 60),
        "source": "rules",
    }


def _build_messages(question: str) -> list[dict[str, str]]:
    schema = {
        "original_question": question,
        "question_type": "pathology_knowledge",
        "entities_zh": ["中文医学实体"],
        "entities_en": ["English MeSH-like terms"],
        "focus": ["pathogenesis", "clinical features", "differential diagnosis"],
        "local_query_zh": "用于中文本地知识库检索的短查询",
        "literature_query_en": "用于 PubMed/Elsevier 的英文布尔检索式",
        "need_literature": False,
        "recommended_months": 60,
    }
    system = (
        "你是口腔病理检索规划助手。你的任务不是回答医学问题，而是把用户问题转换为"
        "本地知识库检索词和英文文献检索式。只输出严格 JSON，不要输出 Markdown。"
        "不要给诊断、治疗建议、处方或医学结论。"
        "question_type 只能是 pathology_knowledge、clinical_case、literature_update 或 unclear。"
        "need_literature 仅在用户询问最新研究/文献/循证/指南更新，或问题明显需要外部证据时为 true。"
    )
    user = {
        "question": question,
        "required_json_schema": schema,
        "notes": [
            "local_query_zh 使用中文核心实体和关注维度，适合教材、指南、病例知识库检索。",
            "literature_query_en 使用英文关键词，可包含 OR/AND 和引号。",
            "recommended_months 限制在 1 到 120。",
        ],
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]


def _normalize_plan(raw: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    result = dict(fallback)
    for key in (
        "original_question",
        "question_type",
        "local_query_zh",
        "literature_query_en",
    ):
        value = str(raw.get(key) or "").strip()
        if value:
            result[key] = value

    for key in ("entities_zh", "entities_en", "focus"):
        value = raw.get(key)
        if isinstance(value, list):
            result[key] = _unique([str(item) for item in value])

    if isinstance(raw.get("need_literature"), bool):
        result["need_literature"] = raw["need_literature"]
    result["recommended_months"] = _safe_months(raw.get("recommended_months"), result.get("recommended_months", 60))
    result["source"] = "llm"

    if not result.get("local_query_zh"):
        result["local_query_zh"] = fallback["local_query_zh"]
    if not result.get("literature_query_en"):
        result["literature_query_en"] = fallback["literature_query_en"]
    return result


def normalize_pathology_query(question: str) -> tuple[dict[str, Any], list[str]]:
    fallback = fallback_normalize_pathology_query(question)
    errors: list[str] = []
    if not is_llm_configured("PATHOLOGY_LLM_API_KEY", "QWEN_API_KEY", "DASHSCOPE_API_KEY"):
        return fallback, errors

    try:
        raw_text = chat_completion(
            _build_messages(question),
            temperature=0.05,
            max_tokens=900,
            api_key_names=("PATHOLOGY_LLM_API_KEY", "QWEN_API_KEY", "DASHSCOPE_API_KEY"),
            base_url_names=("PATHOLOGY_LLM_BASE_URL", "QWEN_BASE_URL", "DASHSCOPE_BASE_URL"),
            model_names=("PATHOLOGY_LLM_MODEL", "QWEN_MODEL", "DASHSCOPE_MODEL"),
        )
        return _normalize_plan(_json_from_text(raw_text), fallback), errors
    except Exception as exc:
        errors.append(f"Term normalization fallback used: {exc}")
        return fallback, errors
