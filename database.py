import os
import time
from typing import Iterable, Optional

import aiosqlite

DB_PATH = os.environ.get("DATABASE_PATH", "bot_database.db")


ROLE_ADMIN = "admin"
ROLE_USER = "user"


async def _column_exists(db: aiosqlite.Connection, table: str, column: str) -> bool:
    async with db.execute(f"PRAGMA table_info({table})") as cursor:
        rows = await cursor.fetchall()
    return any(r[1] == column for r in rows)


async def _ensure_user_columns(db: aiosqlite.Connection) -> None:
    if not await _column_exists(db, "users", "role"):
        await db.execute(
            "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'"
        )
    if not await _column_exists(db, "users", "created_at"):
        await db.execute("ALTER TABLE users ADD COLUMN created_at INTEGER")
    if not await _column_exists(db, "users", "created_by"):
        await db.execute("ALTER TABLE users ADD COLUMN created_by INTEGER")
    if not await _column_exists(db, "users", "tg_username"):
        await db.execute("ALTER TABLE users ADD COLUMN tg_username TEXT")
    if not await _column_exists(db, "users", "tg_first_name"):
        await db.execute("ALTER TABLE users ADD COLUMN tg_first_name TEXT")
    if not await _column_exists(db, "users", "tg_last_name"):
        await db.execute("ALTER TABLE users ADD COLUMN tg_last_name TEXT")


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                tg_id INTEGER PRIMARY KEY,
                uuid TEXT,
                short_uuid TEXT,
                username TEXT,
                expire_date TIMESTAMP,
                role TEXT NOT NULL DEFAULT 'user',
                created_at INTEGER,
                created_by INTEGER,
                tg_username TEXT,
                tg_first_name TEXT,
                tg_last_name TEXT
            )
            """
        )
        await _ensure_user_columns(db)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_tg_username ON users(tg_username COLLATE NOCASE)"
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS access_tokens (
                token_hash TEXT PRIMARY KEY,
                created_by INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                consumed_by_tg_id INTEGER,
                consumed_at INTEGER,
                expire_days INTEGER NOT NULL,
                hwid_device_limit INTEGER NOT NULL,
                revoked INTEGER NOT NULL DEFAULT 0
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS promocodes (
                code TEXT PRIMARY KEY COLLATE NOCASE,
                bonus_days INTEGER NOT NULL,
                max_uses INTEGER,
                used_count INTEGER NOT NULL DEFAULT 0,
                created_by INTEGER,
                created_at INTEGER NOT NULL,
                revoked INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS promocode_uses (
                code TEXT NOT NULL COLLATE NOCASE,
                tg_id INTEGER NOT NULL,
                used_at INTEGER NOT NULL,
                PRIMARY KEY (code, tg_id)
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER NOT NULL,
                uuid TEXT NOT NULL UNIQUE,
                short_uuid TEXT,
                username TEXT,
                expire_date INTEGER,
                label TEXT,
                created_by INTEGER,
                created_at INTEGER NOT NULL
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_subscriptions_tg_id ON subscriptions(tg_id)"
        )

        # One-time backfill: copy legacy users.uuid → subscriptions for users
        # that don't have a corresponding subscription yet.
        async with db.execute(
            """
            SELECT u.tg_id, u.uuid, u.short_uuid, u.username, u.expire_date,
                   u.created_by, COALESCE(u.created_at, ?)
            FROM users u
            WHERE u.uuid IS NOT NULL
              AND u.uuid NOT IN (SELECT uuid FROM subscriptions)
            """,
            (int(time.time()),),
        ) as cursor:
            legacy_rows = await cursor.fetchall()
        for tg_id, uuid, short_uuid, username, expire_date, created_by, created_at in legacy_rows:
            await db.execute(
                """
                INSERT OR IGNORE INTO subscriptions
                  (tg_id, uuid, short_uuid, username, expire_date, label, created_by, created_at)
                VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (tg_id, uuid, short_uuid, username, expire_date, created_by, created_at),
            )

        await db.commit()


