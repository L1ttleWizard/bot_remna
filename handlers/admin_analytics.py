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
import keyboards as kb
from app import api, dp, safe_edit
from formatters import human_bytes, format_expire_display

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
        [
            InlineKeyboardButton(text="📋 Дайджест", callback_data="admin_stats:digest_menu"),
            InlineKeyboardButton(text="🏆 Итоги (Recap)", callback_data="admin_stats:recap"),
        ],
        [
            InlineKeyboardButton(text="📈 Трафик системы", callback_data="admin_stats:bandwidth"),
            InlineKeyboardButton(text="📀 Ноды", callback_data="admin_stats:nodes_menu"),
        ],
        [
            InlineKeyboardButton(text="📱 Запросы & SRR", callback_data="admin_stats:srr"),
            InlineKeyboardButton(text="📱 Топ устройств", callback_data="admin_stats:top_hwid"),
        ],
        [
            InlineKeyboardButton(text="📅 Подписки & БД", callback_data="admin_stats:db_menu"),
            InlineKeyboardButton(text="📈 Топ по трафику", callback_data="admin_stats:traffic"),
        ],
        [
            InlineKeyboardButton(text="⏰ Срок действия", callback_data="admin_stats:expiring"),
            InlineKeyboardButton(text="🎁 Промокоды", callback_data="admin_stats:promos"),
        ],
        [
            InlineKeyboardButton(text="🔑 Токены", callback_data="admin_stats:tokens"),
            InlineKeyboardButton(text="🩺 Здоровье системы", callback_data="admin_stats:health"),
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


async def _send_admin_stats_digest(callback: CallbackQuery, period: str = "7d", *, prefer_edit: bool = True) -> None:
    now = datetime.now(timezone.utc)
    if period == "24h":
        start_dt = now - timedelta(days=1)
        period_label = "24 часа"
    elif period == "30d":
        start_dt = now - timedelta(days=30)
        period_label = "30 дней"
    else:
        start_dt = now - timedelta(days=7)
        period_label = "7 дней"

    start_iso = start_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end_iso = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    digest = await api.get_system_digest(start_iso, end_iso)
    if not digest:
        text = "❌ <b>Не удалось получить дайджест от сервера.</b>"
    else:
        users = digest.get("users") or {}
        traffic = digest.get("traffic") or {}
        hwid = digest.get("hwidDevices") or {}

        created_users = users.get("createdCount", 0)
        expired_users = users.get("expiredCount", 0)
        total_traffic_bytes = int(traffic.get("totalBytes") or 0)
        new_users_traffic_bytes = int(traffic.get("byUsersCreatedInRangeBytes") or 0)
        created_hwid = hwid.get("createdCount", 0)

        text = (
            f"📋 <b>Сводный дайджест системы ({period_label})</b>\n\n"
            f"👥 <b>Пользователи:</b>\n"
            f"  · Создано новых: <b>+{created_users}</b>\n"
            f"  · Истекло подписок: <b>{expired_users}</b>\n\n"
            f"📦 <b>Трафик:</b>\n"
            f"  · Общий объём: <b>{html.escape(human_bytes(total_traffic_bytes))}</b>\n"
            f"  · Трафик новых юзеров: <b>{html.escape(human_bytes(new_users_traffic_bytes))}</b>\n\n"
            f"📱 <b>Устройства:</b>\n"
            f"  · Добавлено HWID: <b>+{created_hwid}</b>"
        )

    await safe_edit(
        callback,
        text,
        parse_mode="HTML",
        reply_markup=kb.admin_stats_digest_keyboard(),
        prefer_edit=prefer_edit,
    )


async def _send_admin_stats_health(callback: CallbackQuery, *, prefer_edit: bool = True) -> None:
    health = await api.get_system_health()
    if not health:
        text = "❌ <b>Не удалось получить состояние сервисов от сервера.</b>"
    else:
        metrics = health.get("runtimeMetrics") or []
        lines = ["🩺 <b>Состояние сервисов Remnawave:</b>\n"]
        for m in metrics:
            inst_type = html.escape(str(m.get("instanceType") or "core"))
            uptime_s = int(m.get("uptime") or 0)
            hours = uptime_s // 3600
            mins = (uptime_s % 3600) // 60
            rss_mb = round(int(m.get("rss") or 0) / (1024 * 1024), 1)
            heap_mb = round(int(m.get("heapUsed") or 0) / (1024 * 1024), 1)
            heap_tot_mb = round(int(m.get("heapTotal") or 0) / (1024 * 1024), 1)
            ev_delay = round(float(m.get("eventLoopDelayMs") or 0), 2)
            lines.append(
                f"🟢 <b>Компонент <code>{inst_type}</code></b> (PID: {m.get('pid')}):\n"
                f"  · Uptime: <b>{hours}ч {mins}м</b>\n"
                f"  · RAM (RSS): <b>{rss_mb} MB</b> (Heap: {heap_mb}/{heap_tot_mb} MB)\n"
                f"  · Event Loop Delay: <b>{ev_delay} ms</b>\n"
            )
        text = "\n".join(lines)

    await safe_edit(
        callback,
        text,
        parse_mode="HTML",
        reply_markup=_stats_back_keyboard(),
        prefer_edit=prefer_edit,
    )


async def _send_admin_stats_recap(callback: CallbackQuery, *, prefer_edit: bool = True) -> None:
    recap = await api.get_system_recap()
    if not recap:
        text = "❌ <b>Не удалось получить итоги от сервера.</b>"
    else:
        this_m = recap.get("thisMonth") or {}
        tot = recap.get("total") or {}
        ver = html.escape(str(recap.get("version") or "3.4.3"))
        init_date = recap.get("initDate") or ""
        init_h = format_expire_display(init_date) if init_date else "—"

        m_users = this_m.get("users", 0)
        m_traffic = int(this_m.get("traffic") or 0)

        tot_users = tot.get("users", 0)
        tot_nodes = tot.get("nodes", 0)
        tot_traffic = int(tot.get("traffic") or 0)
        tot_ram = int(tot.get("nodesRam") or 0)
        tot_cores = tot.get("nodesCpuCores", 0)
        tot_countries = tot.get("distinctCountries", 0)

        text = (
            "🏆 <b>Итоги и сводка сервиса (Recap)</b>\n\n"
            "📅 <b>Текущий месяц:</b>\n"
            f"  · Новых пользователей: <b>+{m_users}</b>\n"
            f"  · Трафик за месяц: <b>{html.escape(human_bytes(m_traffic))}</b>\n\n"
            "🌐 <b>За всё время работы:</b>\n"
            f"  · Всего пользователей: <b>{tot_users}</b>\n"
            f"  · Всего трафика: <b>{html.escape(human_bytes(tot_traffic))}</b>\n"
            f"  · Серверов в кластере: <b>{tot_nodes}</b> в <b>{tot_countries}</b> странах\n"
            f"  · Мощность кластера: <b>{tot_cores} CPU cores</b> · <b>{html.escape(human_bytes(tot_ram))} RAM</b>\n\n"
            f"⏱ <b>Дата запуска:</b> {html.escape(init_h)} (v{ver})"
        )

    await safe_edit(
        callback,
        text,
        parse_mode="HTML",
        reply_markup=_stats_back_keyboard(),
        prefer_edit=prefer_edit,
    )


async def _send_admin_stats_bandwidth(callback: CallbackQuery, *, prefer_edit: bool = True) -> None:
    bw = await api.get_system_bandwidth()
    if not bw:
        text = "❌ <b>Не удалось получить статистику трафика от сервера.</b>"
    else:
        d2 = bw.get("bandwidthLastTwoDays") or {}
        d7 = bw.get("bandwidthLastSevenDays") or {}
        d30 = bw.get("bandwidthLast30Days") or {}
        c_month = bw.get("bandwidthCalendarMonth") or {}
        c_year = bw.get("bandwidthCurrentYear") or {}

        def _fmt_row(item: dict) -> str:
            cur = html.escape(str(item.get("current") or "0"))
            prev = html.escape(str(item.get("previous") or "0"))
            diff = html.escape(str(item.get("difference") or "0"))
            sign = "+" if not diff.startswith("-") and diff != "0" else ""
            return f"<b>{cur}</b> (пред.: {prev} · <i>{sign}{diff}</i>)"

        text = (
            "📈 <b>Агрегированный трафик системы</b>\n\n"
            f"• <b>За 2 дня:</b> {_fmt_row(d2)}\n"
            f"• <b>За 7 дней:</b> {_fmt_row(d7)}\n"
            f"• <b>За 30 дней:</b> {_fmt_row(d30)}\n"
            f"• <b>Текущий месяц:</b> {_fmt_row(c_month)}\n"
            f"• <b>Текущий год:</b> <b>{html.escape(str(c_year.get('current') or '0'))}</b>\n\n"
            "<i>Сравнение производится с аналогичным предыдущим периодом.</i>"
        )

    await safe_edit(
        callback,
        text,
        parse_mode="HTML",
        reply_markup=_stats_back_keyboard(),
        prefer_edit=prefer_edit,
    )


async def _send_admin_stats_top_hwid(callback: CallbackQuery, *, prefer_edit: bool = True) -> None:
    res = await api.get_top_hwid_users(start=0, size=15)
    if not res:
        text = "❌ <b>Не удалось получить топ по устройствам от сервера.</b>"
    else:
        users = res.get("users") or []
        total = res.get("total", len(users))
        lines = [f"📱 <b>Топ пользователей по HWID-устройствам</b> (всего: {total}):\n"]
        for idx, u in enumerate(users, 1):
            uname = html.escape(str(u.get("username") or u.get("id") or "—"))
            count = u.get("devicesCount", 0)
            lines.append(f"<b>{idx}.</b> <code>{uname}</code> — <b>{count}</b> устр.")
        lines.append("\n<i>Полезно для выявления шеринга и передачи ключей.</i>")
        text = "\n".join(lines)

    await safe_edit(
        callback,
        text,
        parse_mode="HTML",
        reply_markup=_stats_back_keyboard(),
        prefer_edit=prefer_edit,
    )


async def _send_admin_stats_srr(callback: CallbackQuery, *, prefer_edit: bool = True) -> None:
    stats_data = await api.get_subscription_request_history_stats()
    history_data = await api.get_all_subscription_request_history(limit=100)
    
    if not stats_data and not history_data:
        text = "❌ <b>Не удалось получить данные SRR и историю запросов.</b>"
    else:
        stats_obj = stats_data or {}
        records = (history_data.get("records") if isinstance(history_data, dict) else history_data) or []
        
        # 1. Apps Breakdown
        by_app = stats_obj.get("byParsedApp") or []
        total_app_requests = sum(item.get("count", 0) for item in by_app)
        
        # 2. Hourly Stats
        hourly = stats_obj.get("hourlyRequestStats") or []
        total_hourly_requests = sum(item.get("requestCount", 0) for item in hourly)
        total_requests = max(total_app_requests, total_hourly_requests, len(records))
        
        lines = [
            "📱 <b>Статистика запросов подписок и правил SRR</b>\n",
            f"📊 <b>Всего обращений:</b> <code>{total_requests}</code> (за последние 48ч)\n",
            "👥 <b>Популярность VPN-клиентов:</b>"
        ]
        
        if by_app:
            for item in by_app[:7]:
                app_name = item.get("app") or "Другие"
                cnt = item.get("count", 0)
                pct = (cnt / total_app_requests * 100) if total_app_requests > 0 else 0
                filled = int(round(pct / 10))
                bar = "█" * filled + "░" * (10 - filled)
                lines.append(f"  · <b>{html.escape(app_name)}</b>: {cnt} (<code>{pct:.1f}%</code>) {bar}")
        else:
            lines.append("  <i>Нет данных о приложениях</i>")
            
        # 3. Breakdown by SRR Rule Name & Format
        lines.append("\n🔄 <b>Правила маршрутизации (SRR Rules):</b>")
        if records:
            rule_counts = {}
            type_counts = {}
            for r in records:
                rname = r.get("srrRuleName") or "Fallback / Без правила"
                rtype = r.get("srrResponseType") or "UNKNOWN"
                rule_counts[rname] = rule_counts.get(rname, 0) + 1
                type_counts[rtype] = type_counts.get(rtype, 0) + 1
                
            tot_hist = len(records)
            for rname, cnt in sorted(rule_counts.items(), key=lambda x: -x[1]):
                pct = (cnt / tot_hist * 100)
                lines.append(f"  · <b>{html.escape(rname)}</b>: {cnt} (<code>{pct:.1f}%</code>)")
                
            lines.append("\n📦 <b>Форматы выдачи конфигов:</b>")
            type_icons = {
                "MIHOMO": "🟣",
                "SINGBOX": "🟢",
                "CLASH": "🟡",
                "XRAY_BASE64": "⚪️",
                "BROWSER": "🌐",
                "STASH": "🟠",
                "UNKNOWN": "❓",
            }
            for rtype, cnt in sorted(type_counts.items(), key=lambda x: -x[1]):
                icon = type_icons.get(rtype, "🔹")
                pct = (cnt / tot_hist * 100)
                lines.append(f"  {icon} <code>{html.escape(rtype)}</code>: {cnt} ({pct:.1f}%)")
        else:
            lines.append("  <i>Нет записей истории</i>")
            
        # 4. Hourly background update activity
        if hourly:
            recent_24 = hourly[-24:]
            counts = [item.get("requestCount", 0) for item in recent_24]
            max_c = max(counts) if counts else 1
            spark_chars = "  ▂▃▄▅▆▇█"
            sparkline = "".join(spark_chars[min(int(c / max_c * (len(spark_chars) - 1)), len(spark_chars) - 1)] for c in counts)
            
            bg_apps = {"happ", "hiddify", "sing-box", "karing", "clash", "flclash", "nekobox", "incy"}
            bg_count = sum(item.get("count", 0) for item in by_app if str(item.get("app", "")).lower() in bg_apps)
            bg_pct = (bg_count / total_app_requests * 100) if total_app_requests > 0 else 0
            
            lines.append("\n⏱ <b>Активность авто-обновлений (24ч):</b>")
            lines.append(f"<code>[{sparkline}]</code> (пик: {max_c} запр/час)")
            lines.append(f"🔄 Доля фоновых авто-синхронизаций: ~<b>{bg_pct:.1f}%</b>")

        text = "\n".join(lines)

    await safe_edit(
        callback,
        text,
        parse_mode="HTML",
        reply_markup=kb.admin_srr_stats_keyboard(),
        prefer_edit=prefer_edit,
    )


async def _send_admin_stats_srr_recent(callback: CallbackQuery, *, prefer_edit: bool = True) -> None:
    history_data = await api.get_all_subscription_request_history(limit=15)
    records = (history_data.get("records") if isinstance(history_data, dict) else history_data) or []
    
    if not records:
        text = "📋 <b>Последние запросы подписок</b>\n\n<i>История обращений пуста.</i>"
    else:
        lines = [f"📋 <b>Последние {len(records)} запросов подписок:</b>\n"]
        for r in records:
            req_at = r.get("requestAt") or ""
            dt_str = format_expire_display(req_at) if req_at else "—"
            ua = html.escape(str(r.get("userAgent") or "—")[:40])
            rname = html.escape(str(r.get("srrRuleName") or r.get("srrResponseType") or "Default"))
            ip = html.escape(str(r.get("requestIp") or "—"))
            uid = r.get("userId")
            u_str = f"user=<code>{uid}</code>" if uid else ""
            lines.append(f"⏱ <code>{dt_str}</code> · {u_str}\n   📱 <i>{ua}</i>\n   ⚡️ Правило: <b>{rname}</b> · IP: <code>{ip}</code>\n")
        text = "\n".join(lines)

    await safe_edit(
        callback,
        text,
        parse_mode="HTML",
        reply_markup=kb.admin_srr_recent_keyboard(),
        prefer_edit=prefer_edit,
    )


async def _send_admin_stats_srr_rules(callback: CallbackQuery, *, prefer_edit: bool = True) -> None:
    settings = await api.get_subscription_settings()
    if not settings:
        text = "⚙️ <b>Не удалось получить настройки SRR из панели.</b>"
    else:
        resp_obj = settings if "response" not in settings else settings["response"]
        rules = resp_obj.get("responseRules", {}).get("rules") or []
        custom_headers = resp_obj.get("customResponseHeaders") or {}
        update_interval = custom_headers.get("profile-update-interval") or "—"
        announce = custom_headers.get("announce") or "—"
        if announce.startswith("rwEncodeBase64:"):
            announce = announce.replace("rwEncodeBase64:", "")
                
        lines = [
            "⚙️ <b>Настройки маршрутизации (Subscription Response Rules)</b>\n",
            f"⏱ <b>Интервал авто-обновления профилей:</b> каждые <code>{update_interval}ч</code>",
            f"📢 <b>Анонс в заголовках:</b> <i>{html.escape(announce[:60])}</i>\n",
            f"📋 <b>Активные правила SRR ({len(rules)}):</b>"
        ]
        
        for idx, r in enumerate(rules, 1):
            rname = html.escape(str(r.get("name") or f"Правило #{idx}"))
            rtype = html.escape(str(r.get("responseType") or "DEFAULT"))
            enabled = "✅" if r.get("enabled", True) else "❌"
            conds = r.get("conditions") or []
            cond_str = f" ({len(conds)} условий)" if conds else " (fallback)"
            lines.append(f"{enabled} <b>{rname}</b> ➔ <code>{rtype}</code>{cond_str}")
            
        text = "\n".join(lines)

    await safe_edit(
        callback,
        text,
        parse_mode="HTML",
        reply_markup=kb.admin_srr_rules_keyboard(),
        prefer_edit=prefer_edit,
    )


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
    elif section == "digest_menu":
        await _send_admin_stats_digest(callback, "7d", prefer_edit=True)
    elif section.startswith("digest:"):
        period = section.split(":", 1)[1]
        await _send_admin_stats_digest(callback, period, prefer_edit=True)
    elif section == "recap":
        await _send_admin_stats_recap(callback, prefer_edit=True)
    elif section == "bandwidth":
        await _send_admin_stats_bandwidth(callback, prefer_edit=True)
    elif section == "top_hwid":
        await _send_admin_stats_top_hwid(callback, prefer_edit=True)
    elif section == "srr":
        await _send_admin_stats_srr(callback, prefer_edit=True)
    elif section == "srr_recent":
        await _send_admin_stats_srr_recent(callback, prefer_edit=True)
    elif section == "srr_rules":
        await _send_admin_stats_srr_rules(callback, prefer_edit=True)
    elif section == "health":
        await _send_admin_stats_health(callback, prefer_edit=True)
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
