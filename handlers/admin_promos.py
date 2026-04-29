"""Админская часть промокодов: создание, отзыв, список, обзорная страница."""
import html
from typing import Optional

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
from app import dp, safe_edit


@dp.callback_query(F.data == "admin_promos")
async def cb_admin_promos(callback: CallbackQuery):
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    rows = await db.list_promocodes(limit=20)
    if not rows:
        text = "🎁 <b>Промокоды</b>\n\nПока ни одного. Создайте первый: <code>/issue_promo CODE 30 [max_uses]</code>"
    else:
        lines = ["🎁 <b>Промокоды</b> (последние 20)\n"]
        for code, bonus, max_uses, used, revoked, _created in rows:
            mu = "∞" if max_uses is None else str(max_uses)
            status = "🚫" if revoked else "✅"
            lines.append(
                f"{status} <code>{html.escape(code)}</code> — +{bonus} дн., {used}/{mu}"
            )
        text = "\n".join(lines)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛠 В админ-панель", callback_data="admin_panel")],
        ]
    )
    await safe_edit(callback, text, parse_mode="HTML", reply_markup=kb, prefer_edit=True)
    await callback.answer()


@dp.message(Command("issue_promo"))
async def cmd_issue_promo(message: Message, command: CommandObject):
    if not await auth.is_admin(message.from_user.id):
        return
    args = (command.args or "").strip().split()
    if len(args) < 2:
        await message.answer(
            "Использование: <code>/issue_promo CODE bonus_days [max_uses]</code>\n"
            "Например: <code>/issue_promo SUMMER25 30 100</code>",
            parse_mode="HTML",
        )
        return
    code = args[0]
    try:
        bonus_days = int(args[1])
    except ValueError:
        await message.answer("bonus_days должен быть целым числом.")
        return
    if bonus_days <= 0 or bonus_days > 3650:
        await message.answer("bonus_days должен быть в диапазоне 1..3650.")
        return
    max_uses: Optional[int] = None
    if len(args) >= 3:
        try:
            max_uses = int(args[2])
        except ValueError:
            await message.answer("max_uses должен быть целым числом или опущен.")
            return
        if max_uses <= 0:
            await message.answer("max_uses должен быть положительным.")
            return
    ok = await db.create_promocode(
        code, bonus_days=bonus_days, max_uses=max_uses, created_by=message.from_user.id
    )
    if not ok:
        await message.answer(f"⚠️ Промокод <code>{html.escape(code)}</code> уже существует.", parse_mode="HTML")
        return
    mu = "∞" if max_uses is None else str(max_uses)
    await message.answer(
        f"✅ Промокод <code>{html.escape(code)}</code> создан.\n"
        f"Бонус: <b>+{bonus_days}</b> дн., лимит использований: <b>{mu}</b>.",
        parse_mode="HTML",
    )


@dp.message(Command("revoke_promo"))
async def cmd_revoke_promo(message: Message, command: CommandObject):
    if not await auth.is_admin(message.from_user.id):
        return
    code = (command.args or "").strip()
    if not code:
        await message.answer("Использование: <code>/revoke_promo CODE</code>", parse_mode="HTML")
        return
    ok = await db.revoke_promocode(code)
    if ok:
        await message.answer(f"🚫 Промокод <code>{html.escape(code)}</code> отозван.", parse_mode="HTML")
    else:
        await message.answer(f"Промокод <code>{html.escape(code)}</code> не найден.", parse_mode="HTML")


@dp.message(Command("list_promos"))
async def cmd_list_promos(message: Message):
    if not await auth.is_admin(message.from_user.id):
        return
    rows = await db.list_promocodes(limit=50)
    if not rows:
        await message.answer("Промокодов пока нет.")
        return
    lines = ["🎁 <b>Промокоды</b>\n"]
    for code, bonus, max_uses, used, revoked, _created in rows:
        mu = "∞" if max_uses is None else str(max_uses)
        status = "🚫" if revoked else "✅"
        lines.append(
            f"{status} <code>{html.escape(code)}</code> — +{bonus} дн., {used}/{mu}"
        )
    await message.answer("\n".join(lines), parse_mode="HTML")
