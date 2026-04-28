from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any

from backend.services.llm_client import LLMConfigurationError, LLMRequestError, chat_completion, is_llm_configured

from .loader import ClinicCase


@dataclass(frozen=True)
class StatsIntent:
    query: str
    keyword: str
    filter_mode: str
    year: str | None
    month: str | None
    months: tuple[str, ...]
    metric: str
    visualizations: tuple[str, ...]
    source: str
    llm_error: str | None = None


STOPWORDS = {
    "统计",
    "病历",
    "病例",
    "门诊",
    "就诊",
    "相关",
    "情况",
    "多少",
    "几例",
    "占比",
    "比例",
    "分布",
    "趋势",
    "治疗",
    "处理",
    "方法",
    "方式",
    "手术",
    "操作",
    "检查",
    "辅查",
    "每月",
    "月度",
    "按月",
    "有哪些",
    "患者",
    "数据",
    "分析",
    "可视化",
    "后三个月",
    "最近三个月",
    "上半年",
    "下半年",
}

COMMON_TERMS = {
    "阻生牙",
    "智齿",
    "慢性根尖牙周炎",
    "根尖牙周炎",
    "慢性牙周炎",
    "牙周炎",
    "牙列缺损",
    "龋齿",
    "残根",
    "残冠",
    "拔牙",
    "拆线",
    "缝线去除",
    "复查",
    "种植",
    "义齿修复",
    "牙科检查",
    "多生牙",
    "乳牙滞留",
    "牙髓炎",
}

METRIC_VISUALS: dict[str, tuple[str, ...]] = {
    "overview": (
        "monthly_trend",
        "operation_distribution",
        "treatment_distribution",
        "exam_aux_distribution",
        "register_type_distribution",
        "operation_by_month",
    ),
    "count": ("operation_distribution",),
    "ratio": ("operation_distribution", "monthly_trend"),
    "monthly_trend": ("monthly_trend", "operation_by_month"),
    "treatment_distribution": ("operation_distribution", "treatment_distribution", "operation_by_month"),
    "exam_distribution": ("exam_aux_distribution",),
    "register_distribution": ("register_type_distribution", "monthly_trend"),
    "diagnosis_distribution": ("diagnosis_distribution", "monthly_trend"),
}


def _available_months(cases: list[ClinicCase], year: str | None = None) -> list[str]:
    months = sorted({case.visit_month for case in cases if case.visit_month})
    if year:
        months = [month for month in months if month.startswith(f"{year}-")]
    return months


def _extract_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    if fenced:
        text = fenced.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    obj = json.loads(text)
    if not isinstance(obj, dict):
        raise ValueError("LLM result is not a JSON object")
    return obj


def _metric(query: str) -> str:
    if any(word in query for word in ("相关情况", "整体情况", "综合分析", "概况", "画像", "总览")):
        return "overview"
    if any(word in query for word in ("每月", "按月", "月度", "趋势", "变化")):
        return "monthly_trend"
    if any(word in query for word in ("治疗", "处置", "处理", "手术", "操作", "方法", "方式")):
        return "treatment_distribution"
    if any(word in query for word in ("检查", "辅查", "影像", "分类")):
        return "exam_distribution"
    if any(word in query for word in ("挂号", "号别", "号源")):
        return "register_distribution"
    if any(word in query for word in ("诊断分布", "诊断类别", "哪些诊断")):
        return "diagnosis_distribution"
    if any(word in query for word in ("占比", "比例", "构成")):
        return "ratio"
    return "count"


def _normalize_month(year: str | None, month: int) -> str | None:
    if 1 <= month <= 12:
        return f"{year or '2025'}-{month:02d}"
    return None


