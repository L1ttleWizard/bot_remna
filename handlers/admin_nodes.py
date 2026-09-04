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
import asyncio
from typing import Any

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
import config
import database as db
import keyboards as kb
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
        InlineKeyboardButton(text="⚡ Speedtest всех", callback_data="nodes:speedtest_all"),
    ])
    rows.append([
        InlineKeyboardButton(text="🔁 Обновить", callback_data="admin_nodes"),
        InlineKeyboardButton(text="◀️ В админ-панель", callback_data="admin_panel"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _node_card_keyboard(node: dict) -> InlineKeyboardMarkup:
    uuid = node.get("uuid") or ""
    is_disabled = bool(node.get("isDisabled"))
    rows = [
        [
            InlineKeyboardButton(text="🔄 Перезапустить", callback_data=f"nodes:act:restart:{uuid}"),
            InlineKeyboardButton(
                text="🔗 Включить" if is_disabled else "⏸ Отключить",
                callback_data=f"nodes:act:{'enable' if is_disabled else 'disable'}:{uuid}",
            ),
        ],
        [
            InlineKeyboardButton(text="🖥 Метрики", callback_data=f"nodes:metrics:{uuid}"),
            InlineKeyboardButton(text="👥 Онлайн", callback_data=f"nodes:conn:{uuid}"),
        ],
        [
            InlineKeyboardButton(text="🌍 Гео-тест", callback_data=f"nodes:geocheck:{uuid}"),
            InlineKeyboardButton(text="👥 Топ юзеров", callback_data=f"nodes:bw_users:{uuid}"),
        ],
        [
            InlineKeyboardButton(text="💳 Биллинг", callback_data=f"nodes:billing:{uuid}"),
            InlineKeyboardButton(text="📈 График нагрузки", callback_data=f"nodes:chart:{uuid}"),
        ],
        [
            InlineKeyboardButton(text="🗹 Сбросить трафик", callback_data=f"nodes:act:reset_traffic:{uuid}"),
            InlineKeyboardButton(text="🗑 Удалить ноду", callback_data=f"nodes:del_confirm:{uuid}"),
        ],
        [
            InlineKeyboardButton(text="⚡ Speedtest", callback_data=f"nodes:speedtest:{uuid}"),
            InlineKeyboardButton(text="🔁 Обновить", callback_data=f"nodes:card:{uuid}"),
        ],
        [
            InlineKeyboardButton(text="✈️ К списку", callback_data="admin_nodes"),
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


async def _node_card_text(node: dict) -> str:
    uuid = node.get("uuid") or ""
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
      # биллинг ноды (если привязана)
    if uuid:
        try:
            from handlers.admin_billing import _find_billing_for_node, _fmt_date, _days_until
            bn = await _find_billing_for_node(uuid)
            if bn:
                prov = bn.get("provider", {}).get("name") or bn.get("providerName") or "—"
                d = _days_until(bn.get("nextBillingAt"))
                tail = f" (через {d} дн.)" if d is not None else ""
                lines.append("")
                lines.append("💳 <b>Биллинг</b>")
                lines.append(f"  · Провайдер: <b>{html.escape(str(prov))}</b>")
                lines.append(f"  · Следующее списание: <b>{_fmt_date(bn.get('nextBillingAt'))}</b>{tail}")
        except Exception as e:
            logger.warning(f"node billing block: {e}")

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
        await _node_card_text(node),
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
            await _node_card_text(node),
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


@dp.callback_query(F.data.startswith("nodes:chart:"))
async def cb_node_chart(callback: CallbackQuery):
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Доступ только для администраторов.", show_alert=True)
        return
        
    uuid = callback.data.split(":", 2)[2]
    
    # 1. Fetch node info to get name
    payload = await api.get_node(uuid)
    node = (payload or {}).get("response") if isinstance(payload, dict) else None
    if not node:
        await callback.answer("Нода не найдена.", show_alert=True)
        return
        
    node_name = node.get("name") or "Без названия"
    await callback.answer("Генерируем график нагрузки...")
    
    # 2. Get metric history for the last 24 hours
    try:
        metrics = await db.get_node_metrics_history(uuid, hours=24)
        
        # 3. Generate chart using our service
        from services.chart_generator import generate_node_load_chart
        loop = asyncio.get_running_loop()
        image_bytes = await loop.run_in_executor(
            None, generate_node_load_chart, node_name, metrics
        )
        
        # 4. Create an inline keyboard to go back to the node card or refresh the chart
        back_kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 Обновить график", callback_data=f"nodes:chart:{uuid}"),
                InlineKeyboardButton(text="◀️ К ноде", callback_data=f"nodes:card:{uuid}"),
            ]
        ])
        
        # 5. Send/update the photo
        photo_file = BufferedInputFile(image_bytes, filename=f"load_chart_{uuid}.png")
        
        # Check if the current message is already a photo message (to update in-place)
        if callback.message.photo:
            try:
                await callback.message.edit_media(
                    media=InputMediaPhoto(
                        media=photo_file, 
                        caption=f"📈 График нагрузки ноды <b>{html.escape(node_name)}</b> за последние 24 часа.", 
                        parse_mode="HTML"
                    ),
                    reply_markup=back_kb
                )
                return
            except Exception as e:
                logger.info("Failed to edit media in-place: %s. Falling back to delete and send.", e)

        # Otherwise, delete the original text card and send a new photo message
        await callback.message.delete()
        await callback.message.answer_photo(
            photo=photo_file,
            caption=f"📈 График нагрузки ноды <b>{html.escape(node_name)}</b> за последние 24 часа.",
            parse_mode="HTML",
            reply_markup=back_kb
        )
        
    except Exception as e:
        logger.exception("Ошибка при генерации графика нагрузки: %s", e)
        await callback.message.answer(f"❌ Не удалось сгенерировать график нагрузки: {e}")


async def _execute_node_speedtest(node: dict) -> tuple[bool, str, str, str, str]:
    """
    Выполняет замер скорости на ноде через SSH с помощью встроенного python-скрипта.
    Возвращает (success, download, upload, ping, error_msg).
    """
    name = node.get("name") or "Без названия"
    address = node.get("address")
    if not address:
        return False, "—", "—", "—", "У ноды нет IP-адреса."
        
    cmd = (
        'echo "aW1wb3J0IHVybGxpYi5yZXF1ZXN0LCB0aW1lLCBzc2wKZGVmIHRlc3Rfc3BlZWQoKToKICAgIHRyeToKICAgICAgICBzc2wuX2NyZWF0ZV9kZWZhdWx0X2h0dHBzX2NvbnRleHQgPSBzc2wuX2NyZWF0ZV91bnZlcmlmaWVkX2NvbnRleHQKICAgIGV4Y2VwdCBFeGNlcHRpb246CiAgICAgICAgcGFzcwogICAgb3BlbmVyID0gdXJsbGliLnJlcXVlc3QuYnVpbGRfb3BlbmVyKCkKICAgIG9wZW5lci5hZGRoZWFkZXJzID0gWygnVXNlci1BZ2VudCcsICdNb3ppbGxhLzUuMCAoV2luZG93cyBOVCAxMC4wOyBXaW42NDsgeDY0KSBBcHBsZVdlYktpdC81MzcuMzYnKV0KICAgIHVybGxpYi5yZXF1ZXN0Lmluc3RhbGxfb3BlbmVyKG9wZW5lcikKICAgIHBpbmdzID0gW10KICAgIGZvciBfIGluIHJhbmdlKDMpOgogICAgICAgIHQwID0gdGltZS50aW1lKCkKICAgICAgICB0cnk6CiAgICAgICAgICAgIHVybGxpYi5yZXF1ZXN0LnVybG9wZW4oJ2h0dHBzOi8vc3BlZWQuY2xvdWRmbGFyZS5jb20vY2RuLWNnaS90cmFjZScsIHRpbWVvdXQ9MikKICAgICAgICAgICAgcGluZ3MuYXBwZW5kKCh0aW1lLnRpbWUoKSAtIHQwKSAqIDEwMDApCiAgICAgICAgZXhjZXB0IEV4Y2VwdGlvbjoKICAgICAgICAgICAgcGFzcwogICAgcGluZyA9IHN1bShwaW5ncykgLyBsZW4ocGluZ3MpIGlmIHBpbmdzIGVsc2UgOTk5LjAKICAgIHQwID0gdGltZS50aW1lKCkKICAgIHRyeToKICAgICAgICB1cmxsaWIucmVxdWVzdC51cmxyZXRyaWV2ZSgnaHR0cHM6Ly9zcGVlZC5jbG91ZGZsYXJlLmNvbS9fX2Rvd24/Ynl0ZXM9MTA0ODU3NjAnLCAnL2Rldi9udWxsJykKICAgICAgICBkdCA9IHRpbWUudGltZSgpIC0gdDAKICAgICAgICBkb3dubG9hZCA9ICgxMCAqIDgpIC8gZHQKICAgIGV4Y2VwdCBFeGNlcHRpb246CiAgICAgICAgZG93bmxvYWQgPSAwLjAKICAgIHQwID0gdGltZS50aW1lKCkKICAgIHRyeToKICAgICAgICBkYXRhID0gYicwJyAqIDUyNDI4ODAKICAgICAgICByZXEgPSB1cmxsaWIucmVxdWVzdC5SZXF1ZXN0KCdodHRwczovL3NwZWVkLmNsb3VkZmxhcmUuY29tL19fdXAnLCBkYXRhPWRhdGEsIG1ldGhvZD0nUE9TVCcpCiAgICAgICAgdXJsbGliLnJlcXVlc3QudXJsb3BlbihyZXEpCiAgICAgICAgZHQgPSB0aW1lLnRpbWUoKSAtIHQwCiAgICAgICAgdXBsb2FkID0gKDUgKiA4KSAvIGR0CiAgICBleGNlcHQgRXhjZXB0aW9uOgogICAgICAgIHVwbG9hZCA9IDAuMAogICAgcHJpbnQoJ1Bpbmc6JywgZid7aW50KHBpbmcpfSBtcycpCiAgICBwcmludCgnRG93bmxvYWQ6JywgZid7ZG93bmxvYWQ6LjJmfSBNYml0L3MnKQogICAgcHJpbnQoJ1VwbG9hZDonLCBmJ3t1cGxvYWQ6LjJmfSBNYml0L3MnKQp0ZXN0X3NwZWVkKCk=" | base64 -d > /tmp/speedtest_cf.py && python3 /tmp/speedtest_cf.py'
    )
    
    ssh_cmd = [
        "ssh",
        "-i", "/run/secrets/master_ssh_key",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=10",
        f"root@{address}",
        cmd
    ]
    
    try:
        proc = await asyncio.create_subprocess_exec(
            *ssh_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=35)
        stdout = stdout_bytes.decode("utf-8", errors="ignore").strip()
        stderr = stderr_bytes.decode("utf-8", errors="ignore").strip()
        
        if proc.returncode != 0:
            err = stderr or "SSH connection error / Auth failed"
            return False, "—", "—", "—", err
            
        ping_val = "—"
        download_val = "—"
        upload_val = "—"
        for line in stdout.splitlines():
            line = line.strip()
            if line.startswith("Ping:"):
                ping_val = line.split(":", 1)[1].strip()
            elif line.startswith("Download:"):
                download_val = line.split(":", 1)[1].strip()
            elif line.startswith("Upload:"):
                upload_val = line.split(":", 1)[1].strip()
                
        return True, download_val, upload_val, ping_val, ""
    except asyncio.TimeoutError:
        return False, "—", "—", "—", "Превышено время ожидания (timeout)"
    except Exception as e:
        return False, "—", "—", "—", str(e)


@dp.callback_query(F.data.startswith("nodes:speedtest:"))
async def cb_node_speedtest(callback: CallbackQuery):
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Доступ только для администраторов.", show_alert=True)
        return

    uuid = callback.data.split(":", 2)[2]
    
    # 1. Fetch node info to get name & address
    payload = await api.get_node(uuid)
    node = (payload or {}).get("response") if isinstance(payload, dict) else None
    if not node:
        await callback.answer("Нода не найдена.", show_alert=True)
        return
        
    node_name = node.get("name") or "Без названия"
    node_address = node.get("address") or ""
    
    if not node_address:
        await callback.answer("У ноды нет IP-адреса.", show_alert=True)
        return

    await callback.answer("Запуск замера скорости...")
    
    back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ К ноде", callback_data=f"nodes:card:{uuid}")]
    ])
    await safe_edit(
        callback,
        f"⏳ <b>{html.escape(node_name)}</b> (<code>{node_address}</code>)\n\n"
        f"<i>Выполняется замер скорости... Это может занять около 15 секунд.</i>",
        parse_mode="HTML",
        reply_markup=back_kb,
        prefer_edit=True
    )

    try:
        ok, dl, ul, png, err = await _execute_node_speedtest(node)
        
        if not ok:
            logger.error("Speedtest failed for node %s: %s", node_name, err)
            await safe_edit(
                callback,
                f"❌ <b>Замер скорости на ноде {html.escape(node_name)} провалился.</b>\n\n"
                f"<code>{html.escape(err)}</code>",
                parse_mode="HTML",
                reply_markup=back_kb,
                prefer_edit=True
            )
            return

        updated_kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🔁 Повторить замер", callback_data=f"nodes:speedtest:{uuid}"),
                InlineKeyboardButton(text="◀️ К ноде", callback_data=f"nodes:card:{uuid}"),
            ]
        ])
        
        result_text = (
            f"⚡ <b>Результаты Speedtest для {html.escape(node_name)}</b>\n"
            f"<code>{node_address}</code>\n\n"
            f"• <b>Ping:</b> {html.escape(png)}\n"
            f"• <b>Download:</b> {html.escape(dl)}\n"
            f"• <b>Upload:</b> {html.escape(ul)}"
        )
        
        await safe_edit(
            callback,
            result_text,
            parse_mode="HTML",
            reply_markup=updated_kb,
            prefer_edit=True
        )

    except Exception as e:
        logger.exception("Unexpected error in cb_node_speedtest")
        await safe_edit(
            callback,
            f"❌ <b>Произошла ошибка при выполнении замера скорости:</b>\n"
            f"<code>{html.escape(str(e))}</code>",
            parse_mode="HTML",
            reply_markup=back_kb,
            prefer_edit=True
        )


