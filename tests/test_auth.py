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


# --- Promocodes ---

def test_promocode_full_lifecycle(db_module):
    asyncio.run(db_module.init_db())
    # Создание
    ok = asyncio.run(db_module.create_promocode(
        "TEST10", bonus_days=30, max_uses=2, created_by=1
    ))
    assert ok is True
    # Дубль
    again = asyncio.run(db_module.create_promocode(
        "test10", bonus_days=10, max_uses=1, created_by=1
    ))
    assert again is False  # case-insensitive PK

    # Первый редем
    status, days = asyncio.run(db_module.redeem_promocode("TEST10", 100))
    assert status == db_module.PROMO_OK
    assert days == 30

    # Тот же юзер — повторно — отказ
    status, _ = asyncio.run(db_module.redeem_promocode("TEST10", 100))
    assert status == db_module.PROMO_ALREADY_USED

    # Другой юзер — ок
    status, _ = asyncio.run(db_module.redeem_promocode("test10", 200))
    assert status == db_module.PROMO_OK  # case-insensitive lookup

    # Лимит исчерпан
    status, _ = asyncio.run(db_module.redeem_promocode("TEST10", 300))
    assert status == db_module.PROMO_EXHAUSTED


def test_promocode_revoked(db_module):
    asyncio.run(db_module.init_db())
    asyncio.run(db_module.create_promocode("REV", bonus_days=7, max_uses=None, created_by=1))
    assert asyncio.run(db_module.revoke_promocode("REV")) is True
    status, _ = asyncio.run(db_module.redeem_promocode("REV", 100))
    assert status == db_module.PROMO_REVOKED


def test_promocode_unlimited(db_module):
    asyncio.run(db_module.init_db())
    asyncio.run(db_module.create_promocode("UNLIM", bonus_days=1, max_uses=None, created_by=1))
    for tg in range(5):
        status, _ = asyncio.run(db_module.redeem_promocode("UNLIM", tg))
        assert status == db_module.PROMO_OK


def test_promocode_not_found(db_module):
    asyncio.run(db_module.init_db())
    status, _ = asyncio.run(db_module.redeem_promocode("NOPE", 1))
    assert status == db_module.PROMO_NOT_FOUND


# --- Settings ---

def test_setting_get_set_clear(db_module):
    asyncio.run(db_module.init_db())
    assert asyncio.run(db_module.get_setting("foo")) is None
    asyncio.run(db_module.set_setting("foo", "bar"))
    assert asyncio.run(db_module.get_setting("foo")) == "bar"
    asyncio.run(db_module.set_setting("foo", "baz"))
    assert asyncio.run(db_module.get_setting("foo")) == "baz"
    asyncio.run(db_module.set_setting("foo", None))
    assert asyncio.run(db_module.get_setting("foo")) is None


# --- delete_user ---

def test_delete_user_wipes_promo_uses(db_module):
    asyncio.run(db_module.init_db())
    asyncio.run(db_module.add_user(
        tg_id=42, uuid="u", short_uuid="s", username="tg_42", expire_date=0,
    ))
    asyncio.run(db_module.create_promocode("CODE", bonus_days=5, max_uses=10, created_by=1))
    status, _ = asyncio.run(db_module.redeem_promocode("CODE", 42))
    assert status == db_module.PROMO_OK

    asyncio.run(db_module.delete_user(42))
    assert asyncio.run(db_module.get_user(42)) is None
    # тот же юзер может снова активировать тот же код после реактивации
    status, _ = asyncio.run(db_module.redeem_promocode("CODE", 42))
    assert status == db_module.PROMO_OK


# --- build_panel_username ---

