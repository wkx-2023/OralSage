from __future__ import annotations

import json
import re
import time
from typing import Any

from backend.services.decision import run_decision_assist
from backend.services.image import run_image_analysis
from backend.services.literature import run_literature_search
from backend.services.llm_client import LLMConfigurationError, LLMRequestError, chat_completion, is_llm_configured
from backend.services.pathology import run_pathology_ask
from backend.services.record_workflow import run_workflow
from backend.services.stats import run_stats_query


AGENT_REGISTRY: dict[str, dict[str, str]] = {
    "oral_pathology_assistant": {
        "label": "口腔病理知识问答助手",
        "description": "疾病定义、病理机制、病理特征、基础知识解释、教学型问答。",
    },
    "oral_cases_assistant": {
        "label": "口腔决策辅助助手",
        "description": "具体病例的鉴别诊断、处理原则、治疗路径和随访建议。",
    },
    "clinic_case_stats_assistant": {
        "label": "门诊病例统计助手",
        "description": "本地脱敏门诊病例统计、分布、占比、趋势、样本病例查询。",
    },
    "oral_frontier_learning_assistant": {
        "label": "口腔前沿学习助手",
        "description": "PubMed/Elsevier 文献检索、最新研究进展、中文综述。",
    },
    "oral_dentist_report_assistant": {
        "label": "口腔病历生成助手",
        "description": "医患对话纠错、字段抽取、标准门诊病历生成。",
    },
    "oral_disease_image_recognition_assistant": {
        "label": "口腔图像识别助手",
        "description": "口腔照片、牙片、全景片、CBCT 截图等影像征象分析。",
    },
}

ALLOWED_AGENTS = set(AGENT_REGISTRY)


