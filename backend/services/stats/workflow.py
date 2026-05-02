from __future__ import annotations

from collections import Counter
import json
import re
from typing import Any

from backend.services.llm_client import LLMConfigurationError, LLMRequestError, chat_completion, is_llm_configured

from .aggregations import build_stats
from .loader import ClinicCase
from .query_parser import (
    CandidateTerm,
    DISEASE_GROUPS,
    GROUP_ALIASES,
    METRIC_VISUALS,
    StatsIntent,
    _available_months,
    _extract_json,
    _extract_time,
    _recall_candidates,
    _target_text,
)


TASK_TYPES = {
    "visit_count",
    "all_overview",
    "disease_distribution",
    "single_disease",
    "multi_disease_compare",
    "operation_distribution",
    "trend",
    "unmatched",
}


def run_stats_workflow(query: str, cases: list[ClinicCase]) -> dict[str, Any]:
    plan = build_stats_plan(query, cases)
    structured = execute_stats_plan(plan, cases)
    report, report_source, report_error = build_stats_report(query, plan, structured)
    if report_error:
        structured.setdefault("errors", []).append(report_error)
    structured["plan"] = plan
    structured["report"] = report
    structured["report_source"] = report_source
    return structured


def build_stats_plan(query: str, cases: list[ClinicCase]) -> dict[str, Any]:
    query = (query or "").strip()
    candidates = _recall_candidates(query, cases, limit=40)
    if is_llm_configured("STATS_PLANNER_LLM_API_KEY", "STATS_LLM_API_KEY", "DECISION_LLM_API_KEY", "QWEN_API_KEY", "DASHSCOPE_API_KEY"):
        try:
            raw_plan = _llm_plan(query, cases, candidates)
            return _validate_plan(query, cases, candidates, raw_plan, source="llm")
        except (LLMConfigurationError, LLMRequestError, ValueError, json.JSONDecodeError, TypeError) as exc:
            plan = _rule_plan(query, cases, candidates)
            plan["source"] = "rules_fallback"
            plan["planner_error"] = str(exc)
            return plan
    return _rule_plan(query, cases, candidates)


def _llm_plan(query: str, cases: list[ClinicCase], candidates: list[CandidateTerm]) -> dict[str, Any]:
    available_months = _available_months(cases)
    candidate_payload = [_candidate_payload(item) for item in candidates]
    group_payload = [
        {"group_name": group, "diagnosis_names": [value for value in values if _diagnosis_count(cases, value) > 0]}
        for group, values in DISEASE_GROUPS.items()
    ]
    group_payload = [item for item in group_payload if item["diagnosis_names"]]
    prompt = f"""
你是门诊统计 Agent 的 Planner。你只负责理解用户自然语言并输出统计计划，不回答问题、不编造数字。

本地数据真实可用月份：{available_months}
候选目标（只能选择这些 id）：{json.dumps(candidate_payload, ensure_ascii=False)}
疾病大类表（只能选择这些 group_name）：{json.dumps(group_payload, ensure_ascii=False)}

合法 task_type：
- visit_count：就诊人数、就诊量、门诊总量、多少条记录
- all_overview：门诊整体情况、总体概况
- disease_distribution：病种/疾病大类/诊断分布
- single_disease：单个疾病大类或单个诊断的统计
- multi_disease_compare：多个疾病大类/诊断的对比
- operation_distribution：操作、治疗方式、手术方式分布
- trend：趋势、月度变化
- unmatched：本地候选中没有可靠匹配

请输出严格 JSON：
{{
  "task_type": "visit_count|all_overview|disease_distribution|single_disease|multi_disease_compare|operation_distribution|trend|unmatched",
  "target_ids": ["候选目标 id，可为空"],
  "months": ["YYYY-MM，可为空，后端会校验"],
  "metrics": ["count","ratio","monthly_trend","operation_distribution","treatment_distribution","disease_distribution"],
  "reason": "简短说明"
}}

规则：
1. 用户问“就诊人数/人数/门诊总量/就诊量”时，task_type 必须是 visit_count，不要把“人数”当成疾病。
2. 用户问“口腔科门诊情况/门诊整体情况”时，task_type 是 all_overview。
3. 用户问“病种分布/疾病分布/各类疾病”时，task_type 是 disease_distribution。
4. 用户问单个疾病，如“龋齿/蛀牙/牙周炎/智齿”，选择相应疾病大类候选。
5. 用户问多个疾病或“对比/分别/和/与”，选择多个 target_ids，task_type 是 multi_disease_compare。
6. target_ids 只能从候选目标中选择；如果没有可靠目标且不是全局统计，返回 unmatched。
7. 不能输出病例数、占比、结论。
""".strip()
    content = chat_completion(
        [{"role": "system", "content": prompt}, {"role": "user", "content": query}],
        temperature=0.0,
        max_tokens=900,
        api_key_names=("STATS_PLANNER_LLM_API_KEY", "STATS_LLM_API_KEY", "DECISION_LLM_API_KEY", "QWEN_API_KEY", "DASHSCOPE_API_KEY"),
        base_url_names=("STATS_PLANNER_LLM_BASE_URL", "STATS_LLM_BASE_URL", "DECISION_LLM_BASE_URL", "QWEN_BASE_URL", "DASHSCOPE_BASE_URL"),
        model_names=("STATS_PLANNER_LLM_MODEL", "STATS_LLM_MODEL", "DECISION_LLM_MODEL", "QWEN_MODEL", "DASHSCOPE_MODEL"),
    )
    return _extract_json(content)


