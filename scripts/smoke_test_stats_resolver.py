from __future__ import annotations

from pathlib import Path
import os
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

for key in (
    "QWEN_API_KEY",
    "DASHSCOPE_API_KEY",
    "DECISION_LLM_API_KEY",
    "STATS_LLM_API_KEY",
    "STATS_PLANNER_LLM_API_KEY",
    "STATS_REPORT_LLM_API_KEY",
):
    os.environ[key] = ""

from backend.services.stats import run_stats_query
from backend.services.stats.dashboard import run_stats_filter


def main() -> None:
    visit_count = run_stats_query("2025年就诊人数统计")
    assert visit_count["plan"]["task_type"] == "visit_count"
    assert visit_count["matched_count"] == 6653
    assert visit_count["matched_count"] == visit_count["denominator_count"]
    assert visit_count["report"]

    overview = run_stats_query("2025年口腔科门诊情况")
    assert overview["plan"]["task_type"] == "all_overview"
    assert overview["matched_count"] == 6653

    wisdom_to_impacted = run_stats_query("2025年智齿治疗方式分布")
    assert wisdom_to_impacted["intent"]["disease_group"] == "阻生牙/埋伏牙类"
    assert wisdom_to_impacted["intent"]["target_matched"] is True
    assert wisdom_to_impacted["matched_count"] > 0

    caries = run_stats_query("2025年龋齿门诊情况")
    assert caries["plan"]["task_type"] == "single_disease"
    assert caries["intent"]["disease_group"] == "龋病类"
    assert set(caries["intent"]["matched_values"]) >= {"龋病", "乳牙深龋", "恒牙深龋"}
    assert caries["matched_count"] == 31

    comparison = run_stats_query("2025年龋齿和牙周炎对比")
    assert comparison["plan"]["task_type"] == "multi_disease_compare"
    assert len(comparison["plan"]["targets"]) == 2
    assert comparison["matched_count"] == 31 + 237

    distribution = run_stats_query("2025年各类疾病分布")
    assert distribution["plan"]["task_type"] == "disease_distribution"
    assert distribution["matched_count"] == 6653

    unknown = run_stats_query("2025年口腔癌门诊情况")
    assert unknown["plan"]["task_type"] == "unmatched"
    assert unknown["intent"]["target_matched"] is False
    assert unknown["matched_count"] == 0

    total = run_stats_query("2025年门诊总量")
    assert total["intent"]["filter_mode"] == "all"
    assert total["matched_count"] == total["denominator_count"]

    out_of_range = run_stats_query("2026年阻生牙门诊情况")
    assert out_of_range["intent"]["disease_group"] == "阻生牙/埋伏牙类"
    assert out_of_range["denominator_count"] == 0

    dashboard_group = run_stats_filter({"disease_groups": ["阻生牙/埋伏牙类"]})
    assert dashboard_group["matched_count"] == 2241

    dashboard_intersection = run_stats_filter({
        "disease_groups": ["阻生牙/埋伏牙类"],
        "diagnosis_names": ["阻生牙"],
    })
    assert dashboard_intersection["matched_count"] == 2223

    dashboard_empty_intersection = run_stats_filter({
        "disease_groups": ["阻生牙/埋伏牙类"],
        "diagnosis_names": ["龋病"],
    })
    assert dashboard_empty_intersection["matched_count"] == 0
    print("stats resolver smoke tests passed")


if __name__ == "__main__":
    main()
