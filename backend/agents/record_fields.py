from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any


MISSING = "未提及"


@dataclass
class RecordFields:
    name: str = MISSING
    sex: str = MISSING
    age: str = MISSING
    chief_complaint: str = MISSING
    present_illness: str = MISSING
    past_history: str = MISSING
    allergy_history: str = MISSING
    oral_exam: str = MISSING
    auxiliary_exam: str = MISSING
    diagnosis: str = MISSING
    treatment: str = MISSING
    advice: str = MISSING
    visit_date: str = ""

    def __post_init__(self) -> None:
        if not self.visit_date or self.visit_date == MISSING:
            self.visit_date = date.today().isoformat()
        for key, value in list(asdict(self).items()):
            if value is None or str(value).strip() == "":
                setattr(self, key, MISSING)

    def to_frontend(self) -> dict[str, str]:
        return {
            "name": self.name,
            "sex": self.sex,
            "age": self.age if self.age.endswith("岁") or self.age == MISSING else f"{self.age}岁",
            "visitDate": self.visit_date,
            "chiefComplaint": self.chief_complaint,
            "presentIllness": self.present_illness,
            "pastHistory": self.past_history,
            "allergyHistory": self.allergy_history,
            "exam": self.oral_exam,
            "auxiliaryExam": self.auxiliary_exam,
            "diagnosis": self.diagnosis,
            "treatment": self.treatment,
            "advice": self.advice,
        }

    def to_chinese(self) -> dict[str, str]:
        return {
            "姓名": self.name,
            "性别": self.sex,
            "年龄": self.age if self.age.endswith("岁") or self.age == MISSING else f"{self.age}岁",
            "主诉": self.chief_complaint,
            "现病史": self.present_illness,
            "既往史": self.past_history,
            "药物过敏史": self.allergy_history,
            "专科检查": self.oral_exam,
            "辅助检查": self.auxiliary_exam,
            "诊断": self.diagnosis,
            "处理": self.treatment,
            "注意事项": self.advice,
            "就诊日期": self.visit_date,
        }

    @classmethod
    def from_mapping(cls, payload: dict[str, Any] | None) -> "RecordFields":
        payload = payload or {}

        def pick(*names: str) -> str:
            for name in names:
                value = payload.get(name)
                if value is not None and str(value).strip():
                    return str(value).strip()
            return MISSING

        return cls(
            name=pick("姓名", "name"),
            sex=pick("性别", "sex"),
            age=pick("年龄", "age"),
            chief_complaint=pick("主诉", "chiefComplaint", "chief_complaint"),
            present_illness=pick("现病史", "presentIllness", "present_illness"),
            past_history=pick("既往史", "pastHistory", "past_history"),
            allergy_history=pick("药物过敏史", "allergyHistory", "allergy_history"),
            oral_exam=pick("专科检查", "exam", "oralExam", "oral_exam"),
            auxiliary_exam=pick("辅助检查", "auxiliaryExam", "auxiliary_exam"),
            diagnosis=pick("诊断", "diagnosis"),
            treatment=pick("处理", "treatment"),
            advice=pick("注意事项", "advice"),
            visit_date=pick("就诊日期", "visitDate", "visit_date"),
        )
