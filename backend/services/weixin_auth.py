"""Lightweight web-session and Weixin binding storage for OralSage ClawBridge."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


DEFAULT_SESSION_COOKIE = "oralsage_session"
DEFAULT_SESSION_TTL_HOURS = 24 * 30
PASSWORD_HASH_ITERATIONS = 200_000


class WeixinAuthError(ValueError):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def default_db_path() -> Path:
    configured = os.getenv("ORALSAGE_WEIXIN_AUTH_DB", "").strip()
    if configured:
        return Path(configured)
    return Path("data/weixin/weixin_auth.sqlite3")


def session_cookie_name() -> str:
    return os.getenv("ORALSAGE_SESSION_COOKIE_NAME", DEFAULT_SESSION_COOKIE).strip() or DEFAULT_SESSION_COOKIE


def session_ttl_hours() -> int:
    raw = os.getenv("ORALSAGE_SESSION_TTL_HOURS", "").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_SESSION_TTL_HOURS


def normalize_weixin_account_id(account_id: str | None) -> str:
    value = str(account_id or "").strip()
    if not value:
        return ""
    if value.endswith("@im.bot"):
        return f"{value[:-7]}-im-bot"
    if value.endswith("@im.wechat"):
        return f"{value[:-10]}-im-wechat"
    return "".join(ch if ch.isalnum() or ch in {"_", "-"} else "-" for ch in value)


def equivalent_weixin_account_ids(account_id: str | None) -> list[str]:
    value = str(account_id or "").strip()
    if not value:
        return []
    candidates = {value}
    normalized = normalize_weixin_account_id(value)
    if normalized:
        candidates.add(normalized)
    if value.endswith("-im-bot"):
        candidates.add(f"{value[:-7]}@im.bot")
    if value.endswith("-im-wechat"):
        candidates.add(f"{value[:-10]}@im.wechat")
    return sorted(candidates)


def build_clawbot_instance_id(account_id: str | None) -> str:
    normalized = normalize_weixin_account_id(account_id)
    return f"clawbridge:{normalized}" if normalized else ""


class WeixinAuthStore:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS web_users (
                    user_id TEXT PRIMARY KEY,
                    display_name TEXT,
                    password_hash TEXT,
                    role TEXT NOT NULL DEFAULT 'user',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS web_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS weixin_bind_sessions (
                    session_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    qr_data_url TEXT,
                    message TEXT,
                    weixin_account_id TEXT,
                    weixin_user_id TEXT,
                    clawbot_instance_id TEXT,
                    binding_revision TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS weixin_bindings (
                    user_id TEXT PRIMARY KEY,
                    weixin_account_id TEXT NOT NULL,
                    weixin_user_id TEXT NOT NULL,
                    clawbot_instance_id TEXT NOT NULL,
                    binding_revision TEXT NOT NULL,
                    bound_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_weixin_binding_identity
                ON weixin_bindings (weixin_account_id, weixin_user_id);

                CREATE UNIQUE INDEX IF NOT EXISTS idx_weixin_binding_instance
                ON weixin_bindings (clawbot_instance_id);

                CREATE INDEX IF NOT EXISTS idx_web_sessions_expires
                ON web_sessions (expires_at);
                """
            )
            self._ensure_column(conn, "web_users", "password_hash", "TEXT")
            self._ensure_column(conn, "web_users", "role", "TEXT NOT NULL DEFAULT 'user'")

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def register(self, *, username: str, password: str, display_name: str | None = None) -> tuple[dict[str, Any], str]:
        normalized_user_id = self._normalize_username(username)
        normalized_display_name = str(display_name or normalized_user_id).strip() or normalized_user_id
        password_hash = self._hash_password(self._validate_new_password(password))
        current = utc_now()
        role = "admin" if self._registered_user_count() == 0 else "user"
        with self._connect() as conn:
            existing = conn.execute("SELECT * FROM web_users WHERE user_id = ?", (normalized_user_id,)).fetchone()
            if existing and str(existing["password_hash"] or "").strip():
                raise WeixinAuthError("Username is already taken.", status_code=409)
            if existing:
                conn.execute(
                    """
                    UPDATE web_users
                    SET display_name = ?, password_hash = ?, role = ?, updated_at = ?
                    WHERE user_id = ?
                    """,
                    (normalized_display_name, password_hash, role, current, normalized_user_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO web_users (user_id, display_name, password_hash, role, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (normalized_user_id, normalized_display_name, password_hash, role, current, current),
                )
        return self._create_session(normalized_user_id)

    def login(self, *, username: str, password: str) -> tuple[dict[str, Any], str]:
        normalized_user_id = self._normalize_username(username)
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM web_users WHERE user_id = ?", (normalized_user_id,)).fetchone()
        if not row or not self._verify_password(password, str(row["password_hash"] or "")):
            raise WeixinAuthError("Invalid username or password.", status_code=401)
        return self._create_session(normalized_user_id)

    def _create_session(self, user_id: str) -> tuple[dict[str, Any], str]:
        current = utc_now()
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=session_ttl_hours())).replace(microsecond=0).isoformat()
        token = secrets.token_urlsafe(32)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO web_sessions (token_hash, user_id, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (self.token_hash(token), user_id, current, expires_at),
            )
        user = self.get_user(user_id)
        if not user:
            raise WeixinAuthError("User not found.", status_code=404)
        return user, token

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM web_users WHERE user_id = ?", (user_id,)).fetchone()
        return self._row_to_user(row) if row else None

    def get_user_by_session(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        current = utc_now()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT u.* FROM web_sessions s
                JOIN web_users u ON u.user_id = s.user_id
                WHERE s.token_hash = ? AND s.expires_at > ?
                """,
                (self.token_hash(token), current),
            ).fetchone()
        return self._row_to_user(row) if row else None

    def _registered_user_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM web_users WHERE password_hash IS NOT NULL AND password_hash <> ''").fetchone()
        return int(row["count"] if row else 0)

    @staticmethod
    def _normalize_username(username: str) -> str:
        normalized = str(username or "").strip()
        if not (3 <= len(normalized) <= 32):
            raise WeixinAuthError("Username must be 3 to 32 characters.", status_code=422)
        if any(not (ch.isascii() and (ch.isalnum() or ch in {"_", "-"})) for ch in normalized):
            raise WeixinAuthError("Username can only contain letters, numbers, underscores, or hyphens.", status_code=422)
        return normalized

    @staticmethod
    def _validate_new_password(password: str) -> str:
        value = str(password or "")
        if not (8 <= len(value) <= 200):
            raise WeixinAuthError("Password must be at least 8 characters.", status_code=422)
        return value

    @staticmethod
    def _hash_password(password: str) -> str:
        salt = secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), PASSWORD_HASH_ITERATIONS)
        return f"pbkdf2_sha256${PASSWORD_HASH_ITERATIONS}${salt}${digest.hex()}"

    @staticmethod
    def _verify_password(password: str, password_hash: str) -> bool:
        try:
            algorithm, iterations_raw, salt, digest = password_hash.split("$", 3)
        except ValueError:
            return False
        if algorithm != "pbkdf2_sha256":
            return False
        try:
            iterations = int(iterations_raw)
        except ValueError:
            return False
        candidate = hashlib.pbkdf2_hmac("sha256", str(password or "").encode("utf-8"), salt.encode("utf-8"), iterations).hex()
        return hmac.compare_digest(candidate, digest)

    @staticmethod
    def _row_to_user(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        return {
            "user_id": data["user_id"],
            "username": data["user_id"],
            "display_name": data.get("display_name") or data["user_id"],
            "role": data.get("role") or "user",
            "created_at": data.get("created_at"),
            "updated_at": data.get("updated_at"),
        }

    def logout(self, token: str | None) -> None:
        if not token:
            return
        with self._connect() as conn:
            conn.execute("DELETE FROM web_sessions WHERE token_hash = ?", (self.token_hash(token),))

    def create_bind_session(
        self,
        *,
        user_id: str,
        session_id: str,
        qr_data_url: str,
        message: str,
        ttl_minutes: int = 10,
    ) -> dict[str, Any]:
        current = utc_now()
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=max(1, ttl_minutes))).replace(microsecond=0).isoformat()
        with self._connect() as conn:
            conn.execute("DELETE FROM weixin_bind_sessions WHERE user_id = ? AND status IN ('waiting', 'expired', 'error')", (user_id,))
            conn.execute(
                """
                INSERT INTO weixin_bind_sessions (
                    session_id, user_id, status, qr_data_url, message,
                    created_at, updated_at, expires_at
                )
                VALUES (?, ?, 'waiting', ?, ?, ?, ?, ?)
                """,
                (session_id, user_id, qr_data_url, message, current, current, expires_at),
            )
        return self.get_bind_session(session_id) or {}

    def get_bind_session(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM weixin_bind_sessions WHERE session_id = ?", (session_id,)).fetchone()
        return dict(row) if row else None

    def update_bind_session(self, session_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        if not updates:
            return self.get_bind_session(session_id)
        payload = {**updates, "updated_at": utc_now()}
        assignments = ", ".join(f"{key} = ?" for key in payload)
        values = list(payload.values())
        values.append(session_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE weixin_bind_sessions SET {assignments} WHERE session_id = ?", values)
        return self.get_bind_session(session_id)

    def bind_weixin_identity(
        self,
        *,
        user_id: str,
        weixin_account_id: str,
        weixin_user_id: str,
        clawbot_instance_id: str | None = None,
    ) -> dict[str, Any]:
        account_id = str(weixin_account_id or "").strip()
        wx_user_id = str(weixin_user_id or "").strip()
        instance_id = str(clawbot_instance_id or "").strip() or build_clawbot_instance_id(account_id)
        if not account_id or not wx_user_id or not instance_id:
            raise ValueError("Weixin identity is incomplete.")
        current = utc_now()
        revision = uuid.uuid4().hex
        account_ids = equivalent_weixin_account_ids(account_id)
        placeholders = ",".join("?" for _ in account_ids)
        with self._connect() as conn:
            if account_ids:
                conn.execute(
                    f"DELETE FROM weixin_bindings WHERE user_id <> ? AND weixin_user_id = ? AND weixin_account_id IN ({placeholders})",
                    [user_id, wx_user_id, *account_ids],
                )
            conn.execute(
                "DELETE FROM weixin_bindings WHERE user_id <> ? AND clawbot_instance_id = ?",
                (user_id, instance_id),
            )
            conn.execute(
                """
                INSERT INTO weixin_bindings (
                    user_id, weixin_account_id, weixin_user_id, clawbot_instance_id,
                    binding_revision, bound_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    weixin_account_id = excluded.weixin_account_id,
                    weixin_user_id = excluded.weixin_user_id,
                    clawbot_instance_id = excluded.clawbot_instance_id,
                    binding_revision = excluded.binding_revision,
                    bound_at = excluded.bound_at,
                    updated_at = excluded.updated_at
                """,
                (user_id, account_id, wx_user_id, instance_id, revision, current, current),
            )
        return self.get_bind_status_for_user(user_id)

    def get_bind_status_for_user(self, user_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM weixin_bindings WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            user = self.get_user(user_id) or {"user_id": user_id, "display_name": user_id}
            return {"bound": False, "user_id": user_id, "display_name": user.get("display_name")}
        data = dict(row)
        return {
            "bound": True,
            "user_id": data["user_id"],
            "display_name": (self.get_user(data["user_id"]) or {}).get("display_name"),
            "weixin_account_id": data["weixin_account_id"],
            "weixin_user_id_masked": mask_external_id(data["weixin_user_id"]),
            "weixin_bound_at": data["bound_at"],
            "clawbot_instance_id": data["clawbot_instance_id"],
            "binding_revision": data["binding_revision"],
        }

    def get_bound_user(
        self,
        *,
        clawbot_instance_id: str | None = None,
        weixin_account_id: str | None = None,
        weixin_user_id: str | None = None,
    ) -> dict[str, Any] | None:
        instance_id = str(clawbot_instance_id or "").strip()
        with self._connect() as conn:
            if instance_id:
                row = conn.execute("SELECT * FROM weixin_bindings WHERE clawbot_instance_id = ?", (instance_id,)).fetchone()
                if row:
                    return self._bound_row_to_user(dict(row))
            account_ids = equivalent_weixin_account_ids(weixin_account_id)
            wx_user_id = str(weixin_user_id or "").strip()
            if account_ids and wx_user_id:
                placeholders = ",".join("?" for _ in account_ids)
                row = conn.execute(
                    f"SELECT * FROM weixin_bindings WHERE weixin_user_id = ? AND weixin_account_id IN ({placeholders})",
                    [wx_user_id, *account_ids],
                ).fetchone()
                if row:
                    return self._bound_row_to_user(dict(row))
        return None

    def unbind_user(self, user_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute("DELETE FROM weixin_bind_sessions WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM weixin_bindings WHERE user_id = ?", (user_id,))
        return self.get_bind_status_for_user(user_id)

    def unbind_identity(
        self,
        *,
        clawbot_instance_id: str | None = None,
        weixin_account_id: str | None = None,
        weixin_user_id: str | None = None,
    ) -> dict[str, Any]:
        user = self.get_bound_user(
            clawbot_instance_id=clawbot_instance_id,
            weixin_account_id=weixin_account_id,
            weixin_user_id=weixin_user_id,
        )
        if not user:
            return {"bound": False}
        return self.unbind_user(str(user["user_id"]))

    def _bound_row_to_user(self, binding: dict[str, Any]) -> dict[str, Any]:
        user = self.get_user(binding["user_id"]) or {"user_id": binding["user_id"], "display_name": binding["user_id"]}
        return {
            **user,
            "weixin_account_id": binding["weixin_account_id"],
            "weixin_user_id": binding["weixin_user_id"],
            "clawbot_instance_id": binding["clawbot_instance_id"],
            "binding_revision": binding["binding_revision"],
        }


def mask_external_id(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) <= 8:
        return f"{text[:2]}***"
    return f"{text[:4]}***{text[-4:]}"
