from __future__ import annotations

from typing import Any

from .loader import PARSED_JSONL, build_parsed_cases, load_cases
from .workflow import run_stats_workflow


SUPPORTED_FIELDS = [
    "就诊时间",
    "挂号类别",
    "就诊科室",
    "病历诊断名称1",
    "病历诊断编码1",
    "病历操作名称1",
    "病历操作编码1",
    "主诉",
    "现病史",
    "既往史",
    "处理",
    "治疗计划",
    "检查",
    "辅查",
]


def run_stats_query(query: str, top_k: int = 10) -> dict[str, Any]:
    query = (query or "").strip()
    if not query:
        return {
            "ok": False,
            "module": "clinic_stats",
            "message": "query is required",
            "supported_fields": SUPPORTED_FIELDS,
        }

    cases = load_cases()
    workflow = run_stats_workflow(query, cases)
    plan = workflow.get("plan") or {}
    method_note = (
        "门诊统计采用 Planner -> 本地统计执行 -> Reporter 的工作流：LLM 只负责理解用户问题、选择本地候选字段并组织报告；"
        "所有病例筛选、计数、占比、趋势和图表数据均由本地结构化门诊病例表计算。"
    )
    if plan.get("planner_error"):
        method_note += f" 当前 LLM 规划不可用，已使用规则兜底：{plan['planner_error']}"

    return {
        "ok": True,
        "module": "clinic_stats",
        "query": query,
        "intent": _legacy_intent_payload(query, workflow),
        "plan": plan,
        "case_source": str(PARSED_JSONL),
        "total_records": len(cases),
        "summary": workflow["summary"],
        "tables": workflow["tables"],
        "charts": workflow["charts"],
        "matched_count": workflow["matched_count"],
        "denominator_count": workflow["denominator_count"],
        "sample_cases": workflow.get("sample_cases", [])[: max(0, min(top_k, 20))],
        "target_resolution": workflow.get("target_resolution", {}),
        "structured_stats": workflow.get("structured_stats", {}),
        "report": workflow.get("report", workflow["summary"]),
        "report_source": workflow.get("report_source", "template"),
        "errors": workflow.get("errors", []),
        "method_note": method_note,
        "supported_fields": SUPPORTED_FIELDS,
    }


def _legacy_intent_payload(query: str, workflow: dict[str, Any]) -> dict[str, Any]:
    plan = workflow.get("plan") or {}
    target_resolution = workflow.get("target_resolution") if isinstance(workflow.get("target_resolution"), dict) else {}
    targets = plan.get("targets") if isinstance(plan.get("targets"), list) else []
    target = targets[0] if targets else {}
    months = list(plan.get("months") or [])
    task_type = str(plan.get("task_type") or "")
    matched = target_resolution.get("matched")
    if matched is None:
        matched = task_type not in {"unmatched"}
    return {
        "keyword": target.get("canonical_term") or target_resolution.get("canonical_term") or "",
        "filter_mode": target.get("filter_mode") or ("all" if task_type in {"visit_count", "all_overview", "disease_distribution"} else "diagnosis"),
        "year": months[0][:4] if months else None,
        "month": months[0] if len(months) == 1 else None,
        "months": months,
        "metric": _metric_from_task(task_type),
        "visualizations": _visualizations_from_task(task_type),
        "source": plan.get("source") or "",
        "target_matched": bool(matched),
        "target_user_term": target_resolution.get("user_term") or "",
        "target_field": target.get("field") or target_resolution.get("field") or "",
        "target_confidence": target.get("score") or target_resolution.get("confidence") or 0.0,
        "target_reason": target.get("reason") or target_resolution.get("reason") or "",
        "match_type": target.get("candidate_type") or target_resolution.get("match_type") or ("all" if task_type in {"visit_count", "all_overview"} else ""),
        "disease_group": _disease_group_from_target(target, target_resolution),
        "matched_values": target.get("diagnosis_names") or target_resolution.get("diagnosis_names") or [],
    }


def _disease_group_from_target(target: dict[str, Any], target_resolution: dict[str, Any]) -> str:
    if target.get("candidate_type") == "disease_group":
        return str(target.get("canonical_term") or "")
    return str(target_resolution.get("disease_group") or "")


def _metric_from_task(task_type: str) -> str:
    if task_type == "trend":
        return "monthly_trend"
    if task_type == "disease_distribution":
        return "diagnosis_distribution"
    if task_type == "operation_distribution":
        return "treatment_distribution"
    return "overview" if task_type in {"all_overview", "single_disease", "multi_disease_compare"} else "count"


def _visualizations_from_task(task_type: str) -> list[str]:
    if task_type == "trend":
        return ["monthly_trend"]
    if task_type == "disease_distribution":
        return ["diagnosis_distribution"]
    if task_type == "operation_distribution":
        return ["treatment_distribution"]
    return ["kpi", "monthly_trend", "operation_distribution", "treatment_distribution", "register_type_distribution"]


def rebuild_stats_cache() -> dict[str, Any]:
    cases = build_parsed_cases(force=True)
    return {"ok": True, "module": "clinic_stats", "case_source": str(PARSED_JSONL), "total_records": len(cases)}
