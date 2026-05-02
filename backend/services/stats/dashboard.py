from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from typing import Any

from .aggregations import _clean, _pct, treatment_category
from .loader import ClinicCase, PARSED_JSONL, load_cases
from .query_parser import DISEASE_GROUPS


def stats_options() -> dict[str, Any]:
    cases = load_cases()
    months = sorted({case.visit_month for case in cases if case.visit_month})
    diagnosis_counter = Counter(_clean(case.diagnosis_name, "") for case in cases)
    operation_counter = Counter(_clean(case.operation_name, "") for case in cases)
    department_counter = Counter(_clean(case.department, "") for case in cases)
    register_counter = Counter(_clean(case.register_type, "") for case in cases)
    group_rows = []
    for group_name, values in DISEASE_GROUPS.items():
        count = sum(1 for case in cases if case.diagnosis_name in values)
        if count:
            group_rows.append({"value": group_name, "label": group_name, "count": count})
    group_rows.sort(key=lambda item: item["count"], reverse=True)
    return {
        "ok": True,
        "module": "clinic_stats_dashboard",
        "case_source": str(PARSED_JSONL),
        "total_records": len(cases),
        "months": [{"value": month, "label": month, "count": sum(1 for case in cases if case.visit_month == month)} for month in months],
        "disease_groups": group_rows,
        "diagnosis_names": _option_rows(diagnosis_counter),
        "operation_names": _option_rows(operation_counter),
        "departments": _option_rows(department_counter),
        "register_types": _option_rows(register_counter),
    }


def run_stats_filter(filters: dict[str, Any] | None = None) -> dict[str, Any]:
    cases = load_cases()
    filters = _normalize_filters(filters or {})
    matched = _filter_cases(cases, filters)
    denominator = _filter_months(cases, filters["months"])
    scope = _scope_label(filters["months"])
    active_filters = _active_filters(filters)
    total = len(matched)
    denom_total = len(denominator)
    return {
        "ok": True,
        "module": "clinic_stats_dashboard",
        "case_source": str(PARSED_JSONL),
        "scope_label": scope,
        "total_records": len(cases),
        "matched_count": total,
        "denominator_count": denom_total,
        "ratio": _pct(total, denom_total),
        "active_filters": active_filters,
        "is_filtered": bool(active_filters),
        "charts": _build_charts(matched, denominator, filters),
        "tables": _build_tables(matched, denominator),
        "sample_cases": [asdict(case) for case in matched[:8]],
        "empty": total == 0,
    }


def _normalize_filters(filters: dict[str, Any]) -> dict[str, list[str]]:
    keys = ("months", "disease_groups", "diagnosis_names", "operation_names", "departments", "register_types")
    normalized: dict[str, list[str]] = {}
    for key in keys:
        value = filters.get(key) or []
        if isinstance(value, str):
            value = [value]
        normalized[key] = [str(item).strip() for item in value if str(item).strip()]
    return normalized


def _filter_months(cases: list[ClinicCase], months: list[str]) -> list[ClinicCase]:
    if not months:
        return list(cases)
    allowed = set(months)
    return [case for case in cases if case.visit_month in allowed]


def _filter_cases(cases: list[ClinicCase], filters: dict[str, list[str]]) -> list[ClinicCase]:
    scoped = _filter_months(cases, filters["months"])
    group_diagnoses: set[str] = set()
    for group_name in filters["disease_groups"]:
        group_diagnoses.update(DISEASE_GROUPS.get(group_name, ()))
    explicit_diagnoses = set(filters["diagnosis_names"])
    operation_names = set(filters["operation_names"])
    departments = set(filters["departments"])
    register_types = set(filters["register_types"])
    matched: list[ClinicCase] = []
    for case in scoped:
        if group_diagnoses and case.diagnosis_name not in group_diagnoses:
            continue
        if explicit_diagnoses and case.diagnosis_name not in explicit_diagnoses:
            continue
        if operation_names and case.operation_name not in operation_names:
            continue
        if departments and case.department not in departments:
            continue
        if register_types and case.register_type not in register_types:
            continue
        matched.append(case)
    return matched


