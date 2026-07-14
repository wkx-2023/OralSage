"""Tests for Direct QA persistence adapter.

Covers:
- Confusion signal → profile + mastery updated
- Multiple KG nodes
- Normal expansion question → no updates
- Empty / invalid KG IDs
- should_reinforce strict-type guard
- User isolation
- Repeated updates accumulate
- Default parameters
"""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from backend.services.learning.memory import LearningMemory
from backend.services.learning.schemas import DEFAULT_PROFILE
from backend.services.qa.direct_qa_persistence import (
    _filter_valid_kg_ids,
    update_profile_from_qa,
)


# ---------------------------------------------------------------------------
# Unit tests for the internal helper
# ---------------------------------------------------------------------------


class FilterValidKgIdsTests(unittest.TestCase):
    """Verify _filter_valid_kg_ids deduplication and filtering."""

    def test_normal_list(self) -> None:
        self.assertEqual(
            _filter_valid_kg_ids(["kg-a", "kg-b", "kg-c"]),
            ["kg-a", "kg-b", "kg-c"],
        )

    def test_deduplicates(self) -> None:
        self.assertEqual(
            _filter_valid_kg_ids(["kg-a", "kg-b", "kg-a"]),
            ["kg-a", "kg-b"],
        )

    def test_strips_whitespace(self) -> None:
        self.assertEqual(
            _filter_valid_kg_ids(["  kg-a  ", "kg-b"]),
            ["kg-a", "kg-b"],
        )

    def test_removes_empty_and_whitespace_only(self) -> None:
        self.assertEqual(
            _filter_valid_kg_ids(["kg-a", "", "   ", None, "kg-b"]),
            ["kg-a", "kg-b"],
        )

    def test_empty_list(self) -> None:
        self.assertEqual(_filter_valid_kg_ids([]), [])

    def test_not_a_list(self) -> None:
        self.assertEqual(_filter_valid_kg_ids(None), [])
        self.assertEqual(_filter_valid_kg_ids("not-a-list"), [])
        self.assertEqual(_filter_valid_kg_ids({}), [])

    def test_preserves_order(self) -> None:
        ids = [f"kg-{i}" for i in range(10)]
        self.assertEqual(_filter_valid_kg_ids(ids), ids)

    def test_all_invalid(self) -> None:
        self.assertEqual(
            _filter_valid_kg_ids(["", "   ", None, False, 0]),
            [],
        )


# ---------------------------------------------------------------------------
# Integration tests for the adapter function
# ---------------------------------------------------------------------------


def _fresh_profile() -> dict:
    return copy.deepcopy(DEFAULT_PROFILE)


def _make_qa_result(
    *,
    should_reinforce: bool = False,
    confusion_kg_node_ids: list | None = None,
    question: str = "",
    resolved_question: str = "",
    used_evidence: list | None = None,
    **extra,
) -> dict:
    result: dict = {
        "should_reinforce": should_reinforce,
        "confusion_kg_node_ids": (
            confusion_kg_node_ids if confusion_kg_node_ids is not None else []
        ),
        "question": question,
        "resolved_question": resolved_question,
        "used_evidence": used_evidence or [],
    }
    result.update(extra)
    return result


