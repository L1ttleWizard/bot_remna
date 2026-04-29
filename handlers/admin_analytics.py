"""Аналитика для админа: сводка БД + панели, топ по трафику, истекающие, промокоды, токены, /stats."""
import asyncio
import html
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from aiogram import F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import auth
import database as db
from app import api, dp, safe_edit
from formatters import human_bytes


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
        [InlineKeyboardButton(text="📈 Топ по трафику", callback_data="admin_stats:traffic")],
        [InlineKeyboardButton(text="⏰ Скоро истекают (7 дн)", callback_data="admin_stats:expiring")],
        [InlineKeyboardButton(text="🎁 Промокоды", callback_data="admin_stats:promos")],
        [InlineKeyboardButton(text="🔑 Токены", callback_data="admin_stats:tokens")],
        [InlineKeyboardButton(text="🔄 Обновить сводку", callback_data="admin_stats")],
        [InlineKeyboardButton(text="🛠 В админ-панель", callback_data="admin_panel")],
    ])


def _stats_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ К аналитике", callback_data="admin_stats")],
    ])


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
        users = resp.get("users") or []
        total_panel = int(resp.get("total") or total_panel)
        if not users:
            break
        for u in users:
            uuid_v = u.get("uuid") or ""
            if not uuid_v:
                continue
            ut = u.get("userTraffic") or {}
            used = int(ut.get("usedTrafficBytes") or 0)
            life = int(ut.get("lifetimeUsedTrafficBytes") or 0)
            status = u.get("status") or "UNKNOWN"
            by_status[status] = by_status.get(status, 0) + 1
            traffic_lifetime += life
            by_uuid[uuid_v] = {
                "used": used,
                "lifetime": life,
                "period": 0,
                "status": status,
                "last_online": u.get("lastOnlineAt") or "",
                "username": u.get("username") or "",
                "expire_at": u.get("expireAt") or "",
            }
        start += ANALYTICS_PANEL_PAGE_SIZE
        if start >= total_panel:
            break

    if with_period_range and by_uuid:
        start_d, end_d = _analytics_date_range()
        uuids = list(by_uuid.keys())
        # Параллельно пакетами по 16, чтобы не ддосить панель и не ловить таймауты.
        chunk = 16
        results: list[Optional[int]] = []
        for i in range(0, len(uuids), chunk):
            batch = uuids[i:i + chunk]
            batch_results = await asyncio.gather(
                *(api.get_user_usage_range(u, start_d, end_d) for u in batch),
                return_exceptions=True,
            )
            for r in batch_results:
                results.append(None if isinstance(r, BaseException) else r)
        for uuid_v, period in zip(uuids, results):
            if period is None:
                continue
            by_uuid[uuid_v]["period"] = int(period)
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
    """Главная страница аналитики — сводка БД + панели."""
    db_stats = await db.stats_users()
    panel = await _collect_panel_traffic()
    by_status = panel["by_status"]
    status_lines = [
        f"  · {html.escape(k)}: <b>{v}</b>" for k, v in sorted(by_status.items(), key=lambda x: -x[1])
    ] or ["  · —"]
    text = (
        "📊 <b>Аналитика — сводка</b>\n\n"
        "<b>База бота</b>\n"
        f"  · Всего юзеров в БД: <b>{db_stats['total_users']}</b> "
        f"(админов: {db_stats['total_admins']})\n"
        f"  · С подписками: <b>{db_stats['users_with_subs']}</b>, "
        f"без подписок: <b>{db_stats['users_without_subs']}</b>\n"
        f"  · Подписок всего: <b>{db_stats['total_subscriptions']}</b>\n"
        f"  · Активных: <b>{db_stats['subs_active']}</b>, "
        f"истекли: <b>{db_stats['subs_expired']}</b>, "
        f"♾ без лимита времени: <b>{db_stats['subs_unlimited']}</b>\n"
        f"  · Истекают за 7 дн: <b>{db_stats['subs_expiring_7d']}</b>\n\n"
        "<b>Панель Remnawave</b>\n"
        f"  · Всего юзеров в панели: <b>{panel['total_panel']}</b>\n"
        f"  · Трафик (за всё время): <b>{html.escape(human_bytes(panel['traffic_lifetime']))}</b>\n"
        "  · По статусам:\n"
        + "\n".join(status_lines)
        + f"\n\n<i>Топ за {ANALYTICS_PERIOD_DAYS} дней — кнопка «📈 Топ по трафику».</i>"
    )
    await safe_edit(
        callback, text, parse_mode="HTML",
        reply_markup=_stats_keyboard(), prefer_edit=prefer_edit,
    )


async def _send_admin_stats_traffic(callback: CallbackQuery, *, prefer_edit: bool) -> None:
    panel = await _collect_panel_traffic(with_period_range=True)
    by_uuid = panel["by_uuid"]
    db_subs = await db.list_all_subscriptions_with_uuid(limit=10000)
    subs_by_uuid = {row[1]: row for row in db_subs}  # uuid → (tg_id, uuid, username)

    rows = []
    for uuid_v, info in by_uuid.items():
        rows.append((info["period"], info["lifetime"], uuid_v, info, subs_by_uuid.get(uuid_v)))
    rows.sort(key=lambda r: r[0], reverse=True)
    top = rows[:ANALYTICS_TOP_N]

    lines = [
        f"📈 <b>Топ-{ANALYTICS_TOP_N} по трафику за {ANALYTICS_PERIOD_DAYS} дней</b>\n"
        f"<i>Сумма трафика всех юзеров за окно: {html.escape(human_bytes(panel['traffic_period']))}.</i>\n",
    ]
    if not top:
        lines.append("Нет данных.")
    for i, (period, life, uuid_v, info, sub) in enumerate(top, 1):
        username_p = info.get("username") or "—"
        tg_part = ""
        if sub:
            tg_part = f" · tg=<code>{sub[0]}</code>"
        lines.append(
            f"{i}. <code>{html.escape(username_p)}</code>{tg_part} — "
            f"<b>{html.escape(human_bytes(period))}</b> "
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


@dp.callback_query(F.data == "admin_stats")
async def cb_admin_stats(callback: CallbackQuery):
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    await callback.answer("Собираю статистику…")
    await _send_admin_stats_summary(callback, prefer_edit=True)


@dp.callback_query(F.data.startswith("admin_stats:"))
async def cb_admin_stats_sub(callback: CallbackQuery):
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    section = callback.data.split(":", 1)[1]
    await callback.answer()
    if section == "traffic":
        await _send_admin_stats_traffic(callback, prefer_edit=True)
    elif section == "expiring":
        await _send_admin_stats_expiring(callback, prefer_edit=True)
    elif section == "promos":
        await _send_admin_stats_promos(callback, prefer_edit=True)
    elif section == "tokens":
        await _send_admin_stats_tokens(callback, prefer_edit=True)
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
