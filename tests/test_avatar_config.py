"""Tests for the public iFLYTEK avatar configuration endpoint."""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.main import app
from backend.services.avatar_service import get_public_avatar_config


class AvatarConfigurationTests(unittest.TestCase):
    def test_public_config_contains_selection_and_never_a_secret(self) -> None:
        with patch.dict(os.environ, {
            "XF_AVATAR_ENABLED": "true",
            "XF_AVATAR_APP_ID": "avatar-app",
            "XF_AVATAR_API_SECRET": "never-public",
            "XF_AVATAR_SDK_URL": "https://cdn.example/avatar-sdk.js",
            "XF_AVATAR_SCENE_ID": "scene-123",
            "XF_AVATAR_ID": "138805001",
            "XF_AVATAR_VCN": "x4_lingxiaoqi_oral",
        }, clear=False):
            payload = get_public_avatar_config()

        self.assertTrue(payload["configured"])
        self.assertEqual(payload["avatar_id"], "138805001")
        self.assertEqual(payload["vcn"], "x4_lingxiaoqi_oral")
        self.assertNotIn("api_secret", payload)
        self.assertNotIn("never-public", str(payload))
        self.assertIn("signed_url", payload)
        self.assertTrue(payload["sdk_available"])

    def test_endpoint_returns_config(self) -> None:
        with patch.dict(os.environ, {
            "XF_AVATAR_ENABLED": "true",
            "XF_AVATAR_APP_ID": "avatar-app",
            "XF_AVATAR_SCENE_ID": "scene-123",
            "XF_AVATAR_API_KEY": "key",
            "XF_AVATAR_API_SECRET": "secret",
            "XF_AVATAR_SDK_URL": "https://cdn.example/avatar-sdk.js",
        }, clear=False):
            response = TestClient(app).get("/api/avatar/config")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["result"]["avatar_id"], "138805001")

    def test_signed_url_uses_web_api_handshake_without_exposing_secret(self) -> None:
        with patch.dict(os.environ, {
            "XF_AVATAR_SERVER_URL": "wss://avatar.example.test/v1/interact",
            "XF_AVATAR_API_KEY": "key",
            "XF_AVATAR_API_SECRET": "secret",
        }, clear=False):
            from backend.services.avatar_service import XfyunAvatarConfig

            signed_url = XfyunAvatarConfig.from_env().signed_url(
                datetime(2026, 7, 29, 0, 0, tzinfo=timezone.utc)
            )

        self.assertTrue(signed_url.startswith("wss://avatar.example.test/v1/interact?"))
        self.assertIn("authorization=", signed_url)
        self.assertNotIn("secret", signed_url)
