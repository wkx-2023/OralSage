from __future__ import annotations

import json
import re
from typing import Any

from backend.agents.record_fields import MISSING, RecordFields
from backend.services.llm_client import LLMConfigurationError, LLMRequestError, chat_completion, is_llm_configured


RECORD_API_KEYS = ("RECORD_LLM_API_KEY", "DECISION_LLM_API_KEY", "QWEN_API_KEY", "DASHSCOPE_API_KEY")
RECORD_BASE_URLS = ("RECORD_LLM_BASE_URL", "DECISION_LLM_BASE_URL", "QWEN_BASE_URL", "DASHSCOPE_BASE_URL")
RECORD_MODELS = ("RECORD_LLM_MODEL", "DECISION_LLM_MODEL", "QWEN_MODEL", "DASHSCOPE_MODEL")


SYSTEM_PROMPT = """你是口腔门诊病历结构化抽取助手。

你会收到医患对话、ASR转写文本或医生口述摘要。请只依据输入中明确出现的信息，抽取为标准门诊病历字段。

必须遵守：
1. 不编造患者信息、检查结果、诊断、处理、用药、注意事项。
2. 未提及字段统一写“未提及”。
3. 可以把口语转成规范医学表达，但不能改变事实。例如“牙烂掉了”可规范为“牙体龋坏”；“烂到神经了”可规范为“龋坏累及牙髓/神经（来自医生表述）”。
4. “诊断”只允许来自医生明确诊断、医生明确判断，或检查结果中直接出现的疾病名；不确定时写“未提及”。
5. “处理”只能来自医生明确说出的治疗计划、操作、用药或医嘱。
6. 如果患者说“拍过片了”，但没有影像结论，辅助检查写“已拍片，具体结果未提及”。
7. 如果患者没有用标准主诉表达，但对话中存在明确就诊问题，可以用“牙齿龋坏要求诊治，时间未提及”“右下后牙疼痛一周”等方式概括主诉；不得编造疼痛、冷热刺激、肿胀等未出现症状。
8. 现病史应简要串联对话中已知事实：发现的问题、已做检查、医生解释、拟处理；没有病程时间时写“时间未提及”。
9. 专科检查可记录医生口腔检查/观察到的事实，例如“牙体龋坏”“龋坏累及牙髓/神经”。
10. 返回严格 JSON，不要 Markdown，不要解释，不要代码块。

JSON 字段固定为：
{
  "姓名": "未提及",
  "性别": "未提及",
  "年龄": "未提及",
  "主诉": "未提及",
  "现病史": "未提及",
  "既往史": "未提及",
  "药物过敏史": "未提及",
  "专科检查": "未提及",
  "辅助检查": "未提及",
  "诊断": "未提及",
  "处理": "未提及",
  "注意事项": "未提及"
}
"""


USER_TEMPLATE = """请抽取下面文本中的口腔门诊病历字段。

文本：
{conversation}
"""


def configured() -> bool:
    return is_llm_configured(*RECORD_API_KEYS)


def _strip_code_fence(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def _loads_json_object(text: str) -> dict[str, Any]:
    cleaned = _strip_code_fence(text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.S)
        if not match:
            raise
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("LLM did not return a JSON object")
    return data


def _normalize_payload(payload: dict[str, Any]) -> dict[str, str]:
    required = [
        "姓名",
        "性别",
        "年龄",
        "主诉",
        "现病史",
        "既往史",
        "药物过敏史",
        "专科检查",
        "辅助检查",
        "诊断",
        "处理",
        "注意事项",
    ]
    normalized: dict[str, str] = {}
    for key in required:
        value = payload.get(key, MISSING)
        if value is None:
            normalized[key] = MISSING
        elif isinstance(value, list):
            normalized[key] = "；".join(str(item).strip() for item in value if str(item).strip()) or MISSING
        else:
            normalized[key] = str(value).strip() or MISSING
    return normalized


def extract_record_fields_with_llm(conversation: str) -> tuple[RecordFields, dict[str, Any]]:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": USER_TEMPLATE.format(conversation=conversation)},
    ]
    raw = chat_completion(
        messages,
        temperature=0,
        max_tokens=1600,
        api_key_names=RECORD_API_KEYS,
        base_url_names=RECORD_BASE_URLS,
        model_names=RECORD_MODELS,
    )
    payload = _normalize_payload(_loads_json_object(raw))
    fields = RecordFields.from_mapping(payload)
    return fields, {"extractor": "llm", "raw_response": raw}


__all__ = [
    "LLMConfigurationError",
    "LLMRequestError",
    "configured",
    "extract_record_fields_with_llm",
]
