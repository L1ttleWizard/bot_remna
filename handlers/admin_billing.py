"""Админский раздел «💳 Инфра-биллинг» — провайдеры, привязки нод, история платежей.

Использует Remnawave API /api/infra-billing/*.
Документация: https://docs.rw/
"""
from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from aiogram import F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import auth
from app import api, dp, safe_edit

logger = logging.getLogger(__name__)

# ---------- FSM ----------
class BillingProviderForm(StatesGroup):
    waiting_name = State()
    waiting_billing_url = State()


class BillingDateForm(StatesGroup):
    waiting_date = State()  # ввод произвольной даты для nextBillingAt


# ---------- Хелперы ----------
def _fmt_date(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        # принимаем 2026-06-15T00:00:00.000Z и аналоги
        s = str(iso).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt.astimezone(timezone(timedelta(hours=3))).strftime("%d.%m.%Y")
    except Exception:
        return html.escape(str(iso)[:10])


def _days_until(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        s = str(iso).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        now = datetime.now(timezone.utc)
        return (dt - now).days
    except Exception:
        return None


async def _find_billing_for_node(node_uuid: str) -> dict | None:
    """Вернёт запись biller-node для конкретной ноды или None."""
    items = await api.list_billing_nodes() or []
    for b in items:
        if (b.get("nodeUuid") or b.get("node", {}).get("uuid")) == node_uuid:
            return b
    return None


# ---------- Главное меню биллинга ----------
def _billing_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏷 Провайдеры", callback_data="billing:provs")],
        [InlineKeyboardButton(text="📀 Привязки нод", callback_data="billing:nodes")],
        [InlineKeyboardButton(text="🧾 История платежей", callback_data="billing:hist:0")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="billing:menu")],
        [InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="admin_panel")],
    ])


async def _render_billing_main(callback: CallbackQuery) -> None:
    providers = await api.list_billing_providers() or []
    nodes = await api.list_billing_nodes() or []

    text_lines = [
        "💳 <b>Инфра-биллинг</b>",
        "",
        f"Провайдеров: <b>{len(providers)}</b>",
        f"Привязанных нод: <b>{len(nodes)}</b>",
    ]
    # ближайшее списание
    soon: list[tuple[int, str]] = []
    for b in nodes:
        d = _days_until(b.get("nextBillingAt"))
        if d is not None:
            name = b.get("node", {}).get("name") or b.get("nodeName") or "—"
            soon.append((d, name))
    if soon:
        soon.sort()
        d, name = soon[0]
        text_lines.append("")
        text_lines.append(f"Ближайшее списание: <b>{html.escape(name)}</b> через <b>{d} дн.</b>")

    await safe_edit(
        callback,
        "\n".join(text_lines),
        parse_mode="HTML",
        reply_markup=_billing_main_keyboard(),
        prefer_edit=True,
    )


@dp.callback_query(F.data == "billing:menu")
async def cb_billing_menu(callback: CallbackQuery) -> None:
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await _render_billing_main(callback)
    await callback.answer()


@dp.message(Command("billing"))
async def cmd_billing(message: Message) -> None:
    if not await auth.is_admin(message.from_user.id):
        return
    fake = type("F", (), {"message": message, "from_user": message.from_user, "answer": lambda *a, **k: None})()
    providers = await api.list_billing_providers() or []
    nodes = await api.list_billing_nodes() or []
    await message.answer(
        f"💳 <b>Инфра-биллинг</b>\n\nПровайдеров: <b>{len(providers)}</b>\nПривязанных нод: <b>{len(nodes)}</b>",
        parse_mode="HTML",
        reply_markup=_billing_main_keyboard(),
    )


# ---------- Провайдеры ----------
def _providers_keyboard(providers: list) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for p in providers:
        uuid = p.get("uuid") or ""
        name = (p.get("name") or "—")[:32]
        rows.append([InlineKeyboardButton(text=f"🏷 {name}", callback_data=f"billing:prov:{uuid}")])
    rows.append([InlineKeyboardButton(text="➕ Новый провайдер", callback_data="billing:prov_new")])
    rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="billing:provs"),
                 InlineKeyboardButton(text="⬅️ Назад", callback_data="billing:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "billing:provs")
async def cb_billing_providers(callback: CallbackQuery) -> None:
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    providers = await api.list_billing_providers() or []
    text = "🏷 <b>Провайдеры биллинга</b>\n\n"
    if not providers:
        text += "<i>Список пуст. Создайте первого провайдера.</i>"
    else:
        for p in providers:
            text += f"• <b>{html.escape(str(p.get('name') or '—'))}</b>"
            if p.get("billingUrl"):
                text += f"  · <a href=\"{html.escape(str(p['billingUrl']))}\">оплата</a>"
            text += "\n"
    await safe_edit(callback, text, parse_mode="HTML",
                    reply_markup=_providers_keyboard(providers), prefer_edit=True,
                    disable_web_page_preview=True)
    await callback.answer()


def _provider_card_keyboard(uuid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"billing:prov_del:{uuid}")],
        [InlineKeyboardButton(text="⬅️ К провайдерам", callback_data="billing:provs")],
    ])


