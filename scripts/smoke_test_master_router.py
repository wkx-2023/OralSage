from __future__ import annotations

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.services.router.agent import _rule_based_plan, _validate_plan


CASES = [
    (
        "\u5e2e\u6211\u628a\u8fd9\u6bb5\u533b\u60a3\u5bf9\u8bdd\u6574\u7406\u6210\u95e8\u8bca\u75c5\u5386",
        "oral_dentist_report_assistant",
    ),
    (
        "2025\u5e74\u963b\u751f\u7259\u6cbb\u7597\u65b9\u5f0f\u5206\u5e03\u7edf\u8ba1",
        "clinic_case_stats_assistant",
    ),
    (
        "2025\u5e74\u9f8b\u9f7f\u548c\u7259\u5468\u708e\u5bf9\u6bd4",
        "clinic_case_stats_assistant",
    ),
    (
        "\u8fd1\u534a\u5e74\u963b\u751f\u7259\u7814\u7a76\u8fdb\u5c55\u548c PubMed \u6587\u732e",
        "oral_frontier_learning_assistant",
    ),
    (
        "\u60a3\u8005\u7537\uff0c35\u5c81\uff0c\u53f3\u4e0b\u667a\u9f7f\u53cd\u590d\u80bf\u75db\uff0c\u5e94\u8be5\u600e\u4e48\u6cbb",
        "oral_cases_assistant",
    ),
    (
        "\u4ec0\u4e48\u662f\u6839\u5c16\u5468\u708e\u7684\u75c5\u7406\u673a\u5236",
        "oral_pathology_assistant",
    ),
]


def main() -> None:
    for message, expected_agent in CASES:
        plan = _rule_based_plan(message, image_base64=None)
        plan, errors = _validate_plan(plan, message, None)
        actual_agent = plan.get("target_agent")
        assert actual_agent == expected_agent, (message, actual_agent, expected_agent, errors)
    image_plan = _rule_based_plan("\u8bf7\u5206\u6790\u8fd9\u5f20\u7259\u7247", image_base64="aGVsbG8=")
    assert image_plan.get("target_agent") == "oral_disease_image_recognition_assistant"
    print("master router smoke tests passed")


if __name__ == "__main__":
    main()
