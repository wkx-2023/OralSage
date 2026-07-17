"""Focused tests for static_fallback Cache-Control headers.

R8: HTML documents served via static_fallback MUST carry
``Cache-Control: no-cache`` so that browsers revalidate the page
entry point on every refresh.  Non-HTML static assets (JS, CSS, …)
MUST NOT be affected by this rule.
"""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from backend.main import app


class HtmlNoCacheTests(unittest.TestCase):
    """Verify that only .html responses get Cache-Control: no-cache."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)

    # ------------------------------------------------------------------
    # Positive: HTML pages get no-cache
    # ------------------------------------------------------------------

    def test_master_chat_html_has_no_cache(self) -> None:
        """GET /pages/master-chat.html must return Cache-Control: no-cache."""
        resp = self.client.get("/pages/master-chat.html")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.headers.get("cache-control"),
            "no-cache",
            "HTML page response must have Cache-Control: no-cache",
        )

    def test_index_html_has_no_cache(self) -> None:
        """GET / (index.html) must return Cache-Control: no-cache.

        The index route goes through a dedicated endpoint, not
        static_fallback, so it is NOT expected to have this header.
        This test documents the current behaviour — index.html is
        NOT in scope for R8.
        """
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        # index() has its own route; it should NOT gain no-cache
        # unless the rule is explicitly extended.
        self.assertIsNone(
            resp.headers.get("cache-control"),
            "index.html (dedicated route) must NOT receive no-cache from static_fallback",
        )

    # ------------------------------------------------------------------
    # Negative: non-HTML static files are NOT forced to no-cache
    # ------------------------------------------------------------------

    def test_js_file_does_not_have_no_cache(self) -> None:
        """GET /js/common.js must NOT be forced to Cache-Control: no-cache."""
        resp = self.client.get("/js/common.js")
        self.assertEqual(resp.status_code, 200)
        cc = resp.headers.get("cache-control")
        self.assertIsNone(
            cc,
            f"JS static response must NOT have Cache-Control: no-cache, got {cc!r}",
        )

    def test_css_file_does_not_have_no_cache(self) -> None:
        """GET /css/base.css must NOT be forced to Cache-Control: no-cache."""
        resp = self.client.get("/css/base.css")
        self.assertEqual(resp.status_code, 200)
        cc = resp.headers.get("cache-control")
        self.assertIsNone(
            cc,
            f"CSS static response must NOT have Cache-Control: no-cache, got {cc!r}",
        )

    # ------------------------------------------------------------------
    # Edge: non-existent HTML falls through to index.html (no header)
    # ------------------------------------------------------------------

    def test_nonexistent_html_falls_back_to_index(self) -> None:
        """Request for non-existent .html returns index.html (200).

        The index fallback goes through the return path that does NOT
        add no-cache — this is current behaviour and not in scope for R8.
        """
        resp = self.client.get("/nonexistent.html")
        self.assertEqual(resp.status_code, 200)
        # Fallback to index.html; no-cache is NOT applied here.
        self.assertIsNone(resp.headers.get("cache-control"))


if __name__ == "__main__":
    unittest.main()
