"""Глобальные синглтоны и общие утилиты бота.

Здесь:
- инстансы `bot`, `dp`, `api`,
- FSM-states (Promo/AdminSearch/AdminDm),
- middleware `TgProfileMiddleware`,
- `safe_edit`, `sync_local_expire_from_panel` — нужны нескольким хендлер-модулям,
- `SUPPORT_KEY` — ключ KV.
"""
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    TelegramObject,
    User,
)

import database as db
from config import BOT_TOKEN, REMNAWAVE_TOKEN, REMNAWAVE_URL
from remnawave_api import RemnawaveAPI

logger = logging.getLogger(__name__)


# --- Глобальные синглтоны ---

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
api = RemnawaveAPI(base_url=REMNAWAVE_URL, api_token=REMNAWAVE_TOKEN)


# --- FSM States ---

class PromoStates(StatesGroup):
    waiting_for_code = State()
    waiting_for_sub_pick = State()


class AdminSearchStates(StatesGroup):
    waiting_for_query = State()


class AdminDmStates(StatesGroup):
    waiting_for_text = State()


# --- Settings KV keys ---

SUPPORT_KEY = "support_text"


# --- Middleware ---

class TgProfileMiddleware(BaseMiddleware):
    """Сохраняет/обновляет Telegram-имя пользователя при каждом обращении к боту."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: Optional[User] = data.get("event_from_user")
        if user is not None and not user.is_bot:
            try:
                await db.upsert_tg_profile(
                    user.id,
                    tg_username=user.username,
                    tg_first_name=user.first_name,
                    tg_last_name=user.last_name,
                )
            except Exception as exc:
                logger.warning("upsert_tg_profile failed for tg_id=%s: %s", user.id, exc)
        return await handler(event, data)


dp.message.middleware(TgProfileMiddleware())
dp.callback_query.middleware(TgProfileMiddleware())


# --- Общие утилиты ---

async def safe_edit(
    callback: CallbackQuery,
    text: str,
    *,
    parse_mode: str,
    reply_markup: InlineKeyboardMarkup,
    prefer_edit: bool,
) -> None:
    """Редактирует исходное сообщение, либо шлёт новое если редактирование не удалось."""
    if prefer_edit:
        try:
            await callback.message.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
            return
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                return
            logger.info("edit_text не удался (%s), отправляем новое сообщение", e)
    await callback.message.answer(text, parse_mode=parse_mode, reply_markup=reply_markup)


async def sync_local_expire_from_panel(tg_id: int, full_uuid: str) -> None:
    """Синкает expire_date конкретной подписки (по uuid) с тем, что отдаёт панель."""
    info = await api.get_user_info(full_uuid)
    if not info or "response" not in info:
        return
    iso = info["response"].get("expireAt")
    if not iso:
        return
    s = iso.replace("Z", "+00:00") if iso.endswith("Z") else iso
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    await db.update_subscription_expire_by_uuid(full_uuid, int(dt.timestamp()))