@dp.callback_query(F.data == "nodes:speedtest_all")
async def cb_nodes_speedtest_all(callback: CallbackQuery):
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Доступ только для администраторов.", show_alert=True)
        return

    nodes = await api.list_nodes()
    if nodes is None:
        nodes = []

    active_nodes = [n for n in nodes if not n.get("isDisabled")]
    if not active_nodes:
        await callback.answer("Нет активных нод для замера скорости.", show_alert=True)
        return

    await callback.answer("Запускаю общий замер скорости...")
    
    back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад к списку нод", callback_data="admin_nodes")]
    ])
    
    await safe_edit(
        callback,
        "⏳ <b>Запуск замера скорости на всех активных нодах одновременно...</b>\n\n"
        f"Активных серверов: {len(active_nodes)}\n"
        "<i>Это может занять до 30 секунд.</i>",
        parse_mode="HTML",
        reply_markup=back_kb,
        prefer_edit=True
    )

    try:
        tasks = [_execute_node_speedtest(n) for n in active_nodes]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        lines = ["⚡ <b>Результаты Speedtest всех нод:</b>\n"]
        
        for n, res in zip(active_nodes, results):
            name = n.get("name") or "Без названия"
            if isinstance(res, Exception):
                lines.append(f"🔴 <b>{html.escape(name)}</b>: ошибка <code>{html.escape(str(res)[:50])}</code>")
            else:
                ok, dl, ul, png, err = res
                if ok:
                    lines.append(f"🟢 <b>{html.escape(name)}</b>:\n   ⬇️ {html.escape(dl)} | ⬆️ {html.escape(ul)} (ping: {html.escape(png)})")
                else:
                    lines.append(f"🔴 <b>{html.escape(name)}</b>: <code>{html.escape(err[:50])}</code>")
                    
        updated_kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🔁 Повторить общий замер", callback_data="nodes:speedtest_all"),
                InlineKeyboardButton(text="◀️ Назад к списку нод", callback_data="admin_nodes"),
            ]
        ])
        
        await safe_edit(
            callback,
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=updated_kb,
            prefer_edit=True
        )
    except Exception as e:
        logger.exception("Unexpected error in cb_nodes_speedtest_all")
        await safe_edit(
            callback,
            f"❌ <b>Произошла ошибка при общем замере скорости:</b>\n"
            f"<code>{html.escape(str(e))}</code>",
            parse_mode="HTML",
            reply_markup=back_kb,
            prefer_edit=True
        )