def _extract_time(query: str, cases: list[ClinicCase]) -> tuple[str | None, str | None, tuple[str, ...]]:
    year = None
    month = None
    months: list[str] = []

    year_match = re.search(r"(20\d{2})\s*年?", query)
    if year_match:
        year = year_match.group(1)

    month_match = re.search(r"(?:20\d{2}\s*年?\s*)?(\d{1,2})\s*月", query)
    if month_match:
        normalized = _normalize_month(year, int(month_match.group(1)))
        if normalized:
            month = normalized

    available = _available_months(cases, year)
    if re.search(r"(后|末|最后|最近)\s*[三3]\s*个?月", query):
        months = available[-3:]
    elif "上半年" in query:
        months = [m for m in available if 1 <= int(m[-2:]) <= 6]
    elif "下半年" in query:
        months = [m for m in available if 7 <= int(m[-2:]) <= 12]
    else:
        q_match = re.search(r"(?:第?\s*([一二三四1234])\s*季度|Q([1-4]))", query, flags=re.I)
        if q_match:
            raw = q_match.group(1) or q_match.group(2)
            quarter_map = {"一": 1, "二": 2, "三": 3, "四": 4}
            quarter = quarter_map.get(raw, int(raw) if raw.isdigit() else 0)
            start = (quarter - 1) * 3 + 1
            months = [m for m in available if start <= int(m[-2:]) <= start + 2]

    if month:
        months = [month]
    return year, month, tuple(months)


def _known_terms(cases: list[ClinicCase]) -> list[str]:
    terms: set[str] = set(COMMON_TERMS)
    for case in cases:
        for value in (case.diagnosis_name, case.operation_name, case.department, case.register_type):
            value = (value or "").strip()
            if len(value) >= 2 and value != "???":
                terms.add(value)
    return sorted(terms, key=len, reverse=True)


def _keyword(query: str, cases: list[ClinicCase]) -> tuple[str, str]:
    for term in _known_terms(cases):
        if term and term in query:
            if term in {case.diagnosis_name for case in cases[:2000]} or term in COMMON_TERMS:
                mode = "diagnosis"
            else:
                mode = "any"
            if term in {"拆线", "缝线去除"}:
                mode = "suture_removal"
            if term in {"拔牙", "复查", "种植", "义齿修复"}:
                mode = "any"
            return term, mode

    candidates = re.findall(r"[\u4e00-\u9fff]{2,}", query)
    for candidate in candidates:
        candidate = candidate.strip()
        if candidate and candidate not in STOPWORDS:
            return candidate, "any"
    return "", "all"


def _clean_visualizations(metric: str, visualizations: Any) -> tuple[str, ...]:
    allowed = {
        "monthly_trend",
        "operation_distribution",
        "treatment_distribution",
        "exam_aux_distribution",
        "register_type_distribution",
        "operation_by_month",
        "diagnosis_distribution",
    }
    if isinstance(visualizations, list):
        cleaned = [str(item) for item in visualizations if str(item) in allowed]
    else:
        cleaned = []
    defaults = list(METRIC_VISUALS.get(metric, METRIC_VISUALS["count"]))
    if cleaned:
        cleaned = defaults + cleaned
    else:
        cleaned = defaults
    if "diagnosis_distribution" in cleaned and metric != "diagnosis_distribution":
        cleaned.remove("diagnosis_distribution")
    return tuple(dict.fromkeys(cleaned))


def _sanitize_intent(
    query: str,
    obj: dict[str, Any],
    cases: list[ClinicCase],
    source: str,
    llm_error: str | None = None,
) -> StatsIntent:
    fallback_year, fallback_month, fallback_months = _extract_time(query, cases)
    fallback_keyword, fallback_mode = _keyword(query, cases)
    available_all = set(_available_months(cases))

    keyword = str(obj.get("keyword") or fallback_keyword or "").strip()
    filter_mode = str(obj.get("filter_mode") or fallback_mode or "any").strip()
    if filter_mode not in {"diagnosis", "operation", "department", "register_type", "suture_removal", "any", "all"}:
        filter_mode = fallback_mode
    if not keyword:
        filter_mode = "all"
    if filter_mode == "register_type":
        register_values = {case.register_type for case in cases if case.register_type}
        if keyword and not any(keyword in value or value in keyword for value in register_values):
            keyword = ""
            filter_mode = "all"
    if filter_mode == "department":
        departments = {case.department for case in cases if case.department}
        if keyword and not any(keyword in value or value in keyword for value in departments):
            keyword = ""
            filter_mode = "all"

    time_range = obj.get("time_range") if isinstance(obj.get("time_range"), dict) else {}
    year = str(time_range.get("year") or obj.get("year") or fallback_year or "").strip() or None

    months_obj = time_range.get("months") or obj.get("months") or []
    months: list[str] = []
    if isinstance(months_obj, list):
        for item in months_obj:
            item = str(item).strip()
            if re.fullmatch(r"20\d{2}-\d{2}", item) and item in available_all:
                months.append(item)
    if fallback_months and re.search(r"(后|末|最后|最近)\s*[三3]\s*个?月", query) and len(months) != 3:
        months = list(fallback_months)
    if not months:
        months = list(fallback_months)
    month = months[0] if len(months) == 1 else fallback_month

    metric = str(obj.get("metric") or _metric(query)).strip()
    if metric not in METRIC_VISUALS:
        metric = _metric(query)

    visualizations = _clean_visualizations(metric, obj.get("visualizations"))
    return StatsIntent(
        query=query,
        keyword=keyword,
        filter_mode=filter_mode,
        year=year,
        month=month,
        months=tuple(dict.fromkeys(months)),
        metric=metric,
        visualizations=visualizations,
        source=source,
        llm_error=llm_error,
    )