@dp.callback_query(F.data.startswith("billing:prov:"))
async def cb_billing_provider_card(callback: CallbackQuery) -> None:
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    uuid = callback.data.split(":", 2)[2]
    p = await api.get_billing_provider(uuid)
    if not p:
        await callback.answer("Провайдер не найден", show_alert=True)
        return
    lines = [
        f"🏷 <b>{html.escape(str(p.get('name') or '—'))}</b>",
        f"UUID: <code>{html.escape(uuid)}</code>",
    ]
    if p.get("billingUrl"):
        lines.append(f"Ссылка для оплаты: <a href=\"{html.escape(str(p['billingUrl']))}\">открыть</a>")
    await safe_edit(callback, "\n".join(lines), parse_mode="HTML",
                    reply_markup=_provider_card_keyboard(uuid), prefer_edit=True,
                    disable_web_page_preview=True)
    await callback.answer()


@dp.callback_query(F.data.startswith("billing:prov_del:"))
async def cb_billing_provider_delete(callback: CallbackQuery) -> None:
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    uuid = callback.data.split(":", 2)[2]
    ok = await api.delete_billing_provider(uuid)
    await callback.answer("✅ Удалено" if ok else "❌ Ошибка удаления", show_alert=True)
    if ok:
        # обновим список
        callback.data = "billing:provs"
        await cb_billing_providers(callback)


# ----- Создание провайдера через FSM -----
@dp.callback_query(F.data == "billing:prov_new")
async def cb_billing_provider_new(callback: CallbackQuery, state: FSMContext) -> None:
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(BillingProviderForm.waiting_name)
    await safe_edit(
        callback,
        "➕ <b>Новый провайдер</b>\n\nВведите название провайдера (например: <code>Hetzner</code>, <code>OVH</code>):",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✖️ Отмена", callback_data="billing:provs")],
        ]),
        prefer_edit=True,
    )
    await callback.answer()


@dp.message(BillingProviderForm.waiting_name)
async def msg_billing_provider_name(message: Message, state: FSMContext) -> None:
    if not await auth.is_admin(message.from_user.id):
        return
    name = (message.text or "").strip()
    if not name or len(name) > 64:
        await message.answer("⚠️ Название должно быть от 1 до 64 символов. Попробуйте ещё раз:")
        return
    await state.update_data(name=name)
    await state.set_state(BillingProviderForm.waiting_billing_url)
    await message.answer(
        "Введите ссылку для оплаты (или отправьте <code>-</code> чтобы пропустить):",
        parse_mode="HTML",
    )


@dp.message(BillingProviderForm.waiting_billing_url)
async def msg_billing_provider_url(message: Message, state: FSMContext) -> None:
    if not await auth.is_admin(message.from_user.id):
        return
    url = (message.text or "").strip()
    data = await state.get_data()
    payload: dict[str, Any] = {"name": data["name"]}
    if url and url != "-":
        if not (url.startswith("http://") or url.startswith("https://")):
            await message.answer("⚠️ Ссылка должна начинаться с http:// или https://. Попробуйте ещё раз (или отправьте <code>-</code>):", parse_mode="HTML")
            return
        payload["billingUrl"] = url
    res = await api.create_billing_provider(payload)
    await state.clear()
    if res:
        await message.answer(f"✅ Провайдер <b>{html.escape(data['name'])}</b> создан.", parse_mode="HTML")
    else:
        await message.answer("❌ Не удалось создать провайдера. Подробности в логах.")


