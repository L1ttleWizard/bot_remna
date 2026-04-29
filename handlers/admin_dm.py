"""Админская команда /dm — отправить сообщение пользователю.

Поддерживает два варианта вызова:
- `/dm <tg_id|@username> <текст>` — однократно
- FSM `AdminDmStates.waiting_for_text` — после клика по «✉️ Написать» в карточке
"""
import html
from typing import Optional

from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

import auth
import database as db
from app import AdminDmStates, bot, dp
from formatters import format_tg_name


@dp.message(Command("dm"))
async def cmd_dm(message: Message, command: CommandObject):
    if not await auth.is_admin(message.from_user.id):
        return
    args = (command.args or "").strip()
    if not args:
        await message.answer(
            "Использование: <code>/dm &lt;tg_id|@username&gt; текст</code>",
            parse_mode="HTML",
        )
        return
    parts = args.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Нужно указать получателя и текст.")
        return
    target_raw, text = parts
    target_tg: Optional[int] = None
    if target_raw.isdigit():
        candidate = int(target_raw)
        existing = await db.get_user_full(candidate)
        if existing:
            target_tg = candidate
    else:
        uname = target_raw[1:] if target_raw.startswith("@") else target_raw
        row = await db.find_user_by_tg_username(uname)
        if row:
            target_tg = int(row[0])
    if target_tg is None:
        await message.answer(
            "Получатель не найден в БД. Можно писать только тем, кто хотя бы раз "
            "взаимодействовал с ботом (`/start` или активация токена)."
        )
        return
    sender_name = format_tg_name(
        message.from_user.username,
        message.from_user.first_name,
        message.from_user.last_name,
    )
    body = (
        f"📩 <b>Сообщение от администратора</b> ({html.escape(sender_name)}):\n\n"
        f"{html.escape(text)}"
    )
    try:
        await bot.send_message(target_tg, body, parse_mode="HTML")
        await message.answer(f"✅ Доставлено пользователю <code>{target_tg}</code>.", parse_mode="HTML")
    except Exception as exc:
        await message.answer(f"❌ Не удалось доставить: <code>{html.escape(str(exc))}</code>", parse_mode="HTML")


@dp.message(AdminDmStates.waiting_for_text)
async def admin_dm_capture(message: Message, state: FSMContext):
    if not await auth.is_admin(message.from_user.id):
        await state.clear()
        return
    text = (message.text or "").strip()
    if text == "/cancel":
        await state.clear()
        await message.answer("Отменено.")
        return
    if not text:
        await message.answer("Текст не может быть пустым. /cancel чтобы выйти.")
        return
    data = await state.get_data()
    target_tg = data.get("target_tg")
    await state.clear()
    if not target_tg:
        await message.answer("Состояние потеряно — повторите из карточки пользователя.")
        return
    sender_name = format_tg_name(
        message.from_user.username,
        message.from_user.first_name,
        message.from_user.last_name,
    )
    body = (
        f"📩 <b>Сообщение от администратора</b> ({html.escape(sender_name)}):\n\n"
        f"{html.escape(text)}"
    )
    try:
        await bot.send_message(int(target_tg), body, parse_mode="HTML")
        await message.answer(f"✅ Доставлено пользователю <code>{target_tg}</code>.", parse_mode="HTML")
    except Exception as exc:
        await message.answer(
            f"❌ Не удалось доставить: <code>{html.escape(str(exc))}</code>",
            parse_mode="HTML",
        )