async def add_user(
    tg_id: int,
    uuid: str,
    short_uuid: str,
    username: str,
    expire_date: int,
    *,
    role: str = ROLE_USER,
    created_by: Optional[int] = None,
):
    """Создаёт identity-запись пользователя (если ещё нет) и связанную подписку.

    `uuid` уникален — при повторном вызове с тем же uuid обновляются только
    short_uuid/username/expire_date этой подписки.
    """
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users (tg_id, role, created_at, created_by)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(tg_id) DO UPDATE SET
                role = CASE WHEN users.role = 'admin' THEN users.role ELSE excluded.role END,
                created_by = COALESCE(users.created_by, excluded.created_by)
            """,
            (tg_id, role, now, created_by),
        )
        await db.execute(
            """
            INSERT INTO subscriptions
              (tg_id, uuid, short_uuid, username, expire_date, label, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
            ON CONFLICT(uuid) DO UPDATE SET
                short_uuid = excluded.short_uuid,
                username = excluded.username,
                expire_date = excluded.expire_date
            """,
            (tg_id, uuid, short_uuid, username, expire_date, created_by, now),
        )
        await db.commit()


async def get_user(tg_id: int):
    """Compat: return (tg_id, uuid, short_uuid, username, expire_date) for the most recent
    subscription, or None when the user has no subscriptions yet.

    Большинство callsite'ов использует это для проверки has_account и для одноподписочных
    операций. Новый код должен использовать `list_subscriptions(tg_id)` напрямую.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT tg_id, uuid, short_uuid, username, expire_date
            FROM subscriptions
            WHERE tg_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (tg_id,),
        ) as cursor:
            return await cursor.fetchone()


async def get_user_full(tg_id: int):
    """Compat: return (tg_id, uuid, short_uuid, username, expire_date, role,
                       tg_username, tg_first_name, tg_last_name) или None.

    uuid/short_uuid/username/expire_date берутся из самой свежей подписки (или NULL,
    если подписок нет). Если запись пользователя отсутствует целиком — None.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT u.tg_id, s.uuid, s.short_uuid, s.username, s.expire_date,
                   u.role, u.tg_username, u.tg_first_name, u.tg_last_name
            FROM users u
            LEFT JOIN (
                SELECT tg_id, uuid, short_uuid, username, expire_date,
                       ROW_NUMBER() OVER (PARTITION BY tg_id ORDER BY created_at DESC, id DESC) AS rn
                FROM subscriptions
            ) s ON s.tg_id = u.tg_id AND s.rn = 1
            WHERE u.tg_id = ?
            """,
            (tg_id,),
        ) as cursor:
            return await cursor.fetchone()


# --- Subscription helpers (new model) ---

async def add_subscription(
    tg_id: int,
    *,
    uuid: str,
    short_uuid: str,
    username: str,
    expire_date: int,
    label: Optional[str] = None,
    created_by: Optional[int] = None,
) -> int:
    """Insert a new subscription and return its sub_id."""
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO subscriptions
              (tg_id, uuid, short_uuid, username, expire_date, label, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (tg_id, uuid, short_uuid, username, expire_date, label, created_by, now),
        )
        await db.commit()
        return int(cursor.lastrowid or 0)


async def list_subscriptions(tg_id: int) -> list:
    """Возвращает список подписок (id, uuid, short_uuid, username, expire_date, label, created_at)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT id, uuid, short_uuid, username, expire_date, label, created_at
            FROM subscriptions
            WHERE tg_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (tg_id,),
        ) as cursor:
            return list(await cursor.fetchall())


async def get_subscription(sub_id: int):
    """Возвращает (id, tg_id, uuid, short_uuid, username, expire_date, label, created_by, created_at) или None."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT id, tg_id, uuid, short_uuid, username, expire_date, label, created_by, created_at
            FROM subscriptions WHERE id = ?
            """,
            (int(sub_id),),
        ) as cursor:
            return await cursor.fetchone()


async def find_subscription_by_uuid(uuid: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT id, tg_id, uuid, short_uuid, username, expire_date, label, created_by, created_at
            FROM subscriptions WHERE uuid = ?
            """,
            (uuid,),
        ) as cursor:
            return await cursor.fetchone()