class ConfusionSignalUpdatesProfileAndMasteryTests(unittest.TestCase):
    """should_reinforce=True + valid KG IDs → profile + mastery updated."""

    def test_single_kg_node_updates_both_profile_and_mastery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                user_id = "user-a"
                profile = memory.get_or_create_profile(user_id)

                result = update_profile_from_qa(
                    memory=memory,
                    user_id=user_id,
                    profile=profile,
                    qa_result=_make_qa_result(
                        should_reinforce=True,
                        confusion_kg_node_ids=["kg-pulp-periapical"],
                        question="牙髓炎与根尖周炎如何鉴别？",
                        resolved_question="牙髓炎与根尖周炎如何鉴别？",
                    ),
                    source_ref="dqa-001",
                )

                self.assertTrue(result["profile_updated"])
                self.assertEqual(
                    result["confusion_kg_node_ids_processed"],
                    ["kg-pulp-periapical"],
                )

                # Profile: known_weak_points should include the KG id
                updated_profile = result["profile"]
                self.assertIn(
                    "kg-pulp-periapical",
                    updated_profile.get("known_weak_points", []),
                )

                # Mastery: one record written
                self.assertEqual(len(result["mastery_records"]), 1)
                rec = result["mastery_records"][0]
                self.assertEqual(rec["user_id"], user_id)
                self.assertEqual(rec["kg_node_id"], "kg-pulp-periapical")
                self.assertEqual(rec["source"], "direct_qa")
                self.assertEqual(rec["source_ref"], "dqa-001")
                # New record: 0.3 + (-0.03) = 0.27
                self.assertAlmostEqual(rec["mastery_score"], 0.27)
                self.assertEqual(
                    rec["evidence"].get("event"),
                    "direct_qa_confusion_signal",
                )

                # Verify persisted in DB
                dqa_recs = memory.get_direct_qa_mastery_by_user(user_id)
                self.assertEqual(len(dqa_recs), 1)
                self.assertEqual(dqa_recs[0]["kg_node_id"], "kg-pulp-periapical")

                # Profile persisted
                reloaded = memory.get_profile(user_id)
                self.assertIsNotNone(reloaded)
                self.assertIn(
                    "kg-pulp-periapical",
                    reloaded.get("known_weak_points", []),  # type: ignore[union-attr]
                )
            finally:
                memory._conn.close()

    def test_multiple_kg_nodes_all_processed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                user_id = "user-b"
                profile = memory.get_or_create_profile(user_id)

                result = update_profile_from_qa(
                    memory=memory,
                    user_id=user_id,
                    profile=profile,
                    qa_result=_make_qa_result(
                        should_reinforce=True,
                        confusion_kg_node_ids=[
                            "kg-caries-etiology",
                            "kg-caries-class",
                            "kg-caries-treatment",
                        ],
                        question="龋病的病因、分类和治疗分别是什么？",
                    ),
                )

                self.assertTrue(result["profile_updated"])
                self.assertEqual(len(result["mastery_records"]), 3)

                # All three KG ids should be in weak_points
                updated_profile = result["profile"]
                for kg_id in [
                    "kg-caries-etiology",
                    "kg-caries-class",
                    "kg-caries-treatment",
                ]:
                    self.assertIn(kg_id, updated_profile["known_weak_points"])

                # Three distinct mastery records
                kg_ids_written = {r["kg_node_id"] for r in result["mastery_records"]}
                self.assertEqual(len(kg_ids_written), 3)

                # DB verification
                dqa_recs = memory.get_direct_qa_mastery_by_user(user_id)
                self.assertEqual(len(dqa_recs), 3)
            finally:
                memory._conn.close()

    def test_duplicate_kg_ids_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                user_id = "user-c"
                profile = memory.get_or_create_profile(user_id)

                result = update_profile_from_qa(
                    memory=memory,
                    user_id=user_id,
                    profile=profile,
                    qa_result=_make_qa_result(
                        should_reinforce=True,
                        confusion_kg_node_ids=[
                            "kg-perio-stage",
                            "kg-perio-stage",
                            "  kg-perio-stage  ",
                            "kg-perio-stage",
                        ],
                        question="牙周炎如何分期？",
                    ),
                )

                self.assertTrue(result["profile_updated"])
                # Only one unique KG node → one mastery record
                self.assertEqual(len(result["mastery_records"]), 1)
                self.assertEqual(
                    result["confusion_kg_node_ids_processed"],
                    ["kg-perio-stage"],
                )
            finally:
                memory._conn.close()