@dp.callback_query(F.data.startswith("nodes:metrics:"))
async def cb_nodes_metrics(callback: CallbackQuery) -> None:
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Только для админов.", show_alert=True)
        return
    uuid = callback.data.split(":", 2)[2]
    node = await api.get_node(uuid)
    if not node:
        await callback.answer("Нода не найдена.", show_alert=True)
        return

    node_data = node.get("response") if isinstance(node, dict) and "response" in node else node
    name = html.escape(str(node_data.get("name") or "—"))
    flag = html.escape(str(node_data.get("countryCode") or ""))

    metrics_list = await api.get_system_nodes_metrics()
    target_metric = None
    if metrics_list:
        for m in metrics_list:
            if m.get("nodeUuid") == uuid:
                target_metric = m
                break

    lines = [f"🖥 <b>Метрики ноды {name} ({flag}):</b>\n"]
    if target_metric:
        users_online = target_metric.get("usersOnline", 0)
        lines.append(f"👥 <b>Пользователей онлайн:</b> <code>{users_online}</code>")
        lines.append(f"🏢 <b>Провайдер:</b> <code>{html.escape(str(target_metric.get('providerName') or '—'))}</code>\n")
        
        inbounds = target_metric.get("inboundsStats") or []
        if inbounds:
            lines.append("📥 <b>Трафик инбаундов:</b>")
            for ib in inbounds:
                tag = html.escape(str(ib.get("tag") or "—"))
                up = html.escape(str(ib.get("upload") or "0"))
                dl = html.escape(str(ib.get("download") or "0"))
                lines.append(f"  · <code>{tag}</code>: ↑ {up} | ↓ {dl}")

        outbounds = target_metric.get("outboundsStats") or []
        if outbounds:
            lines.append("\n📤 <b>Трафик аутбаундов:</b>")
            for ob in outbounds:
                tag = html.escape(str(ob.get("tag") or "—"))
                up = html.escape(str(ob.get("upload") or "0"))
                dl = html.escape(str(ob.get("download") or "0"))
                lines.append(f"  · <code>{tag}</code>: ↑ {up} | ↓ {dl}")
    else:
        lines.append("<i>Метрики в реальном времени пока недоступны для этой ноды.</i>")

    await safe_edit(
        callback,
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb.node_metrics_keyboard(uuid),
        prefer_edit=True,
    )


