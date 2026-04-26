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
                created_by INTEGER
            )
            """
        )
        await _ensure_user_columns(db)

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
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users (tg_id, uuid, short_uuid, username, expire_date, role, created_at, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tg_id) DO UPDATE SET
                uuid=excluded.uuid,
                short_uuid=excluded.short_uuid,
                username=excluded.username,
                expire_date=excluded.expire_date,
                created_by=COALESCE(users.created_by, excluded.created_by)
            """,
            (
                tg_id,
                uuid,
                short_uuid,
                username,
                expire_date,
                role,
                int(time.time()),
                created_by,
            ),
        )
        await db.commit()


async def get_user(tg_id: int):
    """Return legacy 5-tuple (tg_id, uuid, short_uuid, username, expire_date) for back-compat."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT tg_id, uuid, short_uuid, username, expire_date FROM users WHERE tg_id = ?",
            (tg_id,),
        ) as cursor:
            return await cursor.fetchone()


async def get_user_full(tg_id: int):
    """Return (tg_id, uuid, short_uuid, username, expire_date, role) or None."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT tg_id, uuid, short_uuid, username, expire_date, role FROM users WHERE tg_id = ?",
            (tg_id,),
        ) as cursor:
            return await cursor.fetchone()


async def update_user_expire(tg_id: int, expire_date: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET expire_date = ? WHERE tg_id = ?",
            (expire_date, tg_id),
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
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT tg_id, uuid, short_uuid, username, expire_date, role
            FROM users
            ORDER BY COALESCE(created_at, 0) DESC, tg_id DESC
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
            WHERE token_hash = ? AND consumed_by_tg_id IS NULL
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
