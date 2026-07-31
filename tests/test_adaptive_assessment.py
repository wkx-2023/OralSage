from __future__ import annotations

import tempfile
from pathlib import Path

from backend.services.learning.adaptive_assessment import (
    annotate_question,
    build_adaptive_submission_report,
    calibrate_submission_items,
    target_difficulty_for_mastery,
)
from backend.services.learning.memory import LearningMemory


def test_annotate_question_maps_mastery_to_bloom_and_difficulty() -> None:
    low = annotate_question(
        {"id": "q-low", "kg_node_ids": ["kg:weak"]},
        {"mastery_by_kg_node": {"kg:weak": {"score": 0.25}}},
        surface="stage_exam",
    )
    high = annotate_question(
        {"id": "q-high", "kg_node_ids": ["kg:strong"]},
        {"mastery_by_kg_node": {"kg:strong": {"score": 0.82}}},
        surface="stage_exam",
    )

    assert low["bloom_level"] in {"remember", "understand"}
    assert high["bloom_level"] in {"evaluate", "create"}
    assert low["target_difficulty"] == target_difficulty_for_mastery(0.25)
    assert high["target_difficulty"] > low["target_difficulty"]
    assert low["adaptive_assessment"]["item_key"].startswith("stage_exam:")


def test_calibrate_submission_items_persists_irt_item_statistics() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        memory = LearningMemory(Path(tmp) / "learning.sqlite3")
        try:
            question = annotate_question(
                {
                    "id": "q-bank-1",
                    "source_question_id": "bank-1",
                    "kg_node_ids": ["kg:perio"],
                },
                {"kg:perio": {"mastery_score": 0.55}},
                surface="comprehensive_exam",
            )
            results = [
                {"question_id": "q-bank-1", "score": 5, "max_score": 5, "is_correct": True}
            ]

            stats = calibrate_submission_items(
                memory,
                user_id="student-irt",
                surface="comprehensive_exam",
                questions=[question],
                results=results,
            )
            report = build_adaptive_submission_report(
                results=results,
                question_by_id={"q-bank-1": question},
                item_stats=stats,
            )

            assert len(stats) == 1
            assert stats[0]["item_key"] == question["adaptive_assessment"]["item_key"]
            assert stats[0]["exposure_count"] == 1
            assert stats[0]["correct_count"] == 1
            assert 0.0 <= stats[0]["correct_rate"] <= 1.0
            assert "difficulty_b" in stats[0]
            assert "discrimination_a" in stats[0]
            assert report["learner_theta"] > 0
            assert report["calibrated_items"][0]["sample_count"] == 1
        finally:
            memory._conn.close()