def _active_filters(filters: dict[str, list[str]]) -> dict[str, list[str]]:
    return {key: values for key, values in filters.items() if values}


def _scope_label(months: list[str]) -> str:
    if not months:
        return "全部时间"
    ordered = sorted(months)
    if len(ordered) == 1:
        return ordered[0]
    return f"{ordered[0]} 至 {ordered[-1]}"


def _option_rows(counter: Counter[str], limit: int = 160) -> list[dict[str, Any]]:
    rows = []
    for value, count in counter.most_common(limit):
        if value:
            rows.append({"value": value, "label": value, "count": count})
    return rows


def _top_rows(counter: Counter[str], total: int, limit: int = 12) -> list[list[Any]]:
    return [[label or "未填写", count, _pct(count, total)] for label, count in counter.most_common(limit)]


def _table(title: str, columns: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    return {"title": title, "columns": columns, "rows": rows}


def _chart(chart_type: str, title: str, rows: list[list[Any]], **extra: Any) -> dict[str, Any]:
    chart = {
        "type": chart_type,
        "title": title,
        "labels": [str(row[0]) for row in rows],
        "values": [int(row[1]) for row in rows],
    }
    chart.update(extra)
    return chart


def _monthly_details(cases: list[ClinicCase], months: list[str]) -> dict[str, Any]:
    details: dict[str, Any] = {}
    for month in sorted(months):
        month_cases = [case for case in cases if case.visit_month == month]
        total = len(month_cases)
        details[month] = {
            "month": month,
            "count": total,
            "top_diagnoses": _top_rows(Counter(_clean(case.diagnosis_name) for case in month_cases), total, 5),
            "top_operations": _top_rows(Counter(_clean(case.operation_name) for case in month_cases), total, 5),
        }
    return details


def _monthly_rows(cases: list[ClinicCase], months: list[str]) -> list[list[Any]]:
    available = months or sorted({case.visit_month for case in cases if case.visit_month})
    counter = Counter(case.visit_month for case in cases if case.visit_month)
    total = sum(counter.values())
    return [[month, counter.get(month, 0), _pct(counter.get(month, 0), total)] for month in sorted(available)]


def _disease_group_rows(cases: list[ClinicCase], limit: int = 14) -> list[list[Any]]:
    total = len(cases)
    rows = []
    assigned_count = 0
    for group_name, values in DISEASE_GROUPS.items():
        count = sum(1 for case in cases if case.diagnosis_name in values)
        if count:
            rows.append([group_name, count, _pct(count, total)])
            assigned_count += count
    rows.sort(key=lambda row: row[1], reverse=True)
    other = total - assigned_count
    if other > 0:
        rows.append(["其他未归类诊断", other, _pct(other, total)])
    return rows[:limit]


def _heatmap(cases: list[ClinicCase], months: list[str]) -> dict[str, Any]:
    x_labels = sorted(months or {case.visit_month for case in cases if case.visit_month})
    y_labels = [row[0] for row in _disease_group_rows(cases, limit=8)]
    values = []
    max_value = 0
    for y_index, group_name in enumerate(y_labels):
        group_values = set(DISEASE_GROUPS.get(group_name, ()))
        for x_index, month in enumerate(x_labels):
            if group_name == "其他未归类诊断":
                known = {value for values in DISEASE_GROUPS.values() for value in values}
                count = sum(1 for case in cases if case.visit_month == month and case.diagnosis_name not in known)
            else:
                count = sum(1 for case in cases if case.visit_month == month and case.diagnosis_name in group_values)
            max_value = max(max_value, count)
            values.append({"x": x_index, "y": y_index, "value": count})
    return {
        "type": "heatmap",
        "title": "月份 × 疾病大类热力矩阵",
        "x_labels": x_labels,
        "y_labels": y_labels,
        "values": values,
        "max": max_value,
    }


def _diagnosis_spectrum(cases: list[ClinicCase]) -> dict[str, Any]:
    rows = _top_rows(Counter(_clean(case.diagnosis_name) for case in cases), len(cases), 22)
    return _chart("diagnosis_spectrum", "诊断频谱", rows, value_unit="例")


def _operation_monthly_stack(cases: list[ClinicCase], months: list[str], limit: int = 5) -> tuple[dict[str, Any], dict[str, Any]]:
    x_labels = sorted(months or {case.visit_month for case in cases if case.visit_month})
    top_operations = [label for label, _ in Counter(_clean(case.operation_name) for case in cases).most_common(limit)]
    series = []
    for operation in top_operations:
        values = [
            sum(1 for case in cases if case.visit_month == month and _clean(case.operation_name) == operation)
            for month in x_labels
        ]
        series.append({"name": operation, "values": values})
    rows = [
        [month, *[item["values"][index] for item in series]]
        for index, month in enumerate(x_labels)
    ]
    chart = {
        "type": "stacked_bar",
        "title": "Top操作月度构成",
        "labels": x_labels,
        "series": series,
        "values": [sum(item["values"][index] for item in series) for index, _ in enumerate(x_labels)],
        "value_unit": "例",
    }
    table = _table("Top操作月度构成", ["月份", *top_operations], rows)
    return chart, table


def _build_charts(matched: list[ClinicCase], denominator: list[ClinicCase], filters: dict[str, list[str]]) -> list[dict[str, Any]]:
    months = filters["months"] or sorted({case.visit_month for case in denominator if case.visit_month})
    trend_rows = _monthly_rows(matched, months)
    disease_rows = _disease_group_rows(matched)
    diagnosis_rows = _top_rows(Counter(_clean(case.diagnosis_name) for case in matched), len(matched), 12)
    operation_rows = _top_rows(Counter(_clean(case.operation_name) for case in matched), len(matched), 12)
    treatment_rows = _top_rows(Counter(treatment_category(case) for case in matched), len(matched), 12)
    register_rows = _top_rows(Counter(_clean(case.register_type) for case in matched), len(matched), 8)
    operation_monthly_chart, _ = _operation_monthly_stack(matched, months)
    return [
        operation_monthly_chart,
        _chart("pulse_line", "门诊流量时间脉冲", trend_rows, value_unit="例", monthly_details=_monthly_details(matched, months)),
        _chart("orbit_bubbles", "疾病大类星图", disease_rows[:10], value_unit="例"),
        _heatmap(matched, months),
        _chart("scan_rank", "操作方式 Top 排名", operation_rows[:10], value_unit="例"),
        _chart("donut", "挂号类别构成", register_rows, value_unit="例"),
        _diagnosis_spectrum(matched),
        _chart("bar", "高频诊断分布", diagnosis_rows[:10], value_unit="例"),
        _chart("scan_rank", "处理/治疗计划语义归并", treatment_rows[:10], value_unit="例"),
    ]


def _build_tables(matched: list[ClinicCase], denominator: list[ClinicCase]) -> list[dict[str, Any]]:
    total = len(matched)
    months = sorted({case.visit_month for case in denominator if case.visit_month})
    _, operation_monthly_table = _operation_monthly_stack(matched, months)
    return [
        operation_monthly_table,
        _table("门诊流量时间脉冲", ["月份", "例数", "占比"], _monthly_rows(matched, months)),
        _table("疾病大类星图", ["疾病大类", "例数", "占比"], _disease_group_rows(matched, limit=20)),
        _table("操作方式 Top 排名", ["操作名称", "例数", "占比"], _top_rows(Counter(_clean(case.operation_name) for case in matched), total, 20)),
        _table("挂号类别构成", ["挂号类别", "例数", "占比"], _top_rows(Counter(_clean(case.register_type) for case in matched), total, 12)),
        _table("高频诊断分布", ["诊断名称", "例数", "占比"], _top_rows(Counter(_clean(case.diagnosis_name) for case in matched), total, 20)),
        _table("处理/治疗计划语义归并", ["类别", "例数", "占比"], _top_rows(Counter(treatment_category(case) for case in matched), total, 20)),
    ]
