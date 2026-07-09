"""Deterministic learner onboarding assessment for OralSage."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any

from .memory import LearningMemory
from .schemas import DEFAULT_PROFILE
from .user_model import merge_profile


INSTRUMENT_NAME = "learning_onboarding_v1"
INSTRUMENT_VERSION = "v1"
QUESTION_COUNT = 9


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


QUESTION_BANK: list[dict[str, Any]] = [
    {
        "question_id": "LA_Q01_GOAL",
        "prompt": "你现在学习这个主题，最主要的目标是什么？",
        "options": [
            {"id": "A", "label": "系统掌握这个知识点，建立完整理解"},
            {"id": "B", "label": "准备考试，抓重点和考点"},
            {"id": "C", "label": "结合病例应用，提升临床理解"},
            {"id": "D", "label": "查缺补漏，把薄弱部分补起来"},
        ],
        "dimension_mapping": {
            "A": {"profile_patch": {"goal": "course_learning"}},
            "B": {
                "profile_patch": {"goal": "exam_prep"},
                "style_dimensions": {"interaction_tendency": {"asks_exam_points": 1.0}},
            },
            "C": {
                "profile_patch": {"goal": "clinical_application"},
                "style_dimensions": {"interaction_tendency": {"asks_examples": 0.8}},
            },
            "D": {
                "profile_patch": {"goal": "gap_repair"},
                "style_dimensions": {"planning_support_need": {"high": 0.5}},
            },
        },
    },
    {
        "question_id": "LA_Q02_DAILY_TIME",
        "prompt": "你每天一般愿意拿出多少时间来学习？",
        "options": [
            {"id": "A", "label": "30 分钟左右"},
            {"id": "B", "label": "45 分钟左右"},
            {"id": "C", "label": "1 小时左右"},
            {"id": "D", "label": "2 小时左右"},
        ],
        "dimension_mapping": {
            "A": {
                "profile_patch": {"daily_available_minutes": 30, "pace": "slow"},
                "style_dimensions": {"pace": {"slow": 1.0}},
            },
            "B": {
                "profile_patch": {"daily_available_minutes": 45, "pace": "standard"},
                "style_dimensions": {"pace": {"standard": 1.0}},
            },
            "C": {
                "profile_patch": {"daily_available_minutes": 60, "pace": "standard"},
                "style_dimensions": {"pace": {"standard": 0.8}},
            },
            "D": {
                "profile_patch": {"daily_available_minutes": 120, "pace": "fast"},
                "style_dimensions": {"pace": {"fast": 1.0}},
            },
        },
    },
    {
        "question_id": "LA_Q03_ENTRY_MODE",
        "prompt": "面对一个新知识点，你更希望系统怎么带你进入？",
        "options": [
            {"id": "A", "label": "先给我一个病例或临床情境"},
            {"id": "B", "label": "先讲清楚基本概念和定义"},
            {"id": "C", "label": "先给我一道题，我做完再讲"},
            {"id": "D", "label": "先快速总结重点，再决定怎么展开"},
        ],
        "dimension_mapping": {
            "A": {
                "profile_patch": {"preferred_explanation": "case_first"},
                "style_dimensions": {"entry_mode": {"case_first": 1.0}},
            },
            "B": {
                "profile_patch": {"preferred_explanation": "concept_then_case"},
                "style_dimensions": {"entry_mode": {"concept_first": 1.0}},
            },
            "C": {
                "profile_patch": {"preferred_explanation": "practice_first"},
                "style_dimensions": {"entry_mode": {"question_first": 1.0}},
            },
            "D": {
                "profile_patch": {"preferred_explanation": "exam_point_first"},
                "style_dimensions": {"entry_mode": {"exam_point_first": 1.0}},
            },
        },
    },
    {
        "question_id": "LA_Q04_EXPLANATION_DEPTH",
        "prompt": "你更喜欢哪种讲解方式？",
        "options": [
            {"id": "A", "label": "简洁一点，重点清楚就行"},
            {"id": "B", "label": "要讲明白机制和原理，我才安心"},
            {"id": "C", "label": "先讲重点，再根据需要展开"},
            {"id": "D", "label": "最好能结合病例来解释"},
        ],
        "dimension_mapping": {
            "A": {
                "style_dimensions": {"explanation_depth": {"concise": 1.0}},
            },
            "B": {
                "style_dimensions": {
                    "explanation_depth": {"mechanism_deep": 1.0},
                    "interaction_tendency": {"asks_why": 0.7},
                },
            },
            "C": {
                "style_dimensions": {"explanation_depth": {"standard": 1.0}},
            },
            "D": {
                "style_dimensions": {
                    "entry_mode": {"case_first": 0.5},
                    "explanation_depth": {"standard": 0.5},
                },
            },
        },
    },
    {
        "question_id": "LA_Q05_PACE",
        "prompt": "你更适合哪种学习节奏？",
        "options": [
            {"id": "A", "label": "慢一点，一次学少一点但更扎实"},
            {"id": "B", "label": "中等节奏，正常推进"},
            {"id": "C", "label": "快一点，只要别太啰嗦"},
            {"id": "D", "label": "节奏可以快，但遇到难点要放慢"},
        ],
        "dimension_mapping": {
            "A": {
                "profile_patch": {"pace": "slow"},
                "style_dimensions": {"pace": {"slow": 1.0}},
            },
            "B": {
                "profile_patch": {"pace": "standard"},
                "style_dimensions": {"pace": {"standard": 1.0}},
            },
            "C": {
                "profile_patch": {"pace": "fast"},
                "style_dimensions": {"pace": {"fast": 1.0}},
            },
            "D": {
                "profile_patch": {"pace": "standard"},
                "style_dimensions": {
                    "pace": {"standard": 0.5},
                    "planning_support_need": {"normal": 0.5},
                },
            },
        },
    },
    {
        "question_id": "LA_Q06_ASSESSMENT_PREFERENCE",
        "prompt": "你对学习过程中穿插小测的接受度如何？",
        "options": [
            {"id": "A", "label": "可以经常测，学完一小节就测最好"},
            {"id": "B", "label": "偶尔测就行，不要太频繁"},
            {"id": "C", "label": "少测一点，尽量别总打断我"},
            {"id": "D", "label": "我更喜欢学完一段再统一测"},
        ],
        "dimension_mapping": {
            "A": {
                "profile_patch": {"assessment_preference": "frequent"},
                "style_dimensions": {"interaction_tendency": {"asks_exam_points": 0.6}},
            },
            "B": {"profile_patch": {"assessment_preference": "moderate"}},
            "C": {"profile_patch": {"assessment_preference": "light"}},
            "D": {
                "profile_patch": {"assessment_preference": "light"},
                "style_dimensions": {"interaction_tendency": {"passive": 0.4}},
            },
        },
    },
    {
        "question_id": "LA_Q07_PLANNING_SUPPORT",
        "prompt": "你希望系统在学习计划上提供多强的帮助？",
        "options": [
            {"id": "A", "label": "希望系统帮我安排得很明确，我按计划走"},
            {"id": "B", "label": "可以给我推荐，但我想自己再调整"},
            {"id": "C", "label": "给我一个大方向就行，我自己安排"},
            {"id": "D", "label": "我先随便学学，之后再慢慢调整"},
        ],
        "dimension_mapping": {
            "A": {
                "profile_patch": {"planning_support_need": "high"},
                "style_dimensions": {"planning_support_need": {"high": 1.0}},
            },
            "B": {
                "profile_patch": {"planning_support_need": "normal"},
                "style_dimensions": {"planning_support_need": {"normal": 1.0}},
            },
            "C": {
                "profile_patch": {"planning_support_need": "low"},
                "style_dimensions": {"planning_support_need": {"low": 1.0}},
            },
            "D": {
                "profile_patch": {"planning_support_need": "high"},
                "style_dimensions": {"planning_support_need": {"high": 0.5}},
            },
        },
    },
    {
        "question_id": "LA_Q08_PRIORITY_MODE",
        "prompt": "如果时间有限，你更希望系统优先帮你什么？",
        "options": [
            {"id": "A", "label": "快速抓住考试重点"},
            {"id": "B", "label": "理清知识结构和逻辑"},
            {"id": "C", "label": "结合病例理解怎么应用"},
            {"id": "D", "label": "先补我最薄弱的地方"},
        ],
        "dimension_mapping": {
            "A": {
                "profile_patch": {"goal": "exam_prep"},
                "style_dimensions": {"entry_mode": {"exam_point_first": 0.5}},
            },
            "B": {
                "profile_patch": {"goal": "course_learning"},
                "style_dimensions": {"explanation_depth": {"mechanism_deep": 0.5}},
            },
            "C": {
                "profile_patch": {"goal": "clinical_application"},
                "style_dimensions": {"entry_mode": {"case_first": 0.5}},
            },
            "D": {
                "profile_patch": {"goal": "gap_repair"},
                "style_dimensions": {"planning_support_need": {"high": 0.5}},
            },
        },
    },
    {
        "question_id": "LA_Q09_AUTO_ADJUST",
        "prompt": "你希望系统根据你的学习表现自动调整计划吗？",
        "options": [
            {"id": "A", "label": "希望，自动调整越多越好"},
            {"id": "B", "label": "可以调整，但最好先给我看推荐"},
            {"id": "C", "label": "可以少量调整，别改太多"},
            {"id": "D", "label": "不希望自动改，我更想自己控制"},
        ],
        "dimension_mapping": {
            "A": {"style_dimensions": {"planning_support_need": {"high": 1.0}}},
            "B": {"style_dimensions": {"planning_support_need": {"normal": 1.0}}},
            "C": {"style_dimensions": {"planning_support_need": {"low": 0.5}}},
            "D": {"style_dimensions": {"planning_support_need": {"low": 1.0}}},
        },
    },
]

STYLE_DIMENSION_KEYS: dict[str, tuple[str, ...]] = {
    "explanation_depth": ("concise", "standard", "mechanism_deep"),
    "entry_mode": ("concept_first", "case_first", "question_first", "exam_point_first"),
    "interaction_tendency": ("asks_why", "asks_examples", "asks_exam_points", "passive"),
    "pace": ("slow", "standard", "fast"),
    "planning_support_need": ("low", "normal", "high"),
}


def get_learner_assessment_definition() -> dict[str, Any]:
    """Return the stable onboarding assessment definition."""
    return {
        "schema_version": "v1",
        "instrument_name": INSTRUMENT_NAME,
        "instrument_version": INSTRUMENT_VERSION,
        "question_count": QUESTION_COUNT,
        "skip_allowed": True,
        "retake_allowed": True,
        "questions": QUESTION_BANK,
    }


def submit_learner_assessment(
    memory: LearningMemory,
    *,
    user_id: str,
    answers: list[dict[str, Any]],
    target_exam: str | None = None,
    skip_assessment: bool = False,
    instrument_name: str = INSTRUMENT_NAME,
    instrument_version: str = INSTRUMENT_VERSION,
) -> dict[str, Any]:
    """Score the onboarding assessment, persist the initial profile, and return the result."""
    if instrument_name != INSTRUMENT_NAME:
        raise ValueError(f"unsupported instrument_name: {instrument_name}")
    if instrument_version != INSTRUMENT_VERSION:
        raise ValueError(f"unsupported instrument_version: {instrument_version}")

    stored_existing = memory.get_profile(user_id)
    state = memory.learner_assessment_state(user_id, profile=stored_existing)
    if state.get("needs_initial_assessment"):
        memory.ensure_user(user_id)
        existing = copy.deepcopy(DEFAULT_PROFILE)
    else:
        existing = memory.get_or_create_profile(user_id)
    if skip_assessment:
        patch = {
            "assessment_skipped": True,
            "assessment_source": f"{instrument_name}:{instrument_version}",
            "last_assessed_at": _now_iso(),
            "confidence": max(float(existing.get("confidence", 0.2)), 0.2),
        }
        updated = memory.save_profile(user_id, merge_profile(existing, patch))
        return {
            "schema_version": "v1",
            "instrument_name": instrument_name,
            "instrument_version": instrument_version,
            "profile_before": existing,
            "profile_patch": patch,
            "profile_after": updated,
            "style_dimensions": {},
            "learner_type": str(updated.get("learner_type") or ""),
            "learner_type_summary": str(updated.get("learner_type_summary") or ""),
            "score_breakdown": _empty_score_breakdown(),
            "assessment_trace": {
                "mode": "deterministic_skip",
                "question_count": 0,
                "assessment_skipped": True,
            },
        }

    selected = _validate_answers(answers)
    score_breakdown = _empty_score_breakdown()
    style_scores = _empty_style_scores()
    profile_patch: dict[str, Any] = {
        "assessment_skipped": False,
        "assessment_source": f"{instrument_name}:{instrument_version}",
        "last_assessed_at": _now_iso(),
        "confidence": 0.72,
    }

    for question in QUESTION_BANK:
        qid = question["question_id"]
        option = selected[qid]
        mapping = (question.get("dimension_mapping") or {}).get(option, {})
        for key, value in (mapping.get("profile_patch") or {}).items():
            profile_patch[key] = value
        for dimension, weighted_options in (mapping.get("style_dimensions") or {}).items():
            if dimension not in style_scores:
                continue
            breakdown_key = f"{dimension}_score"
            for option_name, delta in weighted_options.items():
                if option_name not in style_scores[dimension]:
                    continue
                style_scores[dimension][option_name] += float(delta)
                if breakdown_key in score_breakdown and option_name in score_breakdown[breakdown_key]:
                    score_breakdown[breakdown_key][option_name] += float(delta)

    if target_exam is not None:
        profile_patch["target_exam"] = target_exam.strip() or None

    profile_patch.setdefault("goal", existing.get("goal", DEFAULT_PROFILE["goal"]))
    profile_patch.setdefault("daily_available_minutes", existing.get("daily_available_minutes", DEFAULT_PROFILE["daily_available_minutes"]))
    profile_patch.setdefault("preferred_explanation", _derive_preferred_explanation(style_scores, existing))
    profile_patch.setdefault("pace", _dominant_style(style_scores, "pace") or existing.get("pace", DEFAULT_PROFILE["pace"]))
    profile_patch.setdefault("assessment_preference", _derive_assessment_preference(selected))
    profile_patch.setdefault("planning_support_need", _dominant_style(style_scores, "planning_support_need") or existing.get("planning_support_need", DEFAULT_PROFILE["planning_support_need"]))

    style_dimensions = _build_style_dimensions(style_scores)
    learner_type, learner_type_summary = _derive_learner_type(
        profile_patch=profile_patch,
        style_dimensions=style_dimensions,
    )
    profile_patch["learner_type"] = learner_type
    profile_patch["learner_type_summary"] = learner_type_summary

    profile_after = _merge_assessment_profile(existing, profile_patch)
    profile_after = memory.save_profile(user_id, profile_after)

    return {
        "schema_version": "v1",
        "instrument_name": instrument_name,
        "instrument_version": instrument_version,
        "profile_before": existing,
        "profile_patch": profile_patch,
        "profile_after": profile_after,
        "style_dimensions": style_dimensions,
        "learner_type": learner_type,
        "learner_type_summary": learner_type_summary,
        "score_breakdown": score_breakdown,
        "assessment_trace": {
            "mode": "deterministic_question_mapping",
            "question_count": QUESTION_COUNT,
            "assessment_skipped": False,
            "answers": answers,
        },
    }


def _empty_score_breakdown() -> dict[str, dict[str, float]]:
    return {
        "explanation_depth_score": {"concise": 0.0, "standard": 0.0, "mechanism_deep": 0.0},
        "entry_mode_score": {"concept_first": 0.0, "case_first": 0.0, "question_first": 0.0, "exam_point_first": 0.0},
        "pace_score": {"slow": 0.0, "standard": 0.0, "fast": 0.0},
        "assessment_preference_score": {"frequent": 0.0, "moderate": 0.0, "light": 0.0},
        "planning_support_need_score": {"low": 0.0, "normal": 0.0, "high": 0.0},
    }


def _empty_style_scores() -> dict[str, dict[str, float]]:
    return {dim: {opt: 0.0 for opt in options} for dim, options in STYLE_DIMENSION_KEYS.items()}


def _validate_answers(answers: list[dict[str, Any]]) -> dict[str, str]:
    if not isinstance(answers, list):
        raise ValueError("answers must be a list")
    answer_map: dict[str, str] = {}
    valid_question_ids = {item["question_id"] for item in QUESTION_BANK}
    for item in answers:
        if not isinstance(item, dict):
            raise ValueError("each answer must be an object")
        question_id = str(item.get("question_id") or "").strip()
        selected_option = str(item.get("selected_option") or "").strip().upper()
        if question_id not in valid_question_ids:
            raise ValueError(f"unknown question_id: {question_id!r}")
        valid_options = {opt["id"] for q in QUESTION_BANK if q["question_id"] == question_id for opt in q["options"]}
        if selected_option not in valid_options:
            raise ValueError(f"invalid option {selected_option!r} for question {question_id}")
        answer_map[question_id] = selected_option
    missing = [q["question_id"] for q in QUESTION_BANK if q["question_id"] not in answer_map]
    if missing:
        raise ValueError(f"missing answers for: {', '.join(missing)}")
    return answer_map


def _dominant_style(style_scores: dict[str, dict[str, float]], dimension: str) -> str:
    options = style_scores.get(dimension) or {}
    if not options:
        return ""
    ordered = sorted(options.items(), key=lambda item: item[1], reverse=True)
    if not ordered or ordered[0][1] <= 0:
        return ""
    return ordered[0][0]


def _derive_preferred_explanation(style_scores: dict[str, dict[str, float]], existing: dict[str, Any]) -> str:
    entry_mode = _dominant_style(style_scores, "entry_mode")
    if entry_mode == "case_first":
        return "case_first"
    if entry_mode == "question_first":
        return "practice_first"
    if entry_mode == "exam_point_first":
        return "exam_point_first"
    if entry_mode == "concept_first":
        return "concept_then_case"
    return str(existing.get("preferred_explanation", DEFAULT_PROFILE["preferred_explanation"]))


def _derive_assessment_preference(selected: dict[str, str]) -> str:
    option = selected.get("LA_Q06_ASSESSMENT_PREFERENCE", "B")
    if option == "A":
        return "frequent"
    if option == "B":
        return "moderate"
    return "light"


def _build_style_dimensions(style_scores: dict[str, dict[str, float]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for dimension, options in style_scores.items():
        ordered = sorted(options.items(), key=lambda item: item[1], reverse=True)
        top_option, top_score = ordered[0]
        total_score = sum(max(value, 0.0) for value in options.values())
        if top_score <= 0:
            continue
        confidence = round(min(0.95, 0.4 + (top_score / max(total_score, 1.0)) * 0.45), 3)
        result[dimension] = {
            "dominant": top_option,
            "status": "observed" if confidence < 0.7 else "stable",
            "confidence": confidence,
            "options": {name: round(score, 3) for name, score in options.items()},
        }
    return result


def _derive_learner_type(*, profile_patch: dict[str, Any], style_dimensions: dict[str, Any]) -> tuple[str, str]:
    goal = str(profile_patch.get("goal") or "")
    preferred = str(profile_patch.get("preferred_explanation") or "")
    pace = str(profile_patch.get("pace") or "")
    assessment_pref = str(profile_patch.get("assessment_preference") or "")
    entry_mode = (style_dimensions.get("entry_mode") or {}).get("dominant", "")
    depth = (style_dimensions.get("explanation_depth") or {}).get("dominant", "")
    planning_support = (style_dimensions.get("planning_support_need") or {}).get("dominant", "")

    if goal == "exam_prep" and (assessment_pref == "frequent" or entry_mode == "exam_point_first"):
        return "exam_oriented_learner", "你更偏向考试导向学习，适合在重点归纳和练习反馈中推进。"
    if preferred == "practice_first" or entry_mode == "question_first" or assessment_pref == "frequent":
        return "practice_driven_learner", "你更适合边做边学，通过问题驱动来建立理解。"
    if preferred == "case_first" or entry_mode == "case_first" or goal == "clinical_application":
        return "case_driven_learner", "你更适合从病例或临床情境进入，再回到概念理解。"
    if depth == "mechanism_deep":
        return "text_depth_learner", "你更偏好机制和原理层面的深入拆解。"
    if pace == "slow" or planning_support == "high":
        return "steady_paced_learner", "你更适合稳定推进、少量多次、循序渐进的学习节奏。"
    return "visual_structured_learner", "你更适合结构清晰、分层明确的学习组织方式。"


def _merge_assessment_profile(existing: dict[str, Any], profile_patch: dict[str, Any]) -> dict[str, Any]:
    preserved = {
        key: existing.get(key, DEFAULT_PROFILE.get(key))
        for key in (
            "known_weak_points",
            "mistake_patterns",
            "mastery_by_kg_node",
            "plan_adherence",
            "review_need",
        )
    }
    merged = merge_profile(existing, {**preserved, **profile_patch})
    for key, value in preserved.items():
        if key not in merged:
            merged[key] = value
    return merged
