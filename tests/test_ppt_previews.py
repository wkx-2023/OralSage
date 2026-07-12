from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.services.learning.ppt_pipeline import (
    _upgrade_legacy_cover_plan,
    build_and_save_pin_and_paper_plan,
    ensure_pin_and_paper_previews,
)


class PptPreviewTests(unittest.TestCase):
    def test_legacy_preview_plan_upgrades_cover_from_saved_deck(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            session_dir = Path(temp) / "lesson-legacy"
            slides_dir = session_dir / "slides"
            slides_dir.mkdir(parents=True)
            plan_path = slides_dir / "pin_and_paper_plan.v1.json"
            plan = {
                "deck_title": "我想学习牙周炎系统学习路径",
                "slides": [{"page_type": "cover", "slots": {"title": "我想学习牙周炎系统学习路径"}}],
                "warnings": [],
            }
            plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
            (slides_dir / "slides.v1.json").write_text(json.dumps({
                "deck_title": "我想学习牙周炎系统学习路径",
                "slides": [{"outline_node_id": "node-1", "title": "定义与分期"}],
            }, ensure_ascii=False), encoding="utf-8")

            rewritten = {
                "title": "牙周炎：组织破坏与诊疗策略",
                "subtitle_line1": "识别炎症与附着丧失",
                "subtitle_line2": "建立分期治疗框架",
            }
            with patch(
                "backend.services.learning.ppt_pipeline._build_cover_copy",
                return_value=(rewritten, [], 1),
            ):
                upgraded = _upgrade_legacy_cover_plan(
                    session_dir=session_dir,
                    plan_path=plan_path,
                    plan=plan,
                )

            self.assertEqual(upgraded["cover_title"], rewritten["title"])
            self.assertEqual(upgraded["slides"][0]["slots"]["title"], rewritten["title"])
            persisted = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["cover_title"], rewritten["title"])

    def test_cover_title_is_rewritten_by_llm_and_cached(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            calls: list[str] = []

            def fake_llm(prompt: str) -> str:
                calls.append(prompt)
                return json.dumps({
                    "title": "口腔溃疡：复发机制与诊疗决策",
                    "subtitle_line1": "辨清病因与临床表型",
                    "subtitle_line2": "建立分层诊疗框架",
                }, ensure_ascii=False)

            kwargs = {
                "session_id": "lesson-cover",
                "outline": {
                    "title": "我想学习口腔溃疡系统学习路径",
                    "nodes": [{"id": "node-1", "title": "定义与疾病本质"}],
                },
                "deck": {
                    "deck_title": "我想学习口腔溃疡系统学习路径",
                    "slides": [],
                    "warnings": [],
                },
                "resource_root": Path(temp),
                "llm_client": fake_llm,
            }
            first = build_and_save_pin_and_paper_plan(**kwargs)
            second = build_and_save_pin_and_paper_plan(**kwargs)

            self.assertEqual(len(calls), 1)
            self.assertEqual(first["cover_title"], "口腔溃疡：复发机制与诊疗决策")
            self.assertEqual(
                first["slides"][0]["slots"]["title"],
                "口腔溃疡：复发机制与诊疗决策",
            )
            self.assertEqual(
                first["slides"][0]["slots"]["subtitle_line1"],
                "辨清病因与临床表型",
            )
            self.assertEqual(second["cover_title"], first["cover_title"])

    def test_real_renderer_pages_are_persisted_and_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            session_id = "lesson-preview"
            plan_path = root / session_id / "slides" / "pin_and_paper_plan.v1.json"
            plan_path.parent.mkdir(parents=True)
            plan = {
                "schema_version": "pin_and_paper_slide_plan.v1",
                "template_slug": "pin-and-paper",
                "deck_title": "牙周炎",
                "session_id": session_id,
                "status": "ready",
                "slides": [
                    {
                        "source_slide_id": "cover",
                        "source_outline_node_id": "",
                        "page_type": "cover",
                        "slots": {"title": "牙周炎"},
                    },
                    {
                        "source_slide_id": "slide-node-1",
                        "source_outline_node_id": "node-1",
                        "page_type": "notes",
                        "slots": {"headline": "病因与机制", "cards": []},
                    },
                ],
            }
            plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")

            calls = 0

            def fake_renderer(*, output_dir: Path, slide_count: int, **_kwargs):
                nonlocal calls
                calls += 1
                images = []
                for index in range(slide_count):
                    path = output_dir / f"slide_{index + 1:03d}.png"
                    path.write_bytes(b"fake-rendered-page" * 100)
                    images.append(path)
                return images

            with patch(
                "backend.services.learning.ppt_pipeline._render_plan_to_pngs",
                side_effect=fake_renderer,
            ):
                first = ensure_pin_and_paper_previews(
                    session_id=session_id,
                    resource_root=root,
                )
                second = ensure_pin_and_paper_previews(
                    session_id=session_id,
                    resource_root=root,
                )

            self.assertEqual(calls, 1)
            self.assertEqual(first["status"], "ready")
            self.assertEqual(first["slide_count"], 2)
            self.assertEqual(first["slides"][1]["source_outline_node_id"], "node-1")
            self.assertEqual(first["slides"][1]["page_type"], "notes")
            self.assertEqual(second["plan_hash"], first["plan_hash"])
            for slide in first["slides"]:
                image = (
                    root
                    / session_id
                    / "slides"
                    / "rendered_ppt"
                    / slide["relative_path"]
                )
                self.assertTrue(image.is_file())


if __name__ == "__main__":
    unittest.main()