@dp.callback_query(F.data.startswith("nodes:conn:"))
async def cb_nodes_conn(callback: CallbackQuery) -> None:
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Только для админов.", show_alert=True)
        return
    uuid = callback.data.split(":", 2)[2]
    node = await api.get_node(uuid)
    if not node:
        await callback.answer("Нода не найдена.", show_alert=True)
        return

    node_data = node.get("response") if isinstance(node, dict) and "response" in node else node
    name = html.escape(str(node_data.get("name") or "—"))

    await callback.answer("Сканирую активные подключения...")
    res = await api.get_node_connections(uuid)

    lines = [f"👥 <b>Активные подключения на ноде {name}:</b>\n"]
    if res and isinstance(res, dict):
        users = res.get("users") or res.get("connections") or []
        if users:
            for idx, u in enumerate(users[:25], 1):
                uname = html.escape(str(u.get("username") or u.get("userId") or "—"))
                ip = html.escape(str(u.get("ip") or u.get("clientIp") or "—"))
                conn_count = u.get("connectionsCount") or u.get("count") or 1
                lines.append(f"<b>{idx}.</b> <code>{uname}</code> — <code>{ip}</code> ({conn_count} conn)")
        else:
            lines.append("<i>На данный момент активных подключений не зафиксировано.</i>")
    else:
        lines.append("<i>На данный момент активных подключений не зафиксировано.</i>")

    await safe_edit(
        callback,
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb.node_sessions_keyboard(uuid),
        prefer_edit=True,
    )


