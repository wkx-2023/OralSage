"""SQLite persistence for standalone clinical speaking sessions."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_DB_PATH = Path("data/learning/clinical_speaking.sqlite3")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class ClinicalSpeakingStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or DEFAULT_DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS clinical_speaking_sessions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_clinical_speaking_user "
                "ON clinical_speaking_sessions (user_id, updated_at DESC)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS clinical_speaking_blueprints (
                    diagnosis TEXT NOT NULL,
                    evidence_signature TEXT NOT NULL,
                    blueprint_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (diagnosis, evidence_signature)
                )
                """
            )
            conn.commit()

    def create(self, state: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        state = {**state, "created_at": now, "updated_at": now}
        with self._lock, closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO clinical_speaking_sessions "
                "(id, user_id, status, state_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    state["id"],
                    state["user_id"],
                    state["status"],
                    json.dumps(state, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            conn.commit()
        return state

    def get(self, session_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT state_json FROM clinical_speaking_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row["state_json"])

    def save(self, state: dict[str, Any]) -> dict[str, Any]:
        state = {**state, "updated_at": _now()}
        with self._lock, closing(self._connect()) as conn:
            cursor = conn.execute(
                "UPDATE clinical_speaking_sessions "
                "SET status = ?, state_json = ?, updated_at = ? WHERE id = ?",
                (
                    state["status"],
                    json.dumps(state, ensure_ascii=False),
                    state["updated_at"],
                    state["id"],
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(state["id"])
            conn.commit()
        return state

    def list_for_user(self, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT state_json FROM clinical_speaking_sessions "
                "WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
                (user_id, max(1, min(limit, 100))),
            ).fetchall()
        return [json.loads(row["state_json"]) for row in rows]

    def get_blueprint(self, diagnosis: str, evidence_signature: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT blueprint_json FROM clinical_speaking_blueprints "
                "WHERE diagnosis = ? AND evidence_signature = ?",
                (diagnosis, evidence_signature),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row["blueprint_json"])

    def save_blueprint(
        self,
        diagnosis: str,
        evidence_signature: str,
        blueprint: dict[str, Any],
    ) -> None:
        with self._lock, closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO clinical_speaking_blueprints "
                "(diagnosis, evidence_signature, blueprint_json, created_at) VALUES (?, ?, ?, ?)",
                (
                    diagnosis,
                    evidence_signature,
                    json.dumps(blueprint, ensure_ascii=False),
                    _now(),
                ),
            )
            conn.commit()
