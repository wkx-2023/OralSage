"""Curated runtime view over the de-identified outpatient case dataset."""

from __future__ import annotations

import hashlib
import json
import random
import re
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CASE_PATH = PROJECT_ROOT / "data" / "processed" / "clinic_stats" / "clinical_cases.parsed.jsonl"

DIAGNOSIS_ALIASES: dict[str, tuple[str, ...]] = {
    "急性冠周炎": ("智齿冠周炎", "第三磨牙冠周炎", "冠周炎"),
    "阻生牙": ("阻生齿", "阻生智齿", "阻生第三磨牙"),
    "埋伏牙": ("埋伏齿",),
    "牙髓炎": ("急性牙髓炎", "慢性牙髓炎"),
    "慢性根尖牙周炎": ("慢性根尖周炎",),
    "急性根尖周炎": (
        "急性根尖牙周炎",
        "急性浆液性根尖周炎",
        "急性化脓性根尖周炎",
        "急性牙槽脓肿",
        "根尖周脓肿",
    ),
    "慢性牙周炎": ("牙周炎",),
    "残根": ("牙根残留",),
    "残冠": ("牙冠残留",),
    "牙折裂": ("牙齿折裂", "牙裂"),
    "乳牙滞留": ("乳牙未脱落",),
    "额外牙［多生牙］": ("额外牙", "多生牙"),
    "根尖周囊肿": ("根尖囊肿",),
    "舌系带过短": ("舌系带短",),
}

# Only diagnoses with a complete textbook disease chapter are used by the
# five-domain consultation and oral-exam workflow.
TRAINING_ELIGIBLE_DIAGNOSES = {
    "急性冠周炎",
    "阻生牙",
    "牙髓炎",
    "急性根尖周炎",
    "慢性根尖牙周炎",
    "慢性牙周炎",
    "根尖周囊肿",
}

FOLLOWUP_MARKERS = re.compile(r"复查|取模|戴牙|拆线|换药|术后|治疗后|病史同前")
EMPTY_VALUES = {"", "无", "暂无", "未见异常", "病史同前"}


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


class ClinicalCaseRepository:
    def __init__(self, path: str | Path | None = None, *, rng: random.Random | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_CASE_PATH
        self._rng = rng or random.SystemRandom()
        self._cache: list[dict[str, Any]] | None = None
        self._mtime_ns: int | None = None

    def _load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        mtime_ns = self.path.stat().st_mtime_ns
        if self._cache is not None and self._mtime_ns == mtime_ns:
            return self._cache
        cases: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                prepared = self._prepare(raw)
                if prepared is not None:
                    cases.append(prepared)
        self._cache = cases
        self._mtime_ns = mtime_ns
        return cases

    def _prepare(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        diagnosis = _clean(raw.get("diagnosis_name"))
        chief = _clean(raw.get("chief_complaint"))
        illness = _clean(raw.get("present_illness"))
        exam = _clean(raw.get("exam"))
        if diagnosis not in TRAINING_ELIGIBLE_DIAGNOSES:
            return None
        if len(chief) < 6 or len(illness) < 10 or len(exam) < 6:
            return None
        if FOLLOWUP_MARKERS.search(f"{chief} {illness}"):
            return None

        facts = [
            {"id": "chief_complaint", "label": "主诉", "value": chief},
            {"id": "present_illness", "label": "现病史", "value": illness},
        ]
        past = _clean(raw.get("past_history"))
        if past not in EMPTY_VALUES:
            facts.append({"id": "past_history", "label": "既往史", "value": past})
        aux_exam = _clean(raw.get("aux_exam"))
        public_ref = hashlib.sha256(
            f"{raw.get('case_id', '')}|{diagnosis}|{chief}|{illness}".encode("utf-8")
        ).hexdigest()[:16]
        return {
            "case_ref": f"case_{public_ref}",
            "diagnosis": diagnosis,
            "diagnosis_aliases": [diagnosis, *DIAGNOSIS_ALIASES[diagnosis]],
            "patient_facts": facts,
            "clinical_findings": {
                "exam": exam,
                "aux_exam": "" if aux_exam in EMPTY_VALUES else aux_exam,
            },
            "department": _clean(raw.get("department")),
        }

    def count(self) -> int:
        return len(self._load())

    def diagnoses(self) -> list[str]:
        return sorted({case["diagnosis"] for case in self._load()})

    def select(
        self,
        _target_terms: list[str],
        *,
        preferred_diagnosis: str | None = None,
        matched_target: str = "",
    ) -> tuple[dict[str, Any], str, str]:
        cases = self._load()
        if not cases:
            raise LookupError("No eligible clinical speaking cases are available")
        if preferred_diagnosis:
            matched = [case for case in cases if case["diagnosis"] == preferred_diagnosis]
            if not matched:
                raise LookupError(f"No eligible cases for selected diagnosis: {preferred_diagnosis}")
            return self._rng.choice(matched), "learner_context", matched_target
        diagnoses = sorted({case["diagnosis"] for case in cases})
        selected_diagnosis = self._rng.choice(diagnoses)
        matched = [case for case in cases if case["diagnosis"] == selected_diagnosis]
        return self._rng.choice(matched), "random", ""
