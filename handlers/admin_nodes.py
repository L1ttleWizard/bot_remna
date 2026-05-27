"""Админ: управление нодами Remnawave.

UI:
- `admin_nodes` — список нод (компактный, c индикаторами online/disabled)
- `nodes:card:<uuid>` — карточка с метриками
- `nodes:act:<action>:<uuid>` — restart / enable / disable / reset_traffic / delete
- `nodes:del_confirm:<uuid>` — подтверждение удаления
- `nodes:restart_all` — перезапуск всех нод
- `/nodes` — текстовый дайджест

Отдельный flow «➕ Добавить ноду» (SSH на master + ansible-playbook) будет в `handlers/admin_add_node.py`.
"""
import html
import logging
from typing import Any

from aiogram import F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import auth
import config
from app import api, dp, safe_edit
from formatters import human_bytes

logger = logging.getLogger(__name__)


# ---------- helpers ----------

def _node_status_emoji(node: dict) -> str:
    """Иконка по xrayUptime / isConnected / isDisabled."""
    if node.get("isDisabled"):
        return "⏸"
    if node.get("isConnected") or node.get("isXrayRunning"):
        return "🟢"
    return "🔴"


def _node_brief_line(node: dict) -> str:
    name = html.escape(str(node.get("name") or "—")[:32])
    addr = html.escape(str(node.get("address") or "—"))
    port = node.get("port") or "—"
    cc = node.get("countryCode") or ""
    flag = f" ({html.escape(cc)})" if cc else ""
    return f"{_node_status_emoji(node)} <b>{name}</b>{flag} · <code>{addr}:{port}</code>"


