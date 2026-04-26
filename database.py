import os

import aiosqlite

DB_PATH = os.environ.get("DATABASE_PATH", "bot_database.db")


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                tg_id INTEGER PRIMARY KEY,
                uuid TEXT,            -- Длинный UUID для API
                short_uuid TEXT,      -- Короткий токен для подписки
                username TEXT,
                expire_date TIMESTAMP
            )
        """)
        await db.commit()

async def add_user(tg_id: int, uuid: str, short_uuid: str, username: str, expire_date: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO users (tg_id, uuid, short_uuid, username, expire_date) VALUES (?, ?, ?, ?, ?)",
            (tg_id, uuid, short_uuid, username, expire_date)
        )
        await db.commit()

async def get_user(tg_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,)) as cursor:
            return await cursor.fetchone()


async def update_user_expire(tg_id: int, expire_date: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET expire_date = ? WHERE tg_id = ?",
            (expire_date, tg_id),
        )
        await db.commit()