"""Regression coverage for iFLYTEK digital-human video task handling."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.services.learning.avatar_video import (
    XfyunVideoConfig,
    _payload_url,
    _script_prompt,
    _signed_url,
    create_avatar_video,
    get_avatar_video_record,
    refresh_avatar_video,
)


VIDEO_ENV = {
    "XF_VIDEO_APP_ID": "video-app",
    "XF_VIDEO_API_KEY": "video-key",
    "XF_VIDEO_API_SECRET": "video-secret",
}


class AvatarVideoTests(unittest.TestCase):
    def test_unsigned_configuration_never_starts_a_task(self) -> None:
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {
            "XF_VIDEO_APP_ID": "",
            "XF_VIDEO_API_KEY": "",
            "XF_VIDEO_API_SECRET": "",
        }, clear=False):
            record = get_avatar_video_record("lesson_1", "node_1", root)

        self.assertEqual(record["status"], "not_configured")
        self.assertFalse(record["configured"])

    def test_signed_url_does_not_expose_api_secret(self) -> None:
        with patch.dict(os.environ, VIDEO_ENV, clear=False):
            signed = _signed_url(
                "https://vms.cn-huadong-1.xf-yun.com/v1/private/video/generate",
                XfyunVideoConfig.from_env(),
            )

        self.assertIn("authorization=", signed)
        self.assertNotIn("video-secret", signed)

    def test_prompt_is_limited_to_provider_maximum(self) -> None:
        prompt, word_count = _script_prompt({
            "slides": [{"narration": "第一句讲稿。" + "补充内容。" * 200}],
        })

        self.assertLessEqual(len(prompt.encode("utf-8")), 300)
        self.assertEqual(word_count, 300)

    def test_payload_url_decodes_nested_provider_video(self) -> None:
        encoded_url = "aHR0cHM6Ly9tZWRpYS5leGFtcGxlL3ZpZGVvLm1wNA=="

        resolved = _payload_url({"video": {"video": encoded_url}}, "video")

        self.assertEqual(resolved, "https://media.example/video.mp4")

    def test_create_and_refresh_persist_completed_video(self) -> None:
        script = {
            "slide_count": 1,
            "updated_at": "2026-07-29T00:00:00+00:00",
            "slides": [{"narration": "这是一段用于测试的口腔医学讲稿。"}],
        }
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, VIDEO_ENV, clear=False), patch(
            "backend.services.learning.avatar_video.get_lecture_script", return_value=script
        ), patch(
            "backend.services.learning.avatar_video._post",
            return_value={"header": {"code": 0, "task_id": "video-task-1"}},
        ):
            created = create_avatar_video("lesson_1", "node_1", root)
            with patch(
                "backend.services.learning.avatar_video._post",
                return_value={
                    "header": {"code": 0, "task_status": "4"},
                    "payload": {
                        "video": "https://media.example/video.mp4",
                        "audio": "https://media.example/audio.wav",
                        "text": "生成后的讲解文字。",
                    },
                },
            ):
                completed = refresh_avatar_video("lesson_1", "node_1", root)
            record_path = Path(root) / "lesson_1" / "avatar_videos" / "node_1.json"
            persisted = json.loads(record_path.read_text(encoding="utf-8"))

        self.assertEqual(created["status"], "created")
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["video_url"], "https://media.example/video.mp4")
        self.assertEqual(persisted["text"], "生成后的讲解文字。")


if __name__ == "__main__":
    unittest.main()