def test_build_panel_username_variants(monkeypatch, tmp_path):
    """Чистый юнит-тест на санитайзер username — без БД."""
    # Импортируем функцию из bot.py — он требует env-переменные. Мокируем только нужное.
    monkeypatch.setenv("BOT_TOKEN", "123456:AAA-BBB_ccc-fakefakefakefakefakefakefa")
    monkeypatch.setenv("REMNAWAVE_URL", "https://x")
    monkeypatch.setenv("REMNAWAVE_TOKEN", "x")
    monkeypatch.setenv("SUB_DOMAIN", "https://y")
    monkeypatch.setenv("ADMIN_TG_IDS", "1")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "x.db"))
    if "bot" in sys.modules:
        importlib.reload(sys.modules["bot"])
    import bot

    assert bot.build_panel_username(123, "wizard", "Andrew") == "tg_123_wizard"
    assert bot.build_panel_username(123, None, "Андрей") == "tg_123"  # кириллица отсеивается
    assert bot.build_panel_username(123, None, "Andrew Ivanov") == "tg_123_AndrewIvanov"
    assert bot.build_panel_username(123, None, None) == "tg_123"
    # длинный никнейм должен обрезаться
    long = "x" * 50
    out = bot.build_panel_username(1, long, None)
    assert len(out) == 32
    assert out.startswith("tg_1_")


# --- Multi-subscription tests (PR #2) ---

def test_subscriptions_helpers_basic(db_module):
    """add_subscription, list_subscriptions, count_subscriptions, get_subscription."""
    asyncio.run(db_module.init_db())
    asyncio.run(db_module.add_user(
        tg_id=100, uuid="uuid-A", short_uuid="sA", username="tg_100", expire_date=0,
    ))
    # add second subscription
    sid2 = asyncio.run(db_module.add_subscription(
        100, uuid="uuid-B", short_uuid="sB", username="tg_100_2", expire_date=999, label=None,
    ))
    assert sid2 > 0

    subs = asyncio.run(db_module.list_subscriptions(100))
    assert len(subs) == 2
    # ordered ASC by created_at, id
    assert subs[0][1] == "uuid-A"
    assert subs[1][1] == "uuid-B"

    cnt = asyncio.run(db_module.count_subscriptions(100))
    assert cnt == 2

    sub = asyncio.run(db_module.get_subscription(sid2))
    assert sub is not None
    assert sub[1] == 100  # tg_id
    assert sub[2] == "uuid-B"
    assert sub[5] == 999  # expire_date


def test_get_user_returns_most_recent_subscription(db_module):
    """get_user (compat) должен возвращать самую свежую подписку."""
    asyncio.run(db_module.init_db())
    asyncio.run(db_module.add_user(
        tg_id=200, uuid="old", short_uuid="so", username="tg_200", expire_date=100,
    ))
    asyncio.run(db_module.add_subscription(
        200, uuid="new", short_uuid="sn", username="tg_200_2", expire_date=200,
    ))
    user = asyncio.run(db_module.get_user(200))
    assert user is not None
    # most recent — последняя добавленная
    assert user[1] == "new"
    assert user[4] == 200


def test_delete_subscription_keeps_others(db_module):
    asyncio.run(db_module.init_db())
    asyncio.run(db_module.add_user(
        tg_id=300, uuid="u1", short_uuid="s1", username="tg_300", expire_date=0,
    ))
    sid2 = asyncio.run(db_module.add_subscription(
        300, uuid="u2", short_uuid="s2", username="tg_300_2", expire_date=0,
    ))
    asyncio.run(db_module.delete_subscription(sid2))
    subs = asyncio.run(db_module.list_subscriptions(300))
    assert len(subs) == 1
    assert subs[0][1] == "u1"


def test_delete_user_cascades_all_subs(db_module):
    asyncio.run(db_module.init_db())
    asyncio.run(db_module.add_user(
        tg_id=400, uuid="ua", short_uuid="sa", username="tg_400", expire_date=0,
    ))
    asyncio.run(db_module.add_subscription(
        400, uuid="ub", short_uuid="sb", username="tg_400_2", expire_date=0,
    ))
    asyncio.run(db_module.add_subscription(
        400, uuid="uc", short_uuid="sc", username="tg_400_3", expire_date=0,
    ))
    asyncio.run(db_module.delete_user(400))
    assert asyncio.run(db_module.count_subscriptions(400)) == 0
    assert asyncio.run(db_module.get_user(400)) is None


