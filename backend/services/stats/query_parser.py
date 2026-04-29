from __future__ import annotations

from dataclasses import dataclass
from collections import Counter
from difflib import SequenceMatcher
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
    target_matched: bool = True
    target_user_term: str = ""
    target_field: str = ""
    target_confidence: float = 1.0
    target_reason: str = ""
    match_type: str = "all"
    disease_group: str = ""
    matched_values: tuple[str, ...] = ()


@dataclass(frozen=True)
class CandidateTerm:
    id: str
    term: str
    field: str
    filter_mode: str
    count: int
    score: float
    candidate_type: str = "field_value"
    values: tuple[str, ...] = ()


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
    "年",
    "月",
    "季度",
    "近",
    "最近",
    "本地",
    "全部",
    "总量",
    "总数",
}

GENERIC_SCOPE_TARGETS = {
    "口腔科",
    "口腔门诊",
    "口腔颌面外科",
    "口腔颌面外科门诊",
    "本科室",
    "本门诊",
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

TERM_ALIASES = {
    "阻生齿": "阻生牙",
    "智齿": "阻生牙",
    "智慧齿": "阻生牙",
    "第三磨牙": "阻生牙",
    "龋齿": "龋病",
    "蛀牙": "龋病",
    "虫牙": "龋病",
}

DISEASE_GROUPS: dict[str, tuple[str, ...]] = {
    "阻生牙/埋伏牙类": ("阻生牙", "埋伏牙"),
    "龋病类": ("龋病", "乳牙深龋", "恒牙深龋", "龋(牙)", "恒牙浅龋"),
    "牙周病类": ("慢性牙周炎", "牙周病", "重度慢性牙周炎", "轻度慢性牙周炎", "牙周脓肿", "急性牙周脓肿"),
    "根尖周病/根尖囊肿类": ("慢性根尖牙周炎", "急性根尖周炎", "根尖周囊肿", "根尖囊肿", "根尖脓肿"),
    "牙髓病类": ("牙髓炎", "慢性牙髓炎", "牙髓和根尖周组织疾病"),
    "牙体缺损/残根残冠类": ("残根", "残冠", "牙体缺损", "牙齿楔状缺损", "牙过度磨耗", "牙发育疾患", "过小牙"),
    "牙列/咬合异常类": ("牙列缺损", "牙列不齐", "错合畸形", "错合畸形安氏Ⅱ类", "牙齿间隙", "牙槽突不齐"),
    "多生牙/乳牙滞留/萌出异常类": ("额外牙［多生牙］", "乳牙滞留", "恒牙萌出过迟", "低位乳牙", "萌出性龈炎"),
    "牙外伤/牙折类": (
        "牙折裂",
        "牙根纵裂",
        "根折",
        "冠折",
        "牙折断",
        "牙震荡",
        "冠根联合折",
        "完全牙脱位",
        "部分牙脱位",
        "创伤性牙脱位",
        "牙槽骨骨折",
    ),
    "颌骨囊肿/肿物类": (
        "下颌骨囊肿",
        "上颌骨囊肿",
        "颌骨含牙囊肿",
        "颌骨肿物",
        "下颌骨良性肿瘤",
        "颌骨纤维结构发育不良",
        "上颌窦囊肿",
        "鼻腭囊肿",
    ),
    "口腔软组织肿物/囊肿类": (
        "口腔肿物",
        "口腔黏液腺囊肿",
        "牙龈肿物",
        "舌下腺囊肿",
        "唇黏液囊肿",
        "牙龈瘤",
        "舌肿物",
        "颊部肿物",
        "唇肿瘤",
        "皮肤肿物",
        "血管瘤",
        "纤维瘤病",
    ),
    "口腔黏膜/溃疡类": (
        "口腔阿弗他溃疡",
        "口腔黏膜溃疡",
        "口腔黏膜下纤维化",
        "创伤性溃疡",
        "舌溃疡",
        "腭部溃疡",
        "口腔感觉异常",
        "外源性色素沉着",
    ),
    "感染/炎症类": (
        "颊间隙感染",
        "眶下间隙感染",
        "颊部炎症",
        "下颌下腺炎",
        "急性腮腺炎",
        "慢性腮腺炎",
        "阻塞性腮腺炎",
        "扁桃体炎",
        "颌骨骨髓炎",
        "骨髓炎",
        "放射性颌骨骨髓炎",
    ),
    "口腔修复/种植相关类": ("种植体周围炎", "牙槽嵴增大", "牙槽骨缺损", "义齿修复"),
    "颞下颌关节/神经痛类": ("颞下颌关节痛", "颞下颌关节紊乱病", "三叉神经痛", "偏头痛", "张口受限"),
    "系带/唇腭裂类": ("唇系带短缩", "舌系带过短", "颊系带附着异常", "唇裂伤", "唇挫伤"),
    "术后/复诊状态类": ("手术后拆除缝线", "手术后恢复期", "拆线术后恢复期", "治疗后恢复期"),
    "其他症状/检查类": ("牙科检查", "牙痛", "疼痛", "食物嵌塞", "牙龈出血", "面部肿胀"),
}

GROUP_ALIASES: dict[str, str] = {
    "阻生牙": "阻生牙/埋伏牙类",
    "智齿": "阻生牙/埋伏牙类",
    "阻生齿": "阻生牙/埋伏牙类",
    "第三磨牙": "阻生牙/埋伏牙类",
    "埋伏牙": "阻生牙/埋伏牙类",
    "龋病": "龋病类",
    "龋齿": "龋病类",
    "蛀牙": "龋病类",
    "虫牙": "龋病类",
    "龋坏": "龋病类",
    "牙周炎": "牙周病类",
    "根尖炎": "根尖周病/根尖囊肿类",
    "根尖周炎": "根尖周病/根尖囊肿类",
    "牙髓炎": "牙髓病类",
    "牙折": "牙外伤/牙折类",
    "牙外伤": "牙外伤/牙折类",
    "囊肿": "颌骨囊肿/肿物类",
    "肿物": "口腔软组织肿物/囊肿类",
    "口腔溃疡": "口腔黏膜/溃疡类",
    "溃疡": "口腔黏膜/溃疡类",
    "拆线": "术后/复诊状态类",
    "术后": "术后/复诊状态类",
}

FIELD_SPECS: tuple[tuple[str, str, str], ...] = (
    ("diagnosis_name", "diagnosis", "诊断名称"),
    ("operation_name", "operation", "操作名称"),
    ("department", "department", "就诊科室"),
    ("register_type", "register_type", "挂号类别"),
)

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


def _normalize_query_terms(query: str) -> str:
    normalized = query or ""
    for alias, canonical in TERM_ALIASES.items():
        normalized = normalized.replace(alias, canonical)
    return normalized


def _normalize_keyword(keyword: str) -> str:
    return TERM_ALIASES.get((keyword or "").strip(), (keyword or "").strip())


def _strip_time_words(query: str) -> str:
    text = query or ""
    text = re.sub(r"20\d{2}\s*年?", " ", text)
    text = re.sub(r"\d{1,2}\s*月", " ", text)
    text = re.sub(r"第?\s*[一二三四1234]\s*季度|Q[1-4]", " ", text, flags=re.I)
    text = re.sub(r"(上半年|下半年|最近|近|后|末|最后)\s*[一二三四两俩三四五六七八九十\d]*\s*个?月?", " ", text)
    return text


def _target_text(query: str) -> str:
    text = _normalize_query_terms(_strip_time_words(query))
    for word in sorted(STOPWORDS, key=len, reverse=True):
        text = text.replace(word, " ")
    text = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _has_specific_target(query: str) -> bool:
    target = _target_text(query)
    if not target:
        return False
    return target not in GENERIC_SCOPE_TARGETS


def _value_counts(cases: list[ClinicCase], attr: str) -> Counter[str]:
    values: Counter[str] = Counter()
    for case in cases:
        value = (getattr(case, attr) or "").strip()
        if value and value != "???":
            values[value] += 1
    return values


def _diagnosis_whitelist(cases: list[ClinicCase]) -> set[str]:
    return set(_value_counts(cases, "diagnosis_name"))


def _active_group_values(group_name: str, cases: list[ClinicCase]) -> tuple[str, ...]:
    available = _diagnosis_whitelist(cases)
    return tuple(value for value in DISEASE_GROUPS.get(group_name, ()) if value in available)


def _group_count(group_name: str, cases: list[ClinicCase]) -> int:
    values = set(_active_group_values(group_name, cases))
    if not values:
        return 0
    return sum(1 for case in cases if (case.diagnosis_name or "").strip() in values)


def _similarity(user_term: str, field_value: str) -> float:
    user_term = _normalize_keyword(user_term.strip())
    field_value = field_value.strip()
    if not user_term or not field_value:
        return 0.0
    if user_term == field_value:
        return 1.0
    if user_term in field_value or field_value in user_term:
        return 0.92
    user_chars = set(user_term)
    value_chars = set(field_value)
    overlap = len(user_chars & value_chars) / max(len(user_chars), 1)
    ratio = SequenceMatcher(None, user_term, field_value).ratio()
    if overlap >= 0.8:
        return max(0.72, ratio)
    if ratio >= 0.72:
        return ratio
    return 0.0


def _recall_candidates(query: str, cases: list[ClinicCase], limit: int = 24) -> list[CandidateTerm]:
    normalized_query = _normalize_query_terms(query)
    target = _target_text(normalized_query)
    if not target:
        return []

    user_terms = [target]
    user_terms.extend(term for term in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,}", target) if term not in user_terms)
    candidates: list[CandidateTerm] = []

    for alias, group_name in GROUP_ALIASES.items():
        if alias in normalized_query or alias in target:
            values = _active_group_values(group_name, cases)
            if values:
                candidates.append(
                    CandidateTerm(
                        id=f"group:{len(candidates)}",
                        term=group_name,
                        field="diagnosis_name",
                        filter_mode="diagnosis",
                        count=_group_count(group_name, cases),
                        score=1.0,
                        candidate_type="disease_group",
                        values=values,
                    )
                )

    for group_name, group_values in DISEASE_GROUPS.items():
        values = tuple(value for value in group_values if value in _diagnosis_whitelist(cases))
        if not values:
            continue
        score = max(
            [_similarity(term, group_name) for term in user_terms]
            + [_similarity(term, value) for term in user_terms for value in values],
            default=0.0,
        )
        if score >= 0.62:
            candidates.append(
                CandidateTerm(
                    id=f"group:{len(candidates)}",
                    term=group_name,
                    field="diagnosis_name",
                    filter_mode="diagnosis",
                    count=sum(1 for case in cases if (case.diagnosis_name or "").strip() in set(values)),
                    score=round(score, 4),
                    candidate_type="disease_group",
                    values=values,
                )
            )

    for attr, mode, _label in FIELD_SPECS:
        counts = _value_counts(cases, attr)
        for value, count in counts.items():
            score = 0.0
            if value in normalized_query:
                score = 1.0
            else:
                score = max((_similarity(term, value) for term in user_terms), default=0.0)
            if attr in {"department", "register_type"} and not any(word in normalized_query for word in ("科室", "挂号", "号别", "号源", value)):
                score *= 0.55
            if score >= 0.62:
                candidates.append(
                    CandidateTerm(
                        id=f"{mode}:{len(candidates)}",
                        term=value,
                        field=attr,
                        filter_mode=mode,
                        count=count,
                        score=round(score, 4),
                        values=(value,),
                    )
                )
    deduped: dict[tuple[str, str, tuple[str, ...]], CandidateTerm] = {}
    for item in candidates:
        key = (item.candidate_type, item.term, item.values)
        previous = deduped.get(key)
        if previous is None or (item.score, item.count) > (previous.score, previous.count):
            deduped[key] = item
    candidates = list(deduped.values())
    candidates.sort(key=lambda item: (item.score, item.candidate_type == "disease_group", item.count, len(item.term)), reverse=True)
    return candidates[:limit]


def _candidate_by_id(candidates: list[CandidateTerm], candidate_id: str) -> CandidateTerm | None:
    return next((item for item in candidates if item.id == candidate_id), None)


def _preferred_group_candidate(query: str, candidates: list[CandidateTerm]) -> CandidateTerm | None:
    normalized_query = _normalize_query_terms(query)
    target = _target_text(query)
    for alias, group_name in GROUP_ALIASES.items():
        if alias in normalized_query or alias in target:
            for candidate in candidates:
                if candidate.candidate_type == "disease_group" and candidate.term == group_name:
                    return candidate
    return None


def _unmatched_intent(
    query: str,
    cases: list[ClinicCase],
    *,
    source: str,
    user_term: str | None = None,
    reason: str = "本地门诊数据字段中未找到可靠匹配项。",
    llm_error: str | None = None,
) -> StatsIntent:
    year, month, months = _extract_time(query, cases)
    metric = _metric(query)
    return StatsIntent(
        query=query,
        keyword=user_term or _target_text(query),
        filter_mode="none",
        year=year,
        month=month,
        months=months,
        metric=metric,
        visualizations=METRIC_VISUALS.get(metric, METRIC_VISUALS["count"]),
        source=source,
        llm_error=llm_error,
        target_matched=False,
        target_user_term=user_term or _target_text(query),
        target_field="",
        target_confidence=0.0,
        target_reason=reason,
        match_type="unmatched",
        disease_group="",
        matched_values=(),
    )


def _intent_from_candidate(
    query: str,
    cases: list[ClinicCase],
    candidate: CandidateTerm,
    *,
    source: str,
    confidence: float | None = None,
    reason: str = "",
    llm_error: str | None = None,
    obj: dict[str, Any] | None = None,
) -> StatsIntent:
    payload = dict(obj or {})
    payload["keyword"] = candidate.term
    payload["filter_mode"] = candidate.filter_mode
    intent = _sanitize_intent(query, payload, cases, source, llm_error)
    return StatsIntent(
        query=intent.query,
        keyword=intent.keyword,
        filter_mode=intent.filter_mode,
        year=intent.year,
        month=intent.month,
        months=intent.months,
        metric=intent.metric,
        visualizations=intent.visualizations,
        source=intent.source,
        llm_error=intent.llm_error,
        target_matched=True,
        target_user_term=_target_text(query),
        target_field=candidate.field,
        target_confidence=round(float(confidence if confidence is not None else candidate.score), 4),
        target_reason=reason or f"用户表达与本地字段“{candidate.term}”匹配。",
        match_type=candidate.candidate_type,
        disease_group=candidate.term if candidate.candidate_type == "disease_group" else "",
        matched_values=candidate.values or (candidate.term,),
    )


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
    elif year and available:
        months = available
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

    explicit_keyword = "keyword" in obj
    explicit_filter_mode = "filter_mode" in obj
    keyword = _normalize_keyword(str(obj.get("keyword") if explicit_keyword else fallback_keyword or "").strip())
    filter_mode = str(obj.get("filter_mode") if explicit_filter_mode else fallback_mode or "any").strip()
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
        target_matched=True,
        target_user_term=_target_text(query),
        target_field="",
        target_confidence=1.0,
        target_reason="",
        match_type="field_value" if keyword else "all",
        disease_group="",
        matched_values=(keyword,) if keyword else (),
    )