# ---------- Привязки нод (биллинг-ноды) ----------
def _billing_nodes_keyboard(items: list) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for b in items:
        uuid = b.get("uuid") or ""
        name = (b.get("node", {}).get("name") or b.get("nodeName") or "—")[:24]
        d = _days_until(b.get("nextBillingAt"))
        tail = f" · {d}д" if d is not None else ""
        rows.append([InlineKeyboardButton(text=f"📀 {name}{tail}", callback_data=f"billing:bn:{uuid}")])
    rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="billing:nodes"),
                 InlineKeyboardButton(text="⬅️ Назад", callback_data="billing:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "billing:nodes")
async def cb_billing_nodes_list(callback: CallbackQuery) -> None:
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    items = await api.list_billing_nodes() or []
    text = "📀 <b>Привязки нод к биллингу</b>\n\n"
    if not items:
        text += "<i>Нет привязанных нод. Откройте карточку ноды и нажмите «💳 Биллинг» чтобы привязать.</i>"
    else:
        for b in items:
            name = b.get("node", {}).get("name") or b.get("nodeName") or "—"
            prov = b.get("provider", {}).get("name") or b.get("providerName") or "—"
            text += f"• <b>{html.escape(name)}</b> → {html.escape(prov)} · {_fmt_date(b.get('nextBillingAt'))}\n"
    await safe_edit(callback, text, parse_mode="HTML",
                    reply_markup=_billing_nodes_keyboard(items), prefer_edit=True)
    await callback.answer()


def _billing_node_card_keyboard(billing_uuid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 +30 дней", callback_data=f"billing:setdate:{billing_uuid}:30"),
         InlineKeyboardButton(text="📅 +90 дней", callback_data=f"billing:setdate:{billing_uuid}:90")],
        [InlineKeyboardButton(text="📅 +1 год", callback_data=f"billing:setdate:{billing_uuid}:365"),
         InlineKeyboardButton(text="📅 Своя дата…", callback_data=f"billing:setdate_custom:{billing_uuid}")],
        [InlineKeyboardButton(text="🗑 Отвязать", callback_data=f"billing:bn_del:{billing_uuid}")],
        [InlineKeyboardButton(text="⬅️ К списку", callback_data="billing:nodes")],
    ])


@dp.callback_query(F.data.startswith("billing:bn:"))
async def cb_billing_node_card(callback: CallbackQuery) -> None:
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    billing_uuid = callback.data.split(":", 2)[2]
    items = await api.list_billing_nodes() or []
    b = next((x for x in items if x.get("uuid") == billing_uuid), None)
    if not b:
        await callback.answer("Привязка не найдена", show_alert=True)
        return
    name = b.get("node", {}).get("name") or b.get("nodeName") or "—"
    prov = b.get("provider", {}).get("name") or b.get("providerName") or "—"
    nb = b.get("nextBillingAt")
    d = _days_until(nb)
    lines = [
        f"📀 <b>{html.escape(name)}</b>",
        f"Провайдер: <b>{html.escape(prov)}</b>",
        f"Следующее списание: <b>{_fmt_date(nb)}</b>" + (f" (через {d} дн.)" if d is not None else ""),
    ]
    await safe_edit(callback, "\n".join(lines), parse_mode="HTML",
                    reply_markup=_billing_node_card_keyboard(billing_uuid), prefer_edit=True)
    await callback.answer()


@dp.callback_query(F.data.startswith("billing:setdate:"))
async def cb_billing_setdate(callback: CallbackQuery) -> None:
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    _, _, billing_uuid, days_str = callback.data.split(":", 3)
    try:
        days = int(days_str)
    except ValueError:
        await callback.answer("Неверный параметр", show_alert=True)
        return
    new_dt = datetime.now(timezone.utc) + timedelta(days=days)
    res = await api.update_billing_nodes({
        "uuids": [billing_uuid],
        "nextBillingAt": new_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    })
    await callback.answer("✅ Дата обновлена" if res else "❌ Ошибка обновления", show_alert=True)
    callback.data = f"billing:bn:{billing_uuid}"
    await cb_billing_node_card(callback)


@dp.callback_query(F.data.startswith("billing:setdate_custom:"))
async def cb_billing_setdate_custom(callback: CallbackQuery, state: FSMContext) -> None:
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    billing_uuid = callback.data.split(":", 2)[2]
    await state.set_state(BillingDateForm.waiting_date)
    await state.update_data(billing_uuid=billing_uuid)
    await safe_edit(
        callback,
        "📅 Введите дату следующего списания в формате <code>ДД.ММ.ГГГГ</code> (например <code>15.07.2026</code>):",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✖️ Отмена", callback_data=f"billing:bn:{billing_uuid}")],
        ]),
        prefer_edit=True,
    )
    await callback.answer()