class NormalQuestionNoUpdateTests(unittest.TestCase):
    """should_reinforce=False → no profile or mastery changes."""

    def test_should_reinforce_false_no_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                user_id = "user-d"
                profile = memory.get_or_create_profile(user_id)
                original_weak = list(profile.get("known_weak_points", []))

                result = update_profile_from_qa(
                    memory=memory,
                    user_id=user_id,
                    profile=profile,
                    qa_result=_make_qa_result(
                        should_reinforce=False,
                        confusion_kg_node_ids=["kg-oral-mucosa"],
                        question="口腔黏膜有哪几类疾病？",
                    ),
                )

                self.assertFalse(result["profile_updated"])
                self.assertEqual(result["mastery_records"], [])
                self.assertEqual(
                    result["confusion_kg_node_ids_processed"], [],
                )

                # Profile must be unchanged
                self.assertEqual(
                    result["profile"].get("known_weak_points", []),
                    original_weak,
                )

                # No mastery records in DB
                dqa_recs = memory.get_direct_qa_mastery_by_user(user_id)
                self.assertEqual(len(dqa_recs), 0)
            finally:
                memory._conn.close()

    def test_should_reinforce_false_even_with_valid_kg_ids(self) -> None:
        """Valid KG ids must not trigger writes when should_reinforce is False."""
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                user_id = "user-e"
                profile = memory.get_or_create_profile(user_id)

                result = update_profile_from_qa(
                    memory=memory,
                    user_id=user_id,
                    profile=profile,
                    qa_result=_make_qa_result(
                        should_reinforce=False,
                        confusion_kg_node_ids=["kg-full-crown", "kg-inlay"],
                        question="全冠和嵌体有什么区别？",
                    ),
                )

                self.assertFalse(result["profile_updated"])
                self.assertEqual(result["mastery_records"], [])

                dqa_recs = memory.get_direct_qa_mastery_by_user(user_id)
                self.assertEqual(len(dqa_recs), 0)
            finally:
                memory._conn.close()


class EmptyOrInvalidKgIdsNoUpdateTests(unittest.TestCase):
    """No valid KG ids → no write, even when should_reinforce=True."""

    def test_empty_confusion_list_no_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                user_id = "user-f"
                profile = memory.get_or_create_profile(user_id)

                result = update_profile_from_qa(
                    memory=memory,
                    user_id=user_id,
                    profile=profile,
                    qa_result=_make_qa_result(
                        should_reinforce=True,
                        confusion_kg_node_ids=[],
                        question="什么是牙周袋？",
                    ),
                )

                self.assertFalse(result["profile_updated"])
                self.assertEqual(result["mastery_records"], [])
                self.assertIn("no valid", result["trace"])
            finally:
                memory._conn.close()

    def test_all_invalid_kg_ids_no_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                user_id = "user-g"
                profile = memory.get_or_create_profile(user_id)

                result = update_profile_from_qa(
                    memory=memory,
                    user_id=user_id,
                    profile=profile,
                    qa_result=_make_qa_result(
                        should_reinforce=True,
                        confusion_kg_node_ids=["", "   ", None],
                        question="根管治疗有哪些步骤？",
                    ),
                )

                self.assertFalse(result["profile_updated"])
                self.assertEqual(result["mastery_records"], [])
            finally:
                memory._conn.close()

    def test_missing_confusion_key_no_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                user_id = "user-h"
                profile = memory.get_or_create_profile(user_id)

                result = update_profile_from_qa(
                    memory=memory,
                    user_id=user_id,
                    profile=profile,
                    qa_result={
                        "should_reinforce": True,
                        "question": "拔牙后多久可以刷牙？",
                        # no confusion_kg_node_ids key
                    },
                )

                self.assertFalse(result["profile_updated"])
                self.assertEqual(result["mastery_records"], [])
            finally:
                memory._conn.close()


class ShouldReinforceStrictTypeGuardTests(unittest.TestCase):
    """Only the Python boolean True triggers writes — truthy values do not."""

    def test_truthy_int_does_not_trigger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                user_id = "user-i"
                profile = memory.get_or_create_profile(user_id)

                result = update_profile_from_qa(
                    memory=memory,
                    user_id=user_id,
                    profile=profile,
                    qa_result={
                        "should_reinforce": 1,  # truthy, not True
                        "confusion_kg_node_ids": ["kg-dry-socket"],
                        "question": "干槽症如何处理？",
                    },
                )

                self.assertFalse(result["profile_updated"])
                self.assertEqual(result["mastery_records"], [])
                self.assertIn("not strictly True", result["trace"])
            finally:
                memory._conn.close()

    def test_truthy_string_does_not_trigger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                user_id = "user-j"
                profile = memory.get_or_create_profile(user_id)

                result = update_profile_from_qa(
                    memory=memory,
                    user_id=user_id,
                    profile=profile,
                    qa_result={
                        "should_reinforce": "true",  # truthy string, not True
                        "confusion_kg_node_ids": ["kg-impacted-tooth"],
                        "question": "阻生牙一定要拔吗？",
                    },
                )

                self.assertFalse(result["profile_updated"])
                self.assertEqual(result["mastery_records"], [])
            finally:
                memory._conn.close()

    def test_false_does_not_trigger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                user_id = "user-k"
                profile = memory.get_or_create_profile(user_id)

                result = update_profile_from_qa(
                    memory=memory,
                    user_id=user_id,
                    profile=profile,
                    qa_result={
                        "should_reinforce": False,
                        "confusion_kg_node_ids": ["kg-ortho-biomech"],
                        "question": "正畸力对牙周组织有什么影响？",
                    },
                )

                self.assertFalse(result["profile_updated"])
            finally:
                memory._conn.close()


