"""Regression tests for the frontend no-store response policy."""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from backend.main import app


EXPECTED_CACHE_CONTROL = "no-store, no-cache, must-revalidate, max-age=0"


class FrontendNoStoreTests(unittest.TestCase):
    """Every file served by the frontend fallback must bypass stale caches."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)

    def assert_no_store(self, path: str) -> None:
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("cache-control"), EXPECTED_CACHE_CONTROL)
        self.assertEqual(response.headers.get("pragma"), "no-cache")
        self.assertEqual(response.headers.get("expires"), "0")

    def test_master_chat_html_has_no_store(self) -> None:
        self.assert_no_store("/pages/master-chat.html")

    def test_index_html_has_no_store(self) -> None:
        self.assert_no_store("/")

    def test_javascript_has_no_store(self) -> None:
        self.assert_no_store("/js/common.js")

    def test_css_has_no_store(self) -> None:
        self.assert_no_store("/css/base.css")

    def test_missing_route_index_fallback_has_no_store(self) -> None:
        self.assert_no_store("/nonexistent.html")


if __name__ == "__main__":
    unittest.main()