def _rule_plan(query: str, cases: list[ClinicCase], candidates: list[CandidateTerm]) -> dict[str, Any]:
    months = _months_for_query(query, cases)
    target_text = _target_text(query)
    q = query or ""
    target_ids = _target_ids_from_query(q, candidates)

    if _is_visit_count(q):
        task_type = "visit_count"
        target_ids = []
    elif _is_all_overview(q, target_text):
        task_type = "all_overview"
        target_ids = []
    elif _is_disease_distribution(q):
        task_type = "disease_distribution"
        target_ids = []
    elif len(target_ids) >= 2 or re.search(r"(对比|比较|分别|和|与|及|以及)", q):
        task_type = "multi_disease_compare" if target_ids else "unmatched"
    elif target_ids:
        task_type = "trend" if re.search(r"(趋势|变化|每月|月度|按月)", q) else "single_disease"
    elif re.search(r"(操作|治疗方式|手术方式|处理方式|处置方式)", q):
        task_type = "operation_distribution"
    else:
        task_type = "unmatched" if target_text else "all_overview"

    return _validate_plan(
        query,
        cases,
        candidates,
        {"task_type": task_type, "target_ids": target_ids, "months": months, "metrics": [], "reason": "规则兜底规划"},
        source="rules",
    )


def _validate_plan(
    query: str,
    cases: list[ClinicCase],
    candidates: list[CandidateTerm],
    raw_plan: dict[str, Any],
    *,
    source: str,
) -> dict[str, Any]:
    task_type = str(raw_plan.get("task_type") or "unmatched")
    if task_type not in TASK_TYPES:
        task_type = "unmatched"

    available = set(_available_months(cases))
    locally_requested_months = _months_for_query(query, cases)
    months = [str(item) for item in raw_plan.get("months", []) if str(item) in available]
    if not months:
        months = locally_requested_months

    candidate_map = {item.id: item for item in candidates}
    target_ids = []
    for item in raw_plan.get("target_ids", []):
        item = str(item)
        if item in candidate_map and item not in target_ids:
            target_ids.append(item)

    if task_type in {"single_disease", "trend"} and len(target_ids) > 1:
        target_ids = target_ids[:1]
    if task_type == "multi_disease_compare" and len(target_ids) < 2:
        task_type = "single_disease" if target_ids else "unmatched"
    if task_type in {"single_disease", "trend"} and not target_ids:
        task_type = "unmatched"
    if task_type in {"visit_count", "all_overview", "disease_distribution"}:
        target_ids = []

    targets = [_candidate_payload(candidate_map[item]) for item in target_ids]
    return {
        "task_type": task_type,
        "query": query,
        "months": months,
        "targets": targets,
        "metrics": raw_plan.get("metrics") or _default_metrics(task_type),
        "reason": str(raw_plan.get("reason") or ""),
        "source": source,
    }


