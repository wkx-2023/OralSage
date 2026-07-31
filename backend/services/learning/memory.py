"""SQLite learning memory layer — persists users, profiles, sessions, outlines,
events, resources, mastery and answer records.

All JSON columns are stored via ``json.dumps(..., ensure_ascii=False)`` and read
back with ``json.loads``.
"""

from __future__ import annotations

import json
import copy
import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .schemas import DEFAULT_PROFILE

_DEFAULT_DB_PATH = Path("data/learning/learning.sqlite3")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY,
    display_name    TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_profiles (
    user_id         TEXT PRIMARY KEY,
    profile_json    TEXT NOT NULL,
    confidence      REAL NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS learning_sessions (
    id                      TEXT PRIMARY KEY,
    user_id                 TEXT NOT NULL,
    topic                   TEXT NOT NULL,
    mode                    TEXT NOT NULL,
    status                  TEXT NOT NULL,
    current_outline_node_id TEXT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lesson_outlines (
    id                 TEXT PRIMARY KEY,
    session_id         TEXT NOT NULL,
    outline_json       TEXT NOT NULL,
    mindmap_json       TEXT NOT NULL,
    source_trace_json  TEXT NOT NULL,
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS generated_resources (
    id                 TEXT PRIMARY KEY,
    session_id         TEXT NOT NULL,
    resource_type      TEXT NOT NULL,
    resource_path      TEXT,
    resource_json      TEXT NOT NULL,
    source_trace_json  TEXT NOT NULL,
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lesson_events (
    id               TEXT PRIMARY KEY,
    session_id       TEXT NOT NULL,
    user_id          TEXT NOT NULL,
    event_type       TEXT NOT NULL,
    outline_node_id  TEXT,
    payload_json     TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mastery_records (
    id                TEXT PRIMARY KEY,
    user_id           TEXT NOT NULL,
    session_id        TEXT NOT NULL,
    kg_node_id        TEXT,
    concept_name      TEXT NOT NULL,
    mastery_score     REAL NOT NULL,
    confidence        REAL NOT NULL,
    exposure_count    INTEGER NOT NULL DEFAULT 0,
    evidence_json     TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_tracing_states (
    user_id                   TEXT NOT NULL,
    kg_node_id                TEXT NOT NULL,
    skill_label               TEXT NOT NULL,
    p_mastery                 REAL NOT NULL,
    uncertainty               REAL NOT NULL,
    confidence                REAL NOT NULL,
    evidence_count            INTEGER NOT NULL DEFAULT 0,
    correct_count             INTEGER NOT NULL DEFAULT 0,
    incorrect_count           INTEGER NOT NULL DEFAULT 0,
    last_observation_correct  INTEGER,
    parameters_json           TEXT NOT NULL,
    trace_json                TEXT NOT NULL,
    updated_at                TEXT NOT NULL,
    PRIMARY KEY (user_id, kg_node_id)
);

CREATE TABLE IF NOT EXISTS direct_qa_mastery_records (
    id                TEXT PRIMARY KEY,
    user_id           TEXT NOT NULL,
    kg_node_id        TEXT,
    concept_name      TEXT NOT NULL,
    mastery_score     REAL NOT NULL,
    confidence        REAL NOT NULL,
    source            TEXT NOT NULL DEFAULT 'direct_qa',
    source_ref        TEXT,
    evidence_json     TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_direct_qa_mastery_user_kg
ON direct_qa_mastery_records (user_id, kg_node_id);

CREATE INDEX IF NOT EXISTS idx_knowledge_tracing_user
ON knowledge_tracing_states (user_id, updated_at);

CREATE TABLE IF NOT EXISTS answer_records (
    id                TEXT PRIMARY KEY,
    session_id        TEXT NOT NULL,
    user_id           TEXT NOT NULL,
    question_id       TEXT NOT NULL,
    outline_node_id   TEXT,
    user_answer       TEXT NOT NULL,
    grading_json      TEXT NOT NULL,
    is_correct        INTEGER NOT NULL,
    score             REAL NOT NULL,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS learner_memory (
    user_id           TEXT NOT NULL,
    slot              TEXT NOT NULL,
    data_json         TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (user_id, slot)
);

CREATE TABLE IF NOT EXISTS learning_summaries (
    id                    TEXT PRIMARY KEY,
    user_id               TEXT NOT NULL,
    session_id            TEXT,
    surface               TEXT NOT NULL,
    summary_json          TEXT NOT NULL,
    source_event_ids_json TEXT NOT NULL,
    created_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_consolidation_meta (
    user_id       TEXT NOT NULL,
    layer         TEXT NOT NULL,
    key           TEXT NOT NULL,
    seen_ids_json TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (user_id, layer, key)
);

CREATE TABLE IF NOT EXISTS study_plan_items (
    id                  TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL,
    plan_date           TEXT NOT NULL,
    title               TEXT NOT NULL,
    notes               TEXT,
    status              TEXT NOT NULL DEFAULT 'open',
    linked_course_id    TEXT,
    linked_course_title TEXT,
    task_type           TEXT NOT NULL DEFAULT 'topic_learning',
    learning_target     TEXT,
    kg_node_ids_json    TEXT NOT NULL,
    learning_scope_json TEXT NOT NULL DEFAULT '{}',
    dependency          TEXT,
    practice_level      TEXT,
    session_id          TEXT,
    sort_order          INTEGER NOT NULL DEFAULT 0,
    deferred_count      INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    completed_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_study_plan_items_user_date
ON study_plan_items (user_id, plan_date, sort_order, created_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _normalize_plan_date(value: str | date | datetime) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError(f"Invalid plan date: {value!r}") from exc


def _json_list(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = [part.strip() for part in value.split(",")]
    else:
        parsed = value
    if not isinstance(parsed, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in parsed:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


_PROFILE_HISTORY_KEYS = (
    "level",
    "goal",
    "preferred_explanation",
    "pace",
    "daily_available_minutes",
    "assessment_preference",
    "planning_support_need",
    "learner_type",
    "learner_type_summary",
    "known_weak_points",
    "mistake_patterns",
    "mastery_by_kg_node",
    "plan_adherence",
    "review_need",
    "target_exam",
    "assessment_skipped",
    "last_assessed_at",
    "assessment_source",
)


def _profile_with_defaults(profile: dict[str, Any] | None) -> dict[str, Any]:
    """Return a compact profile with all stable fields present."""
    result = copy.deepcopy(DEFAULT_PROFILE)
    if isinstance(profile, dict):
        result.update(profile)
    for key in ("known_weak_points", "mistake_patterns"):
        if not isinstance(result.get(key), list):
            result[key] = []
    if not isinstance(result.get("mastery_by_kg_node"), dict):
        result["mastery_by_kg_node"] = {}
    return result


def _profile_has_history_data(profile: dict[str, Any] | None) -> bool:
    """Return True when a profile contains user-specific non-default data."""
    if not isinstance(profile, dict):
        return False
    normalized = _profile_with_defaults(profile)
    defaults = copy.deepcopy(DEFAULT_PROFILE)
    for key in _PROFILE_HISTORY_KEYS:
        if normalized.get(key) != defaults.get(key):
            return True
    try:
        confidence = float(normalized.get("confidence", defaults["confidence"]))
    except (TypeError, ValueError):
        return True
    return confidence > float(defaults["confidence"])


def _learner_memory_row_has_history(slot: str, data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    if slot == "profile_state":
        if data.get("evidence_ids"):
            return True
        if data.get("weakness_summary") or data.get("mastery_summary"):
            return True
        style = data.get("style")
        if isinstance(style, dict):
            for dimension in style.values():
                if not isinstance(dimension, dict):
                    continue
                options = dimension.get("options")
                if not isinstance(options, dict):
                    continue
                for option in options.values():
                    if not isinstance(option, dict):
                        continue
                    if option.get("evidence_ids"):
                        return True
                    try:
                        if float(option.get("evidence_count", 0)) > 0:
                            return True
                    except (TypeError, ValueError):
                        continue
        return False
    items = data.get("items")
    return isinstance(items, list) and any(isinstance(item, dict) for item in items)


class LearningMemory:
    """SQLite-backed memory store for the learning agent.

    Parameters
    ----------
    db_path:
        Path to the SQLite file.  Defaults to ``data/learning/learning.sqlite3``.
        Parent directories are created automatically.
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else _DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        self._conn.executescript(_SCHEMA_SQL)
        # Migration: add exposure_count if missing (for existing databases)
        try:
            self._conn.execute(
                "ALTER TABLE mastery_records ADD COLUMN exposure_count INTEGER NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError:
            pass  # column already exists
        for statement in (
            "ALTER TABLE study_plan_items ADD COLUMN task_type TEXT NOT NULL DEFAULT 'topic_learning'",
            "ALTER TABLE study_plan_items ADD COLUMN learning_target TEXT",
        ):
            try:
                self._conn.execute(statement)
            except sqlite3.OperationalError:
                pass  # column already exists
        # Migration: add learning_scope_json for optional scope persistence
        try:
            self._conn.execute(
                "ALTER TABLE study_plan_items ADD COLUMN learning_scope_json TEXT NOT NULL DEFAULT '{}'"
            )
        except sqlite3.OperationalError:
            pass  # column already exists
        # Migration: add dependency / practice_level / session_id for task semantics
        for statement in (
            "ALTER TABLE study_plan_items ADD COLUMN dependency TEXT",
            "ALTER TABLE study_plan_items ADD COLUMN practice_level TEXT",
            "ALTER TABLE study_plan_items ADD COLUMN session_id TEXT",
        ):
            try:
                self._conn.execute(statement)
            except sqlite3.OperationalError:
                pass  # column already exists
        self._conn.commit()

    def _row_to_dict(self, row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        return dict(row)

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    # -- user ----------------------------------------------------------

    def ensure_user(self, user_id: str, display_name: str | None = None) -> None:
        """Create a user row if it does not already exist."""
        self._conn.execute(
            "INSERT OR IGNORE INTO users (id, display_name, created_at) VALUES (?, ?, ?)",
            (user_id, display_name, _now()),
        )
        self._conn.commit()

    # -- profile -------------------------------------------------------

    def get_or_create_profile(
        self, user_id: str, display_name: str | None = None
    ) -> dict:
        """Return the stored profile dict or create one from DEFAULT_PROFILE."""
        self.ensure_user(user_id, display_name)
        profile = self.get_profile(user_id)
        if profile is not None:
            return profile
        return self.save_profile(user_id, copy.deepcopy(DEFAULT_PROFILE))

    def get_profile(self, user_id: str) -> dict[str, Any] | None:
        """Return the stored profile dict, or None without creating defaults."""
        row = self._conn.execute(
            "SELECT * FROM user_profiles WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return None
        profile = json.loads(row["profile_json"])
        profile["confidence"] = row["confidence"]
        return _profile_with_defaults(profile)

    def save_profile(self, user_id: str, profile: dict) -> dict:
        """Persist a profile dict.  ``confidence`` is extracted to its own column."""
        profile = _profile_with_defaults(profile)
        confidence = float(profile.pop("confidence", DEFAULT_PROFILE["confidence"]))
        profile_json = json.dumps(profile, ensure_ascii=False)
        self._conn.execute(
            """INSERT OR REPLACE INTO user_profiles
               (user_id, profile_json, confidence, updated_at)
               VALUES (?, ?, ?, ?)""",
            (user_id, profile_json, confidence, _now()),
        )
        self._conn.commit()
        # return the full dict as the caller would see it
        profile["confidence"] = confidence
        return profile

    def user_has_history_data(
        self,
        user_id: str,
        *,
        profile: dict[str, Any] | None = None,
        include_profile: bool = True,
    ) -> bool:
        """Return True when the user has persisted learning or profile history."""
        if include_profile:
            stored_profile = profile if profile is not None else self.get_profile(user_id)
            if _profile_has_history_data(stored_profile):
                return True

        for table in (
            "learning_sessions",
            "lesson_events",
            "mastery_records",
            "answer_records",
            "knowledge_tracing_states",
            "learning_summaries",
            "study_plan_items",
        ):
            row = self._conn.execute(
                f"SELECT 1 FROM {table} WHERE user_id = ? LIMIT 1",
                (user_id,),
            ).fetchone()
            if row is not None:
                return True

        rows = self._conn.execute(
            "SELECT slot, data_json FROM learner_memory WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        for row in rows:
            try:
                data = json.loads(row["data_json"])
            except json.JSONDecodeError:
                continue
            if _learner_memory_row_has_history(str(row["slot"]), data):
                return True
        return False

    def learner_assessment_state(
        self,
        user_id: str,
        *,
        profile: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return onboarding state used by frontend gates."""
        stored_profile = profile if profile is not None else self.get_profile(user_id)
        normalized = _profile_with_defaults(stored_profile)
        has_assessment_record = bool(
            normalized.get("assessment_source")
            or normalized.get("assessment_skipped")
            or normalized.get("last_assessed_at")
        )
        is_default_profile = not _profile_has_history_data(stored_profile)
        has_history_data = self.user_has_history_data(
            user_id,
            profile=stored_profile,
            include_profile=True,
        )
        needs_initial_assessment = not has_assessment_record and not has_history_data
        reason = "ready"
        if needs_initial_assessment:
            reason = "no_assessment_or_history"
        elif not has_assessment_record and has_history_data:
            reason = "history_without_assessment"
        elif normalized.get("assessment_skipped"):
            reason = "assessment_skipped"
        elif has_assessment_record:
            reason = "assessment_completed"
        return {
            "schema_version": "v1",
            "has_assessment_record": has_assessment_record,
            "assessment_skipped": bool(normalized.get("assessment_skipped")),
            "assessment_source": normalized.get("assessment_source"),
            "last_assessed_at": normalized.get("last_assessed_at"),
            "has_history_data": has_history_data,
            "is_default_profile": is_default_profile,
            "needs_initial_assessment": needs_initial_assessment,
            "can_retake": True,
            "reason": reason,
        }

    # -- study plan ----------------------------------------------------

    def _study_plan_row_to_dict(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item["kg_node_ids"] = _json_list(item.pop("kg_node_ids_json", "[]"))
        scope_raw = item.pop("learning_scope_json", "{}")
        try:
            scope = json.loads(scope_raw) if isinstance(scope_raw, str) else scope_raw
        except json.JSONDecodeError:
            scope = {}
        item["learning_scope"] = scope if isinstance(scope, dict) else {}
        return item

    def _next_study_plan_sort_order(self, user_id: str, plan_date: str) -> int:
        row = self._conn.execute(
            """SELECT COALESCE(MAX(sort_order), -1) AS max_order
               FROM study_plan_items
               WHERE user_id = ? AND plan_date = ?""",
            (user_id, plan_date),
        ).fetchone()
        return int(row["max_order"] if row else -1) + 1

    def list_study_plan_items(
        self,
        user_id: str,
        start_date: str | date | datetime,
        end_date: str | date | datetime,
    ) -> list[dict[str, Any]]:
        """Return study-plan items for a date range, oldest first."""
        self.ensure_user(user_id)
        start = _normalize_plan_date(start_date)
        end = _normalize_plan_date(end_date)
        rows = self._conn.execute(
            """SELECT * FROM study_plan_items
               WHERE user_id = ? AND plan_date >= ? AND plan_date <= ?
               ORDER BY plan_date ASC, sort_order ASC, created_at ASC""",
            (user_id, start, end),
        ).fetchall()
        return [
            item for item in (self._study_plan_row_to_dict(row) for row in rows)
            if item is not None
        ]

    def list_study_plan_items_for_day(
        self,
        user_id: str,
        plan_date: str | date | datetime,
    ) -> list[dict[str, Any]]:
        """Return study-plan items for one day."""
        day = _normalize_plan_date(plan_date)
        return self.list_study_plan_items(user_id, day, day)

    def list_open_study_plan_items(
        self,
        user_id: str,
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return all open study-plan items for *user_id*, oldest first.

        Items are sorted by ``(plan_date ASC, sort_order ASC, created_at ASC)``
        so the result is stable across calls.  Pass *limit* to cap the result
        (e.g. for candidate selection in the router).
        """
        self.ensure_user(user_id)
        query = """SELECT * FROM study_plan_items
                   WHERE user_id = ? AND status = 'open'
                   ORDER BY plan_date ASC, sort_order ASC, created_at ASC"""
        params: list[Any] = [user_id]
        if limit is not None:
            query += " LIMIT ?"
            params.append(max(1, int(limit)))
        rows = self._conn.execute(query, params).fetchall()
        return [
            item for item in (self._study_plan_row_to_dict(row) for row in rows)
            if item is not None
        ]

    def get_study_plan_item(
        self,
        item_id: str,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Return one study-plan item. Raises ``KeyError`` when missing."""
        if user_id:
            row = self._conn.execute(
                "SELECT * FROM study_plan_items WHERE id = ? AND user_id = ?",
                (item_id, user_id),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT * FROM study_plan_items WHERE id = ?",
                (item_id,),
            ).fetchone()
        item = self._study_plan_row_to_dict(row)
        if item is None:
            raise KeyError(f"Study plan item not found: {item_id}")
        return item

    def create_study_plan_item(
        self,
        *,
        user_id: str,
        plan_date: str | date | datetime,
        title: str,
        notes: str | None = None,
        linked_course_id: str | None = None,
        linked_course_title: str | None = None,
        task_type: str = "topic_learning",
        learning_target: str | None = None,
        kg_node_ids: list[str] | None = None,
        learning_scope: dict[str, Any] | None = None,
        dependency: str | None = None,
        practice_level: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a study-plan to-do item."""
        self.ensure_user(user_id)
        day = _normalize_plan_date(plan_date)
        clean_title = str(title or "").strip()
        if not clean_title:
            raise ValueError("title is required")
        item_id = _new_id("plan_")
        now = _now()
        scope = learning_scope if isinstance(learning_scope, dict) else {}
        self._conn.execute(
            """INSERT INTO study_plan_items
               (id, user_id, plan_date, title, notes, status,
                linked_course_id, linked_course_title, task_type, learning_target,
                kg_node_ids_json, learning_scope_json,
                dependency, practice_level, session_id,
                sort_order, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, ?, ?, ?)""",
            (
                item_id,
                user_id,
                day,
                clean_title[:180],
                str(notes or "").strip()[:800] or None,
                str(linked_course_id or "").strip() or None,
                str(linked_course_title or "").strip()[:180] or None,
                str(task_type or "topic_learning").strip()[:64] or "topic_learning",
                str(learning_target or "").strip()[:180] or None,
                json.dumps(_json_list(kg_node_ids or []), ensure_ascii=False),
                json.dumps(scope, ensure_ascii=False),
                str(dependency or "").strip() or None,
                str(practice_level or "").strip()[:32] or None,
                str(session_id or "").strip() or None,
                self._next_study_plan_sort_order(user_id, day),
                now,
                now,
            ),
        )
        self._conn.commit()
        return self.get_study_plan_item(item_id, user_id=user_id)

    def update_study_plan_item(
        self,
        item_id: str,
        *,
        user_id: str,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        """Update editable fields on a study-plan item."""
        self.get_study_plan_item(item_id, user_id=user_id)
        allowed: dict[str, Any] = {}
        if "plan_date" in updates:
            allowed["plan_date"] = _normalize_plan_date(updates["plan_date"])
        if "title" in updates:
            clean_title = str(updates.get("title") or "").strip()
            if not clean_title:
                raise ValueError("title is required")
            allowed["title"] = clean_title[:180]
        if "notes" in updates:
            allowed["notes"] = str(updates.get("notes") or "").strip()[:800] or None
        if "linked_course_id" in updates:
            allowed["linked_course_id"] = str(updates.get("linked_course_id") or "").strip() or None
        if "linked_course_title" in updates:
            allowed["linked_course_title"] = str(updates.get("linked_course_title") or "").strip()[:180] or None
        if "task_type" in updates:
            allowed["task_type"] = str(updates.get("task_type") or "topic_learning").strip()[:64] or "topic_learning"
        if "learning_target" in updates:
            allowed["learning_target"] = str(updates.get("learning_target") or "").strip()[:180] or None
        if "kg_node_ids" in updates:
            allowed["kg_node_ids_json"] = json.dumps(
                _json_list(updates.get("kg_node_ids") or []),
                ensure_ascii=False,
            )
        if "learning_scope" in updates:
            scope = updates.get("learning_scope")
            if not isinstance(scope, dict):
                raise ValueError("learning_scope must be a dict or null")
            allowed["learning_scope_json"] = json.dumps(scope, ensure_ascii=False)
        if "dependency" in updates:
            allowed["dependency"] = str(updates.get("dependency") or "").strip() or None
        if "practice_level" in updates:
            allowed["practice_level"] = str(updates.get("practice_level") or "").strip()[:32] or None
        if "session_id" in updates:
            allowed["session_id"] = str(updates.get("session_id") or "").strip() or None
        if not allowed:
            return self.get_study_plan_item(item_id, user_id=user_id)

        allowed["updated_at"] = _now()
        assignments = ", ".join(f"{key} = ?" for key in allowed)
        params = list(allowed.values()) + [item_id, user_id]
        self._conn.execute(
            f"""UPDATE study_plan_items
                SET {assignments}
                WHERE id = ? AND user_id = ?""",
            params,
        )
        self._conn.commit()
        return self.get_study_plan_item(item_id, user_id=user_id)

    def set_study_plan_item_completion(
        self,
        item_id: str,
        *,
        user_id: str,
        completed: bool,
    ) -> dict[str, Any]:
        """Mark a study-plan item completed or open."""
        self.get_study_plan_item(item_id, user_id=user_id)
        now = _now()
        self._conn.execute(
            """UPDATE study_plan_items
               SET status = ?, completed_at = ?, updated_at = ?
               WHERE id = ? AND user_id = ?""",
            (
                "completed" if completed else "open",
                now if completed else None,
                now,
                item_id,
                user_id,
            ),
        )
        self._conn.commit()
        return self.get_study_plan_item(item_id, user_id=user_id)

    def defer_study_plan_item(
        self,
        item_id: str,
        *,
        user_id: str,
        to_date: str | date | datetime | None = None,
    ) -> dict[str, Any]:
        """Move a study-plan item to another day and reopen it."""
        item = self.get_study_plan_item(item_id, user_id=user_id)
        if to_date is None:
            current = date.fromisoformat(item["plan_date"])
            target = (current + timedelta(days=1)).isoformat()
        else:
            target = _normalize_plan_date(to_date)
        now = _now()
        self._conn.execute(
            """UPDATE study_plan_items
               SET plan_date = ?, status = 'open', completed_at = NULL,
                   sort_order = ?, deferred_count = deferred_count + 1,
                   updated_at = ?
               WHERE id = ? AND user_id = ?""",
            (
                target,
                self._next_study_plan_sort_order(user_id, target),
                now,
                item_id,
                user_id,
            ),
        )
        self._conn.commit()
        return self.get_study_plan_item(item_id, user_id=user_id)

    def delete_study_plan_item(
        self,
        item_id: str,
        *,
        user_id: str,
    ) -> dict[str, Any]:
        """Delete a study-plan item and return the removed row."""
        item = self.get_study_plan_item(item_id, user_id=user_id)
        self._conn.execute(
            "DELETE FROM study_plan_items WHERE id = ? AND user_id = ?",
            (item_id, user_id),
        )
        self._conn.commit()
        return item

    # -- session -------------------------------------------------------

    def create_session(
        self,
        user_id: str,
        topic: str,
        mode: str = "course_learning",
        *,
        session_id: str | None = None,
    ) -> dict:
        """Create a learning session, optionally idempotent by session id."""
        self.ensure_user(user_id)
        sid = str(session_id or _new_id("lesson_")).strip()
        if not sid:
            raise ValueError("session_id must not be empty")
        existing = self._conn.execute(
            "SELECT * FROM learning_sessions WHERE id = ?", (sid,)
        ).fetchone()
        if existing is not None:
            stored = dict(existing)
            if (
                stored["user_id"] != user_id
                or stored["topic"] != topic
                or stored["mode"] != mode
            ):
                raise ValueError(f"Session id conflicts with existing session: {sid}")
            return stored
        now = _now()
        self._conn.execute(
            """INSERT INTO learning_sessions
               (id, user_id, topic, mode, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'active', ?, ?)
               ON CONFLICT(id) DO NOTHING""",
            (sid, user_id, topic, mode, now, now),
        )
        self._conn.commit()
        stored = self.get_session(sid)
        if (
            stored["user_id"] != user_id
            or stored["topic"] != topic
            or stored["mode"] != mode
        ):
            raise ValueError(f"Session id conflicts with existing session: {sid}")
        return stored

    def get_session(self, session_id: str) -> dict:
        """Return a session dict.  Raises ``KeyError`` when not found."""
        row = self._conn.execute(
            "SELECT * FROM learning_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Session not found: {session_id}")
        return dict(row)

    def set_session_status(self, session_id: str, status: str) -> dict:
        """Persist a lifecycle status for a learning session."""
        normalized = str(status or "").strip()
        if normalized not in {"active", "completed", "abandoned"}:
            raise ValueError(f"Unsupported learning session status: {status}")
        cursor = self._conn.execute(
            "UPDATE learning_sessions SET status = ?, updated_at = ? WHERE id = ?",
            (normalized, _now(), session_id),
        )
        if cursor.rowcount != 1:
            self._conn.rollback()
            raise KeyError(f"Session not found: {session_id}")
        self._conn.commit()
        return self.get_session(session_id)

    def list_sessions_for_user(
        self,
        user_id: str,
        *,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Return recent learning sessions for a user, newest first."""
        self.ensure_user(user_id)
        rows = self._conn.execute(
            """SELECT * FROM learning_sessions
               WHERE user_id = ?
               ORDER BY updated_at DESC, created_at DESC
               LIMIT ?""",
            (user_id, max(1, min(int(limit), 100))),
        ).fetchall()
        return [dict(row) for row in rows]

    def set_current_outline_node(
        self, session_id: str, node_id: str | None
    ) -> None:
        """Update the session's current outline node pointer."""
        self._conn.execute(
            "UPDATE learning_sessions SET current_outline_node_id = ?, updated_at = ? WHERE id = ?",
            (node_id, _now(), session_id),
        )
        self._conn.commit()

    # -- outline -------------------------------------------------------

    def save_outline(
        self,
        session_id: str,
        outline: dict,
        mindmap: dict,
        source_trace: dict,
    ) -> None:
        """Persist a lesson outline (one-per-session in MVP)."""
        oid = _new_id("outline_")
        self._conn.execute(
            """INSERT OR REPLACE INTO lesson_outlines
               (id, session_id, outline_json, mindmap_json, source_trace_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                oid,
                session_id,
                json.dumps(outline, ensure_ascii=False),
                json.dumps(mindmap, ensure_ascii=False),
                json.dumps(source_trace, ensure_ascii=False),
                _now(),
            ),
        )
        self._conn.commit()

    def latest_outline(self, session_id: str) -> dict | None:
        """Return the most recent outline dict for *session_id*, or *None*."""
        row = self._conn.execute(
            "SELECT * FROM lesson_outlines WHERE session_id = ? ORDER BY created_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "outline": json.loads(row["outline_json"]),
            "mindmap": json.loads(row["mindmap_json"]),
            "source_trace": json.loads(row["source_trace_json"]),
            "created_at": row["created_at"],
        }

    # -- events --------------------------------------------------------

    def record_event(
        self,
        session_id: str,
        user_id: str,
        event_type: str,
        outline_node_id: str | None,
        payload: dict,
    ) -> str:
        """Append a lesson event (immutable — no updates)."""
        eid = _new_id("evt_")
        self._conn.execute(
            """INSERT INTO lesson_events
               (id, session_id, user_id, event_type, outline_node_id, payload_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                eid,
                session_id,
                user_id,
                event_type,
                outline_node_id,
                json.dumps(payload or {}, ensure_ascii=False),
                _now(),
            ),
        )
        self._conn.commit()
        return eid

    def record_event_once(
        self,
        event_id: str,
        session_id: str,
        user_id: str,
        event_type: str,
        outline_node_id: str | None,
        payload: dict,
    ) -> str:
        """Insert a deterministic event once and reject id collisions."""
        normalized_id = str(event_id or "").strip()
        if not normalized_id:
            raise ValueError("event_id must not be empty")
        self._conn.execute(
            """INSERT INTO lesson_events
               (id, session_id, user_id, event_type, outline_node_id, payload_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO NOTHING""",
            (
                normalized_id,
                session_id,
                user_id,
                event_type,
                outline_node_id,
                json.dumps(payload or {}, ensure_ascii=False),
                _now(),
            ),
        )
        self._conn.commit()
        stored = self.get_event(normalized_id)
        if stored is None or any(
            (
                stored["session_id"] != session_id,
                stored["user_id"] != user_id,
                stored["event_type"] != event_type,
            )
        ):
            raise ValueError(f"Event id conflicts with existing event: {normalized_id}")
        return normalized_id

    def get_event(
        self,
        event_id: str,
        user_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Return one learning event with parsed payload, or None."""
        if user_id:
            row = self._conn.execute(
                "SELECT * FROM lesson_events WHERE id = ? AND user_id = ?",
                (event_id, user_id),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT * FROM lesson_events WHERE id = ?",
                (event_id,),
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["payload"] = json.loads(d.pop("payload_json", "{}"))
        return d

    def event_exists(self, user_id: str, event_id: str) -> bool:
        """Return True when *event_id* belongs to *user_id*."""
        row = self._conn.execute(
            "SELECT 1 FROM lesson_events WHERE id = ? AND user_id = ? LIMIT 1",
            (event_id, user_id),
        ).fetchone()
        return row is not None

    def list_recent_events(
        self,
        user_id: str,
        session_id: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Return recent learning events for a user, newest last."""
        limit = max(1, min(int(limit), 100))
        if session_id:
            rows = self._conn.execute(
                """SELECT * FROM lesson_events
                   WHERE user_id = ? AND session_id = ?
                   ORDER BY created_at DESC
                   LIMIT ?""",
                (user_id, session_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT * FROM lesson_events
                   WHERE user_id = ?
                   ORDER BY created_at DESC
                   LIMIT ?""",
                (user_id, limit),
            ).fetchall()
        events: list[dict[str, Any]] = []
        for row in reversed(rows):
            d = dict(row)
            d["payload"] = json.loads(d.pop("payload_json", "{}"))
            events.append(d)
        return events

    def list_events(
        self,
        user_id: str,
        session_id: str | None = None,
        surface: str | None = None,
        event_types: list[str] | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Return learning events for consolidation, oldest first."""
        limit = max(1, min(int(limit), 2000))
        clauses = ["user_id = ?"]
        params: list[Any] = [user_id]
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if event_types:
            placeholders = ",".join("?" for _ in event_types)
            clauses.append(f"event_type IN ({placeholders})")
            params.extend(event_types)
        params.append(limit)
        rows = self._conn.execute(
            f"""SELECT * FROM lesson_events
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at ASC
                LIMIT ?""",
            params,
        ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            d = dict(row)
            d["payload"] = json.loads(d.pop("payload_json", "{}"))
            if surface and d.get("payload", {}).get("surface") != surface:
                continue
            events.append(d)
        return events

    # -- learner memory ------------------------------------------------

    def get_learner_memory_slot(self, user_id: str, slot: str) -> dict[str, Any]:
        """Return one L3 learner-memory slot as JSON data."""
        row = self._conn.execute(
            "SELECT data_json FROM learner_memory WHERE user_id = ? AND slot = ?",
            (user_id, slot),
        ).fetchone()
        if row is None:
            return {"items": []}
        try:
            data = json.loads(row["data_json"])
        except json.JSONDecodeError:
            return {"items": []}
        return data if isinstance(data, dict) else {"items": []}

    def save_learner_memory_slot(
        self,
        user_id: str,
        slot: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist one L3 learner-memory slot."""
        self.ensure_user(user_id)
        payload = data if isinstance(data, dict) else {"items": []}
        self._conn.execute(
            """INSERT OR REPLACE INTO learner_memory
               (user_id, slot, data_json, updated_at)
               VALUES (?, ?, ?, ?)""",
            (user_id, slot, json.dumps(payload, ensure_ascii=False), _now()),
        )
        self._conn.commit()
        return payload

    def get_learner_memory(self, user_id: str) -> dict[str, Any]:
        """Return all L3 learner-memory slots for a user."""
        rows = self._conn.execute(
            "SELECT slot, data_json, updated_at FROM learner_memory WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        result: dict[str, Any] = {}
        for row in rows:
            try:
                data = json.loads(row["data_json"])
            except json.JSONDecodeError:
                data = {"items": []}
            if isinstance(data, dict):
                data.setdefault("updated_at", row["updated_at"])
            result[row["slot"]] = data
        return result

    # -- L2 learning summaries ----------------------------------------

    def append_learning_summary(
        self,
        user_id: str,
        surface: str,
        summary: dict[str, Any],
        source_event_ids: list[str],
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Append a per-surface L2 learning summary."""
        sid = _new_id("sum_")
        now = _now()
        source_ids = [str(eid) for eid in source_event_ids if str(eid).strip()]
        self._conn.execute(
            """INSERT INTO learning_summaries
               (id, user_id, session_id, surface, summary_json,
                source_event_ids_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                sid,
                user_id,
                session_id,
                surface,
                json.dumps(summary or {}, ensure_ascii=False),
                json.dumps(source_ids, ensure_ascii=False),
                now,
            ),
        )
        self._conn.commit()
        return {
            "id": sid,
            "user_id": user_id,
            "session_id": session_id,
            "surface": surface,
            "summary": summary or {},
            "source_event_ids": source_ids,
            "created_at": now,
        }

    def list_learning_summaries(
        self,
        user_id: str,
        session_id: str | None = None,
        surface: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return recent L2 summaries, newest last."""
        limit = max(1, min(int(limit), 200))
        clauses = ["user_id = ?"]
        params: list[Any] = [user_id]
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if surface:
            clauses.append("surface = ?")
            params.append(surface)
        params.append(limit)
        rows = self._conn.execute(
            f"""SELECT * FROM learning_summaries
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at DESC
                LIMIT ?""",
            params,
        ).fetchall()
        summaries: list[dict[str, Any]] = []
        for row in reversed(rows):
            d = dict(row)
            d["summary"] = json.loads(d.pop("summary_json", "{}"))
            d["source_event_ids"] = json.loads(d.pop("source_event_ids_json", "[]"))
            summaries.append(d)
        return summaries

    # -- consolidation metadata ---------------------------------------

    def get_consolidation_seen_ids(
        self,
        user_id: str,
        layer: str,
        key: str,
    ) -> set[str]:
        """Return upstream ids already processed by a consolidation pass."""
        row = self._conn.execute(
            """SELECT seen_ids_json FROM memory_consolidation_meta
               WHERE user_id = ? AND layer = ? AND key = ?""",
            (user_id, layer, key),
        ).fetchone()
        if row is None:
            return set()
        try:
            ids = json.loads(row["seen_ids_json"])
        except json.JSONDecodeError:
            return set()
        if not isinstance(ids, list):
            return set()
        return {str(item) for item in ids if str(item).strip()}

    def save_consolidation_seen_ids(
        self,
        user_id: str,
        layer: str,
        key: str,
        seen_ids: set[str] | list[str],
    ) -> None:
        """Persist upstream ids already processed by a consolidation pass."""
        self.ensure_user(user_id)
        payload = sorted({str(item) for item in seen_ids if str(item).strip()})
        self._conn.execute(
            """INSERT OR REPLACE INTO memory_consolidation_meta
               (user_id, layer, key, seen_ids_json, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                user_id,
                layer,
                key,
                json.dumps(payload, ensure_ascii=False),
                _now(),
            ),
        )
        self._conn.commit()

    # -- answer records ------------------------------------------------

    def record_answer(
        self,
        session_id: str,
        user_id: str,
        question_id: str,
        outline_node_id: str | None,
        user_answer: str,
        grading: dict[str, Any],
    ) -> None:
        """Persist an answer record."""
        aid = _new_id("ans_")
        is_correct = int(bool(grading.get("is_correct", False)))
        score = float(grading.get("score", 0.0))
        self._conn.execute(
            """INSERT INTO answer_records
               (id, session_id, user_id, question_id, outline_node_id,
                user_answer, grading_json, is_correct, score, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                aid,
                session_id,
                user_id,
                question_id,
                outline_node_id,
                user_answer,
                json.dumps(grading, ensure_ascii=False),
                is_correct,
                score,
                _now(),
            ),
        )
        self._conn.commit()

    def list_answer_records(self, session_id: str) -> list[dict[str, Any]]:
        """Return all answer records for *session_id* with parsed grading."""
        rows = self._conn.execute(
            "SELECT * FROM answer_records WHERE session_id = ? ORDER BY created_at ASC",
            (session_id,),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["grading"] = json.loads(d.pop("grading_json", "{}"))
            result.append(d)
        return result

    def list_answer_records_for_user(
        self,
        user_id: str,
        *,
        start_at: str | None = None,
        end_at: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Return answer records for one user, optionally constrained by UTC ISO time."""
        limit = max(1, min(int(limit), 1000))
        clauses = ["user_id = ?"]
        params: list[Any] = [user_id]
        if start_at:
            clauses.append("created_at >= ?")
            params.append(start_at)
        if end_at:
            clauses.append("created_at < ?")
            params.append(end_at)
        params.append(limit)
        rows = self._conn.execute(
            f"""SELECT * FROM answer_records
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at ASC
                LIMIT ?""",
            params,
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["grading"] = json.loads(item.pop("grading_json", "{}"))
            result.append(item)
        return result

    # -- knowledge tracing ---------------------------------------------

    def get_knowledge_trace_state(
        self,
        user_id: str,
        kg_node_id: str,
    ) -> dict[str, Any] | None:
        """Return the BKT/DKT-compatible state for one KG node, if present."""
        kg_id = str(kg_node_id or "").strip()
        if not kg_id:
            return None
        row = self._conn.execute(
            """SELECT * FROM knowledge_tracing_states
               WHERE user_id = ? AND kg_node_id = ?""",
            (user_id, kg_id),
        ).fetchone()
        if row is None:
            return None
        return self._knowledge_trace_row_to_dict(row)

    def list_knowledge_trace_states(
        self,
        user_id: str,
        *,
        kg_node_ids: list[str] | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Return recent knowledge-tracing states for a learner."""
        self.ensure_user(user_id)
        limit = max(1, min(int(limit), 1000))
        ids = _json_list(kg_node_ids or [])
        params: list[Any] = [user_id]
        clauses = ["user_id = ?"]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            clauses.append(f"kg_node_id IN ({placeholders})")
            params.extend(ids)
        params.append(limit)
        rows = self._conn.execute(
            f"""SELECT * FROM knowledge_tracing_states
                WHERE {' AND '.join(clauses)}
                ORDER BY updated_at DESC
                LIMIT ?""",
            params,
        ).fetchall()
        return [self._knowledge_trace_row_to_dict(row) for row in rows]

    def save_knowledge_trace_state(
        self,
        user_id: str,
        kg_node_id: str,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist one KG-node cognitive state estimated by a tracing model."""
        self.ensure_user(user_id)
        kg_id = str(kg_node_id or "").strip()
        if not kg_id:
            raise ValueError("kg_node_id is required")
        now = _now()
        params = state.get("parameters")
        trace = state.get("trace")
        self._conn.execute(
            """INSERT OR REPLACE INTO knowledge_tracing_states
               (user_id, kg_node_id, skill_label, p_mastery, uncertainty,
                confidence, evidence_count, correct_count, incorrect_count,
                last_observation_correct, parameters_json, trace_json, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                kg_id,
                str(state.get("skill_label") or kg_id).strip()[:180],
                float(state.get("p_mastery", 0.0)),
                float(state.get("uncertainty", 1.0)),
                float(state.get("confidence", 0.0)),
                int(state.get("evidence_count", 0)),
                int(state.get("correct_count", 0)),
                int(state.get("incorrect_count", 0)),
                (
                    1
                    if state.get("last_observation_correct") is True
                    else 0
                    if state.get("last_observation_correct") is False
                    else None
                ),
                json.dumps(params if isinstance(params, dict) else {}, ensure_ascii=False),
                json.dumps(trace if isinstance(trace, dict) else {}, ensure_ascii=False),
                now,
            ),
        )
        self._conn.commit()
        saved = self.get_knowledge_trace_state(user_id, kg_id)
        return saved if saved is not None else {}

    def _knowledge_trace_row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        try:
            item["parameters"] = json.loads(item.pop("parameters_json", "{}"))
        except json.JSONDecodeError:
            item["parameters"] = {}
        try:
            item["trace"] = json.loads(item.pop("trace_json", "{}"))
        except json.JSONDecodeError:
            item["trace"] = {}
        marker = item.get("last_observation_correct")
        item["last_observation_correct"] = (
            True if marker == 1 else False if marker == 0 else None
        )
        return item

    # -- mastery records ------------------------------------------------

    def upsert_mastery(
        self,
        user_id: str,
        session_id: str,
        concept_name: str,
        mastery_delta: float,
        kg_node_id: str | None = None,
        evidence: dict[str, Any] | None = None,
        confidence_delta: float = 0.05,
        exposure_increment: int = 0,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Insert or update a mastery record.

        Looks up the latest record matching (user_id, session_id, kg_node_id,
        concept_name).  If found, updates it; otherwise inserts a new one.

        ``mastery_score`` is clamped to [0.0, 1.0].
        ``confidence`` is clamped to [0.0, 1.0].

        Parameters
        ----------
        exposure_increment:
            Number to add to exposure_count (default 0).  Use 1 when recording
            a tutor exposure event — the tutor "taught" this KG node.
        """
        # Try to find an existing record
        existing_row = self._conn.execute(
            """SELECT * FROM mastery_records
               WHERE user_id = ? AND session_id = ?
                 AND (kg_node_id = ? OR (kg_node_id IS NULL AND ? IS NULL))
                 AND concept_name = ?
               ORDER BY updated_at DESC LIMIT 1""",
            (user_id, session_id, kg_node_id, kg_node_id, concept_name),
        ).fetchone()

        if existing_row:
            existing = dict(existing_row)
            if idempotency_key:
                existing_evidence = json.loads(existing.get("evidence_json") or "{}")
                if existing_evidence.get("idempotency_key") == idempotency_key:
                    return self._mastery_row_to_dict(existing["id"])
            new_score = max(0.0, min(1.0, existing["mastery_score"] + mastery_delta))
            new_confidence = max(
                0.0, min(1.0, existing["confidence"] + confidence_delta)
            )
            new_exposure = existing.get("exposure_count", 0) + exposure_increment
            self._conn.execute(
                """UPDATE mastery_records
                   SET mastery_score = ?, confidence = ?,
                       exposure_count = ?, evidence_json = ?,
                       updated_at = ?
                   WHERE id = ?""",
                (
                    new_score,
                    new_confidence,
                    new_exposure,
                    json.dumps(
                        {**(evidence or {}), **({"idempotency_key": idempotency_key} if idempotency_key else {})},
                        ensure_ascii=False,
                    ),
                    _now(),
                    existing["id"],
                ),
            )
            self._conn.commit()
            return self._mastery_row_to_dict(existing["id"])
        else:
            mid = _new_id("mst_")
            new_score = max(0.0, min(1.0, 0.3 + mastery_delta))
            new_confidence = max(0.0, min(1.0, 0.3 + confidence_delta))
            new_exposure = exposure_increment
            self._conn.execute(
                """INSERT INTO mastery_records
                   (id, user_id, session_id, kg_node_id, concept_name,
                    mastery_score, confidence, exposure_count, evidence_json, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    mid,
                    user_id,
                    session_id,
                    kg_node_id,
                    concept_name,
                    new_score,
                    new_confidence,
                    new_exposure,
                    json.dumps(
                        {**(evidence or {}), **({"idempotency_key": idempotency_key} if idempotency_key else {})},
                        ensure_ascii=False,
                    ),
                    _now(),
                ),
            )
            self._conn.commit()
            return self._mastery_row_to_dict(mid)

    def _mastery_row_to_dict(self, mid: str) -> dict[str, Any]:
        """Read a mastery record by id and return it as a dict."""
        row = self._conn.execute(
            "SELECT * FROM mastery_records WHERE id = ?", (mid,)
        ).fetchone()
        if row is None:
            return {}
        d = dict(row)
        d["evidence"] = json.loads(d.pop("evidence_json", "{}"))
        return d

    def get_mastery_by_kg_node(
        self,
        user_id: str,
        session_id: str,
    ) -> dict[str, dict[str, Any]]:
        """Return mastery records keyed by ``kg_node_id``.

        Returns a dict mapping ``kg_node_id -> mastery_record`` (with
        ``evidence`` parsed from JSON).  Records with ``kg_node_id=NULL``
        are keyed under the special string ``"__no_kg_node__"``.
        """
        rows = self._conn.execute(
            """SELECT * FROM mastery_records
               WHERE user_id = ? AND session_id = ?
               ORDER BY updated_at DESC""",
            (user_id, session_id),
        ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for r in rows:
            d = dict(r)
            d["evidence"] = json.loads(d.pop("evidence_json", "{}"))
            key = d.get("kg_node_id") or "__no_kg_node__"
            # Keep only the latest per kg_node_id (rows are DESC by updated_at)
            if key not in result:
                result[key] = d
        return result

    def record_tutor_exposure(
        self,
        user_id: str,
        session_id: str,
        outline_node_id: str,
        kg_node_ids: list[str],
        handout_section_title: str,
        concept_names: list[str] | None = None,
    ) -> tuple[list[dict[str, Any]], str]:
        """Record a tutor exposure event and increment exposure_count.

        For each KG node in *kg_node_ids*, increments ``exposure_count`` by 1
        and adds a small mastery bump (default +0.02 — "讲过" is weak evidence).

        Does **not** advance ``current_outline_node_id``.

        Parameters
        ----------
        user_id:
            The user id.
        session_id:
            The session id.
        outline_node_id:
            The outline node being taught.
        kg_node_ids:
            KG node ids the tutor covered in this exposure.
        handout_section_title:
            The handout section title used by the tutor.
        concept_names:
            Optional per-kg-node concept names. If shorter than kg_node_ids,
            falls back to ``handout_section_title`` for the rest.

        Returns
        -------
        (mastery_records, event_id)
            *mastery_records* is the list of updated mastery dicts.
            *event_id* is the exposure event id.
        """
        concept_names = concept_names or []
        mastery_records: list[dict[str, Any]] = []

        for i, kg_id in enumerate(kg_node_ids):
            concept = (
                concept_names[i] if i < len(concept_names) else handout_section_title
            )
            m_rec = self.upsert_mastery(
                user_id=user_id,
                session_id=session_id,
                concept_name=concept,
                mastery_delta=0.02,  # small bump for exposure only
                kg_node_id=kg_id,
                evidence={
                    "event": "tutor_exposure",
                    "outline_node_id": outline_node_id,
                    "handout_section_title": handout_section_title,
                },
                confidence_delta=0.03,
                exposure_increment=1,
            )
            mastery_records.append(m_rec)

        # Record the exposure event
        eid = _new_id("evt_")
        self._conn.execute(
            """INSERT INTO lesson_events
               (id, session_id, user_id, event_type, outline_node_id, payload_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                eid,
                session_id,
                user_id,
                "tutor_exposure",
                outline_node_id,
                json.dumps({
                    "surface": "lecture",
                    "kg_node_ids": kg_node_ids,
                    "handout_section_title": handout_section_title,
                    "mastery_records_count": len(mastery_records),
                }, ensure_ascii=False),
                _now(),
            ),
        )
        self._conn.commit()

        return mastery_records, eid

    # -- Direct QA mastery records ------------------------------------

    def upsert_direct_qa_mastery(
        self,
        user_id: str,
        concept_name: str,
        mastery_delta: float,
        kg_node_id: str | None = None,
        evidence: dict[str, Any] | None = None,
        confidence_delta: float = 0.05,
        source_ref: str | None = None,
    ) -> dict[str, Any]:
        """Insert or update a Direct QA mastery record.

        Looks up the latest record matching (user_id, kg_node_id, concept_name).
        If found, updates it; otherwise inserts a new one.

        ``mastery_score`` is clamped to [0.0, 1.0].
        ``confidence`` is clamped to [0.0, 1.0].
        ``source`` is always ``'direct_qa'``.

        This is **physically isolated** from course ``mastery_records`` — it
        writes to ``direct_qa_mastery_records`` only and never touches
        course session-scoped mastery rows.

        Parameters
        ----------
        user_id:
            The user id.
        concept_name:
            Human-readable concept label (required).
        mastery_delta:
            Signed delta to apply to mastery_score (e.g. -0.03 for confusion).
        kg_node_id:
            Optional KG node id.  When omitted the record is scoped to
            concept_name alone.
        evidence:
            Arbitrary evidence dict (question, resolved_question, KAG results,
            etc.).  Stored as JSON.
        confidence_delta:
            Signed delta to apply to confidence (default 0.05).
        source_ref:
            Opaque reference for upstream traceability (e.g. a Direct QA
            conversation / event id).  Optional.
        """
        # Try to find an existing record
        existing_row = self._conn.execute(
            """SELECT * FROM direct_qa_mastery_records
               WHERE user_id = ?
                 AND (kg_node_id = ? OR (kg_node_id IS NULL AND ? IS NULL))
                 AND concept_name = ?
               ORDER BY updated_at DESC LIMIT 1""",
            (user_id, kg_node_id, kg_node_id, concept_name),
        ).fetchone()

        if existing_row:
            existing = dict(existing_row)
            new_score = max(0.0, min(1.0, existing["mastery_score"] + mastery_delta))
            new_confidence = max(
                0.0, min(1.0, existing["confidence"] + confidence_delta)
            )
            self._conn.execute(
                """UPDATE direct_qa_mastery_records
                   SET mastery_score = ?, confidence = ?,
                       source_ref = ?, evidence_json = ?,
                       updated_at = ?
                   WHERE id = ?""",
                (
                    new_score,
                    new_confidence,
                    source_ref,
                    json.dumps(evidence or {}, ensure_ascii=False),
                    _now(),
                    existing["id"],
                ),
            )
            self._conn.commit()
            return self._direct_qa_mastery_row_to_dict(existing["id"])
        else:
            mid = _new_id("dqa_")
            new_score = max(0.0, min(1.0, 0.3 + mastery_delta))
            new_confidence = max(0.0, min(1.0, 0.3 + confidence_delta))
            self._conn.execute(
                """INSERT INTO direct_qa_mastery_records
                   (id, user_id, kg_node_id, concept_name,
                    mastery_score, confidence, source, source_ref,
                    evidence_json, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'direct_qa', ?, ?, ?)""",
                (
                    mid,
                    user_id,
                    kg_node_id,
                    concept_name,
                    new_score,
                    new_confidence,
                    source_ref,
                    json.dumps(evidence or {}, ensure_ascii=False),
                    _now(),
                ),
            )
            self._conn.commit()
            return self._direct_qa_mastery_row_to_dict(mid)

    def _direct_qa_mastery_row_to_dict(self, mid: str) -> dict[str, Any]:
        """Read a Direct QA mastery record by id and return it as a dict."""
        row = self._conn.execute(
            "SELECT * FROM direct_qa_mastery_records WHERE id = ?", (mid,)
        ).fetchone()
        if row is None:
            return {}
        d = dict(row)
        d["evidence"] = json.loads(d.pop("evidence_json", "{}"))
        return d

    def get_direct_qa_mastery_by_user(
        self,
        user_id: str,
        kg_node_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return all Direct QA mastery records for *user_id*, newest first.

        When *kg_node_id* is provided the result is filtered to that single
        KG node.

        Each record includes ``evidence`` parsed from JSON.
        """
        if kg_node_id:
            rows = self._conn.execute(
                """SELECT * FROM direct_qa_mastery_records
                   WHERE user_id = ? AND kg_node_id = ?
                   ORDER BY updated_at DESC""",
                (user_id, kg_node_id),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT * FROM direct_qa_mastery_records
                   WHERE user_id = ?
                   ORDER BY updated_at DESC""",
                (user_id,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["evidence"] = json.loads(d.pop("evidence_json", "{}"))
            result.append(d)
        return result

    # -- summary -------------------------------------------------------

    def session_summary(self, session_id: str) -> dict:
        """Return a rich summary dict for a session.

        Includes the session row, user profile, latest outline, all events
        (with ``payload`` parsed from JSON), answer records, and mastery
        records (with ``evidence`` parsed from JSON).
        """
        session = self.get_session(session_id)

        profile_row = self._conn.execute(
            "SELECT * FROM user_profiles WHERE user_id = ?", (session["user_id"],)
        ).fetchone()
        if profile_row:
            profile = json.loads(profile_row["profile_json"])
            profile["confidence"] = profile_row["confidence"]
        else:
            profile = dict(DEFAULT_PROFILE)

        outline = self.latest_outline(session_id)

        event_rows = self._conn.execute(
            "SELECT * FROM lesson_events WHERE session_id = ? ORDER BY created_at ASC",
            (session_id,),
        ).fetchall()
        events = []
        for er in event_rows:
            d = dict(er)
            d["payload"] = json.loads(d.pop("payload_json", "{}"))
            events.append(d)

        mastery_rows = self._conn.execute(
            "SELECT * FROM mastery_records WHERE session_id = ? ORDER BY updated_at DESC",
            (session_id,),
        ).fetchall()
        mastery = []
        for mr in mastery_rows:
            md = dict(mr)
            md["evidence"] = json.loads(md.pop("evidence_json", "{}"))
            mastery.append(md)

        answers = self.list_answer_records(session_id)
        learner_memory = self.get_learner_memory(session["user_id"])
        learning_summaries = self.list_learning_summaries(
            session["user_id"],
            session_id=session_id,
            limit=50,
        )

        return {
            "session": session,
            "profile": profile,
            "outline": outline,
            "events": events,
            "mastery": mastery,
            "answers": answers,
            "learner_memory": learner_memory,
            "learning_summaries": learning_summaries,
        }
