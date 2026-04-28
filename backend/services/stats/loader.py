from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
from pathlib import Path
import re


PROJECT_ROOT = Path(__file__).resolve().parents[3]
SOURCE_JSONL = PROJECT_ROOT / "data" / "processed" / "knowledge_base" / "clinical_cases.jsonl"
PARSED_DIR = PROJECT_ROOT / "data" / "processed" / "clinic_stats"
PARSED_JSONL = PARSED_DIR / "clinical_cases.parsed.jsonl"


FIELD_ALIASES = {
    "门诊信息查询": "序号",
    "Unnamed: 1": "住院号/门诊号",
    "Unnamed: 2": "挂号类别",
    "Unnamed: 3": "就诊时间",
    "Unnamed: 4": "就诊科室",
    "Unnamed: 5": "病历操作名称1",
    "Unnamed: 6": "病历操作编码1",
    "Unnamed: 7": "病历诊断名称1",
    "Unnamed: 8": "病历诊断编码1",
    "Unnamed: 9": "主诉",
    "Unnamed: 10": "现病史",
    "Unnamed: 11": "既往史",
    "Unnamed: 12": "处理",
    "Unnamed: 13": "治疗计划",
    "Unnamed: 14": "检查",
    "Unnamed: 15": "辅查",
}


@dataclass(frozen=True)
class ClinicCase:
    case_id: str
    visit_no: str
    register_type: str
    visit_date: str
    visit_month: str
    department: str
    operation_name: str
    operation_code: str
    diagnosis_name: str
    diagnosis_code: str
    chief_complaint: str
    present_illness: str
    past_history: str
    treatment: str
    treatment_plan: str
    exam: str
    aux_exam: str
    raw_text: str

    def searchable_text(self) -> str:
        return "\n".join(
            [
                self.operation_name,
                self.diagnosis_name,
                self.chief_complaint,
                self.present_illness,
                self.past_history,
                self.treatment,
                self.treatment_plan,
                self.exam,
                self.aux_exam,
                self.department,
                self.register_type,
            ]
        )


def _parse_lines(text: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    current_key: str | None = None
    for raw_line in (text or "").splitlines():
        line = raw_line.rstrip()
        match = re.match(r"^(门诊信息查询|Unnamed:\s*\d+)：(.*)$", line)
        if match:
            raw_key = re.sub(r"\s+", " ", match.group(1).strip())
            current_key = FIELD_ALIASES.get(raw_key, raw_key)
            parsed[current_key] = match.group(2).strip()
        elif current_key and line.strip():
            parsed[current_key] = (parsed.get(current_key, "") + "\n" + line.strip()).strip()
    return parsed


def _normalize_date(value: str) -> tuple[str, str]:
    value = (value or "").strip()
    if not value:
        return "", ""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(value[:19], fmt)
            return dt.date().isoformat(), dt.strftime("%Y-%m")
        except ValueError:
            continue
    match = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})?", value)
    if match:
        year = int(match.group(1))
        month = int(match.group(2))
        day = int(match.group(3) or 1)
        return f"{year:04d}-{month:02d}-{day:02d}", f"{year:04d}-{month:02d}"
    return value, value[:7]


def _case_from_text(text: str) -> ClinicCase | None:
    data = _parse_lines(text)
    case_id = data.get("序号", "").strip()
    if not case_id or case_id == "序号":
        return None
    visit_date, visit_month = _normalize_date(data.get("就诊时间", ""))
    return ClinicCase(
        case_id=case_id,
        visit_no=data.get("住院号/门诊号", ""),
        register_type=data.get("挂号类别", ""),
        visit_date=visit_date,
        visit_month=visit_month,
        department=data.get("就诊科室", ""),
        operation_name=data.get("病历操作名称1", ""),
        operation_code=data.get("病历操作编码1", ""),
        diagnosis_name=data.get("病历诊断名称1", ""),
        diagnosis_code=data.get("病历诊断编码1", ""),
        chief_complaint=data.get("主诉", ""),
        present_illness=data.get("现病史", ""),
        past_history=data.get("既往史", ""),
        treatment=data.get("处理", ""),
        treatment_plan=data.get("治疗计划", ""),
        exam=data.get("检查", ""),
        aux_exam=data.get("辅查", ""),
        raw_text=text,
    )


def build_parsed_cases(force: bool = False) -> list[ClinicCase]:
    if PARSED_JSONL.exists() and not force:
        return load_cases()
    PARSED_DIR.mkdir(parents=True, exist_ok=True)
    cases: list[ClinicCase] = []
    with SOURCE_JSONL.open("r", encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            obj = json.loads(line)
            case = _case_from_text(obj.get("text", ""))
            if case:
                cases.append(case)
    with PARSED_JSONL.open("w", encoding="utf-8") as target:
        for case in cases:
            target.write(json.dumps(asdict(case), ensure_ascii=False) + "\n")
    return cases


def load_cases() -> list[ClinicCase]:
    if not PARSED_JSONL.exists():
        return build_parsed_cases(force=True)
    cases: list[ClinicCase] = []
    with PARSED_JSONL.open("r", encoding="utf-8") as source:
        for line in source:
            if line.strip():
                cases.append(ClinicCase(**json.loads(line)))
    return cases
