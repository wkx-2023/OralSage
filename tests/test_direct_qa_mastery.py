"""Tests for Direct QA mastery persistence — isolated from course mastery_records."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.services.learning.memory import LearningMemory


class DirectQAMasteryFirstWriteTests(unittest.TestCase):
    """Verify that a first-time Direct QA mastery write creates a correct record."""

    def test_first_write_creates_record_with_source_direct_qa(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                evidence = {
                    "question": "牙髓炎与根尖周炎如何鉴别？",
                    "resolved_question": "牙髓炎与根尖周炎如何鉴别？",
                    "confusion_kg_node_ids": ["kg-pulp-periapical"],
                    "used_evidence": [{"chunk_id": "ch001", "text": "..."}],
                }
                rec = memory.upsert_direct_qa_mastery(
                    user_id="user-a",
                    concept_name="牙髓炎与根尖周炎鉴别",
                    mastery_delta=-0.03,
                    kg_node_id="kg-pulp-periapical",
                    evidence=evidence,
                    source_ref="dqa-event-001",
                )

                self.assertEqual(rec["user_id"], "user-a")
                self.assertEqual(rec["kg_node_id"], "kg-pulp-periapical")
                self.assertEqual(rec["concept_name"], "牙髓炎与根尖周炎鉴别")
                self.assertEqual(rec["source"], "direct_qa")
                self.assertEqual(rec["source_ref"], "dqa-event-001")
                # New record: base 0.3 + (-0.03) = 0.27
                self.assertAlmostEqual(rec["mastery_score"], 0.27)
                # New record: base 0.3 + 0.05 = 0.35
                self.assertAlmostEqual(rec["confidence"], 0.35)
                self.assertEqual(rec["evidence"], evidence)
                self.assertIsNotNone(rec["updated_at"])
            finally:
                memory._conn.close()

    def test_first_write_without_kg_node_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                rec = memory.upsert_direct_qa_mastery(
                    user_id="user-b",
                    concept_name="牙周袋深度测量",
                    mastery_delta=-0.03,
                )

                self.assertIsNone(rec["kg_node_id"])
                self.assertEqual(rec["source"], "direct_qa")
                self.assertAlmostEqual(rec["mastery_score"], 0.27)
            finally:
                memory._conn.close()


class DirectQAMasteryRepeatedUpdateTests(unittest.TestCase):
    """Verify that repeated updates modify the same record, not create duplicates."""

    def test_repeated_negative_update_clamps_and_does_not_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                # First write
                r1 = memory.upsert_direct_qa_mastery(
                    user_id="user-a",
                    concept_name="根管治疗步骤",
                    mastery_delta=-0.03,
                    kg_node_id="kg-root-canal",
                    evidence={"round": 1},
                )
                self.assertAlmostEqual(r1["mastery_score"], 0.27)

                # Second update on same user/KG/concept
                r2 = memory.upsert_direct_qa_mastery(
                    user_id="user-a",
                    concept_name="根管治疗步骤",
                    mastery_delta=-0.03,
                    kg_node_id="kg-root-canal",
                    evidence={"round": 2},
                )
                # Should update the same row (score: 0.27 - 0.03 = 0.24)
                self.assertEqual(r2["id"], r1["id"])
                self.assertAlmostEqual(r2["mastery_score"], 0.24)
                self.assertEqual(r2["evidence"], {"round": 2})

                # Verify only one row exists
                all_recs = memory.get_direct_qa_mastery_by_user(
                    "user-a", kg_node_id="kg-root-canal"
                )
                self.assertEqual(len(all_recs), 1)
            finally:
                memory._conn.close()

    def test_mastery_clamped_to_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                # Drive mastery below 0 with large negative delta
                rec = memory.upsert_direct_qa_mastery(
                    user_id="user-c",
                    concept_name="极度薄弱概念",
                    mastery_delta=-5.0,
                    kg_node_id="kg-weak",
                )
                # New: 0.3 - 5.0 = -4.7 → clamped to 0.0
                self.assertAlmostEqual(rec["mastery_score"], 0.0)
            finally:
                memory._conn.close()

    def test_mastery_clamped_to_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                rec = memory.upsert_direct_qa_mastery(
                    user_id="user-d",
                    concept_name="非常熟练概念",
                    mastery_delta=5.0,
                    kg_node_id="kg-strong",
                )
                # New: 0.3 + 5.0 = 5.3 → clamped to 1.0
                self.assertAlmostEqual(rec["mastery_score"], 1.0)
            finally:
                memory._conn.close()

    def test_confidence_clamped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                rec = memory.upsert_direct_qa_mastery(
                    user_id="user-e",
                    concept_name="测试置信度边界",
                    mastery_delta=0.0,
                    confidence_delta=10.0,
                )
                # New: 0.3 + 10.0 = 10.3 → clamped to 1.0
                self.assertAlmostEqual(rec["confidence"], 1.0)
            finally:
                memory._conn.close()


class DirectQAMasteryUserIsolationTests(unittest.TestCase):
    """Verify user A's Direct QA mastery never affects user B."""

    def test_users_are_fully_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                # User A: confusion on a concept
                memory.upsert_direct_qa_mastery(
                    user_id="user-a",
                    concept_name="龋病分类",
                    mastery_delta=-0.03,
                    kg_node_id="kg-caries-class",
                    evidence={"user": "a"},
                )

                # User B: same concept, higher mastery
                memory.upsert_direct_qa_mastery(
                    user_id="user-b",
                    concept_name="龋病分类",
                    mastery_delta=0.10,
                    kg_node_id="kg-caries-class",
                    evidence={"user": "b"},
                )

                a_recs = memory.get_direct_qa_mastery_by_user("user-a")
                b_recs = memory.get_direct_qa_mastery_by_user("user-b")

                self.assertEqual(len(a_recs), 1)
                self.assertEqual(len(b_recs), 1)

                a_rec = a_recs[0]
                b_rec = b_recs[0]

                self.assertNotEqual(a_rec["id"], b_rec["id"])
                self.assertEqual(a_rec["user_id"], "user-a")
                self.assertEqual(b_rec["user_id"], "user-b")
                self.assertAlmostEqual(a_rec["mastery_score"], 0.27)  # 0.3 - 0.03
                self.assertAlmostEqual(b_rec["mastery_score"], 0.40)  # 0.3 + 0.10
            finally:
                memory._conn.close()

    def test_user_a_does_not_see_user_b_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                memory.upsert_direct_qa_mastery(
                    user_id="user-a",
                    concept_name="牙周炎分期",
                    mastery_delta=-0.03,
                    kg_node_id="kg-perio-stage",
                )
                memory.upsert_direct_qa_mastery(
                    user_id="user-b",
                    concept_name="口腔正畸生物力学",
                    mastery_delta=-0.03,
                    kg_node_id="kg-ortho-biomech",
                )

                a_recs = memory.get_direct_qa_mastery_by_user("user-a")
                self.assertEqual(len(a_recs), 1)
                self.assertEqual(a_recs[0]["kg_node_id"], "kg-perio-stage")
                self.assertNotIn("kg-ortho-biomech", [r["kg_node_id"] for r in a_recs])
            finally:
                memory._conn.close()


