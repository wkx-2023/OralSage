"""SQLite learning memory layer — persists users, profiles, sessions, outlines,
events, resources, mastery and answer records.

All JSON columns are stored via ``json.dumps(..., ensure_ascii=False)`` and read
back with ``json.loads``.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
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
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


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
        self._conn = sqlite3.connect(str(self.db_path))
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
        row = self._conn.execute(
            "SELECT * FROM user_profiles WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row:
            profile = json.loads(row["profile_json"])
            profile["confidence"] = row["confidence"]
            return profile
        return self.save_profile(user_id, dict(DEFAULT_PROFILE))

    def save_profile(self, user_id: str, profile: dict) -> dict:
        """Persist a profile dict.  ``confidence`` is extracted to its own column."""
        profile = {**profile}
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

    # -- session -------------------------------------------------------

    def create_session(
        self, user_id: str, topic: str, mode: str = "course_learning"
    ) -> dict:
        """Create a new learning session and return it as a dict."""
        self.ensure_user(user_id)
        sid = _new_id("lesson_")
        now = _now()
        self._conn.execute(
            """INSERT INTO learning_sessions
               (id, user_id, topic, mode, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'active', ?, ?)""",
            (sid, user_id, topic, mode, now, now),
        )
        self._conn.commit()
        return self.get_session(sid)

    def get_session(self, session_id: str) -> dict:
        """Return a session dict.  Raises ``KeyError`` when not found."""
        row = self._conn.execute(
            "SELECT * FROM learning_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Session not found: {session_id}")
        return dict(row)

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
    ) -> None:
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
                    json.dumps(evidence or {}, ensure_ascii=False),
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
                    json.dumps(evidence or {}, ensure_ascii=False),
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
                    "kg_node_ids": kg_node_ids,
                    "handout_section_title": handout_section_title,
                    "mastery_records_count": len(mastery_records),
                }, ensure_ascii=False),
                _now(),
            ),
        )
        self._conn.commit()

        return mastery_records, eid

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

        return {
            "session": session,
            "profile": profile,
            "outline": outline,
            "events": events,
            "mastery": mastery,
            "answers": answers,
        }
