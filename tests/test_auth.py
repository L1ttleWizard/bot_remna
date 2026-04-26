"""Smoke-тесты для миграций БД и логики токенов.

Не требуют сетевых вызовов и BOT_TOKEN/REMNAWAVE_TOKEN.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _reload_db_with_path(tmp_path: Path):
    db_file = tmp_path / "test_bot.db"
    os.environ["DATABASE_PATH"] = str(db_file)
    if "database" in sys.modules:
        importlib.reload(sys.modules["database"])
    import database  # noqa: WPS433 — late import after env setup
    return database


@pytest.fixture()
def db_module(tmp_path):
    return _reload_db_with_path(tmp_path)


def test_init_db_idempotent(db_module):
    asyncio.run(db_module.init_db())
    asyncio.run(db_module.init_db())  # повторный init не должен падать


def test_legacy_users_table_migration(tmp_path):
    """Старая БД с минимальной схемой users должна получить новые колонки."""
    import sqlite3

    legacy_db = tmp_path / "legacy.db"
    with sqlite3.connect(legacy_db) as conn:
        conn.execute(
            """
            CREATE TABLE users (
                tg_id INTEGER PRIMARY KEY,
                uuid TEXT,
                short_uuid TEXT,
                username TEXT,
                expire_date TIMESTAMP
            )
            """
        )
        conn.execute(
            "INSERT INTO users (tg_id, uuid, short_uuid, username, expire_date) VALUES (1, 'u', 's', 'tg_1', 1)"
        )
        conn.commit()

    os.environ["DATABASE_PATH"] = str(legacy_db)
    if "database" in sys.modules:
        importlib.reload(sys.modules["database"])
    import database

    asyncio.run(database.init_db())
    role = asyncio.run(database.get_role(1))
    assert role == "user"


def test_bootstrap_admins_and_roles(db_module):
    asyncio.run(db_module.init_db())
    asyncio.run(db_module.bootstrap_admins([10, 20]))
    assert asyncio.run(db_module.is_admin(10)) is True
    assert asyncio.run(db_module.is_admin(20)) is True
    assert asyncio.run(db_module.is_admin(30)) is False


def test_token_lifecycle(db_module, monkeypatch):
    asyncio.run(db_module.init_db())
    asyncio.run(db_module.bootstrap_admins([1]))

    import auth
    importlib.reload(auth)

    raw = asyncio.run(auth.issue_token(created_by=1, expire_days=7, hwid_device_limit=2))
    assert raw and len(raw) > 16

    # Token can be looked up
    found = asyncio.run(auth.find_redeemable_token(raw))
    assert found is not None
    assert found.expire_days == 7
    assert found.hwid_device_limit == 2

    # Consume it
    ok = asyncio.run(auth.consume_token(found.token_hash, tg_id=42))
    assert ok is True

    # Second consume fails
    ok2 = asyncio.run(auth.consume_token(found.token_hash, tg_id=42))
    assert ok2 is False

    # Already-consumed token is no longer redeemable
    found2 = asyncio.run(auth.find_redeemable_token(raw))
    assert found2 is None


def test_token_revoke(db_module):
    asyncio.run(db_module.init_db())
    asyncio.run(db_module.bootstrap_admins([1]))

    import auth
    importlib.reload(auth)

    raw = asyncio.run(auth.issue_token(created_by=1, expire_days=30, hwid_device_limit=3))
    token_hash = auth.hash_token(raw)

    revoked = asyncio.run(db_module.revoke_access_token(token_hash))
    assert revoked is True

    # Revoked token is not redeemable
    assert asyncio.run(auth.find_redeemable_token(raw)) is None


def test_hash_token_is_deterministic():
    import auth
    importlib.reload(auth)
    assert auth.hash_token("abc") == auth.hash_token("abc")
    assert auth.hash_token("abc") != auth.hash_token("abcd")


def test_unknown_token_not_redeemable(db_module):
    asyncio.run(db_module.init_db())
    import auth
    importlib.reload(auth)
    assert asyncio.run(auth.find_redeemable_token("definitely-not-a-token")) is None