class DirectQAMasteryKGIsolationTests(unittest.TestCase):
    """Verify different KG nodes are tracked independently."""

    def test_different_kg_nodes_are_independent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                r1 = memory.upsert_direct_qa_mastery(
                    user_id="user-a",
                    concept_name="牙髓炎诊断",
                    mastery_delta=-0.03,
                    kg_node_id="kg-pulp-diag",
                )
                r2 = memory.upsert_direct_qa_mastery(
                    user_id="user-a",
                    concept_name="根尖周炎诊断",
                    mastery_delta=-0.05,
                    kg_node_id="kg-periapical-diag",
                )

                self.assertNotEqual(r1["id"], r2["id"])

                all_recs = memory.get_direct_qa_mastery_by_user("user-a")
                self.assertEqual(len(all_recs), 2)

                # Filter by single KG node
                pulp_recs = memory.get_direct_qa_mastery_by_user(
                    "user-a", kg_node_id="kg-pulp-diag"
                )
                self.assertEqual(len(pulp_recs), 1)
                self.assertEqual(pulp_recs[0]["kg_node_id"], "kg-pulp-diag")
                self.assertAlmostEqual(pulp_recs[0]["mastery_score"], 0.27)

                periapical_recs = memory.get_direct_qa_mastery_by_user(
                    "user-a", kg_node_id="kg-periapical-diag"
                )
                self.assertEqual(len(periapical_recs), 1)
                self.assertEqual(periapical_recs[0]["kg_node_id"], "kg-periapical-diag")
                self.assertAlmostEqual(periapical_recs[0]["mastery_score"], 0.25)
            finally:
                memory._conn.close()

    def test_same_kg_node_repeated_updates_accumulate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                # Three confusion events on same KG node
                for i in range(3):
                    memory.upsert_direct_qa_mastery(
                        user_id="user-a",
                        concept_name="牙周手术适应症",
                        mastery_delta=-0.03,
                        kg_node_id="kg-perio-surgery",
                        evidence={"round": i},
                    )

                all_recs = memory.get_direct_qa_mastery_by_user(
                    "user-a", kg_node_id="kg-perio-surgery"
                )
                self.assertEqual(len(all_recs), 1)
                # 0.3 + 3*(-0.03) = 0.21
                self.assertAlmostEqual(all_recs[0]["mastery_score"], 0.21)
            finally:
                memory._conn.close()