def _rule_intent(query: str, cases: list[ClinicCase], *, source: str = "rules", llm_error: str | None = None) -> StatsIntent:
    year, month, months = _extract_time(query, cases)
    keyword, filter_mode = _keyword(query, cases)
    metric = _metric(query)
    return StatsIntent(
        query=query,
        keyword=keyword,
        filter_mode=filter_mode,
        year=year,
        month=month,
        months=months,
        metric=metric,
        visualizations=METRIC_VISUALS.get(metric, METRIC_VISUALS["count"]),
        source=source,
        llm_error=llm_error,
    )


def _llm_intent(query: str, cases: list[ClinicCase]) -> StatsIntent:
    months = _available_months(cases)
    diagnoses = sorted({case.diagnosis_name for case in cases if case.diagnosis_name and case.diagnosis_name != "???"})[:80]
    operations = sorted({case.operation_name for case in cases if case.operation_name and case.operation_name != "???"})[:80]
    prompt = f"""
你是口腔颌面外科门诊统计助手，只负责把用户自然语言统计需求解析为 JSON，不计算任何统计值。
本地病例数据可用月份：{months}
常见诊断示例：{diagnoses}
常见操作示例：{operations}

请返回严格 JSON，字段如下：
{{
  "keyword": "用于筛选病例的核心词，如阻生牙；无筛选则为空字符串",
  "filter_mode": "diagnosis|operation|department|register_type|suture_removal|any|all",
  "time_range": {{"year": "2025或空", "months": ["YYYY-MM"]}},
  "metric": "overview|count|ratio|monthly_trend|treatment_distribution|exam_distribution|register_distribution|diagnosis_distribution",
  "visualizations": ["monthly_trend","operation_distribution","treatment_distribution","exam_aux_distribution","register_type_distribution","operation_by_month"]
}}
规则：
1. “相关情况/整体情况/概况”解析为 overview，并尽量给出多种可视化。
2. “2025年后三个月/最近三个月/末三个月”应按本地可用月份解释为 2025-09、2025-10、2025-11。
3. 默认不要返回 diagnosis_distribution，除非用户明确要求诊断分布。
4. LLM 只解析意图，不能编造计数、比例或结论。
"""
    content = chat_completion(
        [
            {"role": "system", "content": prompt},
            {"role": "user", "content": query},
        ],
        temperature=0.05,
        max_tokens=700,
        api_key_names=("STATS_LLM_API_KEY", "DECISION_LLM_API_KEY", "QWEN_API_KEY", "DASHSCOPE_API_KEY"),
        base_url_names=("STATS_LLM_BASE_URL", "DECISION_LLM_BASE_URL", "QWEN_BASE_URL", "DASHSCOPE_BASE_URL"),
        model_names=("STATS_LLM_MODEL", "DECISION_LLM_MODEL", "QWEN_MODEL", "DASHSCOPE_MODEL"),
    )
    return _sanitize_intent(query, _extract_json(content), cases, "llm")


def parse_intent(query: str, cases: list[ClinicCase]) -> StatsIntent:
    query = (query or "").strip()
    if is_llm_configured("STATS_LLM_API_KEY", "DECISION_LLM_API_KEY", "QWEN_API_KEY", "DASHSCOPE_API_KEY"):
        try:
            return _llm_intent(query, cases)
        except (LLMConfigurationError, LLMRequestError, ValueError, json.JSONDecodeError) as exc:
            return _rule_intent(query, cases, source="llm_fallback", llm_error=str(exc))
    return _rule_intent(query, cases)
