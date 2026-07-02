from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.services.env_loader import load_project_env
from backend.services.llm_client import chat_completion, is_llm_configured


DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "course_graph.yaml"
DEFAULT_TEXTBOOK_ID = "operative_endodontics_zhouxuedong_5e"
ALLOWED_RELATIONS = {
    "progresses_to",
    "causes_or_predisposes",
    "shares_mechanism_with",
    "differentiates_from",
    "complicates_to",
    "treated_similarly_to",
    "treated_differently_from",
    "subtype_of",
    "symptom_overlap",
}
ALLOWED_DIMENSIONS = {
    "etiology",
    "mechanism",
    "symptoms",
    "diagnosis",
    "treatment",
    "prognosis",
    "complication",
    "classification",
}


def stable_id(prefix: str, *parts: object, length: int = 16) -> str:
    raw = "||".join(str(part) for part in parts)
    digest = hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:length]
    return f"{prefix}:{digest}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def compact(text: Any, limit: int = 260) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value if len(value) <= limit else value[: limit - 3].rstrip() + "..."


def clean_text(value: Any, limit: int = 420) -> str:
    return compact(value, limit).strip(" \n\t")


def clean_name(value: Any) -> str:
    text = re.sub(r"\s+", "", str(value or "")).strip()
    text = text.strip(" ：:，,。；;、·. ")
    return text


def as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def as_strings(value: Any, *, limit: int = 18, item_limit: int = 220) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in as_list(value):
        text = clean_text(item, item_limit)
        if text and text not in seen:
            result.append(text)
            seen.add(text)
        if len(result) >= limit:
            break
    return result


def merge_strings(values: Iterable[Any], *, limit: int = 24, item_limit: int = 240) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        for item in as_strings(value, limit=limit, item_limit=item_limit):
            if item not in seen:
                result.append(item)
                seen.add(item)
            if len(result) >= limit:
                return result
    return result


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def read_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                yield value


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            count += 1
    return count


