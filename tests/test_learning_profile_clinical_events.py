import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class LearningProfileClinicalEventTests(unittest.TestCase):
    def test_clinical_event_types_have_dedicated_timeline_presentations(self) -> None:
        script = (ROOT / "frontend" / "js" / "learning-profile.js").read_text(encoding="utf-8")

        expected = {
            "clinical_speaking_started": "开始问诊模拟",
            "clinical_diagnosis_confirmed": "完成诊断",
            "clinical_speaking_answer_assessed": "完成表达考核",
            "clinical_speaking_completed": "完成问诊模拟",
        }
        for event_type, label in expected.items():
            self.assertIn(f"{event_type}: '{label}'", script)
            self.assertIn(f"event.event_type === '{event_type}'", script)

        self.assertGreaterEqual(script.count("category: 'clinical'"), 4)

    def test_learning_profile_has_a_clinical_filter(self) -> None:
        page = (ROOT / "frontend" / "pages" / "learning-profile.html").read_text(encoding="utf-8")

        self.assertIn('data-filter="clinical">问诊</button>', page)


if __name__ == "__main__":
    unittest.main()