async def update_subscription_expire(sub_id: int, expire_date: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE subscriptions SET expire_date = ? WHERE id = ?",
            (int(expire_date), int(sub_id)),
        )
        await db.commit()


async def update_subscription_expire_by_uuid(uuid: str, expire_date: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE subscriptions SET expire_date = ? WHERE uuid = ?",
            (int(expire_date), uuid),
        )
        await db.commit()


async def update_subscription_label(sub_id: int, label: Optional[str]) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE subscriptions SET label = ? WHERE id = ?",
            (label, int(sub_id)),
        )
        await db.commit()


async def delete_subscription(sub_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM subscriptions WHERE id = ?", (int(sub_id),))
        await db.commit()


async def count_subscriptions(tg_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM subscriptions WHERE tg_id = ?", (int(tg_id),),
        ) as cursor:
            row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def list_all_active_subscriptions(now_ts: int, window_end_ts: int) -> list:
    """Все подписки, истекающие в окне (now, window_end]. Возвращает (tg_id, sub_id, expire_date, role)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT s.tg_id, s.id, s.expire_date, COALESCE(u.role, 'user')
            FROM subscriptions s
            JOIN users u ON u.tg_id = s.tg_id
            WHERE s.expire_date IS NOT NULL
              AND s.expire_date > ?
              AND s.expire_date <= ?
            """,
            (now_ts, window_end_ts),
        ) as cursor:
            return list(await cursor.fetchall())


async def upsert_tg_profile(
    tg_id: int,
    *,
    tg_username: Optional[str],
    tg_first_name: Optional[str],
    tg_last_name: Optional[str],
) -> None:
    """Сохраняет/обновляет Telegram-имена. Создаёт пустую запись, если её ещё нет."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users (tg_id, role, created_at, tg_username, tg_first_name, tg_last_name)
            VALUES (?, 'user', ?, ?, ?, ?)
            ON CONFLICT(tg_id) DO UPDATE SET
                tg_username   = excluded.tg_username,
                tg_first_name = excluded.tg_first_name,
                tg_last_name  = excluded.tg_last_name
            """,
            (tg_id, int(time.time()), tg_username, tg_first_name, tg_last_name),
        )
        await db.commit()


async def find_user_by_tg_username(tg_username: str):
    """Поиск по @username (без @, без учёта регистра). Возвращает get_user_full-кортеж или None."""
    name = tg_username.lstrip("@").strip()
    if not name:
        return None
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT u.tg_id, s.uuid, s.short_uuid, s.username, s.expire_date,
                   u.role, u.tg_username, u.tg_first_name, u.tg_last_name
            FROM users u
            LEFT JOIN (
                SELECT tg_id, uuid, short_uuid, username, expire_date,
                       ROW_NUMBER() OVER (PARTITION BY tg_id ORDER BY created_at DESC, id DESC) AS rn
                FROM subscriptions
            ) s ON s.tg_id = u.tg_id AND s.rn = 1
            WHERE u.tg_username IS NOT NULL AND u.tg_username = ? COLLATE NOCASE
            LIMIT 1
            """,
            (name,),
        ) as cursor:
            return await cursor.fetchone()


async def update_user_expire(tg_id: int, expire_date: int):
    """Compat: апдейтит expire_date у самой свежей подписки пользователя.
    В новом коде используйте `update_subscription_expire(sub_id, ts)` или _by_uuid.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE subscriptions
            SET expire_date = ?
            WHERE id = (
                SELECT id FROM subscriptions WHERE tg_id = ?
                ORDER BY created_at DESC, id DESC LIMIT 1
            )
            """,
            (int(expire_date), int(tg_id)),
        )
        await db.commit()


async def get_role(tg_id: int) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT role FROM users WHERE tg_id = ?", (tg_id,)
        ) as cursor:
            row = await cursor.fetchone()
    return row[0] if row else None


async def is_admin(tg_id: int) -> bool:
    return (await get_role(tg_id)) == ROLE_ADMIN


async def set_role(tg_id: int, role: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users (tg_id, role, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(tg_id) DO UPDATE SET role=excluded.role
            """,
            (tg_id, role, int(time.time())),
        )
        await db.commit()


