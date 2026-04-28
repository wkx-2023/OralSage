from __future__ import annotations

from typing import Any

from .aggregations import build_stats
from .loader import PARSED_JSONL, build_parsed_cases, load_cases
from .query_parser import parse_intent


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
    intent = parse_intent(query, cases)
    stats = build_stats(intent, cases)
    method_note = (
        "自然语言需求先解析为结构化统计意图，LLM 只负责理解筛选词、时间范围和图表偏好；"
        "所有病例筛选、计数、占比和图表数据均由本地结构化门诊病例表计算。"
    )
    if intent.llm_error:
        method_note += f" 当前 LLM 解析不可用，已使用规则兜底：{intent.llm_error}"

    return {
        "ok": True,
        "module": "clinic_stats",
        "query": query,
        "intent": {
            "keyword": intent.keyword,
            "filter_mode": intent.filter_mode,
            "year": intent.year,
            "month": intent.month,
            "months": list(intent.months),
            "metric": intent.metric,
            "visualizations": list(intent.visualizations),
            "source": intent.source,
        },
        "case_source": str(PARSED_JSONL),
        "total_records": len(cases),
        "summary": stats["summary"],
        "tables": stats["tables"],
        "charts": stats["charts"],
        "matched_count": stats["matched_count"],
        "denominator_count": stats["denominator_count"],
        "sample_cases": stats.get("sample_cases", [])[: max(0, min(top_k, 20))],
        "method_note": method_note,
        "supported_fields": SUPPORTED_FIELDS,
    }


def rebuild_stats_cache() -> dict[str, Any]:
    cases = build_parsed_cases(force=True)
    return {"ok": True, "module": "clinic_stats", "case_source": str(PARSED_JSONL), "total_records": len(cases)}
