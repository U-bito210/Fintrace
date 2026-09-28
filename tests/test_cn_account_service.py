"""Offline coverage for local account hashing and Agent-task ownership."""

from __future__ import annotations

import sqlite3

import pytest

from src.cn import account_service as accounts


@pytest.fixture()
def account_db(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "ACCOUNT_DB_PATH", tmp_path / "accounts.sqlite3")
    return accounts.ACCOUNT_DB_PATH


def test_registered_password_is_hashed_and_login_is_case_insensitive(account_db):
    user = accounts.register_user("Researcher_01", "safe-password-01")
    authenticated = accounts.authenticate_user("researcher_01", "safe-password-01")

    assert authenticated["id"] == user["id"]
    with sqlite3.connect(account_db) as connection:
        stored = connection.execute("SELECT password_hash FROM users").fetchone()[0]
    assert stored.startswith("scrypt$")
    assert "safe-password-01" not in stored


def test_failed_login_and_cross_account_task_access_are_rejected(account_db):
    first = accounts.register_user("first_user", "safe-password-01")
    second = accounts.register_user("second_user", "safe-password-02")
    with pytest.raises(accounts.AccountError, match="用户名或密码"):
        accounts.authenticate_user("first_user", "wrong-password")

    accounts.assign_agent_task(first["id"], "agent_task_001")
    assert accounts.agent_task_ids_for_user(first["id"]) == {"agent_task_001"}
    assert accounts.agent_task_ids_for_user(second["id"]) == set()
    with pytest.raises(accounts.AccountError, match="其他账号"):
        accounts.assign_agent_task(second["id"], "agent_task_001")
