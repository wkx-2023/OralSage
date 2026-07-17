from __future__ import annotations

import json
import os
import unittest
from io import BytesIO
from urllib.error import HTTPError
from unittest.mock import patch

from backend.services.learning.tutor import _TeachingTextStreamParser
from backend.services.llm_client import (
    LLMRequestError,
    chat_completion,
    classify_llm_error,
)


class _StreamingResponse:
    headers = {"Content-Type": "text/event-stream; charset=utf-8"}

    def __init__(self, lines: list[bytes]) -> None:
        self.lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def __iter__(self):
        return iter(self.lines)


class _JsonResponse:
    headers = {"Content-Type": "application/json"}

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class LlmStreamingTests(unittest.TestCase):
    def test_chat_completion_forwards_sse_content_deltas(self) -> None:
        captured = []
        response = _StreamingResponse([
            b'data: {"choices":[{"delta":{"content":"hello "}}]}\n',
            b'data: {"choices":[{"delta":{"content":"world"}}]}\n',
            b'data: [DONE]\n',
        ])
        with (
            patch.dict(os.environ, {"QWEN_API_KEY": "test-key"}),
            patch("backend.services.llm_client.urlopen", return_value=response) as mocked,
        ):
            result = chat_completion(
                [{"role": "user", "content": "test"}],
                stream_callback=captured.append,
            )

        request = mocked.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertTrue(payload["stream"])
        self.assertEqual(captured, ["hello ", "world"])
        self.assertEqual(result, "hello world")

    def test_chat_completion_retries_without_json_mode_on_400(self) -> None:
        calls = []

        def fake_urlopen(request, timeout):
            calls.append(json.loads(request.data.decode("utf-8")))
            if len(calls) == 1:
                raise HTTPError(
                    request.full_url,
                    400,
                    "Bad Request",
                    hdrs=None,
                    fp=BytesIO(b"response_format unsupported"),
                )
            return _JsonResponse({
                "choices": [{"message": {"content": "{\"ok\": true}"}}],
            })

        with (
            patch.dict(os.environ, {"QWEN_API_KEY": "test-key"}),
            patch("backend.services.llm_client.urlopen", side_effect=fake_urlopen),
        ):
            result = chat_completion(
                [{"role": "user", "content": "test"}],
                response_format={"type": "json_object"},
            )

        self.assertEqual(result, "{\"ok\": true}")
        self.assertIn("response_format", calls[0])
        self.assertNotIn("response_format", calls[1])

    def test_http_error_body_remains_available_for_classification(self) -> None:
        def fake_urlopen(request, timeout):
            raise HTTPError(
                request.full_url,
                422,
                "Unprocessable Entity",
                hdrs=None,
                fp=BytesIO(b"response_format is not supported"),
            )

        with (
            patch.dict(os.environ, {"QWEN_API_KEY": "test-key"}),
            patch("backend.services.llm_client.urlopen", side_effect=fake_urlopen),
            self.assertRaises(LLMRequestError) as raised,
        ):
            chat_completion(
                [{"role": "user", "content": "test"}],
                response_format={"type": "json_object"},
            )

        self.assertEqual(classify_llm_error(raised.exception), "unsupported")

    def test_chat_completion_uses_explicit_timeout(self) -> None:
        observed_timeouts = []

        def fake_urlopen(_request, timeout):
            observed_timeouts.append(timeout)
            return _JsonResponse({
                "choices": [{"message": {"content": "ok"}}],
            })

        with (
            patch.dict(os.environ, {"QWEN_API_KEY": "test-key"}),
            patch("backend.services.llm_client.urlopen", side_effect=fake_urlopen),
        ):
            result = chat_completion(
                [{"role": "user", "content": "test"}],
                timeout=7,
            )

        self.assertEqual(result, "ok")
        self.assertEqual(observed_timeouts, [7])

    def test_tutor_parser_only_emits_teaching_text(self) -> None:
        chunks = []
        parser = _TeachingTextStreamParser(chunks.append)
        for delta in (
            '{"tutor_status":"llm_generated","teaching_',
            'text":"第一段讲解。\\n',
            '第二段讲解。","questions":[{"stem":"不应输出"}]}',
        ):
            parser.feed(delta)

        self.assertEqual("".join(chunks), "第一段讲解。\n第二段讲解。")
        self.assertNotIn("不应输出", "".join(chunks))


if __name__ == "__main__":
    unittest.main()
