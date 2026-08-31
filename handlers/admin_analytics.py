"""Аналитика для админа: сводка БД + панели, топ по трафику, истекающие, промокоды, токены, /stats."""
import asyncio
import html
import time
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from aiogram import F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    BufferedInputFile,
    InputMediaPhoto,
)

import auth
import database as db
from app import api, dp, safe_edit
from formatters import human_bytes

logger = logging.getLogger(__name__)

ANALYTICS_TOP_N = 10
ANALYTICS_PANEL_PAGE_SIZE = 200
ANALYTICS_MAX_PANEL_USERS = 5000
ANALYTICS_PERIOD_DAYS = 30


def _analytics_date_range(days: int = ANALYTICS_PERIOD_DAYS) -> tuple[str, str]:
    """(start, end) в формате YYYY-MM-DD для запроса к панели за последние N дней."""
    end_dt = datetime.now(timezone.utc).date()
    start_dt = end_dt - timedelta(days=days - 1)
    return start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d")


def _stats_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📀 Статистика нод", callback_data="admin_stats:nodes_menu")],
        [InlineKeyboardButton(text="📅 Подписки & БД", callback_data="admin_stats:db_menu")],
        [
            InlineKeyboardButton(text="📈 Топ по трафику", callback_data="admin_stats:traffic"),
            InlineKeyboardButton(text="⏰ Срок действия", callback_data="admin_stats:expiring"),
        ],
        [
            InlineKeyboardButton(text="🎁 Промокоды", callback_data="admin_stats:promos"),
            InlineKeyboardButton(text="🔑 Токены", callback_data="admin_stats:tokens"),
        ],
        [
            InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_stats"),
            InlineKeyboardButton(text="🛠 В админ-панель", callback_data="admin_panel"),
        ],
    ])


def _nodes_stats_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📈 Общий трафик (график)", callback_data="admin_stats:nodes_total_chart")],
        [InlineKeyboardButton(text="📊 Сравнение нод (график)", callback_data="admin_stats:nodes_compare_chart")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_stats")],
    ])


def _stats_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ К аналитике", callback_data="admin_stats")],
    ])


def draw_nodes_traffic_text_chart(nodes_traffic: list[tuple[str, int]]) -> str:
    if not nodes_traffic:
        return "Нет данных по трафику нод."
    total_traffic = sum(val for _, val in nodes_traffic)
    if total_traffic == 0:
        return "Трафик на всех нодах равен 0."
    
    lines = []
    for name, val in nodes_traffic:
        pct = (val / total_traffic) * 100
        filled = round(pct / 10)
        filled = max(0, min(10, filled))
        empty = 10 - filled
        bar = "█" * filled + "░" * empty
        lines.append(f"{html.escape(name):<15} | {bar} | {pct:>5.1f}% ({html.escape(human_bytes(val))})")
    return "\n".join(lines)


