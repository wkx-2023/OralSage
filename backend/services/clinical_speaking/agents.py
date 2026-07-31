"""Strict Spark agents for clinical speaking training.

There is deliberately no rule-based or template fallback in this module. Any
configuration, transport, parsing, or contract failure is surfaced to the
caller and stops the current workflow.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from typing import Any, Callable

from backend.services.llm_client import (
    LLMConfigurationError,
    LLMRequestError,
    chat_completion,
    classify_llm_error,
)


API_KEY_NAMES = ("CLINICAL_SPEAKING_LLM_API_KEY",)
BASE_URL_NAMES = ("CLINICAL_SPEAKING_LLM_BASE_URL",)
MODEL_NAMES = ("CLINICAL_SPEAKING_LLM_MODEL",)
SPARK_REASONING_PROVIDER = "xfyun_spark_reasoning_http"
SPARK_REASONING_MODEL = "xsparkx2flash"
SPARK_MIN_OUTPUT_TOKENS = 4096
MAX_RETRY_RESPONSE_CHARS = 1600
MAX_CONTRACT_ATTEMPTS = 5
MAX_JUDGE_CONTRACT_ATTEMPTS = 8
MAX_TRANSIENT_ATTEMPTS = 3
MAX_JUDGE_TRANSIENT_ATTEMPTS = 4
MAX_PATIENT_DIALOGUE_ATTEMPTS = 4
REQUIRED_AGENT_ENV = (
    "CLINICAL_SPEAKING_LLM_API_KEY",
    "CLINICAL_SPEAKING_LLM_BASE_URL",
    "CLINICAL_SPEAKING_LLM_MODEL",
)

logger = logging.getLogger(__name__)
_READINESS_LOCK = threading.Lock()
_READINESS_CACHE: dict[str, Any] = {}
_READINESS_TTL_SECONDS = 120
_READINESS_FAILURE_TTL_SECONDS = 10

EXAM_DOMAINS = (
    ("pathogenesis", "病理机制"),
    ("clinical_features", "临床表现与诊断依据"),
    ("differential", "鉴别诊断"),
    ("treatment", "治疗原则"),
    ("risk_followup", "风险与随访"),
)


class StrictAgentError(RuntimeError):
    def __init__(self, message: str, *, code: str = "agent_failed", status_code: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def agent_readiness(*, verify: bool = False) -> dict[str, Any]:
    missing = [name for name in REQUIRED_AGENT_ENV if not str(os.getenv(name) or "").strip()]
    configured_model = str(os.getenv("CLINICAL_SPEAKING_LLM_MODEL") or "").strip()
    if configured_model and configured_model != SPARK_REASONING_MODEL:
        missing.append(f"CLINICAL_SPEAKING_LLM_MODEL={SPARK_REASONING_MODEL}")
    result = {
        "provider": SPARK_REASONING_PROVIDER,
        "configured": not missing,
        "model": configured_model,
        "missing": missing,
        "fallback_used": False,
    }
    if not verify or missing:
        return result

    config_key = tuple(str(os.getenv(name) or "") for name in REQUIRED_AGENT_ENV)
    now = time.monotonic()
    with _READINESS_LOCK:
        if (
            _READINESS_CACHE.get("config_key") == config_key
            and now - float(_READINESS_CACHE.get("checked_at") or 0)
            < float(_READINESS_CACHE.get("ttl") or _READINESS_TTL_SECONDS)
        ):
            return {**result, **dict(_READINESS_CACHE["result"])}
        last_probe_error: Exception | None = None
        category = "unknown"
        for probe_attempt in range(MAX_TRANSIENT_ATTEMPTS):
            try:
                use_stream = probe_attempt % 2 == 0
                probe_text = chat_completion(
                    [
                        {
                            "role": "user",
                            "content": '只输出 JSON 对象 {"ok": true}，不要输出解释。',
                        }
                    ],
                    temperature=0.0,
                    max_tokens=2048,
                    api_key_names=API_KEY_NAMES,
                    base_url_names=BASE_URL_NAMES,
                    model_names=MODEL_NAMES,
                    extra_body={"search_disable": True},
                    stream_callback=(lambda _delta: None) if use_stream else None,
                )
                if not str(probe_text or "").strip():
                    raise LLMRequestError(
                        "Spark readiness probe returned empty final content"
                    )
                if _extract_json(probe_text).get("ok") is not True:
                    raise LLMRequestError("Spark readiness probe returned an invalid payload")
                last_probe_error = None
                break
            except (LLMConfigurationError, LLMRequestError, ValueError, TypeError) as exc:
                last_probe_error = exc
                category = classify_llm_error(exc)
                retryable = category in {
                    "timeout",
                    "network",
                    "http_server_error",
                    "rate_limit",
                    "unknown",
                }
                if retryable and probe_attempt + 1 < MAX_TRANSIENT_ATTEMPTS:
                    logger.warning(
                        "Spark readiness probe failed transiently (%s); retrying",
                        category,
                    )
                    time.sleep(0.5 * (probe_attempt + 1))
                    continue
                break

        if last_probe_error is None:
            verified = {"authorized": True, "authorization_error": None}
        else:
            logger.error("Spark readiness verification failed: %s", last_probe_error)
            verified = {
                "authorized": False,
                "authorization_error": category,
                "missing": [
                    f"讯飞星火推理服务 {SPARK_REASONING_MODEL} 模型授权或可用额度"
                    if category == "auth"
                    else f"讯飞星火推理服务 {SPARK_REASONING_MODEL} 在线服务"
                ],
            }
        _READINESS_CACHE.clear()
        _READINESS_CACHE.update(
            {
                "config_key": config_key,
                "checked_at": now,
                "ttl": (
                    _READINESS_TTL_SECONDS
                    if verified["authorized"]
                    else _READINESS_FAILURE_TTL_SECONDS
                ),
                "result": verified,
            }
        )
        return {**result, **verified}


def _extract_json(text: str) -> dict[str, Any]:
    value = str(text or "").strip()
    if value.startswith("```"):
        value = value.strip("`").removeprefix("json").strip()
    start = value.find("{")
    end = value.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("response does not contain a JSON object")
    parsed = json.loads(value[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("response root must be a JSON object")
    return parsed


def _strict_json_call(
    messages: list[dict[str, str]],
    validator: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    max_tokens: int = 1800,
    contract_attempts: int = MAX_CONTRACT_ATTEMPTS,
    transient_attempts: int = MAX_TRANSIENT_ATTEMPTS,
    agent_label: str = "Agent",
) -> dict[str, Any]:
    ready = agent_readiness()
    if not ready["configured"]:
        raise StrictAgentError(
            "临床表达训练所需的讯飞星火配置不完整。",
            code="spark_not_configured",
            status_code=503,
        )
    working = list(messages)
    last_error = ""
    for attempt in range(max(1, contract_attempts)):
        text = ""
        try:
            for transport_attempt in range(max(1, transient_attempts)):
                try:
                    use_stream = transport_attempt % 2 == 0
                    text = chat_completion(
                        working,
                        temperature=0.08,
                        max_tokens=max(max_tokens, SPARK_MIN_OUTPUT_TOKENS),
                        api_key_names=API_KEY_NAMES,
                        base_url_names=BASE_URL_NAMES,
                        model_names=MODEL_NAMES,
                        extra_body={"search_disable": True},
                        stream_callback=(lambda _delta: None) if use_stream else None,
                    )
                    if not str(text or "").strip():
                        raise LLMRequestError(
                            "Spark response contained no final text content"
                        )
                    break
                except LLMRequestError as exc:
                    detail = str(exc)
                    transport_name = "SSE" if use_stream else "non-stream"
                    if "TokenOverLimitError" in detail or "code=1010" in detail:
                        logger.error(
                            "Spark %s exceeded context via %s: %s",
                            agent_label,
                            transport_name,
                            detail,
                        )
                        raise StrictAgentError(
                            "讯飞星火上下文超过模型限制，当前流程已停止。",
                            code="spark_context_over_limit",
                            status_code=502,
                        ) from exc
                    category = classify_llm_error(exc)
                    retryable = category in {
                        "timeout",
                        "network",
                        "http_server_error",
                        "rate_limit",
                        "unknown",
                    }
                    if retryable and transport_attempt + 1 < max(1, transient_attempts):
                        logger.warning(
                            "Spark %s transient failure via %s (%s); retrying with %s",
                            agent_label,
                            transport_name,
                            category,
                            "non-stream" if use_stream else "SSE",
                        )
                        delay = (1.0 if category == "rate_limit" else 0.5) * (
                            transport_attempt + 1
                        )
                        time.sleep(delay)
                        continue
                    logger.error(
                        "Spark %s request failed via %s (%s): %s",
                        agent_label,
                        transport_name,
                        category,
                        detail,
                    )
                    error_messages = {
                        "auth": f"讯飞星火{agent_label}鉴权或模型授权失败，请检查 {SPARK_REASONING_MODEL} 权限和可用额度。",
                        "timeout": f"讯飞星火{agent_label}响应超时，当前轮次未提交。",
                        "network": f"讯飞星火{agent_label}网络连接失败，当前轮次未提交。",
                        "rate_limit": f"讯飞星火{agent_label}请求频率受限，当前轮次未提交。",
                        "unsupported": f"讯飞星火当前模型不支持{agent_label}所需的结构化请求。",
                    }
                    raise StrictAgentError(
                        error_messages.get(
                            category,
                            f"讯飞星火{agent_label}请求失败，当前轮次未提交。",
                        ),
                        code="spark_auth_failed" if category == "auth" else "spark_request_failed",
                        status_code=502,
                    ) from exc
            return validator(_extract_json(text))
        except (ValueError, TypeError, KeyError) as exc:
            last_error = str(exc)
            if attempt + 1 < max(1, contract_attempts):
                working = [
                    *messages,
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "validation_error": last_error,
                                "invalid_response_excerpt": str(text or "")[:MAX_RETRY_RESPONSE_CHARS],
                                "instruction": (
                                    "上一响应无效。请重新执行最初任务并输出完整 JSON 根对象；"
                                    "不得只修补或返回出错的局部字段，不要解释。"
                                ),
                            },
                            ensure_ascii=False,
                        ),
                    },
                ]
                continue
        except LLMConfigurationError as exc:
            raise StrictAgentError(str(exc), code="spark_not_configured", status_code=503) from exc
        except StrictAgentError:
            raise
    logger.error("Spark %s response failed JSON validation after retries: %s", agent_label, last_error)
    raise StrictAgentError(
        f"讯飞星火{agent_label}连续未返回完整的结构化结果，当前轮次已保留，请重试。",
        code="spark_contract_failed",
        status_code=502,
    )


def _agent_trace(role: str) -> dict[str, Any]:
    return {
        "role": role,
        "provider": SPARK_REASONING_PROVIDER,
        "model": str(os.getenv("CLINICAL_SPEAKING_LLM_MODEL") or "").strip(),
        "fallback_used": False,
    }


def validate_judge_blueprint(
    blueprint: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    """Validate the persisted handoff contract used by the oral-exam state machine."""
    if not isinstance(blueprint, dict):
        raise ValueError("blueprint must be an object")
    questions = blueprint.get("questions")
    if not isinstance(questions, list) or len(questions) != len(EXAM_DOMAINS):
        raise ValueError("blueprint must contain exactly five questions")
    evidence_indexes = {
        item.get("index")
        for item in evidence
        if isinstance(item, dict)
        and isinstance(item.get("index"), int)
        and not isinstance(item.get("index"), bool)
    }
    if not evidence_indexes:
        raise ValueError("blueprint evidence is empty")

    for index, ((category, label), question) in enumerate(zip(EXAM_DOMAINS, questions), 1):
        if not isinstance(question, dict):
            raise ValueError(f"question must be an object: {category}")
        if question.get("question_id") != f"judge_{index}":
            raise ValueError(f"question_id is invalid: {category}")
        if question.get("category") != category or question.get("label") != label:
            raise ValueError(f"question domain handoff is invalid: {category}")
        if question.get("max_score") != 20:
            raise ValueError(f"max_score is invalid: {category}")
        if not str(question.get("prompt") or "").strip():
            raise ValueError(f"question prompt is empty: {category}")
        points = question.get("rubric_points")
        if (
            not isinstance(points, list)
            or not 2 <= len(points) <= 6
            or any(not isinstance(point, str) or not point.strip() for point in points)
        ):
            raise ValueError(f"rubric_points are invalid: {category}")
        refs = question.get("evidence_refs")
        if (
            not isinstance(refs, list)
            or not refs
            or any(
                not isinstance(ref, int)
                or isinstance(ref, bool)
                or ref not in evidence_indexes
                for ref in refs
            )
        ):
            raise ValueError(f"evidence_refs are invalid: {category}")
    trace = blueprint.get("trace")
    if not isinstance(trace, dict) or trace.get("fallback_used") is not False:
        raise ValueError("blueprint trace is invalid")
    return blueprint


def select_case_target(learner_targets: list[str], diagnoses: list[str]) -> dict[str, Any]:
    """Map broad learning context to the closest diagnosis in the curated case pool."""
    targets = [" ".join(str(item).split()) for item in learner_targets if str(item).strip()][:8]
    allowed = [str(item).strip() for item in diagnoses if str(item).strip()]
    if not targets or not allowed:
        return {
            "diagnosis": None,
            "matched_target": "",
            "reason": "当前没有可用于病例匹配的学习上下文。",
            "trace": _agent_trace("case_selection_policy"),
        }

    def validate(data: dict[str, Any]) -> dict[str, Any]:
        diagnosis = data.get("diagnosis")
        diagnosis = str(diagnosis).strip() if diagnosis is not None else None
        matched_target = str(data.get("matched_target") or "").strip()
        reason = " ".join(str(data.get("reason") or "").split())
        if diagnosis in {"", "null", "None"}:
            diagnosis = None
        if diagnosis is not None and diagnosis not in allowed:
            raise ValueError("diagnosis must be selected from available_diagnoses")
        if diagnosis is not None and matched_target not in targets:
            raise ValueError("matched_target must be copied from learner_targets")
        if diagnosis is None:
            matched_target = ""
        if not reason:
            raise ValueError("reason is required")
        return {
            "diagnosis": diagnosis,
            "matched_target": matched_target,
            "reason": reason[:240],
            "trace": _agent_trace("case_selection_agent"),
        }

    return _strict_json_call(
        [
            {
                "role": "system",
                "content": (
                    "你是口腔医学临床训练的病例选题 Agent。根据学习者薄弱点和近期学习主题，"
                    "从 available_diagnoses 中选择教学关联最明确的一个病种。允许根据学科、章节和临床主题做医学语义匹配，"
                    "但不得输出列表之外的病种；没有明确关联时 diagnosis 必须为 null。只输出严格 JSON。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "learner_targets": targets,
                        "available_diagnoses": allowed,
                        "required": {
                            "diagnosis": "列表中的病种或 null",
                            "matched_target": "原样复制一个 learner_targets；未匹配则为空字符串",
                            "reason": "简短医学教学关联依据",
                        },
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        validate,
        max_tokens=450,
        agent_label="病例选择 Agent",
    )


def _verify_patient_grounding(
    facts: list[dict[str, Any]],
    reply: str,
    *,
    doctor_message: str = "",
    declared_unknown: bool = False,
) -> dict[str, Any]:
    """Independently assess whether a patient utterance is fact-grounded."""

    def validate(data: dict[str, Any]) -> dict[str, Any]:
        grounded = data.get("grounded")
        unsupported = data.get("unsupported_claims")
        if not isinstance(grounded, bool) or not isinstance(unsupported, list):
            raise ValueError("grounded and unsupported_claims are required")
        return {
            "grounded": grounded,
            "unsupported_claims": [
                " ".join(str(item).split()) for item in unsupported if str(item).strip()
            ][:8],
            "trace": _agent_trace("patient_grounding_guard"),
        }

    result = _strict_json_call(
        [
            {
                "role": "system",
                "content": (
                    "你是虚拟患者事实落地校验 Agent。逐项判断 patient_reply 中的客观陈述是否都能由 patient_facts 直接支持。"
                    "常识补全、推测、诊断、治疗建议和事实中没有的否认项都属于 unsupported_claims。"
                    "当 declared_unknown=true 时，‘我不知道、记不清、不确定’等知识状态表达不是客观病情主张，"
                    "可以视为有依据；但同一句中附带的症状、时间、检查等客观陈述仍必须由 patient_facts 支持。"
                    "结合 doctor_message 检查患者角色：患者不得确认或否定医生提出的诊断，不得评价诊断是否正确，"
                    "也不得自行提出病例事实中没有的检查、治疗或下一步行动。此类内容必须列入 unsupported_claims。"
                    "自然的第一人称改写和不改变含义的时间表达可以视为支持。"
                    "输出只能包含 grounded 和 unsupported_claims 两个字段，只输出严格 JSON。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "patient_facts": facts,
                        "doctor_message": doctor_message,
                        "patient_reply": reply,
                        "declared_unknown": declared_unknown,
                        "required_output": {
                            "grounded": "boolean，所有陈述均有事实支持时为 true",
                            "unsupported_claims": ["无事实支持的原文片段；没有则为空数组"],
                        },
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        validate,
        max_tokens=450,
        agent_label="患者事实校验 Agent",
    )
    return result


def _verify_diagnosis_decision(
    case: dict[str, Any],
    doctor_text: str,
    decision: dict[str, Any],
) -> dict[str, Any]:
    allowed_codes = {
        "explicit_correct_diagnosis",
        "not_a_diagnosis_assertion",
        "negated_or_excluded",
        "uncertain_or_question",
        "wrong_diagnosis",
    }

    def validate(data: dict[str, Any]) -> dict[str, Any]:
        approved = data.get("approved")
        code = data.get("reason_code")
        candidate = data.get("diagnosis_candidate")
        if not isinstance(approved, bool) or code not in allowed_codes:
            raise ValueError("diagnosis verification decision is invalid")
        if not isinstance(candidate, str):
            raise ValueError("verified diagnosis candidate must be a string")
        if approved != (code == "explicit_correct_diagnosis"):
            raise ValueError("diagnosis verification fields conflict")
        if approved and not candidate.strip():
            raise ValueError("approved diagnosis requires a candidate")
        return {
            "approved": approved,
            "reason_code": code,
            "diagnosis_candidate": " ".join(candidate.split())[:80],
            "trace": _agent_trace("patient_diagnosis_guard"),
        }

    return _strict_json_call(
        [
            {
                "role": "system",
                "content": (
                    "你是虚拟患者 Agent 的诊断切换复核器。独立检查医生原话是否明确、肯定地给出了正确诊断。"
                    "只有明确诊断结论与 target_diagnosis 或 aliases 语义一致时才能 approved=true。"
                    "疑问、求证、否定、排除、鉴别、假设和不确定可能性必须拒绝。"
                    "不要补全医生没有说出的病名，不要解释，只输出 approved、reason_code、diagnosis_candidate 的严格 JSON。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "doctor_message": doctor_text,
                        "target_diagnosis": case.get("diagnosis"),
                        "aliases": case.get("diagnosis_aliases", []),
                        "proposed_decision": {
                            key: decision[key]
                            for key in ("intent", "diagnosis_candidate", "diagnosis_correct")
                        },
                        "allowed_reason_codes": sorted(allowed_codes),
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        validate,
        max_tokens=450,
        agent_label="诊断复核 Agent",
    )


def normalize_medical_transcript(text: str, terms: list[str]) -> dict[str, Any]:
    raw = " ".join(str(text or "").split())
    if not raw:
        raise StrictAgentError("语音转写内容为空。", code="empty_transcript", status_code=422)
    prompt = {
        "text_to_normalize": raw,
        "authorized_terms": terms[:80],
        "required_json_keys": ["normalized_text", "corrections"],
        "output_example_when_no_correction_is_needed": {
            "normalized_text": raw,
            "corrections": [],
        },
    }

    def validate(data: dict[str, Any]) -> dict[str, Any]:
        normalized = " ".join(str(data.get("normalized_text") or "").split())
        if not normalized:
            raise ValueError("normalized_text is empty")
        if len(normalized) > max(len(raw) * 2, len(raw) + 24):
            raise ValueError("normalized_text changes too much content")
        raw_compact = re.sub(r"\s+", "", raw)
        normalized_compact = re.sub(r"\s+", "", normalized)
        similarity = SequenceMatcher(None, raw_compact, normalized_compact).ratio()
        if similarity < 0.55:
            raise ValueError("normalized_text is not grounded in raw_transcript")
        corrections = data.get("corrections")
        if not isinstance(corrections, list):
            raise ValueError("corrections must be a list")
        cleaned: list[dict[str, str]] = []
        for item in corrections[:12]:
            if not isinstance(item, dict):
                raise ValueError("correction must be an object")
            source = str(item.get("from") or "").strip()
            target = str(item.get("to") or "").strip()
            if not source or not target or source not in raw or target not in normalized:
                raise ValueError("correction is not grounded in the transcript")
            cleaned.append({"from": source, "to": target, "reason": str(item.get("reason") or "术语纠正")})
        return {
            "raw_text": raw,
            "normalized_text": normalized,
            "corrections": cleaned,
            "trace": _agent_trace("terminology_agent"),
        }

    return _strict_json_call(
        [
            {
                "role": "system",
                "content": (
                    "你是口腔医学语音转写校对 Agent。只能纠正由语音识别造成的医学术语同音、近音和断句错误，"
                    "禁止补充原文没有的诊断、症状、推理或结论。normalized_text 必须填写校对后的实际转写文本，"
                    "不得复制字段说明；不需要纠正时必须逐字返回 text_to_normalize。只输出严格 JSON。"
                ),
            },
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ],
        validate,
        max_tokens=600,
        agent_label="术语校正 Agent",
    )


def patient_opening(case: dict[str, Any]) -> dict[str, Any]:
    facts = case.get("patient_facts", [])

    def validate(data: dict[str, Any]) -> dict[str, Any]:
        reply = " ".join(str(data.get("reply") or "").split())
        raw_supported = data.get("supported_fact_ids")
        if not isinstance(raw_supported, list) or any(
            not isinstance(item, str) for item in raw_supported
        ):
            raise ValueError("supported_fact_ids must be a string array")
        supported = list(dict.fromkeys(item.strip() for item in raw_supported if item.strip()))
        allowed = {str(item.get("id")) for item in facts if isinstance(item, dict)}
        if not reply or not supported or not set(supported).issubset(allowed):
            raise ValueError("opening reply is not grounded in patient facts")
        return {"reply": reply[:240], "supported_fact_ids": supported, "trace": _agent_trace("patient_agent")}

    base_messages = [
            {
                "role": "system",
                "content": (
                    "你是接受口腔问诊的患者，不是医生。根据给定事实用第一人称自然陈述主诉，"
                    "不得说出诊断、治疗记录或未提供的信息。reply 必须是一句完整的患者口语。"
                    "必须像普通患者面对面说话，把病历书面语改成生活化表达，例如把方位说成‘左上最后面那颗牙’，"
                    "把‘一月余’说成‘有一个多月了’。不要使用‘不适、一月余、患牙、检查确认’等病历或医生措辞。"
                    "语言简短自然，不要逐项朗读病历，不要自行分析原因。"
                    "supported_fact_ids 只能逐字复制 allowed_fact_ids 中实际支持 reply 的编号。"
                    "输出只能包含 reply 和 supported_fact_ids 两个字段，只输出严格 JSON。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task": "根据 patient_facts 生成一句第一人称患者开场主诉",
                        "patient_facts": facts,
                        "allowed_fact_ids": [str(item.get("id")) for item in facts],
                        "required_output": {
                            "reply": "string，完整患者口语",
                            "supported_fact_ids": ["一个或多个 allowed_fact_ids"],
                        },
                    },
                    ensure_ascii=False,
                ),
            },
        ]
    messages = base_messages
    last_grounding: dict[str, Any] | None = None
    for attempt in range(MAX_PATIENT_DIALOGUE_ATTEMPTS):
        result = _strict_json_call(
            messages,
            validate,
            max_tokens=400,
            agent_label="患者开场 Agent",
        )
        grounding = _verify_patient_grounding(facts, result["reply"])
        if grounding["grounded"] and not grounding["unsupported_claims"]:
            result["grounding"] = grounding["trace"]
            result["repair_attempts"] = attempt
            return result
        last_grounding = grounding
        messages = [
            *base_messages,
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "validation_error": "opening_not_grounded",
                        "unsupported_claims": grounding["unsupported_claims"],
                        "invalid_reply": result["reply"],
                        "instruction": "重新执行原任务，只使用 patient_facts 生成新的完整 JSON，不要解释。",
                    },
                    ensure_ascii=False,
                ),
            },
        ]
    logger.error("Patient opening grounding failed after repairs: %s", last_grounding)
    raise StrictAgentError(
        "患者 Agent 多次修正后仍未通过病例事实校验，当前轮次已停止。",
        code="patient_grounding_failed",
        status_code=502,
    )


def patient_reply(case: dict[str, Any], history: list[dict[str, Any]], doctor_text: str) -> dict[str, Any]:
    facts = case.get("patient_facts", [])
    allowed_intents = {
        "question",
        "diagnosis_assertion",
        "diagnosis_question",
        "differential",
        "other",
    }

    def validate(data: dict[str, Any]) -> dict[str, Any]:
        action = data.get("action")
        intent = data.get("intent")
        candidate = data.get("diagnosis_candidate")
        correct = data.get("diagnosis_correct")
        if action not in {"reply", "diagnosis_correct"} or intent not in allowed_intents:
            raise ValueError("patient dialogue action or intent is invalid")
        if not isinstance(candidate, str) or not isinstance(correct, bool):
            raise ValueError("diagnosis_candidate and diagnosis_correct are required")
        candidate = " ".join(candidate.split())[:80]
        reply = " ".join(str(data.get("reply") or "").split())
        raw_supported = data.get("supported_fact_ids")
        if not isinstance(raw_supported, list) or any(
            not isinstance(item, str) for item in raw_supported
        ):
            raise ValueError("supported_fact_ids must be a string array")
        supported = list(dict.fromkeys(item.strip() for item in raw_supported if item.strip()))
        unknown = data.get("unknown")
        if not isinstance(unknown, bool):
            raise ValueError("unknown must be a boolean")
        allowed = {str(item.get("id")) for item in facts if isinstance(item, dict)}
        if not set(supported).issubset(allowed):
            raise ValueError("patient reply is not grounded in patient facts")
        if action == "diagnosis_correct":
            if intent != "diagnosis_assertion" or not correct or not candidate:
                raise ValueError("diagnosis handoff fields conflict")
            if reply or supported or unknown:
                raise ValueError("diagnosis handoff must not contain a patient reply")
        else:
            if correct or not reply:
                raise ValueError("patient reply action fields conflict")
            if not unknown and not supported:
                raise ValueError("grounded patient reply requires supported_fact_ids")
            reply_compact = re.sub(r"\s+", "", reply)
            forbidden_diagnoses = {
                str(item).strip()
                for item in [
                    case.get("diagnosis"),
                    *case.get("diagnosis_aliases", []),
                    candidate,
                ]
                if str(item or "").strip()
            }
            if any(
                re.sub(r"\s+", "", diagnosis) in reply_compact
                for diagnosis in forbidden_diagnoses
            ):
                raise ValueError("patient reply must not repeat or reveal a diagnosis")
        return {
            "action": action,
            "intent": intent,
            "diagnosis_candidate": candidate,
            "diagnosis_correct": correct,
            "reply": reply[:260],
            "supported_fact_ids": supported,
            "unknown": unknown,
            "trace": _agent_trace("patient_agent"),
            "dialogue_trace": _agent_trace("patient_dialogue_controller"),
        }

    dialogue = history
    if dialogue and dialogue[-1].get("role") == "doctor":
        dialogue = dialogue[:-1]
    recent_dialogue = [
        {
            "role": str(turn.get("role") or ""),
            "stage": str(turn.get("stage") or ""),
            "content": " ".join(str(turn.get("content") or "").split())[:320],
        }
        for turn in dialogue[-12:]
    ]
    payload = {
        "task": "只根据病例事实生成患者身份的自然回答；诊断交接由独立门控处理",
        "target_diagnosis": case.get("diagnosis"),
        "diagnosis_aliases": case.get("diagnosis_aliases", []),
        "patient_facts": facts,
        "allowed_fact_ids": [str(item.get("id")) for item in facts],
        "recent_dialogue": recent_dialogue,
        "doctor_message": doctor_text,
        "allowed_actions": ["reply"],
        "allowed_intents": sorted(allowed_intents),
        "required_output": {
            "action": "reply",
            "intent": "one allowed intent",
            "diagnosis_candidate": "医生实际说出的病名；没有则为空字符串",
            "diagnosis_correct": "boolean",
            "reply": "string，完整患者口语",
            "supported_fact_ids": ["零个或多个 allowed_fact_ids"],
            "unknown": "boolean，病例未提供答案时为 true",
        },
    }
    base_messages = [
            {
                "role": "system",
                "content": (
                    "你是口腔医学教学中的虚拟患者 Agent，同时负责本轮对话动作。"
                    "你的公开任务是生成患者台词；诊断交接由并行的专用诊断门控 Agent 独立决定。"
                    "你必须 action=reply、diagnosis_correct=false，不得自行触发诊断交接。"
                    "疑问、求证、否定、排除、鉴别、假设、不确定可能性和错误诊断都应继续患者对话。"
                    "诊断相关话语要提取医生实际说出的 diagnosis_candidate；普通问诊没有病名时填空字符串。"
                    "action=reply 时你只能使用 patient_facts 回答，不知道自己的诊断，也不得评价医生诊断对错。"
                    "当医生询问、猜测或给出错误诊断时，不得用‘是的、对、没错’确认，也不得否定诊断；"
                    "只能用普通人的口吻表示自己不懂这些并令 unknown=true。reply 中绝对不能复述或泄露"
                    "target_diagnosis、diagnosis_aliases 或 diagnosis_candidate，即使医生刚刚说出了病名也不能复述。"
                    "不能说‘需要进一步检查确认’，不能自行提出拍片、检查、用药、治疗或下一步计划。"
                    "病例未提供的信息应自然回答不知道或记不清并令 unknown=true；禁止医疗建议。"
                    "所有患者台词都必须生活化、简短、像真实门诊对话。把‘不适、一月余、患牙、辅助检查’等病历词"
                    "改成普通人会说的话；不要照抄病历，不要使用医生式总结。"
                    "supported_fact_ids 只能逐字复制 allowed_fact_ids。不要解释判断过程。"
                    "只输出 action、intent、diagnosis_candidate、diagnosis_correct、reply、supported_fact_ids、unknown 的严格 JSON。"
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    messages = base_messages
    gate_proposal = {
        "intent": "other",
        "diagnosis_candidate": "",
        "diagnosis_correct": False,
    }
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="clinical-patient") as executor:
        gate_future = executor.submit(
            _verify_diagnosis_decision,
            case,
            doctor_text,
            gate_proposal,
        )
        dialogue_future = executor.submit(
            _strict_json_call,
            messages,
            validate,
            max_tokens=4096,
            agent_label="患者对话 Agent",
        )
        diagnosis_gate = gate_future.result()
        first_result = None if diagnosis_gate["approved"] else dialogue_future.result()

    if diagnosis_gate["approved"]:
        return {
            "action": "diagnosis_correct",
            "intent": "diagnosis_assertion",
            "diagnosis_candidate": diagnosis_gate["diagnosis_candidate"],
            "diagnosis_correct": True,
            "reply": "",
            "supported_fact_ids": [],
            "unknown": False,
            "trace": _agent_trace("patient_dialogue_controller"),
            "dialogue_trace": _agent_trace("patient_dialogue_controller"),
            "diagnosis_verification": diagnosis_gate["trace"],
            "repair_attempts": 0,
        }

    last_failure: dict[str, Any] | None = None
    for attempt in range(MAX_PATIENT_DIALOGUE_ATTEMPTS):
        result = first_result if attempt == 0 else _strict_json_call(
            messages,
            validate,
            max_tokens=4096,
            agent_label="患者对话 Agent",
        )
        if result is None:
            raise StrictAgentError(
                "患者 Agent 未形成有效回答，当前轮次未提交。",
                code="patient_dialogue_failed",
                status_code=502,
            )
        if result["action"] == "diagnosis_correct":
            verification = diagnosis_gate
            if verification["approved"]:
                return {
                    **result,
                    "trace": result["dialogue_trace"],
                    "diagnosis_candidate": verification["diagnosis_candidate"],
                    "diagnosis_verification": verification["trace"],
                    "repair_attempts": attempt,
                }
            last_failure = {
                "validation_error": "diagnosis_handoff_rejected",
                "reason_code": verification["reason_code"],
                "invalid_result": {
                    key: result[key]
                    for key in ("action", "intent", "diagnosis_candidate", "diagnosis_correct")
                },
                "instruction": (
                    "诊断复核未通过。重新执行原任务并返回完整 JSON；本次不得触发交接，"
                    "应以患者身份自然回答且不得评价诊断对错。不要解释。"
                ),
            }
            messages = [
                *base_messages,
                {"role": "user", "content": json.dumps(last_failure, ensure_ascii=False)},
            ]
            continue
        grounding = _verify_patient_grounding(
            facts,
            result["reply"],
            doctor_message=doctor_text,
            declared_unknown=result["unknown"],
        )
        if grounding["grounded"] and not grounding["unsupported_claims"]:
            return {
                **result,
                "dialogue_intent": result["intent"],
                "grounding": grounding["trace"],
                "repair_attempts": attempt,
            }
        last_failure = {
            "validation_error": "patient_reply_not_grounded",
            "unsupported_claims": grounding["unsupported_claims"],
            "invalid_reply": result["reply"],
            "instruction": (
                "重新执行原任务，删除所有无 patient_facts 支持的客观陈述并返回新的完整 JSON。"
                "若病例没有答案，自然回答不知道并令 unknown=true。不要解释。"
            ),
        }
        messages = [
            *base_messages,
            {"role": "user", "content": json.dumps(last_failure, ensure_ascii=False)},
        ]
    logger.error("Patient dialogue failed after agent repairs: %s", last_failure)
    raise StrictAgentError(
        "患者 Agent 多次自主修正后仍未形成可信回答，当前轮次已停止。",
        code="patient_grounding_failed",
        status_code=502,
    )


def build_judge_blueprint(diagnosis: str, evidence: list[dict[str, Any]]) -> dict[str, Any]:
    evidence_indexes = {
        int(item["index"])
        for item in evidence
        if isinstance(item, dict) and isinstance(item.get("index"), int)
    }
    if not evidence_indexes:
        raise StrictAgentError(
            "判官 Agent 没有可用的知识库证据。",
            code="judge_evidence_insufficient",
            status_code=503,
        )
    domain_positions = {
        category: (index, label)
        for index, (category, label) in enumerate(EXAM_DOMAINS, 1)
    }
    domain_evidence_indexes = {
        category: set(evidence_indexes)
        for category, _ in EXAM_DOMAINS
    }

    def normalize_question(category: str, item: Any) -> dict[str, Any]:
        if category not in domain_positions or not isinstance(item, dict):
            raise ValueError(f"question domain must be an object: {category}")
        index, label = domain_positions[category]
        prompt = " ".join(str(item.get("prompt") or "").split())
        points = item.get("rubric_points")
        if not prompt or not isinstance(points, list):
            raise ValueError(f"invalid rubric for category: {category}")
        cleaned_points = [" ".join(str(point).split()) for point in points if str(point).strip()]
        if not 2 <= len(cleaned_points) <= 6:
            raise ValueError(f"rubric_points must contain 2 to 6 non-empty items: {category}")
        if re.fullmatch(r"一道仅考查.+的口头问题", prompt) or any(
            re.fullmatch(r"证据(?:直接)?支持的(?:评分)?要点[一二三四五六0-9]*", point)
            for point in cleaned_points
        ):
            raise ValueError(f"template placeholder content is forbidden: {category}")
        refs = item.get("evidence_refs")
        normalized_refs: list[int] = []
        if isinstance(refs, list):
            for ref in refs:
                if isinstance(ref, bool):
                    continue
                if isinstance(ref, int):
                    normalized_refs.append(ref)
                elif isinstance(ref, str) and ref.strip().isdigit():
                    normalized_refs.append(int(ref.strip()))
        allowed_refs = domain_evidence_indexes[category]
        if (
            not isinstance(refs, list)
            or not normalized_refs
            or len(normalized_refs) != len(refs)
            or any(ref not in allowed_refs for ref in normalized_refs)
        ):
            raise ValueError(f"evidence_refs are invalid for category: {category}")
        return {
            "question_id": f"judge_{index}",
            "category": category,
            "label": label,
            "prompt": prompt[:300],
            "max_score": 20,
            "rubric_points": cleaned_points,
            "evidence_refs": list(dict.fromkeys(normalized_refs))[:6],
        }

    def collect_valid_questions(data: dict[str, Any]) -> dict[str, Any]:
        questions = data.get("questions")
        if not isinstance(questions, dict):
            raise ValueError("questions must be an object keyed by the five required domains")
        valid: dict[str, dict[str, Any]] = {}
        invalid: list[str] = []
        for category, _ in EXAM_DOMAINS:
            try:
                valid[category] = normalize_question(category, questions.get(category))
            except (ValueError, TypeError, KeyError):
                invalid.append(category)
        return {"questions": valid, "invalid_categories": invalid}

    question_contract = {
        category: {
            "prompt": "string",
            "rubric_points": "array of 2 to 6 specific medical strings",
            "evidence_refs": "non-empty array of integer evidence indexes",
        }
        for category, _ in EXAM_DOMAINS
    }
    payload = {
        "diagnosis": diagnosis,
        "evidence": evidence,
        "domain_evidence_indexes": {
            category: sorted(indexes)
            for category, indexes in domain_evidence_indexes.items()
        },
        "required_shape": {"questions": question_contract},
        "instruction": (
            "questions 必须是对象，并完整保留 required_shape 中的五个英文键，不得增删或改名；"
            "每个领域填写一道独立问题、2到6个可被 evidence 直接支持的评分点，以及支持它们的证据 index。"
            "每个领域的 evidence_refs 只能从 domain_evidence_indexes 对应列表中选择。"
            "prompt 和 rubric_points 必须写出具体医学内容，禁止输出字段说明、示例措辞或占位文本。"
        ),
    }
    initial = _strict_json_call(
        [
            {
                "role": "system",
                "content": (
                    "你是口腔医学临床表达训练的判官 Agent。必须仅依据给定知识库证据构建考核，"
                    "不得把病例中的真实治疗记录当作指南，不得添加无证据事实。"
                    "输出只能包含 questions 字段；questions 必须使用指定的五个领域键，"
                    "每个领域只能包含 prompt、rubric_points、evidence_refs。禁止使用 rubic_points 等错误拼写。"
                    "必须返回完整根对象，不得只返回单个领域。只输出严格 JSON。"
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        collect_valid_questions,
        max_tokens=1800,
        agent_label="判官出题 Agent",
    )
    questions_by_category = dict(initial["questions"])
    repaired_categories: list[str] = []
    for category in initial["invalid_categories"]:
        _, label = domain_positions[category]
        allowed_refs = domain_evidence_indexes[category]
        scoped_evidence = [item for item in evidence if item.get("index") in allowed_refs]

        def validate_domain(data: dict[str, Any], *, current_category: str = category) -> dict[str, Any]:
            return normalize_question(current_category, data)

        repaired = _strict_json_call(
            [
                {
                    "role": "system",
                    "content": (
                        "你是口腔医学临床表达训练的判官 Agent。仅依据给定知识库证据生成指定领域的一道考核题。"
                        "不得添加证据没有的医学事实。JSON 根对象只能包含 prompt、rubric_points、evidence_refs 三个字段，"
                        "禁止增加 question 包装层。必须使用 rubric_points 的准确拼写，只输出严格 JSON。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "diagnosis": diagnosis,
                            "category": category,
                            "label": label,
                            "evidence": scoped_evidence,
                            "allowed_evidence_refs": sorted(allowed_refs),
                            "required_shape": {
                                "prompt": "string",
                                "rubric_points": "array of 2 to 6 specific medical strings",
                                "evidence_refs": "non-empty integer array",
                            },
                            "constraints": {
                                "prompt": f"必须是针对{label}的具体口头问题",
                                "rubric_points": "必须是2到6条由证据直接支持的具体医学评分点",
                                "evidence_refs": "必须是allowed_evidence_refs中的整数数组",
                            },
                            "instruction": "禁止输出字段说明、示例措辞或占位文本。",
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            validate_domain,
            max_tokens=700,
            agent_label="判官出题修复 Agent",
        )
        questions_by_category[category] = repaired
        repaired_categories.append(category)

    ordered = [questions_by_category[category] for category, _ in EXAM_DOMAINS]
    trace = _agent_trace("judge_agent")
    trace.update(
        {
            "orchestration": "full_blueprint_with_targeted_agent_repair",
            "repaired_domains": repaired_categories,
        }
    )
    return validate_judge_blueprint({"questions": ordered, "trace": trace}, evidence)


def assess_exam_answer(
    diagnosis: str,
    question: dict[str, Any],
    answer: str,
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    question_refs = {
        ref for ref in question.get("evidence_refs", []) if isinstance(ref, int)
    }
    scoped_evidence = [item for item in evidence if item.get("index") in question_refs]
    allowed_evidence_refs = {
        item["index"]
        for item in scoped_evidence
        if isinstance(item.get("index"), int)
    }
    if not scoped_evidence:
        raise StrictAgentError(
            "当前考核题缺少可追溯的知识库证据。",
            code="judge_evidence_insufficient",
            status_code=503,
        )

    def validate(data: dict[str, Any]) -> dict[str, Any]:
        score = data.get("score")
        if (
            not isinstance(score, (int, float))
            or isinstance(score, bool)
            or not 0 <= float(score) <= 20
        ):
            raise ValueError("score must be between 0 and 20")
        critique = " ".join(str(data.get("critique") or "").split())
        if not critique:
            raise ValueError("critique is required")
        refs = data.get("evidence_refs", [])
        if (
            not isinstance(refs, list)
            or not refs
            or any(not isinstance(ref, int) or isinstance(ref, bool) for ref in refs)
        ):
            raise ValueError("evidence_refs must be a non-empty integer array")
        if any(ref not in allowed_evidence_refs for ref in refs):
            raise ValueError("evidence reference is out of range")
        feedback_lists: dict[str, list[str]] = {}
        for field, limit in (("strengths", 6), ("missing_points", 8), ("errors", 6)):
            values = data.get(field)
            if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
                raise ValueError(f"{field} must be a string array")
            feedback_lists[field] = [item.strip() for item in values if item.strip()][:limit]
        return {
            "question_id": question["question_id"],
            "category": question["category"],
            "label": question["label"],
            "score": round(float(score), 1),
            "max_score": 20,
            "critique": critique[:900],
            "strengths": feedback_lists["strengths"],
            "missing_points": feedback_lists["missing_points"],
            "errors": feedback_lists["errors"],
            "evidence_refs": refs[:8],
            "trace": _agent_trace("judge_agent"),
        }

    payload = {
        "diagnosis": diagnosis,
        "question": question,
        "student_answer": answer,
        "evidence": scoped_evidence,
        "allowed_evidence_refs": sorted(allowed_evidence_refs),
        "required_output_shape": {
            "score": "JSON number between 0 and 20",
            "critique": "string",
            "strengths": "array of strings",
            "missing_points": "array of strings",
            "errors": "array of strings",
            "evidence_refs": "non-empty array of integers",
        },
        "instruction": (
            "evidence_refs 必须至少包含一个整数，并且只能逐项复制 allowed_evidence_refs 中的编号；"
            "score 不得使用字符串、百分比或‘分数/20’形式。"
        ),
    }
    return _strict_json_call(
        [
            {
                "role": "system",
                "content": (
                    "你是严格的口腔医学判官 Agent。根据固定评分要点和知识库证据评价学生回答。"
                    "不得因表达流畅而掩盖医学错误，不得引用未提供的知识，不得虚构证据。只输出严格 JSON。"
                    "JSON 根对象只能包含 score、critique、strengths、missing_points、errors、evidence_refs；"
                    "score 必须是 0 到 20 的 JSON 数字，evidence_refs 必须是非空整数数组。"
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        validate,
        max_tokens=1000,
        contract_attempts=MAX_JUDGE_CONTRACT_ATTEMPTS,
        transient_attempts=MAX_JUDGE_TRANSIENT_ATTEMPTS,
        agent_label="判官评分 Agent",
    )
