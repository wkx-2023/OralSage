from __future__ import annotations

import re


TERM_REPLACEMENTS = {
    "组生涯": "阻生牙",
    "祖生涯": "阻生牙",
    "阻生涯": "阻生牙",
    "智牙": "智齿",
    "根管冶疗": "根管治疗",
    "跟管治疗": "根管治疗",
    "牙周刮制": "牙周刮治",
    "牙龈下刮治": "龈下刮治",
    "阿莫西灵": "阿莫西林",
    "布洛分": "布洛芬",
    "克林霉数": "克林霉素",
    "拍片子": "拍片",
}

FILLER_PATTERN = re.compile(r"(嗯+|啊+|呃+|这个+|那个+|就是+|然后然后+|对对+)")


def _normalize_speaker(line: str, previous_role: str | None) -> tuple[str, str]:
    stripped = line.strip(" ，,。；;\n\t")
    match = re.match(r"^(医生|医师|大夫|患者|病人|患方)\s*[:：]\s*(.*)$", stripped)
    if match:
        role = "医生" if match.group(1) in {"医生", "医师", "大夫"} else "患者"
        return role, match.group(2).strip(" ，,。；;")

    # Single-line chart summaries often start with "患者，女，32岁...".
    if re.match(r"^患者[，,、\s]", stripped):
        return "患者", re.sub(r"^患者[，,、\s]*", "", stripped).strip(" ，,。；;")

    if re.match(r"^(哪里|请问|张口|检查|需要|建议|诊断|处理|治疗|术后|最近|有没有|您)", stripped):
        role = "医生"
    elif previous_role == "医生":
        role = "患者"
    else:
        role = "患者" if re.search(r"(自诉|主诉|疼痛|肿痛|敏感|不适)", stripped) else "医生"
    return role, stripped


def correct_conversation(conversation: str) -> str:
    """Correct obvious ASR noise without adding medical facts."""
    text = conversation.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""

    text = re.sub(r"(?<!^)(医生|医师|大夫|患者|病人|患方)\s*[:：]", r"\n\1：", text)
    for wrong, right in TERM_REPLACEMENTS.items():
        text = text.replace(wrong, right)
    text = FILLER_PATTERN.sub("", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)

    chunks = [chunk.strip(" ，,。；;") for chunk in re.split(r"\n+|(?<=。)\s*", text) if chunk.strip(" ，,。；;")]
    normalized: list[str] = []
    previous_role: str | None = None
    for chunk in chunks:
        role, content = _normalize_speaker(chunk, previous_role)
        if not content:
            continue
        content = content.replace("疼了", "疼痛")
        content = content.replace("吃东西就疼", "进食时疼痛")
        normalized.append(f"{role}：{content}")
        previous_role = role

    return "\n".join(normalized) if normalized else text
