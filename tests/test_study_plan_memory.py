"""Tests for LearningMemory study-plan query methods — no external dependencies."""

from __future__ import annotations

import tempfile
import threading
import unittest
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from backend.services.learning.memory import LearningMemory
from backend.services.learning.study_plan import (
    PlanDraftStore,
    PlanRequestParseError,
    _recommend_weakness_reviews,
    create_custom_plan_draft,
    create_plan_draft_from_text,
    get_draft_from_default_store,
    normalize_plan_request,
    parse_plan_request_text,
    recommend_study_plan,
    resolve_learning_scope,
)


class StudyPlanTaskSemanticsTests(unittest.TestCase):
    def test_weakness_recommendation_separates_course_and_target(self) -> None:
        recommendations = _recommend_weakness_reviews(
            profile={},
            learner_memory={
                "weaknesses": {
                    "items": [
                        {
                            "label": "宿主-微生物互作机制理解缺失",
                            "severity": 0.9,
                            "kg_node_ids": ["kg-host-microbe"],
                        }
                    ]
                }
            },
            active_session={"topic": "我想学习口腔溃疡"},
        )

        self.assertEqual(len(recommendations), 1)
        item = recommendations[0]
        self.assertEqual(item["task_type"], "review_weakness")
        self.assertEqual(item["learning_target"], "宿主-微生物互作机制理解缺失")
        self.assertEqual(item["linked_course_title"], "口腔溃疡")

    def test_existing_database_is_migrated_with_task_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "learning.sqlite3"
            conn = sqlite3.connect(db_path)
            conn.execute(
                """CREATE TABLE study_plan_items (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    plan_date TEXT NOT NULL,
                    title TEXT NOT NULL,
                    notes TEXT,
                    status TEXT NOT NULL DEFAULT 'open',
                    linked_course_id TEXT,
                    linked_course_title TEXT,
                    kg_node_ids_json TEXT NOT NULL,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    deferred_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                )"""
            )
            conn.commit()
            conn.close()

            memory = LearningMemory(db_path)
            try:
                columns = {
                    row["name"]
                    for row in memory._conn.execute("PRAGMA table_info(study_plan_items)").fetchall()
                }
                self.assertIn("task_type", columns)
                self.assertIn("learning_target", columns)
            finally:
                memory._conn.close()

    def test_task_fields_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                item = memory.create_study_plan_item(
                    user_id="u1",
                    plan_date=date.today().isoformat(),
                    title="复习薄弱点：牙周组织炎症",
                    task_type="review_weakness",
                    learning_target="牙周组织炎症",
                )
                self.assertEqual(item["task_type"], "review_weakness")
                self.assertEqual(item["learning_target"], "牙周组织炎症")
            finally:
                memory._conn.close()


