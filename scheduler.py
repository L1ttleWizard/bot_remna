import logging
import time
from datetime import datetime, timedelta, timezone

from aiogram import Bot

import database as db
from config import ADMIN_TG_IDS
from app import (
    CLIENT_NOTIFY_DAYS_KEY,
    CLIENT_NOTIFY_ENABLED_KEY,
    CLIENT_NOTIFY_TEXT_KEY,
    ADMIN_NOTIFY_DAYS_KEY,
    ADMIN_NOTIFY_ENABLED_KEY,
    ADMIN_NOTIFY_TEXT_KEY,
    NODE_DOWN_NOTIFY_ENABLED_KEY,
    CPU_NOTIFY_ENABLED_KEY,
    CPU_THRESHOLD_KEY,
    CPU_SUSTAINED_MINUTES_KEY,
    api,
)

logger = logging.getLogger(__name__)

# Moscow timezone (UTC+3) is the default for formatting dates in text.
MSK = timezone(timedelta(hours=3))

DEFAULT_CLIENT_TEMPLATE = (
    "⚠️ Ваша подписка #{sub_id} ({username}) заканчивается {days_word} ({date}).\n\n"
    "Активируйте промокод в боте (кнопка «🎁 Промокод») или обратитесь к администратору через «❓ Поддержка»."
)

DEFAULT_ADMIN_TEMPLATE = (
    "🔔 <b>Дайджест истекающих подписок:</b>\n\n"
    "{list}\n\n"
    "Управление пользователями: <code>/admin</code>"
)


def get_days_word(days: int) -> str:
    if days == 0:
        return "сегодня"
    elif days == 1:
        return "завтра"
    elif days == -1:
        return "вчера"
    elif days > 0:
        if days % 10 == 1 and days % 100 != 11:
            return f"через {days} день"
        elif days % 10 in (2, 3, 4) and days % 100 not in (12, 13, 14):
            return f"через {days} дня"
        else:
            return f"через {days} дней"
    else:
        abs_days = abs(days)
        if abs_days % 10 == 1 and abs_days % 100 != 11:
            return f"{abs_days} день назад"
        elif abs_days % 10 in (2, 3, 4) and abs_days % 100 not in (12, 13, 14):
            return f"{abs_days} дня назад"
        else:
            return f"{abs_days} дней назад"


def format_client_notification(
    template: str,
    days_left: int,
    sub_id: int,
    username: str,
    label: str,
    expire_date: int,
    tg_username: str,
    tg_first_name: str,
    tg_last_name: str,
) -> str:
    dt = datetime.fromtimestamp(expire_date, MSK)
    date_str = dt.strftime("%d.%m.%Y %H:%M")

    # Fallbacks
    label_val = label if label else ""
    username_val = username if username else ""
    tg_user = f"@{tg_username}" if tg_username else ""
    first_name = tg_first_name if tg_first_name else ""
    last_name = tg_last_name if tg_last_name else ""
    full_name = f"{first_name} {last_name}".strip() or "Пользователь"

    days_word = get_days_word(days_left)

    tpl = template if template else DEFAULT_CLIENT_TEMPLATE

    # If using default template and sub is already expired:
    if not template and days_left < 0:
        tpl = (
            "❌ Ваша подписка #{sub_id} ({username}) истекла {days_word} ({date}).\n\n"
            "Вы можете продлить её, активировав промокод (кнопка «🎁 Промокод») или связавшись с администратором."
        )

    try:
        return tpl.format(
            sub_id=sub_id,
            username=username_val,
            label=label_val,
            days=days_left,
            days_word=days_word,
            date=date_str,
            tg_username=tg_username or "",
            tg_user=tg_user,
            tg_first_name=tg_first_name or "",
            tg_last_name=tg_last_name or "",
            full_name=full_name,
        )
    except Exception as e:
        logger.warning("Failed to format client notification template: %s. Using default fallback.", e)
        # Safe fallback
        fallback = (
            f"⚠️ Подписка #{sub_id} ({username_val}) заканчивается {days_word} ({date_str})."
            if days_left >= 0 else
            f"❌ Подписка #{sub_id} ({username_val}) истекла {days_word} ({date_str})."
        )
        return fallback