class UserIsolationTests(unittest.TestCase):
    """Direct QA persistence must isolate users completely."""

    def test_two_users_independent_updates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                profile_a = memory.get_or_create_profile("user-a")
                profile_b = memory.get_or_create_profile("user-b")

                # User A: confusion on KG node X
                result_a = update_profile_from_qa(
                    memory=memory,
                    user_id="user-a",
                    profile=profile_a,
                    qa_result=_make_qa_result(
                        should_reinforce=True,
                        confusion_kg_node_ids=["kg-pulp-revasc"],
                        question="牙髓血运重建的适应证？",
                    ),
                )
                self.assertTrue(result_a["profile_updated"])
                self.assertAlmostEqual(
                    result_a["mastery_records"][0]["mastery_score"], 0.27,
                )

                # User B: confusion on a different KG node
                result_b = update_profile_from_qa(
                    memory=memory,
                    user_id="user-b",
                    profile=profile_b,
                    qa_result=_make_qa_result(
                        should_reinforce=True,
                        confusion_kg_node_ids=["kg-micro-endo"],
                        question="显微根管治疗的优点？",
                    ),
                )
                self.assertTrue(result_b["profile_updated"])

                # User A should only see their own records
                a_recs = memory.get_direct_qa_mastery_by_user("user-a")
                self.assertEqual(len(a_recs), 1)
                self.assertEqual(a_recs[0]["kg_node_id"], "kg-pulp-revasc")

                b_recs = memory.get_direct_qa_mastery_by_user("user-b")
                self.assertEqual(len(b_recs), 1)
                self.assertEqual(b_recs[0]["kg_node_id"], "kg-micro-endo")

                # Profiles are isolated
                reloaded_a = memory.get_profile("user-a")
                reloaded_b = memory.get_profile("user-b")
                self.assertIn("kg-pulp-revasc", reloaded_a["known_weak_points"])  # type: ignore[index]
                self.assertIn("kg-micro-endo", reloaded_b["known_weak_points"])  # type: ignore[index]
                self.assertNotIn("kg-micro-endo", reloaded_a["known_weak_points"])  # type: ignore[index]
            finally:
                memory._conn.close()


