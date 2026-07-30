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

    def test_chat_completion_ignores_reasoning_only_sse_deltas(self) -> None:
        captured = []
        response = _StreamingResponse([
            b'data: {"choices":[{"delta":{"reasoning_content":"private reasoning"}}]}\n',
            b'\n',
            b'data: {"choices":[{"delta":{"content":"{\\"ok\\":"}}]}\n',
            b'data: {"choices":[{"delta":{"content":"true}"}}]}\n',
            b'data: [DONE]\n',
        ])
        with (
            patch.dict(os.environ, {"QWEN_API_KEY": "test-key"}),
            patch("backend.services.llm_client.urlopen", return_value=response),
        ):
            result = chat_completion(
                [{"role": "user", "content": "test"}],
                stream_callback=captured.append,
            )

        self.assertEqual(result, '{"ok":true}')
        self.assertEqual(captured, ['{"ok":', "true}"])

    def test_chat_completion_rejects_reasoning_only_sse_response(self) -> None:
        response = _StreamingResponse([
            b'data: {"choices":[{"delta":{"reasoning_content":"private reasoning"}}]}\n',
            b'data: {"choices":[{"delta":{"content":""},"finish_reason":"stop"}]}\n',
            b'data: [DONE]\n',
        ])
        with (
            patch.dict(os.environ, {"QWEN_API_KEY": "test-key"}),
            patch("backend.services.llm_client.urlopen", return_value=response),
            self.assertRaises(LLMRequestError) as raised,
        ):
            chat_completion(
                [{"role": "user", "content": "test"}],
                stream_callback=lambda _delta: None,
            )

        self.assertIn("no text content", str(raised.exception))

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

    def test_http_429_is_classified_as_retryable_rate_limit(self) -> None:
        request_error = LLMRequestError("HTTP Error 429: Too Many Requests")
        request_error.__cause__ = HTTPError(
            "https://example.test/v2/chat/completions",
            429,
            "Too Many Requests",
            hdrs=None,
            fp=BytesIO(b'{"message":"rate limited"}'),
        )

        self.assertEqual(classify_llm_error(request_error), "rate_limit")

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

    def test_chat_completion_sends_xfyun_reasoning_request_to_v2_endpoint(self) -> None:
        response = _JsonResponse({
            "choices": [{"message": {"content": '{"ok": true}'}}],
        })
        clinical_env = {
            "CLINICAL_SPEAKING_LLM_API_KEY": "test-key",
            "CLINICAL_SPEAKING_LLM_BASE_URL": "https://maas-api.cn-huabei-1.xf-yun.com/v2",
            "CLINICAL_SPEAKING_LLM_MODEL": "xsparkx2flash",
        }
        with (
            patch.dict(os.environ, clinical_env),
            patch("backend.services.llm_client.urlopen", return_value=response) as mocked,
        ):
            result = chat_completion(
                [{"role": "user", "content": "test"}],
                api_key_names=("CLINICAL_SPEAKING_LLM_API_KEY",),
                base_url_names=("CLINICAL_SPEAKING_LLM_BASE_URL",),
                model_names=("CLINICAL_SPEAKING_LLM_MODEL",),
                extra_body={"search_disable": True},
            )

        request = mocked.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(
            request.full_url,
            "https://maas-api.cn-huabei-1.xf-yun.com/v2/chat/completions",
        )
        self.assertEqual(payload["model"], "xsparkx2flash")
        self.assertNotIn("response_format", payload)
        self.assertTrue(payload["search_disable"])
        self.assertEqual(request.headers["Authorization"], "Bearer test-key")
        self.assertEqual(result, '{"ok": true}')

    def test_chat_completion_rejects_extra_body_core_field_overrides(self) -> None:
        with (
            patch.dict(os.environ, {"QWEN_API_KEY": "test-key"}),
            self.assertRaises(ValueError),
        ):
            chat_completion(
                [{"role": "user", "content": "test"}],
                extra_body={"model": "unexpected-model"},
            )

    def test_chat_completion_does_not_duplicate_full_endpoint_suffix(self) -> None:
        response = _JsonResponse({
            "choices": [{"message": {"content": "ok"}}],
        })
        with (
            patch.dict(
                os.environ,
                {
                    "QWEN_API_KEY": "test-key",
                    "QWEN_BASE_URL": "https://example.test/v1/chat/completions",
                },
            ),
            patch("backend.services.llm_client.urlopen", return_value=response) as mocked,
        ):
            chat_completion(
                [{"role": "user", "content": "test"}],
                base_url_names=("QWEN_BASE_URL",),
            )

        self.assertEqual(
            mocked.call_args.args[0].full_url,
            "https://example.test/v1/chat/completions",
        )

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