@dp.callback_query(F.data.startswith("nodes:geocheck:"))
async def cb_nodes_geocheck(callback: CallbackQuery) -> None:
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Только для админов.", show_alert=True)
        return
    uuid = callback.data.split(":", 2)[2]
    node = await api.get_node(uuid)
    if not node:
        await callback.answer("Нода не найдена.", show_alert=True)
        return
    node_data = node.get("response") if isinstance(node, dict) and "response" in node else node
    name = html.escape(str(node_data.get("name") or "—"))

    await callback.answer("Запускаю гео-проверку ноды...")
    res = await api.node_geocheck(uuid)
    lines = [f"🌍 <b>Гео-проверка ноды {name}:</b>\n"]
    if res and isinstance(res, dict):
        ip = html.escape(str(res.get("ip") or res.get("query") or "—"))
        country = html.escape(str(res.get("country") or res.get("countryCode") or "—"))
        city = html.escape(str(res.get("city") or "—"))
        isp = html.escape(str(res.get("isp") or res.get("org") or "—"))
        as_name = html.escape(str(res.get("as") or "—"))
        lines.append(f"• <b>IP:</b> <code>{ip}</code>")
        lines.append(f"• <b>Локация:</b> <b>{country}</b>, {city}")
        lines.append(f"• <b>Провайдер / AS:</b> {isp} ({as_name})")
    else:
        lines.append("<i>Гео-проверка завершилась без ответа или сервис гео-проверки недоступен.</i>")

    await safe_edit(
        callback,
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb.node_geocheck_keyboard(uuid),
        prefer_edit=True,
    )