def test_update_subscription_expire_by_uuid(db_module):
    asyncio.run(db_module.init_db())
    asyncio.run(db_module.add_user(
        tg_id=500, uuid="uX", short_uuid="sX", username="tg_500", expire_date=10,
    ))
    asyncio.run(db_module.update_subscription_expire_by_uuid("uX", 9999))
    sub = asyncio.run(db_module.find_subscription_by_uuid("uX"))
    assert sub is not None
    assert sub[5] == 9999


def test_legacy_users_uuid_backfilled_to_subscriptions(tmp_path):
    """При апгрейде на новую схему — строки users.uuid должны попасть в subscriptions."""
    import sqlite3

    legacy_db = tmp_path / "legacy_with_uuid.db"
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
            "INSERT INTO users (tg_id, uuid, short_uuid, username, expire_date) "
            "VALUES (777, 'legacy-uuid', 'leg-short', 'tg_777', 12345)"
        )
        conn.commit()

    os.environ["DATABASE_PATH"] = str(legacy_db)
    if "database" in sys.modules:
        importlib.reload(sys.modules["database"])
    import database
    asyncio.run(database.init_db())

    subs = asyncio.run(database.list_subscriptions(777))
    assert len(subs) == 1
    assert subs[0][1] == "legacy-uuid"
    assert subs[0][3] == "tg_777"


def test_has_promocode_use(db_module):
    asyncio.run(db_module.init_db())
    asyncio.run(db_module.add_user(
        tg_id=600, uuid="u6", short_uuid="s6", username="tg_600", expire_date=0,
    ))
    asyncio.run(db_module.create_promocode("CODE6", bonus_days=3, max_uses=10, created_by=1))
    assert asyncio.run(db_module.has_promocode_use("CODE6", 600)) is False
    asyncio.run(db_module.redeem_promocode("CODE6", 600))
    assert asyncio.run(db_module.has_promocode_use("CODE6", 600)) is True
    # case-insensitive
    assert asyncio.run(db_module.has_promocode_use("code6", 600)) is True


def test_list_all_active_subscriptions_window(db_module):
    asyncio.run(db_module.init_db())
    asyncio.run(db_module.add_user(
        tg_id=700, uuid="u7", short_uuid="s7", username="tg_700",
        expire_date=1_000,  # before window
    ))
    asyncio.run(db_module.add_subscription(
        700, uuid="u7b", short_uuid="s7b", username="tg_700_2", expire_date=2_000,  # in window
    ))
    asyncio.run(db_module.add_subscription(
        700, uuid="u7c", short_uuid="s7c", username="tg_700_3", expire_date=10_000,  # after window
    ))
    rows = asyncio.run(db_module.list_all_active_subscriptions(1_500, 2_500))
    assert len(rows) == 1
    assert rows[0][0] == 700


def test_search_users_by_substrings(db_module):
    """search_users / count_search_users должны находить по tg_id, @username, имени, фамилии и panel-username."""
    asyncio.run(db_module.init_db())
    asyncio.run(db_module.add_user(
        tg_id=4242, uuid="ux1", short_uuid="sx1", username="tg_4242_alice", expire_date=2_000,
    ))
    asyncio.run(db_module.upsert_tg_profile(
        4242, tg_username="alice_chat", tg_first_name="Алиса", tg_last_name="Иванова"
    ))
    asyncio.run(db_module.add_user(
        tg_id=5151, uuid="uy1", short_uuid="sy1", username="tg_5151_bob", expire_date=3_000,
    ))
    asyncio.run(db_module.upsert_tg_profile(
        5151, tg_username="bob_chat", tg_first_name="Боб", tg_last_name=None
    ))

    # tg_id substring
    assert asyncio.run(db_module.count_search_users("424")) == 1
    rows = asyncio.run(db_module.search_users("424"))
    assert {r[0] for r in rows} == {4242}

    # full numeric tg_id
    rows = asyncio.run(db_module.search_users("4242"))
    assert {r[0] for r in rows} == {4242}

    # @username case-insensitive
    rows = asyncio.run(db_module.search_users("ALICE_CHAT"))
    assert {r[0] for r in rows} == {4242}

    # first_name (cyrillic)
    rows = asyncio.run(db_module.search_users("Алис"))
    assert {r[0] for r in rows} == {4242}

    # panel username
    rows = asyncio.run(db_module.search_users("_bob"))
    assert {r[0] for r in rows} == {5151}

    # nothing found
    assert asyncio.run(db_module.count_search_users("zzzzzzz")) == 0
    assert asyncio.run(db_module.search_users("zzzzzzz")) == []

    # empty query
    assert asyncio.run(db_module.search_users("")) == []
    assert asyncio.run(db_module.count_search_users("")) == 0