async def _collect_panel_traffic(
    max_users: int = ANALYTICS_MAX_PANEL_USERS,
    *,
    with_period_range: bool = False,
) -> dict:
    """Постранично выкачивает users из Remnawave, агрегирует трафик/статусы.

    Если ``with_period_range=True`` — для каждого юзера дополнительно дергает
    `/api/bandwidth-stats/users/{uuid}` за последние ANALYTICS_PERIOD_DAYS дней
    (это медленно при больших панелях — N запросов).

    Возвращает dict с ключами:
      total_panel, by_status, traffic_period (за окно или 0 если не считалось),
      traffic_lifetime, by_uuid (uuid → {used, lifetime, period, status,
      last_online, username, expire_at}), period_days.
    """
    by_uuid: dict = {}
    by_status: dict = {}
    total_panel = 0
    traffic_period_total = 0
    traffic_lifetime = 0
    start = 0
    while start < max_users:
        page = await api.list_users(size=ANALYTICS_PANEL_PAGE_SIZE, start=start)
        if not page or "response" not in page:
            break
        resp = page["response"]
        if isinstance(resp, list):
            users = resp
            total_panel = len(resp)
        elif isinstance(resp, dict):
            users = resp.get("users") or []
            total_panel = int(resp.get("total") or len(users))
        else:
            users = []

        if not users:
            break

        for u in users:
            uuid_v = u.get("uuid") or ""
            id_v = u.get("id")
            if not uuid_v and not id_v:
                continue
            key_v = uuid_v or str(id_v)
            ut = u.get("userTraffic") or {}
            used = int(ut.get("usedTrafficBytes") or 0)
            life = int(ut.get("lifetimeUsedTrafficBytes") or 0)
            status = u.get("status") or "UNKNOWN"
            by_status[status] = by_status.get(status, 0) + 1
            traffic_lifetime += life
            by_uuid[key_v] = {
                "id": id_v,
                "uuid": uuid_v,
                "used": used,
                "lifetime": life,
                "period": 0,
                "status": status,
                "last_online": u.get("lastOnlineAt") or "",
                "username": u.get("username") or "",
                "expire_at": u.get("expireAt") or "",
            }

        start += ANALYTICS_PANEL_PAGE_SIZE
        if start >= total_panel or isinstance(resp, list):
            break

    if with_period_range and by_uuid:
        start_d, end_d = _analytics_date_range()
        keys = list(by_uuid.keys())
        # Параллельно пакетами по 16, чтобы не ддосить панель и не ловить таймауты.
        chunk = 16
        results: list[Optional[int]] = []
        for i in range(0, len(keys), chunk):
            batch_keys = keys[i:i + chunk]
            batch_ids = [by_uuid[k].get("id") or by_uuid[k].get("uuid") or k for k in batch_keys]
            batch_results = await asyncio.gather(
                *(api.get_user_usage_range(uid, start_d, end_d) for uid in batch_ids),
                return_exceptions=True,
            )
            for r in batch_results:
                results.append(None if isinstance(r, BaseException) else r)

        for key_v, period in zip(keys, results):
            if period is None:
                # Если за период API вернул None, используем used
                period = by_uuid[key_v]["used"]
            by_uuid[key_v]["period"] = int(period)
            traffic_period_total += int(period)

    return {
        "total_panel": total_panel,
        "by_status": by_status,
        "traffic_period": traffic_period_total,
        "traffic_lifetime": traffic_lifetime,
        "by_uuid": by_uuid,
        "period_days": ANALYTICS_PERIOD_DAYS if with_period_range else 0,
    }


async def _send_admin_stats_summary(callback: CallbackQuery, *, prefer_edit: bool) -> None:
    """Главная страница аналитики — компактная сводка."""
    db_stats = await db.stats_users()
    panel = await _collect_panel_traffic()
    text = (
        "📊 <b>Аналитика — Главное меню</b>\n\n"
        "<b>База бота:</b>\n"
        f"  · Всего юзеров: <b>{db_stats['total_users']}</b>\n"
        f"  · Подписок: <b>{db_stats['total_subscriptions']}</b>\n\n"
        "<b>Панель Remnawave:</b>\n"
        f"  · Всего юзеров: <b>{panel['total_panel']}</b>\n"
        f"  · Трафик (всего): <b>{html.escape(human_bytes(panel['traffic_lifetime']))}</b>\n\n"
        "<i>Выберите интересующий раздел ниже для детальной информации:</i>"
    )
    await safe_edit(
        callback, text, parse_mode="HTML",
        reply_markup=_stats_keyboard(), prefer_edit=prefer_edit,
    )