async def bootstrap_admins(admin_ids: Iterable[int]) -> None:
    ids = [int(x) for x in admin_ids if x]
    if not ids:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        for tg_id in ids:
            await db.execute(
                """
                INSERT INTO users (tg_id, role, created_at)
                VALUES (?, 'admin', ?)
                ON CONFLICT(tg_id) DO UPDATE SET role='admin'
                """,
                (tg_id, int(time.time())),
            )
        await db.commit()


async def list_users(limit: int = 50, offset: int = 0) -> list:
    """Возвращает per-user сводку: 9-tuple (tg_id, uuid, short_uuid, username, expire_date, role,
    tg_username, tg_first_name, tg_last_name) — uuid/short_uuid/username/expire_date берутся из
    самой свежей подписки (или NULL, если подписок нет).

    Сортировка: пользователи с подписками выше (по самой свежей подписке), без подписок — внизу.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT u.tg_id, s.uuid, s.short_uuid, s.username, s.expire_date,
                   u.role, u.tg_username, u.tg_first_name, u.tg_last_name
            FROM users u
            LEFT JOIN (
                SELECT tg_id, uuid, short_uuid, username, expire_date, created_at,
                       ROW_NUMBER() OVER (PARTITION BY tg_id ORDER BY created_at DESC, id DESC) AS rn
                FROM subscriptions
            ) s ON s.tg_id = u.tg_id AND s.rn = 1
            ORDER BY
                CASE WHEN s.created_at IS NULL THEN 1 ELSE 0 END,
                COALESCE(s.created_at, u.created_at, 0) DESC,
                u.tg_id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ) as cursor:
            return list(await cursor.fetchall())


async def count_users() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cursor:
            row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def search_users(query: str, limit: int = 50, offset: int = 0) -> list:
    """Находит юзеров по подстроке (без учёта регистра) в tg_id, tg_username,
    tg_first_name, tg_last_name или username последней подписки.

    Возвращает тот же 9-tuple что и list_users.
    """
    q = (query or "").strip()
    if not q:
        return []
    pattern = f"%{q}%"
    digit_match = q.lstrip("@").isdigit()
    digit_value: Optional[int] = int(q.lstrip("@")) if digit_match else None
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT u.tg_id, s.uuid, s.short_uuid, s.username, s.expire_date,
                   u.role, u.tg_username, u.tg_first_name, u.tg_last_name
            FROM users u
            LEFT JOIN (
                SELECT tg_id, uuid, short_uuid, username, expire_date, created_at,
                       ROW_NUMBER() OVER (PARTITION BY tg_id ORDER BY created_at DESC, id DESC) AS rn
                FROM subscriptions
            ) s ON s.tg_id = u.tg_id AND s.rn = 1
            WHERE
                CAST(u.tg_id AS TEXT) LIKE ?
                OR (u.tg_username   IS NOT NULL AND u.tg_username   LIKE ? COLLATE NOCASE)
                OR (u.tg_first_name IS NOT NULL AND u.tg_first_name LIKE ? COLLATE NOCASE)
                OR (u.tg_last_name  IS NOT NULL AND u.tg_last_name  LIKE ? COLLATE NOCASE)
                OR (s.username      IS NOT NULL AND s.username      LIKE ? COLLATE NOCASE)
                OR (? IS NOT NULL AND u.tg_id = ?)
            ORDER BY
                CASE WHEN s.created_at IS NULL THEN 1 ELSE 0 END,
                COALESCE(s.created_at, u.created_at, 0) DESC,
                u.tg_id DESC
            LIMIT ? OFFSET ?
            """,
            (pattern, pattern, pattern, pattern, pattern, digit_value, digit_value, limit, offset),
        ) as cursor:
            return list(await cursor.fetchall())


