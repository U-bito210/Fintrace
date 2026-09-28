"""Local account ownership for the Streamlit workbench.

This deliberately small service is for a single trusted deployment.  Passwords
are salted with ``scrypt`` and only task ownership is stored here; snapshots,
evidence and Agent task payloads remain in their existing versioned stores.
The SQLite database lives under ``output/`` and is ignored by Git.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ACCOUNT_DB_PATH = PROJECT_ROOT / "output" / "accounts.sqlite3"
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,32}$")
MIN_PASSWORD_LENGTH = 8


class AccountError(ValueError):
    """A safe, user-displayable account action failure."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connection() -> sqlite3.Connection:
    ACCOUNT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(ACCOUNT_DB_PATH, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            username_key TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS agent_task_owners (
            task_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )"""
    )
    return connection


def _normalize_username(username: str) -> tuple[str, str]:
    display = str(username or "").strip()
    if not USERNAME_PATTERN.fullmatch(display):
        raise AccountError("用户名需为 3–32 位字母、数字、下划线或连字符。")
    return display, display.casefold()


def _validate_password(password: str) -> str:
    value = str(password or "")
    if len(value) < MIN_PASSWORD_LENGTH:
        raise AccountError(f"密码至少需要 {MIN_PASSWORD_LENGTH} 位。")
    if len(value) > 256:
        raise AccountError("密码长度不能超过 256 位。")
    return value


def _hash_password(password: str, *, salt: bytes | None = None) -> str:
    actual_salt = salt or os.urandom(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=actual_salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$%s$%s" % (
        base64.urlsafe_b64encode(actual_salt).decode("ascii"),
        base64.urlsafe_b64encode(derived).decode("ascii"),
    )


def _password_matches(password: str, stored: str) -> bool:
    try:
        algorithm, encoded_salt, encoded_digest = stored.split("$", 2)
        if algorithm != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(encoded_salt.encode("ascii"))
        expected = base64.urlsafe_b64decode(encoded_digest.encode("ascii"))
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=len(expected))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, UnicodeError):
        return False


def _public_user(row: sqlite3.Row) -> dict[str, Any]:
    return {"id": int(row["id"]), "username": str(row["username"]), "created_at": str(row["created_at"])}


def register_user(username: str, password: str) -> dict[str, Any]:
    """Create one local account; the plaintext password is never persisted."""
    display, key = _normalize_username(username)
    password_value = _validate_password(password)
    with _connection() as connection:
        try:
            cursor = connection.execute(
                "INSERT INTO users (username, username_key, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (display, key, _hash_password(password_value), _now()),
            )
        except sqlite3.IntegrityError as exc:
            raise AccountError("该用户名已被使用。") from exc
        row = connection.execute("SELECT * FROM users WHERE id = ?", (cursor.lastrowid,)).fetchone()
    if row is None:  # pragma: no cover - SQLite insert/select invariant
        raise AccountError("账号创建失败，请重试。")
    return _public_user(row)


def authenticate_user(username: str, password: str) -> dict[str, Any]:
    """Verify a local account without revealing whether a username exists."""
    _display, key = _normalize_username(username)
    password_value = _validate_password(password)
    with _connection() as connection:
        row = connection.execute("SELECT * FROM users WHERE username_key = ?", (key,)).fetchone()
    if row is None or not _password_matches(password_value, str(row["password_hash"])):
        raise AccountError("用户名或密码不正确。")
    return _public_user(row)


def assign_agent_task(user_id: int, task_id: str) -> None:
    """Bind a newly created Agent task to exactly one account."""
    with _connection() as connection:
        user = connection.execute("SELECT id FROM users WHERE id = ?", (int(user_id),)).fetchone()
        if user is None:
            raise AccountError("登录状态已失效，请重新登录。")
        existing = connection.execute("SELECT user_id FROM agent_task_owners WHERE task_id = ?", (task_id,)).fetchone()
        if existing is not None and int(existing["user_id"]) != int(user_id):
            raise AccountError("该研究任务已归属于其他账号。")
        connection.execute(
            "INSERT OR IGNORE INTO agent_task_owners (task_id, user_id, created_at) VALUES (?, ?, ?)",
            (task_id, int(user_id), _now()),
        )


def agent_task_ids_for_user(user_id: int) -> set[str]:
    """Return only task IDs explicitly assigned to this account."""
    with _connection() as connection:
        rows = connection.execute("SELECT task_id FROM agent_task_owners WHERE user_id = ?", (int(user_id),)).fetchall()
    return {str(row["task_id"]) for row in rows}