@dp.message(BillingDateForm.waiting_date)
async def msg_billing_custom_date(message: Message, state: FSMContext) -> None:
    if not await auth.is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    try:
        dt = datetime.strptime(raw, "%d.%m.%Y").replace(tzinfo=timezone.utc)
    except ValueError:
        await message.answer("⚠️ Формат ДД.ММ.ГГГГ. Попробуйте ещё раз:")
        return
    data = await state.get_data()
    billing_uuid = data.get("billing_uuid")
    await state.clear()
    res = await api.update_billing_nodes({
        "uuids": [billing_uuid],
        "nextBillingAt": dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    })
    if res:
        await message.answer(f"✅ Дата обновлена на <b>{_fmt_date(dt.isoformat())}</b>", parse_mode="HTML")
    else:
        await message.answer("❌ Не удалось обновить дату.")


@dp.callback_query(F.data.startswith("billing:bn_del:"))
async def cb_billing_node_delete(callback: CallbackQuery) -> None:
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    billing_uuid = callback.data.split(":", 2)[2]
    ok = await api.delete_billing_node(billing_uuid)
    await callback.answer("✅ Отвязано" if ok else "❌ Ошибка", show_alert=True)
    if ok:
        callback.data = "billing:nodes"
        await cb_billing_nodes_list(callback)


# ---------- Привязка из карточки ноды ----------
@dp.callback_query(F.data.startswith("nodes:billing:"))
async def cb_node_billing_open(callback: CallbackQuery) -> None:
    """Открывается из карточки ноды (handlers/admin_nodes.py)."""
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    node_uuid = callback.data.split(":", 2)[2]
    existing = await _find_billing_for_node(node_uuid)

    if existing:
        # уже привязана — открыть карточку привязки
        callback.data = f"billing:bn:{existing.get('uuid')}"
        await cb_billing_node_card(callback)
        return

    # не привязана — показать выбор провайдера
    providers = await api.list_billing_providers() or []
    if not providers:
        await safe_edit(
            callback,
            "💳 <b>Биллинг ноды</b>\n\n<i>Сначала создайте хотя бы одного провайдера в разделе «Инфра-биллинг».</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏷 К провайдерам", callback_data="billing:provs")],
                [InlineKeyboardButton(text="⬅️ К ноде", callback_data=f"nodes:card:{node_uuid}")],
            ]),
            prefer_edit=True,
        )
        await callback.answer()
        return

    rows = [
        [InlineKeyboardButton(text=f"🏷 {(p.get('name') or '—')[:32]}",
                              callback_data=f"billing:attach:{node_uuid}:{p.get('uuid')}")]
        for p in providers
    ]
    rows.append([InlineKeyboardButton(text="⬅️ К ноде", callback_data=f"nodes:card:{node_uuid}")])
    await safe_edit(
        callback,
        "💳 <b>Привязка ноды к провайдеру</b>\n\nВыберите провайдера:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        prefer_edit=True,
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("billing:attach:"))
async def cb_billing_attach(callback: CallbackQuery) -> None:
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    _, _, node_uuid, provider_uuid = callback.data.split(":", 3)
    # привязываем с дефолтной датой +30 дней
    new_dt = datetime.now(timezone.utc) + timedelta(days=30)
    res = await api.create_billing_node({
        "providerUuid": provider_uuid,
        "nodeUuid": node_uuid,
        "nextBillingAt": new_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    })
    if res:
        await callback.answer("✅ Привязано (списание через 30 дн.)", show_alert=True)
        callback.data = f"nodes:card:{node_uuid}"
        from handlers.admin_nodes import cb_node_card  # noqa: WPS433
        await cb_node_card(callback)
    else:
        await callback.answer("❌ Ошибка привязки", show_alert=True)


# ---------- История платежей ----------
PAGE = 10


@dp.callback_query(F.data.startswith("billing:hist:"))
async def cb_billing_history(callback: CallbackQuery) -> None:
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    try:
        offset = int(callback.data.split(":", 2)[2])
    except ValueError:
        offset = 0
    items = await api.list_billing_history({"start": offset, "limit": PAGE}) or []
    text = "🧾 <b>История платежей</b>\n\n"
    if not items:
        text += "<i>Записей нет.</i>"
    else:
        for r in items:
            node_name = r.get("node", {}).get("name") or r.get("nodeName") or "—"
            amount = r.get("amount") or "—"
            currency = r.get("currency") or ""
            paid_at = _fmt_date(r.get("billedAt") or r.get("paidAt") or r.get("createdAt"))
            text += f"• <b>{html.escape(str(node_name))}</b> · {amount} {html.escape(str(currency))} · {paid_at}\n"

    rows = []
    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"billing:hist:{max(0, offset - PAGE)}"))
    if len(items) == PAGE:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"billing:hist:{offset + PAGE}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="billing:menu")])
    await safe_edit(callback, text, parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), prefer_edit=True)
    await callback.answer()