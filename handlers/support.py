"""Поддержка — отображение и редактирование контактов поддержки."""
from aiogram import F
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import auth
import database as db
from app import SUPPORT_KEY, dp, safe_edit
from keyboards import back_only_keyboard


@dp.callback_query(F.data == "support")
async def cb_support(callback: CallbackQuery):
    if not (await auth.is_authorized(callback.from_user.id) or await auth.is_admin(callback.from_user.id)):
        await callback.answer("Доступ только по приглашению.", show_alert=True)
        return
    text = await db.get_setting(SUPPORT_KEY)
    body = (
        f"❓ <b>Поддержка</b>\n\n{text}"
        if text
        else "❓ <b>Поддержка</b>\n\nКонтакты поддержки пока не настроены."
    )
    await safe_edit(callback, body, parse_mode="HTML", reply_markup=back_only_keyboard(), prefer_edit=True)
    await callback.answer()


@dp.callback_query(F.data == "admin_support")
async def cb_admin_support(callback: CallbackQuery):
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    cur = await db.get_setting(SUPPORT_KEY)
    body = (
        "❓ <b>Контакты поддержки</b>\n\n"
        f"Текущий текст:\n{cur if cur else '<i>не задан</i>'}\n\n"
        "Изменить: <code>/set_support &lt;HTML-текст&gt;</code>\n"
        "Очистить: <code>/set_support</code> без аргументов."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛠 В админ-панель", callback_data="admin_panel")],
        ]
    )
    await safe_edit(callback, body, parse_mode="HTML", reply_markup=kb, prefer_edit=True)
    await callback.answer()


@dp.message(Command("set_support"))
async def cmd_set_support(message: Message, command: CommandObject):
    if not await auth.is_admin(message.from_user.id):
        return
    text = (command.args or "").strip()
    if not text:
        await db.set_setting(SUPPORT_KEY, None)
        await message.answer("Контакты поддержки очищены.")
        return
    await db.set_setting(SUPPORT_KEY, text)
    await message.answer(
        "✅ Контакты поддержки сохранены. Пользователи увидят кнопку «❓ Поддержка» в меню."
    )