def test_revoke_access_token_via_prefix(db_module):
    """Сценарий: создаём токен, находим по префиксу, отзываем, повторный отзыв не работает."""
    asyncio.run(db_module.init_db())
    import auth as auth_mod  # noqa: WPS433
    importlib.reload(auth_mod)
    raw = asyncio.run(auth_mod.issue_token(created_by=1, expire_days=30, hwid_device_limit=3))
    assert raw
    tokens = asyncio.run(db_module.list_active_tokens())
    assert len(tokens) == 1
    full_hash = tokens[0][0]
    prefix = full_hash[:12]
    found = asyncio.run(db_module.find_token_by_hash_prefix(prefix))
    assert found == full_hash
    assert asyncio.run(db_module.revoke_access_token(full_hash)) is True
    # повторный отзыв уже отозванного не должен сработать
    assert asyncio.run(db_module.revoke_access_token(full_hash)) is False
    # после отзыва токен исчезает из активного списка
    assert asyncio.run(db_module.list_active_tokens()) == []


def test_dm_target_must_exist_in_db(db_module):
    """Контракт: get_user_full(tg_id) возвращает None для несуществующих юзеров,
    что используется в /dm для отказа отправки."""
    asyncio.run(db_module.init_db())
    asyncio.run(db_module.add_user(
        tg_id=7001, uuid="u70", short_uuid="s70", username="tg_7001", expire_date=10,
    ))
    assert asyncio.run(db_module.get_user_full(7001)) is not None
    assert asyncio.run(db_module.get_user_full(99999)) is None


def test_stats_users_counts(db_module):
    """stats_users() возвращает корректные агрегаты:
    активные/истёкшие/безлимитные/истекающие за 7д."""
    import time as _t
    from datetime import datetime, timezone

    asyncio.run(db_module.init_db())
    now = int(_t.time())
    far_future = int(datetime(2099, 6, 1, tzinfo=timezone.utc).timestamp())

    # Юзер с 3 подписками: активная (>30д), истекающая (через 3д), без лимита
    asyncio.run(db_module.add_user(
        tg_id=9001, uuid="ua", short_uuid="sa", username="u_active",
        expire_date=now + 30 * 86400,
    ))
    asyncio.run(db_module.add_subscription(
        9001, uuid="ub", short_uuid="sb", username="u_soon",
        expire_date=now + 3 * 86400, created_by=1,
    ))
    asyncio.run(db_module.add_subscription(
        9001, uuid="uc", short_uuid="sc", username="u_inf",
        expire_date=far_future, created_by=1,
    ))
    # Юзер с истёкшей
    asyncio.run(db_module.add_user(
        tg_id=9002, uuid="ud", short_uuid="sd", username="u_expired",
        expire_date=now - 86400,
    ))
    # Юзер без подписок
    asyncio.run(db_module.upsert_tg_profile(
        9003, tg_username="noone", tg_first_name="No", tg_last_name="Body",
    ))

    stats = asyncio.run(db_module.stats_users())
    assert stats["total_users"] == 3
    assert stats["users_with_subs"] == 2
    assert stats["users_without_subs"] == 1
    assert stats["total_subscriptions"] == 4
    assert stats["subs_active"] == 3  # active, soon, inf
    assert stats["subs_expired"] == 1
    assert stats["subs_unlimited"] == 1
    assert stats["subs_expiring_7d"] == 1