def build_admin_digest_text(template: str, subs: list) -> str:
    # Group subs by days_left
    from collections import defaultdict
    groups = defaultdict(list)
    for sub_id, username, label, tg_username, tg_first_name, tg_last_name, days_left, expire_date in subs:
        groups[days_left].append((sub_id, username, label, tg_username, tg_first_name, tg_last_name, expire_date))

    list_parts = []
    # Sort groups so upcoming are on top, then expired at the bottom
    sorted_days = sorted(groups.keys(), key=lambda x: (x < 0, abs(x)))

    for d in sorted_days:
        sub_list = groups[d]
        days_word = get_days_word(d)
        if d == 0:
            header = "⏳ <b>Заканчиваются сегодня (0 дн.):</b>"
        elif d == 1:
            header = "⏳ <b>Заканчиваются завтра (1 дн.):</b>"
        elif d < 0:
            header = f"❌ <b>Истекли {days_word} ({d} дн.):</b>"
        else:
            header = f"⏳ <b>Заканчиваются {days_word} ({d} дн.):</b>"

        list_parts.append(header)
        for sub_id, username, label, tg_username, tg_first_name, tg_last_name, expire_date in sub_list:
            label_part = f", {label}" if label else ""

            # Telegram profile info formatting
            first_name = tg_first_name if tg_first_name else ""
            last_name = tg_last_name if tg_last_name else ""
            full_name = f"{first_name} {last_name}".strip()

            if tg_username:
                user_part = f"@{tg_username}"
                if full_name:
                    user_part += f" ({full_name})"
            else:
                user_part = full_name if full_name else f"ID: {sub_id}"

            list_parts.append(f"• #{sub_id} ({username}{label_part}) — {user_part}")

        list_parts.append("")  # empty line after group

    list_str = "\n".join(list_parts).strip()

    tpl = template if template else DEFAULT_ADMIN_TEMPLATE

    try:
        return tpl.format(list=list_str)
    except Exception as e:
        logger.warning("Failed to format admin digest template: %s. Using default.", e)
        return DEFAULT_ADMIN_TEMPLATE.format(list=list_str)


async def check_expiring_subscriptions(bot: Bot) -> None:
    """Напоминания об истечении подписки для клиентов и сводный дайджест для администраторов."""
    now = int(time.time())
    day_sec = 24 * 60 * 60

    # 1. Загрузка настроек
    client_enabled = (await db.get_setting(CLIENT_NOTIFY_ENABLED_KEY)) != "0"
    admin_enabled = (await db.get_setting(ADMIN_NOTIFY_ENABLED_KEY)) != "0"

    client_days_str = (await db.get_setting(CLIENT_NOTIFY_DAYS_KEY)) or "3,1,0"
    admin_days_str = (await db.get_setting(ADMIN_NOTIFY_DAYS_KEY)) or "3,1,0,-1"

    try:
        client_days = [int(x.strip()) for x in client_days_str.split(",") if x.strip()]
    except ValueError:
        client_days = [3, 1, 0]

    try:
        admin_days = [int(x.strip()) for x in admin_days_str.split(",") if x.strip()]
    except ValueError:
        admin_days = [3, 1, 0, -1]

    client_text = await db.get_setting(CLIENT_NOTIFY_TEXT_KEY)
    admin_text = await db.get_setting(ADMIN_NOTIFY_TEXT_KEY)

    # Если всё выключено — выходим
    if not client_enabled and not admin_enabled:
        logger.info("Subscription notifications are disabled for both clients and admins.")
        return

    # 2. Вычисляем временной интервал для поиска подписок
    all_target_days = set(client_days) | set(admin_days)
    if not all_target_days:
        return

    min_target = min(all_target_days)
    max_target = max(all_target_days)

    # Добавим буфер в 2 дня, чтобы захватить всё
    start_ts = now + (min_target - 2) * day_sec
    end_ts = now + (max_target + 2) * day_sec

    # 3. Выборка подписок
    rows = await db.list_expiring_and_expired_subscriptions(start_ts, end_ts)
    if not rows:
        return

    # 4. Обработка уведомлений клиентов
    if client_enabled:
        for tg_id, sub_id, expire_date, role, label, username, tg_username, tg_first_name, tg_last_name in rows:
            # Считаем дни до конца
            days_left = (expire_date - now) // day_sec
            if days_left not in client_days:
                continue

            # Проверяем дубликат
            if await db.was_notification_sent(tg_id, sub_id, days_left):
                continue

            # Отправляем сообщение клиенту
            msg_text = format_client_notification(
                client_text,
                days_left,
                sub_id,
                username,
                label,
                expire_date,
                tg_username,
                tg_first_name,
                tg_last_name,
            )
            try:
                await bot.send_message(chat_id=tg_id, text=msg_text, parse_mode="HTML")
                await db.mark_notification_sent(tg_id, sub_id, days_left)
                logger.info("Sent subscription notification to client %s for sub #%s (days left: %s)", tg_id, sub_id, days_left)
            except Exception as e:
                logger.warning("Failed to send notification to client %s for sub #%s: %s", tg_id, sub_id, e)

    # 5. Обработка уведомлений администраторов (дайджест)
    if admin_enabled:
        # Список админов
        db_admins = await db.list_admins()
        admins = set(db_admins) | ADMIN_TG_IDS

        if admins:
            for admin_id in admins:
                admin_digest_subs = []
                for tg_id, sub_id, expire_date, role, label, username, tg_username, tg_first_name, tg_last_name in rows:
                    days_left = (expire_date - now) // day_sec
                    if days_left not in admin_days:
                        continue

                    # Проверяем, было ли уже отправлено это уведомление этому админу
                    if await db.was_notification_sent(admin_id, sub_id, days_left):
                        continue

                    admin_digest_subs.append((sub_id, username, label, tg_username, tg_first_name, tg_last_name, days_left, expire_date))

                if admin_digest_subs:
                    digest_text = build_admin_digest_text(admin_text, admin_digest_subs)
                    try:
                        await bot.send_message(chat_id=admin_id, text=digest_text, parse_mode="HTML")
                        logger.info("Sent daily subscription digest to admin %s (%s subs)", admin_id, len(admin_digest_subs))
                        # Маркируем как отправленное
                        for sub_item in admin_digest_subs:
                            sub_id, _, _, _, _, _, days_left, _ = sub_item
                            await db.mark_notification_sent(admin_id, sub_id, days_left)
                    except Exception as e:
                        logger.warning("Failed to send admin digest to %s: %s", admin_id, e)

    # 6. Очистка старых логов (старше 30 дней)
    try:
        thirty_days_ago = now - 30 * day_sec
        await db.cleanup_old_notifications(thirty_days_ago)
    except Exception as e:
        logger.warning("Failed to cleanup old notifications: %s", e)