async def _send_admin_stats_db(callback: CallbackQuery, *, prefer_edit: bool) -> None:
    """Подменю: Подписки & БД."""
    db_stats = await db.stats_users()
    text = (
        "📅 <b>Аналитика — Подписки & БД</b>\n\n"
        "<b>Статистика пользователей в БД</b>\n"
        f"  · Всего юзеров: <b>{db_stats['total_users']}</b> "
        f"(админов: {db_stats['total_admins']})\n"
        f"  · С подписками: <b>{db_stats['users_with_subs']}</b>, "
        f"без подписок: <b>{db_stats['users_without_subs']}</b>\n\n"
        "<b>Статистика подписок в БД</b>\n"
        f"  · Подписок всего: <b>{db_stats['total_subscriptions']}</b>\n"
        f"  · Активных: <b>{db_stats['subs_active']}</b>\n"
        f"  · Истекли: <b>{db_stats['subs_expired']}</b>\n"
        f"  · ♾ без лимита времени: <b>{db_stats['subs_unlimited']}</b>\n"
        f"  · Истекают за 7 дн: <b>{db_stats['subs_expiring_7d']}</b>"
    )
    await safe_edit(
        callback, text, parse_mode="HTML",
        reply_markup=_stats_back_keyboard(), prefer_edit=prefer_edit,
    )


async def _send_admin_stats_nodes_menu(callback: CallbackQuery, *, prefer_edit: bool) -> None:
    """Подменю: Детальная статистика нод."""
    nodes = await api.list_nodes()
    if nodes is None:
        nodes = []
    
    total_nodes = len(nodes)
    disabled_nodes = sum(1 for n in nodes if n.get("isDisabled"))
    active_nodes = total_nodes - disabled_nodes
    
    online_nodes = 0
    offline_nodes = 0
    for n in nodes:
        if n.get("isDisabled"):
            continue
        uptime_s = n.get("xrayUptime")
        is_connected = bool(n.get("isConnected"))
        if "isXrayRunning" in n:
            is_xray = bool(n.get("isXrayRunning"))
        else:
            try:
                is_xray = is_connected and uptime_s is not None and int(uptime_s) > 0
            except (TypeError, ValueError):
                is_xray = is_connected
        if is_xray:
            online_nodes += 1
        else:
            offline_nodes += 1
            
    total_online_users = sum(int(n.get("usersOnline") or 0) for n in nodes if not n.get("isDisabled"))
    
    start_d, end_d = _analytics_date_range(30)
    bw_stats = await api.get_nodes_bandwidth_stats(start_d, end_d)
    
    total_traffic_bytes = 0
    text_chart = ""
    
    if bw_stats and "series" in bw_stats:
        series = bw_stats.get("series") or []
        nodes_traffic = []
        for s in series:
            name = s.get("name") or "Unnamed"
            val = int(s.get("total") or sum(int(x) for x in s.get("data") or []))
            nodes_traffic.append((name, val))
        
        total_traffic_bytes = sum(val for _, val in nodes_traffic)
        nodes_traffic.sort(key=lambda x: x[1], reverse=True)
        text_chart = draw_nodes_traffic_text_chart(nodes_traffic)
    else:
        text_chart = "Нет данных по потреблению трафика."
        
    text = (
        "📀 <b>Аналитика — Статистика нод</b>\n\n"
        "<b>Текущее состояние серверов:</b>\n"
        f"  · Активных нод: <b>{active_nodes}</b> (онлайн: {online_nodes}, оффлайн: {offline_nodes})\n"
        f"  · Отключенных нод: <b>{disabled_nodes}</b>\n"
        f"  · Суммарный онлайн: <b>{total_online_users} чел.</b>\n\n"
        f"<b>Потребление трафика (за последние 30 дней):</b>\n"
        f"  · Общий трафик: <b>{html.escape(human_bytes(total_traffic_bytes))}</b>\n\n"
        "<b>Распределение по нодам:</b>\n"
        f"<pre>{text_chart}</pre>"
    )
    
    await safe_edit(
        callback, text, parse_mode="HTML",
        reply_markup=_nodes_stats_keyboard(), prefer_edit=prefer_edit,
    )