def _nodes_list_keyboard(nodes: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for n in nodes:
        uuid = n.get("uuid") or ""
        if not uuid:
            continue
        label = f"{_node_status_emoji(n)} {str(n.get('name') or '—')[:24]}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"nodes:card:{uuid}")])
    if config.master_ssh_configured():
        rows.append([InlineKeyboardButton(text="➕ Добавить ноду", callback_data="addnode:start")])
    rows.append([
        InlineKeyboardButton(text="🔄 Перезапустить все", callback_data="nodes:restart_all_confirm"),
        InlineKeyboardButton(text="🔁 Обновить", callback_data="admin_nodes"),
    ])
    rows.append([InlineKeyboardButton(text="◀️ В админ-панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _node_card_keyboard(node: dict) -> InlineKeyboardMarkup:
    uuid = node.get("uuid") or ""
    is_disabled = bool(node.get("isDisabled"))
    rows = [
        [
            InlineKeyboardButton(text="🔄 Перезапустить", callback_data=f"nodes:act:restart:{uuid}"),
            InlineKeyboardButton(
                text="▶️ Включить" if is_disabled else "⏸ Отключить",
                callback_data=f"nodes:act:{'enable' if is_disabled else 'disable'}:{uuid}",
            ),
        ],
        [
            InlineKeyboardButton(text="🧹 Сбросить трафик", callback_data=f"nodes:act:reset_traffic:{uuid}"),
            InlineKeyboardButton(text="🗑 Удалить ноду", callback_data=f"nodes:del_confirm:{uuid}"),
        ],
        [
            InlineKeyboardButton(text="🔁 Обновить", callback_data=f"nodes:card:{uuid}"),
            InlineKeyboardButton(text="◀️ К списку", callback_data="admin_nodes"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_cores_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "ядро"
    elif n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "ядра"
    else:
        return "ядер"


def _node_card_text(node: dict) -> str:
    name = html.escape(str(node.get("name") or "—"))
    addr = html.escape(str(node.get("address") or "—"))
    port = node.get("port") or "—"
    cc = html.escape(str(node.get("countryCode") or ""))
    is_disabled = bool(node.get("isDisabled"))
    is_connected = bool(node.get("isConnected"))
    uptime_s = node.get("xrayUptime")
    # Remnawave API больше не отдаёт `isXrayRunning` / `xrayVersion` на верхнем
    # уровне ноды. Состояние xray вычисляем как «нода connected + xray uptime>0»
    # (uptime в секундах, считается с момента запуска xray на remnanode).
    # Если поля isXrayRunning ещё приходит у каких-то нод — учитываем как fallback.
    versions = node.get("versions") or {}
    xray_ver_raw = (
        node.get("xrayVersion")
        or versions.get("xray")
        or "—"
    )
    xray_ver = html.escape(str(xray_ver_raw))
    if "isXrayRunning" in node:
        is_xray = bool(node.get("isXrayRunning"))
    else:
        try:
            is_xray = is_connected and uptime_s is not None and int(uptime_s) > 0
        except (TypeError, ValueError):
            is_xray = is_connected
    last_status_change = html.escape(str(node.get("lastStatusChange") or "—"))
    last_status_msg = html.escape(str(node.get("lastStatusMessage") or "—")[:200])
    users_online = node.get("usersOnline") or 0
    traffic_used = node.get("trafficUsedBytes") or 0
    traffic_limit = node.get("trafficLimitBytes") or 0
    traffic_reset = html.escape(str(node.get("trafficResetDay") or "—"))
    # В новой схеме железо ноды лежит в node["system"]["info"] / ["stats"].
    sys_info = (node.get("system") or {}).get("info") or {}
    sys_stats = (node.get("system") or {}).get("stats") or {}

    cpu = node.get("cpuModel") or sys_info.get("cpuModel")
    cpus = sys_info.get("cpus") or 1
    load_avg = sys_stats.get("loadAvg") or []

    total_ram = node.get("totalRam") or sys_info.get("memoryTotal")
    used_ram = sys_stats.get("memoryUsed")

    lines = [
        f"🌐 <b>{name}</b> {f'({cc})' if cc else ''}",
        f"<code>{addr}:{port}</code>",
        "",
        f"Статус: {'⏸ отключена' if is_disabled else ('🟢 online' if is_connected else '🔴 offline')}",
        f"Xray: {'✅ работает' if is_xray else '❌ не работает'} · версия <code>{xray_ver}</code>",
    ]
    if uptime_s is not None:
        try:
            sec = int(uptime_s)
            d, sec = divmod(sec, 86400)
            h, sec = divmod(sec, 3600)
            m, _ = divmod(sec, 60)
            uptime_str = f"{d}д {h}ч {m}м" if d else f"{h}ч {m}м"
            lines.append(f"Аптайм Xray: {uptime_str}")
        except Exception:
            pass
    lines.append(f"Юзеров online: <b>{users_online}</b>")
    lines.append("")
    lines.append("📊 <b>Трафик</b>")
    if traffic_limit:
        lines.append(f"Использовано: {html.escape(human_bytes(traffic_used))} / {html.escape(human_bytes(traffic_limit))}")
    else:
        lines.append(f"Использовано: {html.escape(human_bytes(traffic_used))} (без лимита)")
    if node.get("trafficResetDay"):
        lines.append(f"Сброс трафика: число {traffic_reset} каждого месяца")

    if cpu or total_ram or load_avg:
        lines.append("")
        lines.append("🖥 <b>Железо</b>")
        if cpu:
            cpu_cores = f" ({cpus} {get_cores_word(cpus)})" if cpus else ""
            lines.append(f"  · CPU: {html.escape(str(cpu))}{cpu_cores}")
        if load_avg and len(load_avg) >= 3:
            load_1m_pct = (load_avg[0] / cpus) * 100.0
            load_5m_pct = (load_avg[1] / cpus) * 100.0
            load_15m_pct = (load_avg[2] / cpus) * 100.0

            lines.append(f"  · Load Average: <code>{', '.join(f'{x:.2f}' for x in load_avg)}</code>")
            lines.append(f"  · Загрузка (1/5/15 мин): <b>{load_1m_pct:.1f}%</b> / <b>{load_5m_pct:.1f}%</b> / <b>{load_15m_pct:.1f}%</b>")
        if total_ram:
            try:
                total_ram_val = int(total_ram)
                if used_ram is not None:
                    used_ram_val = int(used_ram)
                    ram_pct = (used_ram_val / total_ram_val) * 100.0
                    lines.append(
                        f"  · RAM: <b>{html.escape(human_bytes(used_ram_val))}</b> / "
                        f"{html.escape(human_bytes(total_ram_val))} "
                        f"(<b>{ram_pct:.1f}%</b>)"
                    )
                else:
                    lines.append(f"  · RAM: {html.escape(human_bytes(total_ram_val))}")
            except (ValueError, TypeError):
                lines.append(f"  · RAM: {html.escape(str(total_ram))}")
    lines.append("")
    lines.append(f"Последнее изменение статуса: {last_status_change}")
    if last_status_msg and last_status_msg != "—":
        lines.append(f"Сообщение: <i>{last_status_msg}</i>")
    return "\n".join(lines)


def _ok_alert(action: str, success: bool) -> str:
    return ("✅ " if success else "❌ ") + {
        "restart": "Команда на перезапуск отправлена" if success else "Не удалось перезапустить",
        "enable": "Нода включена" if success else "Не удалось включить",
        "disable": "Нода отключена" if success else "Не удалось отключить",
        "reset_traffic": "Трафик сброшен" if success else "Не удалось сбросить трафик",
        "delete": "Нода удалена" if success else "Не удалось удалить",
        "restart_all": "Команда на перезапуск всех нод отправлена" if success else "Не удалось перезапустить все",
    }.get(action, "Готово" if success else "Ошибка")


# ---------- handlers ----------

async def _render_nodes_list(callback: CallbackQuery) -> None:
    """Дёргает список нод и редактирует сообщение. НЕ вызывает callback.answer().

    Используется и из самого `cb_admin_nodes`, и из других хендлеров (после
    delete / restart_all), которые уже сделали callback.answer() со своим алертом.
    """
    nodes = await api.list_nodes()
    if nodes is None:
        await safe_edit(
            callback,
            "❌ Не удалось получить список нод от панели.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔁 Повторить", callback_data="admin_nodes"),
                InlineKeyboardButton(text="◀️ Назад", callback_data="admin_panel"),
            ]]),
            prefer_edit=True,
        )
        return
    if not nodes:
        empty_rows = []
        if config.master_ssh_configured():
            empty_rows.append([InlineKeyboardButton(text="➕ Добавить ноду", callback_data="addnode:start")])
        empty_rows.append([InlineKeyboardButton(text="◀️ В админ-панель", callback_data="admin_panel")])
        await safe_edit(
            callback,
            "🌐 <b>Ноды</b>\n\n<i>Нод пока нет.</i> Используй «➕ Добавить ноду», когда поднимешь сервер.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=empty_rows),
            prefer_edit=True,
        )
        return

    online = sum(1 for n in nodes if n.get("isConnected") and not n.get("isDisabled"))
    disabled = sum(1 for n in nodes if n.get("isDisabled"))
    total_users = sum((n.get("usersOnline") or 0) for n in nodes)
    total_traffic = sum((n.get("trafficUsedBytes") or 0) for n in nodes)

    lines = [
        f"🌐 <b>Ноды</b> ({len(nodes)})",
        f"🟢 online: <b>{online}</b> · ⏸ disabled: <b>{disabled}</b> · 🔴 offline: <b>{len(nodes) - online - disabled}</b>",
        f"Юзеров online суммарно: <b>{total_users}</b>",
        f"Трафик суммарно: <b>{html.escape(human_bytes(total_traffic))}</b>",
        "",
        "Выбери ноду чтобы открыть карточку:",
    ]
    for n in nodes:
        lines.append(_node_brief_line(n))

    await safe_edit(
        callback,
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_nodes_list_keyboard(nodes),
        prefer_edit=True,
    )


@dp.callback_query(F.data == "admin_nodes")
async def cb_admin_nodes(callback: CallbackQuery):
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Доступ только для администраторов.", show_alert=True)
        return
    await callback.answer("Загрузка…")
    await _render_nodes_list(callback)


@dp.callback_query(F.data.startswith("nodes:card:"))
async def cb_node_card(callback: CallbackQuery):
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Доступ только для администраторов.", show_alert=True)
        return
    uuid = callback.data.split(":", 2)[2]
    payload = await api.get_node(uuid)
    node = (payload or {}).get("response") if isinstance(payload, dict) else None
    if not node:
        await callback.answer("Нода не найдена.", show_alert=True)
        return
    await callback.answer("Загрузка…")
    await safe_edit(
        callback,
        _node_card_text(node),
        parse_mode="HTML",
        reply_markup=_node_card_keyboard(node),
        prefer_edit=True,
    )


@dp.callback_query(F.data.startswith("nodes:act:"))
async def cb_node_action(callback: CallbackQuery):
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Доступ только для администраторов.", show_alert=True)
        return
    parts = callback.data.split(":", 3)
    if len(parts) != 4:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    action = parts[2]
    uuid = parts[3]
    method_map: dict[str, Any] = {
        "restart": api.restart_node,
        "enable": api.enable_node,
        "disable": api.disable_node,
        "reset_traffic": api.reset_node_traffic,
        "delete": api.delete_node,
    }
    method = method_map.get(action)
    if not method:
        await callback.answer("Неизвестное действие.", show_alert=True)
        return
    ok = await method(uuid)
    await callback.answer(_ok_alert(action, ok), show_alert=True)
    if action == "delete":
        # после удаления карточки уже нет — возвращаемся к списку
        if ok:
            await _render_nodes_list(callback)
        return
    # перерисуем карточку для нерегруппирующих действий
    payload = await api.get_node(uuid)
    node = (payload or {}).get("response") if isinstance(payload, dict) else None
    if node:
        await safe_edit(
            callback,
            _node_card_text(node),
            parse_mode="HTML",
            reply_markup=_node_card_keyboard(node),
            prefer_edit=True,
        )


@dp.callback_query(F.data.startswith("nodes:del_confirm:"))
async def cb_node_del_confirm(callback: CallbackQuery):
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Доступ только для администраторов.", show_alert=True)
        return
    uuid = callback.data.split(":", 2)[2]
    await safe_edit(
        callback,
        "⚠️ <b>Удалить ноду?</b>\n\nЭто действие нельзя отменить. "
        "Нода будет удалена из панели Remnawave; контейнеры на самом сервере останутся работать "
        "до тех пор, пока ты не остановишь их вручную.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"nodes:act:delete:{uuid}")],
            [InlineKeyboardButton(text="◀️ Отмена", callback_data=f"nodes:card:{uuid}")],
        ]),
        prefer_edit=True,
    )
    await callback.answer()


@dp.callback_query(F.data == "nodes:restart_all_confirm")
async def cb_nodes_restart_all_confirm(callback: CallbackQuery):
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Доступ только для администраторов.", show_alert=True)
        return
    await safe_edit(
        callback,
        "⚠️ <b>Перезапустить все ноды?</b>\n\nКоманда будет отправлена на все активные ноды одновременно. "
        "Подключения юзеров оборвутся на 5–10 секунд.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Да, перезапустить все", callback_data="nodes:restart_all")],
            [InlineKeyboardButton(text="◀️ Отмена", callback_data="admin_nodes")],
        ]),
        prefer_edit=True,
    )
    await callback.answer()


@dp.callback_query(F.data == "nodes:restart_all")
async def cb_nodes_restart_all(callback: CallbackQuery):
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Доступ только для администраторов.", show_alert=True)
        return
    ok = await api.restart_all_nodes()
    await callback.answer(_ok_alert("restart_all", ok), show_alert=True)
    await _render_nodes_list(callback)


# ---------- /nodes command (текстовый дайджест) ----------

@dp.message(Command("nodes"))
async def cmd_nodes(message: Message):
    if not await auth.is_admin(message.from_user.id):
        return
    nodes = await api.list_nodes()
    if nodes is None:
        await message.answer("❌ Не удалось получить список нод.")
        return
    if not nodes:
        await message.answer("🌐 Нод пока нет.")
        return
    lines = [f"🌐 <b>Ноды</b> ({len(nodes)})", ""]
    for n in nodes:
        lines.append(_node_brief_line(n))
        users = n.get("usersOnline") or 0
        traffic = n.get("trafficUsedBytes") or 0
        if users or traffic:
            lines.append(
                f"   юзеров online: {users} · трафик: {html.escape(human_bytes(traffic))}"
            )
    await message.answer("\n".join(lines), parse_mode="HTML")