async def check_nodes_health(bot: Bot) -> None:
    """Периодическая проверка доступности нод (каждые 2 минуты)."""
    # 1. Загрузка настроек
    enabled = (await db.get_setting(NODE_DOWN_NOTIFY_ENABLED_KEY)) != "0"
    if not enabled:
        return

    # 2. Получение списка нод
    nodes = await api.list_nodes()
    if not nodes:
        logger.warning("check_nodes_health: не удалось получить список нод")
        return

    # 3. Получение списка админов для алертов
    db_admins = await db.list_admins()
    admins = set(db_admins) | ADMIN_TG_IDS

    now_ts = int(time.time())

    for node in nodes:
        uuid = node.get("uuid")
        if not uuid:
            continue

        # Пропускаем отключенные вручную ноды
        if node.get("isDisabled"):
            continue

        name = node.get("name") or "Без названия"
        is_connected = bool(node.get("isConnected"))
        address = node.get("address") or "—"
        port = node.get("port") or "—"

        status_row = await db.get_node_status(uuid)

        # Обновляем/вставляем статус в БД
        await db.upsert_node_status(uuid, name, is_connected, now_ts)

        if status_row is not None:
            was_connected = status_row["was_connected"]
            alerted_down = status_row["alerted_down"]

            # Переход: Был онлайн -> Стал оффлайн
            if was_connected and not is_connected:
                if not alerted_down:
                    # Отправляем алерт админам
                    alert_text = (
                        f"🔴 <b>Сервер оффлайн!</b>\n\n"
                        f"Сервер: <b>{name}</b>\n"
                        f"Адрес: <code>{address}:{port}</code>\n"
                        f"Состояние: 🔴 offline"
                    )
                    for admin_id in admins:
                        try:
                            await bot.send_message(chat_id=admin_id, text=alert_text, parse_mode="HTML")
                        except Exception as e:
                            logger.warning("Failed to send node down alert to %s: %s", admin_id, e)
                    await db.mark_node_alerted(uuid)
                    logger.info("Sent node down alert for node %s (%s)", name, uuid)

            # Переход: Был оффлайн -> Стал онлайн
            elif not was_connected and is_connected:
                if alerted_down:
                    # Отправляем сообщение о восстановлении
                    recovery_text = (
                        f"🟢 <b>Сервер снова онлайн</b>\n\n"
                        f"Сервер: <b>{name}</b>\n"
                        f"Адрес: <code>{address}:{port}</code>\n"
                        f"Состояние: 🟢 online"
                    )
                    for admin_id in admins:
                        try:
                            await bot.send_message(chat_id=admin_id, text=recovery_text, parse_mode="HTML")
                        except Exception as e:
                            logger.warning("Failed to send node recovery message to %s: %s", admin_id, e)
                    await db.clear_node_alert(uuid)
                    logger.info("Sent node recovery message for node %s (%s)", name, uuid)


