from __future__ import annotations

import asyncio
import unittest

from backend.services.router.jobs import RouterJobManager


class _LearningRouter:
    async def route(self, **_kwargs):
        await asyncio.sleep(0.01)
        return {
            "ok": True,
            "route": {
                "intent": "learning",
                "target": "learning_orchestrator",
                "source": "llm_router",
                "reason": "test",
            },
            "final_result": {
                "session": {"id": "lesson-test", "topic": "牙周炎", "user_id": "u1"},
                "outline": {
                    "title": "牙周炎",
                    "nodes": [{"id": "node-1", "title": "病因与机制", "secret": "drop"}],
                },
                "mindmap": {
                    "format": "markmap_json",
                    "root": {
                        "name": "牙周炎",
                        "children": [{"id": "node-1", "name": "病因与机制"}],
                    },
                },
                "evidence_pool": {"large": "must not reach chat"},
                "current_outline_node_id": "node-1",
            },
            "next_action": "start_session",
        }


class _LearningOrchestrator:
    def next_step(self, session_id, stream_callback=None):
        if stream_callback is not None:
            stream_callback("这是本节")
            stream_callback("有效讲稿。")
        return {
            "session_id": session_id,
            "outline_node_id": "node-1",
            "teaching_text": "这是本节有效讲稿。",
            "questions": [],
            "content_status": "llm_generated",
            "internal_trace": {"drop": True},
        }

    def wait_for_ppt_resources(self, session_id, *, timeout_seconds):
        return {"session_id": session_id, "status": "pending", "timeout": timeout_seconds}


class _ContinueRouter:
    async def route(self, **_kwargs):
        return {
            "ok": True,
            "route": {
                "intent": "learning",
                "target": "learning_orchestrator",
                "source": "explicit_recommendation",
                "reason": "continue selected recommendation",
            },
            "final_result": {
                "session": {"id": "lesson-existing", "topic": "口腔溃疡", "user_id": "u1"},
                "tutor_step": {
                    "session_id": "lesson-existing",
                    "outline_node_id": "node-2",
                    "teaching_text": "已经推进后的第二节。",
                    "questions": [],
                },
                "lesson_completed": False,
            },
            "next_action": "continue_session",
        }


class _ContinueOrchestrator(_LearningOrchestrator):
    def next_step(self, session_id, stream_callback=None):
        raise AssertionError("continued plan task must not advance the session twice")


class RouterJobManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_learning_job_emits_replayable_compact_result(self):
        manager = RouterJobManager(_LearningRouter(), _LearningOrchestrator())
        created = manager.create(
            message="学习牙周炎",
            user_id="u1",
            history=[],
            context={"user_id": "u1"},
        )
        job_id = created["job_id"]

        for _ in range(100):
            snapshot = manager.snapshot(job_id)
            if snapshot["done"]:
                break
            await asyncio.sleep(0.01)
        else:
            self.fail("router job did not complete")

        self.assertEqual(snapshot["status"], "complete")
        event_types = [event["type"] for event in snapshot["events"]]
        self.assertIn("route_ready", event_types)
        self.assertIn("content_delta", event_types)
        self.assertEqual(event_types[-1], "complete")

        result = snapshot["events"][-1]["data"]
        router_result = result["router_result"]
        self.assertNotIn("evidence_pool", router_result["final_result"])
        self.assertEqual(
            router_result["final_result"]["outline"]["nodes"],
            [{"id": "node-1", "title": "病因与机制"}],
        )
        self.assertEqual(
            router_result["final_result"]["mindmap"]["root"]["children"][0],
            {"id": "node-1", "name": "病因与机制"},
        )
        self.assertEqual(result["learning_step"]["teaching_text"], "这是本节有效讲稿。")
        self.assertNotIn("internal_trace", result["learning_step"])

        last_id = snapshot["events"][-2]["id"]
        replay = manager.snapshot(job_id, after=last_id)
        self.assertEqual(len(replay["events"]), 1)
        self.assertEqual(replay["events"][0]["type"], "complete")

    async def test_continued_recommendation_does_not_advance_twice(self):
        manager = RouterJobManager(_ContinueRouter(), _ContinueOrchestrator())
        created = manager.create(
            message="",
            user_id="u1",
            history=[],
            context={"user_id": "u1", "recommendation_id": "continue:test"},
        )

        for _ in range(100):
            snapshot = manager.snapshot(created["job_id"])
            if snapshot["done"]:
                break
            await asyncio.sleep(0.01)
        else:
            self.fail("router job did not complete")

        self.assertEqual(snapshot["status"], "complete")
        result = snapshot["events"][-1]["data"]
        self.assertEqual(result["learning_step"]["outline_node_id"], "node-2")
        self.assertEqual(result["learning_step"]["teaching_text"], "已经推进后的第二节。")


if __name__ == "__main__":
    unittest.main()
