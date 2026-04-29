from __future__ import annotations

import json
import re
from typing import Any


def parse_model_output(raw_output: str) -> tuple[dict[str, Any], str]:
    text = (raw_output or "").strip()
    structured = _parse_json_object(text)
    if structured is None:
        return {}, text
    return structured, _report_from_structured(structured)


def extract_boxes(structured: dict[str, Any]) -> list[dict[str, Any]]:
    boxes: list[dict[str, Any]] = []
    findings = structured.get("findings")
    if not isinstance(findings, list):
        return boxes

    for item in findings:
        if not isinstance(item, dict):
            continue
        raw_box = (
            item.get("bbox_2d")
            or item.get("bbox2d")
            or item.get("box2d")
            or item.get("bbox")
            or item.get("box")
        )
        box = _normalize_box(raw_box)
        if box is None:
            continue
        label = item.get("sign") or item.get("region") or item.get("label") or "异常区域"
        if item.get("region") and item.get("sign"):
            label = f"{item.get('region')}：{item.get('sign')}"
        boxes.append(
            {
                "label": str(label),
                "x1": box[0],
                "y1": box[1],
                "x2": box[2],
                "y2": box[3],
                "confidence": str(item.get("confidence") or ""),
            }
        )
    return boxes


def _parse_json_object(text: str) -> dict[str, Any] | None:
    candidates = [_strip_code_fence(text)]
    extracted = _extract_first_json_object(text)
    if extracted:
        candidates.append(extracted)

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            return {"findings": parsed}
    return None


def _strip_code_fence(text: str) -> str:
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, flags=re.DOTALL | re.IGNORECASE)
    return fenced.group(1).strip() if fenced else text


def _extract_first_json_object(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _normalize_box(raw_box: Any) -> list[float] | None:
    if raw_box is None:
        return None
    if isinstance(raw_box, str):
        values = re.findall(r"-?\d+(?:\.\d+)?", raw_box)
        raw_box = [float(value) for value in values[:4]]
    if not isinstance(raw_box, (list, tuple)) or len(raw_box) < 4:
        return None

    try:
        nums = [float(raw_box[index]) for index in range(4)]
    except (TypeError, ValueError):
        return None

    max_value = max(nums)
    if max_value > 1:
        scale = 1000.0 if max_value <= 1000 else max_value
        nums = [value / scale for value in nums]

    x1, y1, x2, y2 = [min(1.0, max(0.0, value)) for value in nums]
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1
    if x2 - x1 <= 0.001 or y2 - y1 <= 0.001:
        return None
    return [x1, y1, x2, y2]


def _report_from_structured(data: dict[str, Any]) -> str:
    lines: list[str] = []
    image_type = data.get("image_type")
    if image_type:
        lines.append(f"影像类型：{image_type}")

    findings = data.get("findings")
    if isinstance(findings, list) and findings:
        lines.append("影像发现：")
        for index, item in enumerate(findings, start=1):
            if not isinstance(item, dict):
                continue
            region = item.get("region") or "未明确部位"
            sign = item.get("sign") or "未明确征象"
            description = item.get("description") or ""
            confidence = item.get("confidence") or "未标注"
            lines.append(f"{index}. {region} - {sign}（置信度：{confidence}）")
            if description:
                lines.append(f"   {description}")

    for title, key in (
        ("可能诊断方向", "possible_diagnoses"),
        ("下一步建议", "next_steps"),
        ("局限性", "limitations"),
    ):
        values = data.get(key)
        if isinstance(values, list) and values:
            lines.append(f"{title}：")
            lines.extend(f"- {value}" for value in values)

    safety_note = data.get("safety_note")
    if safety_note:
        lines.append(str(safety_note))
    elif lines:
        lines.append("本结果仅供辅助判读，需结合临床检查并由医生最终判断。")

    return "\n".join(lines).strip()