async def check_cpu_load(bot: Bot) -> None:
    """Периодическая проверка загрузки CPU на нодах (каждые 3 минуты)."""
    # 1. Загрузка настроек
    enabled = (await db.get_setting(CPU_NOTIFY_ENABLED_KEY)) != "0"
    if not enabled:
        return

    threshold = int((await db.get_setting(CPU_THRESHOLD_KEY)) or "80")
    sustained_minutes = int((await db.get_setting(CPU_SUSTAINED_MINUTES_KEY)) or "5")

    # 2. Получение списка нод
    nodes = await api.list_nodes()
    if not nodes:
        return

    # 3. Получение списка админов для алертов
    db_admins = await db.list_admins()
    admins = set(db_admins) | ADMIN_TG_IDS

    now_ts = int(time.time())

    for node in nodes:
        uuid = node.get("uuid")
        if not uuid:
            continue

        # Пропускаем отключенные/оффлайн ноды, сбрасывая их лог CPU
        if node.get("isDisabled") or not node.get("isConnected"):
            await db.clear_cpu_high(uuid)
            continue

        name = node.get("name") or "Без названия"
        address = node.get("address") or "—"
        port = node.get("port") or "—"

        # Запрашиваем подробную инфо для получения stats
        payload = await api.get_node(uuid)
        node_card = (payload or {}).get("response") if isinstance(payload, dict) else None
        if not node_card:
            continue

        sys_stats = (node_card.get("system") or {}).get("stats") or {}

        # Извлекаем CPU по разным возможным ключам
        cpu_usage = None
        for key in ("cpu", "cpuUsage", "cpu_usage", "cpuPercent"):
            val = sys_stats.get(key)
            if val is not None:
                try:
                    f_val = float(val)
                    # Эвристика: если значение <= 1.0 (например, 0.85 для 85%),
                    # а порог задан как > 1.0 (например, 80), то переводим в проценты.
                    if f_val <= 1.0 and f_val > 0.0 and threshold > 1.0:
                        cpu_usage = f_val * 100.0
                    else:
                        cpu_usage = f_val
                    break
                except (ValueError, TypeError):
                    pass

        if cpu_usage is None:
            # CPU stats не поддерживаются или отсутствуют в ответе панели
            continue

        # Проверяем превышение порога
        if cpu_usage > threshold:
            await db.upsert_cpu_high(uuid, name, now_ts)
            cpu_high = await db.get_cpu_high(uuid)

            if cpu_high:
                first_high_ts = cpu_high["first_high_ts"]
                alerted = cpu_high["alerted"]
                duration_sec = now_ts - first_high_ts

                if duration_sec >= sustained_minutes * 60:
                    if not alerted:
                        # Отправляем алерт админам
                        alert_text = (
                            f"⚠️ <b>Высокая загрузка CPU!</b>\n\n"
                            f"Сервер: <b>{name}</b>\n"
                            f"Адрес: <code>{address}:{port}</code>\n"
                            f"Загрузка CPU: <b>{cpu_usage:.1f}%</b> (порог: {threshold}%)\n"
                            f"Длительность: &gt; {sustained_minutes} мин."
                        )
                        for admin_id in admins:
                            try:
                                await bot.send_message(chat_id=admin_id, text=alert_text, parse_mode="HTML")
                            except Exception as e:
                                logger.warning("Failed to send CPU alert to %s: %s", admin_id, e)
                        await db.mark_cpu_alerted(uuid)
                        logger.info("Sent CPU alert for node %s (%s): %s%%", name, uuid, cpu_usage)
        else:
            # Загрузка нормализовалась (или в пределах нормы)
            cpu_high = await db.get_cpu_high(uuid)
            if cpu_high and cpu_high["alerted"]:
                # Отправляем сообщение о нормализации
                recovery_text = (
                    f"🟢 <b>Загрузка CPU нормализовалась</b>\n\n"
                    f"Сервер: <b>{name}</b>\n"
                    f"Адрес: <code>{address}:{port}</code>\n"
                    f"Загрузка CPU: <b>{cpu_usage:.1f}%</b> (порог: {threshold}%)"
                )
                for admin_id in admins:
                    try:
                        await bot.send_message(chat_id=admin_id, text=recovery_text, parse_mode="HTML")
                    except Exception as e:
                        logger.warning("Failed to send CPU recovery message to %s: %s", admin_id, e)
                logger.info("Sent CPU recovery for node %s (%s): %s%%", name, uuid, cpu_usage)

            await db.clear_cpu_high(uuid)
