import logging
import time

from aiogram import Bot

import database as db

logger = logging.getLogger(__name__)


async def check_expiring_subscriptions(bot: Bot) -> None:
    """Напоминания об истечении подписки. Шлются раз в сутки на окне «3, 1, 0 дней до конца»
    для каждой подписки каждого пользователя.
    """
    now = int(time.time())
    day_sec = 24 * 60 * 60
    window_end = now + 3 * day_sec

    rows = await db.list_all_active_subscriptions(now, window_end)

    for tg_id, sub_id, expire_date, role in rows:
        days_left = (expire_date - now) // day_sec
        if days_left not in (0, 1, 3):
            continue
        if days_left == 0:
            head = f"⚠️ Подписка #{sub_id} заканчивается <b>сегодня</b>."
        elif days_left == 1:
            head = f"⚠️ Подписка #{sub_id} заканчивается <b>завтра</b>."
        else:
            head = f"⚠️ Подписка #{sub_id} заканчивается через <b>3 дня</b>."

        if role == "admin":
            tail = "Продлите её в админ-панели: <code>/admin → 👥 Пользователи</code>."
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
            logger.warning("Напоминание об истечении не отправлено %s/sub=%s: %s", tg_id, sub_id, e)