@dp.callback_query(F.data.startswith("nodes:bw_users:"))
async def cb_nodes_bw_users(callback: CallbackQuery) -> None:
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Только для админов.", show_alert=True)
        return
    from datetime import datetime, timedelta, timezone
    uuid = callback.data.split(":", 2)[2]
    node = await api.get_node(uuid)
    if not node:
        await callback.answer("Нода не найдена.", show_alert=True)
        return
    node_data = node.get("response") if isinstance(node, dict) and "response" in node else node
    name = html.escape(str(node_data.get("name") or "—"))

    end_dt = datetime.now(timezone.utc).date()
    start_dt = end_dt - timedelta(days=7)
    start_d = start_dt.strftime("%Y-%m-%d")
    end_d = end_dt.strftime("%Y-%m-%d")

    await callback.answer("Загружаю статистику...")
    bw_data = await api.get_node_bandwidth_users(uuid, start_d, end_d, top_limit=10)
    lines = [f"👥 <b>Топ пользователей на ноде {name}</b> (за 7 дней):\n"]
    if bw_data and isinstance(bw_data, dict):
        top_users = bw_data.get("topUsers") or []
        if top_users:
            for idx, u in enumerate(top_users, 1):
                uname = html.escape(str(u.get("username") or u.get("userId") or "—"))
                tot = int(u.get("total") or 0)
                lines.append(f"<b>{idx}.</b> <code>{uname}</code> — <b>{html.escape(human_bytes(tot))}</b>")
        else:
            lines.append("<i>Данных по использованию за этот период нет.</i>")
    else:
        lines.append("<i>Данные по пользователям ноды недоступны.</i>")

    await safe_edit(
        callback,
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb.node_bw_users_keyboard(uuid),
        prefer_edit=True,
    )






