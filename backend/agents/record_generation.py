from __future__ import annotations

from .record_fields import MISSING, RecordFields


def _section(value: str) -> str:
    return value.strip() if value and value.strip() else MISSING


def _numbered_if_list(value: str) -> str:
    value = _section(value)
    if value == MISSING:
        return value
    parts = [part.strip(" ：:，,。；;") for part in value.replace("\n", "；").split("；") if part.strip(" ：:，,。；;")]
    if len(parts) <= 1:
        return value
    return "\n".join(f"{idx}. {part}" for idx, part in enumerate(parts, start=1))


def render_record_markdown(fields: RecordFields) -> str:
    age = fields.age if fields.age.endswith("岁") or fields.age == MISSING else f"{fields.age}岁"
    return "\n".join(
        [
            "# 口腔门诊病历",
            "",
            "## 基本信息",
            "",
            "| 项目 | 内容 |",
            "| --- | --- |",
            f"| 姓名 | {_section(fields.name)} |",
            f"| 性别 | {_section(fields.sex)} |",
            f"| 年龄 | {_section(age)} |",
            f"| 就诊日期 | {_section(fields.visit_date)} |",
            "",
            "## 主诉",
            "",
            _section(fields.chief_complaint),
            "",
            "## 现病史",
            "",
            _section(fields.present_illness),
            "",
            "## 既往史",
            "",
            _section(fields.past_history),
            "",
            "## 药物过敏史",
            "",
            _section(fields.allergy_history),
            "",
            "## 专科检查",
            "",
            _section(fields.oral_exam),
            "",
            "## 辅助检查",
            "",
            _section(fields.auxiliary_exam),
            "",
            "## 诊断",
            "",
            _numbered_if_list(fields.diagnosis),
            "",
            "## 处理",
            "",
            _numbered_if_list(fields.treatment),
            "",
            "## 注意事项",
            "",
            _numbered_if_list(fields.advice),
        ]
    )

