import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FrontendNavigationTests(unittest.TestCase):
    def test_global_navigation_has_the_requested_order(self) -> None:
        common = (ROOT / "frontend" / "js" / "common.js").read_text(encoding="utf-8")
        nav_block = re.search(r"const NAV_ITEMS = \[(.*?)\n  \];", common, re.DOTALL)

        self.assertIsNotNone(nav_block)
        names = re.findall(r"name: (?:'([^']*)'|\"([^\"]*)\")", nav_block.group(1))
        decoded = [
            bytes(first or second, "utf-8").decode("unicode_escape")
            if "\\u" in (first or second)
            else first or second
            for first, second in names
        ]
        self.assertEqual(
            decoded,
            ["总控工作台", "智能问答", "问诊模拟", "学习计划", "学习画像", "知识图谱"],
        )
        self.assertNotIn("/pages/evidence.html", nav_block.group(1))

    def test_home_moves_evidence_into_the_toolbox(self) -> None:
        home = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        feature_area = re.search(
            r'<div class="feature-list">(.*?)</div>\s*</section>',
            home,
            re.DOTALL,
        )

        self.assertIsNotNone(feature_area)
        self.assertNotIn('href="/pages/evidence.html"', feature_area.group(1))
        self.assertEqual(feature_area.group(1).count('href="/pages/clinical-speaking.html"'), 1)
        self.assertIn('data-tool-url="/pages/evidence.html"', home)

    def test_digital_human_layers_are_transparent(self) -> None:
        css = (ROOT / "frontend" / "css" / "clinical-speaking.css").read_text(encoding="utf-8")

        for selector in (".patient-pane", ".patient-avatar-stage", ".avatar-loading-state"):
            block = re.search(rf"{re.escape(selector)}\s*\{{(.*?)\}}", css, re.DOTALL)
            self.assertIsNotNone(block)
            self.assertIn("background: transparent", block.group(1))

    def test_master_chat_does_not_override_the_global_navigation(self) -> None:
        css = (ROOT / "frontend" / "css" / "master-chat.css").read_text(encoding="utf-8")

        self.assertNotIn(".chat-main-area .topbar", css)


if __name__ == "__main__":
    unittest.main()