def _compact(value: Any, limit: int = 900) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _json_loads_loose(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            raise
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("router plan must be a JSON object")
    return data


def _agent_descriptions() -> str:
    lines = []
    for name, item in AGENT_REGISTRY.items():
        lines.append(f"- {name}: {item['label']}；{item['description']}")
    return "\n".join(lines)


def _build_router_messages(
    message: str,
    *,
    image_base64: str | None,
    history: list[dict[str, Any]],
    context: dict[str, Any],
) -> list[dict[str, str]]:
    system = f"""
你是 Oral_Master_Router_Agent，总控智能调度中心。

你的职责：
1. 只做意图识别、字段抽取、任务编排，不直接回答医学问题。
2. 只能选择白名单里的 agent。
3. 输出必须是严格 JSON，不要输出 Markdown、解释性正文或代码块。
4. 当信息不足以安全调用专业 agent 时，输出 mode="clarify" 并给出澄清问题。
5. 医疗建议必须交给专业 agent，不能由你直接生成。

可用 agent：
{_agent_descriptions()}

路由优先级：
P0: 只要请求包含真实图片 image_base64，优先考虑 oral_disease_image_recognition_assistant。
P1: 具体病例，包含年龄/性别/主诉/检查/影像描述/治疗咨询，路由 oral_cases_assistant。
P2: 定义、病理机制、病理特征、教学解释，路由 oral_pathology_assistant。
P3: 医患对话整理、字段抽取、生成门诊病历，路由 oral_dentist_report_assistant。
P4: 统计、分布、占比、趋势、多少例，路由 clinic_case_stats_assistant。
P5: 最新研究、文献、PubMed、Elsevier、近几年/近半年进展、综述，路由 oral_frontier_learning_assistant。

输出 schema：
单 agent：
{{
  "intent": "clinical_decision|pathology_qa|clinic_stats|frontier_literature|record_generation|image_analysis",
  "mode": "single_agent",
  "target_agent": "白名单 agent 名",
  "confidence": 0.0,
  "payload": {{}},
  "reason": "简短说明"
}}

多 agent：
{{
  "intent": "image_to_decision|record_to_decision|pathology_with_literature",
  "mode": "multi_agent",
  "confidence": 0.0,
  "steps": [
    {{"agent": "白名单 agent 名", "payload": {{}}, "depends_on": []}}
  ],
  "reason": "简短说明"
}}

澄清：
{{
  "intent": "clarification",
  "mode": "clarify",
  "confidence": 0.0,
  "question": "需要向用户追问的问题",
  "reason": "为什么不能直接路由"
}}

payload 字段要求：
- oral_pathology_assistant: {{"question": "...", "top_k": 8, "allow_web": true}}
- oral_cases_assistant: {{"case_text": "...", "top_k": 8, "allow_web": true}}
- clinic_case_stats_assistant: {{"query": "...", "top_k": 10}}
- oral_frontier_learning_assistant: {{"query": "...", "months": 6, "limit": 5}}
- oral_dentist_report_assistant: {{"conversation": "..."}}
- oral_disease_image_recognition_assistant: {{"image_base64": "<from_request>", "query": "...", "allow_web": true}}
""".strip()
    user = {
        "message": message,
        "has_image_base64": bool(image_base64),
        "history": history[-8:],
        "context": context,
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]


def _looks_like_case(message: str) -> bool:
    clinical = ("患者", "男", "女", "岁", "主诉", "现病史", "检查", "影像", "X片", "牙片", "肿痛", "龋", "阻生", "拔", "治疗", "处理", "诊断")
    ask_decision = ("怎么治", "如何处理", "治疗方案", "处理建议", "是否需要", "能不能", "诊疗", "决策", "建议")
    return any(word in message for word in clinical) and any(word in message for word in ask_decision)


def _rule_based_plan(message: str, *, image_base64: str | None) -> dict[str, Any]:
    text = message or ""
    has_image = bool(image_base64)
    wants_decision = _looks_like_case(text)
    if has_image and wants_decision:
        return {
            "intent": "image_to_decision",
            "mode": "multi_agent",
            "confidence": 0.82,
            "steps": [
                {
                    "agent": "oral_disease_image_recognition_assistant",
                    "payload": {"image_base64": "<from_request>", "query": text, "allow_web": True},
                    "depends_on": [],
                },
                {
                    "agent": "oral_cases_assistant",
                    "payload": {"case_text": text, "top_k": 8, "allow_web": True},
                    "depends_on": [0],
                },
            ],
            "reason": "检测到影像输入和病例处理请求，先做影像分析，再进入临床决策辅助。",
        }
    if has_image or any(word in text for word in ("图片", "影像", "牙片", "全景片", "X光", "X线", "CBCT", "片子")):
        if not has_image and any(word in text for word in ("看", "分析", "识别", "上传")):
            return {
                "intent": "clarification",
                "mode": "clarify",
                "confidence": 0.7,
                "question": "你提到了影像分析，但当前请求没有图片。请上传牙片或补充影像文字描述。",
                "reason": "影像识别 agent 需要 image_base64 输入。",
            }
        return {
            "intent": "image_analysis",
            "mode": "single_agent",
            "target_agent": "oral_disease_image_recognition_assistant",
            "confidence": 0.8,
            "payload": {"image_base64": "<from_request>", "query": text, "allow_web": True},
            "reason": "检测到影像相关请求。",
        }
    if any(word in text for word in ("病历", "医患对话", "对话整理", "字段抽取", "生成门诊", "SOAP", "主诉和现病史")):
        return {
            "intent": "record_generation",
            "mode": "single_agent",
            "target_agent": "oral_dentist_report_assistant",
            "confidence": 0.84,
            "payload": {"conversation": text},
            "reason": "检测到病历生成或病历字段抽取需求。",
        }
    if any(word in text for word in ("统计", "占比", "分布", "趋势", "对比", "比较", "分别", "多少例", "多少人", "就诊人数", "就诊量", "病例数", "门诊数据", "门诊情况", "样本")):
        return {
            "intent": "clinic_stats",
            "mode": "single_agent",
            "target_agent": "clinic_case_stats_assistant",
            "confidence": 0.84,
            "payload": {"query": text, "top_k": 10},
            "reason": "检测到门诊病例统计需求。",
        }
    if any(word.lower() in text.lower() for word in ("最新", "文献", "PubMed".lower(), "Elsevier".lower(), "研究进展", "综述", "近半年", "近一年")):
        return {
            "intent": "frontier_literature",
            "mode": "single_agent",
            "target_agent": "oral_frontier_learning_assistant",
            "confidence": 0.82,
            "payload": {"query": text, "months": 6, "limit": 5},
            "reason": "检测到前沿文献或研究进展需求。",
        }
    if wants_decision:
        return {
            "intent": "clinical_decision",
            "mode": "single_agent",
            "target_agent": "oral_cases_assistant",
            "confidence": 0.78,
            "payload": {"case_text": text, "top_k": 8, "allow_web": True},
            "reason": "检测到具体病例和处理建议需求。",
        }
    return {
        "intent": "pathology_qa",
        "mode": "single_agent",
        "target_agent": "oral_pathology_assistant",
        "confidence": 0.68,
        "payload": {"question": text, "top_k": 8, "allow_web": True},
        "reason": "未命中特定工作流，默认按口腔病理知识问答处理。",
    }


def _normalize_payload(agent: str, payload: dict[str, Any], message: str, image_base64: str | None) -> dict[str, Any]:
    payload = dict(payload or {})
    if payload.get("image_base64") == "<from_request>":
        payload["image_base64"] = image_base64

    if agent == "oral_pathology_assistant":
        payload["question"] = str(payload.get("question") or payload.get("query") or message)
        payload["top_k"] = int(payload.get("top_k") or 8)
        payload["allow_web"] = bool(payload.get("allow_web", True))
    elif agent == "oral_cases_assistant":
        payload["case_text"] = str(payload.get("case_text") or payload.get("query") or payload.get("question") or message)
        payload["top_k"] = int(payload.get("top_k") or 8)
        payload["allow_web"] = bool(payload.get("allow_web", True))
    elif agent == "clinic_case_stats_assistant":
        payload["query"] = str(payload.get("query") or message)
        payload["top_k"] = int(payload.get("top_k") or 10)
    elif agent == "oral_frontier_learning_assistant":
        payload["query"] = str(payload.get("query") or message)
        payload["months"] = payload.get("months")
        payload["limit"] = int(payload.get("limit") or 5)
    elif agent == "oral_dentist_report_assistant":
        payload["conversation"] = str(payload.get("conversation") or payload.get("text") or message)
    elif agent == "oral_disease_image_recognition_assistant":
        payload["image_base64"] = payload.get("image_base64") or image_base64
        payload["query"] = str(payload.get("query") or message)
        payload["allow_web"] = bool(payload.get("allow_web", True))
    return payload


def _validate_plan(plan: dict[str, Any], message: str, image_base64: str | None) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    mode = str(plan.get("mode") or "single_agent")
    if mode not in {"single_agent", "multi_agent", "clarify"}:
        errors.append(f"unsupported mode: {mode}")
        mode = "single_agent"
        plan["mode"] = mode

    if mode == "clarify":
        fallback = _rule_based_plan(message, image_base64=image_base64)
        if fallback.get("mode") != "clarify" and float(fallback.get("confidence") or 0) >= 0.78:
            errors.append("router clarify overridden by high-confidence local intent rules")
            fallback["router_fallback_from"] = "clarify"
            return fallback, errors
        question = str(plan.get("question") or "请补充更具体的信息后再试。")
        plan["question"] = question
        plan["confidence"] = float(plan.get("confidence") or 0.5)
        return plan, errors

    if mode == "single_agent":
        agent = str(plan.get("target_agent") or "")
        if agent not in ALLOWED_AGENTS:
            errors.append(f"target_agent is not allowed: {agent}")
            fallback = _rule_based_plan(message, image_base64=image_base64)
            fallback["router_fallback_from"] = agent or "missing"
            return _validate_plan(fallback, message, image_base64)[0], errors
        plan["payload"] = _normalize_payload(agent, plan.get("payload") or {}, message, image_base64)
        plan["confidence"] = float(plan.get("confidence") or 0.5)
        return plan, errors

    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append("multi_agent plan requires non-empty steps")
        fallback = _rule_based_plan(message, image_base64=image_base64)
        return _validate_plan(fallback, message, image_base64)[0], errors

    normalized_steps: list[dict[str, Any]] = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            errors.append(f"step {index} is not an object")
            continue
        agent = str(step.get("agent") or "")
        if agent not in ALLOWED_AGENTS:
            errors.append(f"step {index} agent is not allowed: {agent}")
            continue
        depends_on = step.get("depends_on") or []
        if not isinstance(depends_on, list):
            depends_on = []
        normalized_steps.append(
            {
                "agent": agent,
                "payload": _normalize_payload(agent, step.get("payload") or {}, message, image_base64),
                "depends_on": [int(i) for i in depends_on if isinstance(i, int) or str(i).isdigit()],
            }
        )
    if not normalized_steps:
        fallback = _rule_based_plan(message, image_base64=image_base64)
        return _validate_plan(fallback, message, image_base64)[0], errors
    plan["steps"] = normalized_steps
    plan["confidence"] = float(plan.get("confidence") or 0.5)
    return plan, errors


def _result_text(result: dict[str, Any]) -> str:
    if not isinstance(result, dict):
        return _compact(result)
    if result.get("module") == "clinic_stats" or ("tables" in result and "matched_count" in result):
        return _format_stats_result(result)
    for key in ("answer", "summary", "markdown", "message"):
        if isinstance(result.get(key), str) and result[key].strip():
            return result[key]
    nested = result.get("result")
    if isinstance(nested, dict):
        for key in ("report_text", "markdown", "answer", "summary", "message"):
            if isinstance(nested.get(key), str) and nested[key].strip():
                return nested[key]
    return _compact(result)


def _format_stats_result(result: dict[str, Any]) -> str:
    report = result.get("report")
    if isinstance(report, str) and report.strip():
        return report.strip()

    summary = str(result.get("summary") or "").strip()
    tables = result.get("tables") if isinstance(result.get("tables"), list) else []
    target = result.get("target_resolution") if isinstance(result.get("target_resolution"), dict) else {}
    lines: list[str] = []

    if summary:
        lines.extend(["## 统计结论", summary])

    if target:
        if target.get("matched") is False:
            reason = target.get("reason") or "本地字段中未找到可靠匹配。"
            lines.extend(["", "## 字段匹配", f"- 未找到可靠匹配：{reason}"])
        elif target.get("canonical_term"):
            user_term = target.get("user_term") or "用户输入"
            canonical = target.get("canonical_term")
            field = target.get("field") or "unknown"
            confidence = target.get("confidence")
            diagnosis_names = target.get("diagnosis_names") if isinstance(target.get("diagnosis_names"), list) else []
            lines.extend(
                [
                    "",
                    "## 字段匹配",
                    f"- {user_term} -> {canonical}",
                    f"- 匹配字段：{field}",
                    f"- 置信度：{confidence}",
                ]
            )
            if diagnosis_names:
                lines.append(f"- 纳入诊断：{'、'.join(str(item) for item in diagnosis_names[:12])}")

    rendered = 0
    preferred_titles = (
        "月度就诊趋势",
        "操作名称分布",
        "处理/治疗计划语义归并",
        "检查/辅查类型分布",
        "挂号类别构成",
        "诊断分布",
    )
    ordered_tables = []
    for title in preferred_titles:
        ordered_tables.extend(table for table in tables if table.get("title") == title)
    ordered_tables.extend(table for table in tables if table not in ordered_tables and table.get("title") != "统计口径")

    for table in ordered_tables:
        title = str(table.get("title") or "明细")
        rows = table.get("rows") if isinstance(table.get("rows"), list) else []
        if not rows:
            continue
        lines.extend(["", f"## {title}"])
        for row in rows[:8]:
            if not isinstance(row, list) or not row:
                continue
            label = str(row[0])
            count = row[1] if len(row) > 1 else ""
            pct = row[2] if len(row) > 2 else ""
            suffix = f"，占比 {pct}" if pct != "" else ""
            lines.append(f"- {label}：{count} 例{suffix}")
        if len(rows) > 8:
            lines.append(f"- 其余 {len(rows) - 8} 项已省略，可在门诊统计页查看完整图表。")
        rendered += 1
        if rendered >= 4:
            break

    if not lines:
        return _compact(result)
    return "\n".join(lines)


def _merge_dependency_payload(payload: dict[str, Any], agent: str, dependency_results: list[dict[str, Any]]) -> dict[str, Any]:
    if not dependency_results:
        return payload
    payload = dict(payload)
    dependency_text = "\n\n".join(
        f"上游步骤 {item.get('step_index')}: {_compact(_result_text(item.get('result') or {}), 1200)}"
        for item in dependency_results
    )
    if agent == "oral_cases_assistant":
        payload["case_text"] = f"{payload.get('case_text') or ''}\n\n可参考的上游分析结果：\n{dependency_text}".strip()
    elif agent == "oral_dentist_report_assistant":
        payload["conversation"] = f"{payload.get('conversation') or ''}\n\n补充信息：\n{dependency_text}".strip()
    elif agent == "oral_pathology_assistant":
        payload["question"] = f"{payload.get('question') or ''}\n\n补充信息：\n{dependency_text}".strip()
    return payload


async def _invoke_agent(agent: str, payload: dict[str, Any]) -> dict[str, Any]:
    if agent == "oral_pathology_assistant":
        return await run_pathology_ask(payload["question"], payload["top_k"], payload["allow_web"])
    if agent == "oral_cases_assistant":
        return await run_decision_assist(payload["case_text"], payload["top_k"], payload["allow_web"])
    if agent == "clinic_case_stats_assistant":
        return run_stats_query(payload["query"], payload["top_k"])
    if agent == "oral_frontier_learning_assistant":
        return await run_literature_search(payload["query"], payload.get("months"), payload["limit"])
    if agent == "oral_dentist_report_assistant":
        return {
            "ok": True,
            "module": "record_workflow",
            "result": run_workflow(payload["conversation"]),
        }
    if agent == "oral_disease_image_recognition_assistant":
        return await run_image_analysis(
            image_base64=payload.get("image_base64") or "",
            query=payload.get("query"),
            allow_web=payload.get("allow_web", True),
        )
    return {"ok": False, "message": f"unknown agent: {agent}"}


async def _execute_plan(plan: dict[str, Any], message: str, image_base64: str | None) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    steps_output: list[dict[str, Any]] = []
    if plan.get("mode") == "clarify":
        final = {
            "ok": True,
            "module": "master_router",
            "answer": plan["question"],
            "needs_clarification": True,
        }
        return final, steps_output, errors

    if plan.get("mode") == "single_agent":
        agent = plan["target_agent"]
        payload = _normalize_payload(agent, plan.get("payload") or {}, message, image_base64)
        start = time.perf_counter()
        result = await _invoke_agent(agent, payload)
        elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
        steps_output.append({"step_index": 0, "agent": agent, "payload": payload, "elapsed_ms": elapsed_ms, "result": result})
        return result, steps_output, errors

    for index, step in enumerate(plan.get("steps") or []):
        agent = step["agent"]
        dependencies = [
            steps_output[i]
            for i in step.get("depends_on", [])
            if isinstance(i, int) and 0 <= i < len(steps_output)
        ]
        payload = _merge_dependency_payload(step.get("payload") or {}, agent, dependencies)
        payload = _normalize_payload(agent, payload, message, image_base64)
        start = time.perf_counter()
        try:
            result = await _invoke_agent(agent, payload)
        except Exception as exc:
            result = {"ok": False, "message": str(exc)}
            errors.append(f"step {index} failed: {exc}")
        elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
        steps_output.append({"step_index": index, "agent": agent, "payload": payload, "elapsed_ms": elapsed_ms, "result": result})

    final = steps_output[-1]["result"] if steps_output else {"ok": False, "message": "no steps executed"}
    return final, steps_output, errors


def _route_with_llm(
    message: str,
    *,
    image_base64: str | None,
    history: list[dict[str, Any]],
    context: dict[str, Any],
) -> tuple[dict[str, Any], str, list[str]]:
    errors: list[str] = []
    if not is_llm_configured("ROUTER_LLM_API_KEY", "QWEN_API_KEY", "DASHSCOPE_API_KEY"):
        plan = _rule_based_plan(message, image_base64=image_base64)
        return plan, "rules", ["router LLM is not configured; used rule fallback"]
    try:
        raw = chat_completion(
            _build_router_messages(message, image_base64=image_base64, history=history, context=context),
            temperature=0.0,
            max_tokens=1600,
            api_key_names=("ROUTER_LLM_API_KEY", "QWEN_API_KEY", "DASHSCOPE_API_KEY"),
            base_url_names=("ROUTER_LLM_BASE_URL", "QWEN_BASE_URL", "DASHSCOPE_BASE_URL"),
            model_names=("ROUTER_LLM_MODEL", "QWEN_MODEL", "DASHSCOPE_MODEL"),
        )
        return _json_loads_loose(raw), "llm", errors
    except (LLMConfigurationError, LLMRequestError, json.JSONDecodeError, ValueError, TypeError) as exc:
        errors.append(f"router LLM fallback used: {exc}")
        return _rule_based_plan(message, image_base64=image_base64), "rules", errors


async def run_master_router(
    *,
    message: str,
    image_base64: str | None = None,
    history: list[dict[str, Any]] | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    message = (message or "").strip()
    if not message and not image_base64:
        return {
            "ok": False,
            "module": "master_router",
            "message": "message or image_base64 is required",
            "route": {},
            "steps": [],
            "final_result": {},
            "trace": {"errors": ["empty request"]},
        }

    history = history or []
    context = context or {}
    started = time.perf_counter()
    raw_plan, route_source, route_errors = _route_with_llm(message, image_base64=image_base64, history=history, context=context)
    plan, validation_errors = _validate_plan(raw_plan, message, image_base64)
    final_result, steps, execution_errors = await _execute_plan(plan, message, image_base64)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    errors = route_errors + validation_errors + execution_errors

    return {
        "ok": bool(final_result.get("ok", True)) if isinstance(final_result, dict) else True,
        "module": "master_router",
        "answer": _result_text(final_result),
        "route": plan,
        "steps": steps,
        "final_result": final_result,
        "trace": {
            "route_source": route_source,
            "selected_agent": plan.get("target_agent") or (steps[-1]["agent"] if steps else None),
            "confidence": plan.get("confidence"),
            "reason": plan.get("reason"),
            "elapsed_ms": elapsed_ms,
            "errors": errors,
        },
    }
