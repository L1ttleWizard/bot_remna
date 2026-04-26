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


def test_upsert_tg_profile_and_lookup(db_module):
    asyncio.run(db_module.init_db())
    # Создаём профиль для нового tg_id (никогда не активировал токен).
    asyncio.run(
        db_module.upsert_tg_profile(
            555,
            tg_username="Alice_TG",
            tg_first_name="Alice",
            tg_last_name="Smith",
        )
    )
    full = asyncio.run(db_module.get_user_full(555))
    assert full is not None
    # Кортеж: (tg_id, uuid, short_uuid, username, expire_date, role,
    #         tg_username, tg_first_name, tg_last_name)
    assert full[0] == 555
    assert full[5] == "user"  # дефолтная роль
    assert full[6] == "Alice_TG"
    assert full[7] == "Alice"
    assert full[8] == "Smith"

    # Поиск по @username (без учёта регистра) находит того же пользователя.
    found = asyncio.run(db_module.find_user_by_tg_username("alice_tg"))
    assert found is not None and found[0] == 555

    # С префиксом @ — тоже находит.
    found2 = asyncio.run(db_module.find_user_by_tg_username("@ALICE_tg"))
    assert found2 is not None and found2[0] == 555

    # Несуществующий username — None.
    assert asyncio.run(db_module.find_user_by_tg_username("nobody_here")) is None
    assert asyncio.run(db_module.find_user_by_tg_username("")) is None


def test_upsert_tg_profile_overwrites(db_module):
    asyncio.run(db_module.init_db())
    asyncio.run(
        db_module.upsert_tg_profile(
            777, tg_username="oldname", tg_first_name="Old", tg_last_name=None
        )
    )
    asyncio.run(
        db_module.upsert_tg_profile(
            777, tg_username="newname", tg_first_name="New", tg_last_name="Surname"
        )
    )
    full = asyncio.run(db_module.get_user_full(777))
    assert full[6] == "newname"
    assert full[7] == "New"
    assert full[8] == "Surname"


def test_bootstrap_admin_preserves_tg_profile(db_module):
    """Bootstrap админа поверх уже сохранённого профиля не должен затирать имя."""
    asyncio.run(db_module.init_db())
    asyncio.run(
        db_module.upsert_tg_profile(
            999, tg_username="boss", tg_first_name="Boss", tg_last_name="One"
        )
    )
    asyncio.run(db_module.bootstrap_admins([999]))
    assert asyncio.run(db_module.is_admin(999)) is True
    full = asyncio.run(db_module.get_user_full(999))
    assert full[5] == "admin"
    assert full[6] == "boss"
    assert full[7] == "Boss"
