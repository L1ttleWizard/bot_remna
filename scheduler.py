import logging
import time

import aiosqlite
from aiogram import Bot

from database import DB_PATH

logger = logging.getLogger(__name__)


async def check_expiring_subscriptions(bot: Bot) -> None:
    """Напоминание за 3 дня до окончания подписки (один раз — когда остаётся ровно 3 полных дня)."""
    now = int(time.time())
    day_sec = 24 * 60 * 60
    window_end = now + 3 * day_sec

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT tg_id, expire_date FROM users WHERE expire_date > ? AND expire_date <= ?",
            (now, window_end),
        ) as cursor:
            rows = await cursor.fetchall()

    for tg_id, expire_date in rows:
        days_left = (expire_date - now) // day_sec
        if days_left != 3:
            continue
        try:
            await bot.send_message(
                chat_id=tg_id,
                text=(
                    "⚠️ Ваша подписка на прокси заканчивается через 3 дня. "
                    "Продлите её в боте: «Управление подпиской»."
                ),
            )
        except Exception as e:
            logger.warning("Напоминание об истечении не отправлено %s: %s", tg_id, e)
