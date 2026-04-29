"""Админ: добавление новой ноды через ansible на master-ноде.

Flow:
1. Админ жмёт «➕ Добавить ноду» на экране списка нод.
2. FSM спрашивает: name, address, ssh-port, node-port, bridge-SNI, country code.
3. Бот SSH-ит на master-ноду (Болгарию), запускает там `scripts/add_node.sh`,
   который добавляет хост в `inventory.ini` и запускает `ansible-playbook deploy.yml`.
4. Лог стримится в Telegram (буферизуется и редактируется одно сообщение).

Конфигурируется через переменные окружения `MASTER_SSH_HOST`/`MASTER_SSH_KEY_PATH`
и т.д. — см. `config.py`. Если они не заданы, кнопка «➕ Добавить ноду» не показывается.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
from typing import Optional

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import auth
import config
from app import dp, safe_edit
from services.master_ssh import (
    MasterSSHConfig,
    MasterSSHError,
    build_add_node_command,
    run_command_streaming,
)

logger = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,30}[a-z0-9]$")
SNI_RE = re.compile(r"^[a-z0-9.-]{3,253}$")
ADDRESS_RE = re.compile(r"^[a-zA-Z0-9.\-]{3,253}$")
COUNTRY_RE = re.compile(r"^[A-Z]{2}$")


class AddNodeStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_address = State()
    waiting_for_ssh_port = State()
    waiting_for_node_port = State()
    waiting_for_sni = State()
    waiting_for_country = State()
    waiting_for_confirm = State()


def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✖️ Отменить", callback_data="addnode:cancel")]]
    )


def _confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Запустить ansible", callback_data="addnode:run")],
        [InlineKeyboardButton(text="✖️ Отменить", callback_data="addnode:cancel")],
    ])


# ---------- entry: button on nodes-list ----------

@dp.callback_query(F.data == "addnode:start")
async def cb_addnode_start(callback: CallbackQuery, state: FSMContext):
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Доступ только для администраторов.", show_alert=True)
        return
    if not config.master_ssh_configured():
        await callback.answer(
            "MASTER_SSH_HOST/KEY_PATH не заданы — добавление невозможно.",
            show_alert=True,
        )
        return
    await state.clear()
    await state.set_state(AddNodeStates.waiting_for_name)
    await safe_edit(
        callback,
        "➕ <b>Добавление ноды</b> (1/6)\n\n"
        "Введи короткое <b>имя ноды</b> для inventory (a-z, 0-9, '-', '_').\n"
        "Например: <code>eu_node_3</code>",
        parse_mode="HTML",
        reply_markup=_cancel_keyboard(),
        prefer_edit=True,
    )
    await callback.answer()


@dp.callback_query(F.data == "addnode:cancel")
async def cb_addnode_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(
        callback,
        "❎ Добавление ноды отменено.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🌐 К списку нод", callback_data="admin_nodes")],
        ]),
        prefer_edit=True,
    )
    await callback.answer()


# ---------- FSM steps ----------

@dp.message(AddNodeStates.waiting_for_name)
async def step_name(message: Message, state: FSMContext):
    if not await auth.is_admin(message.from_user.id):
        return
    name = (message.text or "").strip().lower()
    if not NAME_RE.match(name):
        await message.answer(
            "Имя должно быть 3-32 символов: a-z 0-9 '-' '_'. Попробуй ещё.",
            reply_markup=_cancel_keyboard(),
        )
        return
    await state.update_data(name=name)
    await state.set_state(AddNodeStates.waiting_for_address)
    await message.answer(
        "<b>2/6</b> · Введи <b>адрес</b> новой ноды — IP или домен.\n"
        "Например: <code>caffeinated.example.com</code> или <code>1.2.3.4</code>",
        parse_mode="HTML",
        reply_markup=_cancel_keyboard(),
    )


@dp.message(AddNodeStates.waiting_for_address)
async def step_address(message: Message, state: FSMContext):
    if not await auth.is_admin(message.from_user.id):
        return
    addr = (message.text or "").strip()
    if not ADDRESS_RE.match(addr):
        await message.answer(
            "Адрес выглядит некорректно. Введи IP или FQDN.",
            reply_markup=_cancel_keyboard(),
        )
        return
    await state.update_data(address=addr)
    await state.set_state(AddNodeStates.waiting_for_ssh_port)
    await message.answer(
        "<b>3/6</b> · SSH-порт новой ноды (по умолчанию 22).\n"
        "Введи число или просто <code>22</code>.",
        parse_mode="HTML",
        reply_markup=_cancel_keyboard(),
    )


def _parse_port(text: str, default: Optional[int] = None) -> Optional[int]:
    text = (text or "").strip()
    if not text and default is not None:
        return default
    try:
        p = int(text)
        if 1 <= p <= 65535:
            return p
    except ValueError:
        pass
    return None


@dp.message(AddNodeStates.waiting_for_ssh_port)
async def step_ssh_port(message: Message, state: FSMContext):
    if not await auth.is_admin(message.from_user.id):
        return
    port = _parse_port(message.text or "", default=22)
    if port is None:
        await message.answer(
            "Порт должен быть числом 1..65535.", reply_markup=_cancel_keyboard()
        )
        return
    await state.update_data(ssh_port=port)
    await state.set_state(AddNodeStates.waiting_for_node_port)
    await message.answer(
        "<b>4/6</b> · Порт <b>Remnawave node</b> внутри ноды (например <code>3743</code>).",
        parse_mode="HTML",
        reply_markup=_cancel_keyboard(),
    )


@dp.message(AddNodeStates.waiting_for_node_port)
async def step_node_port(message: Message, state: FSMContext):
    if not await auth.is_admin(message.from_user.id):
        return
    port = _parse_port(message.text or "")
    if port is None:
        await message.answer(
            "Порт должен быть числом 1..65535.", reply_markup=_cancel_keyboard()
        )
        return
    await state.update_data(node_port=port)
    await state.set_state(AddNodeStates.waiting_for_sni)
    await message.answer(
        "<b>5/6</b> · <b>SNI</b> для bridge (домен под который маскируемся).\n"
        "Например: <code>www.microsoft.com</code>",
        parse_mode="HTML",
        reply_markup=_cancel_keyboard(),
    )


@dp.message(AddNodeStates.waiting_for_sni)
async def step_sni(message: Message, state: FSMContext):
    if not await auth.is_admin(message.from_user.id):
        return
    sni = (message.text or "").strip().lower()
    if not SNI_RE.match(sni):
        await message.answer(
            "SNI выглядит некорректно. Это должен быть домен.",
            reply_markup=_cancel_keyboard(),
        )
        return
    await state.update_data(bridge_sni=sni)
    await state.set_state(AddNodeStates.waiting_for_country)
    await message.answer(
        "<b>6/6</b> · ISO-код страны (2 буквы, верхний регистр).\n"
        "Например: <code>BG</code>, <code>NL</code>, <code>DE</code>",
        parse_mode="HTML",
        reply_markup=_cancel_keyboard(),
    )


@dp.message(AddNodeStates.waiting_for_country)
async def step_country(message: Message, state: FSMContext):
    if not await auth.is_admin(message.from_user.id):
        return
    cc = (message.text or "").strip().upper()
    if not COUNTRY_RE.match(cc):
        await message.answer(
            "Код страны — ровно 2 латинские буквы (например <code>BG</code>).",
            parse_mode="HTML",
            reply_markup=_cancel_keyboard(),
        )
        return
    await state.update_data(country_code=cc)
    data = await state.get_data()
    await state.set_state(AddNodeStates.waiting_for_confirm)
    summary = (
        "🔍 <b>Проверь параметры</b>\n\n"
        f"Имя: <code>{html.escape(data['name'])}</code>\n"
        f"Адрес: <code>{html.escape(data['address'])}</code>\n"
        f"SSH-порт: <code>{data['ssh_port']}</code>\n"
        f"Node-порт: <code>{data['node_port']}</code>\n"
        f"SNI: <code>{html.escape(data['bridge_sni'])}</code>\n"
        f"Страна: <code>{html.escape(data['country_code'])}</code>\n\n"
        "Бот зайдёт на master-ноду и запустит <code>scripts/add_node.sh</code>:\n"
        "  1. Допишет хост в <code>inventory.ini</code>\n"
        "  2. Положит SSH-ключ master на новую ноду\n"
        "  3. Запустит <code>ansible-playbook deploy.yml -l NAME</code>\n\n"
        "Лог будет редактироваться сюда же. Запускаем?"
    )
    await message.answer(summary, parse_mode="HTML", reply_markup=_confirm_keyboard())


# ---------- run ansible ----------

@dp.callback_query(F.data == "addnode:run", AddNodeStates.waiting_for_confirm)
async def cb_addnode_run(callback: CallbackQuery, state: FSMContext):
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Доступ только для администраторов.", show_alert=True)
        return
    data = await state.get_data()
    await state.clear()
    await callback.answer("Запускаю…")
    try:
        cfg = MasterSSHConfig.from_env()
    except MasterSSHError as e:
        await safe_edit(
            callback,
            f"❌ {html.escape(str(e))}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🌐 К списку нод", callback_data="admin_nodes")],
            ]),
            prefer_edit=True,
        )
        return
    cmd = build_add_node_command(
        cfg,
        name=data["name"],
        address=data["address"],
        ssh_port=int(data["ssh_port"]),
        node_port=int(data["node_port"]),
        bridge_sni=data["bridge_sni"],
        country_code=data["country_code"],
    )
    await _stream_to_message(callback, cmd, cfg, name=data["name"])


async def _stream_to_message(
    callback: CallbackQuery,
    cmd: str,
    cfg: MasterSSHConfig,
    name: str,
) -> None:
    """Стримим вывод ssh-команды редактируя одно сообщение, не чаще раза в 1.5с."""
    header = f"⏳ <b>Деплой ноды {html.escape(name)}</b>\n"
    buffer: list[str] = []
    last_edit = 0.0
    last_text = ""

    def _render(custom_header: Optional[str] = None) -> str:
        """Telegram 4096 limit; режем по escaped длине, а не по сырой."""
        h = custom_header if custom_header is not None else header
        body = "\n".join(buffer)
        escaped = html.escape(body)
        max_body = 4096 - len(h) - len("<pre></pre>") - 64  # запас
        if len(escaped) > max_body:
            escaped = "…\n" + escaped[-max_body:]
        return f"{h}<pre>{escaped}</pre>"

    async def _maybe_edit(force: bool = False) -> None:
        nonlocal last_edit, last_text
        now = asyncio.get_event_loop().time()
        if not force and now - last_edit < 1.5:
            return
        text = _render()
        if text == last_text:
            return
        try:
            await safe_edit(callback, text, parse_mode="HTML", reply_markup=None, prefer_edit=True)
            last_edit = now
            last_text = text
        except Exception as e:  # noqa: BLE001
            logger.debug("safe_edit во время стрима: %s", e)

    await safe_edit(callback, _render(), parse_mode="HTML", reply_markup=None, prefer_edit=True)
    ok = True
    try:
        async for line in run_command_streaming(cfg, cmd, timeout=1800.0):
            buffer.append(line)
            if len(buffer) > 600:
                buffer.pop(0)
            await _maybe_edit()
    except MasterSSHError as e:
        buffer.append(f"[ERROR] {e}")
        ok = False
    except Exception as e:  # noqa: BLE001
        logger.exception("Ошибка во время стрима ansible-логов")
        buffer.append(f"[ERROR] {e}")
        ok = False

    final_header = (
        f"✅ <b>Нода {html.escape(name)} раскатана</b>\n"
        if ok
        else f"❌ <b>Не удалось раскатать ноду {html.escape(name)}</b>\n"
    )
    final_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 К списку нод", callback_data="admin_nodes")],
    ])
    try:
        await safe_edit(
            callback,
            _render(final_header),
            parse_mode="HTML",
            reply_markup=final_keyboard,
            prefer_edit=True,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Финальный safe_edit упал (%s) — шлю кратко.", e)
        try:
            await callback.message.answer(
                final_header + "<i>(лог обрезан / превысил лимит Telegram)</i>",
                parse_mode="HTML",
                reply_markup=final_keyboard,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось отправить даже краткий финальный статус")