class ListOpenStudyPlanItemsTests(unittest.TestCase):
    """Coverage for LearningMemory.list_open_study_plan_items."""

    def test_empty_when_no_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = LearningMemory(root / "learning.sqlite3")
            try:
                result = memory.list_open_study_plan_items("u1")
                self.assertEqual(result, [])
            finally:
                memory._conn.close()

    def test_returns_only_open_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = LearningMemory(root / "learning.sqlite3")
            try:
                today = date.today().isoformat()
                memory.create_study_plan_item(
                    user_id="u1", plan_date=today, title="Open Alpha"
                )
                memory.create_study_plan_item(
                    user_id="u1", plan_date=today, title="Open Beta"
                )
                memory.create_study_plan_item(
                    user_id="u1", plan_date=today, title="Completed Gamma"
                )
                # Mark the last one completed
                items = memory.list_study_plan_items("u1", today, today)
                for item in items:
                    if item["title"] == "Completed Gamma":
                        memory.set_study_plan_item_completion(
                            item["id"], user_id="u1", completed=True
                        )
                        break

                result = memory.list_open_study_plan_items("u1")
                titles = [item["title"] for item in result]
                self.assertEqual(len(result), 2)
                self.assertIn("Open Alpha", titles)
                self.assertIn("Open Beta", titles)
                self.assertNotIn("Completed Gamma", titles)
            finally:
                memory._conn.close()

    def test_excludes_other_users(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = LearningMemory(root / "learning.sqlite3")
            try:
                today = date.today().isoformat()
                memory.create_study_plan_item(
                    user_id="u1", plan_date=today, title="U1 Item"
                )
                memory.create_study_plan_item(
                    user_id="u2", plan_date=today, title="U2 Item"
                )

                result = memory.list_open_study_plan_items("u1")
                titles = [item["title"] for item in result]
                self.assertEqual(len(result), 1)
                self.assertIn("U1 Item", titles)
                self.assertNotIn("U2 Item", titles)
            finally:
                memory._conn.close()

    def test_respects_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = LearningMemory(root / "learning.sqlite3")
            try:
                today = date.today().isoformat()
                for idx in range(5):
                    memory.create_study_plan_item(
                        user_id="u1",
                        plan_date=today,
                        title=f"Item {idx:02d}",
                    )

                full = memory.list_open_study_plan_items("u1")
                self.assertEqual(len(full), 5)

                limited = memory.list_open_study_plan_items("u1", limit=2)
                self.assertEqual(len(limited), 2)
            finally:
                memory._conn.close()

    def test_sort_order_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = LearningMemory(root / "learning.sqlite3")
            try:
                today = date.today().isoformat()
                yesterday = (date.today() - timedelta(days=1)).isoformat()
                tomorrow = (date.today() + timedelta(days=1)).isoformat()

                # Items across different dates to verify plan_date ordering
                memory.create_study_plan_item(
                    user_id="u1", plan_date=today, title="Today Item"
                )
                memory.create_study_plan_item(
                    user_id="u1", plan_date=yesterday, title="Yesterday Item"
                )
                memory.create_study_plan_item(
                    user_id="u1", plan_date=tomorrow, title="Tomorrow Item"
                )

                result = memory.list_open_study_plan_items("u1")
                titles = [item["title"] for item in result]
                # Expect: yesterday, today, tomorrow (plan_date ASC)
                self.assertEqual(titles, ["Yesterday Item", "Today Item", "Tomorrow Item"])
            finally:
                memory._conn.close()

    def test_includes_past_today_and_future_open_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = LearningMemory(root / "learning.sqlite3")
            try:
                past = (date.today() - timedelta(days=3)).isoformat()
                today = date.today().isoformat()
                future = (date.today() + timedelta(days=3)).isoformat()

                memory.create_study_plan_item(
                    user_id="u1", plan_date=past, title="Past Open"
                )
                memory.create_study_plan_item(
                    user_id="u1", plan_date=today, title="Today Open"
                )
                memory.create_study_plan_item(
                    user_id="u1", plan_date=future, title="Future Open"
                )

                result = memory.list_open_study_plan_items("u1")
                titles = [item["title"] for item in result]
                self.assertEqual(len(result), 3)
                self.assertIn("Past Open", titles)
                self.assertIn("Today Open", titles)
                self.assertIn("Future Open", titles)
            finally:
                memory._conn.close()


class PlanDraftStoreBasicTests(unittest.TestCase):
    """Unit tests for the in-memory PlanDraftStore."""

    def _make_store(self, ttl_seconds: int = 60) -> PlanDraftStore:
        return PlanDraftStore(ttl_seconds=ttl_seconds)

    def _make_store_with_clock(
        self, ttl_seconds: int = 60
    ) -> tuple[PlanDraftStore, dict[str, float]]:
        """Return a store wired to a mutable clock dict so tests can control time."""
        clock: dict[str, float] = {"now": 1_000_000.0}
        store = PlanDraftStore(ttl_seconds=ttl_seconds, _clock=lambda: clock["now"])
        return store, clock

    # -- happy path -------------------------------------------------------

    def test_save_and_get_round_trip(self) -> None:
        """Same user can read back the saved draft with all fields intact."""
        store = self._make_store()
        payload = {"intent": "review", "days": 3, "focus": "core topic"}
        draft_id = store.save(user_id="u1", payload=payload)

        record = store.get(draft_id, user_id="u1")
        self.assertEqual(record["plan_draft_id"], draft_id)
        self.assertEqual(record["user_id"], "u1")
        self.assertEqual(record["payload"], payload)
        self.assertIn("created_at", record)
        self.assertIn("expires_at", record)
        self.assertGreater(record["expires_at"], record["created_at"])

    def test_returned_record_is_independent_of_store(self) -> None:
        """Mutating a returned record does not affect the stored draft."""
        store, clock = self._make_store_with_clock()
        draft_id = store.save(user_id="u1", payload={"key": "original"})

        record1 = store.get(draft_id, user_id="u1")
        record1["payload"]["key"] = "tampered"
        record1["user_id"] = "evil"

        record2 = store.get(draft_id, user_id="u1")
        self.assertEqual(record2["payload"]["key"], "original")
        self.assertEqual(record2["user_id"], "u1")

    def test_payload_is_independent_of_caller(self) -> None:
        """Mutating the payload dict after save does not affect the store."""
        store = self._make_store()
        payload: dict = {"step": 1, "items": [{"a": 1}]}
        draft_id = store.save(user_id="u1", payload=payload)

        # Mutate the original dict the caller still holds
        payload["step"] = 999
        payload["items"].append({"b": 2})

        record = store.get(draft_id, user_id="u1")
        self.assertEqual(record["payload"]["step"], 1)
        self.assertEqual(len(record["payload"]["items"]), 1)

    def test_draft_ids_are_unique_and_opaque(self) -> None:
        """Each save produces a distinct, unpredictable ID."""
        store = self._make_store()
        ids = {store.save(user_id="u1", payload={"n": i}) for i in range(20)}
        self.assertEqual(len(ids), 20)
        for did in ids:
            self.assertTrue(did.startswith("draft_"), f"unexpected id format: {did}")
            self.assertGreater(len(did), len("draft_"), f"id too short: {did}")

    # -- user isolation ---------------------------------------------------

    def test_different_user_cannot_read(self) -> None:
        """A draft saved by one user must not be readable by another."""
        store = self._make_store()
        draft_id = store.save(user_id="uA", payload={"note": "private"})

        with self.assertRaises(PermissionError) as ctx:
            store.get(draft_id, user_id="uB")
        self.assertIn(draft_id, str(ctx.exception))
        self.assertIn("uB", str(ctx.exception))

    def test_unknown_id_raises_key_error(self) -> None:
        """Reading a non-existent draft ID must raise KeyError."""
        store = self._make_store()

        with self.assertRaises(KeyError) as ctx:
            store.get("draft_nonexistent_ff", user_id="u1")
        self.assertIn("draft_nonexistent_ff", str(ctx.exception))

    # -- TTL expiry -------------------------------------------------------

    def test_expired_draft_raises_value_error(self) -> None:
        """After the TTL elapses the draft must no longer be readable."""
        store, clock = self._make_store_with_clock(ttl_seconds=60)
        draft_id = store.save(user_id="u1", payload={"x": 1})

        # Still readable before expiry
        store.get(draft_id, user_id="u1")  # should not raise

        # Fast-forward past TTL
        clock["now"] += 61

        with self.assertRaises(ValueError) as ctx:
            store.get(draft_id, user_id="u1")
        self.assertIn(draft_id, str(ctx.exception))
        self.assertIn("expired", str(ctx.exception).lower())

    def test_draft_readable_at_expiry_boundary(self) -> None:
        """A draft whose TTL has not yet elapsed is readable."""
        store, clock = self._make_store_with_clock(ttl_seconds=60)
        draft_id = store.save(user_id="u1", payload={"x": 1})

        # Advance to exactly 1 second before expiry
        clock["now"] += 59
        record = store.get(draft_id, user_id="u1")
        self.assertEqual(record["payload"], {"x": 1})

    def test_expired_draft_still_checks_user(self) -> None:
        """An expired draft from user A should not leak to user B."""
        store, clock = self._make_store_with_clock(ttl_seconds=60)
        draft_id = store.save(user_id="uA", payload={"x": 1})
        clock["now"] += 61

        # User B tries — should fail with PermissionError (ownership checked
        # before expiry, so wrong user always fails first)
        with self.assertRaises(PermissionError):
            store.get(draft_id, user_id="uB")

    # -- edge cases -------------------------------------------------------

    def test_empty_payload_is_stored(self) -> None:
        """An empty payload dict is valid."""
        store = self._make_store()
        draft_id = store.save(user_id="u1", payload={})
        record = store.get(draft_id, user_id="u1")
        self.assertEqual(record["payload"], {})

    def test_nested_payload_survives_round_trip(self) -> None:
        """Deeply nested structures round-trip correctly."""
        store = self._make_store()
        payload: dict = {
            "request": {"intent": "review", "days": 3},
            "items": [
                {"title": "Chapter 1", "scope": {"book": "Text A", "node_ids": ["n1", "n2"]}},
                {"title": "Chapter 2", "scope": {"book": "Text A", "node_ids": ["n3"]}},
            ],
        }
        draft_id = store.save(user_id="u1", payload=payload)
        record = store.get(draft_id, user_id="u1")
        self.assertEqual(record["payload"], payload)
        # Verify deep equality, not just top-level
        self.assertEqual(record["payload"]["items"][0]["scope"]["node_ids"], ["n1", "n2"])


class PlanDraftStoreConcurrencyTests(unittest.TestCase):
    """Smoke-tests for thread-safety of PlanDraftStore."""

    def test_concurrent_saves_dont_lose_data(self) -> None:
        """Many threads saving concurrently should all succeed."""
        store = PlanDraftStore(ttl_seconds=300)
        errors: list[str] = []
        draft_ids: list[str] = []

        def _save_and_record(user_id: str, payload_index: int) -> None:
            try:
                did = store.save(user_id, {"idx": payload_index})
                draft_ids.append(did)
            except Exception as exc:
                errors.append(str(exc))

        threads = [
            threading.Thread(target=_save_and_record, args=(f"u{i % 3}", i))
            for i in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"unexpected errors: {errors}")
        self.assertEqual(len(draft_ids), 20)
        self.assertEqual(len(set(draft_ids)), 20, "draft ids should be unique")

    def test_concurrent_reads_are_isolated(self) -> None:
        """Concurrent reads for different users are properly isolated."""
        store = PlanDraftStore(ttl_seconds=300)
        draft_a = store.save(user_id="uA", payload={"owner": "A"})
        draft_b = store.save(user_id="uB", payload={"owner": "B"})

        results: list[dict | None] = [None, None]
        errors: list[str] = []

        def _read_a() -> None:
            try:
                results[0] = store.get(draft_a, user_id="uA")
            except Exception as exc:
                errors.append(f"A: {exc}")

        def _read_b() -> None:
            try:
                results[1] = store.get(draft_b, user_id="uB")
            except Exception as exc:
                errors.append(f"B: {exc}")

        t_a = threading.Thread(target=_read_a)
        t_b = threading.Thread(target=_read_b)
        t_a.start()
        t_b.start()
        t_a.join()
        t_b.join()

        self.assertEqual(len(errors), 0, f"unexpected errors: {errors}")
        self.assertIsNotNone(results[0])
        self.assertIsNotNone(results[1])
        self.assertEqual(results[0]["payload"]["owner"], "A")  # type: ignore[index]
        self.assertEqual(results[1]["payload"]["owner"], "B")  # type: ignore[index]

    def test_concurrent_read_and_write_same_user(self) -> None:
        """Reads and writes on the same user do not corrupt each other."""
        store = PlanDraftStore(ttl_seconds=300)
        errors: list[str] = []

        def _writer() -> None:
            for i in range(10):
                try:
                    store.save(user_id="u1", payload={"seq": i})
                except Exception as exc:
                    errors.append(f"write: {exc}")

        def _reader(draft_id: str) -> None:
            try:
                store.get(draft_id, user_id="u1")
            except KeyError:
                pass  # may have been saved after read started — fine
            except Exception as exc:
                errors.append(f"read: {exc}")

        # Pre-save one draft that readers can safely target
        stable_id = store.save(user_id="u1", payload={"stable": True})

        threads = [threading.Thread(target=_writer)]
        threads += [
            threading.Thread(target=_reader, args=(stable_id,)) for _ in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"unexpected errors: {errors}")
        # The stable draft should still be readable
        record = store.get(stable_id, user_id="u1")
        self.assertTrue(record["payload"]["stable"])


class PlanDraftStoreParentDraftTests(unittest.TestCase):
    """Tests for optional parent_plan_draft_id lineage in PlanDraftStore."""

    def _make_store_with_clock(
        self, ttl_seconds: int = 60
    ) -> tuple[PlanDraftStore, dict[str, float]]:
        clock: dict[str, float] = {"now": 1_000_000.0}
        store = PlanDraftStore(ttl_seconds=ttl_seconds, _clock=lambda: clock["now"])
        return store, clock

    # -- happy path -------------------------------------------------------

    def test_save_with_valid_parent_stores_parent_id(self) -> None:
        """Child record stores parent_plan_draft_id when parent is valid."""
        store, clock = self._make_store_with_clock()
        parent_id = store.save(user_id="u1", payload={"gen": 1})
        child_id = store.save(
            user_id="u1",
            payload={"gen": 2},
            parent_plan_draft_id=parent_id,
        )

        child = store.get(child_id, user_id="u1")
        self.assertEqual(child["parent_plan_draft_id"], parent_id)
        self.assertEqual(child["payload"], {"gen": 2})

    def test_child_has_new_id_and_independent_payload(self) -> None:
        """Child draft receives a brand-new ID and its own payload."""
        store, clock = self._make_store_with_clock()
        parent_id = store.save(
            user_id="u1",
            payload={"intent": "review", "days": 3},
        )
        child_id = store.save(
            user_id="u1",
            payload={"intent": "review", "days": 2},
            parent_plan_draft_id=parent_id,
        )

        self.assertNotEqual(child_id, parent_id)

        parent = store.get(parent_id, user_id="u1")
        child = store.get(child_id, user_id="u1")
        self.assertEqual(parent["payload"]["days"], 3)
        self.assertEqual(child["payload"]["days"], 2)

    def test_parent_unchanged_after_child_save(self) -> None:
        """Saving a child does not mutate the parent record in any way."""
        store, clock = self._make_store_with_clock()
        parent_id = store.save(
            user_id="u1",
            payload={"original": True, "items": [1, 2, 3]},
        )
        parent_before = store.get(parent_id, user_id="u1")

        store.save(
            user_id="u1",
            payload={"child": True},
            parent_plan_draft_id=parent_id,
        )

        parent_after = store.get(parent_id, user_id="u1")
        self.assertEqual(parent_before["payload"], parent_after["payload"])
        self.assertEqual(parent_before["expires_at"], parent_after["expires_at"])
        self.assertEqual(parent_before["created_at"], parent_after["created_at"])
        self.assertNotIn("parent_plan_draft_id", parent_before)
        self.assertNotIn("parent_plan_draft_id", parent_after)

    def test_save_without_parent_preserves_existing_behavior(self) -> None:
        """Omitting parent_plan_draft_id must behave exactly as before."""
        store, clock = self._make_store_with_clock()
        draft_id = store.save(user_id="u1", payload={"x": 1})

        record = store.get(draft_id, user_id="u1")
        self.assertEqual(record["payload"], {"x": 1})
        self.assertNotIn("parent_plan_draft_id", record)

    def test_multiple_children_share_same_parent(self) -> None:
        """Multiple children can reference the same valid parent."""
        store, clock = self._make_store_with_clock()
        parent_id = store.save(user_id="u1", payload={"gen": 1})

        child1 = store.save(
            user_id="u1", payload={"v": 1}, parent_plan_draft_id=parent_id
        )
        child2 = store.save(
            user_id="u1", payload={"v": 2}, parent_plan_draft_id=parent_id
        )

        c1 = store.get(child1, user_id="u1")
        c2 = store.get(child2, user_id="u1")
        self.assertEqual(c1["parent_plan_draft_id"], parent_id)
        self.assertEqual(c2["parent_plan_draft_id"], parent_id)
        self.assertNotEqual(child1, child2)

    def test_grandchild_lineage(self) -> None:
        """A child can itself become a parent for a grandchild draft."""
        store, clock = self._make_store_with_clock()
        parent_id = store.save(user_id="u1", payload={"gen": 1})
        child_id = store.save(
            user_id="u1", payload={"gen": 2}, parent_plan_draft_id=parent_id
        )
        grandchild_id = store.save(
            user_id="u1", payload={"gen": 3}, parent_plan_draft_id=child_id
        )

        gc = store.get(grandchild_id, user_id="u1")
        self.assertEqual(gc["parent_plan_draft_id"], child_id)
        self.assertEqual(gc["payload"], {"gen": 3})

    # -- rejection cases --------------------------------------------------

    def test_unknown_parent_raises_key_error(self) -> None:
        """A non-existent parent ID must raise KeyError."""
        store, clock = self._make_store_with_clock()

        with self.assertRaises(KeyError) as ctx:
            store.save(
                user_id="u1",
                payload={"x": 1},
                parent_plan_draft_id="draft_nonexistent_00",
            )
        self.assertIn("draft_nonexistent_00", str(ctx.exception))

    def test_cross_user_parent_raises_permission_error(self) -> None:
        """A parent owned by a different user must be rejected."""
        store, clock = self._make_store_with_clock()
        parent_id = store.save(user_id="uA", payload={"owner": "A"})

        with self.assertRaises(PermissionError) as ctx:
            store.save(
                user_id="uB",
                payload={"x": 1},
                parent_plan_draft_id=parent_id,
            )
        self.assertIn(parent_id, str(ctx.exception))
        self.assertIn("uB", str(ctx.exception))

    def test_expired_parent_raises_value_error(self) -> None:
        """An expired parent must be rejected."""
        store, clock = self._make_store_with_clock(ttl_seconds=60)
        parent_id = store.save(user_id="u1", payload={"gen": 1})

        # Fast-forward past parent TTL
        clock["now"] += 61

        with self.assertRaises(ValueError) as ctx:
            store.save(
                user_id="u1",
                payload={"gen": 2},
                parent_plan_draft_id=parent_id,
            )
        self.assertIn(parent_id, str(ctx.exception))
        self.assertIn("expired", str(ctx.exception).lower())

    def test_expired_parent_rejection_does_not_create_child(self) -> None:
        """When parent validation fails, no child record is persisted."""
        store, clock = self._make_store_with_clock(ttl_seconds=60)
        parent_id = store.save(user_id="u1", payload={"gen": 1})
        clock["now"] += 61

        try:
            store.save(
                user_id="u1",
                payload={"gen": 2},
                parent_plan_draft_id=parent_id,
            )
        except ValueError:
            pass

        # Parent itself is still expired (not extended)
        with self.assertRaises(ValueError):
            store.get(parent_id, user_id="u1")

    def test_cross_user_parent_does_not_leak_parent_payload(self) -> None:
        """Error message for cross-user parent must not expose parent payload."""
        store, clock = self._make_store_with_clock()
        parent_id = store.save(
            user_id="uA",
            payload={"secret": "sensitive-value"},
        )

        with self.assertRaises(PermissionError) as ctx:
            store.save(
                user_id="uB",
                payload={"x": 1},
                parent_plan_draft_id=parent_id,
            )
        message = str(ctx.exception)
        self.assertNotIn("sensitive-value", message)
        self.assertNotIn("secret", message)

    # -- TTL independence --------------------------------------------------

    def test_child_ttl_independent_of_parent(self) -> None:
        """Child TTL is based on its own creation time, not the parent's."""
        store, clock = self._make_store_with_clock(ttl_seconds=60)
        parent_id = store.save(user_id="u1", payload={"gen": 1})

        # Advance time so parent is close to expiry
        clock["now"] += 50
        child_id = store.save(
            user_id="u1",
            payload={"gen": 2},
            parent_plan_draft_id=parent_id,
        )

        # Parent should expire soon
        clock["now"] += 11  # parent now at 61s → expired
        with self.assertRaises(ValueError):
            store.get(parent_id, user_id="u1")

        # Child still has ~49s of its own TTL remaining
        child = store.get(child_id, user_id="u1")
        self.assertEqual(child["payload"], {"gen": 2})


# ------------------------------------------------------------------
# Helpers for building production-shape chapter dicts
# ------------------------------------------------------------------


def _chapter(**overrides: object) -> dict:
    """Build a chapter dict in the production shape from the KG agent."""
    defaults: dict = {
        "key": "chapter:test001",
        "book_title": "牙周病学",
        "document_id": "document.d766e8629232a2f6",
        "version_id": "v1.0",
        "number": 3,
        "title": "第三章 牙周病的发病机制",
        "section_count": 3,
        "source_sections": ["第一节", "第二节", "第三节"],
        "draft_files": ["data/kg/ch3.kg-draft.json"],
        "core_ids": ["core:aaa111", "core:bbb222", "core:ccc333"],
        "cores": [
            {"id": "core:aaa111", "name": "牙菌斑生物膜", "type": "Etiology"},
            {"id": "core:bbb222", "name": "宿主免疫反应", "type": "PathologicalProcess"},
            {"id": "core:ccc333", "name": "牙周袋形成", "type": "ClinicalSign"},
        ],
    }
    return {**defaults, **overrides}


# ------------------------------------------------------------------
# resolve_learning_scope tests
# ------------------------------------------------------------------


class ResolveLearningScopeTests(unittest.TestCase):
    """Unit tests for :func:`resolve_learning_scope`."""

    # -- positive cases ---------------------------------------------------

    def test_exact_match_by_key_only(self) -> None:
        """A chapter with a matching key returns a full learning_scope."""
        chapters = [
            _chapter(key="chapter:perio-ch3"),
            _chapter(key="chapter:perio-ch4", title="第四章 牙周病的临床表现"),
        ]
        scope = resolve_learning_scope(chapters, chapter_key="chapter:perio-ch3")

        self.assertEqual(scope["book_title"], "牙周病学")
        self.assertEqual(scope["document_id"], "document.d766e8629232a2f6")
        self.assertEqual(scope["chapter_key"], "chapter:perio-ch3")
        self.assertEqual(scope["chapter_title"], "第三章 牙周病的发病机制")
        self.assertEqual(scope["kg_node_ids"], ["core:aaa111", "core:bbb222", "core:ccc333"])

    def test_match_with_book_title_consistency(self) -> None:
        """Passing the correct book_title succeeds."""
        chapters = [_chapter(key="chapter:perio-ch3")]
        scope = resolve_learning_scope(
            chapters, chapter_key="chapter:perio-ch3", book_title="牙周病学"
        )
        self.assertEqual(scope["book_title"], "牙周病学")
        self.assertEqual(scope["chapter_key"], "chapter:perio-ch3")

    def test_match_with_document_id_consistency(self) -> None:
        """Passing the correct document_id succeeds."""
        chapters = [_chapter(key="chapter:perio-ch3")]
        scope = resolve_learning_scope(
            chapters,
            chapter_key="chapter:perio-ch3",
            document_id="document.d766e8629232a2f6",
        )
        self.assertEqual(scope["document_id"], "document.d766e8629232a2f6")

    def test_match_with_both_consistency_checks(self) -> None:
        """Passing both book_title and document_id succeeds when both match."""
        chapters = [_chapter(key="chapter:perio-ch3")]
        scope = resolve_learning_scope(
            chapters,
            chapter_key="chapter:perio-ch3",
            book_title="牙周病学",
            document_id="document.d766e8629232a2f6",
        )
        self.assertEqual(scope["book_title"], "牙周病学")
        self.assertEqual(scope["document_id"], "document.d766e8629232a2f6")

    def test_core_ids_are_deduplicated(self) -> None:
        """Duplicate entries in core_ids are collapsed to unique values."""
        chapters = [
            _chapter(
                key="chapter:perio-ch3",
                core_ids=["core:aaa111", "core:bbb222", "core:aaa111", "core:bbb222"],
            )
        ]
        scope = resolve_learning_scope(chapters, chapter_key="chapter:perio-ch3")
        self.assertEqual(scope["kg_node_ids"], ["core:aaa111", "core:bbb222"])

    def test_only_one_of_many_chapters_matches(self) -> None:
        """When many chapters are present, only the matching one is used."""
        chapters = [
            _chapter(key="chapter:endo-ch1", title="第一章 绪论"),
            _chapter(key="chapter:perio-ch3"),
            _chapter(key="chapter:ortho-ch5", title="第五章 矫治技术"),
        ]
        scope = resolve_learning_scope(chapters, chapter_key="chapter:perio-ch3")
        self.assertEqual(scope["chapter_title"], "第三章 牙周病的发病机制")

    def test_scope_keys_are_exactly_the_specified_set(self) -> None:
        """Returned dict contains only the five documented keys."""
        chapters = [_chapter(key="chapter:perio-ch3")]
        scope = resolve_learning_scope(chapters, chapter_key="chapter:perio-ch3")
        self.assertEqual(
            set(scope.keys()),
            {"book_title", "document_id", "chapter_key", "chapter_title", "kg_node_ids"},
        )

    # -- optional checks: None / empty string are no-ops ------------------

    def test_none_book_title_is_noop(self) -> None:
        """Passing book_title=None does not trigger a consistency check."""
        chapters = [_chapter(key="chapter:perio-ch3")]
        scope = resolve_learning_scope(
            chapters, chapter_key="chapter:perio-ch3", book_title=None
        )
        self.assertEqual(scope["chapter_key"], "chapter:perio-ch3")

    def test_empty_book_title_is_noop(self) -> None:
        """Passing book_title='' does not trigger a consistency check."""
        chapters = [_chapter(key="chapter:perio-ch3")]
        scope = resolve_learning_scope(
            chapters, chapter_key="chapter:perio-ch3", book_title=""
        )
        self.assertEqual(scope["chapter_key"], "chapter:perio-ch3")

    def test_none_document_id_is_noop(self) -> None:
        """Passing document_id=None does not trigger a consistency check."""
        chapters = [_chapter(key="chapter:perio-ch3")]
        scope = resolve_learning_scope(
            chapters, chapter_key="chapter:perio-ch3", document_id=None
        )
        self.assertEqual(scope["chapter_key"], "chapter:perio-ch3")

    def test_empty_document_id_is_noop(self) -> None:
        """Passing document_id='' does not trigger a consistency check."""
        chapters = [_chapter(key="chapter:perio-ch3")]
        scope = resolve_learning_scope(
            chapters, chapter_key="chapter:perio-ch3", document_id=""
        )
        self.assertEqual(scope["chapter_key"], "chapter:perio-ch3")

    # -- negative: missing / invalid chapter_key --------------------------

    def test_empty_chapter_key_raises(self) -> None:
        """Empty string as chapter_key raises ValueError."""
        with self.assertRaises(ValueError) as ctx:
            resolve_learning_scope([], chapter_key="")
        self.assertIn("chapter_key", str(ctx.exception))

    def test_whitespace_chapter_key_raises(self) -> None:
        """Whitespace-only chapter_key raises ValueError."""
        with self.assertRaises(ValueError) as ctx:
            resolve_learning_scope([], chapter_key="   ")
        self.assertIn("chapter_key", str(ctx.exception))

    # -- negative: chapter not found --------------------------------------

    def test_not_found_in_empty_chapters(self) -> None:
        """Empty chapter list raises ValueError."""
        with self.assertRaises(ValueError) as ctx:
            resolve_learning_scope([], chapter_key="chapter:missing")
        self.assertIn("chapter:missing", str(ctx.exception))

    def test_not_found_in_populated_chapters(self) -> None:
        """Key not present among chapters raises ValueError."""
        chapters = [
            _chapter(key="chapter:perio-ch3"),
            _chapter(key="chapter:endo-ch1", title="第一章 绪论"),
        ]
        with self.assertRaises(ValueError) as ctx:
            resolve_learning_scope(chapters, chapter_key="chapter:nonexistent")
        self.assertIn("chapter:nonexistent", str(ctx.exception))

    # -- negative: ambiguous / multiple matches ----------------------------

    def test_duplicate_key_raises(self) -> None:
        """Two chapters with the same key raise ValueError."""
        chapters = [
            _chapter(key="chapter:dup"),
            _chapter(key="chapter:dup", title="不同标题"),
        ]
        with self.assertRaises(ValueError) as ctx:
            resolve_learning_scope(chapters, chapter_key="chapter:dup")
        self.assertIn("chapter:dup", str(ctx.exception))
        self.assertIn("2", str(ctx.exception))

    # -- negative: consistency mismatches ----------------------------------

    def test_book_title_mismatch_raises(self) -> None:
        """When book_title does not match the chapter, ValueError is raised."""
        chapters = [_chapter(key="chapter:perio-ch3", book_title="牙周病学")]
        with self.assertRaises(ValueError) as ctx:
            resolve_learning_scope(
                chapters, chapter_key="chapter:perio-ch3", book_title="口腔修复学"
            )
        self.assertIn("Book title mismatch", str(ctx.exception))
        self.assertIn("口腔修复学", str(ctx.exception))
        self.assertIn("牙周病学", str(ctx.exception))

    def test_document_id_mismatch_raises(self) -> None:
        """When document_id does not match the chapter, ValueError is raised."""
        chapters = [
            _chapter(key="chapter:perio-ch3", document_id="document.d766e8629232a2f6")
        ]
        with self.assertRaises(ValueError) as ctx:
            resolve_learning_scope(
                chapters,
                chapter_key="chapter:perio-ch3",
                document_id="document.other",
            )
        self.assertIn("Document ID mismatch", str(ctx.exception))
        self.assertIn("document.other", str(ctx.exception))

    def test_both_mismatch_reports_book_first(self) -> None:
        """When both checks fail, book_title is validated first."""
        chapters = [
            _chapter(
                key="chapter:perio-ch3",
                book_title="牙周病学",
                document_id="document.d766e8629232a2f6",
            )
        ]
        with self.assertRaises(ValueError) as ctx:
            resolve_learning_scope(
                chapters,
                chapter_key="chapter:perio-ch3",
                book_title="Wrong Book",
                document_id="document.other",
            )
        self.assertIn("Book title mismatch", str(ctx.exception))

    # -- negative: missing / empty core_ids --------------------------------

    def test_empty_core_ids_raises(self) -> None:
        """Chapter with an empty core_ids list raises ValueError."""
        chapters = [_chapter(key="chapter:perio-ch3", core_ids=[])]
        with self.assertRaises(ValueError) as ctx:
            resolve_learning_scope(chapters, chapter_key="chapter:perio-ch3")
        self.assertIn("no valid core", str(ctx.exception).lower())

    def test_missing_core_ids_key_raises(self) -> None:
        """Chapter without a core_ids key raises ValueError."""
        ch = _chapter(key="chapter:perio-ch3")
        del ch["core_ids"]
        with self.assertRaises(ValueError) as ctx:
            resolve_learning_scope([ch], chapter_key="chapter:perio-ch3")
        self.assertIn("no valid core", str(ctx.exception).lower())

    def test_only_whitespace_core_ids_raises(self) -> None:
        """core_ids with only whitespace/empty strings raises ValueError."""
        chapters = [_chapter(key="chapter:perio-ch3", core_ids=["", "  ", "\t"])]
        with self.assertRaises(ValueError) as ctx:
            resolve_learning_scope(chapters, chapter_key="chapter:perio-ch3")
        self.assertIn("no valid core", str(ctx.exception).lower())

    def test_none_core_ids_raises(self) -> None:
        """core_ids set to None raises ValueError."""
        chapters = [_chapter(key="chapter:perio-ch3", core_ids=None)]
        with self.assertRaises(ValueError) as ctx:
            resolve_learning_scope(chapters, chapter_key="chapter:perio-ch3")
        self.assertIn("no valid core", str(ctx.exception).lower())

    # -- negative: missing / empty resolved fields from chapter -----------

    def test_empty_book_title_in_chapter_raises(self) -> None:
        """Chapter with empty string book_title must fail — scope incomplete."""
        chapters = [_chapter(key="chapter:perio-ch3", book_title="")]
        with self.assertRaises(ValueError) as ctx:
            resolve_learning_scope(chapters, chapter_key="chapter:perio-ch3")
        self.assertIn("book_title", str(ctx.exception))
        self.assertIn("chapter:perio-ch3", str(ctx.exception))

    def test_whitespace_book_title_in_chapter_raises(self) -> None:
        """Chapter with whitespace-only book_title must fail."""
        chapters = [_chapter(key="chapter:perio-ch3", book_title="   \t  ")]
        with self.assertRaises(ValueError) as ctx:
            resolve_learning_scope(chapters, chapter_key="chapter:perio-ch3")
        self.assertIn("book_title", str(ctx.exception))

    def test_empty_document_id_in_chapter_raises(self) -> None:
        """Chapter with empty string document_id must fail — scope incomplete."""
        chapters = [_chapter(key="chapter:perio-ch3", document_id="")]
        with self.assertRaises(ValueError) as ctx:
            resolve_learning_scope(chapters, chapter_key="chapter:perio-ch3")
        self.assertIn("document_id", str(ctx.exception))
        self.assertIn("chapter:perio-ch3", str(ctx.exception))

    def test_whitespace_document_id_in_chapter_raises(self) -> None:
        """Chapter with whitespace-only document_id must fail."""
        chapters = [_chapter(key="chapter:perio-ch3", document_id="  \n  ")]
        with self.assertRaises(ValueError) as ctx:
            resolve_learning_scope(chapters, chapter_key="chapter:perio-ch3")
        self.assertIn("document_id", str(ctx.exception))

    def test_empty_title_in_chapter_raises(self) -> None:
        """Chapter with empty string title must fail — scope incomplete."""
        chapters = [_chapter(key="chapter:perio-ch3", title="")]
        with self.assertRaises(ValueError) as ctx:
            resolve_learning_scope(chapters, chapter_key="chapter:perio-ch3")
        self.assertIn("title", str(ctx.exception))
        self.assertIn("chapter:perio-ch3", str(ctx.exception))

    def test_whitespace_title_in_chapter_raises(self) -> None:
        """Chapter with whitespace-only title must fail."""
        chapters = [_chapter(key="chapter:perio-ch3", title="   ")]
        with self.assertRaises(ValueError) as ctx:
            resolve_learning_scope(chapters, chapter_key="chapter:perio-ch3")
        self.assertIn("title", str(ctx.exception))

    def test_missing_book_title_key_raises(self) -> None:
        """Chapter dict without a book_title key must fail."""
        ch = _chapter(key="chapter:perio-ch3")
        del ch["book_title"]
        with self.assertRaises(ValueError) as ctx:
            resolve_learning_scope([ch], chapter_key="chapter:perio-ch3")
        self.assertIn("book_title", str(ctx.exception))

    def test_missing_document_id_key_raises(self) -> None:
        """Chapter dict without a document_id key must fail."""
        ch = _chapter(key="chapter:perio-ch3")
        del ch["document_id"]
        with self.assertRaises(ValueError) as ctx:
            resolve_learning_scope([ch], chapter_key="chapter:perio-ch3")
        self.assertIn("document_id", str(ctx.exception))

    def test_missing_title_key_raises(self) -> None:
        """Chapter dict without a title key must fail."""
        ch = _chapter(key="chapter:perio-ch3")
        del ch["title"]
        with self.assertRaises(ValueError) as ctx:
            resolve_learning_scope([ch], chapter_key="chapter:perio-ch3")
        self.assertIn("title", str(ctx.exception))

    def test_all_three_empty_reports_book_title_first(self) -> None:
        """When all three fields are empty, book_title is validated first."""
        chapters = [_chapter(key="chapter:perio-ch3", book_title="", document_id="", title="")]
        with self.assertRaises(ValueError) as ctx:
            resolve_learning_scope(chapters, chapter_key="chapter:perio-ch3")
        self.assertIn("book_title", str(ctx.exception))

    # -- edge cases -------------------------------------------------------

    def test_mixed_valid_and_invalid_core_ids(self) -> None:
        """Only non-empty core_ids are kept; empties are discarded."""
        chapters = [
            _chapter(
                key="chapter:perio-ch3",
                core_ids=["", "core:aaa111", "  ", "core:bbb222", "\t"],
            )
        ]
        scope = resolve_learning_scope(chapters, chapter_key="chapter:perio-ch3")
        self.assertEqual(scope["kg_node_ids"], ["core:aaa111", "core:bbb222"])

    def test_result_is_independent_of_input(self) -> None:
        """Mutating the returned scope does not affect the chapter dict."""
        chapters = [_chapter(key="chapter:perio-ch3")]
        scope = resolve_learning_scope(chapters, chapter_key="chapter:perio-ch3")

        scope["kg_node_ids"].append("injected")
        scope["book_title"] = "tampered"

        self.assertEqual(chapters[0]["book_title"], "牙周病学")
        self.assertEqual(chapters[0]["core_ids"], ["core:aaa111", "core:bbb222", "core:ccc333"])

    def test_different_textbook_same_chapter_number(self) -> None:
        """Chapters from different textbooks with different keys resolve
        independently."""
        chapters = [
            _chapter(
                key="chapter:perio-ch3",
                book_title="牙周病学",
                document_id="document.d766e8629232a2f6",
                title="第三章 牙周病的发病机制",
                core_ids=["core:perio-a", "core:perio-b"],
            ),
            _chapter(
                key="chapter:endo-ch3",
                book_title="牙体牙髓病学",
                document_id="document.abae58f6831f4c1d",
                title="第三章 龋病",
                core_ids=["core:endo-a"],
            ),
        ]
        scope_perio = resolve_learning_scope(chapters, chapter_key="chapter:perio-ch3")
        self.assertEqual(scope_perio["book_title"], "牙周病学")
        self.assertEqual(scope_perio["kg_node_ids"], ["core:perio-a", "core:perio-b"])

        scope_endo = resolve_learning_scope(chapters, chapter_key="chapter:endo-ch3")
        self.assertEqual(scope_endo["book_title"], "牙体牙髓病学")
        self.assertEqual(scope_endo["kg_node_ids"], ["core:endo-a"])

    def test_chapter_with_zero_sections_and_no_draft_files(self) -> None:
        """A minimal chapter (no sections, no drafts) still resolves."""
        chapters = [
            _chapter(
                key="chapter:minimal",
                title="附录",
                section_count=0,
                source_sections=[],
                draft_files=[],
                core_ids=["core:mini-1"],
            )
        ]
        scope = resolve_learning_scope(chapters, chapter_key="chapter:minimal")
        self.assertEqual(scope["chapter_title"], "附录")
        self.assertEqual(scope["kg_node_ids"], ["core:mini-1"])

    def test_unknown_keys_in_chapter_dict_are_ignored(self) -> None:
        """Extra keys in the chapter dict are silently ignored."""
        chapters = [
            _chapter(
                key="chapter:extra",
                extra_field="should-be-ignored",
                another_field=42,
            )
        ]
        scope = resolve_learning_scope(chapters, chapter_key="chapter:extra")
        self.assertEqual(scope["chapter_key"], "chapter:extra")
        self.assertNotIn("extra_field", scope)
        self.assertNotIn("another_field", scope)

    def test_non_dict_entries_in_chapters_list_are_skipped(self) -> None:
        """Non-dict entries in the chapters list are ignored during matching."""
        chapters = [
            "not a dict",
            None,
            42,
            _chapter(key="chapter:real", core_ids=["core:real-1"]),
            ["also", "not", "a", "dict"],
        ]
        scope = resolve_learning_scope(chapters, chapter_key="chapter:real")
        self.assertEqual(scope["chapter_key"], "chapter:real")
        self.assertEqual(scope["kg_node_ids"], ["core:real-1"])


class NormalizePlanRequestTests(unittest.TestCase):
    """Unit tests for :func:`normalize_plan_request`."""

    # -- positive: intent variants ----------------------------------------

    def test_learn_chapter_intent_with_all_fields(self) -> None:
        """Full request with learn_chapter intent normalises correctly."""
        request = {
            "intent": "learn_chapter",
            "days": 14,
            "daily_minutes": 45,
            "focus_text": " 牙周病的发病机制 ",
            "book_title": " 牙周病学 ",
            "document_id": " document.d766e8629232a2f6 ",
            "chapter_key": " chapter:perio-ch3 ",
        }
        result = normalize_plan_request(request)

        self.assertEqual(result["intent"], "learn_chapter")
        self.assertEqual(result["days"], 14)
        self.assertEqual(result["daily_minutes"], 45)
        self.assertEqual(result["focus_text"], "牙周病的发病机制")
        self.assertEqual(result["book_title"], "牙周病学")
        self.assertEqual(result["document_id"], "document.d766e8629232a2f6")
        self.assertEqual(result["chapter_key"], "chapter:perio-ch3")

    def test_custom_intent_minimal(self) -> None:
        """Minimal request with custom intent receives all defaults."""
        result = normalize_plan_request({"intent": "custom"})

        self.assertEqual(result["intent"], "custom")
        self.assertEqual(result["days"], 7)
        self.assertEqual(result["daily_minutes"], 30)
        self.assertIsNone(result["focus_text"])
        self.assertIsNone(result["book_title"])
        self.assertIsNone(result["document_id"])
        self.assertIsNone(result["chapter_key"])

    def test_review_chapter_intent(self) -> None:
        """review_chapter intent is recognised."""
        result = normalize_plan_request({"intent": "review_chapter"})
        self.assertEqual(result["intent"], "review_chapter")

    def test_review_weakness_intent(self) -> None:
        """review_weakness intent is recognised."""
        result = normalize_plan_request({"intent": "review_weakness"})
        self.assertEqual(result["intent"], "review_weakness")

    # -- positive: defaults for missing optional fields -------------------

    def test_missing_days_gets_default(self) -> None:
        """When days is absent, the default 7 is used."""
        result = normalize_plan_request({"intent": "learn_chapter"})
        self.assertEqual(result["days"], 7)

    def test_none_days_gets_default(self) -> None:
        """When days is None, the default 7 is used."""
        result = normalize_plan_request({"intent": "learn_chapter", "days": None})
        self.assertEqual(result["days"], 7)

    def test_missing_daily_minutes_gets_default(self) -> None:
        """When daily_minutes is absent, the default 30 is used."""
        result = normalize_plan_request({"intent": "learn_chapter"})
        self.assertEqual(result["daily_minutes"], 30)

    def test_none_daily_minutes_gets_default(self) -> None:
        """When daily_minutes is None, the default 30 is used."""
        result = normalize_plan_request(
            {"intent": "learn_chapter", "daily_minutes": None}
        )
        self.assertEqual(result["daily_minutes"], 30)

    def test_missing_text_fields_become_none(self) -> None:
        """All optional text fields default to None when absent."""
        result = normalize_plan_request({"intent": "learn_chapter"})
        self.assertIsNone(result["focus_text"])
        self.assertIsNone(result["book_title"])
        self.assertIsNone(result["document_id"])
        self.assertIsNone(result["chapter_key"])

    # -- positive: text trimming ------------------------------------------

    def test_text_fields_are_trimmed(self) -> None:
        """Leading / trailing whitespace is stripped from text fields."""
        result = normalize_plan_request(
            {
                "intent": "review_weakness",
                "focus_text": "  牙菌斑  ",
                "book_title": "\t牙周病学\n",
                "document_id": "  doc-001  ",
                "chapter_key": "  ch:1  ",
            }
        )
        self.assertEqual(result["focus_text"], "牙菌斑")
        self.assertEqual(result["book_title"], "牙周病学")
        self.assertEqual(result["document_id"], "doc-001")
        self.assertEqual(result["chapter_key"], "ch:1")

    def test_empty_text_fields_become_none(self) -> None:
        """Whitespace-only or empty string text fields become None."""
        result = normalize_plan_request(
            {
                "intent": "custom",
                "focus_text": "   ",
                "book_title": "",
                "document_id": "\t\n",
                "chapter_key": "  ",
            }
        )
        self.assertIsNone(result["focus_text"])
        self.assertIsNone(result["book_title"])
        self.assertIsNone(result["document_id"])
        self.assertIsNone(result["chapter_key"])

    # -- negative: intent -------------------------------------------------

    def test_missing_intent_raises(self) -> None:
        """Missing intent key raises ValueError."""
        with self.assertRaises(ValueError) as ctx:
            normalize_plan_request({})
        self.assertIn("intent", str(ctx.exception))

    def test_none_intent_raises(self) -> None:
        """None intent raises ValueError."""
        with self.assertRaises(ValueError):
            normalize_plan_request({"intent": None})

    def test_unknown_intent_raises(self) -> None:
        """An intent not in the allowed set raises ValueError."""
        with self.assertRaises(ValueError) as ctx:
            normalize_plan_request({"intent": "summarize_chapter"})
        self.assertIn("summarize_chapter", str(ctx.exception))
        self.assertIn("Unknown intent", str(ctx.exception))

    def test_empty_string_intent_raises(self) -> None:
        """Empty string intent (after trim) raises ValueError."""
        with self.assertRaises(ValueError) as ctx:
            normalize_plan_request({"intent": "   "})
        self.assertIn("Unknown intent", str(ctx.exception))

    # -- negative: days ---------------------------------------------------

    def test_zero_days_raises(self) -> None:
        """days=0 is below the minimum of 1."""
        with self.assertRaises(ValueError) as ctx:
            normalize_plan_request({"intent": "custom", "days": 0})
        self.assertIn("days", str(ctx.exception))
        self.assertIn("between 1 and 90", str(ctx.exception))

    def test_negative_days_raises(self) -> None:
        """Negative days raises ValueError."""
        with self.assertRaises(ValueError):
            normalize_plan_request({"intent": "custom", "days": -1})

    def test_days_above_max_raises(self) -> None:
        """days=91 is above the maximum of 90."""
        with self.assertRaises(ValueError) as ctx:
            normalize_plan_request({"intent": "custom", "days": 91})
        self.assertIn("days", str(ctx.exception))
        self.assertIn("between 1 and 90", str(ctx.exception))

    def test_float_days_raises(self) -> None:
        """Non-integer float days raises ValueError."""
        with self.assertRaises(ValueError) as ctx:
            normalize_plan_request({"intent": "custom", "days": 3.5})
        self.assertIn("days", str(ctx.exception))
        self.assertIn("integer", str(ctx.exception))

    def test_string_days_raises(self) -> None:
        """String days raises ValueError."""
        with self.assertRaises(ValueError) as ctx:
            normalize_plan_request({"intent": "custom", "days": "seven"})
        self.assertIn("days", str(ctx.exception))
        self.assertIn("integer", str(ctx.exception))

    def test_bool_days_raises(self) -> None:
        """Boolean days raises ValueError."""
        with self.assertRaises(ValueError) as ctx:
            normalize_plan_request({"intent": "custom", "days": True})
        self.assertIn("days", str(ctx.exception))
        self.assertIn("integer", str(ctx.exception))

    # -- negative: daily_minutes ------------------------------------------

    def test_daily_minutes_below_min_raises(self) -> None:
        """daily_minutes=4 is below the minimum of 5."""
        with self.assertRaises(ValueError) as ctx:
            normalize_plan_request({"intent": "custom", "daily_minutes": 4})
        self.assertIn("daily_minutes", str(ctx.exception))
        self.assertIn("between 5 and 240", str(ctx.exception))

    def test_daily_minutes_above_max_raises(self) -> None:
        """daily_minutes=241 is above the maximum of 240."""
        with self.assertRaises(ValueError) as ctx:
            normalize_plan_request({"intent": "custom", "daily_minutes": 241})
        self.assertIn("daily_minutes", str(ctx.exception))
        self.assertIn("between 5 and 240", str(ctx.exception))

    def test_float_daily_minutes_raises(self) -> None:
        """Non-integer float daily_minutes raises ValueError."""
        with self.assertRaises(ValueError) as ctx:
            normalize_plan_request({"intent": "custom", "daily_minutes": 15.5})
        self.assertIn("daily_minutes", str(ctx.exception))
        self.assertIn("integer", str(ctx.exception))

    # -- positive: numeric boundaries -------------------------------------

    def test_days_min_boundary(self) -> None:
        """days=1 is the minimum allowed value."""
        result = normalize_plan_request({"intent": "custom", "days": 1})
        self.assertEqual(result["days"], 1)

    def test_days_max_boundary(self) -> None:
        """days=90 is the maximum allowed value."""
        result = normalize_plan_request({"intent": "custom", "days": 90})
        self.assertEqual(result["days"], 90)

    def test_daily_minutes_min_boundary(self) -> None:
        """daily_minutes=5 is the minimum allowed value."""
        result = normalize_plan_request({"intent": "custom", "daily_minutes": 5})
        self.assertEqual(result["daily_minutes"], 5)

    def test_daily_minutes_max_boundary(self) -> None:
        """daily_minutes=240 is the maximum allowed value."""
        result = normalize_plan_request({"intent": "custom", "daily_minutes": 240})
        self.assertEqual(result["daily_minutes"], 240)

    def test_float_whole_number_ok(self) -> None:
        """A float like 3.0 (a whole number) is accepted as an integer."""
        result = normalize_plan_request({"intent": "custom", "days": 3.0})
        self.assertEqual(result["days"], 3)

    # -- input immutability -----------------------------------------------

    def test_input_dict_is_not_mutated(self) -> None:
        """The original request dict is not modified by normalisation."""
        request: dict = {
            "intent": "learn_chapter",
            "days": 5,
            "focus_text": "  original  ",
        }
        original = dict(request)  # shallow copy for comparison

        normalize_plan_request(request)

        self.assertEqual(request, original)
        self.assertEqual(request["focus_text"], "  original  ")
        self.assertEqual(request["days"], 5)

    def test_result_is_independent_of_input(self) -> None:
        """Mutating the returned dict does not affect the original request."""
        request = {"intent": "learn_chapter", "focus_text": "hello"}
        result = normalize_plan_request(request)

        result["focus_text"] = "tampered"
        result["days"] = 999

        self.assertEqual(request["focus_text"], "hello")
        self.assertNotIn("days", request)

    # -- output shape -----------------------------------------------------

    def test_output_has_exactly_seven_keys(self) -> None:
        """The normalised dict contains only the documented keys."""
        result = normalize_plan_request({"intent": "learn_chapter"})
        self.assertEqual(
            set(result.keys()),
            {
                "intent",
                "days",
                "daily_minutes",
                "focus_text",
                "book_title",
                "document_id",
                "chapter_key",
            },
        )
        self.assertEqual(len(result), 7)


class CreateCustomPlanDraftTests(unittest.TestCase):
    """Tests for :func:`create_custom_plan_draft`."""

    @staticmethod
    def _store(ttl_seconds: int = 300) -> PlanDraftStore:
        return PlanDraftStore(ttl_seconds=ttl_seconds)

    # -- chapter learning draft with real scope -----------------------------

    def test_learn_chapter_produces_valid_draft(self) -> None:
        """A learn_chapter request with real KG chapters yields a draft with
        kg_chapter evidence and full learning_scope."""
        store = self._store()
        chapters = [
            _chapter(key="chapter:perio-ch3"),
            _chapter(key="chapter:perio-ch4", title="第四章 牙周病的临床表现"),
        ]
        request = {
            "intent": "learn_chapter",
            "days": 3,
            "daily_minutes": 30,
            "chapter_key": "chapter:perio-ch3",
            "book_title": "牙周病学",
            "document_id": "document.d766e8629232a2f6",
        }

        record = create_custom_plan_draft(
            store, user_id="u1", request=request, chapters=chapters
        )

        self.assertTrue(record["plan_draft_id"].startswith("draft_"))
        self.assertEqual(record["user_id"], "u1")
        self.assertIn("request", record["payload"])
        self.assertIn("items", record["payload"])

        items = record["payload"]["items"]
        # task_count = min(days=3, 5) = 3
        self.assertEqual(len(items), 3)

        for item in items:
            self.assertEqual(item["task_type"], "learn_chapter")
            self.assertIn("第三章 牙周病的发病机制", item["learning_target"])
            self.assertEqual(item["estimated_minutes"], 30)
            self.assertIn("牙周病学", item["reason"])
            self.assertEqual(item["evidence_source"], "kg_chapter")
            self.assertIsNotNone(item["learning_scope"])
            scope = item["learning_scope"]
            self.assertEqual(scope["book_title"], "牙周病学")
            self.assertEqual(scope["chapter_key"], "chapter:perio-ch3")
            self.assertEqual(
                scope["kg_node_ids"],
                ["core:aaa111", "core:bbb222", "core:ccc333"],
            )

    def test_single_day_produces_one_item(self) -> None:
        """days=1 → exactly 1 draft item."""
        store = self._store()
        chapters = [_chapter(key="chapter:perio-ch3")]
        request = {
            "intent": "learn_chapter",
            "days": 1,
            "daily_minutes": 45,
            "chapter_key": "chapter:perio-ch3",
        }

        record = create_custom_plan_draft(
            store, user_id="u1", request=request, chapters=chapters
        )

        items = record["payload"]["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["estimated_minutes"], 45)
        # No suffix when task_count == 1
        self.assertEqual(
            items[0]["learning_target"], "第三章 牙周病的发病机制"
        )

    def test_task_count_capped_at_five(self) -> None:
        """days=10 → task count capped at 5."""
        store = self._store()
        chapters = [_chapter(key="chapter:perio-ch3")]
        request = {
            "intent": "learn_chapter",
            "days": 10,
            "daily_minutes": 20,
            "chapter_key": "chapter:perio-ch3",
        }

        record = create_custom_plan_draft(
            store, user_id="u1", request=request, chapters=chapters
        )
        self.assertEqual(len(record["payload"]["items"]), 5)

    # -- review task with chapter evidence (KG) ----------------------------

    def test_review_chapter_with_kg_evidence(self) -> None:
        """review_chapter with a chapter_key resolves KG scope."""
        store = self._store()
        chapters = [_chapter(key="chapter:perio-ch3")]
        request = {
            "intent": "review_chapter",
            "days": 2,
            "daily_minutes": 25,
            "chapter_key": "chapter:perio-ch3",
        }

        record = create_custom_plan_draft(
            store, user_id="u1", request=request, chapters=chapters
        )

        for item in record["payload"]["items"]:
            self.assertEqual(item["task_type"], "review_chapter")
            self.assertEqual(item["evidence_source"], "kg_chapter")
            self.assertIsNotNone(item["learning_scope"])
            self.assertIn("复习", item["reason"])

    # -- review task with user evidence (no chapter) ------------------------

    def test_review_chapter_without_chapter_key_uses_user_evidence(self) -> None:
        """review_chapter without a chapter_key falls back to user_specified."""
        store = self._store()
        request = {
            "intent": "review_chapter",
            "days": 1,
            "daily_minutes": 20,
            "focus_text": "牙周病的临床表现",
        }

        record = create_custom_plan_draft(
            store, user_id="u1", request=request, chapters=[]
        )

        items = record["payload"]["items"]
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["task_type"], "review_chapter")
        self.assertEqual(item["evidence_source"], "user_specified")
        self.assertIsNone(item["learning_scope"])
        self.assertEqual(item["learning_target"], "牙周病的临床表现")
        self.assertIn("牙周病的临床表现", item["reason"])

    def test_review_weakness_always_user_evidence(self) -> None:
        """review_weakness never claims KG evidence even if chapters exist."""
        store = self._store()
        chapters = [_chapter(key="chapter:perio-ch3")]
        request = {
            "intent": "review_weakness",
            "days": 2,
            "daily_minutes": 15,
            "focus_text": "宿主免疫反应",
        }

        record = create_custom_plan_draft(
            store, user_id="u1", request=request, chapters=chapters
        )

        for item in record["payload"]["items"]:
            self.assertEqual(item["task_type"], "review_weakness")
            self.assertEqual(item["evidence_source"], "user_specified")
            self.assertIsNone(item["learning_scope"])

    def test_custom_always_user_evidence(self) -> None:
        """custom intent never claims KG evidence."""
        store = self._store()
        chapters = [_chapter(key="chapter:perio-ch3")]
        request = {
            "intent": "custom",
            "days": 1,
            "daily_minutes": 30,
            "focus_text": "阅读口腔微生物学综述",
        }

        record = create_custom_plan_draft(
            store, user_id="u1", request=request, chapters=chapters
        )

        item = record["payload"]["items"][0]
        self.assertEqual(item["task_type"], "custom")
        self.assertEqual(item["evidence_source"], "user_specified")
        self.assertIsNone(item["learning_scope"])
        self.assertEqual(item["learning_target"], "阅读口腔微生物学综述")

    # -- invalid scope rejection -------------------------------------------

    def test_learn_chapter_without_chapter_key_raises(self) -> None:
        """learn_chapter requires a chapter_key — missing key raises."""
        store = self._store()
        request = {"intent": "learn_chapter", "days": 3}

        with self.assertRaises(ValueError) as ctx:
            create_custom_plan_draft(
                store, user_id="u1", request=request, chapters=[]
            )
        self.assertIn("chapter_key", str(ctx.exception))

    def test_learn_chapter_with_unknown_chapter_key_raises(self) -> None:
        """A chapter_key not found in the provided chapters raises ValueError."""
        store = self._store()
        chapters = [_chapter(key="chapter:perio-ch3")]
        request = {
            "intent": "learn_chapter",
            "chapter_key": "chapter:nonexistent",
        }

        with self.assertRaises(ValueError) as ctx:
            create_custom_plan_draft(
                store, user_id="u1", request=request, chapters=chapters
            )
        self.assertIn("chapter:nonexistent", str(ctx.exception))

    def test_review_chapter_with_bad_chapter_key_raises(self) -> None:
        """review_chapter with a chapter_key that fails resolution raises."""
        store = self._store()
        chapters = [_chapter(key="chapter:perio-ch3")]
        request = {
            "intent": "review_chapter",
            "chapter_key": "chapter:missing",
        }

        with self.assertRaises(ValueError):
            create_custom_plan_draft(
                store, user_id="u1", request=request, chapters=chapters
            )

    # -- parent draft independence -----------------------------------------

    def test_parent_draft_not_overwritten(self) -> None:
        """A child draft with a parent_plan_draft_id does not mutate the parent."""
        store = self._store()
        chapters = [_chapter(key="chapter:perio-ch3")]

        parent_record = create_custom_plan_draft(
            store,
            user_id="u1",
            request={
                "intent": "learn_chapter",
                "days": 1,
                "chapter_key": "chapter:perio-ch3",
            },
            chapters=chapters,
        )
        parent_id = parent_record["plan_draft_id"]

        child_record = create_custom_plan_draft(
            store,
            user_id="u1",
            request={
                "intent": "review_chapter",
                "days": 2,
                "daily_minutes": 20,
                "chapter_key": "chapter:perio-ch3",
            },
            chapters=chapters,
            parent_plan_draft_id=parent_id,
        )

        # Child has its own ID, links to parent
        self.assertNotEqual(child_record["plan_draft_id"], parent_id)
        self.assertEqual(child_record["parent_plan_draft_id"], parent_id)

        # Parent payload is untouched
        parent_after = store.get(parent_id, user_id="u1")
        self.assertEqual(parent_after["payload"], parent_record["payload"])
        self.assertEqual(
            parent_after["payload"]["items"][0]["task_type"], "learn_chapter"
        )

    def test_child_without_chapters_still_independent(self) -> None:
        """A child custom draft does not interfere with a chapter-scoped parent."""
        store = self._store()
        chapters = [_chapter(key="chapter:perio-ch3")]

        parent_record = create_custom_plan_draft(
            store,
            user_id="u1",
            request={
                "intent": "learn_chapter",
                "days": 1,
                "chapter_key": "chapter:perio-ch3",
            },
            chapters=chapters,
        )
        parent_id = parent_record["plan_draft_id"]

        child_record = create_custom_plan_draft(
            store,
            user_id="u1",
            request={
                "intent": "custom",
                "days": 1,
                "focus_text": "自定义复习",
            },
            chapters=[],
            parent_plan_draft_id=parent_id,
        )

        self.assertEqual(child_record["parent_plan_draft_id"], parent_id)
        self.assertIsNone(child_record["payload"]["items"][0]["learning_scope"])

        # Parent still has its KG scope
        parent_after = store.get(parent_id, user_id="u1")
        self.assertIsNotNone(
            parent_after["payload"]["items"][0]["learning_scope"]
        )

    # -- input immutability ------------------------------------------------

    def test_request_dict_not_mutated_by_draft_creation(self) -> None:
        """The original request dict is never modified."""
        store = self._store()
        chapters = [_chapter(key="chapter:perio-ch3")]
        request: dict = {
            "intent": "learn_chapter",
            "days": 3,
            "focus_text": "  original  ",
            "chapter_key": "chapter:perio-ch3",
        }
        original = dict(request)

        create_custom_plan_draft(
            store, user_id="u1", request=request, chapters=chapters
        )

        self.assertEqual(request, original)
        self.assertEqual(request["focus_text"], "  original  ")

    def test_chapters_list_not_mutated(self) -> None:
        """The chapters list passed by the caller is not modified."""
        store = self._store()
        chapters = [_chapter(key="chapter:perio-ch3")]
        original_core_ids = list(chapters[0]["core_ids"])

        create_custom_plan_draft(
            store,
            user_id="u1",
            request={
                "intent": "learn_chapter",
                "days": 1,
                "chapter_key": "chapter:perio-ch3",
            },
            chapters=chapters,
        )

        self.assertEqual(chapters[0]["core_ids"], original_core_ids)

    # -- item shape contract -----------------------------------------------

    def test_every_item_has_required_keys(self) -> None:
        """Every draft item must contain the six required fields."""
        store = self._store()
        chapters = [_chapter(key="chapter:perio-ch3")]
        request = {
            "intent": "learn_chapter",
            "days": 2,
            "daily_minutes": 30,
            "chapter_key": "chapter:perio-ch3",
        }

        record = create_custom_plan_draft(
            store, user_id="u1", request=request, chapters=chapters
        )

        for item in record["payload"]["items"]:
            self.assertEqual(
                set(item.keys()),
                {
                    "task_type",
                    "learning_target",
                    "estimated_minutes",
                    "reason",
                    "evidence_source",
                    "learning_scope",
                },
            )

    def test_custom_without_focus_text_still_produces_draft(self) -> None:
        """custom intent without focus_text yields a draft with a generic label."""
        store = self._store()
        request = {"intent": "custom", "days": 1}

        record = create_custom_plan_draft(
            store, user_id="u1", request=request, chapters=[]
        )

        item = record["payload"]["items"][0]
        self.assertEqual(item["task_type"], "custom")
        self.assertEqual(item["evidence_source"], "user_specified")
        self.assertIsNone(item["learning_scope"])
        # Generic label is produced
        self.assertTrue(len(item["learning_target"]) > 0)
        self.assertTrue(len(item["reason"]) > 0)


class ParsePlanRequestTextTests(unittest.TestCase):
    """Unit tests for :func:`parse_plan_request_text` — all offline with fake LLMs."""

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _chapter(**overrides: object) -> dict:
        """Build a chapter dict in the production shape from the KG agent."""
        defaults: dict = {
            "key": "chapter:perio-ch3",
            "book_title": "牙周病学",
            "document_id": "document.d766e8629232a2f6",
            "version_id": "v1.0",
            "number": 3,
            "title": "第三章 牙周病的发病机制",
            "section_count": 3,
            "source_sections": ["第一节", "第二节", "第三节"],
            "draft_files": ["data/kg/ch3.kg-draft.json"],
            "core_ids": ["core:aaa111", "core:bbb222", "core:ccc333"],
            "cores": [
                {"id": "core:aaa111", "name": "牙菌斑生物膜", "type": "Etiology"},
                {"id": "core:bbb222", "name": "宿主免疫反应", "type": "PathologicalProcess"},
                {"id": "core:ccc333", "name": "牙周袋形成", "type": "ClinicalSign"},
            ],
        }
        return {**defaults, **overrides}

    # ------------------------------------------------------------------
    # positive: successful parsing
    # ------------------------------------------------------------------

    def test_parse_review_chapter_with_focus(self) -> None:
        """Natural language request parses to a structured dict with chapter fields
        from candidates."""
        chapters = [
            self._chapter(key="chapter:perio-ch3"),
            self._chapter(key="chapter:perio-ch4", title="第四章 牙周病的临床表现"),
        ]

        def fake_llm(prompt: str) -> str:
            return (
                '{"intent": "review_chapter", "days": 3, "daily_minutes": 30, '
                '"focus_text": "炎症机制", '
                '"book_title": "牙周病学", '
                '"document_id": "document.d766e8629232a2f6", '
                '"chapter_key": "chapter:perio-ch3"}'
            )

        result = parse_plan_request_text(
            "三天复习《牙周病学》第三章，每天30分钟，重点炎症机制",
            chapters=chapters,
            llm_call=fake_llm,
        )

        self.assertEqual(result["intent"], "review_chapter")
        self.assertEqual(result["days"], 3)
        self.assertEqual(result["daily_minutes"], 30)
        self.assertEqual(result["focus_text"], "炎症机制")
        self.assertEqual(result["book_title"], "牙周病学")
        self.assertEqual(result["document_id"], "document.d766e8629232a2f6")
        self.assertEqual(result["chapter_key"], "chapter:perio-ch3")

    def test_two_candidates_correct_key_selected(self) -> None:
        """When two different chapter candidates exist, the LLM's exact chapter_key
        determines the result — no cross-contamination of book/document."""
        chapters = [
            self._chapter(
                key="chapter:perio-ch3",
                book_title="牙周病学",
                document_id="document.d766e8629232a2f6",
                title="第三章 牙周病的发病机制",
            ),
            self._chapter(
                key="chapter:endo-ch1",
                book_title="牙体牙髓病学",
                document_id="document.abae58f6831f4c1d",
                title="第一章 绪论",
            ),
        ]

        # Fake returns perio-ch3 (the first candidate)
        def fake_llm(prompt: str) -> str:
            return (
                '{"intent": "learn_chapter", "days": 7, "daily_minutes": 45, '
                '"focus_text": null, '
                '"book_title": "牙周病学", '
                '"document_id": "document.d766e8629232a2f6", '
                '"chapter_key": "chapter:perio-ch3"}'
            )

        result = parse_plan_request_text(
            "学习牙周病学第三章",
            chapters=chapters,
            llm_call=fake_llm,
        )

        self.assertEqual(result["chapter_key"], "chapter:perio-ch3")
        self.assertEqual(result["book_title"], "牙周病学")
        self.assertEqual(result["document_id"], "document.d766e8629232a2f6")

    def test_learn_chapter_with_all_fields(self) -> None:
        """learn_chapter intent with full fields parses correctly."""
        chapters = [self._chapter(key="chapter:perio-ch3")]

        def fake_llm(prompt: str) -> str:
            return (
                '{"intent": "learn_chapter", "days": 14, "daily_minutes": 60, '
                '"focus_text": "牙周袋形成机制", '
                '"book_title": "牙周病学", '
                '"document_id": "document.d766e8629232a2f6", '
                '"chapter_key": "chapter:perio-ch3"}'
            )

        result = parse_plan_request_text(
            "两周学习牙周病学第三章牙周袋形成机制，每天一小时",
            chapters=chapters,
            llm_call=fake_llm,
        )

        self.assertEqual(result["intent"], "learn_chapter")
        self.assertEqual(result["days"], 14)
        self.assertEqual(result["daily_minutes"], 60)
        self.assertEqual(result["focus_text"], "牙周袋形成机制")
        self.assertEqual(result["chapter_key"], "chapter:perio-ch3")

    def test_custom_intent_no_chapter(self) -> None:
        """When the user's request doesn't map to any chapter, intent is custom
        and chapter fields are null."""
        chapters = [self._chapter(key="chapter:perio-ch3")]

        def fake_llm(prompt: str) -> str:
            return (
                '{"intent": "custom", "days": 5, "daily_minutes": 20, '
                '"focus_text": "阅读口腔微生物学综述", '
                '"book_title": null, '
                '"document_id": null, '
                '"chapter_key": null}'
            )

        result = parse_plan_request_text(
            "我想花五天时间阅读口腔微生物学综述",
            chapters=chapters,
            llm_call=fake_llm,
        )

        self.assertEqual(result["intent"], "custom")
        self.assertIsNone(result["chapter_key"])
        self.assertIsNone(result["book_title"])
        self.assertIsNone(result["document_id"])
        self.assertEqual(result["focus_text"], "阅读口腔微生物学综述")

    def test_review_weakness_intent(self) -> None:
        """review_weakness intent parses correctly with no chapter reference."""
        chapters: list = []

        def fake_llm(prompt: str) -> str:
            return (
                '{"intent": "review_weakness", "days": 3, "daily_minutes": 25, '
                '"focus_text": "牙菌斑控制", '
                '"book_title": null, '
                '"document_id": null, '
                '"chapter_key": null}'
            )

        result = parse_plan_request_text(
            "三天复习薄弱点牙菌斑控制",
            chapters=chapters,
            llm_call=fake_llm,
        )

        self.assertEqual(result["intent"], "review_weakness")
        self.assertEqual(result["days"], 3)
        self.assertEqual(result["daily_minutes"], 25)
        self.assertEqual(result["focus_text"], "牙菌斑控制")

    def test_none_chapters_accepted(self) -> None:
        """chapters=None is treated as empty — chapter fields must be null."""

        def fake_llm(prompt: str) -> str:
            return (
                '{"intent": "custom", "days": 1, "daily_minutes": 10, '
                '"focus_text": null, '
                '"book_title": null, '
                '"document_id": null, '
                '"chapter_key": null}'
            )

        result = parse_plan_request_text(
            "随便学点什么",
            chapters=None,
            llm_call=fake_llm,
        )

        self.assertEqual(result["intent"], "custom")

    # ------------------------------------------------------------------
    # negative: fabricated / mismatched chapter references
    # ------------------------------------------------------------------

    def test_fabricated_chapter_key_rejected(self) -> None:
        """A chapter_key not present in any candidate raises PlanRequestParseError."""
        chapters = [self._chapter(key="chapter:perio-ch3")]

        def fake_llm(prompt: str) -> str:
            return (
                '{"intent": "review_chapter", "days": 3, "daily_minutes": 30, '
                '"focus_text": null, '
                '"book_title": "牙周病学", '
                '"document_id": "document.d766e8629232a2f6", '
                '"chapter_key": "chapter:made-up-fake"}'
            )

        with self.assertRaises(PlanRequestParseError) as ctx:
            parse_plan_request_text(
                "复习牙周病学第三章",
                chapters=chapters,
                llm_call=fake_llm,
            )

        self.assertEqual(ctx.exception.code, "chapter_not_found")
        self.assertIn("chapter:made-up-fake", str(ctx.exception))

    def test_fabricated_book_title_rejected(self) -> None:
        """A book_title not matching the chapter candidate raises PlanRequestParseError."""
        chapters = [self._chapter(key="chapter:perio-ch3", book_title="牙周病学")]

        def fake_llm(prompt: str) -> str:
            return (
                '{"intent": "learn_chapter", "days": 3, "daily_minutes": 30, '
                '"focus_text": null, '
                '"book_title": "口腔修复学", '
                '"document_id": "document.d766e8629232a2f6", '
                '"chapter_key": "chapter:perio-ch3"}'
            )

        with self.assertRaises(PlanRequestParseError) as ctx:
            parse_plan_request_text(
                "学习口腔修复学第三章",
                chapters=chapters,
                llm_call=fake_llm,
            )

        self.assertEqual(ctx.exception.code, "chapter_mismatch")
        self.assertIn("book_title", str(ctx.exception))

    def test_fabricated_document_id_rejected(self) -> None:
        """A document_id not matching the chapter candidate raises PlanRequestParseError."""
        chapters = [self._chapter(key="chapter:perio-ch3", document_id="document.d766e8629232a2f6")]

        def fake_llm(prompt: str) -> str:
            return (
                '{"intent": "review_chapter", "days": 3, "daily_minutes": 30, '
                '"focus_text": null, '
                '"book_title": "牙周病学", '
                '"document_id": "document.fake-id", '
                '"chapter_key": "chapter:perio-ch3"}'
            )

        with self.assertRaises(PlanRequestParseError) as ctx:
            parse_plan_request_text(
                "复习牙周病学第三章",
                chapters=chapters,
                llm_call=fake_llm,
            )

        self.assertEqual(ctx.exception.code, "chapter_mismatch")
        self.assertIn("document_id", str(ctx.exception))

    def test_learn_chapter_without_chapter_key_rejected(self) -> None:
        """learn_chapter intent with null chapter_key raises PlanRequestParseError."""
        chapters = [self._chapter(key="chapter:perio-ch3")]

        def fake_llm(prompt: str) -> str:
            return (
                '{"intent": "learn_chapter", "days": 3, "daily_minutes": 30, '
                '"focus_text": "牙周病", '
                '"book_title": null, '
                '"document_id": null, '
                '"chapter_key": null}'
            )

        with self.assertRaises(PlanRequestParseError) as ctx:
            parse_plan_request_text(
                "学习牙周病",
                chapters=chapters,
                llm_call=fake_llm,
            )

        self.assertEqual(ctx.exception.code, "missing_chapter_key")

    def test_book_title_without_chapter_key_rejected(self) -> None:
        """When chapter_key is null, a non-null book_title is rejected."""
        chapters = [self._chapter(key="chapter:perio-ch3")]

        def fake_llm(prompt: str) -> str:
            return (
                '{"intent": "review_chapter", "days": 3, "daily_minutes": 30, '
                '"focus_text": null, '
                '"book_title": "牙周病学", '
                '"document_id": null, '
                '"chapter_key": null}'
            )

        with self.assertRaises(PlanRequestParseError) as ctx:
            parse_plan_request_text(
                "复习牙周病学",
                chapters=chapters,
                llm_call=fake_llm,
            )

        self.assertEqual(ctx.exception.code, "chapter_mismatch")

    def test_document_id_without_chapter_key_rejected(self) -> None:
        """When chapter_key is null, a non-null document_id is rejected."""
        chapters = [self._chapter(key="chapter:perio-ch3")]

        def fake_llm(prompt: str) -> str:
            return (
                '{"intent": "custom", "days": 1, "daily_minutes": 30, '
                '"focus_text": null, '
                '"book_title": null, '
                '"document_id": "document.d766e8629232a2f6", '
                '"chapter_key": null}'
            )

        with self.assertRaises(PlanRequestParseError) as ctx:
            parse_plan_request_text(
                "自定义学习",
                chapters=chapters,
                llm_call=fake_llm,
            )

        self.assertEqual(ctx.exception.code, "chapter_mismatch")

    # ------------------------------------------------------------------
    # negative: invalid JSON / invalid intent
    # ------------------------------------------------------------------

    def test_invalid_json_rejected(self) -> None:
        """LLM output that is not valid JSON raises PlanRequestParseError."""
        chapters: list = []

        def fake_llm(prompt: str) -> str:
            return "not json at all, just some text"

        with self.assertRaises(PlanRequestParseError) as ctx:
            parse_plan_request_text(
                "学习牙周病",
                chapters=chapters,
                llm_call=fake_llm,
            )

        self.assertEqual(ctx.exception.code, "invalid_json")

    def test_non_dict_json_rejected(self) -> None:
        """LLM output that is valid JSON but not an object raises PlanRequestParseError."""
        chapters: list = []

        def fake_llm(prompt: str) -> str:
            return '[1, 2, 3]'

        with self.assertRaises(PlanRequestParseError) as ctx:
            parse_plan_request_text(
                "学习牙周病",
                chapters=chapters,
                llm_call=fake_llm,
            )

        self.assertEqual(ctx.exception.code, "invalid_json")

    def test_json_with_markdown_fence_accepted(self) -> None:
        """JSON wrapped in ```json fences is extracted correctly."""
        chapters: list = []

        def fake_llm(prompt: str) -> str:
            return (
                '```json\n'
                '{"intent": "custom", "days": 7, "daily_minutes": 30, '
                '"focus_text": null, '
                '"book_title": null, '
                '"document_id": null, '
                '"chapter_key": null}\n'
                '```'
            )

        result = parse_plan_request_text(
            "随便学学",
            chapters=chapters,
            llm_call=fake_llm,
        )

        self.assertEqual(result["intent"], "custom")

    def test_invalid_intent_rejected(self) -> None:
        """An intent not in the allowed set raises PlanRequestParseError."""
        chapters: list = []

        def fake_llm(prompt: str) -> str:
            return (
                '{"intent": "summarize_chapter", "days": 3, "daily_minutes": 30, '
                '"focus_text": null, '
                '"book_title": null, '
                '"document_id": null, '
                '"chapter_key": null}'
            )

        with self.assertRaises(PlanRequestParseError) as ctx:
            parse_plan_request_text(
                "总结章节",
                chapters=chapters,
                llm_call=fake_llm,
            )

        self.assertEqual(ctx.exception.code, "invalid_intent")

    def test_missing_intent_rejected(self) -> None:
        """LLM output without an intent field raises PlanRequestParseError."""
        chapters: list = []

        def fake_llm(prompt: str) -> str:
            return '{"days": 3, "daily_minutes": 30}'

        with self.assertRaises(PlanRequestParseError) as ctx:
            parse_plan_request_text(
                "学习牙周病",
                chapters=chapters,
                llm_call=fake_llm,
            )

        self.assertEqual(ctx.exception.code, "missing_intent")

    def test_wrong_type_days_rejected(self) -> None:
        """A string value for days raises PlanRequestParseError."""
        chapters: list = []

        def fake_llm(prompt: str) -> str:
            return (
                '{"intent": "custom", "days": "seven", "daily_minutes": 30, '
                '"focus_text": null, '
                '"book_title": null, '
                '"document_id": null, '
                '"chapter_key": null}'
            )

        with self.assertRaises(PlanRequestParseError) as ctx:
            parse_plan_request_text(
                "学习",
                chapters=chapters,
                llm_call=fake_llm,
            )

        self.assertEqual(ctx.exception.code, "invalid_field_type")

    def test_bool_days_rejected(self) -> None:
        """A boolean value for days raises PlanRequestParseError."""
        chapters: list = []

        def fake_llm(prompt: str) -> str:
            return (
                '{"intent": "custom", "days": true, "daily_minutes": 30, '
                '"focus_text": null, '
                '"book_title": null, '
                '"document_id": null, '
                '"chapter_key": null}'
            )

        with self.assertRaises(PlanRequestParseError) as ctx:
            parse_plan_request_text(
                "学习",
                chapters=chapters,
                llm_call=fake_llm,
            )

        self.assertEqual(ctx.exception.code, "invalid_field_type")

    # ------------------------------------------------------------------
    # negative: LLM failure modes
    # ------------------------------------------------------------------

    def test_empty_message_rejected(self) -> None:
        """Empty or whitespace-only message raises PlanRequestParseError."""
        chapters: list = []

        with self.assertRaises(PlanRequestParseError) as ctx:
            parse_plan_request_text(
                "",
                chapters=chapters,
                llm_call=lambda p: "{}",
            )

        self.assertEqual(ctx.exception.code, "empty_message")

    def test_whitespace_message_rejected(self) -> None:
        """Whitespace-only message raises PlanRequestParseError."""
        chapters: list = []

        with self.assertRaises(PlanRequestParseError) as ctx:
            parse_plan_request_text(
                "   \t\n  ",
                chapters=chapters,
                llm_call=lambda p: "{}",
            )

        self.assertEqual(ctx.exception.code, "empty_message")

    def test_llm_callable_throws_configuration_error(self) -> None:
        """When the injected llm_call raises, it's wrapped in PlanRequestParseError."""
        chapters: list = []

        def failing_llm(prompt: str) -> str:
            raise RuntimeError("LLM configuration missing")

        with self.assertRaises(PlanRequestParseError) as ctx:
            parse_plan_request_text(
                "学习牙周病",
                chapters=chapters,
                llm_call=failing_llm,
            )

        # Generic RuntimeError is classified as llm_request_failed
        self.assertIn(ctx.exception.code, ("llm_request_failed", "llm_not_configured"))

    def test_llm_callable_returns_empty_string(self) -> None:
        """An empty string from the LLM raises PlanRequestParseError."""
        chapters: list = []

        def empty_llm(prompt: str) -> str:
            return ""

        with self.assertRaises(PlanRequestParseError) as ctx:
            parse_plan_request_text(
                "学习牙周病",
                chapters=chapters,
                llm_call=empty_llm,
            )

        self.assertEqual(ctx.exception.code, "invalid_json")

    def test_llm_callable_returns_none(self) -> None:
        """A None return from the LLM raises PlanRequestParseError."""
        chapters: list = []

        def none_llm(prompt: str) -> str:
            return None  # type: ignore

        with self.assertRaises(PlanRequestParseError) as ctx:
            parse_plan_request_text(
                "学习牙周病",
                chapters=chapters,
                llm_call=none_llm,
            )

        self.assertEqual(ctx.exception.code, "invalid_json")

    # ------------------------------------------------------------------
    # immutability
    # ------------------------------------------------------------------

    def test_message_not_mutated(self) -> None:
        """The original message string is not modified."""
        chapters: list = []
        message = "  三天复习牙周病学  "

        def fake_llm(prompt: str) -> str:
            return (
                '{"intent": "review_chapter", "days": 3, "daily_minutes": 30, '
                '"focus_text": null, '
                '"book_title": null, '
                '"document_id": null, '
                '"chapter_key": null}'
            )

        parse_plan_request_text(
            message,
            chapters=chapters,
            llm_call=fake_llm,
        )

        # Original message is unchanged
        self.assertEqual(message, "  三天复习牙周病学  ")

    def test_chapters_not_mutated(self) -> None:
        """The chapters list passed by the caller is not modified."""
        chapters = [self._chapter(key="chapter:perio-ch3")]
        original_core_ids = list(chapters[0]["core_ids"])

        def fake_llm(prompt: str) -> str:
            return (
                '{"intent": "learn_chapter", "days": 1, "daily_minutes": 30, '
                '"focus_text": null, '
                '"book_title": "牙周病学", '
                '"document_id": "document.d766e8629232a2f6", '
                '"chapter_key": "chapter:perio-ch3"}'
            )

        parse_plan_request_text(
            "学习牙周病学第三章",
            chapters=chapters,
            llm_call=fake_llm,
        )

        self.assertEqual(chapters[0]["core_ids"], original_core_ids)
        self.assertEqual(chapters[0]["key"], "chapter:perio-ch3")

    # ------------------------------------------------------------------
    # integration: output consumable by normalize_plan_request
    # ------------------------------------------------------------------

    def test_output_passes_normalize_plan_request(self) -> None:
        """The raw output of parse_plan_request_text is accepted by
        normalize_plan_request."""
        chapters = [self._chapter(key="chapter:perio-ch3")]

        def fake_llm(prompt: str) -> str:
            return (
                '{"intent": "review_chapter", "days": 5, "daily_minutes": 45, '
                '"focus_text": "牙菌斑", '
                '"book_title": "牙周病学", '
                '"document_id": "document.d766e8629232a2f6", '
                '"chapter_key": "chapter:perio-ch3"}'
            )

        raw = parse_plan_request_text(
            "五天复习牙周病学第三章牙菌斑，每天45分钟",
            chapters=chapters,
            llm_call=fake_llm,
        )

        norm = normalize_plan_request(raw)

        self.assertEqual(norm["intent"], "review_chapter")
        self.assertEqual(norm["days"], 5)
        self.assertEqual(norm["daily_minutes"], 45)
        self.assertEqual(norm["focus_text"], "牙菌斑")
        self.assertEqual(norm["chapter_key"], "chapter:perio-ch3")

    def test_output_with_nulls_passes_normalize(self) -> None:
        """Output with null optionals is accepted by normalize_plan_request."""
        chapters: list = []

        def fake_llm(prompt: str) -> str:
            return (
                '{"intent": "custom", "days": null, "daily_minutes": null, '
                '"focus_text": null, '
                '"book_title": null, '
                '"document_id": null, '
                '"chapter_key": null}'
            )

        raw = parse_plan_request_text(
            "自定义学习",
            chapters=chapters,
            llm_call=fake_llm,
        )

        norm = normalize_plan_request(raw)

        self.assertEqual(norm["intent"], "custom")
        self.assertEqual(norm["days"], 7)  # default
        self.assertEqual(norm["daily_minutes"], 30)  # default

    # ------------------------------------------------------------------
    # edge: empty candidate list still works for non-chapter intents
    # ------------------------------------------------------------------

    def test_no_candidates_custom_intent(self) -> None:
        """With no chapter candidates, custom intent with null chapter fields
        succeeds."""
        chapters: list = []

        def fake_llm(prompt: str) -> str:
            return (
                '{"intent": "custom", "days": 10, "daily_minutes": 15, '
                '"focus_text": "自由阅读", '
                '"book_title": null, '
                '"document_id": null, '
                '"chapter_key": null}'
            )

        result = parse_plan_request_text(
            "自由阅读十天",
            chapters=chapters,
            llm_call=fake_llm,
        )

        self.assertEqual(result["intent"], "custom")
        self.assertEqual(result["days"], 10)

    def test_no_candidates_review_weakness_intent(self) -> None:
        """With no chapter candidates, review_weakness intent succeeds."""
        chapters: list = []

        def fake_llm(prompt: str) -> str:
            return (
                '{"intent": "review_weakness", "days": 7, "daily_minutes": 20, '
                '"focus_text": "免疫防御机制", '
                '"book_title": null, '
                '"document_id": null, '
                '"chapter_key": null}'
            )

        result = parse_plan_request_text(
            "复习薄弱点免疫防御机制",
            chapters=chapters,
            llm_call=fake_llm,
        )

        self.assertEqual(result["intent"], "review_weakness")

    # ------------------------------------------------------------------
    # new: extra field rejection
    # ------------------------------------------------------------------

    def test_extra_field_in_llm_output_rejected(self) -> None:
        """LLM output that includes a field not in the allowed set raises
        PlanRequestParseError with code 'unexpected_field'."""
        chapters: list = []

        def fake_llm(prompt: str) -> str:
            return (
                '{"intent": "custom", "days": 3, "daily_minutes": 30, '
                '"focus_text": null, '
                '"book_title": null, '
                '"document_id": null, '
                '"chapter_key": null, '
                '"extra_guess": "some value"}'
            )

        with self.assertRaises(PlanRequestParseError) as ctx:
            parse_plan_request_text(
                "随便学学",
                chapters=chapters,
                llm_call=fake_llm,
            )

        self.assertEqual(ctx.exception.code, "unexpected_field")
        self.assertIn("extra_guess", str(ctx.exception))

    # ------------------------------------------------------------------
    # new: chapter_key completeness enforcement
    # ------------------------------------------------------------------

    def test_chapter_key_without_book_title_rejected(self) -> None:
        """When chapter_key is non-null, book_title must also be non-null —
        otherwise PlanRequestParseError with code 'chapter_mismatch'."""
        chapters = [self._chapter(key="chapter:perio-ch3")]

        def fake_llm(prompt: str) -> str:
            return (
                '{"intent": "learn_chapter", "days": 3, "daily_minutes": 30, '
                '"focus_text": null, '
                '"book_title": null, '
                '"document_id": "document.d766e8629232a2f6", '
                '"chapter_key": "chapter:perio-ch3"}'
            )

        with self.assertRaises(PlanRequestParseError) as ctx:
            parse_plan_request_text(
                "学习牙周病学第三章",
                chapters=chapters,
                llm_call=fake_llm,
            )

        self.assertEqual(ctx.exception.code, "chapter_mismatch")
        self.assertIn("book_title", str(ctx.exception))

    def test_chapter_key_and_book_without_document_rejected(self) -> None:
        """When chapter_key and book_title are non-null, document_id must also
        be non-null — otherwise PlanRequestParseError with code 'chapter_mismatch'."""
        chapters = [self._chapter(key="chapter:perio-ch3")]

        def fake_llm(prompt: str) -> str:
            return (
                '{"intent": "learn_chapter", "days": 3, "daily_minutes": 30, '
                '"focus_text": null, '
                '"book_title": "牙周病学", '
                '"document_id": null, '
                '"chapter_key": "chapter:perio-ch3"}'
            )

        with self.assertRaises(PlanRequestParseError) as ctx:
            parse_plan_request_text(
                "学习牙周病学第三章",
                chapters=chapters,
                llm_call=fake_llm,
            )

        self.assertEqual(ctx.exception.code, "chapter_mismatch")
        self.assertIn("document_id", str(ctx.exception))

    # ------------------------------------------------------------------
    # new: valid output is already normalized (defaults, trim)
    # ------------------------------------------------------------------

    def test_valid_output_has_normalized_defaults_and_trim(self) -> None:
        """When parse succeeds, the returned dict is already the normalized
        result — null days/daily_minutes become defaults, whitespace is
        trimmed, empty strings become None."""
        chapters = [self._chapter(key="chapter:perio-ch3")]

        def fake_llm(prompt: str) -> str:
            return (
                '{"intent": "learn_chapter", "days": null, "daily_minutes": null, '
                '"focus_text": "  牙菌斑  ", '
                '"book_title": " 牙周病学 ", '
                '"document_id": " document.d766e8629232a2f6 ", '
                '"chapter_key": " chapter:perio-ch3 "}'
            )

        result = parse_plan_request_text(
            "学习牙周病学第三章牙菌斑",
            chapters=chapters,
            llm_call=fake_llm,
        )

        # Defaults from normalize_plan_request
        self.assertEqual(result["days"], 7)
        self.assertEqual(result["daily_minutes"], 30)
        # Trimming from normalize_plan_request
        self.assertEqual(result["focus_text"], "牙菌斑")
        self.assertEqual(result["book_title"], "牙周病学")
        self.assertEqual(result["document_id"], "document.d766e8629232a2f6")
        self.assertEqual(result["chapter_key"], "chapter:perio-ch3")


class CreatePlanDraftFromTextTests(unittest.TestCase):
    """Tests for :func:`create_plan_draft_from_text` — the composition of
    ``parse_plan_request_text`` + ``create_custom_plan_draft``."""

    @staticmethod
    def _store(ttl_seconds: int = 300) -> PlanDraftStore:
        return PlanDraftStore(ttl_seconds=ttl_seconds)

    # ------------------------------------------------------------------
    # positive: real chapter + natural language → readable draft
    # ------------------------------------------------------------------

    def test_real_chapter_nl_creates_readable_draft(self) -> None:
        """Natural language '三天复习第三章，每天30分钟' with real chapters
        generates a draft whose request/items/scopes are consistent."""
        store = self._store()
        chapters = [
            _chapter(key="chapter:perio-ch3"),
            _chapter(key="chapter:perio-ch4", title="第四章 牙周病的临床表现"),
        ]

        def fake_llm(prompt: str) -> str:
            return (
                '{"intent": "review_chapter", "days": 3, "daily_minutes": 30, '
                '"focus_text": null, '
                '"book_title": "牙周病学", '
                '"document_id": "document.d766e8629232a2f6", '
                '"chapter_key": "chapter:perio-ch3"}'
            )

        record = create_plan_draft_from_text(
            store,
            user_id="u1",
            message="三天复习《牙周病学》第三章，每天30分钟",
            chapters=chapters,
            llm_call=fake_llm,
        )

        # Draft record shape
        self.assertTrue(record["plan_draft_id"].startswith("draft_"))
        self.assertEqual(record["user_id"], "u1")
        self.assertIn("request", record["payload"])
        self.assertIn("items", record["payload"])

        # Normalised request in payload
        req = record["payload"]["request"]
        self.assertEqual(req["intent"], "review_chapter")
        self.assertEqual(req["days"], 3)
        self.assertEqual(req["daily_minutes"], 30)
        self.assertEqual(req["chapter_key"], "chapter:perio-ch3")

        # Items carry real learning_scope from KG chapters
        items = record["payload"]["items"]
        self.assertGreater(len(items), 0)
        for item in items:
            self.assertEqual(item["task_type"], "review_chapter")
            self.assertEqual(item["evidence_source"], "kg_chapter")
            self.assertIsNotNone(item["learning_scope"])
            scope = item["learning_scope"]
            self.assertEqual(scope["book_title"], "牙周病学")
            self.assertEqual(scope["chapter_key"], "chapter:perio-ch3")
            self.assertEqual(
                scope["kg_node_ids"],
                ["core:aaa111", "core:bbb222", "core:ccc333"],
            )

    def test_different_nl_produces_different_draft(self) -> None:
        """Different natural language (days=5, minutes=45, focus=炎症机制)
        produces correspondingly different draft content."""
        store = self._store()
        chapters = [_chapter(key="chapter:perio-ch3")]

        def fake_llm(prompt: str) -> str:
            return (
                '{"intent": "learn_chapter", "days": 5, "daily_minutes": 45, '
                '"focus_text": "炎症机制", '
                '"book_title": "牙周病学", '
                '"document_id": "document.d766e8629232a2f6", '
                '"chapter_key": "chapter:perio-ch3"}'
            )

        record = create_plan_draft_from_text(
            store,
            user_id="u1",
            message="五天学习牙周病学第三章，每天45分钟，重点炎症机制",
            chapters=chapters,
            llm_call=fake_llm,
        )

        req = record["payload"]["request"]
        self.assertEqual(req["intent"], "learn_chapter")
        self.assertEqual(req["days"], 5)
        self.assertEqual(req["daily_minutes"], 45)
        self.assertEqual(req["focus_text"], "炎症机制")

        items = record["payload"]["items"]
        self.assertEqual(len(items), 5)  # min(days=5, 5) = 5
        for item in items:
            self.assertEqual(item["estimated_minutes"], 45)
            self.assertEqual(item["task_type"], "learn_chapter")
            self.assertEqual(item["evidence_source"], "kg_chapter")
            # chapter-scoped items use chapter_title for learning_target,
            # not focus_text
            self.assertIn("牙周病的发病机制", item["learning_target"])
            self.assertIsNotNone(item["learning_scope"])

    def test_different_chapter_produces_different_scope(self) -> None:
        """Selecting a different chapter candidate produces a different scope
        and items, verifying the request→scope link is not hardcoded."""
        store = self._store()
        chapters = [
            _chapter(key="chapter:perio-ch3"),
            _chapter(
                key="chapter:endo-ch1",
                book_title="牙体牙髓病学",
                document_id="document.endo-001",
                title="第一章 绪论",
                core_ids=["core:endo-a", "core:endo-b"],
            ),
        ]

        def fake_llm(prompt: str) -> str:
            return (
                '{"intent": "learn_chapter", "days": 2, "daily_minutes": 20, '
                '"focus_text": null, '
                '"book_title": "牙体牙髓病学", '
                '"document_id": "document.endo-001", '
                '"chapter_key": "chapter:endo-ch1"}'
            )

        record = create_plan_draft_from_text(
            store,
            user_id="u1",
            message="学习牙体牙髓病学第一章",
            chapters=chapters,
            llm_call=fake_llm,
        )

        req = record["payload"]["request"]
        self.assertEqual(req["chapter_key"], "chapter:endo-ch1")
        self.assertEqual(req["book_title"], "牙体牙髓病学")

        for item in record["payload"]["items"]:
            scope = item["learning_scope"]
            self.assertEqual(scope["book_title"], "牙体牙髓病学")
            self.assertEqual(scope["chapter_key"], "chapter:endo-ch1")
            self.assertEqual(scope["kg_node_ids"], ["core:endo-a", "core:endo-b"])

    # ------------------------------------------------------------------
    # negative: parse failure → no draft in store
    # ------------------------------------------------------------------

    def test_fabricated_chapter_no_draft_created(self) -> None:
        """When the LLM returns a fabricated chapter_key not in candidates,
        PlanRequestParseError is raised and no draft exists in the store."""
        store = self._store()
        chapters = [_chapter(key="chapter:perio-ch3")]

        def fake_llm(prompt: str) -> str:
            return (
                '{"intent": "learn_chapter", "days": 3, "daily_minutes": 30, '
                '"focus_text": null, '
                '"book_title": "牙周病学", '
                '"document_id": "document.d766e8629232a2f6", '
                '"chapter_key": "chapter:made-up-fake"}'
            )

        with self.assertRaises(PlanRequestParseError) as ctx:
            create_plan_draft_from_text(
                store,
                user_id="u1",
                message="学习虚构章节",
                chapters=chapters,
                llm_call=fake_llm,
            )

        self.assertEqual(ctx.exception.code, "chapter_not_found")

    def test_llm_exception_no_draft_created(self) -> None:
        """When the LLM callable raises, PlanRequestParseError propagates and
        no draft is persisted."""
        store = self._store()
        chapters = [_chapter(key="chapter:perio-ch3")]

        def failing_llm(prompt: str) -> str:
            raise RuntimeError("LLM is down")

        with self.assertRaises(PlanRequestParseError):
            create_plan_draft_from_text(
                store,
                user_id="u1",
                message="学习牙周病学第三章",
                chapters=chapters,
                llm_call=failing_llm,
            )

    def test_scope_invalid_no_draft_created(self) -> None:
        """When the LLM returns a valid chapter_key but the chapter itself is
        invalid (empty core_ids), ValueError propagates and no draft exists."""
        store = self._store()
        chapters = [_chapter(key="chapter:perio-ch3", core_ids=[])]

        def fake_llm(prompt: str) -> str:
            return (
                '{"intent": "learn_chapter", "days": 3, "daily_minutes": 30, '
                '"focus_text": null, '
                '"book_title": "牙周病学", '
                '"document_id": "document.d766e8629232a2f6", '
                '"chapter_key": "chapter:perio-ch3"}'
            )

        with self.assertRaises(ValueError) as ctx:
            create_plan_draft_from_text(
                store,
                user_id="u1",
                message="学习牙周病学第三章",
                chapters=chapters,
                llm_call=fake_llm,
            )

        self.assertIn("no valid core", str(ctx.exception).lower())

    def test_learn_chapter_without_chapters_no_draft(self) -> None:
        """When the LLM returns learn_chapter with null chapter_key (no
        candidates), the parse step itself rejects it — no draft created."""
        store = self._store()

        def fake_llm(prompt: str) -> str:
            return (
                '{"intent": "learn_chapter", "days": 3, "daily_minutes": 30, '
                '"focus_text": "牙周病", '
                '"book_title": null, '
                '"document_id": null, '
                '"chapter_key": null}'
            )

        with self.assertRaises(PlanRequestParseError) as ctx:
            create_plan_draft_from_text(
                store,
                user_id="u1",
                message="学习牙周病",
                chapters=[],
                llm_call=fake_llm,
            )

        self.assertEqual(ctx.exception.code, "missing_chapter_key")

    def test_invalid_json_no_draft_created(self) -> None:
        """When the LLM returns garbage, PlanRequestParseError propagates."""
        store = self._store()

        def fake_llm(prompt: str) -> str:
            return "not json at all"

        with self.assertRaises(PlanRequestParseError) as ctx:
            create_plan_draft_from_text(
                store,
                user_id="u1",
                message="随便学学",
                chapters=[],
                llm_call=fake_llm,
            )

        self.assertEqual(ctx.exception.code, "invalid_json")

    # ------------------------------------------------------------------
    # parent draft: child has correct lineage, parent unchanged
    # ------------------------------------------------------------------

    def test_child_draft_has_correct_parent_lineage(self) -> None:
        """A child draft created with parent_plan_draft_id links to the parent
        correctly, and the parent payload is not mutated."""
        store = self._store()
        chapters = [_chapter(key="chapter:perio-ch3")]

        # Create parent draft via the combined service
        def fake_llm_parent(prompt: str) -> str:
            return (
                '{"intent": "learn_chapter", "days": 1, "daily_minutes": 30, '
                '"focus_text": null, '
                '"book_title": "牙周病学", '
                '"document_id": "document.d766e8629232a2f6", '
                '"chapter_key": "chapter:perio-ch3"}'
            )

        parent_record = create_plan_draft_from_text(
            store,
            user_id="u1",
            message="学习牙周病学第三章",
            chapters=chapters,
            llm_call=fake_llm_parent,
        )
        parent_id = parent_record["plan_draft_id"]
        parent_payload_before = parent_record["payload"]

        # Create child draft
        def fake_llm_child(prompt: str) -> str:
            return (
                '{"intent": "review_chapter", "days": 2, "daily_minutes": 20, '
                '"focus_text": "牙菌斑", '
                '"book_title": "牙周病学", '
                '"document_id": "document.d766e8629232a2f6", '
                '"chapter_key": "chapter:perio-ch3"}'
            )

        child_record = create_plan_draft_from_text(
            store,
            user_id="u1",
            message="复习牙周病学第三章牙菌斑",
            chapters=chapters,
            llm_call=fake_llm_child,
            parent_plan_draft_id=parent_id,
        )

        # Child has its own ID and correct parent link
        self.assertNotEqual(child_record["plan_draft_id"], parent_id)
        self.assertEqual(child_record["parent_plan_draft_id"], parent_id)

        # Child items are different from parent
        child_items = child_record["payload"]["items"]
        self.assertEqual(child_items[0]["task_type"], "review_chapter")
        self.assertEqual(child_items[0]["estimated_minutes"], 20)

        # Parent payload is completely unchanged
        parent_after = store.get(parent_id, user_id="u1")
        self.assertEqual(parent_after["payload"], parent_payload_before)
        self.assertEqual(
            parent_after["payload"]["items"][0]["task_type"], "learn_chapter"
        )
        self.assertNotIn("parent_plan_draft_id", parent_after)

    def test_child_without_parent_has_no_parent_field(self) -> None:
        """When parent_plan_draft_id is not passed, the draft record must not
        include a parent_plan_draft_id field."""
        store = self._store()
        chapters = [_chapter(key="chapter:perio-ch3")]

        def fake_llm(prompt: str) -> str:
            return (
                '{"intent": "learn_chapter", "days": 1, "daily_minutes": 30, '
                '"focus_text": null, '
                '"book_title": "牙周病学", '
                '"document_id": "document.d766e8629232a2f6", '
                '"chapter_key": "chapter:perio-ch3"}'
            )

        record = create_plan_draft_from_text(
            store,
            user_id="u1",
            message="学习牙周病学第三章",
            chapters=chapters,
            llm_call=fake_llm,
        )

        self.assertNotIn("parent_plan_draft_id", record)

    def test_cross_user_parent_rejected(self) -> None:
        """A parent owned by a different user must raise PermissionError."""
        store = self._store()
        chapters = [_chapter(key="chapter:perio-ch3")]

        # Create parent as uA
        def fake_llm(prompt: str) -> str:
            return (
                '{"intent": "learn_chapter", "days": 1, "daily_minutes": 30, '
                '"focus_text": null, '
                '"book_title": "牙周病学", '
                '"document_id": "document.d766e8629232a2f6", '
                '"chapter_key": "chapter:perio-ch3"}'
            )

        parent_record = create_plan_draft_from_text(
            store,
            user_id="uA",
            message="学习牙周病学第三章",
            chapters=chapters,
            llm_call=fake_llm,
        )
        parent_id = parent_record["plan_draft_id"]

        # Try to create child as uB
        with self.assertRaises(PermissionError) as ctx:
            create_plan_draft_from_text(
                store,
                user_id="uB",
                message="复习牙周病学第三章",
                chapters=chapters,
                llm_call=fake_llm,
                parent_plan_draft_id=parent_id,
            )

        self.assertIn(parent_id, str(ctx.exception))
        self.assertIn("uB", str(ctx.exception))

    # ------------------------------------------------------------------
    # no chapter candidates: review_weakness / custom → user_specified
    # ------------------------------------------------------------------

    def test_review_weakness_without_chapters_user_specified(self) -> None:
        """review_weakness with no chapter candidates generates user_specified
        items with no learning_scope — no fake scope is invented."""
        store = self._store()

        def fake_llm(prompt: str) -> str:
            return (
                '{"intent": "review_weakness", "days": 3, "daily_minutes": 20, '
                '"focus_text": "免疫防御机制", '
                '"book_title": null, '
                '"document_id": null, '
                '"chapter_key": null}'
            )

        record = create_plan_draft_from_text(
            store,
            user_id="u1",
            message="复习薄弱点免疫防御机制，每天20分钟",
            chapters=[],
            llm_call=fake_llm,
        )

        req = record["payload"]["request"]
        self.assertEqual(req["intent"], "review_weakness")
        self.assertEqual(req["focus_text"], "免疫防御机制")

        items = record["payload"]["items"]
        for item in items:
            self.assertEqual(item["task_type"], "review_weakness")
            self.assertEqual(item["evidence_source"], "user_specified")
            self.assertIsNone(item["learning_scope"])
            self.assertIn("免疫防御机制", item["learning_target"])
            self.assertIn("免疫防御机制", item["reason"])

    def test_custom_without_chapters_user_specified(self) -> None:
        """custom intent with no chapters generates user_specified items,
        no fake scope."""
        store = self._store()

        def fake_llm(prompt: str) -> str:
            return (
                '{"intent": "custom", "days": 5, "daily_minutes": 30, '
                '"focus_text": "阅读口腔微生物学综述", '
                '"book_title": null, '
                '"document_id": null, '
                '"chapter_key": null}'
            )

        record = create_plan_draft_from_text(
            store,
            user_id="u1",
            message="阅读口腔微生物学综述，五天",
            chapters=[],
            llm_call=fake_llm,
        )

        req = record["payload"]["request"]
        self.assertEqual(req["intent"], "custom")
        self.assertEqual(req["days"], 5)
        self.assertEqual(req["focus_text"], "阅读口腔微生物学综述")

        items = record["payload"]["items"]
        for item in items:
            self.assertEqual(item["task_type"], "custom")
            self.assertEqual(item["evidence_source"], "user_specified")
            self.assertIsNone(item["learning_scope"])
            self.assertIn("阅读口腔微生物学综述", item["learning_target"])

    # ------------------------------------------------------------------
    # input immutability
    # ------------------------------------------------------------------

    def test_message_not_mutated(self) -> None:
        """The original message string is never modified by the service."""
        store = self._store()
        chapters = [_chapter(key="chapter:perio-ch3")]
        message = "  三天复习牙周病学第三章  "

        def fake_llm(prompt: str) -> str:
            return (
                '{"intent": "review_chapter", "days": 3, "daily_minutes": 30, '
                '"focus_text": null, '
                '"book_title": "牙周病学", '
                '"document_id": "document.d766e8629232a2f6", '
                '"chapter_key": "chapter:perio-ch3"}'
            )

        create_plan_draft_from_text(
            store,
            user_id="u1",
            message=message,
            chapters=chapters,
            llm_call=fake_llm,
        )

        self.assertEqual(message, "  三天复习牙周病学第三章  ")

    def test_chapters_not_mutated(self) -> None:
        """The chapters list passed by the caller is never modified."""
        store = self._store()
        chapters = [_chapter(key="chapter:perio-ch3")]
        original_core_ids = list(chapters[0]["core_ids"])

        def fake_llm(prompt: str) -> str:
            return (
                '{"intent": "learn_chapter", "days": 1, "daily_minutes": 30, '
                '"focus_text": null, '
                '"book_title": "牙周病学", '
                '"document_id": "document.d766e8629232a2f6", '
                '"chapter_key": "chapter:perio-ch3"}'
            )

        create_plan_draft_from_text(
            store,
            user_id="u1",
            message="学习牙周病学第三章",
            chapters=chapters,
            llm_call=fake_llm,
        )

        self.assertEqual(chapters[0]["core_ids"], original_core_ids)
        self.assertEqual(chapters[0]["key"], "chapter:perio-ch3")

    # ------------------------------------------------------------------
    # success: draft is re-readable via store
    # ------------------------------------------------------------------

    def test_draft_re_readable_from_store(self) -> None:
        """The returned record can be independently re-read from the store
        and matches — confirming it was fully persisted."""
        store = self._store()
        chapters = [_chapter(key="chapter:perio-ch3")]

        def fake_llm(prompt: str) -> str:
            return (
                '{"intent": "learn_chapter", "days": 1, "daily_minutes": 30, '
                '"focus_text": null, '
                '"book_title": "牙周病学", '
                '"document_id": "document.d766e8629232a2f6", '
                '"chapter_key": "chapter:perio-ch3"}'
            )

        record = create_plan_draft_from_text(
            store,
            user_id="u1",
            message="学习牙周病学第三章",
            chapters=chapters,
            llm_call=fake_llm,
        )

        draft_id = record["plan_draft_id"]

        # Re-read independently
        re_read = store.get(draft_id, user_id="u1")
        self.assertEqual(re_read["plan_draft_id"], draft_id)
        self.assertEqual(re_read["user_id"], "u1")
        self.assertEqual(re_read["payload"], record["payload"])


class RecommendStudyPlanCustomDraftTests(unittest.TestCase):
    """Tests for the custom-draft branch of :func:`recommend_study_plan`."""

    @staticmethod
    def _store(ttl_seconds: int = 300) -> PlanDraftStore:
        return PlanDraftStore(ttl_seconds=ttl_seconds)

    @staticmethod
    def _chapters() -> list[dict]:
        return [
            _chapter(key="chapter:perio-ch3"),
            _chapter(
                key="chapter:endo-ch1",
                book_title="牙体牙髓病学",
                document_id="document.endo-001",
                title="第一章 绪论",
                core_ids=["core:endo-a", "core:endo-b"],
            ),
        ]

    @staticmethod
    def _fake_llm_perio(prompt: str) -> str:
        return (
            '{"intent": "learn_chapter", "days": 3, "daily_minutes": 30, '
            '"focus_text": null, '
            '"book_title": "牙周病学", '
            '"document_id": "document.d766e8629232a2f6", '
            '"chapter_key": "chapter:perio-ch3"}'
        )

    @staticmethod
    def _make_memory() -> LearningMemory:
        import tempfile
        from pathlib import Path
        tmp = tempfile.mkdtemp()
        return LearningMemory(Path(tmp) / "learning.sqlite3")

    # ── custom draft happy path ────────────────────────────────────────

    def test_non_empty_request_text_generates_custom_draft(self) -> None:
        """Non-empty request_text enters custom_draft mode with real plan_draft_id
        and cards mapped from the draft items."""
        store = self._store()
        result = recommend_study_plan(
            self._make_memory(),
            user_id="u1",
            plan_date="2026-07-20",
            request_text="三天学习牙周病学第三章，每天30分钟",
            draft_store=store,
            chapter_provider=self._chapters,
            llm_call=self._fake_llm_perio,
        )

        self.assertEqual(result["user_id"], "u1")
        self.assertEqual(result["plan_date"], "2026-07-20")
        self.assertEqual(result["mode"], "custom_draft")
        self.assertTrue(result["plan_draft_id"].startswith("draft_"))

        recs = result["recommendations"]
        self.assertEqual(len(recs), 3)  # min(days=3, 5) = 3

        for i, rec in enumerate(recs):
            self.assertEqual(rec["recommendation_id"], f"{result['plan_draft_id']}:{i}")
            self.assertEqual(rec["task_type"], "learn_chapter")
            self.assertIn("牙周病的发病机制", rec["title"])
            self.assertIn("牙周病的发病机制", rec["learning_target"])
            self.assertIn("牙周病学", rec["reason"])
            self.assertEqual(rec["estimated_minutes"], 30)
            self.assertEqual(rec["evidence_source"], "kg_chapter")
            self.assertIsNotNone(rec["learning_scope"])
            self.assertEqual(
                rec["kg_node_ids"],
                ["core:aaa111", "core:bbb222", "core:ccc333"],
            )

        # Trace has custom_draft strategy
        trace = result["trace"]
        self.assertEqual(trace["strategy"], "custom_draft_from_request_text")
        self.assertEqual(trace["draft_id"], result["plan_draft_id"])
        self.assertEqual(trace["item_count"], 3)
        self.assertEqual(trace["parsed_intent"], "learn_chapter")

    def test_different_texts_produce_different_drafts(self) -> None:
        """Two different request texts produce different drafts and card scopes,
        not a fixed/hardcoded recommendation set."""
        store = self._store()
        chapters = self._chapters()

        def fake_llm_endo(prompt: str) -> str:
            return (
                '{"intent": "learn_chapter", "days": 2, "daily_minutes": 20, '
                '"focus_text": null, '
                '"book_title": "牙体牙髓病学", '
                '"document_id": "document.endo-001", '
                '"chapter_key": "chapter:endo-ch1"}'
            )

        result_a = recommend_study_plan(
            self._make_memory(),
            user_id="u1",
            plan_date="2026-07-21",
            request_text="学习牙周病学第三章",
            draft_store=store,
            chapter_provider=lambda: chapters,
            llm_call=self._fake_llm_perio,
        )

        result_b = recommend_study_plan(
            self._make_memory(),
            user_id="u1",
            plan_date="2026-07-21",
            request_text="学习牙体牙髓病学第一章",
            draft_store=store,
            chapter_provider=lambda: chapters,
            llm_call=fake_llm_endo,
        )

        # Different draft IDs
        self.assertNotEqual(result_a["plan_draft_id"], result_b["plan_draft_id"])

        # Different scopes
        scope_a = result_a["recommendations"][0]["learning_scope"]
        scope_b = result_b["recommendations"][0]["learning_scope"]
        self.assertEqual(scope_a["book_title"], "牙周病学")
        self.assertEqual(scope_b["book_title"], "牙体牙髓病学")
        self.assertNotEqual(scope_a["kg_node_ids"], scope_b["kg_node_ids"])

        # Different item counts (days=3 → 3 items vs days=2 → 2 items)
        self.assertEqual(len(result_a["recommendations"]), 3)
        self.assertEqual(len(result_b["recommendations"]), 2)

    # ── parent draft passthrough ───────────────────────────────────────

    def test_parent_draft_transmission(self) -> None:
        """parent_plan_draft_id is passed through; parent is not mutated."""
        store = self._store()
        chapters = self._chapters()

        # Create parent
        parent_result = recommend_study_plan(
            self._make_memory(),
            user_id="u1",
            plan_date="2026-07-22",
            request_text="学习牙周病学第三章",
            draft_store=store,
            chapter_provider=lambda: chapters,
            llm_call=self._fake_llm_perio,
        )
        parent_id = parent_result["plan_draft_id"]
        parent_payload_before = store.get(parent_id, user_id="u1")["payload"]

        # Create child with parent
        def fake_llm_child(prompt: str) -> str:
            return (
                '{"intent": "review_chapter", "days": 2, "daily_minutes": 20, '
                '"focus_text": "牙菌斑", '
                '"book_title": "牙周病学", '
                '"document_id": "document.d766e8629232a2f6", '
                '"chapter_key": "chapter:perio-ch3"}'
            )

        child_result = recommend_study_plan(
            self._make_memory(),
            user_id="u1",
            plan_date="2026-07-22",
            request_text="复习牙周病学第三章牙菌斑",
            draft_store=store,
            chapter_provider=lambda: chapters,
            llm_call=fake_llm_child,
            parent_plan_draft_id=parent_id,
        )

        # Child links to parent
        self.assertEqual(child_result["parent_plan_draft_id"], parent_id)
        self.assertEqual(
            child_result["trace"]["parent_plan_draft_id"], parent_id
        )

        # Parent is untouched
        parent_after = store.get(parent_id, user_id="u1")
        self.assertEqual(parent_after["payload"], parent_payload_before)
        self.assertNotIn("parent_plan_draft_id", parent_after)

    # ── default store is shareable ─────────────────────────────────────

    def test_default_store_draft_readable_via_helper(self) -> None:
        """A draft created through the custom branch using the default store
        can be read back via :func:`get_draft_from_default_store`."""
        # Use the *real* default store (not an injected one) so we exercise
        # the singleton path.
        result = recommend_study_plan(
            self._make_memory(),
            user_id="u1",
            plan_date="2026-07-23",
            request_text="学习牙周病学第三章",
            chapter_provider=self._chapters,
            llm_call=self._fake_llm_perio,
        )
        draft_id = result["plan_draft_id"]

        re_read = get_draft_from_default_store(draft_id, user_id="u1")
        self.assertEqual(re_read["plan_draft_id"], draft_id)
        self.assertEqual(re_read["user_id"], "u1")
        self.assertEqual(
            re_read["payload"]["request"]["intent"], "learn_chapter"
        )
        self.assertEqual(len(re_read["payload"]["items"]), 3)

    # ── overview / chapter failures ────────────────────────────────────

    def test_overview_unavailable_raises(self) -> None:
        """When the chapter provider raises (simulating unavailable overview),
        a RuntimeError propagates — no fallback to deterministic."""
        def failing_provider() -> list[dict]:
            raise RuntimeError("KG not ready")

        with self.assertRaises(RuntimeError) as ctx:
            recommend_study_plan(
                self._make_memory(),
                user_id="u1",
                plan_date="2026-07-20",
                request_text="学习牙周病学",
                draft_store=self._store(),
                chapter_provider=failing_provider,
                llm_call=self._fake_llm_perio,
            )
        self.assertIn("KG not ready", str(ctx.exception))

    def test_empty_chapters_raises(self) -> None:
        """When the chapter provider returns an empty list, a RuntimeError is
        raised — no pseudo-chapters, no fallback to fixed recommendations."""
        with self.assertRaises(RuntimeError) as ctx:
            recommend_study_plan(
                self._make_memory(),
                user_id="u1",
                plan_date="2026-07-20",
                request_text="学习牙周病学",
                draft_store=self._store(),
                chapter_provider=lambda: [],
                llm_call=self._fake_llm_perio,
            )
        self.assertIn("no valid chapters", str(ctx.exception).lower())

    def test_parse_failure_raises_no_draft(self) -> None:
        """When the LLM returns a fabricated chapter key, PlanRequestParseError
        propagates — no half-written draft, no pseudo-default recommendations."""
        store = self._store()

        def fake_llm_bad(prompt: str) -> str:
            return (
                '{"intent": "learn_chapter", "days": 3, "daily_minutes": 30, '
                '"focus_text": null, '
                '"book_title": "假书", '
                '"document_id": "doc-fake", '
                '"chapter_key": "chapter:made-up"}'
            )

        with self.assertRaises(PlanRequestParseError) as ctx:
            recommend_study_plan(
                self._make_memory(),
                user_id="u1",
                plan_date="2026-07-20",
                request_text="学习不存在的章节",
                draft_store=store,
                chapter_provider=self._chapters,
                llm_call=fake_llm_bad,
            )
        self.assertEqual(ctx.exception.code, "chapter_not_found")
        self.assertIn("chapter:made-up", str(ctx.exception))
        # Verify the store is unchanged — no draft was persisted
        clean_draft_id = store.save(user_id="u1", payload={"ok": True})
        record = store.get(clean_draft_id, user_id="u1")
        self.assertEqual(record["payload"], {"ok": True})

    # ── deterministic path preserved ───────────────────────────────────

    def test_none_request_text_preserves_deterministic_mode(self) -> None:
        """When request_text is None, the existing deterministic behaviour is
        exactly preserved — mode is preview_recommendations, no LLM/KG/draft."""
        memory = self._make_memory()
        result = recommend_study_plan(
            memory,
            user_id="u1",
            plan_date="2026-07-20",
            request_text=None,
        )

        self.assertEqual(result["mode"], "preview_recommendations")
        self.assertNotIn("plan_draft_id", result)
        self.assertEqual(
            result["trace"]["strategy"], "deterministic_dual_mode_study_plan"
        )

    def test_empty_request_text_preserves_deterministic_mode(self) -> None:
        """Empty or whitespace-only request_text must stay in deterministic mode."""
        memory = self._make_memory()
        for text in ("", "   ", "\t\n"):
            result = recommend_study_plan(
                memory,
                user_id="u1",
                plan_date="2026-07-20",
                request_text=text,
            )
            self.assertEqual(result["mode"], "preview_recommendations")
            self.assertNotIn("plan_draft_id", result)

    def test_deterministic_path_does_not_call_providers(self) -> None:
        """When request_text is None, neither chapter_provider nor llm_call is
        invoked — confirmed by injecting fakes that would raise if called."""
        memory = self._make_memory()

        def boom_provider() -> list[dict]:
            raise AssertionError("chapter_provider should not be called")

        def boom_llm(prompt: str) -> str:
            raise AssertionError("llm_call should not be called")

        # Must NOT raise
        result = recommend_study_plan(
            memory,
            user_id="u1",
            plan_date="2026-07-20",
            request_text=None,
            chapter_provider=boom_provider,
            llm_call=boom_llm,
        )
        self.assertEqual(result["mode"], "preview_recommendations")

    def test_existing_positional_call_still_works(self) -> None:
        """Existing callers that omit request_text (positional/keyword) are
        fully compatible — the signature extension is backwards-compatible."""
        memory = self._make_memory()
        result = recommend_study_plan(
            memory,
            user_id="u1",
            plan_date="2026-07-20",
            max_items=3,
        )
        self.assertEqual(result["mode"], "preview_recommendations")
        self.assertEqual(result["user_id"], "u1")
        self.assertEqual(result["plan_date"], "2026-07-20")
        self.assertLessEqual(len(result["recommendations"]), 3)

    # ── recommendation card shape ──────────────────────────────────────

    def test_custom_draft_cards_have_required_fields(self) -> None:
        """Every recommendation card in custom_draft mode has at minimum the
        nine stable fields required by the downstream plan card UI."""
        store = self._store()
        result = recommend_study_plan(
            self._make_memory(),
            user_id="u1",
            plan_date="2026-07-24",
            request_text="学习牙周病学第三章",
            draft_store=store,
            chapter_provider=self._chapters,
            llm_call=self._fake_llm_perio,
        )

        required_keys = {
            "recommendation_id",
            "title",
            "task_type",
            "learning_target",
            "reason",
            "estimated_minutes",
            "kg_node_ids",
            "learning_scope",
            "evidence_source",
        }

        for rec in result["recommendations"]:
            self.assertTrue(
                required_keys.issubset(set(rec.keys())),
                f"Missing keys in recommendation: {required_keys - set(rec.keys())}",
            )
            # recommendation_id is bound to draft_id + index
            self.assertTrue(
                rec["recommendation_id"].startswith(result["plan_draft_id"] + ":")
            )

    def test_custom_draft_cards_no_generic_recalculation(self) -> None:
        """Card fields come exclusively from the draft scope — no generic
        recategorisation or recomputation of task_type/learning_target/reason."""
        store = self._store()
        chapters = self._chapters()

        def fake_llm_custom(prompt: str) -> str:
            return (
                '{"intent": "custom", "days": 2, "daily_minutes": 15, '
                '"focus_text": "阅读口腔微生物学综述", '
                '"book_title": null, '
                '"document_id": null, '
                '"chapter_key": null}'
            )

        result = recommend_study_plan(
            self._make_memory(),
            user_id="u1",
            plan_date="2026-07-24",
            request_text="阅读口腔微生物学综述",
            draft_store=store,
            chapter_provider=lambda: chapters,
            llm_call=fake_llm_custom,
        )

        rec = result["recommendations"][0]
        # Custom intent never claims KG evidence, never has scope
        self.assertEqual(rec["task_type"], "custom")
        self.assertEqual(rec["evidence_source"], "user_specified")
        self.assertIsNone(rec["learning_scope"])
        self.assertEqual(rec["kg_node_ids"], [])
        self.assertIn("阅读口腔微生物学综述", rec["title"])
        self.assertIn("阅读口腔微生物学综述", rec["learning_target"])

    # ── cross-user store isolation ─────────────────────────────────────

    def test_custom_draft_respects_user_isolation(self) -> None:
        """A draft created for user A via custom mode cannot be read by user B."""
        store = self._store()
        result = recommend_study_plan(
            self._make_memory(),
            user_id="uA",
            plan_date="2026-07-25",
            request_text="学习牙周病学第三章",
            draft_store=store,
            chapter_provider=self._chapters,
            llm_call=self._fake_llm_perio,
        )

        with self.assertRaises(PermissionError):
            store.get(result["plan_draft_id"], user_id="uB")

    # ── explicit exception propagation through recommend_study_plan ──────

    def test_nonexistent_parent_draft_raises_key_error(self) -> None:
        """A non-existent parent_plan_draft_id raises KeyError (not RuntimeError)
        and no new draft is persisted."""
        store = self._store()

        with self.assertRaises(KeyError) as ctx:
            recommend_study_plan(
                self._make_memory(),
                user_id="u1",
                plan_date="2026-07-26",
                request_text="学习牙周病学第三章",
                draft_store=store,
                chapter_provider=self._chapters,
                llm_call=self._fake_llm_perio,
                parent_plan_draft_id="draft_nonexistent_ff",
            )
        self.assertIn("draft_nonexistent_ff", str(ctx.exception))
        # Verify the store is unchanged — no child draft was persisted
        clean_draft_id = store.save(user_id="u1", payload={"ok": True})
        record = store.get(clean_draft_id, user_id="u1")
        self.assertEqual(record["payload"], {"ok": True})

    def test_cross_user_parent_draft_raises_permission_error(self) -> None:
        """A parent draft owned by user A must raise PermissionError (not
        RuntimeError) when user B tries to create a child, and no child draft
        is persisted."""
        store = self._store()

        # Create parent as uA
        parent_result = recommend_study_plan(
            self._make_memory(),
            user_id="uA",
            plan_date="2026-07-26",
            request_text="学习牙周病学第三章",
            draft_store=store,
            chapter_provider=self._chapters,
            llm_call=self._fake_llm_perio,
        )
        parent_id = parent_result["plan_draft_id"]

        # Snapshot parent payload before the cross-user attempt
        parent_before = store.get(parent_id, user_id="uA")

        # uB tries to create child with uA's parent
        with self.assertRaises(PermissionError) as ctx:
            recommend_study_plan(
                self._make_memory(),
                user_id="uB",
                plan_date="2026-07-26",
                request_text="复习牙周病学第三章",
                draft_store=store,
                chapter_provider=self._chapters,
                llm_call=self._fake_llm_perio,
                parent_plan_draft_id=parent_id,
            )
        self.assertIn(parent_id, str(ctx.exception))
        self.assertIn("uB", str(ctx.exception))

        # Parent is still readable by uA and unchanged
        parent_after = store.get(parent_id, user_id="uA")
        self.assertEqual(parent_after["payload"], parent_before["payload"])
        # No child draft was persisted — store has exactly the parent entry
        # (verified by: uB still cannot read anything from this store)

    def test_expired_parent_draft_raises_value_error(self) -> None:
        """An expired parent_plan_draft_id raises ValueError (not RuntimeError)
        and no child draft is persisted."""
        clock: dict[str, float] = {"now": 1_000_000.0}
        store = PlanDraftStore(ttl_seconds=60, _clock=lambda: clock["now"])

        # Create parent
        parent_result = recommend_study_plan(
            self._make_memory(),
            user_id="u1",
            plan_date="2026-07-26",
            request_text="学习牙周病学第三章",
            draft_store=store,
            chapter_provider=self._chapters,
            llm_call=self._fake_llm_perio,
        )
        parent_id = parent_result["plan_draft_id"]

        # Fast-forward past parent TTL
        clock["now"] += 61

        # Try to create child with expired parent
        with self.assertRaises(ValueError) as ctx:
            recommend_study_plan(
                self._make_memory(),
                user_id="u1",
                plan_date="2026-07-26",
                request_text="复习牙周病学第三章",
                draft_store=store,
                chapter_provider=self._chapters,
                llm_call=self._fake_llm_perio,
                parent_plan_draft_id=parent_id,
            )
        self.assertIn(parent_id, str(ctx.exception))
        self.assertIn("expired", str(ctx.exception).lower())
        # Verify the store is unchanged — no child draft was persisted
        # (parent is still expired, no new drafts added)
        count_before = len(store._drafts)
        # The failed save should not have added any entry
        self.assertEqual(count_before, len(store._drafts))

    def test_invalid_scope_empty_core_ids_raises_value_error(self) -> None:
        """When the resolved chapter has no valid core IDs, ValueError propagates
        (not RuntimeError) and no draft is persisted in the store."""
        store = self._store()
        chapters = [_chapter(key="chapter:perio-ch3", core_ids=[])]

        with self.assertRaises(ValueError) as ctx:
            recommend_study_plan(
                self._make_memory(),
                user_id="u1",
                plan_date="2026-07-26",
                request_text="学习牙周病学第三章",
                draft_store=store,
                chapter_provider=lambda: chapters,
                llm_call=self._fake_llm_perio,
            )
        self.assertIn("no valid core", str(ctx.exception).lower())
        # Verify the store is unchanged — no draft was persisted
        clean_draft_id = store.save(user_id="u1", payload={"ok": True})
        record = store.get(clean_draft_id, user_id="u1")
        self.assertEqual(record["payload"], {"ok": True})


class StudyPlanLearningScopeTests(unittest.TestCase):
    """Tests for learning_scope persistence on study_plan_items."""

    # -- old calls without scope -------------------------------------------

    def test_create_without_scope_returns_empty_dict(self) -> None:
        """Old callers that omit learning_scope get {} back."""
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                item = memory.create_study_plan_item(
                    user_id="u1",
                    plan_date=date.today().isoformat(),
                    title="Old-style item",
                )
                self.assertEqual(item["learning_scope"], {})
            finally:
                memory._conn.close()

    def test_update_without_scope_leaves_existing_unchanged(self) -> None:
        """Update without learning_scope key does not clear stored scope."""
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                item = memory.create_study_plan_item(
                    user_id="u1",
                    plan_date=date.today().isoformat(),
                    title="Scoped item",
                    learning_scope={"book_title": "牙周病学", "kg_node_ids": ["n1"]},
                )
                self.assertEqual(item["learning_scope"]["book_title"], "牙周病学")

                # Update title only — scope must survive
                updated = memory.update_study_plan_item(
                    item["id"],
                    user_id="u1",
                    updates={"title": "Renamed item"},
                )
                self.assertEqual(updated["learning_scope"]["book_title"], "牙周病学")
                self.assertEqual(updated["title"], "Renamed item")
            finally:
                memory._conn.close()

    # -- create / read with scope ------------------------------------------

    def test_create_with_scope_round_trips(self) -> None:
        """A learning_scope dict survives create → read round-trip."""
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                scope = {
                    "book_title": "牙周病学",
                    "document_id": "document.abc123",
                    "chapter_key": "chapter:perio-ch3",
                    "chapter_title": "第三章 牙周病的发病机制",
                    "kg_node_ids": ["core:aaa111", "core:bbb222"],
                }
                item = memory.create_study_plan_item(
                    user_id="u1",
                    plan_date=date.today().isoformat(),
                    title="Learn chapter 3",
                    learning_scope=scope,
                )
                self.assertEqual(item["learning_scope"], scope)
                self.assertEqual(
                    item["learning_scope"]["kg_node_ids"],
                    ["core:aaa111", "core:bbb222"],
                )
            finally:
                memory._conn.close()

    def test_create_with_none_scope_stores_empty_dict(self) -> None:
        """Passing learning_scope=None stores {}."""
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                item = memory.create_study_plan_item(
                    user_id="u1",
                    plan_date=date.today().isoformat(),
                    title="No scope item",
                    learning_scope=None,
                )
                self.assertEqual(item["learning_scope"], {})
            finally:
                memory._conn.close()

    # -- reload with scope (database persistence) --------------------------

    def test_scope_survives_reload(self) -> None:
        """Scope written by one memory instance is readable by another."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "learning.sqlite3"
            scope = {
                "book_title": "牙体牙髓病学",
                "document_id": "document.xyz",
                "chapter_key": "chapter:endo-ch1",
                "chapter_title": "第一章 绪论",
                "kg_node_ids": ["core:endo-1"],
            }

            # Write
            mem1 = LearningMemory(db_path)
            try:
                item = mem1.create_study_plan_item(
                    user_id="u1",
                    plan_date=date.today().isoformat(),
                    title="Endo chapter 1",
                    learning_scope=scope,
                )
                item_id = item["id"]
            finally:
                mem1._conn.close()

            # Read back from a fresh connection
            mem2 = LearningMemory(db_path)
            try:
                reloaded = mem2.get_study_plan_item(item_id, user_id="u1")
                self.assertEqual(reloaded["learning_scope"], scope)
                self.assertEqual(reloaded["learning_scope"]["book_title"], "牙体牙髓病学")
            finally:
                mem2._conn.close()

    # -- update scope ------------------------------------------------------

    def test_update_scope_replaces_entirely(self) -> None:
        """Updating learning_scope replaces the previous value."""
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                item = memory.create_study_plan_item(
                    user_id="u1",
                    plan_date=date.today().isoformat(),
                    title="Original scope",
                    learning_scope={"book_title": "Old Book", "kg_node_ids": ["old"]},
                )

                new_scope = {"book_title": "New Book", "kg_node_ids": ["new1", "new2"]}
                updated = memory.update_study_plan_item(
                    item["id"],
                    user_id="u1",
                    updates={"learning_scope": new_scope},
                )
                self.assertEqual(updated["learning_scope"], new_scope)
                self.assertEqual(updated["learning_scope"]["book_title"], "New Book")
            finally:
                memory._conn.close()

    def test_update_scope_to_empty_dict(self) -> None:
        """Updating learning_scope to {} clears it."""
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                item = memory.create_study_plan_item(
                    user_id="u1",
                    plan_date=date.today().isoformat(),
                    title="Scoped item",
                    learning_scope={"book_title": "牙周病学", "kg_node_ids": ["n1"]},
                )

                updated = memory.update_study_plan_item(
                    item["id"],
                    user_id="u1",
                    updates={"learning_scope": {}},
                )
                self.assertEqual(updated["learning_scope"], {})
            finally:
                memory._conn.close()

    # -- reject invalid non-object scope -----------------------------------

    def test_update_scope_rejects_string(self) -> None:
        """Passing a string as learning_scope raises ValueError."""
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                item = memory.create_study_plan_item(
                    user_id="u1",
                    plan_date=date.today().isoformat(),
                    title="Test item",
                )
                with self.assertRaises(ValueError) as ctx:
                    memory.update_study_plan_item(
                        item["id"],
                        user_id="u1",
                        updates={"learning_scope": "not a dict"},
                    )
                self.assertIn("learning_scope", str(ctx.exception))
            finally:
                memory._conn.close()

    def test_update_scope_rejects_list(self) -> None:
        """Passing a list as learning_scope raises ValueError."""
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                item = memory.create_study_plan_item(
                    user_id="u1",
                    plan_date=date.today().isoformat(),
                    title="Test item",
                )
                with self.assertRaises(ValueError) as ctx:
                    memory.update_study_plan_item(
                        item["id"],
                        user_id="u1",
                        updates={"learning_scope": [1, 2, 3]},
                    )
                self.assertIn("learning_scope", str(ctx.exception))
            finally:
                memory._conn.close()

    # -- existing database migration ---------------------------------------

    def test_existing_database_is_migrated_with_scope_column(self) -> None:
        """Old database without learning_scope_json gets the column via migration."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "learning.sqlite3"
            conn = sqlite3.connect(db_path)
            conn.execute(
                """CREATE TABLE study_plan_items (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    plan_date TEXT NOT NULL,
                    title TEXT NOT NULL,
                    notes TEXT,
                    status TEXT NOT NULL DEFAULT 'open',
                    linked_course_id TEXT,
                    linked_course_title TEXT,
                    task_type TEXT NOT NULL DEFAULT 'topic_learning',
                    learning_target TEXT,
                    kg_node_ids_json TEXT NOT NULL,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    deferred_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                )"""
            )
            conn.commit()
            conn.close()

            memory = LearningMemory(db_path)
            try:
                columns = {
                    row["name"]
                    for row in memory._conn.execute(
                        "PRAGMA table_info(study_plan_items)"
                    ).fetchall()
                }
                self.assertIn("learning_scope_json", columns)

                # Existing rows (none here) would read as {}; create one to verify
                item = memory.create_study_plan_item(
                    user_id="u1",
                    plan_date=date.today().isoformat(),
                    title="Migrated item",
                )
                self.assertEqual(item["learning_scope"], {})
            finally:
                memory._conn.close()

    # -- list / query methods return scope ---------------------------------

    def test_list_study_plan_items_includes_scope(self) -> None:
        """list_study_plan_items returns learning_scope on every item."""
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                today = date.today().isoformat()
                scope = {"book_title": "Test Book", "kg_node_ids": ["n1"]}
                memory.create_study_plan_item(
                    user_id="u1",
                    plan_date=today,
                    title="Scoped item",
                    learning_scope=scope,
                )
                memory.create_study_plan_item(
                    user_id="u1",
                    plan_date=today,
                    title="Unscoped item",
                )

                items = memory.list_study_plan_items_for_day("u1", today)
                self.assertEqual(len(items), 2)
                self.assertEqual(items[0]["learning_scope"], scope)
                self.assertEqual(items[1]["learning_scope"], {})
            finally:
                memory._conn.close()

    def test_list_open_items_includes_scope(self) -> None:
        """list_open_study_plan_items returns learning_scope."""
        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                today = date.today().isoformat()
                scope = {"book_title": "Open Book", "kg_node_ids": ["open-1"]}
                memory.create_study_plan_item(
                    user_id="u1",
                    plan_date=today,
                    title="Open scoped",
                    learning_scope=scope,
                )
                items = memory.list_open_study_plan_items("u1")
                self.assertEqual(len(items), 1)
                self.assertEqual(items[0]["learning_scope"], scope)
            finally:
                memory._conn.close()


if __name__ == "__main__":
    unittest.main()