async def count_search_users(query: str) -> int:
    q = (query or "").strip()
    if not q:
        return 0
    pattern = f"%{q}%"
    digit_match = q.lstrip("@").isdigit()
    digit_value: Optional[int] = int(q.lstrip("@")) if digit_match else None
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT COUNT(*)
            FROM users u
            LEFT JOIN (
                SELECT tg_id, username,
                       ROW_NUMBER() OVER (PARTITION BY tg_id ORDER BY created_at DESC, id DESC) AS rn
                FROM subscriptions
            ) s ON s.tg_id = u.tg_id AND s.rn = 1
            WHERE
                CAST(u.tg_id AS TEXT) LIKE ?
                OR (u.tg_username   IS NOT NULL AND u.tg_username   LIKE ? COLLATE NOCASE)
                OR (u.tg_first_name IS NOT NULL AND u.tg_first_name LIKE ? COLLATE NOCASE)
                OR (u.tg_last_name  IS NOT NULL AND u.tg_last_name  LIKE ? COLLATE NOCASE)
                OR (s.username      IS NOT NULL AND s.username      LIKE ? COLLATE NOCASE)
                OR (? IS NOT NULL AND u.tg_id = ?)
            """,
            (pattern, pattern, pattern, pattern, pattern, digit_value, digit_value),
        ) as cursor:
            row = await cursor.fetchone()
    return int(row[0]) if row else 0


# --- Access tokens ---

async def create_access_token(
    *,
    token_hash: str,
    created_by: int,
    expire_days: int,
    hwid_device_limit: int,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO access_tokens (
                token_hash, created_by, created_at, expire_days, hwid_device_limit
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (token_hash, created_by, int(time.time()), expire_days, hwid_device_limit),
        )
        await db.commit()


async def get_access_token(token_hash: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT token_hash, created_by, created_at,
                   consumed_by_tg_id, consumed_at,
                   expire_days, hwid_device_limit, revoked
            FROM access_tokens WHERE token_hash = ?
            """,
            (token_hash,),
        ) as cursor:
            return await cursor.fetchone()


async def consume_access_token(token_hash: str, tg_id: int) -> bool:
    """Atomically mark token as consumed by tg_id. Returns True if it was successfully consumed."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            UPDATE access_tokens
            SET consumed_by_tg_id = ?, consumed_at = ?
            WHERE token_hash = ?
              AND consumed_by_tg_id IS NULL
              AND revoked = 0
            """,
            (tg_id, int(time.time()), token_hash),
        ) as cursor:
            changed = cursor.rowcount
        await db.commit()
    return changed == 1


async def revoke_access_token(token_hash: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            UPDATE access_tokens SET revoked = 1
            WHERE token_hash = ? AND consumed_by_tg_id IS NULL AND revoked = 0
            """,
            (token_hash,),
        ) as cursor:
            changed = cursor.rowcount
        await db.commit()
    return changed == 1


async def list_active_tokens(limit: int = 50) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT token_hash, created_by, created_at, expire_days, hwid_device_limit
            FROM access_tokens
            WHERE consumed_by_tg_id IS NULL AND revoked = 0
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ) as cursor:
            return list(await cursor.fetchall())


async def find_token_by_hash_prefix(prefix: str) -> Optional[str]:
    """Return the full token_hash matching the given prefix, if unique."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT token_hash FROM access_tokens WHERE token_hash LIKE ? LIMIT 2",
            (prefix + "%",),
        ) as cursor:
            rows = await cursor.fetchall()
    if len(rows) == 1:
        return rows[0][0]
    return None


# --- Promocodes ---

PROMO_OK = "ok"
PROMO_NOT_FOUND = "not_found"
PROMO_REVOKED = "revoked"
PROMO_EXHAUSTED = "exhausted"
PROMO_ALREADY_USED = "already_used"


