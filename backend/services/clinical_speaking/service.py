"""Standalone clinical speaking workflow, isolated from the master router."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import uuid
import weakref
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from backend.services.avatar_service import get_public_avatar_config
from backend.services.learning.memory import LearningMemory
from backend.services.retrieval import search_knowledge_base
from backend.services.xfyun_iat import get_public_iat_config

from .agents import (
    StrictAgentError,
    agent_readiness,
    assess_exam_answer,
    build_judge_blueprint,
    normalize_medical_transcript,
    patient_opening,
    patient_reply,
    select_case_target,
    validate_judge_blueprint,
)
from .case_repository import DIAGNOSIS_ALIASES, ClinicalCaseRepository
from .store import ClinicalSpeakingStore


MAX_EVIDENCE_ITEMS = 6
MAX_EVIDENCE_TEXT_CHARS = 650
MAX_EVIDENCE_TOTAL_CHARS = 3600
EVIDENCE_DOMAIN_QUERIES = (
    ("pathogenesis", "病因 病理机制 发病机制"),
    ("clinical_features", "临床表现 症状 检查 诊断依据"),
    ("differential", "鉴别诊断 诊断区别"),
    ("treatment", "治疗原则 治疗方法 处理"),
    ("risk_followup", "并发症 风险 预后 随访"),
)
EVIDENCE_KNOWLEDGE_ALIASES: dict[str, tuple[str, ...]] = {
    "急性冠周炎": ("智齿冠周炎",),
    "舌系带过短": ("舌系带矫正术", "舌系带修整术"),
    "额外牙［多生牙］": ("额外牙", "多生牙"),
    "慢性根尖牙周炎": ("慢性根尖周炎",),
    "牙折裂": ("牙齿折裂", "冠根折裂"),
}
EVIDENCE_METADATA_KEYS = (
    "kg_node_id",
    "core_id",
    "node_id",
    "kg_node_ids",
    "core_ids",
    "node_ids",
)
BLUEPRINT_SCHEMA_VERSION = "judge-blueprint-v4"
MAX_PROCESSED_REQUESTS = 80
REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}")

logger = logging.getLogger(__name__)


def _compact_evidence_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key in EVIDENCE_METADATA_KEYS:
        value = metadata.get(key)
        if isinstance(value, list):
            values = [str(item).strip()[:120] for item in value if str(item).strip()][:4]
            if values:
                compact[key] = values
        elif value is not None and str(value).strip():
            compact[key] = str(value).strip()[:120]
    return compact


def _diagnosis_anchors(diagnosis: str) -> list[str]:
    candidates = [diagnosis, *DIAGNOSIS_ALIASES.get(diagnosis, ())]
    anchors: list[str] = []
    for candidate in candidates:
        compact = re.sub(r"\s+", "", str(candidate or ""))
        for value in (compact, re.sub(r"^(?:急性|慢性)", "", compact)):
            if len(value) >= 2 and value not in anchors:
                anchors.append(value)
        for value in re.findall(r"[\u4e00-\u9fff]{2,}", compact):
            if value not in anchors:
                anchors.append(value)
    return anchors


def _evidence_excerpt(
    text: str,
    anchors: list[str],
    limit: int,
    *,
    focus_anchors: list[str] | None = None,
) -> str:
    if len(text) <= limit:
        return text
    focus_positions = [
        text.find(anchor)
        for anchor in (focus_anchors or [])
        if text.find(anchor) >= 0
    ]
    positions = focus_positions or [
        text.find(anchor) for anchor in anchors if text.find(anchor) >= 0
    ]
    if not positions:
        return text[:limit]
    match_position = min(positions)
    start = max(0, match_position - 80)
    start = min(start, len(text) - limit)
    return text[start : start + limit]


class ClinicalSpeakingError(RuntimeError):
    def __init__(self, message: str, *, code: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class ClinicalSpeakingService:
    def __init__(
        self,
        *,
        memory: LearningMemory | None = None,
        repository: ClinicalCaseRepository | None = None,
        store: ClinicalSpeakingStore | None = None,
        retrieval_fn: Callable[..., list[Any]] = search_knowledge_base,
    ) -> None:
        self.memory = memory or LearningMemory()
        self.repository = repository or ClinicalCaseRepository()
        self.store = store or ClinicalSpeakingStore()
        self.retrieval_fn = retrieval_fn
        self._operation_locks: weakref.WeakValueDictionary[str, threading.RLock] = (
            weakref.WeakValueDictionary()
        )
        self._operation_locks_guard = threading.Lock()

    @contextmanager
    def _operation_lock(self, key: str):
        with self._operation_locks_guard:
            lock = self._operation_locks.setdefault(key, threading.RLock())
        with lock:
            yield

    @staticmethod
    def _normalize_request_id(request_id: str | None) -> str | None:
        if request_id is None:
            return None
        value = str(request_id).strip()
        if not REQUEST_ID_PATTERN.fullmatch(value):
            raise ClinicalSpeakingError(
                "请求标识格式无效。",
                code="invalid_request_id",
                status_code=422,
            )
        return value

    @staticmethod
    def _processed_request(
        state: dict[str, Any],
        request_id: str | None,
        operation: str,
    ) -> dict[str, Any] | None:
        if not request_id:
            return None
        entry = dict(state.get("processed_requests", {}).get(request_id) or {})
        if not entry:
            return None
        if entry.get("operation") != operation:
            raise ClinicalSpeakingError(
                "同一个请求标识不能用于不同操作。",
                code="request_id_conflict",
                status_code=409,
            )
        return entry

    @staticmethod
    def _record_processed_request(
        state: dict[str, Any],
        request_id: str | None,
        operation: str,
        **details: Any,
    ) -> None:
        if not request_id:
            return
        processed = dict(state.get("processed_requests") or {})
        processed[request_id] = {"operation": operation, **details}
        while len(processed) > MAX_PROCESSED_REQUESTS:
            processed.pop(next(iter(processed)))
        state["processed_requests"] = processed

    def _record_learning_event_once(
        self,
        state: dict[str, Any],
        event_type: str,
        event_key: str,
        payload: dict[str, Any],
        *,
        legacy_match: dict[str, Any],
    ) -> None:
        existing = self.memory.list_events(
            state["user_id"],
            session_id=state["learning_session_id"],
            event_types=[event_type],
        )
        for event in existing:
            stored_payload = event.get("payload") or {}
            if stored_payload.get("clinical_event_key") == event_key:
                return
            if all(stored_payload.get(key) == value for key, value in legacy_match.items()):
                return
        digest = hashlib.sha256(
            f"{state['id']}:{event_key}".encode("utf-8")
        ).hexdigest()[:24]
        self.memory.record_event_once(
            f"evt_{digest}",
            state["learning_session_id"],
            state["user_id"],
            event_type,
            None,
            {
                **payload,
                "clinical_session_id": state["id"],
                "clinical_event_key": event_key,
            },
        )

    def _sync_started_event(self, state: dict[str, Any]) -> None:
        self._record_learning_event_once(
            state,
            "clinical_speaking_started",
            "started",
            {
                "surface": "clinical_speaking",
                "case_ref": state["case"]["case_ref"],
                "selection_source": state["selection"]["source"],
                "matched_target": state["selection"].get("matched_target", ""),
                "fallback_used": False,
            },
            legacy_match={"case_ref": state["case"]["case_ref"]},
        )

    def _sync_diagnosis_event(self, state: dict[str, Any]) -> None:
        self._record_learning_event_once(
            state,
            "clinical_diagnosis_confirmed",
            "diagnosis_confirmed",
            {
                "surface": "clinical_speaking",
                "diagnosis": state["case"]["diagnosis"],
                "case_ref": state["case"]["case_ref"],
                "fallback_used": False,
            },
            legacy_match={"case_ref": state["case"]["case_ref"]},
        )

    def _sync_completed_event(self, state: dict[str, Any]) -> None:
        learning_session = self.memory.get_session(state["learning_session_id"])
        if learning_session["status"] != "completed":
            self.memory.set_session_status(state["learning_session_id"], "completed")
        self._record_learning_event_once(
            state,
            "clinical_speaking_completed",
            "completed",
            {
                "surface": "clinical_speaking",
                "diagnosis": state["case"]["diagnosis"],
                "score": state["completed_score"],
                "max_score": 100,
                "fallback_used": False,
            },
            legacy_match={
                "diagnosis": state["case"]["diagnosis"],
                "score": state["completed_score"],
            },
        )

    def readiness(self) -> dict[str, Any]:
        spark = agent_readiness(verify=True)
        iat = get_public_iat_config()
        avatar = get_public_avatar_config()
        case_count = self.repository.count()
        checks = {
            "spark_agents": spark["configured"] and spark.get("authorized", True),
            "xfyun_iat": iat["configured"],
            "xfyun_avatar": bool(avatar.get("configured")),
            "clinical_cases": case_count > 0,
        }
        missing: list[str] = []
        if not checks["spark_agents"]:
            missing.extend(spark.get("missing", []))
        if not checks["xfyun_iat"]:
            missing.extend(iat.get("missing", []))
        if not checks["xfyun_avatar"]:
            missing.extend(avatar.get("missing", []))
        if not checks["clinical_cases"]:
            missing.append(str(self.repository.path))
        iat_status = {key: value for key, value in iat.items() if key != "signed_url"}
        avatar_status = {key: value for key, value in avatar.items() if key != "signed_url"}
        return {
            "ready": all(checks.values()),
            "checks": checks,
            "missing": list(dict.fromkeys(missing)),
            "case_count": case_count,
            "diagnoses": self.repository.diagnoses(),
            "providers": {"agents": spark, "iat": iat_status, "avatar": avatar_status},
            "fallback_used": False,
        }

    def _require_ready(self) -> None:
        result = self.readiness()
        if not result["ready"]:
            raise ClinicalSpeakingError(
                "临床问诊训练依赖未就绪：" + "、".join(result["missing"]),
                code="clinical_speaking_not_ready",
                status_code=503,
            )

    def _learner_targets(self, user_id: str) -> list[str]:
        profile = self.memory.get_or_create_profile(user_id)
        targets = [str(item) for item in profile.get("known_weak_points", []) if str(item).strip()]
        for session in self.memory.list_sessions_for_user(user_id, limit=8):
            if session.get("mode") == "clinical_speaking":
                continue
            topic = str(session.get("topic") or "").strip()
            if topic and topic not in targets:
                targets.append(topic)
        return targets

    def _retrieve_evidence(self, diagnosis: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        knowledge_anchors = [
            re.sub(r"\s+", "", value)
            for value in EVIDENCE_KNOWLEDGE_ALIASES.get(diagnosis, ())
            if value.strip()
        ]
        anchors = list(dict.fromkeys([*_diagnosis_anchors(diagnosis), *knowledge_anchors]))
        strict_anchors = list(
            dict.fromkeys(
                [
                    re.sub(r"\s+", "", value)
                    for value in (
                        diagnosis,
                        *DIAGNOSIS_ALIASES.get(diagnosis, ()),
                        *EVIDENCE_KNOWLEDGE_ALIASES.get(diagnosis, ()),
                    )
                    if str(value).strip()
                ]
            )
        )

        def retrieve(query: str, top_k: int) -> list[Any]:
            try:
                chunks = self.retrieval_fn(
                    query,
                    top_k=top_k,
                    task_type="clinical_speaking_judge",
                )
            except Exception as exc:  # retrieval failure must stop the workflow
                raise ClinicalSpeakingError(
                    f"知识库检索失败：{exc}", code="judge_evidence_failed", status_code=503
                ) from exc
            filtered = [
                chunk
                for chunk in chunks
                if str(getattr(chunk, "source_type", "")) != "clinical_case"
                and any(
                    anchor in " ".join(
                        (
                            str(getattr(chunk, "source", "") or ""),
                            str(getattr(chunk, "title", "") or ""),
                            str(getattr(chunk, "text", "") or ""),
                        )
                    )
                    for anchor in strict_anchors
                )
            ]
            query_terms = [
                term
                for term in re.split(r"\s+", query)
                if len(term) >= 2 and term != diagnosis and term not in anchors
            ]

            def relevance_key(chunk: Any) -> tuple[int, int, int, int]:
                metadata = dict(getattr(chunk, "metadata", {}) or {})
                heading = " ".join(
                    [
                        str(getattr(chunk, "title", "") or ""),
                        *[str(item) for item in (metadata.get("heading_path") or [])],
                    ]
                )
                text = str(getattr(chunk, "text", "") or "")
                primary_anchors = [diagnosis, *knowledge_anchors]
                heading_positions = [
                    heading.find(anchor)
                    for anchor in primary_anchors
                    if heading.find(anchor) >= 0
                ]
                text_positions = [
                    text.find(anchor)
                    for anchor in primary_anchors
                    if text.find(anchor) >= 0
                ]
                if heading_positions:
                    centrality = (0, min(heading_positions))
                elif text_positions:
                    centrality = (1, min(text_positions))
                else:
                    alias_positions = [
                        text.find(anchor)
                        for anchor in anchors
                        if text.find(anchor) >= 0
                    ]
                    centrality = (2, min(alias_positions) if alias_positions else 999999)
                source_priority = int(
                    str(getattr(chunk, "source_type", ""))
                    not in {"textbook", "clinical_norm", "clinical_norms"}
                )
                domain_priority = int(
                    bool(query_terms)
                    and not any(term in f"{heading} {text}" for term in query_terms)
                )
                return domain_priority, centrality[0], centrality[1], source_priority

            filtered.sort(key=relevance_key)
            return filtered

        def chunk_key(chunk: Any) -> tuple[str, str, str]:
            chunk_id = str(getattr(chunk, "id", "") or "").strip()
            if chunk_id:
                return ("id", chunk_id, "")
            return (
                str(getattr(chunk, "source", "") or ""),
                str(getattr(chunk, "title", "") or ""),
                " ".join(str(getattr(chunk, "text", "") or "").split())[:240],
            )

        base_chunks = retrieve(diagnosis, 24)
        if len(base_chunks) < 3:
            raise ClinicalSpeakingError(
                f"知识库没有返回足够的“{diagnosis}”教材或规范证据，训练未启动。",
                code="judge_evidence_insufficient",
                status_code=503,
            )
        domain_chunks: dict[str, list[Any]] = {}
        for category, terms in EVIDENCE_DOMAIN_QUERIES:
            scoped = retrieve(f"{diagnosis} {terms}", 16)
            domain_chunks[category] = scoped or base_chunks

        selected: list[tuple[Any, list[str], list[str]]] = []
        selected_keys: set[tuple[str, str, str]] = set()
        for category, terms in EVIDENCE_DOMAIN_QUERIES:
            candidates = domain_chunks[category]
            chosen = candidates[0]
            key = chunk_key(chosen)
            focus_terms = [term for term in re.split(r"\s+", terms) if len(term) >= 2]
            selected.append((chosen, [category], focus_terms))
            selected_keys.add(key)
        for chunk in base_chunks:
            if len(selected) >= MAX_EVIDENCE_ITEMS:
                break
            key = chunk_key(chunk)
            if key not in selected_keys:
                selected.append((chunk, [], []))
                selected_keys.add(key)

        private: list[dict[str, Any]] = []
        public: list[dict[str, Any]] = []
        evidence_chars = 0
        for chunk, domains, focus_terms in selected:
            if len(private) >= MAX_EVIDENCE_ITEMS or evidence_chars >= MAX_EVIDENCE_TOTAL_CHARS:
                break
            text = " ".join(str(getattr(chunk, "text", "") or "").split())
            if not text:
                continue
            remaining = MAX_EVIDENCE_TOTAL_CHARS - evidence_chars
            text = _evidence_excerpt(
                text,
                anchors,
                min(MAX_EVIDENCE_TEXT_CHARS, remaining),
                focus_anchors=focus_terms,
            )
            evidence_chars += len(text)
            index = len(private) + 1
            metadata = _compact_evidence_metadata(dict(getattr(chunk, "metadata", {}) or {}))
            item = {
                "index": index,
                "source": str(getattr(chunk, "source", "") or "口腔医学知识库")[:120],
                "title": str(getattr(chunk, "title", "") or "知识库证据")[:180],
                "text": text,
                "metadata": metadata,
                "domains": domains,
            }
            private.append(item)
            public.append(
                {
                    "index": index,
                    "source": item["source"],
                    "title": item["title"],
                    "excerpt": item["text"][:220],
                }
            )
        if len(private) < 3:
            raise ClinicalSpeakingError(
                "知识库没有返回足够的有效教材或规范证据，训练未启动。",
                code="judge_evidence_insufficient",
                status_code=503,
            )
        return private, public

    @staticmethod
    def _term_catalog(case: dict[str, Any], blueprint: dict[str, Any]) -> list[str]:
        terms = list(case.get("diagnosis_aliases", []))
        for question in blueprint.get("questions", []):
            terms.extend(question.get("rubric_points", []))
        normalized = [" ".join(str(term).split())[:80] for term in terms if str(term).strip()]
        return list(dict.fromkeys(normalized))[:50]

    @staticmethod
    def _prepare_transcript(
        transcript: str,
        terms: list[str],
        input_mode: str,
    ) -> dict[str, Any]:
        raw = " ".join(str(transcript or "").split())
        if not raw:
            raise StrictAgentError(
                "输入内容为空。",
                code="empty_transcript",
                status_code=422,
            )
        if input_mode == "speech":
            return normalize_medical_transcript(raw, terms)
        return {
            "raw_text": raw,
            "normalized_text": raw,
            "corrections": [],
            "trace": {
                "role": "typed_input",
                "provider": "browser_text",
                "fallback_used": False,
            },
        }

    def _get_or_build_blueprint(
        self,
        diagnosis: str,
        evidence: list[dict[str, Any]],
    ) -> dict[str, Any]:
        signature_payload = {
            "schema": BLUEPRINT_SCHEMA_VERSION,
            "diagnosis": diagnosis,
            "evidence": [
                {
                    "index": item.get("index"),
                    "source": item.get("source"),
                    "title": item.get("title"),
                    "text": item.get("text"),
                    "domains": item.get("domains", []),
                }
                for item in evidence
            ],
        }
        signature = hashlib.sha256(
            json.dumps(signature_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        cached = self.store.get_blueprint(diagnosis, signature)
        if cached is not None:
            cached = json.loads(json.dumps(cached, ensure_ascii=False))
            try:
                validate_judge_blueprint(cached, evidence)
            except (ValueError, TypeError, KeyError) as exc:
                logger.warning("Discarding invalid cached judge blueprint for %s: %s", diagnosis, exc)
            else:
                cached["trace"] = {**cached.get("trace", {}), "cache_hit": True}
                return cached
        blueprint = build_judge_blueprint(diagnosis, evidence)
        validate_judge_blueprint(blueprint, evidence)
        blueprint["trace"] = {**blueprint.get("trace", {}), "cache_hit": False}
        self.store.save_blueprint(diagnosis, signature, blueprint)
        return blueprint

    def start_session(self, user_id: str, request_id: str | None = None) -> dict[str, Any]:
        request_id = self._normalize_request_id(request_id)
        if request_id:
            with self._operation_lock(f"start:{user_id}:{request_id}"):
                return self._start_session_locked(user_id, request_id)
        return self._start_session_locked(user_id, None)

    def _start_session_locked(self, user_id: str, request_id: str | None) -> dict[str, Any]:
        session_id = (
            "cs_"
            + hashlib.sha256(f"{user_id}:{request_id}".encode("utf-8")).hexdigest()[:16]
            if request_id
            else f"cs_{uuid.uuid4().hex[:16]}"
        )
        existing = self.store.get(session_id)
        if existing is not None:
            if existing.get("user_id") != user_id:
                raise ClinicalSpeakingError(
                    "请求标识与已有会话冲突。",
                    code="request_id_conflict",
                    status_code=409,
                )
            try:
                self._validate_session_state(existing)
            except (ValueError, TypeError, KeyError) as exc:
                raise ClinicalSpeakingError(
                    "重复启动请求对应的会话状态不完整。",
                    code="session_state_invalid",
                    status_code=500,
                ) from exc
            self._sync_started_event(existing)
            return self.public_session(existing)
        self._require_ready()
        targets = self._learner_targets(user_id)
        try:
            selection = select_case_target(targets, self.repository.diagnoses())
            case, selection_source, matched_target = self.repository.select(
                [],
                preferred_diagnosis=selection["diagnosis"],
                matched_target=selection["matched_target"],
            )
            evidence, public_evidence = self._retrieve_evidence(case["diagnosis"])
            blueprint = self._get_or_build_blueprint(case["diagnosis"], evidence)
            opening = patient_opening(case)
        except StrictAgentError as exc:
            raise ClinicalSpeakingError(str(exc), code=exc.code, status_code=exc.status_code) from exc
        except LookupError as exc:
            raise ClinicalSpeakingError(str(exc), code="case_unavailable", status_code=503) from exc

        learning_session = self.memory.create_session(
            user_id,
            topic=case["diagnosis"],
            mode="clinical_speaking",
            session_id=(
                "lesson_"
                + hashlib.sha256(f"clinical:{session_id}".encode("utf-8")).hexdigest()[:16]
            ),
        )
        state = self.store.create(
            {
                "id": session_id,
                "user_id": user_id,
                "learning_session_id": learning_session["id"],
                "status": "consultation",
                "selection": {
                    "source": selection_source,
                    "matched_target": matched_target,
                    "learner_targets": targets[:8],
                    "reason": selection["reason"],
                    "trace": selection["trace"],
                },
                "case": case,
                "evidence": evidence,
                "public_evidence": public_evidence,
                "blueprint": blueprint,
                "turns": [
                    {
                        "role": "patient",
                        "stage": "consultation",
                        "content": opening["reply"],
                        "supported_fact_ids": opening["supported_fact_ids"],
                        "trace": opening["trace"],
                        "grounding_trace": opening["grounding"],
                    }
                ],
                "diagnosis_candidate": None,
                "question_index": 0,
                "assessments": [],
                "processed_requests": {},
                "start_request_id": request_id,
                "fallback_used": False,
            }
        )
        self._sync_started_event(state)
        return self.public_session(state)

    def _get_owned(self, session_id: str, user_id: str) -> dict[str, Any]:
        state = self.store.get(session_id)
        if state is None:
            raise ClinicalSpeakingError("训练会话不存在。", code="session_not_found", status_code=404)
        if state["user_id"] != user_id:
            raise ClinicalSpeakingError("无权访问该训练会话。", code="session_forbidden", status_code=403)
        try:
            self._validate_session_state(state)
        except (ValueError, TypeError, KeyError) as exc:
            logger.error("Invalid clinical speaking session %s: %s", session_id, exc)
            raise ClinicalSpeakingError(
                "训练会话状态不完整，无法继续。",
                code="session_state_invalid",
                status_code=500,
            ) from exc
        return state

    @staticmethod
    def _validate_session_state(state: dict[str, Any]) -> None:
        if not isinstance(state, dict):
            raise ValueError("session state must be an object")
        status = state.get("status")
        if status not in {"consultation", "diagnosis_confirmation", "oral_exam", "completed"}:
            raise ValueError("session status is invalid")
        case = state.get("case")
        evidence = state.get("evidence")
        if not isinstance(case, dict) or not str(case.get("diagnosis") or "").strip():
            raise ValueError("session case is invalid")
        if not str(state.get("learning_session_id") or "").strip():
            raise ValueError("learning_session_id is invalid")
        if not isinstance(evidence, list) or not evidence:
            raise ValueError("session evidence is invalid")
        validate_judge_blueprint(state.get("blueprint"), evidence)
        turns = state.get("turns")
        assessments = state.get("assessments")
        question_index = state.get("question_index")
        if not isinstance(turns, list) or not turns:
            raise ValueError("session turns are invalid")
        for turn in turns:
            if not isinstance(turn, dict):
                raise ValueError("session turn must be an object")
            if turn.get("role") not in {"patient", "doctor", "judge"}:
                raise ValueError("session turn role is invalid")
            if turn.get("stage") not in {"consultation", "oral_exam"}:
                raise ValueError("session turn stage is invalid")
            if not str(turn.get("content") or "").strip():
                raise ValueError("session turn content is invalid")
            if "corrections" in turn and not isinstance(turn["corrections"], list):
                raise ValueError("session turn corrections are invalid")
        if not isinstance(assessments, list):
            raise ValueError("session assessments are invalid")
        if not isinstance(question_index, int) or isinstance(question_index, bool):
            raise ValueError("question_index is invalid")
        question_count = len(state["blueprint"]["questions"])
        if not 0 <= question_index <= question_count:
            raise ValueError("question_index is out of range")
        if len(assessments) != question_index:
            raise ValueError("assessment count does not match question_index")
        expected_ids = [
            item["question_id"]
            for item in state["blueprint"]["questions"][:question_index]
        ]
        if [item.get("question_id") for item in assessments] != expected_ids:
            raise ValueError("assessment order does not match blueprint")
        evidence_ids = {
            item.get("index")
            for item in evidence
            if isinstance(item, dict)
            and isinstance(item.get("index"), int)
            and not isinstance(item.get("index"), bool)
        }
        question_by_id = {
            item["question_id"]: item for item in state["blueprint"]["questions"]
        }
        for assessment in assessments:
            if not isinstance(assessment, dict):
                raise ValueError("assessment must be an object")
            question = question_by_id[assessment["question_id"]]
            if (
                assessment.get("category") != question["category"]
                or assessment.get("label") != question["label"]
                or assessment.get("max_score") != question["max_score"]
            ):
                raise ValueError("assessment metadata does not match blueprint")
            score = assessment.get("score")
            if (
                not isinstance(score, (int, float))
                or isinstance(score, bool)
                or not 0 <= float(score) <= float(question["max_score"])
            ):
                raise ValueError("assessment score is invalid")
            if not str(assessment.get("critique") or "").strip():
                raise ValueError("assessment critique is invalid")
            for field in ("strengths", "missing_points", "errors"):
                values = assessment.get(field)
                if not isinstance(values, list) or any(
                    not isinstance(value, str) for value in values
                ):
                    raise ValueError(f"assessment {field} is invalid")
            refs = assessment.get("evidence_refs")
            allowed_refs = set(question["evidence_refs"]) & evidence_ids
            if (
                not isinstance(refs, list)
                or not refs
                or any(
                    not isinstance(ref, int)
                    or isinstance(ref, bool)
                    or ref not in allowed_refs
                    for ref in refs
                )
            ):
                raise ValueError("assessment evidence references are invalid")
            trace = assessment.get("trace")
            if (
                not isinstance(trace, dict)
                or trace.get("role") != "judge_agent"
                or trace.get("fallback_used") is not False
            ):
                raise ValueError("assessment trace is invalid")
        if status == "oral_exam" and question_index >= question_count:
            raise ValueError("oral exam has no current question")
        if status == "completed" and question_index != question_count:
            raise ValueError("completed session has unanswered questions")
        if status == "diagnosis_confirmation" and not state.get("diagnosis_candidate"):
            raise ValueError("diagnosis confirmation is missing its candidate")
        processed = state.get("processed_requests", {})
        if not isinstance(processed, dict):
            raise ValueError("processed_requests is invalid")
        question_ids = set(question_by_id)
        for request_id, entry in processed.items():
            if not isinstance(request_id, str) or not REQUEST_ID_PATTERN.fullmatch(request_id):
                raise ValueError("processed request id is invalid")
            if not isinstance(entry, dict):
                raise ValueError("processed request entry is invalid")
            operation = entry.get("operation")
            if operation == "consultation_turn":
                candidate = entry.get("diagnosis_candidate")
                patient_text = entry.get("patient_reply")
                if candidate is not None and not isinstance(candidate, dict):
                    raise ValueError("processed diagnosis candidate is invalid")
                if patient_text is not None and not isinstance(patient_text, str):
                    raise ValueError("processed patient reply is invalid")
            elif operation == "confirm_diagnosis":
                if not isinstance(entry.get("confirmed"), bool):
                    raise ValueError("processed diagnosis confirmation is invalid")
                if entry["confirmed"] and entry.get("question_id") not in question_ids:
                    raise ValueError("processed diagnosis question is invalid")
            elif operation == "exam_answer":
                if entry.get("question_id") not in question_ids:
                    raise ValueError("processed assessment question is invalid")
                next_question_id = entry.get("next_question_id")
                if next_question_id is not None and next_question_id not in question_ids:
                    raise ValueError("processed next question is invalid")
                if not isinstance(entry.get("completed"), bool):
                    raise ValueError("processed assessment completion is invalid")
                if entry["completed"] != (next_question_id is None):
                    raise ValueError("processed assessment progression is invalid")
            else:
                raise ValueError("processed request operation is invalid")
        if state.get("fallback_used") is not False:
            raise ValueError("session fallback marker is invalid")

    def submit_consultation_turn(
        self,
        session_id: str,
        user_id: str,
        transcript: str,
        request_id: str | None = None,
        input_mode: str = "speech",
    ) -> dict[str, Any]:
        request_id = self._normalize_request_id(request_id)
        with self._operation_lock(f"session:{session_id}"):
            return self._submit_consultation_turn_locked(
                session_id,
                user_id,
                transcript,
                request_id,
                input_mode,
            )

    def _submit_consultation_turn_locked(
        self,
        session_id: str,
        user_id: str,
        transcript: str,
        request_id: str | None,
        input_mode: str,
    ) -> dict[str, Any]:
        state = self._get_owned(session_id, user_id)
        replay = self._processed_request(state, request_id, "consultation_turn")
        if replay is not None:
            return {
                "session": self.public_session(state),
                "diagnosis_candidate": replay.get("diagnosis_candidate"),
                "patient_reply": replay.get("patient_reply"),
                "fallback_used": False,
            }
        if state["status"] != "consultation":
            raise ClinicalSpeakingError("当前不处于问诊阶段。", code="invalid_stage", status_code=409)
        try:
            normalized = self._prepare_transcript(
                transcript,
                self._term_catalog(state["case"], state["blueprint"]),
                input_mode,
            )
        except StrictAgentError as exc:
            raise ClinicalSpeakingError(str(exc), code=exc.code, status_code=exc.status_code) from exc
        text = normalized["normalized_text"]
        user_turn = {
            "role": "doctor",
            "stage": "consultation",
            "content": text,
            "raw_transcript": normalized["raw_text"],
            "corrections": normalized["corrections"],
            "trace": normalized["trace"],
        }
        state["turns"].append(user_turn)
        try:
            reply = patient_reply(state["case"], state["turns"], text)
        except StrictAgentError as exc:
            raise ClinicalSpeakingError(str(exc), code=exc.code, status_code=exc.status_code) from exc
        action = reply.get("action")
        if action == "diagnosis_correct":
            candidate = str(reply.get("diagnosis_candidate") or "").strip()
            if not candidate:
                raise ClinicalSpeakingError(
                    "患者 Agent 的诊断交接结果缺少诊断名称。",
                    code="patient_dialogue_contract_failed",
                    status_code=502,
                )
            user_turn["dialogue_trace"] = reply["trace"]
            user_turn["diagnosis_verification_trace"] = reply["diagnosis_verification"]
            state["status"] = "diagnosis_confirmation"
            state["diagnosis_candidate"] = {
                "display_name": state["case"]["diagnosis"],
                "matched_alias": candidate,
                "matched": True,
                "trace": reply["trace"],
                "verification_trace": reply["diagnosis_verification"],
            }
            self._record_processed_request(
                state,
                request_id,
                "consultation_turn",
                diagnosis_candidate=state["diagnosis_candidate"],
                patient_reply=None,
            )
            state = self.store.save(state)
            return {
                "session": self.public_session(state),
                "diagnosis_candidate": state["diagnosis_candidate"],
                "patient_reply": None,
                "fallback_used": False,
            }
        if action not in {None, "reply"}:
            raise ClinicalSpeakingError(
                "患者 Agent 返回了无法识别的对话动作。",
                code="patient_dialogue_contract_failed",
                status_code=502,
            )
        state["turns"].append(
            {
                "role": "patient",
                "stage": "consultation",
                "content": reply["reply"],
                "supported_fact_ids": reply["supported_fact_ids"],
                "unknown": reply["unknown"],
                "trace": reply["trace"],
                "dialogue_intent": reply.get("dialogue_intent"),
                "dialogue_trace": reply.get("dialogue_trace"),
                "grounding_trace": reply["grounding"],
                "repair_attempts": reply.get("repair_attempts", 0),
            }
        )
        self._record_processed_request(
            state,
            request_id,
            "consultation_turn",
            diagnosis_candidate=None,
            patient_reply=reply["reply"],
        )
        state = self.store.save(state)
        return {
            "session": self.public_session(state),
            "diagnosis_candidate": None,
            "patient_reply": reply["reply"],
            "fallback_used": False,
        }

    def confirm_diagnosis(
        self,
        session_id: str,
        user_id: str,
        confirmed: bool,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        request_id = self._normalize_request_id(request_id)
        with self._operation_lock(f"session:{session_id}"):
            return self._confirm_diagnosis_locked(session_id, user_id, confirmed, request_id)

    def _confirm_diagnosis_locked(
        self,
        session_id: str,
        user_id: str,
        confirmed: bool,
        request_id: str | None,
    ) -> dict[str, Any]:
        state = self._get_owned(session_id, user_id)
        replay = self._processed_request(state, request_id, "confirm_diagnosis")
        if replay is not None:
            replay_confirmed = bool(replay.get("confirmed"))
            question = None
            if replay_confirmed:
                question_id = str(replay.get("question_id") or "")
                question = next(
                    (
                        item
                        for item in state["blueprint"]["questions"]
                        if item.get("question_id") == question_id
                    ),
                    None,
                )
            response = {"session": self.public_session(state), "fallback_used": False}
            if replay_confirmed:
                self._sync_diagnosis_event(state)
                response.update(
                    {
                        "message": "诊断正确，进入知识表达考核。",
                        "question": self._public_question(question),
                    }
                )
            return response
        if state["status"] != "diagnosis_confirmation" or not state.get("diagnosis_candidate"):
            raise ClinicalSpeakingError("没有待确认的诊断。", code="diagnosis_not_pending", status_code=409)
        if not confirmed:
            state["status"] = "consultation"
            state["diagnosis_candidate"] = None
            self._record_processed_request(
                state,
                request_id,
                "confirm_diagnosis",
                confirmed=False,
            )
            state = self.store.save(state)
            return {"session": self.public_session(state), "fallback_used": False}
        state["status"] = "oral_exam"
        state["question_index"] = 0
        state["confirmed_diagnosis"] = state["diagnosis_candidate"]
        state["diagnosis_candidate"] = None
        question = state["blueprint"]["questions"][0]
        state["turns"].append(
            {"role": "judge", "stage": "oral_exam", "content": question["prompt"], "question_id": question["question_id"]}
        )
        self._record_processed_request(
            state,
            request_id,
            "confirm_diagnosis",
            confirmed=True,
            question_id=question["question_id"],
        )
        state = self.store.save(state)
        self._sync_diagnosis_event(state)
        return {
            "session": self.public_session(state),
            "message": "诊断正确，进入知识表达考核。",
            "question": self._public_question(question),
            "fallback_used": False,
        }

    @staticmethod
    def _kg_node_id(evidence: list[dict[str, Any]]) -> str | None:
        for item in evidence:
            metadata = item.get("metadata", {})
            for key in ("kg_node_ids", "core_ids", "node_ids"):
                values = metadata.get(key)
                if isinstance(values, list) and values:
                    return str(values[0])
            for key in ("kg_node_id", "core_id", "node_id"):
                if metadata.get(key):
                    return str(metadata[key])
        return None

    def _apply_assessment(self, state: dict[str, Any], result: dict[str, Any]) -> None:
        ratio = float(result["score"]) / float(result["max_score"])
        delta = max(-0.06, min(0.04, (ratio - 0.6) * 0.1))
        concept = f"{state['case']['diagnosis']}：{result['label']}"
        mastery = self.memory.upsert_mastery(
            user_id=state["user_id"],
            session_id=state["learning_session_id"],
            concept_name=concept,
            mastery_delta=round(delta, 4),
            kg_node_id=self._kg_node_id(state["evidence"]),
            evidence={
                "event": "clinical_speaking_assessment",
                "question_id": result["question_id"],
                "score": result["score"],
                "max_score": result["max_score"],
                "missing_points": result["missing_points"],
                "fallback_used": False,
            },
            confidence_delta=0.08,
            exposure_increment=1,
            idempotency_key=(
                f"clinical_speaking:{state['id']}:{result['question_id']}"
            ),
        )
        result["mastery_update"] = mastery
        profile = self.memory.get_or_create_profile(state["user_id"])
        weaknesses = [str(item) for item in profile.get("known_weak_points", [])]
        if ratio < 0.6 and concept not in weaknesses:
            weaknesses.append(concept)
        elif ratio >= 0.85 and concept in weaknesses:
            weaknesses.remove(concept)
        profile["known_weak_points"] = weaknesses[-30:]
        self.memory.save_profile(state["user_id"], profile)
        self._record_learning_event_once(
            state,
            "clinical_speaking_answer_assessed",
            f"assessment:{result['question_id']}",
            {
                "surface": "clinical_speaking",
                "diagnosis": state["case"]["diagnosis"],
                "question_id": result["question_id"],
                "category": result["category"],
                "label": result["label"],
                "score": result["score"],
                "max_score": result["max_score"],
                "missing_points": result["missing_points"],
                "fallback_used": False,
            },
            legacy_match={"category": result["category"]},
        )

    def submit_exam_answer(
        self,
        session_id: str,
        user_id: str,
        transcript: str,
        request_id: str | None = None,
        input_mode: str = "speech",
    ) -> dict[str, Any]:
        request_id = self._normalize_request_id(request_id)
        with self._operation_lock(f"session:{session_id}"):
            return self._submit_exam_answer_locked(
                session_id,
                user_id,
                transcript,
                request_id,
                input_mode,
            )

    def _submit_exam_answer_locked(
        self,
        session_id: str,
        user_id: str,
        transcript: str,
        request_id: str | None,
        input_mode: str,
    ) -> dict[str, Any]:
        state = self._get_owned(session_id, user_id)
        replay = self._processed_request(state, request_id, "exam_answer")
        if replay is not None:
            question_id = str(replay.get("question_id") or "")
            result = next(
                (item for item in state["assessments"] if item.get("question_id") == question_id),
                None,
            )
            if result is None:
                raise ClinicalSpeakingError(
                    "已处理请求缺少对应评分记录。",
                    code="session_state_invalid",
                    status_code=500,
                )
            next_question_id = str(replay.get("next_question_id") or "")
            next_question = next(
                (
                    item
                    for item in state["blueprint"]["questions"]
                    if item.get("question_id") == next_question_id
                ),
                None,
            )
            completed = bool(replay.get("completed"))
            self._apply_assessment(state, result)
            if completed:
                self._sync_completed_event(state)
            return {
                "session": self.public_session(state),
                "assessment": self._public_assessment(result, state["public_evidence"]),
                "next_question": self._public_question(next_question),
                "completed": completed,
                "summary": self._summary(state) if completed else None,
                "fallback_used": False,
            }
        if state["status"] != "oral_exam":
            raise ClinicalSpeakingError("当前不处于知识表达考核阶段。", code="invalid_stage", status_code=409)
        question = state["blueprint"]["questions"][state["question_index"]]
        try:
            normalized = self._prepare_transcript(
                transcript,
                self._term_catalog(state["case"], state["blueprint"]),
                input_mode,
            )
            result = assess_exam_answer(
                state["case"]["diagnosis"],
                question,
                normalized["normalized_text"],
                state["evidence"],
            )
        except StrictAgentError as exc:
            raise ClinicalSpeakingError(str(exc), code=exc.code, status_code=exc.status_code) from exc
        state["turns"].append(
            {
                "role": "doctor",
                "stage": "oral_exam",
                "content": normalized["normalized_text"],
                "raw_transcript": normalized["raw_text"],
                "corrections": normalized["corrections"],
                "question_id": question["question_id"],
                "trace": normalized["trace"],
            }
        )
        state["assessments"].append(result)
        state["question_index"] += 1
        next_question = None
        if state["question_index"] >= len(state["blueprint"]["questions"]):
            state["status"] = "completed"
            state["completed_score"] = round(sum(item["score"] for item in state["assessments"]), 1)
        else:
            next_question = state["blueprint"]["questions"][state["question_index"]]
            state["turns"].append(
                {
                    "role": "judge",
                    "stage": "oral_exam",
                    "content": next_question["prompt"],
                    "question_id": next_question["question_id"],
                }
            )
        self._record_processed_request(
            state,
            request_id,
            "exam_answer",
            question_id=question["question_id"],
            next_question_id=next_question["question_id"] if next_question else None,
            completed=state["status"] == "completed",
        )
        state = self.store.save(state)
        self._apply_assessment(state, result)
        if state["status"] == "completed":
            self._sync_completed_event(state)
        return {
            "session": self.public_session(state),
            "assessment": self._public_assessment(result, state["public_evidence"]),
            "next_question": self._public_question(next_question) if next_question else None,
            "completed": state["status"] == "completed",
            "summary": self._summary(state) if state["status"] == "completed" else None,
            "fallback_used": False,
        }

    @staticmethod
    def _public_question(question: dict[str, Any] | None) -> dict[str, Any] | None:
        if not question:
            return None
        return {
            key: question[key]
            for key in ("question_id", "category", "label", "prompt", "max_score")
        }

    @staticmethod
    def _public_assessment(result: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, Any]:
        by_index = {item["index"]: item for item in evidence}
        return {
            key: result[key]
            for key in (
                "question_id", "category", "label", "score", "max_score",
                "critique", "strengths", "missing_points", "errors", "trace",
            )
        } | {"evidence": [by_index[index] for index in result["evidence_refs"] if index in by_index]}

    @staticmethod
    def _summary(state: dict[str, Any]) -> dict[str, Any]:
        return {
            "score": state.get("completed_score", 0),
            "max_score": 100,
            "diagnosis": state["case"]["diagnosis"],
            "assessments": [
                {
                    "category": item["category"],
                    "label": item["label"],
                    "score": item["score"],
                    "max_score": item["max_score"],
                }
                for item in state["assessments"]
            ],
        }

    def public_session(self, state: dict[str, Any]) -> dict[str, Any]:
        current_question = None
        if state["status"] == "oral_exam" and state["question_index"] < len(state["blueprint"]["questions"]):
            current_question = self._public_question(state["blueprint"]["questions"][state["question_index"]])
        return {
            "id": state["id"],
            "status": state["status"],
            "selection": {
                "source": state["selection"]["source"],
                "matched": state["selection"]["source"] == "learner_context",
            },
            "case": {
                "case_ref": state["case"]["case_ref"],
                "department": state["case"]["department"],
                "clinical_findings": state["case"]["clinical_findings"],
            },
            "turns": [
                {
                    key: turn[key]
                    for key in ("role", "stage", "content", "question_id", "corrections")
                    if key in turn
                }
                for turn in state["turns"]
            ],
            "diagnosis_candidate": state.get("diagnosis_candidate"),
            "question_index": state.get("question_index", 0),
            "question_count": len(state["blueprint"]["questions"]),
            "current_question": current_question,
            "assessments": [self._public_assessment(item, state["public_evidence"]) for item in state["assessments"]],
            "summary": self._summary(state) if state["status"] == "completed" else None,
            "created_at": state["created_at"],
            "updated_at": state["updated_at"],
            "fallback_used": False,
        }

    def get_session(self, session_id: str, user_id: str) -> dict[str, Any]:
        return self.public_session(self._get_owned(session_id, user_id))

    def list_sessions(self, user_id: str) -> list[dict[str, Any]]:
        sessions: list[dict[str, Any]] = []
        for state in self.store.list_for_user(user_id):
            try:
                self._validate_session_state(state)
            except (ValueError, TypeError, KeyError) as exc:
                logger.error(
                    "Skipping invalid clinical speaking history %s: %s",
                    state.get("id"),
                    exc,
                )
                continue
            sessions.append(self.public_session(state))
        return sessions