class RepeatedUpdatesAccumulateTests(unittest.TestCase):
    """Repeated confusion on the same KG node accumulates mastery penalties."""

    def test_three_confusion_events_on_same_kg_node(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                user_id = "user-l"
                profile = memory.get_or_create_profile(user_id)

                # Three rounds of confusion on the same KG node
                for i in range(3):
                    result = update_profile_from_qa(
                        memory=memory,
                        user_id=user_id,
                        profile=profile,
                        qa_result=_make_qa_result(
                            should_reinforce=True,
                            confusion_kg_node_ids=["kg-perio-surgery"],
                            question=f"牙周手术适应症 (round {i})？",
                        ),
                    )
                    self.assertTrue(result["profile_updated"])
                    profile = result["profile"]  # feed back for next round

                # Only one row in DB (upsert)
                dqa_recs = memory.get_direct_qa_mastery_by_user(
                    user_id, kg_node_id="kg-perio-surgery"
                )
                self.assertEqual(len(dqa_recs), 1)
                # 0.3 + 3*(-0.03) = 0.21
                self.assertAlmostEqual(dqa_recs[0]["mastery_score"], 0.21)
            finally:
                memory._conn.close()


class DefaultParametersTests(unittest.TestCase):
    """source_ref and outline_node_id defaults behave correctly."""

    def test_default_source_ref_is_empty_string(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                user_id = "user-m"
                profile = memory.get_or_create_profile(user_id)

                result = update_profile_from_qa(
                    memory=memory,
                    user_id=user_id,
                    profile=profile,
                    qa_result=_make_qa_result(
                        should_reinforce=True,
                        confusion_kg_node_ids=["kg-oral-mucosa"],
                        question="口腔白斑是否一定会癌变？",
                    ),
                )

                self.assertTrue(result["profile_updated"])
                rec = result["mastery_records"][0]
                self.assertEqual(rec["source_ref"], "")
            finally:
                memory._conn.close()

    def test_default_outline_node_id_is_empty_string(self) -> None:
        """outline_node_id="" should not add section: prefix."""
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                user_id = "user-n"
                profile = memory.get_or_create_profile(user_id)

                result = update_profile_from_qa(
                    memory=memory,
                    user_id=user_id,
                    profile=profile,
                    qa_result=_make_qa_result(
                        should_reinforce=True,
                        confusion_kg_node_ids=["kg-caries-etiology"],
                        question="龋病的病因是什么？",
                    ),
                    # outline_node_id defaults to ""
                )

                updated = result["profile"]
                # No section: prefix when outline_node_id is empty
                section_entries = [
                    w for w in updated.get("known_weak_points", [])
                    if w.startswith("section:")
                ]
                self.assertEqual(len(section_entries), 0)
            finally:
                memory._conn.close()


class ProfileFieldsIntegrityTests(unittest.TestCase):
    """Verify the returned profile has all required fields."""

    def test_updated_profile_contains_all_default_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                user_id = "user-o"
                profile = memory.get_or_create_profile(user_id)

                result = update_profile_from_qa(
                    memory=memory,
                    user_id=user_id,
                    profile=profile,
                    qa_result=_make_qa_result(
                        should_reinforce=True,
                        confusion_kg_node_ids=["kg-full-crown"],
                        question="全冠预备的基本步骤？",
                    ),
                )

                updated = result["profile"]
                for key in DEFAULT_PROFILE:
                    self.assertIn(key, updated, f"Missing key: {key}")
            finally:
                memory._conn.close()

    def test_mistake_patterns_include_qa_question(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                user_id = "user-p"
                profile = memory.get_or_create_profile(user_id)

                result = update_profile_from_qa(
                    memory=memory,
                    user_id=user_id,
                    profile=profile,
                    qa_result=_make_qa_result(
                        should_reinforce=True,
                        confusion_kg_node_ids=["kg-perio-stage"],
                        question="牙周炎的1999年分类与2018年分类有何不同？",
                    ),
                )

                updated = result["profile"]
                # mistake_patterns should include a QA疑问 entry
                qa_patterns = [
                    p for p in updated.get("mistake_patterns", [])
                    if p.startswith("QA疑问:")
                ]
                self.assertGreater(len(qa_patterns), 0)
            finally:
                memory._conn.close()


class NoPollutionOfCourseTablesTests(unittest.TestCase):
    """Direct QA writes never affect course mastery_records or lesson_events."""

    def test_direct_qa_does_not_write_to_mastery_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                user_id = "user-q"
                profile = memory.get_or_create_profile(user_id)

                update_profile_from_qa(
                    memory=memory,
                    user_id=user_id,
                    profile=profile,
                    qa_result=_make_qa_result(
                        should_reinforce=True,
                        confusion_kg_node_ids=["kg-pulp-diag"],
                        question="牙髓活力测试有哪些方法？",
                    ),
                )

                # Course mastery_records must be empty
                course_rows = memory._conn.execute(
                    "SELECT * FROM mastery_records WHERE user_id = ?",
                    (user_id,),
                ).fetchall()
                self.assertEqual(len(course_rows), 0)

                # lesson_events must be empty (no fake session created)
                evt_rows = memory._conn.execute(
                    "SELECT * FROM lesson_events WHERE user_id = ?",
                    (user_id,),
                ).fetchall()
                self.assertEqual(len(evt_rows), 0)
            finally:
                memory._conn.close()

    def test_direct_qa_does_not_create_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                user_id = "user-r"
                profile = memory.get_or_create_profile(user_id)

                update_profile_from_qa(
                    memory=memory,
                    user_id=user_id,
                    profile=profile,
                    qa_result=_make_qa_result(
                        should_reinforce=True,
                        confusion_kg_node_ids=["kg-root-canal"],
                        question="根管治疗的成功率是多少？",
                    ),
                )

                sessions = memory.list_sessions_for_user(user_id)
                self.assertEqual(len(sessions), 0)
            finally:
                memory._conn.close()


if __name__ == "__main__":
    unittest.main()
