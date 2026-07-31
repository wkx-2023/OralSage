"""Student digital-twin simulation for learning-plan decisions.

The agent is intentionally deterministic and explainable.  It builds a compact
virtual learner state from existing memory/profile signals, predicts near-term
bottlenecks, simulates several candidate learning paths, and returns the path
with the best expected benefit under the learner's available time.
"""

from __future__ import annotations

import re
from functools import lru_cache
from datetime import date, datetime
from typing import Any

from .memory import LearningMemory
from .spaced_repetition import build_spaced_review_schedule


_DEFAULT_AVAILABLE_MINUTES = 20
_INTERNAL_KG_REFERENCE_RE = re.compile(
    r"(?:kg://|core:)[^\s，。；;、：:！？!?（）()\[\]【】《》<>\"']+",
    re.I,
)
_GENERIC_LABELS = {
    "薄弱知识点",
    "低掌握知识点",
    "核心薄弱点",
    "相关知识点",
    "当前课程进度",
    "学习任务",
}
_COLD_START_TOPIC_HINTS = (
    "牙周炎",
    "慢性牙周炎",
    "侵袭性牙周炎",
    "牙槽骨吸收",
    "龋病",
    "牙髓炎",
    "根尖周炎",
    "牙龈炎",
    "口腔黏膜",
    "错畸形",
)
_NEXT_LESSON_PRIORS = (
    ("coldstart:periodontitis-staging", "牙周炎诊断与分期"),
    ("coldstart:caries-diagnosis", "龋病诊断"),
    ("coldstart:pulpitis-treatment", "牙髓炎治疗原则"),
    ("coldstart:periapical-periodontitis-diagnosis", "根尖周炎诊断"),
    ("coldstart:periodontal-maintenance", "牙周维护治疗"),
)
_ENGLISH_TERM_RULES = (
    ("aggressive periodontitis", "侵袭性牙周炎"),
    ("chronic periodontitis", "慢性牙周炎"),
    ("periodontitis", "牙周炎"),
    ("periodontal disease", "牙周病"),
    ("alveolar bone resorption", "牙槽骨吸收"),
    ("bone resorption", "骨吸收"),
    ("differential diagnosis", "鉴别诊断"),
    ("diagnosis", "诊断"),
    ("treatment", "治疗"),
    ("mechanism", "机制"),
    ("etiology", "病因"),
    ("risk factor", "危险因素"),
)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _as_float(value: Any, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result != result or result in (float("inf"), float("-inf")):
        return default
    return result


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _target_date(value: str | date | None) -> date:
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return date.today()
    return date.fromisoformat(text[:10])


def _profile_daily_minutes(profile: dict[str, Any]) -> int:
    for key in ("daily_available_minutes", "daily_minutes", "available_minutes"):
        value = profile.get(key)
        if value is not None:
            return max(10, min(_as_int(value, _DEFAULT_AVAILABLE_MINUTES), 120))
    return _DEFAULT_AVAILABLE_MINUTES


def _memory_slot_items(learner_memory: dict[str, Any], slot: str) -> list[dict[str, Any]]:
    data = learner_memory.get(slot)
    if not isinstance(data, dict):
        return []
    return [item for item in data.get("items", []) if isinstance(item, dict)]


def _normalize_kg_ids(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _clean_label(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _is_generic_or_internal_label(value: Any) -> bool:
    text = _clean_label(value)
    if not text:
        return True
    if text in _GENERIC_LABELS:
        return True
    if _INTERNAL_KG_REFERENCE_RE.search(text):
        return True
    return False


def _translate_english_concept(value: Any) -> str:
    text = _clean_label(value).replace("_", " ").replace("-", " ")
    if not text or re.search(r"[\u4e00-\u9fff]", text):
        return text
    lowered = text.lower()
    pieces: list[str] = []
    matched_phrases: list[str] = []
    used: set[str] = set()
    for phrase, translated in _ENGLISH_TERM_RULES:
        if any(phrase in matched for matched in matched_phrases):
            continue
        if phrase in lowered and translated not in used:
            pieces.append(translated)
            matched_phrases.append(phrase)
            used.add(translated)
    if pieces:
        return "".join(pieces)
    return text


@lru_cache(maxsize=1)
def _kg_display_name_index() -> dict[str, str]:
    index: dict[str, str] = {}
    try:
        from backend.services.learning.planner import load_kg_nodes
        from backend.services.knowledge_graph.agent import (
            canonical_book_title,
            stable_core_identity,
        )
    except Exception:
        return index

    try:
        nodes = load_kg_nodes()
    except Exception:
        return index

    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = _clean_label(node.get("node_id"))
        name = _clean_label(node.get("canonical_name") or node.get("name"))
        if not node_id or not name:
            continue
        index.setdefault(node_id, name)
        book_title = _clean_label(node.get("book_title"))
        node_type = _clean_label(node.get("node_type"))
        metadata = node.get("metadata")
        document_id = (
            _clean_label(metadata.get("document_id"))
            if isinstance(metadata, dict)
            else ""
        )
        if book_title and node_type:
            stable_id = stable_core_identity(
                canonical_book_title(document_id, book_title),
                node_type,
                name,
            )
            index.setdefault(stable_id, name)
    return index


def _kg_display_name(kg_node_id: Any) -> str:
    kg_id = _clean_label(kg_node_id)
    if not kg_id:
        return ""
    indexed = _kg_display_name_index().get(kg_id)
    if indexed:
        return indexed
    try:
        from backend.services.knowledge_graph import get_graph_node

        result = get_graph_node(kg_id)
    except Exception:
        result = {}
    if isinstance(result, dict) and result.get("ok"):
        for source in (result.get("card"), result.get("node")):
            if isinstance(source, dict):
                name = _clean_label(source.get("canonical_name") or source.get("name") or source.get("label"))
                if name:
                    return name
    if kg_id.startswith("core:"):
        return _translate_english_concept(kg_id.removeprefix("core:"))
    if kg_id.startswith("kg://"):
        return _translate_english_concept(kg_id.rsplit("/", 1)[-1])
    return ""


def _concrete_kg_label(kg_node_id: Any, fallback: Any) -> str:
    kg_name = _kg_display_name(kg_node_id)
    fallback_text = _translate_english_concept(fallback)
    if kg_name:
        if fallback_text and not _is_generic_or_internal_label(fallback_text):
            if re.search(r"[\u4e00-\u9fff]", fallback_text) and kg_name in fallback_text:
                return fallback_text
            lowered = _clean_label(fallback).lower()
            if "differential diagnosis" in lowered and "鉴别诊断" not in kg_name:
                return f"{kg_name}鉴别诊断"
            if "mechanism" in lowered and "机制" not in kg_name:
                return f"{kg_name}机制"
        return kg_name
    if fallback_text and not _is_generic_or_internal_label(fallback_text):
        return fallback_text
    return "核心薄弱知识点"


def _resolve_bottleneck_labels(bottlenecks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for item in bottlenecks:
        kg_id = item.get("kg_node_id")
        label = _concrete_kg_label(kg_id, item.get("label"))
        item["label"] = label
        if kg_id:
            item["kg_node_names"] = {str(kg_id): label}
    return bottlenecks


def _iter_outline_nodes(value: Any) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if any(key in value for key in ("kg_node_ids", "title", "name", "learning_objectives")):
            nodes.append(value)
        for child_key in ("nodes", "children", "outline", "items", "sections"):
            child = value.get(child_key)
            if isinstance(child, (dict, list)):
                nodes.extend(_iter_outline_nodes(child))
    elif isinstance(value, list):
        for item in value:
            nodes.extend(_iter_outline_nodes(item))
    return nodes


def _cold_start_candidate(
    *,
    kg_node_id: str,
    label: str,
    mastery: float,
    risk: float,
    reason: str,
    evidence_source: str,
) -> dict[str, Any] | None:
    kg_id = _clean_label(kg_node_id)
    display = _concrete_kg_label(kg_id, label)
    if not kg_id or _is_generic_or_internal_label(display):
        return None
    return {
        "kg_node_id": kg_id,
        "label": display,
        "mastery_score": round(_clamp(mastery, 0.0, 1.0), 3),
        "risk_score": round(_clamp(risk, 0.0, 0.98), 3),
        "retention_estimate": round(_clamp(0.46 + mastery * 0.42, 0.0, 1.0), 3),
        "overdue_days": 0,
        "difficulty": round(_clamp(1.0 - mastery, 0.0, 1.0), 3),
        "reason": reason,
        "evidence_sources": [evidence_source],
        "simulation_hint": "cold_start_concrete_kg_node",
        "kg_node_names": {kg_id: display},
    }


def _build_cold_start_bottlenecks(
    memory: LearningMemory,
    *,
    sessions: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    seen_labels: set[str] = set()

    def add(item: dict[str, Any] | None) -> None:
        if not item:
            return
        kg_id = str(item.get("kg_node_id") or "").strip()
        label_key = str(item.get("label") or "").strip().lower()
        if kg_id and kg_id not in candidates and label_key not in seen_labels:
            candidates[kg_id] = item
            if label_key:
                seen_labels.add(label_key)

    for session in sessions[:4]:
        session_id = _clean_label(session.get("id"))
        if not session_id:
            continue
        try:
            outline_record = memory.latest_outline(session_id)
        except Exception:
            outline_record = None
        outline = outline_record.get("outline") if isinstance(outline_record, dict) else None
        for node in _iter_outline_nodes(outline)[:12]:
            kg_ids = _normalize_kg_ids(node.get("kg_node_ids"))
            if not kg_ids:
                continue
            title = _clean_label(node.get("title") or node.get("name"))
            objectives = node.get("learning_objectives")
            if isinstance(objectives, list) and objectives:
                title = title or _clean_label(objectives[0])
            for kg_id in kg_ids[:2]:
                add(_cold_start_candidate(
                    kg_node_id=kg_id,
                    label=title or kg_id,
                    mastery=0.56,
                    risk=0.52,
                    reason="当前用户暂无显式薄弱点，系统选取最近课程中的关键 KG 节点进行冷启动仿真。",
                    evidence_source="recent_course_outline",
                ))
                if len(candidates) >= limit:
                    return list(candidates.values())[:limit]

    try:
        from backend.services.learning.planner import load_kg_nodes

        kg_nodes = load_kg_nodes()
    except Exception:
        kg_nodes = []

    scored: list[tuple[int, int, str, str]] = []
    for node in kg_nodes:
        if not isinstance(node, dict):
            continue
        kg_id = _clean_label(node.get("node_id"))
        name = _clean_label(node.get("canonical_name") or node.get("name"))
        if not kg_id or not name or _is_generic_or_internal_label(name):
            continue
        haystack = f"{name} {_clean_label(node.get('book_title'))}"
        score = 0
        for rank, hint in enumerate(_COLD_START_TOPIC_HINTS):
            if name == hint:
                score = max(score, 300 - rank)
            elif name.startswith(hint):
                score = max(score, 180 - rank)
            elif hint in haystack:
                score = max(score, 100 - rank)
        if score:
            scored.append((score, -len(name), name, kg_id))

    for index, (_, _, name, kg_id) in enumerate(sorted(scored, key=lambda item: (-item[0], -item[1], item[2], item[3]))):
        add(_cold_start_candidate(
            kg_node_id=kg_id,
            label=name,
            mastery=max(0.48, 0.6 - index * 0.025),
            risk=min(0.72, 0.54 + index * 0.025),
            reason="当前用户暂无显式薄弱点，系统基于口腔医学知识图谱选取高频核心概念进行冷启动仿真。",
            evidence_source="kg_cold_start_prior",
        ))
        if len(candidates) >= limit:
            break

    hardcoded_priors = [
        ("coldstart:periodontitis", "牙周炎"),
        ("coldstart:alveolar-bone-resorption", "牙槽骨吸收"),
        ("coldstart:caries-differential-diagnosis", "龋病鉴别诊断"),
        ("coldstart:pulpitis", "牙髓炎"),
        ("coldstart:periapical-periodontitis", "根尖周炎"),
    ]
    for index, (kg_id, name) in enumerate(hardcoded_priors):
        add(_cold_start_candidate(
            kg_node_id=kg_id,
            label=name,
            mastery=max(0.48, 0.58 - index * 0.025),
            risk=min(0.7, 0.55 + index * 0.025),
            reason="当前用户暂无显式薄弱点，系统基于口腔医学高频核心概念进行冷启动仿真。",
            evidence_source="medical_cold_start_prior",
        ))
        if len(candidates) >= limit:
            break

    return list(candidates.values())[:limit]


def _build_next_lesson_target(
    memory: LearningMemory,
    *,
    sessions: list[dict[str, Any]],
    bottlenecks: list[dict[str, Any]],
) -> dict[str, Any]:
    weak_ids = {str(item.get("kg_node_id") or "").strip() for item in bottlenecks}
    weak_labels = {str(item.get("label") or "").strip() for item in bottlenecks}

    def candidate(kg_node_id: Any, label: Any, source: str) -> dict[str, Any] | None:
        kg_id = _clean_label(kg_node_id)
        display = _concrete_kg_label(kg_id, label)
        if not kg_id or not display:
            return None
        if kg_id in weak_ids or display in weak_labels or _is_generic_or_internal_label(display):
            return None
        return {
            "kg_node_id": kg_id,
            "label": display,
            "source": source,
            "kg_node_names": {kg_id: display},
        }

    for session in sessions[:4]:
        session_id = _clean_label(session.get("id"))
        if not session_id:
            continue
        try:
            outline_record = memory.latest_outline(session_id)
        except Exception:
            outline_record = None
        outline = outline_record.get("outline") if isinstance(outline_record, dict) else None
        outline_nodes = _iter_outline_nodes(outline)
        current_id = _clean_label(session.get("current_outline_node_id"))
        start_index = 0
        if current_id:
            for index, node in enumerate(outline_nodes):
                if _clean_label(node.get("id") or node.get("node_id")) == current_id:
                    start_index = min(index + 1, len(outline_nodes))
                    break
        ordered_nodes = outline_nodes[start_index:] + outline_nodes[:start_index]
        for node in ordered_nodes[:16]:
            label = _clean_label(node.get("title") or node.get("name"))
            objectives = node.get("learning_objectives")
            if isinstance(objectives, list) and objectives:
                label = label or _clean_label(objectives[0])
            for kg_id in _normalize_kg_ids(node.get("kg_node_ids")):
                item = candidate(kg_id, label or kg_id, "next_course_outline_node")
                if item:
                    return item

    try:
        from backend.services.learning.planner import load_kg_nodes

        kg_nodes = load_kg_nodes()
    except Exception:
        kg_nodes = []
    scored: list[tuple[int, int, str, str]] = []
    for node in kg_nodes:
        if not isinstance(node, dict):
            continue
        kg_id = _clean_label(node.get("node_id"))
        name = _clean_label(node.get("canonical_name") or node.get("name"))
        if not kg_id or kg_id in weak_ids or not name or name in weak_labels:
            continue
        haystack = f"{name} {_clean_label(node.get('book_title'))}"
        score = 0
        for rank, hint in enumerate(_COLD_START_TOPIC_HINTS):
            if name == hint:
                score = max(score, 260 - rank)
            elif name.startswith(hint):
                score = max(score, 170 - rank)
            elif hint in haystack:
                score = max(score, 90 - rank)
        if score:
            scored.append((score, -len(name), name, kg_id))
    for _, _, name, kg_id in sorted(scored, key=lambda item: (-item[0], -item[1], item[2], item[3])):
        item = candidate(kg_id, name, "kg_next_lesson_prior")
        if item:
            return item

    for kg_id, label in _NEXT_LESSON_PRIORS:
        item = candidate(kg_id, label, "medical_next_lesson_prior")
        if item:
            return item
    return {
        "kg_node_id": "coldstart:periodontitis-staging",
        "label": "牙周炎诊断与分期",
        "source": "medical_next_lesson_prior",
        "kg_node_names": {"coldstart:periodontitis-staging": "牙周炎诊断与分期"},
    }


def _build_fallback_bottlenecks(
    learner_memory: dict[str, Any],
    profile: dict[str, Any],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}

    def merge(
        kg_node_id: str,
        *,
        label: str,
        mastery: float,
        risk: float,
        reason: str,
        evidence_source: str,
    ) -> None:
        kg_id = str(kg_node_id or "").strip()
        if not kg_id:
            return
        existing = candidates.get(kg_id)
        if existing and existing["risk_score"] >= risk:
            if evidence_source not in existing["evidence_sources"]:
                existing["evidence_sources"].append(evidence_source)
            return
        candidates[kg_id] = {
            "kg_node_id": kg_id,
            "label": " ".join(str(label or kg_id).split()).strip() or kg_id,
            "mastery_score": round(_clamp(mastery, 0.0, 1.0), 3),
            "risk_score": round(_clamp(risk, 0.0, 0.98), 3),
            "retention_estimate": round(_clamp(0.42 + mastery * 0.45, 0.0, 1.0), 3),
            "overdue_days": 0,
            "difficulty": round(_clamp(1.0 - mastery, 0.0, 1.0), 3),
            "reason": reason,
            "evidence_sources": [evidence_source],
            "simulation_hint": "fallback_mastery_signal",
        }

    for item in _memory_slot_items(learner_memory, "weaknesses"):
        severity = _as_float(item.get("severity", item.get("confidence", 0.65)), 0.65)
        label = str(item.get("label") or item.get("text") or "薄弱知识点")
        for kg_id in _normalize_kg_ids(item.get("kg_node_ids")):
            mastery = _clamp(0.5 - severity * 0.24, 0.08, 0.7)
            merge(
                kg_id,
                label=label,
                mastery=mastery,
                risk=0.48 + severity * 0.42,
                reason="长期记忆中标记为薄弱点，建议在新课前先修复前置理解。",
                evidence_source="learner_memory_weakness",
            )

    for kg_id, raw in (profile.get("mastery_by_kg_node") or {}).items():
        if isinstance(raw, dict):
            score = _as_float(raw.get("score", raw.get("mastery")), 0.5)
            label = str(raw.get("label") or kg_id)
        else:
            score = _as_float(raw, 0.5)
            label = str(kg_id)
        if score <= 0.72:
            merge(
                str(kg_id),
                label=label,
                mastery=score,
                risk=0.42 + (1.0 - score) * 0.5,
                reason="画像中的 KG 节点掌握度偏低，后续学习可能出现概念断点。",
                evidence_source="profile_mastery_by_kg_node",
            )

    rows = sorted(
        candidates.values(),
        key=lambda item: (-float(item["risk_score"]), str(item["label"])),
    )
    return rows[: max(1, min(limit, 8))]


def _bottlenecks_from_spaced_reviews(
    spaced_reviews: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    bottlenecks: list[dict[str, Any]] = []
    for item in spaced_reviews[: max(1, min(limit, 8))]:
        mastery = _as_float(item.get("mastery_score"), 0.45)
        retention = _as_float(item.get("retention_estimate"), 0.55)
        overdue = max(0, _as_int(item.get("overdue_days"), 0))
        difficulty = _as_float(item.get("difficulty"), 0.5)
        risk = _clamp(
            0.18
            + (1.0 - mastery) * 0.42
            + (1.0 - retention) * 0.28
            + min(0.18, overdue * 0.035)
            + difficulty * 0.12,
            0.0,
            0.98,
        )
        if overdue > 0:
            reason = f"掌握度约 {round(mastery * 100)}%，已超过最佳复习点 {overdue} 天。"
        elif item.get("next_review_date"):
            reason = f"掌握度约 {round(mastery * 100)}%，最佳复习点为 {item['next_review_date']}。"
        else:
            reason = f"掌握度约 {round(mastery * 100)}%，记忆保持率估计偏低。"
        bottlenecks.append({
            "kg_node_id": item.get("kg_node_id"),
            "label": item.get("label") or item.get("kg_node_id"),
            "mastery_score": round(mastery, 3),
            "risk_score": round(risk, 3),
            "retention_estimate": round(retention, 3),
            "overdue_days": overdue,
            "difficulty": round(difficulty, 3),
            "reason": reason,
            "evidence_sources": item.get("evidence_sources", []),
            "simulation_hint": "sm2_forgetting_risk",
            "spaced_repetition": {
                "algorithm": item.get("trace", {}).get("algorithm", "sm2_kg_node_v1"),
                "interval_days": item.get("interval_days"),
                "next_review_date": item.get("next_review_date"),
                "priority": item.get("priority"),
            },
        })
    return bottlenecks


def _overall_mastery(bottlenecks: list[dict[str, Any]]) -> float:
    if not bottlenecks:
        return 0.72
    scores = [_as_float(item.get("mastery_score"), 0.5) for item in bottlenecks]
    return _clamp(sum(scores) / len(scores), 0.0, 1.0)


def _overall_risk(bottlenecks: list[dict[str, Any]]) -> float:
    if not bottlenecks:
        return 0.22
    top = [_as_float(item.get("risk_score"), 0.0) for item in bottlenecks[:3]]
    return _clamp(sum(top) / max(1, len(top)), 0.0, 0.98)


def _simulate_paths(
    bottlenecks: list[dict[str, Any]],
    *,
    available_minutes: int,
    next_lesson_target: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    top_risk = _overall_risk(bottlenecks)
    weak_count = len(bottlenecks)
    time_factor = _clamp(available_minutes / 30.0, 0.45, 1.35)
    top = bottlenecks[0] if bottlenecks else {}
    second = bottlenecks[1] if len(bottlenecks) > 1 else top

    continue_gain = _clamp(0.05 * time_factor + max(0.0, 0.08 - top_risk * 0.05), 0.02, 0.12)
    continue_risk_drop = _clamp(0.04 * time_factor - top_risk * 0.015, 0.01, 0.08)

    review_gain = _clamp(0.10 * time_factor + top_risk * 0.07, 0.06, 0.20)
    review_risk_drop = _clamp(0.16 * time_factor + top_risk * 0.16 + min(0.05, weak_count * 0.012), 0.10, 0.36)

    practice_gain = _clamp(0.08 * time_factor + top_risk * 0.05, 0.05, 0.16)
    practice_risk_drop = _clamp(0.11 * time_factor + top_risk * 0.12, 0.07, 0.28)

    def score(gain: float, risk_drop: float, cognitive_load: float) -> float:
        time_penalty = max(0.0, (20 - available_minutes) / 100.0)
        return round(_clamp(gain * 2.2 + risk_drop * 1.8 - cognitive_load - time_penalty, 0.0, 0.98), 3)

    top_label = str(top.get("label") or "核心薄弱点")
    second_label = str(second.get("label") or top_label)
    top_kg_id = str(top.get("kg_node_id") or "").strip()
    second_kg_id = str(second.get("kg_node_id") or "").strip()
    next_target = next_lesson_target or {}
    next_label = str(next_target.get("label") or "牙周炎诊断与分期")
    next_kg_id = str(next_target.get("kg_node_id") or "coldstart:periodontitis-staging").strip()
    next_kg_names = (
        dict(next_target.get("kg_node_names") or {})
        if isinstance(next_target.get("kg_node_names"), dict)
        else ({next_kg_id: next_label} if next_kg_id else {})
    )
    paths = [
        {
            "path_id": "continue_new_lesson",
            "name": "继续学习新章节",
            "strategy": "new_lesson_first",
            "summary": f"进入“{next_label}”，同时监控“{top_label}”对新章节理解的影响。",
            "estimated_minutes": available_minutes,
            "mastery_gain": round(continue_gain, 3),
            "risk_reduction": round(continue_risk_drop, 3),
            "cognitive_load": 0.08,
            "score": score(continue_gain, continue_risk_drop, 0.08),
            "steps": [
                {
                    "title": f"新学：{next_label}",
                    "minutes": available_minutes,
                    "task_type": "continue_lesson",
                    "learning_target": next_label,
                    "kg_node_ids": [next_kg_id] if next_kg_id else [],
                    "kg_node_names": next_kg_names,
                    "monitoring_kg_node": {
                        "kg_node_id": top_kg_id,
                        "label": top_label,
                        "risk_score": top.get("risk_score"),
                    } if top_kg_id else {},
                }
            ],
        },
        {
            "path_id": "spaced_review_focus",
            "name": "薄弱点间隔复习",
            "strategy": "review_first",
            "summary": f"优先修复“{top_label}”，降低后续章节理解阻塞。",
            "estimated_minutes": min(available_minutes, 18),
            "mastery_gain": round(review_gain * 0.86, 3),
            "risk_reduction": round(review_risk_drop * 0.86, 3),
            "cognitive_load": 0.05,
            "score": score(review_gain * 0.86, review_risk_drop * 0.86, 0.05),
            "steps": [
                {
                    "title": f"复习：{top_label}",
                    "minutes": min(available_minutes, 15),
                    "task_type": "review_section",
                    "learning_target": top_label,
                    "kg_node_ids": [top_kg_id] if top_kg_id else [],
                    "kg_node_names": {top_kg_id: top_label} if top_kg_id else {},
                }
            ],
        },
        {
            "path_id": "review_plus_practice",
            "name": "薄弱点复习 + 小测",
            "strategy": "review_then_practice",
            "summary": f"先复习“{top_label}”，再用病例题验证迁移。",
            "estimated_minutes": available_minutes,
            "mastery_gain": round(review_gain, 3),
            "risk_reduction": round(review_risk_drop, 3),
            "cognitive_load": 0.07,
            "score": score(review_gain, review_risk_drop, 0.07),
            "steps": [
                {
                    "title": f"复习：{top_label}",
                    "minutes": max(8, min(12, round(available_minutes * 0.6))),
                    "task_type": "review_section",
                    "learning_target": top_label,
                    "kg_node_ids": [top_kg_id] if top_kg_id else [],
                    "kg_node_names": {top_kg_id: top_label} if top_kg_id else {},
                },
                {
                    "title": f"小测：{second_label}",
                    "minutes": max(6, available_minutes - max(8, min(12, round(available_minutes * 0.6)))),
                    "task_type": "basic_practice",
                    "learning_target": second_label,
                    "kg_node_ids": [second_kg_id] if second_kg_id else [],
                    "kg_node_names": {second_kg_id: second_label} if second_kg_id else {},
                    "practice_level": "basic",
                },
            ],
        },
    ]
    paths.sort(key=lambda item: (-float(item["score"]), str(item["path_id"])))
    for item in paths:
        item["recommended"] = item is paths[0]
    return paths


def _plan_items_from_path(
    path: dict[str, Any],
    *,
    plan_date: str,
    bottlenecks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    risk_by_kg = {
        str(item.get("kg_node_id")): item
        for item in bottlenecks
        if item.get("kg_node_id")
    }
    items: list[dict[str, Any]] = []
    for index, step in enumerate(path.get("steps", []), start=1):
        kg_ids = [str(value) for value in step.get("kg_node_ids", []) if value]
        kg_node_names = {
            str(key): str(value)
            for key, value in (step.get("kg_node_names") or {}).items()
            if str(key).strip() and str(value).strip()
        }
        first_risk = risk_by_kg.get(kg_ids[0]) if kg_ids else None
        task_type = str(step.get("task_type") or "topic_learning")
        scope = {
            "scope_type": "student_twin_simulation",
            "path_id": path.get("path_id"),
            "strategy": path.get("strategy"),
            "kg_node_ids": kg_ids,
            "kg_node_names": kg_node_names,
            "student_twin": {
                "agent": "student_twin_v1",
                "mastery_gain": path.get("mastery_gain"),
                "risk_reduction": path.get("risk_reduction"),
                "path_score": path.get("score"),
                "bottleneck": first_risk,
            },
        }
        title = str(step.get("title") or "数字孪生学习任务")
        items.append({
            "recommendation_id": f"student-twin:{path.get('path_id')}:{index}",
            "title": title,
            "category": "student_twin",
            "task_type": task_type,
            "learning_target": step.get("learning_target") or title,
            "reason": path.get("summary") or "数字孪生仿真建议。",
            "notes": (
                "StudentTwinAgent 基于掌握度、薄弱点、遗忘风险和可用时间模拟"
                "不同学习路径后生成。"
            ),
            "estimated_minutes": max(5, min(_as_int(step.get("minutes"), 10), 90)),
            "kg_node_ids": kg_ids,
            "kg_node_names": kg_node_names,
            "learning_scope": scope,
            "practice_level": step.get("practice_level"),
            "plan_date": plan_date,
        })
    return items


def _path_display_label(path: dict[str, Any]) -> str:
    strategy = str(path.get("strategy") or path.get("path_id") or "")
    if strategy == "new_lesson_first":
        return "继续新课"
    if strategy == "review_first":
        return "间隔复习"
    if strategy == "review_then_practice":
        return "复习 + 小测"
    return str(path.get("name") or "学习路径")


def _build_mastery_forecast(
    *,
    overall_mastery: float,
    paths: list[dict[str, Any]],
    horizon_days: int = 3,
) -> dict[str, Any]:
    series: list[dict[str, Any]] = []
    colors = {
        "new_lesson_first": "#8b97a8",
        "review_first": "#0f766e",
        "review_then_practice": "#2563eb",
    }
    for path in paths:
        gain = _as_float(path.get("mastery_gain"), 0.06)
        risk_drop = _as_float(path.get("risk_reduction"), 0.05)
        load = _as_float(path.get("cognitive_load"), 0.06)
        points: list[dict[str, Any]] = []
        for day in range(horizon_days + 1):
            if day == 0:
                mastery = overall_mastery
            else:
                retention_decay = 0.012 * day + load * 0.03
                consolidation = gain * (1 - (0.54 ** day)) + risk_drop * 0.18 * (day / horizon_days)
                mastery = overall_mastery + consolidation - retention_decay
            points.append({
                "day": day,
                "mastery": round(_clamp(mastery, 0.0, 0.98), 3),
            })
        strategy = str(path.get("strategy") or "")
        series.append({
            "path_id": path.get("path_id"),
            "label": _path_display_label(path),
            "color": colors.get(strategy, "#6d5bd0"),
            "recommended": bool(path.get("recommended")),
            "points": points,
        })
    return {
        "horizon_days": horizon_days,
        "target_retention": 0.9,
        "series": series,
    }


def _risk_status(*, risk: float, mastery: float) -> str:
    if risk >= 0.68 or mastery < 0.45:
        return "danger"
    if risk >= 0.5 or mastery < 0.62:
        return "warn"
    if risk >= 0.34 or mastery < 0.76:
        return "mid"
    return "ok"


def _build_cognitive_map(bottlenecks: list[dict[str, Any]]) -> dict[str, Any]:
    default_nodes = [
        {"label": "牙周炎", "mastery_score": 0.72, "risk_score": 0.28},
        {"label": "牙槽骨吸收", "mastery_score": 0.66, "risk_score": 0.36},
        {"label": "龋病鉴别诊断", "mastery_score": 0.54, "risk_score": 0.52},
        {"label": "慢性牙周炎", "mastery_score": 0.39, "risk_score": 0.74},
        {"label": "根尖周炎", "mastery_score": 0.71, "risk_score": 0.3},
        {"label": "牙髓炎", "mastery_score": 0.42, "risk_score": 0.69},
    ]
    source_nodes = list(bottlenecks[:6])
    while len(source_nodes) < 6:
        source_nodes.append(default_nodes[len(source_nodes)])

    nodes: list[dict[str, Any]] = []
    for index, item in enumerate(source_nodes[:6]):
        mastery = _as_float(item.get("mastery_score"), 0.55)
        risk = _as_float(item.get("risk_score"), 0.4)
        nodes.append({
            "node_index": index,
            "kg_node_id": item.get("kg_node_id") or f"synthetic:{index}",
            "label": str(item.get("label") or default_nodes[index]["label"]),
            "mastery": round(_clamp(mastery, 0.0, 1.0), 3),
            "risk": round(_clamp(risk, 0.0, 0.98), 3),
            "status": _risk_status(risk=risk, mastery=mastery),
            "reason": item.get("reason"),
        })

    base_edges = [
        {"source_index": 0, "target_index": 1, "type": "prerequisite"},
        {"source_index": 0, "target_index": 2, "type": "transfer"},
        {"source_index": 1, "target_index": 3, "type": "concept_link"},
        {"source_index": 2, "target_index": 3, "type": "risk_bridge"},
        {"source_index": 2, "target_index": 4, "type": "reinforcement"},
        {"source_index": 3, "target_index": 5, "type": "confusion_link"},
    ]
    ranked = sorted(nodes, key=lambda node: float(node["risk"]), reverse=True)
    high_risk_indices = [int(node["node_index"]) for node in ranked[:3]]
    risk_edges: list[dict[str, Any]] = []
    if len(high_risk_indices) >= 2:
        risk_edges.append({
            "source_index": high_risk_indices[0],
            "target_index": high_risk_indices[1],
            "risk_flow": round((ranked[0]["risk"] + ranked[1]["risk"]) / 2, 3),
        })
    if len(high_risk_indices) >= 3:
        risk_edges.append({
            "source_index": high_risk_indices[1],
            "target_index": high_risk_indices[2],
            "risk_flow": round((ranked[1]["risk"] + ranked[2]["risk"]) / 2, 3),
        })
    return {
        "layout": "fixed_six_node_v1",
        "nodes": nodes,
        "edges": base_edges,
        "risk_edges": risk_edges,
        "caption": "红色链路表示预测卡点传播路径",
    }


def _build_optimal_strategy(
    *,
    recommended_path: dict[str, Any],
    recommended_items: list[dict[str, Any]],
    available_minutes: int,
) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    for item in recommended_items[:3]:
        minutes = _as_int(item.get("estimated_minutes"), 0)
        steps.append({
            "minutes": minutes or max(5, min(available_minutes, 12)),
            "title": item.get("title") or item.get("learning_target") or "数字孪生学习任务",
            "description": item.get("reason") or item.get("notes") or "根据数字孪生仿真结果生成。",
            "task_type": item.get("task_type"),
            "kg_node_ids": item.get("kg_node_ids", []),
        })
    if not steps:
        steps.append({
            "minutes": available_minutes,
            "title": recommended_path.get("name") or "执行推荐学习路径",
            "description": recommended_path.get("summary") or "按推荐路径完成今日学习。",
            "task_type": "student_twin",
            "kg_node_ids": [],
        })
    return {
        "title": f"今日最优 {available_minutes} 分钟策略",
        "subtitle": f"推荐路径：{recommended_path.get('name') or '数字孪生推荐路径'}",
        "path_id": recommended_path.get("path_id"),
        "can_generate_plan": bool(recommended_items),
        "steps": steps,
    }


def _build_dashboard(
    *,
    overall_mastery: float,
    bottlenecks: list[dict[str, Any]],
    paths: list[dict[str, Any]],
    recommended_path: dict[str, Any],
    recommended_items: list[dict[str, Any]],
    available_minutes: int,
) -> dict[str, Any]:
    return {
        "path_simulation": {
            "title": "学习路径仿真对比",
            "subtitle": "虚拟学生 Agent 先模拟不同路径，再选择单位时间收益最高方案",
            "paths": paths,
            "recommended_path_id": recommended_path.get("path_id"),
        },
        "mastery_forecast": _build_mastery_forecast(
            overall_mastery=overall_mastery,
            paths=paths,
        ),
        "cognitive_map": _build_cognitive_map(bottlenecks),
        "optimal_strategy": _build_optimal_strategy(
            recommended_path=recommended_path,
            recommended_items=recommended_items,
            available_minutes=available_minutes,
        ),
    }


def build_student_twin_simulation(
    memory: LearningMemory,
    *,
    user_id: str,
    plan_date: str | date | None = None,
    available_minutes: int | None = None,
    max_bottlenecks: int = 5,
) -> dict[str, Any]:
    """Return a StudentTwinAgent simulation snapshot for a learner."""
    target = _target_date(plan_date)
    profile = memory.get_or_create_profile(user_id)
    learner_memory = memory.get_learner_memory(user_id)
    sessions = memory.list_sessions_for_user(user_id, limit=8)
    minutes = max(10, min(
        int(available_minutes) if available_minutes is not None else _profile_daily_minutes(profile),
        120,
    ))
    limit = max(1, min(int(max_bottlenecks), 8))

    spaced_reviews = build_spaced_review_schedule(
        memory,
        user_id=user_id,
        target_date=target,
        learner_memory=learner_memory,
        sessions=sessions,
        profile=profile,
        max_items=limit,
    )
    bottlenecks = _bottlenecks_from_spaced_reviews(spaced_reviews, limit=limit)
    if len(bottlenecks) < min(3, limit):
        seen = {str(item.get("kg_node_id")) for item in bottlenecks}
        for item in _build_fallback_bottlenecks(learner_memory, profile, limit=limit):
            if str(item.get("kg_node_id")) not in seen:
                bottlenecks.append(item)
                seen.add(str(item.get("kg_node_id")))
            if len(bottlenecks) >= limit:
                break
    if len(bottlenecks) < min(3, limit):
        seen = {str(item.get("kg_node_id")) for item in bottlenecks}
        for item in _build_cold_start_bottlenecks(
            memory,
            sessions=sessions,
            limit=limit,
        ):
            if str(item.get("kg_node_id")) not in seen:
                bottlenecks.append(item)
                seen.add(str(item.get("kg_node_id")))
            if len(bottlenecks) >= limit:
                break

    bottlenecks.sort(key=lambda item: (-float(item.get("risk_score", 0.0)), str(item.get("label") or "")))
    bottlenecks = bottlenecks[:limit]
    bottlenecks = _resolve_bottleneck_labels(bottlenecks)

    overall_mastery = _overall_mastery(bottlenecks)
    overall_risk = _overall_risk(bottlenecks)
    retention_values = [_as_float(item.get("retention_estimate"), 0.62) for item in bottlenecks]
    retention = sum(retention_values) / len(retention_values) if retention_values else 0.72
    review_debt = sum(1 for item in bottlenecks if _as_int(item.get("overdue_days"), 0) > 0)
    if review_debt == 0:
        review_debt = sum(1 for item in bottlenecks if _as_float(item.get("risk_score"), 0.0) >= 0.68)

    next_lesson_target = _build_next_lesson_target(
        memory,
        sessions=sessions,
        bottlenecks=bottlenecks,
    )
    paths = _simulate_paths(
        bottlenecks,
        available_minutes=minutes,
        next_lesson_target=next_lesson_target,
    )
    recommended_path = paths[0] if paths else {}
    recommended_items = _plan_items_from_path(
        recommended_path,
        plan_date=target.isoformat(),
        bottlenecks=bottlenecks,
    ) if recommended_path else []
    dashboard = _build_dashboard(
        overall_mastery=overall_mastery,
        bottlenecks=bottlenecks,
        paths=paths,
        recommended_path=recommended_path,
        recommended_items=recommended_items,
        available_minutes=minutes,
    )
    next_best = paths[1] if len(paths) > 1 else None
    counterfactual = ""
    if recommended_path and next_best:
        best_drop = round(float(recommended_path.get("risk_reduction", 0.0)) * 100)
        alt_drop = round(float(next_best.get("risk_reduction", 0.0)) * 100)
        top_label = str(bottlenecks[0].get("label") or "核心薄弱点") if bottlenecks else "核心薄弱点"
        counterfactual = (
            f"如果选择“{next_best.get('name')}”，预计风险下降约 {alt_drop}%；"
            f"优先执行“{recommended_path.get('name')}”可先修复“{top_label}”，"
            f"预计风险下降约 {best_drop}%。"
        )

    status = "stable"
    if overall_risk >= 0.68:
        status = "high_risk"
    elif overall_risk >= 0.48:
        status = "watch"

    return {
        "user_id": user_id,
        "plan_date": target.isoformat(),
        "agent": "StudentTwinAgent",
        "schema_version": "student_twin.simulation.v1",
        "status": status,
        "available_minutes": minutes,
        "state": {
            "overall_mastery": round(overall_mastery, 3),
            "overall_risk": round(overall_risk, 3),
            "retention_estimate": round(_clamp(retention, 0.0, 1.0), 3),
            "review_debt": int(review_debt),
            "bottleneck_count": len(bottlenecks),
        },
        "bottlenecks": bottlenecks,
        "paths": paths,
        "recommended_path_id": recommended_path.get("path_id"),
        "recommended_plan_items": recommended_items,
        "counterfactual": counterfactual,
        "dashboard": dashboard,
        "trace": {
            "model": "student_twin_v1",
            "simulation_type": "deterministic_counterfactual_path_scoring",
            "uses_spaced_repetition": True,
            "path_score_formula": "mastery_gain * 2.2 + risk_reduction * 1.8 - cognitive_load - time_penalty",
            "spaced_review_candidates": len(spaced_reviews),
            "next_lesson_target": next_lesson_target,
            "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        },
    }