async def _send_admin_stats_traffic(callback: CallbackQuery, *, prefer_edit: bool) -> None:
    panel = await _collect_panel_traffic(with_period_range=True)
    by_uuid = panel["by_uuid"]
    db_subs = await db.list_all_subscriptions_with_uuid(limit=10000)
    subs_by_uuid = {row[1]: row for row in db_subs}  # uuid → (tg_id, uuid, username)

    rows = []
    for key_v, info in by_uuid.items():
        sub = subs_by_uuid.get(info.get("uuid")) or subs_by_uuid.get(key_v)
        sort_key = info["period"] if info["period"] > 0 else (info["used"] if info["used"] > 0 else info["lifetime"])
        rows.append((sort_key, info["period"], info["lifetime"], key_v, info, sub))
    rows.sort(key=lambda r: r[0], reverse=True)
    top = rows[:ANALYTICS_TOP_N]

    lines = [
        f"📈 <b>Топ-{ANALYTICS_TOP_N} по трафику за {ANALYTICS_PERIOD_DAYS} дней</b>\n"
        f"<i>Сумма трафика всех юзеров за окно: {html.escape(human_bytes(panel['traffic_period']))}.</i>\n",
    ]
    if not top:
        lines.append("Нет данных.")
    for i, (_sort_k, period, life, _key, info, sub) in enumerate(top, 1):
        username_p = info.get("username") or "—"
        tg_part = ""
        if sub:
            tg_part = f" · tg=<code>{sub[0]}</code>"
        val_str = human_bytes(period) if period > 0 else (f"{human_bytes(info.get('used', 0))} (тек.)" if info.get('used', 0) > 0 else human_bytes(0))
        lines.append(
            f"{i}. <code>{html.escape(username_p)}</code>{tg_part} — "
            f"<b>{html.escape(val_str)}</b> "
            f"(всего: {html.escape(human_bytes(life))})"
        )
    await safe_edit(
        callback, "\n".join(lines), parse_mode="HTML",
        reply_markup=_stats_back_keyboard(), prefer_edit=prefer_edit,
    )