def test_stats_tokens_aggregation(db_module):
    """stats_tokens() считает выпущенные/использованные/отозванные и группирует по автору."""
    asyncio.run(db_module.init_db())
    # admin 100 — два токена, один использован
    asyncio.run(db_module.create_access_token(
        token_hash="h1", created_by=100, expire_days=30, hwid_device_limit=2,
    ))
    asyncio.run(db_module.create_access_token(
        token_hash="h2", created_by=100, expire_days=30, hwid_device_limit=2,
    ))
    asyncio.run(db_module.consume_access_token(token_hash="h1", tg_id=555))
    # admin 200 — один отозванный
    asyncio.run(db_module.create_access_token(
        token_hash="h3", created_by=200, expire_days=30, hwid_device_limit=2,
    ))
    asyncio.run(db_module.revoke_access_token("h3"))

    stats = asyncio.run(db_module.stats_tokens())
    assert stats["total"] == 3
    assert stats["redeemed"] == 1
    assert stats["revoked"] == 1
    assert stats["active"] == 1  # h2
    by_admin = {row[0]: row for row in stats["by_admin"]}
    assert by_admin[100][1] == 2 and by_admin[100][2] == 1
    assert by_admin[200][1] == 1 and by_admin[200][3] == 1


def test_stats_promocodes_top(db_module):
    """stats_promocodes() сортирует топ-коды по used_count и считает выданные бонус-дни."""
    asyncio.run(db_module.init_db())
    asyncio.run(db_module.create_promocode(
        code="ALPHA", bonus_days=7, max_uses=10, created_by=1,
    ))
    asyncio.run(db_module.create_promocode(
        code="BETA", bonus_days=3, max_uses=None, created_by=1,
    ))
    asyncio.run(db_module.create_promocode(
        code="GAMMA", bonus_days=1, max_uses=5, created_by=1,
    ))
    # ALPHA: 2 использования, BETA: 5 использований, GAMMA: 0
    asyncio.run(db_module.add_user(tg_id=1, uuid="u1", short_uuid="s1", username="x", expire_date=0))
    for tg in (1, 2, 3, 4, 5):
        asyncio.run(db_module.redeem_promocode(code="BETA", tg_id=tg))
    for tg in (10, 11):
        asyncio.run(db_module.redeem_promocode(code="ALPHA", tg_id=tg))

    stats = asyncio.run(db_module.stats_promocodes())
    assert stats["total"] == 3
    assert stats["active"] == 3
    assert stats["total_uses"] == 7
    # 5*3 + 2*7 + 0*1 = 29
    assert stats["bonus_days_granted"] == 29
    top_codes = [row[0] for row in stats["top_codes"]]
    assert top_codes[0] == "BETA"
    assert top_codes[1] == "ALPHA"


def test_list_subs_expiring_in_window(db_module):
    """list_subs_expiring_in возвращает только подписки в окне now..now+window."""
    import time as _t
    asyncio.run(db_module.init_db())
    now = int(_t.time())
    asyncio.run(db_module.add_user(
        tg_id=8001, uuid="e1", short_uuid="s1", username="soon",
        expire_date=now + 2 * 86400,
    ))
    asyncio.run(db_module.add_subscription(
        8001, uuid="e2", short_uuid="s2", username="far",
        expire_date=now + 30 * 86400, created_by=1,
    ))
    asyncio.run(db_module.add_subscription(
        8001, uuid="e3", short_uuid="s3", username="past",
        expire_date=now - 86400, created_by=1,
    ))

    rows = asyncio.run(db_module.list_subs_expiring_in(7 * 86400))
    assert len(rows) == 1
    assert rows[0][1] == "e1"


def test_list_all_subscriptions_with_uuid(db_module):
    """list_all_subscriptions_with_uuid отдаёт только записи с непустым uuid."""
    asyncio.run(db_module.init_db())
    asyncio.run(db_module.add_user(
        tg_id=9100, uuid="x1", short_uuid="s1", username="a", expire_date=0,
    ))
    asyncio.run(db_module.add_subscription(
        9100, uuid="x2", short_uuid="s2", username="b", expire_date=0, created_by=1,
    ))
    rows = asyncio.run(db_module.list_all_subscriptions_with_uuid())
    uuids = {r[1] for r in rows}
    assert uuids == {"x1", "x2"}
