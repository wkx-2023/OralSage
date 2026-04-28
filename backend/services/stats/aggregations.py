from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict
import re
from typing import Any

from .loader import ClinicCase
from .query_parser import StatsIntent


EMPTY_VALUES = {"", "无", "暂无", "未提及", "无特殊", "nan", "None", "???", "null"}


def _clean(value: str, fallback: str = "未填写") -> str:
    value = (value or "").strip()
    return fallback if value in EMPTY_VALUES else value


def _valid(value: str) -> bool:
    return _clean(value, "") != ""


def _pct(count: int, total: int) -> str:
    if total <= 0:
        return "0.0%"
    return f"{count / total * 100:.1f}%"


def _table(title: str, columns: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    return {"title": title, "columns": columns, "rows": rows}


def _chart(chart_type: str, title: str, rows: list[list[Any]], **extra: Any) -> dict[str, Any]:
    labels = [str(row[0]) for row in rows]
    values = [int(row[1]) for row in rows]
    chart = {"type": chart_type, "title": title, "labels": labels, "values": values}
    chart.update(extra)
    return chart


def _top_counter(counter: Counter[str], total: int, limit: int = 12) -> list[list[Any]]:
    rows = []
    for label, count in counter.most_common(limit):
        rows.append([_clean(label), count, _pct(count, total)])
    return rows


def _scope_label(intent: StatsIntent) -> str:
    if intent.months:
        if len(intent.months) == 1:
            return intent.months[0]
        return f"{intent.months[0]} 至 {intent.months[-1]}"
    if intent.month:
        return intent.month
    if intent.year:
        return f"{intent.year}年"
    return "全部时间"


def _in_time(case: ClinicCase, intent: StatsIntent) -> bool:
    if intent.months:
        return case.visit_month in set(intent.months)
    if intent.month:
        return case.visit_month == intent.month
    if intent.year:
        return case.visit_date.startswith(intent.year)
    return True


def _matches(case: ClinicCase, intent: StatsIntent) -> bool:
    if not _in_time(case, intent):
        return False
    if not intent.keyword:
        return True

    keyword = intent.keyword
    if intent.filter_mode == "suture_removal":
        return (
            "拆除缝线" in (case.diagnosis_name or "")
            or "缝线去除" in (case.operation_name or "")
            or "拆线" in case.searchable_text()
        )
    if intent.filter_mode == "diagnosis":
        return keyword in (case.diagnosis_name or "")
    if intent.filter_mode == "operation":
        return keyword in (case.operation_name or "")
    if intent.filter_mode == "department":
        return keyword in (case.department or "")
    if intent.filter_mode == "register_type":
        return keyword in (case.register_type or "")
    return keyword in case.searchable_text()


def filter_cases(cases: list[ClinicCase], intent: StatsIntent) -> tuple[list[ClinicCase], list[ClinicCase]]:
    denominator = [case for case in cases if _in_time(case, intent)]
    matched = [case for case in denominator if _matches(case, intent)]
    return matched, denominator


def treatment_category(case: ClinicCase) -> str:
    text = f"{case.operation_name}\n{case.treatment}\n{case.treatment_plan}"
    parts: list[str] = []
    if any(key in text for key in ("根管", "牙髓")):
        parts.append("根管/牙髓治疗")
    if any(key in text for key in ("拔除", "拔牙", "拔除术")):
        parts.append("拔除/拔牙")
    if any(key in text for key in ("翻瓣", "去骨", "分冠", "分根")):
        parts.append("翻瓣去骨/分牙")
    if any(key in text for key in ("抗炎", "消炎", "阿莫西林", "甲硝唑", "药")):
        parts.append("药物抗炎")
    if any(key in text for key in ("缝合", "拆线", "缝线")):
        parts.append("缝合/拆线")
    if any(key in text for key in ("修复", "义齿", "取模", "印模", "戴牙")):
        parts.append("修复治疗")
    if any(key in text for key in ("种植", "基台", "二期", "一期")):
        parts.append("种植相关处理")
    if any(key in text for key in ("复查", "随诊", "预约", "会诊")):
        parts.append("复查/随诊")
    if any(key in text for key in ("检查", "拍片", "CBCT", "X线", "CT")):
        parts.append("检查评估")
    if parts:
        return " + ".join(dict.fromkeys(parts[:3]))
    return _clean(case.operation_name or case.treatment_plan, "未归类处理")


def exam_category(case: ClinicCase) -> str:
    text = f"{case.exam}\n{case.aux_exam}"
    if not _valid(text):
        return "未记录检查/辅查"
    if any(key.lower() in text.lower() for key in ("cbct", "ct")):
        return "CBCT/CT影像"
    if any(key in text for key in ("X线", "x线", "根尖片", "片示", "低密度影")):
        return "X线/根尖片"
    if any(key in text for key in ("水平", "近中", "远中", "垂直", "埋伏", "阻生")):
        return "阻生方向/埋伏评估"
    if any(key in text for key in ("叩", "探", "松动", "红肿", "瘘管")):
        return "口内专科检查"
    if any(key in text for key in ("根尖", "龋", "折裂", "残根", "残冠")):
        return "牙体/根尖病变评估"
    return "其他检查记录"


def _monthly_rows(cases: list[ClinicCase]) -> list[list[Any]]:
    counter = Counter(case.visit_month or "未知月份" for case in cases)
    total = sum(counter.values())
    return [[month, counter[month], _pct(counter[month], total)] for month in sorted(counter)]


def _operation_by_month_chart(cases: list[ClinicCase], limit: int = 5) -> dict[str, Any] | None:
    months = sorted({case.visit_month for case in cases if case.visit_month})
    if not months:
        return None
    top_ops = [label for label, _ in Counter(_clean(case.operation_name) for case in cases).most_common(limit)]
    series = []
    for op in top_ops:
        series.append(
            {
                "name": op,
                "values": [
                    sum(1 for case in cases if case.visit_month == month and _clean(case.operation_name) == op)
                    for month in months
                ],
            }
        )
    return {
        "type": "stacked_bar",
        "title": "Top操作月度构成",
        "labels": months,
        "series": series,
    }


def _field_completeness_chart(cases: list[ClinicCase]) -> dict[str, Any]:
    fields = [
        ("主诉", "chief_complaint"),
        ("现病史", "present_illness"),
        ("既往史", "past_history"),
        ("处理", "treatment"),
        ("治疗计划", "treatment_plan"),
        ("检查", "exam"),
        ("辅查", "aux_exam"),
    ]
    rows = []
    total = len(cases)
    for label, attr in fields:
        count = sum(1 for case in cases if _valid(getattr(case, attr)))
        rows.append([label, count, _pct(count, total)])
    return _chart("bar", "病例字段完整度", rows, value_unit="例")


def _kpi_chart(intent: StatsIntent, total: int, denom_total: int, matched: list[ClinicCase]) -> dict[str, Any]:
    ratio = f"{(total / denom_total * 100):.1f}%" if denom_total else "0.0%"
    active_months = len({case.visit_month for case in matched if case.visit_month})
    operation_types = len({_clean(case.operation_name) for case in matched if _clean(case.operation_name)})
    metrics = [
        {"label": "命中病例", "value": total, "sub": "例"},
        {"label": "同时间总量", "value": denom_total, "sub": "例"},
        {"label": "命中占比", "value": ratio, "sub": _scope_label(intent)},
        {"label": "操作类型", "value": operation_types, "sub": f"覆盖 {active_months or 0} 个月"},
    ]
    return {"type": "kpi", "title": "统计摘要", "metrics": metrics}


def _add_table_once(tables: list[dict[str, Any]], title: str, columns: list[str], rows: list[list[Any]]) -> None:
    if not any(table["title"] == title for table in tables):
        tables.append(_table(title, columns, rows))


def build_stats(intent: StatsIntent, cases: list[ClinicCase]) -> dict[str, Any]:
    matched, denominator = filter_cases(cases, intent)
    total = len(matched)
    denom_total = len(denominator)
    keyword_label = intent.keyword or "全部门诊记录"
    scope = _scope_label(intent)

    tables: list[dict[str, Any]] = [
        _table(
            "统计口径",
            ["指标", "数值"],
            [
                ["统计对象", keyword_label],
                ["筛选方式", intent.filter_mode],
                ["统计时间范围", scope],
                ["命中病例数", total],
                ["同时间范围总记录数", denom_total],
                ["占比", _pct(total, denom_total)],
                ["意图解析来源", intent.source],
            ],
        )
    ]
    charts: list[dict[str, Any]] = []

    if total == 0:
        return {
            "summary": f"当前未检索到与“{keyword_label}”匹配的门诊记录，时间范围为 {scope}。",
            "tables": tables,
            "charts": charts,
            "matched_count": total,
            "denominator_count": denom_total,
        }

    op_rows = _top_counter(Counter(_clean(case.operation_name) for case in matched), total)
    month_rows = _monthly_rows(matched)
    tx_rows = _top_counter(Counter(treatment_category(case) for case in matched), total)
    exam_rows = _top_counter(Counter(exam_category(case) for case in matched), total)
    register_rows = _top_counter(Counter(_clean(case.register_type) for case in matched), total)

    for visual in intent.visualizations:
        if visual == "kpi":
            charts.append(_kpi_chart(intent, total, denom_total, matched))
        elif visual == "monthly_trend":
            _add_table_once(tables, "月度就诊趋势", ["月份", "例数", "占比"], month_rows)
            charts.append(_chart("line", "月度就诊趋势", month_rows, value_unit="例"))
        elif visual == "operation_distribution":
            _add_table_once(tables, "操作名称分布", ["类别", "例数", "占比"], op_rows)
            charts.append(_chart("rank_line", "操作名称分布", op_rows[:10], value_unit="例"))
        elif visual == "treatment_distribution":
            _add_table_once(tables, "处理/治疗计划语义归并", ["类别", "例数", "占比"], tx_rows)
            charts.append(_chart("bar", "处理/治疗计划语义归并", tx_rows[:10], value_unit="例"))
        elif visual == "exam_aux_distribution":
            _add_table_once(tables, "检查/辅查类型分布", ["类别", "例数", "占比"], exam_rows)
            charts.append(_chart("bar", "检查/辅查类型分布", exam_rows[:10], value_unit="例"))
        elif visual == "register_type_distribution":
            _add_table_once(tables, "挂号类别构成", ["类别", "例数", "占比"], register_rows)
            charts.append(_chart("donut", "挂号类别构成", register_rows, value_unit="例"))
        elif visual == "operation_by_month":
            chart = _operation_by_month_chart(matched)
            if chart:
                charts.append(chart)
        elif visual == "field_completeness":
            charts.append(_field_completeness_chart(matched))
        elif visual == "diagnosis_distribution" and intent.metric == "diagnosis_distribution":
            rows = _top_counter(Counter(_clean(case.diagnosis_name) for case in matched), total)
            _add_table_once(tables, "诊断分布", ["类别", "例数", "占比"], rows)
            charts.append(_chart("bar", "诊断分布", rows[:10], value_unit="例"))

    if not any(table["title"] == "操作名称分布" for table in tables):
        _add_table_once(tables, "操作名称分布", ["类别", "例数", "占比"], op_rows)
    if not charts:
        charts.append(_chart("rank_line", "操作名称分布", op_rows[:10], value_unit="例"))

    summary = (
        f"基于本地门诊病例表，{scope} 范围内共命中 {total} 例“{keyword_label}”相关记录，"
        f"同时间范围总记录数为 {denom_total} 例，占比 {_pct(total, denom_total)}。"
    )
    return {
        "summary": summary,
        "tables": tables,
        "charts": charts,
        "matched_count": total,
        "denominator_count": denom_total,
        "sample_cases": [asdict(case) for case in matched[:5]],
    }
