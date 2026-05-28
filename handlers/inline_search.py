"""Inline-mode автокомплит юзеров для админа.

Включается у BotFather: /mybots → @bot → Bot Settings → Inline Mode → Turn on.
Использование: в любом чате (или прямо в боте) ввести @<bot_username> <запрос>.
Только админ получит результаты — остальные увидят пустой список с подсказкой.
"""
from datetime import datetime

from aiogram.filters import StateFilter
from aiogram.types import (
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
)

import auth
import database as db
from app import dp
from formatters import format_tg_name


@dp.inline_query(StateFilter("*"))
async def inline_user_search(query: InlineQuery):
    if not await auth.is_admin(query.from_user.id):
        await query.answer(
            results=[],
            cache_time=1,
            is_personal=True,
            switch_pm_text="Нет доступа",
            switch_pm_parameter="no_access",
        )
        return
    q = (query.query or "").strip()
    if not q:
        rows = await db.list_users(limit=20, offset=0)
    else:
        rows = await db.search_users(q, limit=20, offset=0)
    results: list[InlineQueryResultArticle] = []
    for (
        tg_id, _uuid, _short, panel_username, expire_date,
        role, tg_username, tg_first_name, tg_last_name,
    ) in rows:
        marker = "👑" if role == db.ROLE_ADMIN else "👤"
        tg_name = format_tg_name(tg_username, tg_first_name, tg_last_name)
        when = (
            datetime.fromtimestamp(int(expire_date)).strftime("%d.%m.%Y")
            if expire_date else "—"
        )
        title = f"{marker} {tg_id} · {tg_name}"[:64]
        desc_parts = []
        if tg_username:
            desc_parts.append(f"@{tg_username}")
        if panel_username:
            desc_parts.append(panel_username)
        desc_parts.append(f"до {when}")
        description = " · ".join(desc_parts)[:128]
        # При выборе — отправится `/whois <tg_id>`, бот покажет полную карточку.
        msg = InputTextMessageContent(message_text=f"/whois {tg_id}")
        results.append(
            InlineQueryResultArticle(
                id=str(tg_id),
                title=title,
                description=description,
                input_message_content=msg,
            )
        )
    await query.answer(
        results=results,
        cache_time=1,
        is_personal=True,
        switch_pm_text="🛠 Открыть бота",
        switch_pm_parameter="from_inline",
    )