class DirectQAMasterySourceAndEvidenceTests(unittest.TestCase):
    """Verify source is always 'direct_qa' and evidence round-trips correctly."""

    def test_source_is_always_direct_qa(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                rec = memory.upsert_direct_qa_mastery(
                    user_id="user-a",
                    concept_name="测试来源",
                    mastery_delta=0.0,
                )
                self.assertEqual(rec["source"], "direct_qa")

                # Update — source must still be direct_qa
                rec2 = memory.upsert_direct_qa_mastery(
                    user_id="user-a",
                    concept_name="测试来源",
                    mastery_delta=0.0,
                )
                self.assertEqual(rec2["source"], "direct_qa")
            finally:
                memory._conn.close()

    def test_evidence_preserved_across_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                evidence_v1 = {
                    "question": "什么是牙周袋？",
                    "resolved_question": "什么是牙周袋？",
                    "confusion_kg_node_ids": ["kg-perio-pocket"],
                    "used_evidence": [
                        {"chunk_id": "ch-a", "text": "牙周袋是..."},
                    ],
                }
                r1 = memory.upsert_direct_qa_mastery(
                    user_id="user-a",
                    concept_name="牙周袋定义",
                    mastery_delta=-0.03,
                    kg_node_id="kg-perio-pocket",
                    evidence=evidence_v1,
                    source_ref="ref-001",
                )
                self.assertEqual(r1["evidence"], evidence_v1)
                self.assertEqual(r1["source_ref"], "ref-001")

                evidence_v2 = {
                    "question": "牙周袋深度如何测量？",
                    "resolved_question": "牙周袋深度如何测量？",
                    "confusion_kg_node_ids": ["kg-perio-pocket"],
                    "used_evidence": [
                        {"chunk_id": "ch-b", "text": "测量方法是..."},
                    ],
                }
                r2 = memory.upsert_direct_qa_mastery(
                    user_id="user-a",
                    concept_name="牙周袋定义",
                    mastery_delta=-0.03,
                    kg_node_id="kg-perio-pocket",
                    evidence=evidence_v2,
                    source_ref="ref-002",
                )
                self.assertEqual(r2["evidence"], evidence_v2)
                self.assertEqual(r2["source_ref"], "ref-002")

                # Read back from DB
                all_recs = memory.get_direct_qa_mastery_by_user(
                    "user-a", kg_node_id="kg-perio-pocket"
                )
                self.assertEqual(len(all_recs), 1)
                self.assertEqual(all_recs[0]["evidence"], evidence_v2)
                self.assertEqual(all_recs[0]["source_ref"], "ref-002")
            finally:
                memory._conn.close()

    def test_empty_evidence_defaults_to_dict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                rec = memory.upsert_direct_qa_mastery(
                    user_id="user-a",
                    concept_name="无证据记录",
                    mastery_delta=-0.03,
                )
                self.assertEqual(rec["evidence"], {})
            finally:
                memory._conn.close()

    def test_null_source_ref_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                rec = memory.upsert_direct_qa_mastery(
                    user_id="user-a",
                    concept_name="无来源引用",
                    mastery_delta=-0.03,
                )
                self.assertIsNone(rec["source_ref"])
            finally:
                memory._conn.close()


class DirectQAMasteryNoPollutionTests(unittest.TestCase):
    """Verify Direct QA writes never pollute course mastery_records."""

    def test_direct_qa_writes_do_not_appear_in_mastery_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                # Write a Direct QA mastery record
                memory.upsert_direct_qa_mastery(
                    user_id="user-a",
                    concept_name="口腔黏膜病分类",
                    mastery_delta=-0.03,
                    kg_node_id="kg-oral-mucosa",
                )

                # Verify it is in the Direct QA table
                dqa_recs = memory.get_direct_qa_mastery_by_user("user-a")
                self.assertEqual(len(dqa_recs), 1)

                # Verify it is NOT in the course mastery_records table
                rows = memory._conn.execute(
                    "SELECT * FROM mastery_records WHERE user_id = ?",
                    ("user-a",),
                ).fetchall()
                self.assertEqual(len(rows), 0)
            finally:
                memory._conn.close()

    def test_course_mastery_does_not_appear_in_direct_qa(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                # Create a course session and write course mastery
                session = memory.create_session(
                    user_id="user-a", topic="牙体牙髓病学"
                )
                memory.upsert_mastery(
                    user_id="user-a",
                    session_id=session["id"],
                    concept_name="龋病病因",
                    mastery_delta=0.05,
                    kg_node_id="kg-caries-etiology",
                )

                # The course mastery should NOT leak into Direct QA reads
                dqa_recs = memory.get_direct_qa_mastery_by_user("user-a")
                self.assertEqual(len(dqa_recs), 0)
            finally:
                memory._conn.close()

    def test_both_tables_can_coexist_without_interference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                # Write course mastery
                session = memory.create_session(
                    user_id="user-a", topic="口腔颌面外科学"
                )
                memory.upsert_mastery(
                    user_id="user-a",
                    session_id=session["id"],
                    concept_name="阻生牙拔除术",
                    mastery_delta=0.05,
                    kg_node_id="kg-impacted-tooth",
                )

                # Write Direct QA mastery (same user, different concept)
                memory.upsert_direct_qa_mastery(
                    user_id="user-a",
                    concept_name="干槽症处理",
                    mastery_delta=-0.03,
                    kg_node_id="kg-dry-socket",
                )

                # Both tables have the right data
                course_rows = memory._conn.execute(
                    "SELECT * FROM mastery_records WHERE user_id = ?",
                    ("user-a",),
                ).fetchall()
                self.assertEqual(len(course_rows), 1)
                self.assertEqual(
                    dict(course_rows[0])["concept_name"], "阻生牙拔除术"
                )

                dqa_recs = memory.get_direct_qa_mastery_by_user("user-a")
                self.assertEqual(len(dqa_recs), 1)
                self.assertEqual(dqa_recs[0]["concept_name"], "干槽症处理")
            finally:
                memory._conn.close()

    def test_existing_get_mastery_by_kg_node_not_affected(self) -> None:
        """Course mastery query still works exactly as before."""
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                session = memory.create_session(
                    user_id="user-a", topic="口腔修复学"
                )
                memory.upsert_mastery(
                    user_id="user-a",
                    session_id=session["id"],
                    concept_name="全冠预备",
                    mastery_delta=0.10,
                    kg_node_id="kg-full-crown",
                )

                # Also write a Direct QA record
                memory.upsert_direct_qa_mastery(
                    user_id="user-a",
                    concept_name="嵌体适应证",
                    mastery_delta=-0.03,
                    kg_node_id="kg-inlay",
                )

                # get_mastery_by_kg_node should only see course mastery
                course_mastery = memory.get_mastery_by_kg_node(
                    "user-a", session["id"]
                )
                self.assertIn("kg-full-crown", course_mastery)
                self.assertNotIn("kg-inlay", course_mastery)
            finally:
                memory._conn.close()


class DirectQAMasteryEmptyReadTests(unittest.TestCase):
    """Verify read methods behave correctly when no data exists."""

    def test_get_direct_qa_mastery_by_user_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                recs = memory.get_direct_qa_mastery_by_user("no-such-user")
                self.assertEqual(recs, [])
            finally:
                memory._conn.close()

    def test_get_direct_qa_mastery_by_user_kg_filter_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                recs = memory.get_direct_qa_mastery_by_user(
                    "no-such-user", kg_node_id="kg-nonexistent"
                )
                self.assertEqual(recs, [])
            finally:
                memory._conn.close()


class DirectQAMasteryMultipleUsersMultipleKGTests(unittest.TestCase):
    """Second-verification sample: at least two users and two KG nodes,
    verifying isolation and repeated updates."""

    def test_two_users_two_kg_nodes_full_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                # User 1, KG node A — three confusion events
                for i in range(3):
                    memory.upsert_direct_qa_mastery(
                        user_id="user-1",
                        concept_name="牙髓血运重建",
                        mastery_delta=-0.03,
                        kg_node_id="kg-pulp-revasc",
                        evidence={"seq": i},
                    )

                # User 2, KG node A — one confusion event
                memory.upsert_direct_qa_mastery(
                    user_id="user-2",
                    concept_name="牙髓血运重建",
                    mastery_delta=-0.03,
                    kg_node_id="kg-pulp-revasc",
                )

                # User 1, KG node B — one confusion event
                memory.upsert_direct_qa_mastery(
                    user_id="user-1",
                    concept_name="显微根管治疗",
                    mastery_delta=-0.03,
                    kg_node_id="kg-micro-endo",
                )

                # User 2, KG node B — two confusion events
                for i in range(2):
                    memory.upsert_direct_qa_mastery(
                        user_id="user-2",
                        concept_name="显微根管治疗",
                        mastery_delta=-0.03,
                        kg_node_id="kg-micro-endo",
                    )

                # Verify user-1
                u1_all = memory.get_direct_qa_mastery_by_user("user-1")
                self.assertEqual(len(u1_all), 2)

                u1_revasc = memory.get_direct_qa_mastery_by_user(
                    "user-1", kg_node_id="kg-pulp-revasc"
                )
                self.assertEqual(len(u1_revasc), 1)
                # 0.3 + 3*(-0.03) = 0.21
                self.assertAlmostEqual(u1_revasc[0]["mastery_score"], 0.21)
                self.assertEqual(u1_revasc[0]["user_id"], "user-1")

                u1_micro = memory.get_direct_qa_mastery_by_user(
                    "user-1", kg_node_id="kg-micro-endo"
                )
                self.assertEqual(len(u1_micro), 1)
                # 0.3 + (-0.03) = 0.27
                self.assertAlmostEqual(u1_micro[0]["mastery_score"], 0.27)

                # Verify user-2
                u2_all = memory.get_direct_qa_mastery_by_user("user-2")
                self.assertEqual(len(u2_all), 2)

                u2_revasc = memory.get_direct_qa_mastery_by_user(
                    "user-2", kg_node_id="kg-pulp-revasc"
                )
                self.assertEqual(len(u2_revasc), 1)
                # 0.3 + (-0.03) = 0.27
                self.assertAlmostEqual(u2_revasc[0]["mastery_score"], 0.27)
                self.assertEqual(u2_revasc[0]["user_id"], "user-2")

                u2_micro = memory.get_direct_qa_mastery_by_user(
                    "user-2", kg_node_id="kg-micro-endo"
                )
                self.assertEqual(len(u2_micro), 1)
                # 0.3 + 2*(-0.03) = 0.24
                self.assertAlmostEqual(u2_micro[0]["mastery_score"], 0.24)

                # Verify IDs are all distinct
                all_ids = {r["id"] for r in u1_all} | {r["id"] for r in u2_all}
                self.assertEqual(len(all_ids), 4)
            finally:
                memory._conn.close()


if __name__ == "__main__":
    unittest.main()