def _rule_intent(query: str, cases: list[ClinicCase], *, source: str = "rules", llm_error: str | None = None) -> StatsIntent:
    query = _normalize_query_terms(query)
    if _has_specific_target(query):
        candidates = _recall_candidates(query, cases)
        if not candidates:
            return _unmatched_intent(query, cases, source=source, llm_error=llm_error)
        best = candidates[0]
        if best.score < 0.7:
            return _unmatched_intent(
                query,
                cases,
                source=source,
                user_term=_target_text(query),
                reason="候选字段相似度不足，未执行模糊统计。",
                llm_error=llm_error,
            )
        return _intent_from_candidate(
            query,
            cases,
            best,
            source=source,
            confidence=best.score,
            reason=f"规则候选召回选择“{best.term}”。",
            llm_error=llm_error,
        )

    year, month, months = _extract_time(query, cases)
    metric = _metric(query)
    return StatsIntent(
        query=query,
        keyword="",
        filter_mode="all",
        year=year,
        month=month,
        months=months,
        metric=metric,
        visualizations=METRIC_VISUALS.get(metric, METRIC_VISUALS["count"]),
        source=source,
        llm_error=llm_error,
        target_matched=True,
        target_user_term="",
        target_field="",
        target_confidence=1.0,
        target_reason="",
        match_type="all",
        disease_group="",
        matched_values=(),
    )