async def _send_admin_stats_expiring(callback: CallbackQuery, *, prefer_edit: bool) -> None:
    expiring = await db.list_subs_expiring_in(7 * 24 * 3600, limit=50)
    lines = ["⏰ <b>Истекают в ближайшие 7 дней</b>\n"]
    if not expiring:
        lines.append("Пусто. Никто не истекает в этом окне.")
    now = int(time.time())
    for tg_id, uuid_v, _short, username_s, expire_date, sub_id, tg_username, tg_first, tg_last in expiring:
        days_left = max(0, (int(expire_date) - now) // 86400)
        when = datetime.fromtimestamp(int(expire_date)).strftime("%d.%m.%Y %H:%M")
        name_bits = []
        if tg_username:
            name_bits.append(f"@{html.escape(tg_username)}")
        if tg_first or tg_last:
            name_bits.append(html.escape(f"{tg_first or ''} {tg_last or ''}".strip()))
        name = " · ".join(name_bits) if name_bits else "—"
        lines.append(
            f"  · <code>{tg_id}</code> · {name} · "
            f"<code>{html.escape(username_s or '')}</code> — "
            f"до <b>{when}</b> ({days_left} дн)"
        )
    await safe_edit(
        callback, "\n".join(lines), parse_mode="HTML",
        reply_markup=_stats_back_keyboard(), prefer_edit=prefer_edit,
    )


async def _send_admin_stats_promos(callback: CallbackQuery, *, prefer_edit: bool) -> None:
    s = await db.stats_promocodes()
    lines = [
        "🎁 <b>Промокоды</b>\n",
        f"  · Всего: <b>{s['total']}</b> (активных: {s['active']}, отозванных: {s['revoked']})",
        f"  · Использований всего: <b>{s['total_uses']}</b>",
        f"  · Бонус-дней выдано: <b>{s['bonus_days_granted']}</b>",
        "",
        "<b>Топ-10 по использованию:</b>",
    ]
    if not s["top_codes"]:
        lines.append("  · —")
    for code, bonus, used, max_uses, revoked in s["top_codes"]:
        mu = "∞" if max_uses is None else str(max_uses)
        flag = "🚫" if revoked else "✅"
        lines.append(
            f"  {flag} <code>{html.escape(code)}</code> — +{bonus} дн., {used}/{mu}"
        )
    await safe_edit(
        callback, "\n".join(lines), parse_mode="HTML",
        reply_markup=_stats_back_keyboard(), prefer_edit=prefer_edit,
    )


async def _send_admin_stats_tokens(callback: CallbackQuery, *, prefer_edit: bool) -> None:
    s = await db.stats_tokens()
    lines = [
        "🔑 <b>Токены</b>\n",
        f"  · Всего выпущено: <b>{s['total']}</b>",
        f"  · Активных (не использованы, не отозваны): <b>{s['active']}</b>",
        f"  · Использовано: <b>{s['redeemed']}</b>",
        f"  · Отозвано: <b>{s['revoked']}</b>",
        "",
        "<b>По авторам выпуска:</b>",
    ]
    if not s["by_admin"]:
        lines.append("  · —")
    for created_by, issued, redeemed, revoked, active in s["by_admin"]:
        lines.append(
            f"  · admin <code>{created_by}</code>: всего {issued}, "
            f"активных {active}, использовано {redeemed}, отозвано {revoked}"
        )
    await safe_edit(
        callback, "\n".join(lines), parse_mode="HTML",
        reply_markup=_stats_back_keyboard(), prefer_edit=prefer_edit,
    )


async def cb_nodes_total_chart(callback: CallbackQuery):
    await callback.answer("Генерируем график общего трафика...")
    try:
        start_d, end_d = _analytics_date_range(30)
        bw_stats = await api.get_nodes_bandwidth_stats(start_d, end_d)
        
        if not bw_stats or "categories" not in bw_stats or "sparklineData" not in bw_stats:
            await callback.message.answer("❌ Не удалось получить данные статистики от API панели.")
            return
            
        categories = bw_stats["categories"] or []
        sparkline_data = bw_stats["sparklineData"] or []
        
        if not categories or not sparkline_data:
            await callback.message.answer("❌ Данные статистики трафика пусты.")
            return
            
        from services.chart_generator import generate_total_traffic_chart
        loop = asyncio.get_running_loop()
        image_bytes = await loop.run_in_executor(
            None, generate_total_traffic_chart, categories, sparkline_data
        )
        
        back_kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_stats:nodes_total_chart"),
                InlineKeyboardButton(text="◀️ Назад к статистике нод", callback_data="admin_stats:nodes_menu"),
            ]
        ])
        
        caption_text = "📈 <b>График общего трафика серверов за последние 30 дней</b>"
        photo_file = BufferedInputFile(image_bytes, filename="total_traffic_chart.png")
        
        if callback.message.photo:
            try:
                await callback.message.edit_media(
                    media=InputMediaPhoto(
                        media=photo_file,
                        caption=caption_text,
                        parse_mode="HTML"
                    ),
                    reply_markup=back_kb
                )
                return
            except Exception as e:
                logger.info("Failed to edit media in-place for nodes_total_chart: %s", e)
                
        await callback.message.delete()
        await callback.message.answer_photo(
            photo=photo_file,
            caption=caption_text,
            parse_mode="HTML",
            reply_markup=back_kb
        )
        
    except Exception as e:
        logger.exception("Ошибка при генерации графика общего трафика: %s", e)
        await callback.message.answer(f"❌ Не удалось сгенерировать график: {e}")


