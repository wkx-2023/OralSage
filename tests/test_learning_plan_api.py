"""API tests for the plan-draft read endpoint — no external dependencies.

Each test saves a draft through the real default PlanDraftStore (using
the service-layer :func:`recommend_study_plan` with fake providers) and
then reads it back through the HTTP endpoint so that the full handoff
— service → HTTP → JSON/error — is exercised.
"""

from __future__ import annotations

import unittest
from typing import Any

from fastapi.testclient import TestClient

from backend.main import app
from backend.services.learning.study_plan import (
    PlanDraftStore,
    recommend_study_plan,
)


def _make_chapter(**overrides: object) -> dict[str, Any]:
    """Build a chapter dict in the production shape from the KG agent."""
    defaults: dict[str, Any] = {
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


def _fake_llm_learn_ch3(prompt: str) -> str:
    """Return a deterministic learn_chapter intent for 牙周病学第三章."""
    return (
        '{"intent": "learn_chapter", "days": 3, "daily_minutes": 30, '
        '"focus_text": null, '
        '"book_title": "牙周病学", '
        '"document_id": "document.d766e8629232a2f6", '
        '"chapter_key": "chapter:perio-ch3"}'
    )


def _fake_memory() -> Any:
    """Return a minimal in-memory LearningMemory double.

    ``recommend_study_plan`` in custom-draft mode does not touch the
    memory object when *request_text* is non-empty, so a lightweight
    stub is sufficient.
    """
    from unittest.mock import MagicMock
    memory = MagicMock()
    return memory


# ---------------------------------------------------------------------------
# TestCase
# ---------------------------------------------------------------------------


class PlanDraftReadApiTests(unittest.TestCase):
    """HTTP-level tests for GET /api/learning/plan/drafts/{plan_draft_id}."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)
        cls._chapters = [_make_chapter()]

    def _save_draft(self, user_id: str, *, request_text: str = "学习牙周病学第三章") -> str:
        """Save a real draft through the default store and return its ID."""
        result = recommend_study_plan(
            _fake_memory(),
            user_id=user_id,
            plan_date="2026-07-23",
            request_text=request_text,
            chapter_provider=lambda: self._chapters,
            llm_call=_fake_llm_learn_ch3,
        )
        return result["plan_draft_id"]

    # ── positive: owner reads own draft ─────────────────────────────────

    def test_owner_can_read_own_draft(self) -> None:
        """The user who created the draft can read it via the HTTP endpoint."""
        draft_id = self._save_draft("u1")

        resp = self.client.get(
            f"/api/learning/plan/drafts/{draft_id}",
            params={"user_id": "u1"},
        )

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["action"], "study_plan_get_draft")
        self.assertEqual(body["result"]["plan_draft_id"], draft_id)
        self.assertEqual(body["result"]["user_id"], "u1")
        self.assertIn("payload", body["result"])
        self.assertEqual(
            body["result"]["payload"]["request"]["intent"], "learn_chapter"
        )
        self.assertEqual(len(body["result"]["payload"]["items"]), 3)

        # The first item carries a real learning_scope resolved from the
        # chapter provider
        item0 = body["result"]["payload"]["items"][0]
        self.assertIsNotNone(item0["learning_scope"])
        self.assertEqual(
            item0["learning_scope"]["book_title"], "牙周病学"
        )

    # ── negative: cross-user rejection ──────────────────────────────────

    def test_other_user_cannot_read(self) -> None:
        """A draft owned by uA must not be readable by uB.

        The error response must NOT contain recommendations, payload or
        items — no fallback to default recommendations.
        """
        draft_id = self._save_draft("uA")

        resp = self.client.get(
            f"/api/learning/plan/drafts/{draft_id}",
            params={"user_id": "uB"},
        )

        self.assertEqual(resp.status_code, 403)
        body = resp.json()
        self.assertFalse(body["ok"])
        self.assertIn("error", body["result"])
        self.assertIn(draft_id, body["result"]["error"])
        self.assertIn("uB", body["result"]["error"])
        # The error response must NOT contain the draft payload
        self.assertNotIn("payload", body.get("result", {}))
        self.assertNotIn("items", body.get("result", {}))
        # Must NOT fall back to default recommendations
        self.assertNotIn("recommendations", body.get("result", {}))

    # ── negative: unknown draft ID ──────────────────────────────────────

    def test_unknown_draft_id_returns_404(self) -> None:
        """A draft ID that was never saved returns 404.

        The error response must NOT contain recommendations, payload or
        items — no fallback to default recommendations.
        """
        resp = self.client.get(
            "/api/learning/plan/drafts/draft_nonexistent_ff",
            params={"user_id": "u1"},
        )

        self.assertEqual(resp.status_code, 404)
        body = resp.json()
        self.assertFalse(body["ok"])
        self.assertIn("error", body["result"])
        self.assertIn("draft_nonexistent_ff", body["result"]["error"])
        # Must NOT fall back to default recommendations
        self.assertNotIn("recommendations", body.get("result", {}))
        self.assertNotIn("payload", body.get("result", {}))
        self.assertNotIn("items", body.get("result", {}))

    # ── negative: TTL expiry ───────────────────────────────────────────

    def test_expired_draft_returns_410(self) -> None:
        """After the default-store TTL elapses, the draft is no longer readable.

        The error response must NOT contain recommendations, payload or
        items — no fallback to default recommendations.
        """
        import time
        from backend.services.learning.study_plan import _get_default_draft_store

        # Use a short-TTL store as the module-level default so we can
        # fast-forward time without affecting other tests.
        store = PlanDraftStore(ttl_seconds=1, _clock=time.time)
        import backend.services.learning.study_plan as sp
        original = sp._default_draft_store
        sp._default_draft_store = store
        try:
            # Save through this short-lived store
            result = recommend_study_plan(
                _fake_memory(),
                user_id="u1",
                plan_date="2026-07-23",
                request_text="学习牙周病学第三章",
                draft_store=store,
                chapter_provider=lambda: self._chapters,
                llm_call=_fake_llm_learn_ch3,
            )
            draft_id = result["plan_draft_id"]

            # Wait for TTL expiry
            time.sleep(1.1)

            resp = self.client.get(
                f"/api/learning/plan/drafts/{draft_id}",
                params={"user_id": "u1"},
            )

            self.assertEqual(resp.status_code, 410)
            body = resp.json()
            self.assertFalse(body["ok"])
            self.assertIn("error", body["result"])
            self.assertIn(draft_id, body["result"]["error"])
            self.assertIn("expired", body["result"]["error"].lower())
            # Must NOT fall back to default recommendations
            self.assertNotIn("recommendations", body.get("result", {}))
            self.assertNotIn("payload", body.get("result", {}))
            self.assertNotIn("items", body.get("result", {}))
        finally:
            sp._default_draft_store = original

    # ── learning_scope API tests ────────────────────────────────────────

    def test_create_item_with_scope_returns_scope(self) -> None:
        """POST items with learning_scope returns it in the response."""
        import tempfile
        from pathlib import Path
        from backend.services.learning.memory import LearningMemory

        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                from backend.main import learning_orchestrator
                original_memory = learning_orchestrator.memory
                learning_orchestrator.memory = memory
                try:
                    scope = {
                        "book_title": "牙周病学",
                        "chapter_key": "chapter:perio-ch3",
                        "kg_node_ids": ["core:aaa111"],
                    }
                    resp = self.client.post(
                        "/api/learning/plan/items",
                        json={
                            "user_id": "u1",
                            "plan_date": "2026-07-23",
                            "title": "Scoped plan item",
                            "learning_scope": scope,
                        },
                    )
                    self.assertEqual(resp.status_code, 200)
                    body = resp.json()
                    self.assertTrue(body["ok"])
                    item = body["result"]["item"]
                    self.assertEqual(item["learning_scope"], scope)
                    self.assertEqual(
                        item["learning_scope"]["book_title"], "牙周病学"
                    )
                finally:
                    learning_orchestrator.memory = original_memory
            finally:
                memory._conn.close()

    def test_create_item_without_scope_returns_empty_dict(self) -> None:
        """POST items without learning_scope returns {} (backward compatible)."""
        import tempfile
        from pathlib import Path
        from backend.services.learning.memory import LearningMemory

        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                from backend.main import learning_orchestrator
                original_memory = learning_orchestrator.memory
                learning_orchestrator.memory = memory
                try:
                    resp = self.client.post(
                        "/api/learning/plan/items",
                        json={
                            "user_id": "u1",
                            "plan_date": "2026-07-23",
                            "title": "Old-style item",
                        },
                    )
                    self.assertEqual(resp.status_code, 200)
                    body = resp.json()
                    self.assertTrue(body["ok"])
                    item = body["result"]["item"]
                    self.assertEqual(item["learning_scope"], {})
                finally:
                    learning_orchestrator.memory = original_memory
            finally:
                memory._conn.close()

    def test_read_item_includes_scope(self) -> None:
        """GET day endpoint returns learning_scope in each item."""
        import tempfile
        from pathlib import Path
        from backend.services.learning.memory import LearningMemory

        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                scope = {"book_title": "Test", "kg_node_ids": ["n1"]}
                memory.create_study_plan_item(
                    user_id="u1",
                    plan_date="2026-07-23",
                    title="Read test item",
                    learning_scope=scope,
                )
                from backend.main import learning_orchestrator
                original_memory = learning_orchestrator.memory
                learning_orchestrator.memory = memory
                try:
                    resp = self.client.get(
                        "/api/learning/plan/u1/day/2026-07-23",
                    )
                    self.assertEqual(resp.status_code, 200)
                    body = resp.json()
                    self.assertTrue(body["ok"])
                    items = body["result"]["items"]
                    self.assertEqual(len(items), 1)
                    self.assertEqual(items[0]["learning_scope"], scope)
                finally:
                    learning_orchestrator.memory = original_memory
            finally:
                memory._conn.close()

    def test_update_item_with_scope(self) -> None:
        """POST update with learning_scope stores and returns the new scope."""
        import tempfile
        from pathlib import Path
        from backend.services.learning.memory import LearningMemory

        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                item = memory.create_study_plan_item(
                    user_id="u1",
                    plan_date="2026-07-23",
                    title="Original",
                    learning_scope={"book_title": "Old", "kg_node_ids": ["old"]},
                )
                from backend.main import learning_orchestrator
                original_memory = learning_orchestrator.memory
                learning_orchestrator.memory = memory
                try:
                    new_scope = {"book_title": "New", "kg_node_ids": ["new1"]}
                    resp = self.client.post(
                        f"/api/learning/plan/items/{item['id']}/update",
                        json={
                            "user_id": "u1",
                            "learning_scope": new_scope,
                        },
                    )
                    self.assertEqual(resp.status_code, 200)
                    body = resp.json()
                    self.assertTrue(body["ok"])
                    updated = body["result"]["item"]
                    self.assertEqual(updated["learning_scope"], new_scope)
                finally:
                    learning_orchestrator.memory = original_memory
            finally:
                memory._conn.close()

    def test_month_endpoint_includes_scope(self) -> None:
        """GET month endpoint returns learning_scope in each item."""
        import tempfile
        from pathlib import Path
        from backend.services.learning.memory import LearningMemory

        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                scope = {"book_title": "Month Book", "kg_node_ids": ["m1"]}
                memory.create_study_plan_item(
                    user_id="u1",
                    plan_date="2026-07-23",
                    title="Month item",
                    learning_scope=scope,
                )
                from backend.main import learning_orchestrator
                original_memory = learning_orchestrator.memory
                learning_orchestrator.memory = memory
                try:
                    resp = self.client.get(
                        "/api/learning/plan/u1/month",
                        params={"year": 2026, "month": 7},
                    )
                    self.assertEqual(resp.status_code, 200)
                    body = resp.json()
                    self.assertTrue(body["ok"])
                    items = body["result"]["items"]
                    self.assertEqual(len(items), 1)
                    self.assertEqual(items[0]["learning_scope"], scope)
                finally:
                    learning_orchestrator.memory = original_memory
            finally:
                memory._conn.close()

    # ── regression: deterministic recommend unchanged ────────────────────

    def test_empty_request_text_still_returns_deterministic(self) -> None:
        """When request_text is omitted, the existing deterministic
        recommendation path is preserved (no draft is created)."""
        from backend.services.learning.memory import LearningMemory
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                result = recommend_study_plan(
                    memory,
                    user_id="u1",
                    plan_date="2026-07-23",
                    # request_text omitted — deterministic
                )
                self.assertEqual(result["mode"], "preview_recommendations")
                self.assertIn("trace", result)
                self.assertNotIn("plan_draft_id", result)
            finally:
                memory._conn.close()

    # ── HTTP-level: recommend endpoint without request_text ─────────────

    def test_recommend_endpoint_without_request_text_is_deterministic(self) -> None:
        """POST /api/learning/plan/recommend without request_text returns
        preview_recommendations mode, no draft ID, no side effects on the
        draft store."""
        import tempfile
        from pathlib import Path
        from backend.services.learning.memory import LearningMemory

        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                from backend.main import learning_orchestrator
                original_memory = learning_orchestrator.memory
                learning_orchestrator.memory = memory
                try:
                    resp = self.client.post(
                        "/api/learning/plan/recommend",
                        json={
                            "user_id": "u1",
                            "plan_date": "2026-07-23",
                            # request_text intentionally omitted
                        },
                    )
                    self.assertEqual(resp.status_code, 200)
                    body = resp.json()
                    self.assertTrue(body["ok"])
                    result = body["result"]
                    self.assertEqual(result["mode"], "preview_recommendations")
                    self.assertNotIn("plan_draft_id", result)
                    self.assertIn("recommendations", result)
                    self.assertEqual(
                        result["trace"]["strategy"],
                        "deterministic_dual_mode_study_plan",
                    )
                finally:
                    learning_orchestrator.memory = original_memory
            finally:
                memory._conn.close()

    def test_recommend_endpoint_empty_request_text_is_deterministic(self) -> None:
        """POST /api/learning/plan/recommend with empty/whitespace
        request_text still enters deterministic mode — no draft created."""
        import tempfile
        from pathlib import Path
        from backend.services.learning.memory import LearningMemory

        with tempfile.TemporaryDirectory() as tmp:
            memory = LearningMemory(Path(tmp) / "learning.sqlite3")
            try:
                from backend.main import learning_orchestrator
                original_memory = learning_orchestrator.memory
                learning_orchestrator.memory = memory
                try:
                    for empty_text in ("", "   ", "\t\n"):
                        resp = self.client.post(
                            "/api/learning/plan/recommend",
                            json={
                                "user_id": "u1",
                                "plan_date": "2026-07-23",
                                "request_text": empty_text,
                            },
                        )
                        self.assertEqual(resp.status_code, 200)
                        body = resp.json()
                        self.assertTrue(body["ok"])
                        result = body["result"]
                        self.assertEqual(
                            result["mode"], "preview_recommendations"
                        )
                        self.assertNotIn("plan_draft_id", result)
                finally:
                    learning_orchestrator.memory = original_memory
            finally:
                memory._conn.close()