async def create_promocode(
    code: str,
    *,
    bonus_days: int,
    max_uses: Optional[int],
    created_by: int,
) -> bool:
    """Создаёт промокод. Возвращает True если создан, False если уже существует."""
    code = code.strip()
    if not code:
        return False
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                """
                INSERT INTO promocodes (code, bonus_days, max_uses, used_count, created_by, created_at)
                VALUES (?, ?, ?, 0, ?, ?)
                """,
                (code, int(bonus_days), max_uses, int(created_by), int(time.time())),
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def revoke_promocode(code: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE promocodes SET revoked = 1 WHERE code = ? COLLATE NOCASE",
            (code.strip(),),
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_promocode(code: str):
    """Returns row (code, bonus_days, max_uses, used_count, created_by, created_at, revoked) or None."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT code, bonus_days, max_uses, used_count, created_by, created_at, revoked
            FROM promocodes WHERE code = ? COLLATE NOCASE
            """,
            (code.strip(),),
        ) as cursor:
            return await cursor.fetchone()


async def has_promocode_use(code: str, tg_id: int) -> bool:
    """True, если данный tg_id уже активировал данный промокод."""
    code = code.strip()
    if not code:
        return False
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM promocode_uses WHERE code = ? COLLATE NOCASE AND tg_id = ?",
            (code, int(tg_id)),
        ) as cursor:
            return await cursor.fetchone() is not None


async def list_promocodes(limit: int = 50) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT code, bonus_days, max_uses, used_count, revoked, created_at
            FROM promocodes
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ) as cursor:
            return list(await cursor.fetchall())


async def redeem_promocode(code: str, tg_id: int) -> tuple[str, Optional[int]]:
    """Атомарно «погашает» один use промокода для tg_id.

    Возвращает (status, bonus_days). status — одна из PROMO_* констант.
    bonus_days возвращается только при PROMO_OK.
    """
    code = code.strip()
    if not code:
        return PROMO_NOT_FOUND, None
    async with aiosqlite.connect(DB_PATH) as db:
        # Получаем запись промо
        async with db.execute(
            """
            SELECT code, bonus_days, max_uses, used_count, revoked
            FROM promocodes WHERE code = ? COLLATE NOCASE
            """,
            (code,),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return PROMO_NOT_FOUND, None
        actual_code, bonus_days, max_uses, _used_count, revoked = row
        if revoked:
            return PROMO_REVOKED, None

        # Этот юзер уже активировал данный промо?
        async with db.execute(
            "SELECT 1 FROM promocode_uses WHERE code = ? COLLATE NOCASE AND tg_id = ?",
            (actual_code, int(tg_id)),
        ) as cursor:
            if await cursor.fetchone():
                return PROMO_ALREADY_USED, None

        # Атомарный инкремент used_count с проверкой лимита и revoked.
        cursor = await db.execute(
            """
            UPDATE promocodes
            SET used_count = used_count + 1
            WHERE code = ? COLLATE NOCASE
              AND revoked = 0
              AND (max_uses IS NULL OR used_count < max_uses)
            """,
            (actual_code,),
        )
        if cursor.rowcount == 0:
            return PROMO_EXHAUSTED, None

        await db.execute(
            "INSERT INTO promocode_uses (code, tg_id, used_at) VALUES (?, ?, ?)",
            (actual_code, int(tg_id), int(time.time())),
        )
        await db.commit()
        return PROMO_OK, int(bonus_days)


# --- Settings (key/value) ---

async def get_setting(key: str) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
    return row[0] if row else None


async def set_setting(key: str, value: Optional[str]) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        if value is None:
            await db.execute("DELETE FROM settings WHERE key = ?", (key,))
        else:
            await db.execute(
                """
                INSERT INTO settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
        await db.commit()


# --- User deletion ---

async def delete_user(tg_id: int) -> None:
    """Удаляет запись о юзере, все его подписки и use-записи промокодов.
    Не удаляет аккаунты в Remnawave — это делает api.delete_user(uuid) из бота для каждой подписки.
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("DELETE FROM promocode_uses WHERE tg_id = ?", (int(tg_id),))
        await conn.execute("DELETE FROM subscriptions WHERE tg_id = ?", (int(tg_id),))
        await conn.execute("DELETE FROM users WHERE tg_id = ?", (int(tg_id),))
        await conn.commit()