def extract_first_json_value(text: str, opener: str, closer: str) -> str:
    value = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", value, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        value = fenced.group(1).strip()
    start = value.find(opener)
    if start < 0:
        return value
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(value)):
        char = value[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return value[start : index + 1]
    return value[start:]


def extract_json_object(text: str) -> dict[str, Any]:
    parsed = json.loads(extract_first_json_value(text, "{", "}"))
    if not isinstance(parsed, dict):
        raise json.JSONDecodeError("Expected JSON object", text, 0)
    return parsed


def llm_env(config: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    kg_cfg = config.get("course_graph") or {}
    model_env = kg_cfg.get("model_env") or {}
    return {
        "api_key_names": tuple(
            model_env.get("api_key_names")
            or ("COURSE_GRAPH_LLM_API_KEY", "QWEN_API_KEY", "DASHSCOPE_API_KEY", "OPENAI_API_KEY")
        ),
        "base_url_names": tuple(
            model_env.get("base_url_names")
            or ("COURSE_GRAPH_LLM_BASE_URL", "QWEN_BASE_URL", "DASHSCOPE_BASE_URL", "OPENAI_BASE_URL")
        ),
        "model_names": tuple(
            model_env.get("model_names")
            or ("COURSE_GRAPH_LLM_MODEL", "QWEN_MODEL", "DASHSCOPE_MODEL", "OPENAI_MODEL")
        ),
    }


def llm_max_tokens(config: dict[str, Any], fallback: int = 8000) -> int:
    value = os.getenv("DISEASE_CORE_LLM_MAX_TOKENS")
    if value:
        try:
            return max(2000, int(value))
        except ValueError:
            pass
    kg_cfg = config.get("course_graph") or {}
    configured = ((kg_cfg.get("llm") or {}).get("disease_core_max_tokens") or fallback)
    return int(configured)


def llm_json(
    messages: list[dict[str, str]],
    config: dict[str, Any],
    *,
    debug_path: Path,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    kg_cfg = config.get("course_graph") or {}
    llm_cfg = kg_cfg.get("llm") or {}
    env = llm_env(config)
    text = chat_completion(
        messages,
        temperature=float(llm_cfg.get("temperature") or 0.05),
        max_tokens=max_tokens or llm_max_tokens(config),
        api_key_names=env["api_key_names"],
        base_url_names=env["base_url_names"],
        model_names=env["model_names"],
    )
    debug_path.parent.mkdir(parents=True, exist_ok=True)
    debug_path.with_suffix(".raw.txt").write_text(text, encoding="utf-8")
    try:
        parsed = extract_json_object(text)
        write_json(debug_path.with_suffix(".json"), parsed)
        return parsed
    except json.JSONDecodeError as first_error:
        debug_path.with_suffix(".parse_error.txt").write_text(str(first_error), encoding="utf-8")
        repaired = repair_json_with_llm(text, config, debug_path.with_suffix(".repair"))
        repaired["_raw_json_repaired"] = True
        write_json(debug_path.with_suffix(".repaired.json"), repaired)
        return repaired


def repair_json_with_llm(raw_text: str, config: dict[str, Any], debug_path: Path) -> dict[str, Any]:
    env = llm_env(config)
    text = chat_completion(
        [
            {
                "role": "system",
                "content": "你是严格的 JSON 修复器。只修复语法，不改写医学内容。只输出合法 JSON 对象。",
            },
            {
                "role": "user",
                "content": "修复下面这个 JSON 对象，保持字段与医学含义不变：\n\n" + raw_text[:50000],
            },
        ],
        temperature=0.0,
        max_tokens=llm_max_tokens(config),
        api_key_names=env["api_key_names"],
        base_url_names=env["base_url_names"],
        model_names=env["model_names"],
    )
    debug_path.parent.mkdir(parents=True, exist_ok=True)
    debug_path.with_suffix(".raw.txt").write_text(text, encoding="utf-8")
    return extract_json_object(text)


def compact_card(card: dict[str, Any]) -> dict[str, Any]:
    evidence = []
    for item in as_list(card.get("evidence"))[:2]:
        if not isinstance(item, dict):
            continue
        evidence.append(
            {
                "page": item.get("page"),
                "quote_or_paraphrase": clean_text(item.get("quote_or_paraphrase"), 150),
            }
        )
    return {
        "id": card.get("id"),
        "card_type": card.get("card_type"),
        "title": card.get("title"),
        "summary": clean_text(card.get("summary"), 180),
        "core_points": as_strings(card.get("core_points"), limit=3, item_limit=90),
        "evidence": evidence,
    }


def compact_relation(relation: dict[str, Any], cards_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    source_id = str(relation.get("source_card_id") or "")
    target_id = str(relation.get("target_card_id") or "")
    return {
        "id": relation.get("id"),
        "relation_type": relation.get("relation_type"),
        "source_card_id": source_id,
        "source": relation.get("source") or cards_by_id.get(source_id, {}).get("title"),
        "target_card_id": target_id,
        "target": relation.get("target") or cards_by_id.get(target_id, {}).get("title"),
        "reason": clean_text(relation.get("reason"), 120),
        "evidence_page": relation.get("evidence_page"),
    }


def load_master(out_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    master_dir = out_dir / "master"
    cards = list(iter_jsonl(master_dir / "cards.jsonl"))
    relations = list(iter_jsonl(master_dir / "relations.jsonl"))
    summary = read_json(master_dir / "summary.json", {})
    if not cards:
        raise RuntimeError("No course graph cards found. Build data/course_graph/master/cards.jsonl first.")
    return cards, relations, summary


def load_chapter_metas(out_dir: Path, textbook_id: str) -> dict[int, dict[str, Any]]:
    metas: dict[int, dict[str, Any]] = {}
    run_root = out_dir / "chapter_runs" / textbook_id
    for path in sorted(run_root.glob("chapter_*/chapter_meta.json")):
        meta = read_json(path, {})
        number = meta.get("number")
        if isinstance(number, int):
            metas[number] = meta
    return metas


def chapter_prompt(
    textbook: dict[str, Any],
    chapter_meta: dict[str, Any],
    cards: list[dict[str, Any]],
    relations: list[dict[str, Any]],
) -> list[dict[str, str]]:
    schema = {
        "chapter_number": chapter_meta.get("number"),
        "disease_cards": [
            {
                "canonical_name": "病症标准名称，必须是病症/疾病/临床病理状态，不是术式、材料、器械、普通概念",
                "disease_kind": "disease|condition|injury|complication|lesion|syndrome",
                "aliases": ["同义名或教材别名"],
                "source_card_ids": ["来自输入的原始卡片 id"],
                "definition": "一句话定义，≤80字",
                "etiology": ["最多5条，每条≤45字"],
                "mechanism": ["最多5条，每条≤45字"],
                "clinical_features": ["最多6条，每条≤45字"],
                "diagnosis": ["最多5条，每条≤45字"],
                "differential_diagnosis": ["最多4条，每条≤30字"],
                "treatment": ["最多6条，每条≤45字"],
                "prognosis": ["最多3条，每条≤45字"],
                "complications": ["最多4条，每条≤35字"],
                "display_priority": 1,
                "evidence": [
                    {
                        "card_id": "证据来自哪个输入卡片",
                        "page": 1,
                        "field": "definition|etiology|mechanism|clinical_features|diagnosis|treatment|prognosis|complications",
                    }
                ],
                "confidence": "high|medium|low",
                "review_notes": [],
            }
        ],
        "disease_edges": [
            {
                "source_name": "必须等于 disease_cards[].canonical_name",
                "target_name": "必须等于 disease_cards[].canonical_name",
                "relation_type": "progresses_to|causes_or_predisposes|shares_mechanism_with|differentiates_from|complicates_to|treated_similarly_to|treated_differently_from|subtype_of|symptom_overlap",
                "dimension": "etiology|mechanism|symptoms|diagnosis|treatment|prognosis|complication|classification",
                "label": "适合显示在边上的短标签",
                "reason": "一句话说明，≤80字",
                "evidence_card_ids": ["输入卡片 id"],
                "evidence_pages": [1],
                "confidence": "high|medium|low",
            }
        ],
        "excluded_cards": [
            {
                "card_id": "被排除或并入的输入卡片 id",
                "category": "not_disease|merged_into_disease|too_granular|ocr_or_extraction_error|insufficient_evidence",
                "merged_into": "如已并入某个病症卡，写 canonical_name，否则空",
                "reason": "排除或合并原因",
            }
        ],
        "review_notes": [],
    }
    system = (
        "你是口腔医学教材知识图谱策展专家。任务是把普通知识卡片重构为“病症主网”。"
        "只能依据输入卡片与证据，不得补充外部事实。禁止关键词硬匹配；必须根据语义、证据和课程结构判断。"
        "主图只允许病症、疾病、损伤、病理状态、并发症或临床诊断实体作为节点。"
        "病因、机制、症状、检查、治疗、材料、器械、术式不能作为主图节点，应并入相关病症卡字段。"
        "如果治疗章节围绕某类病症，请创建或补充该病症卡；不要把术式本身当作病症。"
        "不确定或识别疑似错误的内容进入 excluded_cards 或 review_notes。输出必须是合法 JSON 对象。"
    )
    user = {
        "task": "对本章输入卡片做病症级重构，生成 disease_cards 和 disease_edges。",
        "textbook": textbook,
        "chapter": chapter_meta,
        "allowed_relation_types": sorted(ALLOWED_RELATIONS),
        "allowed_dimensions": sorted(ALLOWED_DIMENSIONS),
        "output_schema": schema,
        "input_cards": [compact_card(card) for card in cards],
        "input_relations": relations,
        "hard_constraints": [
            "不得输出非病症节点。",
            "每个 disease_card 必须至少有一个 source_card_ids。",
            "治疗、诊断、机制等内容必须作为病症字段，不得单独建节点。",
            "disease_edges 只连接本章输出的 disease_cards。",
            "输出必须紧凑：每章最多8个 disease_cards；每个 disease_card 的 evidence 最多3条；不要输出长引文或教材原文。",
            "excluded_cards 只列重要排除项；普通非病症卡若已明显并入 source_card_ids，可不逐条列出。",
            "只输出 JSON，不要解释。",
        ],
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]


def normalize_chapter_result(
    result: dict[str, Any],
    chapter_meta: dict[str, Any],
    cards_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    chapter_number = int(chapter_meta.get("number") or result.get("chapter_number") or 0)
    diseases: list[dict[str, Any]] = []
    name_to_temp: dict[str, str] = {}
    review_notes: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []

    for index, raw in enumerate(as_list(result.get("disease_cards")), 1):
        if not isinstance(raw, dict):
            continue
        name = clean_name(raw.get("canonical_name"))
        source_ids = [item for item in as_strings(raw.get("source_card_ids"), limit=60) if item in cards_by_id]
        if not name or not source_ids:
            review_notes.append(
                {
                    "type": "invalid_chapter_disease_card",
                    "chapter_number": chapter_number,
                    "raw_name": raw.get("canonical_name"),
                    "reason": "Missing canonical_name or valid source_card_ids.",
                    "review_status": "pending",
                }
            )
            continue
        temp_id = f"chapter:{chapter_number:02d}:disease:{index:03d}"
        name_to_temp[name] = temp_id
        diseases.append(
            {
                "temp_id": temp_id,
                "chapter_number": chapter_number,
                "chapter_title": chapter_meta.get("title"),
                "canonical_name": name,
                "disease_kind": clean_text(raw.get("disease_kind"), 40) or "disease",
                "aliases": as_strings(raw.get("aliases"), limit=12, item_limit=80),
                "source_card_ids": source_ids,
                "definition": clean_text(raw.get("definition"), 180),
                "etiology": as_strings(raw.get("etiology"), limit=5, item_limit=90),
                "mechanism": as_strings(raw.get("mechanism"), limit=5, item_limit=90),
                "clinical_features": as_strings(raw.get("clinical_features"), limit=6, item_limit=90),
                "diagnosis": as_strings(raw.get("diagnosis"), limit=5, item_limit=90),
                "differential_diagnosis": as_strings(raw.get("differential_diagnosis"), limit=4, item_limit=70),
                "treatment": as_strings(raw.get("treatment"), limit=6, item_limit=90),
                "prognosis": as_strings(raw.get("prognosis"), limit=3, item_limit=90),
                "complications": as_strings(raw.get("complications"), limit=4, item_limit=70),
                "display_priority": normalize_priority(raw.get("display_priority")),
                "evidence": normalize_llm_evidence(raw.get("evidence"), cards_by_id, source_ids),
                "confidence": normalize_confidence(raw.get("confidence")),
                "review_notes": as_list(raw.get("review_notes"))[:4],
            }
        )

    for raw in as_list(result.get("excluded_cards")):
        if not isinstance(raw, dict):
            continue
        card_id = str(raw.get("card_id") or "").strip()
        if not card_id:
            continue
        excluded.append(
            {
                "type": "chapter_card_exclusion",
                "chapter_number": chapter_number,
                "card_id": card_id,
                "card_title": cards_by_id.get(card_id, {}).get("title"),
                "category": clean_text(raw.get("category"), 80),
                "merged_into": clean_name(raw.get("merged_into")),
                "reason": clean_text(raw.get("reason"), 360),
                "review_status": "accepted" if raw.get("category") in {"not_disease", "merged_into_disease"} else "pending",
            }
        )

    edges: list[dict[str, Any]] = []
    for raw in as_list(result.get("disease_edges")):
        if not isinstance(raw, dict):
            continue
        source_name = clean_name(raw.get("source_name"))
        target_name = clean_name(raw.get("target_name"))
        if source_name not in name_to_temp or target_name not in name_to_temp or source_name == target_name:
            review_notes.append(
                {
                    "type": "unresolved_chapter_disease_edge",
                    "chapter_number": chapter_number,
                    "source_name": source_name,
                    "target_name": target_name,
                    "reason": clean_text(raw.get("reason"), 260),
                    "review_status": "pending",
                }
            )
            continue
        relation_type = normalize_relation_type(raw.get("relation_type"))
        dimension = normalize_dimension(raw.get("dimension"), relation_type)
        evidence_card_ids = [item for item in as_strings(raw.get("evidence_card_ids"), limit=24) if item in cards_by_id]
        edges.append(
            {
                "temp_source": name_to_temp[source_name],
                "temp_target": name_to_temp[target_name],
                "relation_type": relation_type,
                "dimension": dimension,
                "label": clean_text(raw.get("label"), 36) or relation_type,
                "reason": clean_text(raw.get("reason"), 220),
                "evidence_card_ids": evidence_card_ids,
                "evidence_pages": normalize_pages(raw.get("evidence_pages")),
                "confidence": normalize_confidence(raw.get("confidence")),
                "relation_scope": "chapter",
                "chapter_number": chapter_number,
            }
        )

    for note in as_list(result.get("review_notes")):
        review_notes.append(
            {
                "type": "chapter_disease_review_note",
                "chapter_number": chapter_number,
                "note": clean_text(note, 520) if not isinstance(note, dict) else clean_text(note.get("note"), 520),
                "review_status": "pending",
            }
        )

    return {
        "chapter_number": chapter_number,
        "chapter_title": chapter_meta.get("title"),
        "disease_cards": diseases,
        "disease_edges": edges,
        "excluded_cards": excluded,
        "review_notes": review_notes,
    }


def normalize_priority(value: Any) -> int:
    try:
        return max(1, min(100, int(value)))
    except (TypeError, ValueError):
        return 50


def normalize_confidence(value: Any) -> str:
    text = str(value or "medium").strip().lower()
    return text if text in {"high", "medium", "low"} else "medium"


def normalize_relation_type(value: Any) -> str:
    text = str(value or "shares_mechanism_with").strip()
    return text if text in ALLOWED_RELATIONS else "shares_mechanism_with"


def normalize_dimension(value: Any, relation_type: str) -> str:
    text = str(value or "").strip()
    if text in ALLOWED_DIMENSIONS:
        return text
    defaults = {
        "progresses_to": "prognosis",
        "causes_or_predisposes": "etiology",
        "shares_mechanism_with": "mechanism",
        "differentiates_from": "diagnosis",
        "complicates_to": "complication",
        "treated_similarly_to": "treatment",
        "treated_differently_from": "treatment",
        "subtype_of": "classification",
        "symptom_overlap": "symptoms",
    }
    return defaults.get(relation_type, "mechanism")


def normalize_pages(value: Any) -> list[int]:
    pages: list[int] = []
    for item in as_list(value):
        try:
            page = int(item)
        except (TypeError, ValueError):
            continue
        if page not in pages:
            pages.append(page)
    return pages[:12]


def normalize_llm_evidence(
    value: Any,
    cards_by_id: dict[str, dict[str, Any]],
    source_card_ids: list[str],
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    seen: set[tuple[str, Any, str]] = set()
    for raw in as_list(value):
        if not isinstance(raw, dict):
            continue
        card_id = str(raw.get("card_id") or raw.get("source_card_id") or "").strip()
        if card_id not in cards_by_id:
            continue
        page = raw.get("page")
        quote = clean_text(raw.get("quote_or_paraphrase") or raw.get("evidence"), 360)
        key = (card_id, page, quote)
        if quote and key not in seen:
            evidence.append(
                {
                    "card_id": card_id,
                    "card_title": cards_by_id[card_id].get("title"),
                    "page": page,
                    "field": clean_text(raw.get("field"), 80),
                    "quote_or_paraphrase": quote,
                }
            )
            seen.add(key)
    for card_id in source_card_ids:
        card = cards_by_id.get(card_id) or {}
        for raw in as_list(card.get("evidence"))[:3]:
            if not isinstance(raw, dict):
                continue
            page = raw.get("page")
            quote = clean_text(raw.get("quote_or_paraphrase"), 360)
            key = (card_id, page, quote)
            if quote and key not in seen:
                evidence.append(
                    {
                        "card_id": card_id,
                        "card_title": card.get("title"),
                        "page": page,
                        "field": "source_card",
                        "quote_or_paraphrase": quote,
                    }
                )
                seen.add(key)
            if len(evidence) >= 16:
                return evidence
    return evidence


def global_prompt(textbook: dict[str, Any], chapter_results: list[dict[str, Any]]) -> list[dict[str, str]]:
    disease_index = []
    for chapter in chapter_results:
        for disease in chapter.get("disease_cards", []):
            disease_index.append(
                {
                    "temp_id": disease.get("temp_id"),
                    "chapter_number": disease.get("chapter_number"),
                    "chapter_title": disease.get("chapter_title"),
                    "canonical_name": disease.get("canonical_name"),
                    "aliases": disease.get("aliases"),
                    "definition": compact(disease.get("definition"), 220),
                    "etiology": disease.get("etiology", [])[:5],
                    "mechanism": disease.get("mechanism", [])[:5],
                    "clinical_features": disease.get("clinical_features", [])[:6],
                    "diagnosis": disease.get("diagnosis", [])[:5],
                    "treatment": disease.get("treatment", [])[:6],
                    "complications": disease.get("complications", [])[:4],
                    "source_card_ids": disease.get("source_card_ids", [])[:16],
                    "display_priority": disease.get("display_priority"),
                    "confidence": disease.get("confidence"),
                }
            )
    schema = {
        "canonical_diseases": [
            {
                "canonical_name": "全书统一病症名",
                "aliases": ["合并的同义名"],
                "source_temp_ids": ["来自 disease_index 的 temp_id"],
                "disease_group": "龋病|牙髓病|根尖周病|牙发育异常和着色|牙外伤|牙慢性损伤|牙本质敏感|治疗并发症|其他",
                "display_priority": 1,
                "merge_reason": "为什么这些章节病症应合并或保持独立",
                "review_status": "accepted|needs_review",
            }
        ],
        "cross_disease_edges": [
            {
                "source_name": "必须等于 canonical_diseases[].canonical_name",
                "target_name": "必须等于 canonical_diseases[].canonical_name",
                "relation_type": "progresses_to|causes_or_predisposes|shares_mechanism_with|differentiates_from|complicates_to|treated_similarly_to|treated_differently_from|subtype_of|symptom_overlap",
                "dimension": "etiology|mechanism|symptoms|diagnosis|treatment|prognosis|complication|classification",
                "label": "短边标签",
                "reason": "基于输入 disease_index 的关系理由",
                "evidence_temp_ids": ["支撑该边的 temp_id"],
                "confidence": "high|medium|low",
            }
        ],
        "review_notes": [],
    }
    system = (
        "你是口腔医学课程知识图谱总编。任务是把逐章病症卡合并为全书病症主网。"
        "只依据输入 disease_index，不补充外部事实。禁止机械名称匹配；必须按语义、章节上下文和证据字段决定合并与连边。"
        "主网只连接病症节点，边应体现症状表现、病理机制、治疗方式、病因、进展、并发或鉴别关系。"
        "如果两个名称相似但临床含义不同，必须保留为不同节点。输出必须是合法 JSON 对象。"
    )
    user = {
        "task": "合并全书病症卡，生成全书 canonical_diseases 与 cross_disease_edges。",
        "textbook": textbook,
        "allowed_relation_types": sorted(ALLOWED_RELATIONS),
        "allowed_dimensions": sorted(ALLOWED_DIMENSIONS),
        "output_schema": schema,
        "disease_index": disease_index,
        "hard_constraints": [
            "canonical_diseases[].source_temp_ids 必须来自 disease_index。",
            "cross_disease_edges 只能连接 canonical_diseases。",
            "不要输出术式、器械、材料、检查作为节点。",
            "只输出 JSON，不要解释。",
        ],
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]


def disease_index_for_prompt(disease: dict[str, Any]) -> dict[str, Any]:
    return {
        "temp_id": disease.get("temp_id"),
        "chapter_number": disease.get("chapter_number"),
        "chapter_title": disease.get("chapter_title"),
        "canonical_name": disease.get("canonical_name"),
        "aliases": disease.get("aliases", [])[:4],
        "definition": compact(disease.get("definition"), 120),
        "etiology": disease.get("etiology", [])[:3],
        "mechanism": disease.get("mechanism", [])[:3],
        "clinical_features": disease.get("clinical_features", [])[:4],
        "diagnosis": disease.get("diagnosis", [])[:3],
        "treatment": disease.get("treatment", [])[:3],
        "complications": disease.get("complications", [])[:2],
        "source_card_ids": disease.get("source_card_ids", [])[:8],
        "display_priority": disease.get("display_priority"),
        "confidence": disease.get("confidence"),
    }


def canonical_index_for_prompt(canonical_state: list[dict[str, Any]], temp_diseases: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in canonical_state:
        source_ids = as_strings(item.get("source_temp_ids"), limit=80)
        sample_sources = [temp_diseases[temp_id] for temp_id in source_ids[:3] if temp_id in temp_diseases]
        rows.append(
            {
                "canonical_name": item.get("canonical_name"),
                "aliases": item.get("aliases", [])[:6],
                "disease_group": item.get("disease_group"),
                "source_temp_ids": source_ids[:20],
                "source_count": len(source_ids),
                "chapters": sorted({source.get("chapter_number") for source in sample_sources if source.get("chapter_number") is not None}),
                "definitions": [compact(source.get("definition"), 90) for source in sample_sources if source.get("definition")],
            }
        )
    return rows


def incremental_global_prompt(
    textbook: dict[str, Any],
    canonical_state: list[dict[str, Any]],
    temp_diseases: dict[str, dict[str, Any]],
    batch_chapters: list[dict[str, Any]],
    batch_index: int,
    total_batches: int,
) -> list[dict[str, str]]:
    new_diseases = [
        disease_index_for_prompt(disease)
        for chapter in batch_chapters
        for disease in chapter.get("disease_cards", [])
    ]
    schema = {
        "canonical_updates": [
            {
                "canonical_name": "若并入既有病症，必须完全等于 existing_canonical_diseases 中的 canonical_name；否则写新病症名",
                "aliases": ["最多5个同义名"],
                "source_temp_ids": ["只能来自 new_disease_index，且每个 new temp_id 全批恰好出现一次"],
                "disease_group": "龋病|牙髓病|根尖周病|牙发育异常和着色|牙外伤|牙慢性损伤|牙本质敏感|治疗并发症|其他",
                "display_priority": 1,
                "merge_reason": "≤80字，说明为何并入或新建",
                "review_status": "accepted|needs_review",
            }
        ],
        "cross_disease_edges": [
            {
                "source_name": "必须等于 existing 或 canonical_updates 中的 canonical_name",
                "target_name": "必须等于 existing 或 canonical_updates 中的 canonical_name",
                "relation_type": "progresses_to|causes_or_predisposes|shares_mechanism_with|differentiates_from|complicates_to|treated_similarly_to|treated_differently_from|subtype_of|symptom_overlap",
                "dimension": "etiology|mechanism|symptoms|diagnosis|treatment|prognosis|complication|classification",
                "label": "≤12字",
                "reason": "≤80字",
                "evidence_temp_ids": ["来自 existing 或 new 的 temp_id"],
                "confidence": "high|medium|low",
            }
        ],
        "review_notes": [],
    }
    system = (
        "你是口腔医学课程知识图谱总编。你正在增量合并逐章病症卡。"
        "只能依据输入，不补充外部事实。禁止关键词硬匹配；必须按语义、章节上下文和证据字段决定是否合并。"
        "主图只允许病症/疾病/损伤/病理状态/并发症作为节点；术式、材料、器械、检查不能作为节点。"
        "输出必须紧凑且是合法 JSON 对象。"
    )
    user = {
        "task": f"全书病症增量合并 batch {batch_index}/{total_batches}。",
        "textbook": textbook,
        "existing_canonical_diseases": canonical_index_for_prompt(canonical_state, temp_diseases),
        "new_disease_index": new_diseases,
        "allowed_relation_types": sorted(ALLOWED_RELATIONS),
        "allowed_dimensions": sorted(ALLOWED_DIMENSIONS),
        "output_schema": schema,
        "hard_constraints": [
            "每个 new_disease_index[].temp_id 必须在 canonical_updates[].source_temp_ids 中恰好出现一次。",
            "即使你认为某个 new temp_id 不适合主图，也必须归入某个 canonical_updates，并把 review_status 设为 needs_review。",
            "不要重复输出已存在 canonical 的旧 source_temp_ids；只输出本批 new temp_id 的归属。",
            "如果并入既有病症，canonical_name 必须完全复制 existing_canonical_diseases 中的名称。",
            "cross_disease_edges 最多输出12条，优先病因、机制、治疗、进展、并发、鉴别关系。",
            "只输出 JSON，不要解释。",
        ],
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]


def retry_incremental_global_prompt(
    textbook: dict[str, Any],
    canonical_state: list[dict[str, Any]],
    temp_diseases: dict[str, dict[str, Any]],
    batch_chapters: list[dict[str, Any]],
    previous_raw: dict[str, Any],
    missing_temp_ids: list[str],
    batch_index: int,
    total_batches: int,
) -> list[dict[str, str]]:
    messages = incremental_global_prompt(
        textbook,
        canonical_state,
        temp_diseases,
        batch_chapters,
        batch_index,
        total_batches,
    )
    retry_payload = {
        "previous_output": previous_raw,
        "missing_temp_ids": missing_temp_ids,
        "missing_disease_index": [disease_index_for_prompt(temp_diseases[temp_id]) for temp_id in missing_temp_ids if temp_id in temp_diseases],
        "instruction": "重新输出完整 JSON。保留你认为正确的既有 canonical_updates，并补齐 missing_temp_ids；每个 new temp_id 仍必须恰好出现一次。",
    }
    messages.append({"role": "user", "content": json.dumps(retry_payload, ensure_ascii=False)})
    return messages


def chapter_batches(chapter_results: list[dict[str, Any]], batch_size: int = 4) -> list[list[dict[str, Any]]]:
    ordered = sorted(chapter_results, key=lambda item: int(item.get("chapter_number") or 0))
    return [ordered[index : index + batch_size] for index in range(0, len(ordered), batch_size)]


def run_incremental_global_consolidation(
    textbook: dict[str, Any],
    config: dict[str, Any],
    out_dir: Path,
    chapter_results: list[dict[str, Any]],
    force: bool,
    run_label: str,
) -> dict[str, Any]:
    disease_dir = out_dir / "disease_core"
    state_path = disease_dir / f"global_consolidation_{run_label}_incremental_state.json"
    final_path = disease_dir / ("global_consolidation.json" if run_label == "full" else f"global_consolidation_{run_label}.json")
    if final_path.exists() and not force:
        existing = read_json(final_path, {})
        if existing:
            return existing

    temp_diseases: dict[str, dict[str, Any]] = {}
    for chapter in chapter_results:
        for disease in chapter.get("disease_cards", []):
            temp_id = str(disease.get("temp_id") or "")
            if temp_id:
                temp_diseases[temp_id] = disease

    state = read_json(state_path, {}) if state_path.exists() and not force else {}
    canonical_state: list[dict[str, Any]] = state.get("canonical_diseases", []) if isinstance(state.get("canonical_diseases"), list) else []
    cross_edges: list[dict[str, Any]] = state.get("cross_disease_edges", []) if isinstance(state.get("cross_disease_edges"), list) else []
    review_notes: list[Any] = state.get("review_notes", []) if isinstance(state.get("review_notes"), list) else []
    completed_batches = set(as_strings(state.get("completed_batches"), limit=200))

    batches = chapter_batches(chapter_results, batch_size=4)
    for index, batch in enumerate(batches, 1):
        batch_id = f"batch_{index:02d}"
        if batch_id in completed_batches and not force:
            continue
        print(f"[disease_core] global merge {batch_id}/{len(batches)}", file=sys.stderr)
        raw = llm_json(
            incremental_global_prompt(textbook, canonical_state, temp_diseases, batch, index, len(batches)),
            config,
            debug_path=disease_dir / "llm_debug" / f"global_{batch_id}",
            max_tokens=max(4200, llm_max_tokens(config)),
        )
        candidate_state, batch_notes, missing = apply_incremental_global_result(canonical_state, raw, batch, temp_diseases)
        retry_count = 0
        while missing and retry_count < 2:
            retry_count += 1
            print(f"[disease_core] retry {batch_id}: missing {len(missing)} temp ids", file=sys.stderr)
            raw = llm_json(
                retry_incremental_global_prompt(
                    textbook,
                    canonical_state,
                    temp_diseases,
                    batch,
                    raw,
                    missing,
                    index,
                    len(batches),
                ),
                config,
                debug_path=disease_dir / "llm_debug" / f"global_{batch_id}_retry_{retry_count}",
                max_tokens=max(4200, llm_max_tokens(config)),
            )
            candidate_state, batch_notes, missing = apply_incremental_global_result(canonical_state, raw, batch, temp_diseases)
        if missing:
            raise RuntimeError(f"Incremental global merge missed temp ids after retries: {missing}")
        canonical_state = candidate_state
        cross_edges.extend(as_list(raw.get("cross_disease_edges")))
        review_notes.extend(batch_notes)
        review_notes.extend(as_list(raw.get("review_notes")))
        completed_batches.add(batch_id)
        write_json(
            state_path,
            {
                "canonical_diseases": canonical_state,
                "cross_disease_edges": cross_edges,
                "review_notes": review_notes,
                "completed_batches": sorted(completed_batches),
                "updated_at": now_iso(),
            },
        )

    final = {
        "canonical_diseases": canonical_state,
        "cross_disease_edges": cross_edges,
        "review_notes": review_notes,
        "batches": len(batches),
        "consolidation_mode": "incremental_llm",
    }
    validate_global_coverage(final, temp_diseases)
    write_json(final_path, final)
    return final


def apply_incremental_global_result(
    canonical_state: list[dict[str, Any]],
    raw: dict[str, Any],
    batch: list[dict[str, Any]],
    temp_diseases: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    next_state = [dict(item) for item in canonical_state]
    batch_temp_ids = {
        str(disease.get("temp_id"))
        for chapter in batch
        for disease in chapter.get("disease_cards", [])
        if disease.get("temp_id")
    }
    assigned: set[str] = set()
    assigned_order: list[str] = []
    notes: list[dict[str, Any]] = []
    by_name = {clean_name(item.get("canonical_name")): item for item in next_state if item.get("canonical_name")}

    for update in as_list(raw.get("canonical_updates")):
        if not isinstance(update, dict):
            continue
        name = clean_name(update.get("canonical_name"))
        source_temp_ids = [item for item in as_strings(update.get("source_temp_ids"), limit=80) if item in batch_temp_ids]
        if not name or not source_temp_ids:
            notes.append(
                {
                    "type": "invalid_incremental_merge_update",
                    "canonical_name": update.get("canonical_name"),
                    "source_temp_ids": update.get("source_temp_ids"),
                    "review_status": "pending",
                }
            )
            continue
        assigned.update(source_temp_ids)
        assigned_order.extend(source_temp_ids)
        aliases = as_strings(update.get("aliases"), limit=12, item_limit=80)
        target = by_name.get(name)
        if target:
            target["source_temp_ids"] = unique([*as_strings(target.get("source_temp_ids"), limit=300), *source_temp_ids])
            target["aliases"] = unique([*as_strings(target.get("aliases"), limit=40), *aliases])
            if update.get("merge_reason"):
                target["merge_reason"] = compact(
                    f"{target.get('merge_reason', '')}；{clean_text(update.get('merge_reason'), 120)}".strip("；"),
                    520,
                )
            if target.get("review_status") != "needs_review":
                target["review_status"] = str(update.get("review_status") or target.get("review_status") or "accepted")
        else:
            target = {
                "canonical_name": name,
                "aliases": aliases,
                "source_temp_ids": source_temp_ids,
                "disease_group": clean_text(update.get("disease_group"), 80) or infer_group_from_sources(source_temp_ids, temp_diseases),
                "display_priority": normalize_priority(update.get("display_priority")),
                "merge_reason": clean_text(update.get("merge_reason"), 520),
                "review_status": str(update.get("review_status") or "accepted"),
            }
            next_state.append(target)
            by_name[name] = target

    missing = sorted(batch_temp_ids - assigned)
    duplicate_count = len(assigned_order) - len(set(assigned_order))
    if duplicate_count:
        raise RuntimeError("Incremental global merge assigned duplicate temp ids.")
    return next_state, notes, missing


def infer_group_from_sources(source_temp_ids: list[str], temp_diseases: dict[str, dict[str, Any]]) -> str:
    for temp_id in source_temp_ids:
        disease = temp_diseases.get(temp_id) or {}
        title = str(disease.get("chapter_title") or "")
        name = str(disease.get("canonical_name") or "")
        text = title + name
        if "龋" in text:
            return "龋病"
        if "牙髓" in text:
            return "牙髓病"
        if "根尖" in text:
            return "根尖周病"
        if "外伤" in text:
            return "牙外伤"
    return "其他"


def validate_global_coverage(global_result: dict[str, Any], temp_diseases: dict[str, dict[str, Any]]) -> None:
    expected = set(temp_diseases)
    assigned: list[str] = []
    for item in as_list(global_result.get("canonical_diseases")):
        if isinstance(item, dict):
            assigned.extend(as_strings(item.get("source_temp_ids"), limit=300))
    assigned_set = set(assigned)
    missing = sorted(expected - assigned_set)
    extra = sorted(assigned_set - expected)
    duplicates = sorted({item for item in assigned if assigned.count(item) > 1})
    if missing or extra or duplicates:
        raise RuntimeError(
            "Global consolidation coverage failed: "
            f"missing={missing[:10]} extra={extra[:10]} duplicates={duplicates[:10]}"
        )


def assemble_disease_core(
    textbook: dict[str, Any],
    chapter_metas: dict[int, dict[str, Any]],
    cards_by_id: dict[str, dict[str, Any]],
    chapter_results: list[dict[str, Any]],
    global_result: dict[str, Any],
) -> dict[str, Any]:
    temp_diseases: dict[str, dict[str, Any]] = {}
    temp_to_chapter: dict[str, dict[str, Any]] = {}
    for chapter in chapter_results:
        for disease in chapter.get("disease_cards", []):
            temp_id = str(disease.get("temp_id") or "")
            if temp_id:
                temp_diseases[temp_id] = disease
                temp_to_chapter[temp_id] = chapter

    canonical_by_name: dict[str, dict[str, Any]] = {}
    temp_to_canonical: dict[str, str] = {}
    review_queue: list[dict[str, Any]] = []
    disease_cards: list[dict[str, Any]] = []

    for raw in as_list(global_result.get("canonical_diseases")):
        if not isinstance(raw, dict):
            continue
        name = clean_name(raw.get("canonical_name"))
        source_temp_ids = [item for item in as_strings(raw.get("source_temp_ids"), limit=200) if item in temp_diseases]
        if not name or not source_temp_ids:
            review_queue.append(
                {
                    "type": "invalid_global_disease_merge",
                    "raw_name": raw.get("canonical_name"),
                    "source_temp_ids": raw.get("source_temp_ids"),
                    "reason": "Missing canonical_name or valid source_temp_ids.",
                    "review_status": "pending",
                }
            )
            continue

        disease_id = stable_id("disease", textbook.get("id"), name)
        sources = [temp_diseases[temp_id] for temp_id in source_temp_ids]
        source_card_ids = unique(
            card_id for source in sources for card_id in as_strings(source.get("source_card_ids"), limit=200)
        )
        chapter_numbers = sorted({int(source.get("chapter_number")) for source in sources if source.get("chapter_number") is not None})
        supporting_cards = [
            {
                "id": card_id,
                "title": cards_by_id.get(card_id, {}).get("title"),
                "card_type": cards_by_id.get(card_id, {}).get("card_type"),
                "chapter_number": cards_by_id.get(card_id, {}).get("chapter_number"),
                "chapter_title": cards_by_id.get(card_id, {}).get("chapter_title"),
                "summary": cards_by_id.get(card_id, {}).get("summary"),
            }
            for card_id in source_card_ids
            if card_id in cards_by_id
        ]
        evidence = merge_evidence(sources, cards_by_id, source_card_ids)
        card = {
            "id": disease_id,
            "type": "disease",
            "textbook_id": textbook.get("id"),
            "textbook_title": textbook.get("title"),
            "name": name,
            "title": name,
            "label": name,
            "aliases": merge_strings([raw.get("aliases"), *(source.get("aliases") for source in sources)], limit=20, item_limit=80),
            "disease_group": clean_text(raw.get("disease_group"), 80) or "其他",
            "definition": first_non_empty([source.get("definition") for source in sources]),
            "etiology": merge_strings(source.get("etiology") for source in sources),
            "mechanism": merge_strings(source.get("mechanism") for source in sources),
            "clinical_features": merge_strings((source.get("clinical_features") for source in sources), limit=32),
            "diagnosis": merge_strings((source.get("diagnosis") for source in sources), limit=32),
            "differential_diagnosis": merge_strings(source.get("differential_diagnosis") for source in sources),
            "treatment": merge_strings((source.get("treatment") for source in sources), limit=36),
            "prognosis": merge_strings(source.get("prognosis") for source in sources),
            "complications": merge_strings(source.get("complications") for source in sources),
            "chapter_numbers": chapter_numbers,
            "chapter_sources": [
                {
                    "number": number,
                    "title": chapter_metas.get(number, {}).get("title"),
                    "start_page": chapter_metas.get(number, {}).get("start_page"),
                    "end_page": chapter_metas.get(number, {}).get("end_page"),
                }
                for number in chapter_numbers
            ],
            "source_temp_ids": source_temp_ids,
            "source_card_ids": source_card_ids,
            "supporting_cards": supporting_cards,
            "evidence": evidence,
            "display_priority": normalize_priority(raw.get("display_priority") or min(source.get("display_priority", 50) for source in sources)),
            "merge_reason": clean_text(raw.get("merge_reason"), 520),
            "review_status": str(raw.get("review_status") or "accepted"),
            "confidence": merged_confidence(sources),
            "created_at": now_iso(),
        }
        disease_cards.append(card)
        canonical_by_name[name] = card
        for temp_id in source_temp_ids:
            temp_to_canonical[temp_id] = disease_id

    for temp_id, disease in temp_diseases.items():
        if temp_id in temp_to_canonical:
            continue
        name = disease.get("canonical_name") or temp_id
        review_queue.append(
            {
                "type": "unmerged_chapter_disease",
                "temp_id": temp_id,
                "name": name,
                "chapter_number": disease.get("chapter_number"),
                "review_status": "pending",
            }
        )

    edges: list[dict[str, Any]] = []
    edge_seen: set[tuple[str, str, str, str]] = set()
    for chapter in chapter_results:
        for raw in chapter.get("disease_edges", []):
            source = temp_to_canonical.get(str(raw.get("temp_source")))
            target = temp_to_canonical.get(str(raw.get("temp_target")))
            if not source or not target or source == target:
                continue
            add_edge(edges, edge_seen, textbook, source, target, raw)

    for raw in as_list(global_result.get("cross_disease_edges")):
        if not isinstance(raw, dict):
            continue
        source_card = canonical_by_name.get(clean_name(raw.get("source_name")))
        target_card = canonical_by_name.get(clean_name(raw.get("target_name")))
        if not source_card or not target_card or source_card["id"] == target_card["id"]:
            review_queue.append(
                {
                    "type": "unresolved_global_disease_edge",
                    "source_name": raw.get("source_name"),
                    "target_name": raw.get("target_name"),
                    "reason": clean_text(raw.get("reason"), 420),
                    "review_status": "pending",
                }
            )
            continue
        evidence_temp_ids = [item for item in as_strings(raw.get("evidence_temp_ids"), limit=40) if item in temp_diseases]
        evidence_card_ids = unique(
            card_id
            for temp_id in evidence_temp_ids
            for card_id in as_strings(temp_diseases[temp_id].get("source_card_ids"), limit=200)
        )
        edge_payload = {
            "relation_type": normalize_relation_type(raw.get("relation_type")),
            "dimension": normalize_dimension(raw.get("dimension"), normalize_relation_type(raw.get("relation_type"))),
            "label": clean_text(raw.get("label"), 36),
            "reason": clean_text(raw.get("reason"), 700),
            "evidence_card_ids": evidence_card_ids,
            "evidence_pages": [],
            "confidence": normalize_confidence(raw.get("confidence")),
            "relation_scope": "global",
        }
        add_edge(edges, edge_seen, textbook, source_card["id"], target_card["id"], edge_payload)

    for chapter in chapter_results:
        for item in chapter.get("excluded_cards", []):
            review_queue.append(item)
        for item in chapter.get("review_notes", []):
            review_queue.append(item)
    for note in as_list(global_result.get("review_notes")):
        review_queue.append(
            {
                "type": "global_disease_review_note",
                "note": clean_text(note, 700) if not isinstance(note, dict) else clean_text(note.get("note"), 700),
                "review_status": "pending",
            }
        )

    chapter_index = build_chapter_index(chapter_metas, disease_cards)
    graph = {
        "nodes": [disease_to_node(card) for card in sorted(disease_cards, key=lambda item: (-item["display_priority"], item["name"]))],
        "edges": sorted(edges, key=lambda item: (item.get("source"), item.get("target"), item.get("type"))),
    }
    summary = {
        "ok": True,
        "textbook_id": textbook.get("id"),
        "textbook_title": textbook.get("title"),
        "disease_cards": len(disease_cards),
        "disease_edges": len(edges),
        "chapters_indexed": len(chapter_index),
        "chapters_with_diseases": len([item for item in chapter_index if item.get("diseases")]),
        "review_items": len(review_queue),
        "generated_at": now_iso(),
        "policy": {
            "main_graph_nodes": "disease_only",
            "non_disease_content": "stored_inside_disease_cards",
            "construction": "llm_curated_no_keyword_fallback",
        },
    }
    return {
        "disease_cards": disease_cards,
        "disease_edges": edges,
        "chapter_index": chapter_index,
        "review_queue": review_queue,
        "disease_graph": graph,
        "summary": summary,
    }


def unique(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def first_non_empty(values: Iterable[Any]) -> str:
    for value in values:
        text = clean_text(value, 900)
        if text:
            return text
    return ""


def merge_evidence(
    sources: list[dict[str, Any]],
    cards_by_id: dict[str, dict[str, Any]],
    source_card_ids: list[str],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, Any, str]] = set()
    for source in sources:
        for item in as_list(source.get("evidence")):
            if not isinstance(item, dict):
                continue
            card_id = str(item.get("card_id") or "").strip()
            page = item.get("page")
            quote = clean_text(item.get("quote_or_paraphrase"), 360)
            key = (card_id, page, quote)
            if card_id and quote and key not in seen:
                result.append(item)
                seen.add(key)
            if len(result) >= 20:
                return result
    for card_id in source_card_ids:
        card = cards_by_id.get(card_id) or {}
        for raw in as_list(card.get("evidence"))[:2]:
            if not isinstance(raw, dict):
                continue
            page = raw.get("page")
            quote = clean_text(raw.get("quote_or_paraphrase"), 360)
            key = (card_id, page, quote)
            if quote and key not in seen:
                result.append(
                    {
                        "card_id": card_id,
                        "card_title": card.get("title"),
                        "page": page,
                        "field": "source_card",
                        "quote_or_paraphrase": quote,
                    }
                )
                seen.add(key)
            if len(result) >= 20:
                return result
    return result


def merged_confidence(sources: list[dict[str, Any]]) -> str:
    values = [source.get("confidence") for source in sources]
    if values and all(value == "high" for value in values):
        return "high"
    if any(value == "low" for value in values):
        return "low"
    return "medium"


def add_edge(
    edges: list[dict[str, Any]],
    seen: set[tuple[str, str, str, str]],
    textbook: dict[str, Any],
    source: str,
    target: str,
    raw: dict[str, Any],
) -> None:
    relation_type = normalize_relation_type(raw.get("relation_type"))
    dimension = normalize_dimension(raw.get("dimension"), relation_type)
    key = (source, target, relation_type, dimension)
    if key in seen:
        return
    seen.add(key)
    edge_id = stable_id("disease_rel", textbook.get("id"), source, relation_type, dimension, target)
    edges.append(
        {
            "id": edge_id,
            "textbook_id": textbook.get("id"),
            "source": source,
            "target": target,
            "type": relation_type,
            "relation_type": relation_type,
            "dimension": dimension,
            "label": clean_text(raw.get("label"), 36) or relation_type,
            "reason": clean_text(raw.get("reason"), 800),
            "evidence_card_ids": as_strings(raw.get("evidence_card_ids"), limit=60),
            "evidence_pages": normalize_pages(raw.get("evidence_pages")),
            "confidence": normalize_confidence(raw.get("confidence")),
            "relation_scope": raw.get("relation_scope") or "global",
            "chapter_number": raw.get("chapter_number"),
            "created_at": now_iso(),
        }
    )


def build_chapter_index(
    chapter_metas: dict[int, dict[str, Any]],
    disease_cards: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_chapter: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for card in disease_cards:
        for number in card.get("chapter_numbers") or []:
            by_chapter[int(number)].append(card)
    result: list[dict[str, Any]] = []
    for number in sorted(chapter_metas):
        meta = chapter_metas[number]
        diseases = sorted(by_chapter.get(number, []), key=lambda item: (-item.get("display_priority", 0), item.get("name", "")))
        result.append(
            {
                "number": number,
                "title": meta.get("title") or f"第{number}章",
                "start_page": meta.get("start_page"),
                "end_page": meta.get("end_page"),
                "disease_ids": [item["id"] for item in diseases],
                "diseases": [
                    {
                        "id": item["id"],
                        "name": item["name"],
                        "disease_group": item.get("disease_group"),
                        "display_priority": item.get("display_priority"),
                        "supporting_card_count": len(item.get("supporting_cards") or []),
                        "review_status": item.get("review_status"),
                    }
                    for item in diseases
                ],
            }
        )
    return result


def disease_to_node(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": card["id"],
        "type": "disease",
        "label": card.get("name") or card["id"],
        "group": card.get("disease_group"),
        "properties": card,
    }


def run_chapter_curation(
    textbook: dict[str, Any],
    config: dict[str, Any],
    out_dir: Path,
    chapter_metas: dict[int, dict[str, Any]],
    cards_by_chapter: dict[int, list[dict[str, Any]]],
    relations_by_chapter: dict[int, list[dict[str, Any]]],
    cards_by_id: dict[str, dict[str, Any]],
    force: bool,
    selected_chapter_numbers: set[int] | None = None,
) -> list[dict[str, Any]]:
    chapter_dir = out_dir / "disease_core" / "chapter_curations"
    results: list[dict[str, Any]] = []
    for number in sorted(cards_by_chapter):
        if selected_chapter_numbers is not None and number not in selected_chapter_numbers:
            continue
        cards = cards_by_chapter[number]
        meta = chapter_metas.get(number) or {
            "number": number,
            "title": cards[0].get("chapter_title") if cards else f"第{number}章",
        }
        result_path = chapter_dir / f"chapter_{number:02d}_disease_curation.json"
        if result_path.exists() and not force:
            normalized = read_json(result_path, {})
            if normalized:
                results.append(normalized)
                continue
        print(f"[disease_core] curate chapter {number:02d} {meta.get('title')} ({len(cards)} source cards)", file=sys.stderr)
        compact_relations = [
            compact_relation(relation, cards_by_id)
            for relation in relations_by_chapter.get(number, [])[:100]
        ]
        raw = llm_json(
            chapter_prompt(textbook, meta, cards, compact_relations),
            config,
            debug_path=chapter_dir / "llm_debug" / f"chapter_{number:02d}",
        )
        normalized = normalize_chapter_result(raw, meta, cards_by_id)
        write_json(result_path, normalized)
        results.append(normalized)
    return results


def run_global_consolidation(
    textbook: dict[str, Any],
    config: dict[str, Any],
    out_dir: Path,
    chapter_results: list[dict[str, Any]],
    force: bool,
    run_label: str = "full",
) -> dict[str, Any]:
    filename = "global_consolidation.json" if run_label == "full" else f"global_consolidation_{run_label}.json"
    path = out_dir / "disease_core" / filename
    if path.exists() and not force:
        existing = read_json(path, {})
        if existing:
            return existing
    if run_label == "full" or sum(len(chapter.get("disease_cards", [])) for chapter in chapter_results) > 60:
        return run_incremental_global_consolidation(textbook, config, out_dir, chapter_results, force, run_label)
    print("[disease_core] consolidate disease cards across chapters", file=sys.stderr)
    raw = llm_json(
        global_prompt(textbook, chapter_results),
        config,
        debug_path=out_dir / "disease_core" / "llm_debug" / "global_consolidation",
        max_tokens=max(9000, llm_max_tokens(config)),
    )
    write_json(path, raw)
    return raw


def build_disease_core(
    textbook_id: str,
    config_path: Path,
    force: bool,
    chapter_start: int | None = None,
    chapter_end: int | None = None,
) -> dict[str, Any]:
    load_project_env()
    config = load_yaml(config_path)
    textbooks = config.get("textbooks") or []
    textbook = next((item for item in textbooks if item.get("id") == textbook_id), None)
    if not textbook:
        raise ValueError(f"Textbook not found in config: {textbook_id}")
    kg_cfg = config.get("course_graph") or {}
    out_dir = PROJECT_ROOT / str(kg_cfg.get("output_dir") or "data/course_graph")

    env = llm_env(config)
    if not is_llm_configured(*env["api_key_names"]):
        raise RuntimeError("No LLM API key configured. Disease-level curation requires model-based semantic analysis.")

    cards, relations, _summary = load_master(out_dir)
    cards_by_id = {str(card.get("id")): card for card in cards if card.get("id")}
    cards_by_chapter: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for card in cards:
        number = card.get("chapter_number")
        if isinstance(number, int):
            cards_by_chapter[number].append(card)

    selected_chapter_numbers: set[int] | None = None
    if chapter_start is not None or chapter_end is not None:
        low = chapter_start if chapter_start is not None else min(cards_by_chapter)
        high = chapter_end if chapter_end is not None else max(cards_by_chapter)
        selected_chapter_numbers = {number for number in cards_by_chapter if low <= number <= high}
        if not selected_chapter_numbers:
            raise RuntimeError(f"No chapters found in selected range: {low}-{high}")

    relations_by_chapter: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for relation in relations:
        source_card = cards_by_id.get(str(relation.get("source_card_id") or ""))
        target_card = cards_by_id.get(str(relation.get("target_card_id") or ""))
        numbers = {
            item.get("chapter_number")
            for item in (source_card, target_card)
            if isinstance(item, dict) and isinstance(item.get("chapter_number"), int)
        }
        for number in numbers:
            relations_by_chapter[int(number)].append(relation)

    chapter_metas = load_chapter_metas(out_dir, textbook_id)
    selected_chapter_metas = (
        {number: meta for number, meta in chapter_metas.items() if selected_chapter_numbers is None or number in selected_chapter_numbers}
        if chapter_metas
        else {}
    )
    chapter_results = run_chapter_curation(
        textbook,
        config,
        out_dir,
        selected_chapter_metas,
        cards_by_chapter,
        relations_by_chapter,
        cards_by_id,
        force,
        selected_chapter_numbers,
    )
    if selected_chapter_numbers is None:
        run_label = "full"
    else:
        run_label = f"chapters_{min(selected_chapter_numbers):02d}_{max(selected_chapter_numbers):02d}"
    global_result = run_global_consolidation(textbook, config, out_dir, chapter_results, force, run_label)
    assembled = assemble_disease_core(textbook, selected_chapter_metas, cards_by_id, chapter_results, global_result)

    disease_dir = out_dir / "disease_core"
    write_jsonl(disease_dir / "disease_cards.jsonl", assembled["disease_cards"])
    write_jsonl(disease_dir / "disease_edges.jsonl", assembled["disease_edges"])
    write_json(disease_dir / "chapter_disease_index.json", assembled["chapter_index"])
    write_jsonl(disease_dir / "review_queue.jsonl", assembled["review_queue"])
    write_json(disease_dir / "disease_graph.json", assembled["disease_graph"])
    write_json(disease_dir / "disease_graph_summary.json", assembled["summary"])
    write_json(
        disease_dir / "last_run_summary.json",
        {
            "ok": True,
            "textbook": textbook,
            "summary": assembled["summary"],
            "source_master_cards": len(cards),
            "source_master_relations": len(relations),
            "chapter_range": (
                None
                if selected_chapter_numbers is None
                else [min(selected_chapter_numbers), max(selected_chapter_numbers)]
            ),
            "output_dir": str(disease_dir.relative_to(PROJECT_ROOT)),
        },
    )
    return assembled["summary"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Build disease-only core graph from textbook knowledge cards.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--textbook-id", default=DEFAULT_TEXTBOOK_ID)
    parser.add_argument("--chapter-start", type=int, default=None)
    parser.add_argument("--chapter-end", type=int, default=None)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    summary = build_disease_core(args.textbook_id, args.config, args.force, args.chapter_start, args.chapter_end)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