def execute_stats_plan(plan: dict[str, Any], cases: list[ClinicCase]) -> dict[str, Any]:
    task_type = plan["task_type"]
    months = tuple(plan.get("months") or ())
    denominator = _filter_months(cases, months)
    if task_type == "unmatched":
        return _execute_unmatched(plan, cases)
    if task_type in {"visit_count", "all_overview", "operation_distribution"}:
        metric = "overview" if task_type != "operation_distribution" else "treatment_distribution"
        intent = _intent_all(plan["query"], months, metric=metric, source=plan["source"])
        stats = build_stats(intent, cases)
        return _structured_from_primary(plan, stats, cases, intent)
    if task_type == "disease_distribution":
        return _execute_disease_distribution(plan, cases, denominator)
    if task_type in {"single_disease", "trend"}:
        target = plan["targets"][0]
        intent = _intent_for_target(plan["query"], months, target, source=plan["source"], metric="monthly_trend" if task_type == "trend" else "overview")
        stats = build_stats(intent, cases)
        return _structured_from_primary(plan, stats, cases, intent)
    if task_type == "multi_disease_compare":
        return _execute_multi_compare(plan, cases, denominator)
    return _execute_unmatched(plan, cases)


def build_stats_report(query: str, plan: dict[str, Any], structured: dict[str, Any]) -> tuple[str, str, str | None]:
    if is_llm_configured("STATS_REPORT_LLM_API_KEY", "STATS_LLM_API_KEY", "DECISION_LLM_API_KEY", "QWEN_API_KEY", "DASHSCOPE_API_KEY"):
        try:
            prompt = """
你是口腔门诊统计报告助手。请只根据输入的结构化统计数据生成中文 Markdown 报告。

硬性规则：
1. 只能使用输入中的结构化统计数据，不得编造病例数、占比、月份、诊断名、操作名或趋势结论。
2. 不输出 HTML、JSON、代码块、LaTeX、脚注，也不要解释你如何生成报告。
3. Markdown 必须规范：标题用 `##` / `###`；加粗只用于字段名或关键结论，格式必须是 `**字段名**：数值`；表格必须是标准 Markdown 表格，表头、分隔行、数据行都要完整，表格行之间不要插入空行。
4. 每张表最多 8 行；名称过长时保留原名，不要自行改写医学术语。
5. 不要把统计口径写成很长的项目符号堆叠，优先用短句和紧凑表格。

推荐结构：
## 统计结论
用 2-3 句话说明统计对象、时间范围、命中数量、占比和最重要发现。

### 统计口径
- **时间范围**：...
- **统计对象**：...
- **命中记录数**：... 例
- **同时间范围总记录数**：... 例
- **占比**：...

### 月度趋势
如果有月度数据，输出标准 Markdown 表格：`| 月份 | 例数 | 占比 |`。

### 主要分布
如果有操作、治疗、诊断或疾病类别分布，输出标准 Markdown 表格。

### 简要分析
- 单类疾病：说明纳入诊断、数量、占比、月度变化和主要操作/处理。
- 多类对比：突出各类数量、占比、排序和差异。
- 全部概览/就诊人数：只输出总量、时间范围、月度趋势和高频诊断/操作，不展开每个疾病的治疗细节。
- 未匹配：说明本地统计表无相关记录，不代表临床不存在该疾病。
""".strip()
            payload = {
                "query": query,
                "plan": plan,
                "structured_stats": _compact_structured(structured),
            }
            report = chat_completion(
                [{"role": "system", "content": prompt}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                temperature=0.1,
                max_tokens=1800,
                api_key_names=("STATS_REPORT_LLM_API_KEY", "STATS_LLM_API_KEY", "DECISION_LLM_API_KEY", "QWEN_API_KEY", "DASHSCOPE_API_KEY"),
                base_url_names=("STATS_REPORT_LLM_BASE_URL", "STATS_LLM_BASE_URL", "DECISION_LLM_BASE_URL", "QWEN_BASE_URL", "DASHSCOPE_BASE_URL"),
                model_names=("STATS_REPORT_LLM_MODEL", "STATS_LLM_MODEL", "DECISION_LLM_MODEL", "QWEN_MODEL", "DASHSCOPE_MODEL"),
            )
            return report, "llm", None
        except (LLMConfigurationError, LLMRequestError, RuntimeError, ValueError, TypeError) as exc:
            return _fallback_report(plan, structured), "template", str(exc)
    return _fallback_report(plan, structured), "template", None


def _execute_unmatched(plan: dict[str, Any], cases: list[ClinicCase]) -> dict[str, Any]:
    months = tuple(plan.get("months") or ())
    denominator = _filter_months(cases, months)
    scope = _scope_label(months)
    target = _target_text(plan.get("query") or "")
    diagnosis_rows = _top_rows(Counter(case.diagnosis_name for case in denominator if case.diagnosis_name), len(denominator), 8)
    operation_rows = _top_rows(Counter(case.operation_name for case in denominator if case.operation_name), len(denominator), 8)
    summary = (
        f"本地门诊病例统计数据中未找到与“{target or plan.get('query')}”可靠匹配的诊断、操作、科室或挂号类别字段，"
        f"时间范围为 {scope}。这表示当前统计表无可计算记录，不代表临床上不存在该疾病。"
    )
    tables = [
        _table("当前时间范围内高频诊断示例", ["诊断", "例数", "占比"], diagnosis_rows),
        _table("当前时间范围内高频操作示例", ["操作", "例数", "占比"], operation_rows),
    ]
    return {
        "summary": summary,
        "tables": tables,
        "charts": [],
        "matched_count": 0,
        "denominator_count": len(denominator),
        "structured_stats": {"task_type": "unmatched", "scope": scope, "target": target, "top_diagnoses": diagnosis_rows, "top_operations": operation_rows},
        "target_resolution": {"matched": False, "user_term": target, "reason": "本地候选字段中无可靠匹配。"},
    }


def _execute_disease_distribution(plan: dict[str, Any], cases: list[ClinicCase], denominator: list[ClinicCase]) -> dict[str, Any]:
    total = len(denominator)
    rows = []
    assigned: set[str] = set()
    for group_name, values in DISEASE_GROUPS.items():
        active = [value for value in values if any(case.diagnosis_name == value for case in denominator)]
        if not active:
            continue
        count = sum(1 for case in denominator if case.diagnosis_name in active)
        if count:
            rows.append([group_name, count, _pct(count, total), "、".join(active)])
            assigned.update(active)
    known_grouped_count = sum(row[1] for row in rows)
    other = total - known_grouped_count
    rows.sort(key=lambda item: item[1], reverse=True)
    if other > 0:
        rows.append(["其他未归类诊断", other, _pct(other, total), "未纳入当前疾病大类表的诊断"])
    scope = _scope_label(tuple(plan.get("months") or ()))
    summary = f"{scope} 范围内共 {total} 条门诊记录，已按疾病大类统计主要病种分布。"
    tables = [_table("疾病大类分布", ["疾病大类", "例数", "占比", "纳入诊断"], rows[:20])]
    charts = [_chart("bar", "疾病大类分布", rows[:12])]
    return {
        "summary": summary,
        "tables": tables,
        "charts": charts,
        "matched_count": total,
        "denominator_count": total,
        "structured_stats": {"task_type": "disease_distribution", "scope": scope, "rows": rows},
        "target_resolution": {"matched": True, "match_type": "disease_distribution"},
    }


def _execute_multi_compare(plan: dict[str, Any], cases: list[ClinicCase], denominator: list[ClinicCase]) -> dict[str, Any]:
    total = len(denominator)
    target_stats = []
    tables = []
    charts = []
    for target in plan["targets"]:
        intent = _intent_for_target(plan["query"], tuple(plan.get("months") or ()), target, source=plan["source"], metric="overview")
        stats = build_stats(intent, cases)
        target_stats.append(
            {
                "name": target["canonical_term"],
                "count": stats["matched_count"],
                "ratio": _pct(stats["matched_count"], total),
                "diagnosis_names": target.get("diagnosis_names", []),
                "summary": stats["summary"],
            }
        )
    rows = [[item["name"], item["count"], item["ratio"], "、".join(item["diagnosis_names"])] for item in target_stats]
    rows.sort(key=lambda item: item[1], reverse=True)
    scope = _scope_label(tuple(plan.get("months") or ()))
    summary = f"{scope} 范围内完成 {len(rows)} 个目标的门诊记录对比。"
    tables.append(_table("多类疾病对比", ["目标", "例数", "占比", "纳入诊断"], rows))
    charts.append(_chart("bar", "多类疾病对比", rows))
    return {
        "summary": summary,
        "tables": tables,
        "charts": charts,
        "matched_count": sum(row[1] for row in rows),
        "denominator_count": total,
        "structured_stats": {"task_type": "multi_disease_compare", "scope": scope, "targets": target_stats},
        "target_resolution": {"matched": True, "match_type": "multi_disease_compare", "targets": plan["targets"]},
    }


def _structured_from_primary(plan: dict[str, Any], stats: dict[str, Any], cases: list[ClinicCase], intent: StatsIntent) -> dict[str, Any]:
    structured = dict(stats)
    structured["structured_stats"] = {
        "task_type": plan["task_type"],
        "scope": _scope_label(tuple(plan.get("months") or ())),
        "total_records": len(cases),
        "matched_count": stats.get("matched_count", 0),
        "denominator_count": stats.get("denominator_count", 0),
        "target": {
            "keyword": intent.keyword,
            "disease_group": intent.disease_group,
            "diagnosis_names": list(intent.matched_values),
        },
        "tables": stats.get("tables", []),
    }
    return structured


def _intent_all(query: str, months: tuple[str, ...], *, metric: str, source: str) -> StatsIntent:
    return StatsIntent(
        query=query,
        keyword="",
        filter_mode="all",
        year=None,
        month=months[0] if len(months) == 1 else None,
        months=months,
        metric=metric,
        visualizations=METRIC_VISUALS.get(metric, METRIC_VISUALS["overview"]),
        source=source,
        match_type="all",
    )


def _intent_for_target(query: str, months: tuple[str, ...], target: dict[str, Any], *, source: str, metric: str) -> StatsIntent:
    values = tuple(target.get("diagnosis_names") or target.get("values") or [target["canonical_term"]])
    return StatsIntent(
        query=query,
        keyword=target["canonical_term"],
        filter_mode=target.get("filter_mode") or "diagnosis",
        year=None,
        month=months[0] if len(months) == 1 else None,
        months=months,
        metric=metric,
        visualizations=METRIC_VISUALS.get(metric, METRIC_VISUALS["overview"]),
        source=source,
        target_matched=True,
        target_user_term=_target_text(query),
        target_field=target.get("field") or "diagnosis_name",
        target_confidence=float(target.get("score") or 1.0),
        target_reason=target.get("reason") or "",
        match_type=target.get("candidate_type") or "field_value",
        disease_group=target["canonical_term"] if target.get("candidate_type") == "disease_group" else "",
        matched_values=values,
    )


def _candidate_payload(item: CandidateTerm) -> dict[str, Any]:
    return {
        "id": item.id,
        "canonical_term": item.term,
        "candidate_type": item.candidate_type,
        "field": item.field,
        "filter_mode": item.filter_mode,
        "diagnosis_names": list(item.values),
        "local_count": item.count,
        "score": item.score,
    }


def _target_ids_from_query(query: str, candidates: list[CandidateTerm]) -> list[str]:
    ids: list[str] = []
    normalized = query or ""
    for alias, group_name in GROUP_ALIASES.items():
        if alias in normalized:
            for candidate in candidates:
                if candidate.candidate_type == "disease_group" and candidate.term == group_name and candidate.id not in ids:
                    ids.append(candidate.id)
    if not ids and candidates:
        ids.append(candidates[0].id)
    return ids


def _months_for_query(query: str, cases: list[ClinicCase]) -> list[str]:
    year, month, months = _extract_time(query, cases)
    if months:
        return list(months)
    if month:
        return [month]
    if year:
        return [f"{year}-{index:02d}" for index in range(1, 13)]
    return []


def _filter_months(cases: list[ClinicCase], months: tuple[str, ...]) -> list[ClinicCase]:
    if not months:
        return list(cases)
    allowed = set(months)
    return [case for case in cases if case.visit_month in allowed]


def _is_visit_count(query: str) -> bool:
    return bool(re.search(r"(就诊人数|就诊人次|人数统计|就诊量|门诊量|门诊总量|总量|总数|多少人|多少例|记录数)", query))


def _is_all_overview(query: str, target_text: str) -> bool:
    generic = target_text in {"", "口腔科", "口腔门诊", "口腔颌面外科", "口腔颌面外科门诊", "本科室", "本门诊"}
    return generic and bool(re.search(r"(情况|概况|整体|总览|分析|统计|门诊)", query))


def _is_disease_distribution(query: str) -> bool:
    return bool(re.search(r"(病种分布|疾病分布|诊断分布|各类疾病|主要疾病|高频诊断|病种构成)", query))


def _diagnosis_count(cases: list[ClinicCase], diagnosis: str) -> int:
    return sum(1 for case in cases if case.diagnosis_name == diagnosis)


def _scope_label(months: tuple[str, ...]) -> str:
    if not months:
        return "全部时间"
    if len(months) == 1:
        return months[0]
    return f"{months[0]} 至 {months[-1]}"


def _pct(count: int, total: int) -> str:
    if total <= 0:
        return "0.0%"
    return f"{count / total * 100:.1f}%"


def _table(title: str, columns: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    return {"title": title, "columns": columns, "rows": rows}


def _chart(chart_type: str, title: str, rows: list[list[Any]]) -> dict[str, Any]:
    return {"type": chart_type, "title": title, "labels": [str(row[0]) for row in rows], "values": [int(row[1]) for row in rows]}


def _top_rows(counter: Counter[str], total: int, limit: int) -> list[list[Any]]:
    return [[name or "未填写", count, _pct(count, total)] for name, count in counter.most_common(limit)]


def _default_metrics(task_type: str) -> list[str]:
    if task_type in {"visit_count", "all_overview"}:
        return ["count", "monthly_trend", "disease_distribution", "operation_distribution"]
    if task_type == "multi_disease_compare":
        return ["count", "ratio", "comparison"]
    if task_type == "disease_distribution":
        return ["count", "ratio", "disease_distribution"]
    return ["count", "ratio", "monthly_trend", "operation_distribution"]


def _fallback_report(plan: dict[str, Any], structured: dict[str, Any]) -> str:
    lines = ["## 统计结论", structured.get("summary", "已完成统计。")]
    stats = structured.get("structured_stats") or {}
    if plan.get("task_type") == "multi_disease_compare":
        lines.append("\n## 对比结果")
        for item in stats.get("targets", []):
            lines.append(f"- {item['name']}：{item['count']} 例，占比 {item['ratio']}")
    elif plan.get("task_type") == "disease_distribution":
        lines.append("\n## 疾病大类分布")
        for row in (stats.get("rows") or [])[:10]:
            lines.append(f"- {row[0]}：{row[1]} 例，占比 {row[2]}")
    else:
        for table in structured.get("tables", [])[:4]:
            if table.get("title") == "统计口径":
                continue
            rows = table.get("rows") or []
            if not rows:
                continue
            lines.append(f"\n## {table.get('title')}")
            for row in rows[:8]:
                suffix = f"，占比 {row[2]}" if len(row) > 2 else ""
                lines.append(f"- {row[0]}：{row[1]} 例{suffix}")
    return "\n".join(lines)


def _compact_structured(structured: dict[str, Any]) -> dict[str, Any]:
    compact = {
        "summary": structured.get("summary"),
        "matched_count": structured.get("matched_count"),
        "denominator_count": structured.get("denominator_count"),
        "structured_stats": structured.get("structured_stats"),
        "target_resolution": structured.get("target_resolution"),
    }
    tables = []
    for table in structured.get("tables", [])[:5]:
        tables.append({**table, "rows": (table.get("rows") or [])[:12]})
    compact["tables"] = tables
    return compact
