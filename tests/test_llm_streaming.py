from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from backend.services.learning.tutor import _TeachingTextStreamParser
from backend.services.llm_client import chat_completion


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