async def cb_nodes_compare_chart(callback: CallbackQuery):
    await callback.answer("Генерируем сравнительный график нод...")
    try:
        start_d, end_d = _analytics_date_range(30)
        bw_stats = await api.get_nodes_bandwidth_stats(start_d, end_d)
        
        if not bw_stats or "categories" not in bw_stats or "series" not in bw_stats:
            await callback.message.answer("❌ Не удалось получить данные статистики от API панели.")
            return
            
        categories = bw_stats["categories"] or []
        series = bw_stats["series"] or []
        
        if not categories or not series:
            await callback.message.answer("❌ Данные статистики для сравнения нод пусты.")
            return
            
        from services.chart_generator import generate_nodes_traffic_comparison_chart
        loop = asyncio.get_running_loop()
        image_bytes = await loop.run_in_executor(
            None, generate_nodes_traffic_comparison_chart, categories, series
        )
        
        back_kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_stats:nodes_compare_chart"),
                InlineKeyboardButton(text="◀️ Назад к статистике нод", callback_data="admin_stats:nodes_menu"),
            ]
        ])
        
        caption_text = "📊 <b>Сравнение распределения трафика по серверам за последние 30 дней</b>"
        photo_file = BufferedInputFile(image_bytes, filename="nodes_compare_chart.png")
        
        if callback.message.photo:
            try:
                await callback.message.edit_media(
                    media=InputMediaPhoto(
                        media=photo_file,
                        caption=caption_text,
                        parse_mode="HTML"
                    ),
                    reply_markup=back_kb
                )
                return
            except Exception as e:
                logger.info("Failed to edit media in-place for nodes_compare_chart: %s", e)
                
        await callback.message.delete()
        await callback.message.answer_photo(
            photo=photo_file,
            caption=caption_text,
            parse_mode="HTML",
            reply_markup=back_kb
        )
        
    except Exception as e:
        logger.exception("Ошибка при генерации сравнительного графика нод: %s", e)
        await callback.message.answer(f"❌ Не удалось сгенерировать график: {e}")


@dp.callback_query(F.data == "admin_stats")
async def cb_admin_stats(callback: CallbackQuery):
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    await callback.answer("Собираю статистику…")
    if callback.message.photo:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await _send_admin_stats_summary(callback, prefer_edit=False)
    else:
        await _send_admin_stats_summary(callback, prefer_edit=True)


@dp.callback_query(F.data.startswith("admin_stats:"))
async def cb_admin_stats_sub(callback: CallbackQuery):
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    section = callback.data.split(":", 1)[1]
    
    if section not in ("nodes_total_chart", "nodes_compare_chart"):
        await callback.answer()
        
    if section == "traffic":
        await _send_admin_stats_traffic(callback, prefer_edit=True)
    elif section == "expiring":
        await _send_admin_stats_expiring(callback, prefer_edit=True)
    elif section == "promos":
        await _send_admin_stats_promos(callback, prefer_edit=True)
    elif section == "tokens":
        await _send_admin_stats_tokens(callback, prefer_edit=True)
    elif section == "db_menu":
        await _send_admin_stats_db(callback, prefer_edit=True)
    elif section == "nodes_menu":
        if callback.message.photo:
            try:
                await callback.message.delete()
            except Exception:
                pass
            await _send_admin_stats_nodes_menu(callback, prefer_edit=False)
        else:
            await _send_admin_stats_nodes_menu(callback, prefer_edit=True)
    elif section == "nodes_total_chart":
        await cb_nodes_total_chart(callback)
    elif section == "nodes_compare_chart":
        await cb_nodes_compare_chart(callback)
    else:
        await _send_admin_stats_summary(callback, prefer_edit=True)


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if not await auth.is_admin(message.from_user.id):
        await message.answer("Команда доступна только администратору.")
        return
    db_stats = await db.stats_users()
    panel = await _collect_panel_traffic()
    by_status = panel["by_status"]
    status_line = ", ".join(f"{k}={v}" for k, v in sorted(by_status.items(), key=lambda x: -x[1])) or "—"
    text = (
        "📊 <b>Аналитика — сводка</b>\n\n"
        f"БД: юзеров <b>{db_stats['total_users']}</b> "
        f"(админов {db_stats['total_admins']}), "
        f"подписок <b>{db_stats['total_subscriptions']}</b> "
        f"(активных {db_stats['subs_active']}, истекли {db_stats['subs_expired']}, "
        f"♾ {db_stats['subs_unlimited']}, истекают за 7д {db_stats['subs_expiring_7d']})\n\n"
        f"Панель: всего юзеров <b>{panel['total_panel']}</b>; "
        f"трафик за всё время <b>{html.escape(human_bytes(panel['traffic_lifetime']))}</b>; "
        f"статусы: {html.escape(status_line)}\n\n"
        "Подробнее — <code>/admin → 📊 Аналитика</code>."
    )
    await message.answer(text, parse_mode="HTML")
