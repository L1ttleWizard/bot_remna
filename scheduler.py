import logging
import time

import aiosqlite
from aiogram import Bot

from database import DB_PATH

logger = logging.getLogger(__name__)


async def check_expiring_subscriptions(bot: Bot) -> None:
    """Напоминания об истечении подписки. Шлются раз в сутки в окне «3, 1, 0 дней до конца».

    Чтобы не дублировать напоминания, опираемся на округление `(expire_date - now) // 86400`
    и предполагаем, что воркер запускается раз в сутки (см. SCHEDULER_CRON_HOUR/MINUTE).
    """
    now = int(time.time())
    day_sec = 24 * 60 * 60
    window_end = now + 3 * day_sec

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT tg_id, expire_date, role
            FROM users
            WHERE expire_date IS NOT NULL
              AND expire_date > ?
              AND expire_date <= ?
            """,
            (now, window_end),
        ) as cursor:
            rows = await cursor.fetchall()

    for tg_id, expire_date, role in rows:
        days_left = (expire_date - now) // day_sec
        if days_left not in (0, 1, 3):
            continue
        if days_left == 0:
            head = "⚠️ Ваша подписка заканчивается <b>сегодня</b>."
        elif days_left == 1:
            head = "⚠️ Ваша подписка заканчивается <b>завтра</b>."
        else:
            head = "⚠️ Ваша подписка заканчивается через <b>3 дня</b>."

        if role == "admin":
            tail = "Продлите её в админ-панели: <code>/admin → ⚙️ Мой аккаунт → 📅 Подписка</code>."
        else:
            tail = (
                "Активируйте промокод в боте (кнопка «🎁 Промокод») "
                "или обратитесь к администратору через «❓ Поддержка»."
            )
        try:
            await bot.send_message(
                chat_id=tg_id,
                text=f"{head} {tail}",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning("Напоминание об истечении не отправлено %s: %s", tg_id, e)