def _llm_intent(query: str, cases: list[ClinicCase]) -> StatsIntent:
    raw_query = query
    normalized_query = _normalize_query_terms(query)
    months = _available_months(cases)
    candidates = _recall_candidates(normalized_query, cases)
    if not _has_specific_target(normalized_query):
        return _sanitize_intent(raw_query, {"keyword": "", "filter_mode": "all"}, cases, "llm")
    if not candidates:
        return _unmatched_intent(raw_query, cases, source="llm", reason="本地诊断、操作、科室、挂号类别字段中没有召回到候选项。")
    preferred_group = _preferred_group_candidate(raw_query, candidates)

    candidate_payload = [
        {
            "id": item.id,
            "canonical_term": item.term,
            "candidate_type": item.candidate_type,
            "field": item.field,
            "filter_mode": item.filter_mode,
            "diagnosis_names": list(item.values),
            "local_count": item.count,
            "recall_score": item.score,
        }
        for item in candidates
    ]
    prompt = f"""
你是口腔颌面外科门诊统计助手，只负责把用户自然语言统计需求解析为 JSON，不计算任何统计值。
本地病例数据可用月份：{months}
本地真实字段候选项：{json.dumps(candidate_payload, ensure_ascii=False)}

请返回严格 JSON，字段如下：
{{
  "matched": true,
  "candidate_id": "只能填写候选项中的 id；没有可靠匹配时为空字符串",
  "confidence": 0.0,
  "reason": "说明用户原词为什么对应这个本地字段；没有可靠匹配时说明原因",
  "time_range": {{"year": "2025或空", "months": ["YYYY-MM"]}},
  "metric": "overview|count|ratio|monthly_trend|treatment_distribution|exam_distribution|register_distribution|diagnosis_distribution",
  "visualizations": ["monthly_trend","operation_distribution","treatment_distribution","exam_aux_distribution","register_type_distribution","operation_by_month"]
}}
规则：
1. candidate_id 只能从候选项中选择，禁止创造新的疾病名、操作名、科室或挂号类别。
2. 优先选择 candidate_type=disease_group 的疾病大类；用户问“龋齿/蛀牙/牙周炎/根尖周炎/牙外伤/囊肿”等泛称时，应选择对应疾病大类而不是单个细分诊断。
3. 如果用户明确询问某个具体诊断，且候选中有 field_value 精确项，可以选择具体诊断。
4. 如果用户表达与候选项没有可靠对应关系，返回 matched=false、candidate_id=""。
5. “相关情况/整体情况/概况”解析为 overview，并尽量给出多种可视化。
6. “2025年后三个月/最近三个月/末三个月”应按本地可用月份解释为 2025-09、2025-10、2025-11。
7. 默认不要返回 diagnosis_distribution，除非用户明确要求诊断分布。
8. LLM 只做字段选择和意图解析，不能编造计数、比例或统计结论。
"""
    content = chat_completion(
        [
            {"role": "system", "content": prompt},
            {"role": "user", "content": raw_query},
        ],
        temperature=0.05,
        max_tokens=700,
        api_key_names=("STATS_LLM_API_KEY", "DECISION_LLM_API_KEY", "QWEN_API_KEY", "DASHSCOPE_API_KEY"),
        base_url_names=("STATS_LLM_BASE_URL", "DECISION_LLM_BASE_URL", "QWEN_BASE_URL", "DASHSCOPE_BASE_URL"),
        model_names=("STATS_LLM_MODEL", "DECISION_LLM_MODEL", "QWEN_MODEL", "DASHSCOPE_MODEL"),
    )
    obj = _extract_json(content)
    if not obj.get("matched", True):
        if candidates and candidates[0].score >= 0.9:
            return _intent_from_candidate(
                raw_query,
                cases,
                candidates[0],
                source="llm_fallback",
                confidence=candidates[0].score,
                reason="LLM 未匹配，但本地候选召回存在高置信精确匹配，已采用本地候选。",
                obj=obj,
            )
        return _unmatched_intent(
            raw_query,
            cases,
            source="llm",
            reason=str(obj.get("reason") or "LLM 未在本地候选字段中找到可靠匹配。"),
        )
    candidate = _candidate_by_id(candidates, str(obj.get("candidate_id") or ""))
    if preferred_group is not None and (candidate is None or candidate.candidate_type != "disease_group"):
        candidate = preferred_group
        obj["reason"] = (
            str(obj.get("reason") or "")
            + f" 已按疾病大类规则归并到“{preferred_group.term}”。"
        ).strip()
    if candidate is None:
        best = candidates[0]
        if best.score < 0.72:
            return _unmatched_intent(
                raw_query,
                cases,
                source="llm_fallback",
                reason="LLM 未返回有效候选项，且规则候选相似度不足。",
            )
        candidate = best
    confidence = float(obj.get("confidence") or candidate.score)
    if confidence < 0.62:
        return _unmatched_intent(
            raw_query,
            cases,
            source="llm",
            reason=str(obj.get("reason") or "LLM 置信度不足，未执行统计。"),
        )
    return _intent_from_candidate(
        raw_query,
        cases,
        candidate,
        source="llm",
        confidence=confidence,
        reason=str(obj.get("reason") or ""),
        obj=obj,
    )


def parse_intent(query: str, cases: list[ClinicCase]) -> StatsIntent:
    query = (query or "").strip()
    if is_llm_configured("STATS_LLM_API_KEY", "DECISION_LLM_API_KEY", "QWEN_API_KEY", "DASHSCOPE_API_KEY"):
        try:
            return _llm_intent(query, cases)
        except (LLMConfigurationError, LLMRequestError, ValueError, json.JSONDecodeError) as exc:
            return _rule_intent(query, cases, source="llm_fallback", llm_error=str(exc))
    return _rule_intent(query, cases)
