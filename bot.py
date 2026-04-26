import asyncio
import html
import io
import logging
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

import qrcode
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    BufferedInputFile,
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Message,
    TelegramObject,
    User,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import auth
import database as db
from config import (
    ADMIN_TG_IDS,
    BOT_TOKEN,
    DEFAULT_TOKEN_EXPIRE_DAYS,
    DEFAULT_TOKEN_HWID_LIMIT,
    LOG_FILE_PATH,
    LOG_LEVEL,
    REMNAWAVE_TOKEN,
    REMNAWAVE_URL,
    SCHEDULER_CRON_HOUR,
    SCHEDULER_CRON_MINUTE,
    SCHEDULER_TIMEZONE,
    SUB_DOMAIN,
)
from remnawave_api import RemnawaveAPI
from scheduler import check_expiring_subscriptions


def _setup_logging() -> None:
    handlers = [logging.StreamHandler(sys.stdout)]
    if LOG_FILE_PATH:
        handlers.append(logging.FileHandler(LOG_FILE_PATH, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        handlers=handlers,
    )


_setup_logging()
logger = logging.getLogger(__name__)


# Лимит устройств по HWID: значение по умолчанию при создании и если панель вернула null
DEFAULT_HWID_DEVICE_LIMIT = 3
# Верхняя граница при +1 / +3 (числовой лимит)
MAX_HWID_INCREMENT_CAP = 9999
# Значение «практически без лимита» (отображается как ♾)
HWID_UNLIMITED_SENTINEL = 9_999_999


def effective_hwid_limit(api_data: dict) -> int:
    v = api_data.get("hwidDeviceLimit")
    if v is None:
        return DEFAULT_HWID_DEVICE_LIMIT
    return int(v)


def is_hwid_unlimited(api_data: dict) -> bool:
    v = api_data.get("hwidDeviceLimit")
    if v is None:
        return False
    return int(v) >= HWID_UNLIMITED_SENTINEL


def hwid_limit_caption(api_data: dict) -> str:
    v = api_data.get("hwidDeviceLimit")
    if v is None:
        return str(DEFAULT_HWID_DEVICE_LIMIT)
    vi = int(v)
    if vi >= HWID_UNLIMITED_SENTINEL:
        return "♾ без лимита"
    return str(vi)


def human_bytes(n: int) -> str:
    n = max(0, int(n))
    for div, name in ((1 << 30, "ГБ"), (1 << 20, "МБ"), (1 << 10, "КБ")):
        if n >= div:
            x = n / div
            s = f"{x:.2f}".rstrip("0").rstrip(".")
            return f"{s} {name}"
    return f"{n} Б"


def traffic_summary_markdown(api_data: dict) -> str:
    ut = api_data.get("userTraffic") or {}
    used = int(ut.get("usedTrafficBytes") or 0)
    life = int(ut.get("lifetimeUsedTrafficBytes") or 0)
    tlim = api_data.get("trafficLimitBytes")
    lim_txt = "без лимита"
    if tlim is not None and int(tlim) > 0:
        lim_txt = human_bytes(int(tlim))
    return (
        f"**Использовано (период):** {human_bytes(used)}\n"
        f"**За всё время:** {human_bytes(life)}\n"
        f"**Лимит трафика:** {lim_txt}"
    )


def format_expire_display(iso_str: Optional[str]) -> str:
    if not iso_str:
        return "—"
    s = iso_str.replace("Z", "+00:00") if iso_str.endswith("Z") else iso_str
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


def sort_hwid_devices(devices: list) -> list:
    return sorted(devices or [], key=lambda x: (x.get("createdAt") or ""))


# --- Клавиатуры ---

def back_only_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад в главное меню", callback_data="back_main")]
        ]
    )


def main_keyboard_user() -> InlineKeyboardMarkup:
    """Меню обычного пользователя — read-only, с поддержкой нескольких подписок."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📅 Мои подписки", callback_data="my_subs")],
            [InlineKeyboardButton(text="📥 Подключить", callback_data="connect")],
            [InlineKeyboardButton(text="🎁 Промокод", callback_data="promo_input")],
            [InlineKeyboardButton(text="❓ Поддержка", callback_data="support")],
        ]
    )


def main_keyboard_admin(tg_id: int, has_account: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users:0")],
        [InlineKeyboardButton(text="🔑 Выдать токен", callback_data="admin_issue_token")],
        [InlineKeyboardButton(text="📋 Активные токены", callback_data="admin_tokens")],
        [InlineKeyboardButton(text="🎁 Промокоды", callback_data="admin_promos")],
        [InlineKeyboardButton(text="❓ Поддержка", callback_data="admin_support")],
        [InlineKeyboardButton(text="📖 Гайд для админа", callback_data="admin_help")],
    ]
    if has_account:
        rows.append(
            [InlineKeyboardButton(text="⚙️ Мой аккаунт", callback_data=f"admu:{tg_id}:open")]
        )
    rows.append([InlineKeyboardButton(text="📥 Подключить", callback_data="connect")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_devices_html(devices: list, limit_label: str) -> str:
    header = f"📱 <b>Устройства</b> (лимит HWID: {html.escape(limit_label)})"
    if not devices:
        return (
            header
            + "\n\nСписок пуст. Устройства появятся после подключения клиента "
              "с поддержкой HWID (Happ, v2RayTun и др.)."
        )
    blocks = [header]
    for i, d in enumerate(devices):
        pl = html.escape(str(d.get("platform") or "—"))
        model = html.escape(str(d.get("deviceModel") or "—"))
        os_ver = d.get("osVersion")
        os_part = f"\n   ОС: {html.escape(str(os_ver))}" if os_ver else ""
        blocks.append(f"\n\n<b>{i + 1}.</b> {model}{os_part}\n   Платформа: {pl}")
    return "".join(blocks)


def devices_admin_keyboard(target_tg: int, device_count: int, show_limit_buttons: bool) -> InlineKeyboardMarkup:
    rows = []
    for i in range(device_count):
        rows.append(
            [InlineKeyboardButton(text=f"🗑 Удалить #{i + 1}", callback_data=f"admu:{target_tg}:hw_rm:{i}")]
        )
    rows.append([InlineKeyboardButton(text="🔄 Обновить список", callback_data=f"admu:{target_tg}:dev_refresh")])
    if show_limit_buttons:
        rows.append(
            [
                InlineKeyboardButton(text="➕ +1 к лимиту", callback_data=f"admu:{target_tg}:hw_lim:1"),
                InlineKeyboardButton(text="➕ +3 к лимиту", callback_data=f"admu:{target_tg}:hw_lim:3"),
            ]
        )
        rows.append(
            [InlineKeyboardButton(text="♾ Без лимита устройств", callback_data=f"admu:{target_tg}:hw_lim:inf")]
        )
    rows.append([InlineKeyboardButton(text="◀️ К пользователю", callback_data=f"admu:{target_tg}:open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def devices_user_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить список", callback_data="my_devices_refresh")],
            [InlineKeyboardButton(text="◀️ Назад в главное меню", callback_data="back_main")],
        ]
    )


def subscription_admin_keyboard(target_tg: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="+7 дней", callback_data=f"admu:{target_tg}:sub_ext:7"),
                InlineKeyboardButton(text="+30 дней", callback_data=f"admu:{target_tg}:sub_ext:30"),
            ],
            [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"admu:{target_tg}:sub_refresh")],
            [InlineKeyboardButton(text="◀️ К пользователю", callback_data=f"admu:{target_tg}:open")],
        ]
    )


def subscription_user_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="my_subscription_refresh")],
            [InlineKeyboardButton(text="◀️ Назад в главное меню", callback_data="back_main")],
        ]
    )


# --- Bot wiring ---

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
api = RemnawaveAPI(base_url=REMNAWAVE_URL, api_token=REMNAWAVE_TOKEN)


class PromoStates(StatesGroup):
    waiting_for_code = State()
    waiting_for_sub_pick = State()


SUPPORT_KEY = "support_text"


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
            except Exception as exc:  # не ломаем хендлер из-за БД
                logger.warning("upsert_tg_profile failed for tg_id=%s: %s", user.id, exc)
        return await handler(event, data)


dp.message.middleware(TgProfileMiddleware())
dp.callback_query.middleware(TgProfileMiddleware())


def format_tg_name(tg_username: Optional[str], first_name: Optional[str], last_name: Optional[str]) -> str:
    """Человекочитаемое имя из TG-полей. Возвращает '—' если ничего не известно."""
    parts: list[str] = []
    full = " ".join(p for p in (first_name, last_name) if p)
    if full:
        parts.append(full)
    if tg_username:
        parts.append(f"@{tg_username}")
    return " · ".join(parts) if parts else "—"


# --- Views ---

async def load_devices_text(full_uuid: str) -> tuple[str, list, bool]:
    info = await api.get_user_info(full_uuid)
    hw_raw = await api.get_user_hwid_devices(full_uuid)
    limit_label = str(DEFAULT_HWID_DEVICE_LIMIT)
    show_limits = True
    if info and "response" in info:
        limit_label = hwid_limit_caption(info["response"])
        show_limits = not is_hwid_unlimited(info["response"])
    devices: list = []
    if hw_raw and "response" in hw_raw:
        devices = sort_hwid_devices(hw_raw["response"].get("devices") or [])
    return format_devices_html(devices, limit_label), devices, show_limits


async def load_subscription_text(full_uuid: str) -> str:
    info = await api.get_user_info(full_uuid)
    if not info or "response" not in info:
        return "📅 <b>Подписка</b>\n\nНе удалось загрузить данные с панели."
    ad = info["response"]
    exp_h = format_expire_display(ad.get("expireAt"))
    return (
        "📅 <b>Подписка</b>\n\n"
        f"Окончание доступа: <b>{html.escape(exp_h)}</b>"
    )


async def safe_edit(callback: CallbackQuery, text: str, *, parse_mode: str, reply_markup: InlineKeyboardMarkup, prefer_edit: bool) -> None:
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


_USERNAME_SAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9_]")
_REMNAWAVE_USERNAME_MAX_LEN = 32


def build_panel_username(
    tg_id: int,
    tg_username: Optional[str],
    tg_first_name: Optional[str],
) -> str:
    """Собирает username для Remnawave из TG-профиля.

    Шаблоны (по приоритету):
      `tg_<id>_<sanitized(@username)>` → если есть @username;
      `tg_<id>_<sanitized(first_name)>` → если есть first_name;
      `tg_<id>` → fallback.
    Sanitize: оставляем только [A-Za-z0-9_], обрезаем до общего лимита 32 символа.
    """
    base = f"tg_{tg_id}"
    raw = tg_username or tg_first_name or ""
    if not raw:
        return base[:_REMNAWAVE_USERNAME_MAX_LEN]
    suffix = _USERNAME_SAFE_CHARS_RE.sub("", raw).strip("_")
    if not suffix:
        return base[:_REMNAWAVE_USERNAME_MAX_LEN]
    full = f"{base}_{suffix}"
    if len(full) <= _REMNAWAVE_USERNAME_MAX_LEN:
        return full
    # обрезаем суффикс, чтобы влезть
    avail = _REMNAWAVE_USERNAME_MAX_LEN - len(base) - 1
    if avail <= 0:
        return base[:_REMNAWAVE_USERNAME_MAX_LEN]
    return f"{base}_{suffix[:avail]}"


async def create_account_for_user(
    tg_id: int,
    *,
    expire_days: int,
    hwid_device_limit: int,
    created_by: Optional[int] = None,
    tg_username: Optional[str] = None,
    tg_first_name: Optional[str] = None,
) -> Optional[str]:
    """Создаёт **новую** подписку в Remnawave для tg_id и кладёт запись в БД.

    Имя в панели: для первой подписки — `tg_<id>_<name>`, для второй+ — добавляется
    суффикс `_<n>`. При коллизии в панели инкрементим n до успеха.
    """
    if tg_username is None and tg_first_name is None:
        existing_profile = await db.get_user_full(tg_id)
        if existing_profile:
            tg_username = existing_profile[6]
            tg_first_name = existing_profile[7]
    base_username = build_panel_username(tg_id, tg_username, tg_first_name)
    existing_count = await db.count_subscriptions(tg_id)

    response = None
    username = base_username
    # Пытаемся создать с прогрессивно увеличивающимся суффиксом, пока не получится.
    for attempt in range(existing_count, existing_count + 20):
        candidate = (
            base_username
            if attempt == 0
            else f"{base_username}_{attempt + 1}"
        )
        # Если получился слишком длинный — обрежем (Remnawave ограничивает 32 символа).
        candidate = candidate[:_REMNAWAVE_USERNAME_MAX_LEN]
        response = await api.create_user(
            username=candidate,
            expire_days=expire_days,
            hwid_device_limit=hwid_device_limit,
        )
        if response and "response" in response:
            username = candidate
            break
        # 409 / username taken → пробуем следующий суффикс
        logger.warning("create_user('%s') не удалось, пробуем следующий суффикс", candidate)
    else:
        logger.error("Не удалось создать аккаунт для tg_id=%s после нескольких попыток", tg_id)
        return None
    if not response or "response" not in response:
        logger.error("Не удалось создать аккаунт для tg_id=%s: %s", tg_id, response)
        return None
    api_data = response["response"]
    sub_url = api_data.get("subscriptionUrl", "")
    full_uuid = api_data.get("uuid", "")
    short_uuid = api_data.get("shortUuid", "")
    expire_time = int(time.time()) + (expire_days * 24 * 60 * 60)
    await db.add_user(
        tg_id=tg_id,
        uuid=full_uuid,
        short_uuid=short_uuid,
        username=username,
        expire_date=expire_time,
        created_by=created_by,
    )
    return sub_url


# --- /start ---

@dp.message(CommandStart(deep_link=True))
async def cmd_start_with_payload(message: Message, command: CommandObject):
    payload = (command.args or "").strip()
    if not payload:
        await _show_start_menu(message)
        return
    await _try_redeem_token(message, payload)


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await _show_start_menu(message)


async def _show_start_menu(message: Message) -> None:
    tg_id = message.from_user.id
    if await auth.is_admin(tg_id):
        has_account = bool(await db.get_user(tg_id))
        await message.answer(
            f"Привет, {html.escape(message.from_user.first_name or '')}! Это админская панель бота.",
            parse_mode="HTML",
            reply_markup=main_keyboard_admin(tg_id, has_account),
        )
        return
    user_data = await db.get_user(tg_id)
    if user_data and user_data[1]:
        await message.answer(
            f"Привет, {html.escape(message.from_user.first_name or '')}! Это бот для управления вашим VPN/Proxy.\n"
            "Выберите нужное действие ниже:",
            parse_mode="HTML",
            reply_markup=main_keyboard_user(),
        )
        return
    await message.answer(
        "🔒 Доступ только по приглашению.\n\n"
        "Получите токен у администратора и активируйте его командой:\n"
        "<code>/redeem ВАШ_ТОКЕН</code>\n\n"
        "Либо перейдите по ссылке-приглашению, которую выдал администратор.",
        parse_mode="HTML",
    )


# --- /redeem ---

@dp.message(Command("redeem"))
async def cmd_redeem(message: Message, command: CommandObject):
    raw = (command.args or "").strip()
    if not raw:
        await message.answer(
            "Использование: <code>/redeem ВАШ_ТОКЕН</code>",
            parse_mode="HTML",
        )
        return
    await _try_redeem_token(message, raw)


async def _try_redeem_token(message: Message, raw_token: str) -> None:
    tg_id = message.from_user.id

    token = await auth.find_redeemable_token(raw_token.split()[0])
    if token is None:
        await message.answer(
            "❌ Токен недействителен, уже использован или отозван.\n"
            "Обратитесь к администратору."
        )
        return

    sub_url = await create_account_for_user(
        tg_id,
        expire_days=token.expire_days,
        hwid_device_limit=token.hwid_device_limit,
        tg_username=message.from_user.username,
        tg_first_name=message.from_user.first_name,
    )
    if not sub_url:
        await message.answer(
            "❌ Не удалось создать аккаунт в панели. Сообщите администратору."
        )
        return

    consumed = await auth.consume_token(token.token_hash, tg_id)
    if not consumed:
        # Кто-то опередил — крайне маловероятно, но логируем.
        logger.warning("Token race for tg_id=%s, hash=%s", tg_id, token.token_hash[:12])

    sub_count = await db.count_subscriptions(tg_id)
    head = (
        "✅ Доступ активирован!"
        if sub_count <= 1
        else f"✅ Добавлена ещё одна подписка (всего: {sub_count})."
    )
    await message.answer(
        f"{head}\n\n"
        f"🔗 Ваша ссылка на подписку:\n<code>{html.escape(sub_url)}</code>\n\n"
        "Скопируйте ссылку и вставьте в приложение или нажмите «📥 Подключить» для пошаговой инструкции.",
        parse_mode="HTML",
        reply_markup=main_keyboard_user(),
    )


# --- Admin commands ---

def _build_invite_link(bot_username: Optional[str], raw_token: str) -> Optional[str]:
    if not bot_username:
        return None
    return f"https://t.me/{bot_username}?start={raw_token}"


@dp.message(Command("issue_token"))
async def cmd_issue_token(message: Message, command: CommandObject):
    if not await auth.is_admin(message.from_user.id):
        await message.answer("Команда доступна только администратору.")
        return
    args = (command.args or "").split()
    expire_days = DEFAULT_TOKEN_EXPIRE_DAYS
    hwid_limit = DEFAULT_TOKEN_HWID_LIMIT
    try:
        if len(args) >= 1:
            expire_days = int(args[0])
        if len(args) >= 2:
            hwid_limit = int(args[1])
    except ValueError:
        await message.answer(
            "Использование: <code>/issue_token [days] [hwid_limit]</code>",
            parse_mode="HTML",
        )
        return
    if expire_days <= 0 or hwid_limit < 0:
        await message.answer("days должно быть > 0, hwid_limit ≥ 0.")
        return

    raw = await auth.issue_token(
        created_by=message.from_user.id,
        expire_days=expire_days,
        hwid_device_limit=hwid_limit,
    )
    me = await bot.get_me()
    invite = _build_invite_link(me.username, raw)
    text_lines = [
        "🔑 Новый одноразовый токен:",
        f"<code>{html.escape(raw)}</code>",
        "",
        f"Срок подписки при активации: <b>{expire_days}</b> дн.",
        f"Лимит устройств (HWID): <b>{hwid_limit}</b>",
    ]
    if invite:
        text_lines += ["", "Ссылка-приглашение:", f"<code>{html.escape(invite)}</code>"]
    text_lines += ["", "⚠️ Сохраните токен сейчас — он показывается только один раз."]
    await message.answer("\n".join(text_lines), parse_mode="HTML")


@dp.message(Command("revoke_token"))
async def cmd_revoke_token(message: Message, command: CommandObject):
    if not await auth.is_admin(message.from_user.id):
        await message.answer("Команда доступна только администратору.")
        return
    raw = (command.args or "").strip()
    if not raw:
        await message.answer(
            "Использование: <code>/revoke_token ХЭШ_ИЛИ_ПРЕФИКС</code>\n"
            "Сам raw-токен использовать тоже можно.",
            parse_mode="HTML",
        )
        return
    # Сначала пробуем как raw-токен
    candidate_hash = auth.hash_token(raw)
    if not await db.get_access_token(candidate_hash):
        # Иначе ищем по префиксу хэша
        candidate_hash = await db.find_token_by_hash_prefix(raw) or candidate_hash
    ok = await db.revoke_access_token(candidate_hash)
    if ok:
        await message.answer("✅ Токен отозван.")
    else:
        await message.answer("❌ Не нашёл подходящий неиспользованный токен.")


@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if not await auth.is_admin(message.from_user.id):
        await message.answer("Команда доступна только администратору.")
        return
    has_account = bool(await db.get_user(message.from_user.id))
    await message.answer(
        "🛠 Админская панель",
        reply_markup=main_keyboard_admin(message.from_user.id, has_account),
    )


ADMIN_HELP_TEXT = (
    "🛠 <b>Гайд для администратора</b>\n\n"
    "<b>📋 Команды</b>\n"
    "• <code>/admin</code> — открыть админ-панель (кнопки внизу).\n"
    "• <code>/help_admin</code> — этот гайд.\n"
    "• <code>/whois &lt;tg_id|@username&gt;</code> — найти юзера в БД (роль, аккаунт, подписка).\n"
    "• <code>/issue_token [days] [hwid_limit]</code> — выпустить одноразовый токен. "
    "По умолчанию: 30 дн., HWID-лимит из конфига. Возвращает <code>raw</code>-токен и invite-ссылку.\n"
    "• <code>/revoke_token &lt;ХЭШ_ИЛИ_ПРЕФИКС&gt;</code> — отозвать неиспользованный токен. "
    "Можно передавать сам raw-токен — посчитается хэш.\n"
    "• <code>/import_users</code> — импорт из Remnawave всех аккаунтов с username "
    "<code>tg_&lt;id&gt;</code> или <code>tg_&lt;id&gt;_&lt;name&gt;</code> в БД. "
    "Существующие записи дополняются недостающими полями.\n"
    "• <code>/issue_promo &lt;CODE&gt; &lt;дни&gt; [max_uses]</code> — создать промокод. "
    "Без <code>max_uses</code> — использований неограниченно.\n"
    "• <code>/revoke_promo &lt;CODE&gt;</code> — отозвать промокод (использовать его больше нельзя).\n"
    "• <code>/list_promos</code> — список последних 20 промокодов.\n"
    "• <code>/dm &lt;tg_id|@username&gt; &lt;текст&gt;</code> — отправить личное сообщение. "
    "Получатель должен существовать в БД (хотя бы раз запускал бота).\n"
    "• <code>/set_support &lt;текст&gt;</code> — задать контакты поддержки (HTML разрешён). "
    "Пустой текст — очистить.\n"
    "• <code>/cancel</code> — выйти из любого ввода (поиск, DM, промо, импорт и т.п.).\n\n"
    "<b>🔘 Кнопки админ-панели</b> (<code>/admin</code>)\n"
    "• <b>👥 Пользователи</b> — список всех юзеров в БД с пагинацией. У каждой записи кнопка-карточка.\n"
    "  В карточке юзера:\n"
    "    · <i>📅 #X · username · до dd.mm.yyyy</i> — открыть конкретную подписку (статус, трафик, устройства).\n"
    "    · <b>✉️ Написать</b> — отправить ему DM от вашего имени (FSM-ввод).\n"
    "    · <b>🗑 Удалить пользователя</b> — снести все подписки в Remnawave + запись в БД (с подтверждением).\n"
    "    · <b>🔎 Поиск</b> в списке — фильтр по подстроке (tg_id, @username, имя, фамилия, panel-username).\n"
    "• <b>🔑 Выдать токен</b> — выпустить новый токен (≡ <code>/issue_token</code> с дефолтами).\n"
    "• <b>📋 Активные токены</b> — список неиспользованных токенов. У каждого:\n"
    "    · <b>✗ Отозвать ХЭШ…</b> — мгновенно пометить токен как отозванный.\n"
    "    · <b>🔄 Обновить</b> — перерисовать список.\n"
    "• <b>🎁 Промокоды</b> — список последних 20 промо. Создание/отзыв через команды.\n"
    "• <b>❓ Поддержка</b> — задать/изменить контакты поддержки (FSM-ввод текста).\n\n"
    "<b>🔍 Inline-поиск</b>\n"
    "В любом чате наберите <code>@&lt;имя_бота&gt; &lt;запрос&gt;</code> — получите список юзеров. "
    "Тап по юзеру отправит <code>/whois &lt;tg_id&gt;</code> в текущий чат и бот покажет карточку.\n"
    "<i>Нужно один раз включить inline-режим у @BotFather:</i> "
    "<code>/mybots → ваш бот → Bot Settings → Inline Mode → Turn on</code>.\n\n"
    "<b>👤 Пользовательский интерфейс</b> (для справки)\n"
    "• <b>📅 Мои подписки</b> — список подписок, тап → меню одной подписки.\n"
    "• <b>📥 Подключить</b> — выбор платформы → клиенты с импорт-ссылками "
    "и кнопками «📋 Скопировать импорт …».\n"
    "• <b>🎁 Промокод</b> — ввод кода, продление выбранной подписки.\n"
    "• <b>❓ Поддержка</b> — показ контактов поддержки.\n"
)


@dp.message(Command("help_admin"))
async def cmd_help_admin(message: Message):
    if not await auth.is_admin(message.from_user.id):
        await message.answer("Команда доступна только администратору.")
        return
    await message.answer(ADMIN_HELP_TEXT, parse_mode="HTML", disable_web_page_preview=True)


@dp.callback_query(F.data == "admin_help")
async def cb_admin_help(callback: CallbackQuery):
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ В админ-панель", callback_data="admin_panel")]]
    )
    await safe_edit(callback, ADMIN_HELP_TEXT, parse_mode="HTML", reply_markup=kb, prefer_edit=True)
    await callback.answer()


def _parse_expire_to_ts(value: Optional[str]) -> int:
    """ISO-строка expireAt → unix timestamp (UTC). Возвращает 0, если не парсится."""
    if not value:
        return 0
    try:
        s = value.replace("Z", "+00:00") if value.endswith("Z") else value
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return 0


_TG_USERNAME_RE = re.compile(r"^tg_(\d+)(?:_[A-Za-z0-9_]*)?$")


@dp.message(Command("import_users"))
async def cmd_import_users(message: Message):
    """Импортирует пользователей из Remnawave с username вида tg_<id> в локальную БД.

    Существующие записи не затираются по роли/created_by — только дополняются
    uuid/short_uuid/expire_date, чтобы админ мог управлять ими через бот.
    """
    if not await auth.is_admin(message.from_user.id):
        await message.answer("Команда доступна только администратору.")
        return

    progress = await message.answer("⏳ Сканирую панель Remnawave…")

    page_size = 200
    start = 0
    total_in_panel = 0
    matched = 0
    inserted = 0
    updated = 0
    skipped_invalid_id = 0
    examples: list[str] = []

    seen_pages = 0
    while True:
        page = await api.list_users(size=page_size, start=start)
        if not page or "response" not in page:
            await progress.edit_text(
                f"❌ Не удалось получить список пользователей с панели "
                f"(start={start}). Проверьте логи бота."
            )
            return
        resp = page["response"]
        total_in_panel = int(resp.get("total") or 0)
        users = resp.get("users") or []
        if not users:
            break
        seen_pages += 1
        for u in users:
            username = u.get("username") or ""
            m = _TG_USERNAME_RE.match(username)
            if not m:
                continue
            try:
                tg_id = int(m.group(1))
            except ValueError:
                skipped_invalid_id += 1
                continue
            matched += 1
            uuid_v = u.get("uuid") or ""
            short_uuid = u.get("shortUuid") or ""
            expire_ts = _parse_expire_to_ts(u.get("expireAt"))

            existing = await db.get_user(tg_id)
            await db.add_user(
                tg_id=tg_id,
                uuid=uuid_v,
                short_uuid=short_uuid,
                username=username,
                expire_date=expire_ts,
                created_by=message.from_user.id,
            )
            if existing:
                updated += 1
            else:
                inserted += 1
                if len(examples) < 5:
                    examples.append(f"<code>{tg_id}</code> → {html.escape(username)}")

        start += len(users)
        if start >= total_in_panel:
            break
        # safety: do not loop forever
        if seen_pages > 200:
            break

    lines = [
        "✅ <b>Импорт завершён</b>",
        f"Юзеров в панели всего: <b>{total_in_panel}</b>",
        f"Подходят под <code>tg_&lt;id&gt;</code>: <b>{matched}</b>",
        f"Добавлено новых: <b>{inserted}</b>",
        f"Обновлено существующих: <b>{updated}</b>",
    ]
    if skipped_invalid_id:
        lines.append(f"Пропущено (битый id): {skipped_invalid_id}")
    if examples:
        lines.append("\nПримеры новых:\n" + "\n".join(examples))
    await progress.edit_text("\n".join(lines), parse_mode="HTML")


@dp.message(Command("whois"))
async def cmd_whois(message: Message, command: CommandObject):
    if not await auth.is_admin(message.from_user.id):
        await message.answer("Команда доступна только администратору.")
        return
    arg = (command.args or "").strip()
    if not arg:
        await message.answer(
            "Использование: <code>/whois &lt;tg_id&gt;</code> или <code>/whois @username</code>",
            parse_mode="HTML",
        )
        return

    full = None
    if arg.lstrip("-").isdigit():
        full = await db.get_user_full(int(arg))
    else:
        full = await db.find_user_by_tg_username(arg)

    if not full:
        await message.answer("Пользователь не найден в базе бота.")
        return

    (
        tg_id,
        full_uuid,
        short_uuid,
        username,
        expire_date,
        role,
        tg_username,
        tg_first_name,
        tg_last_name,
    ) = full
    role = role or db.ROLE_USER
    tg_name = format_tg_name(tg_username, tg_first_name, tg_last_name)
    expire_str = (
        datetime.fromtimestamp(int(expire_date)).strftime("%d.%m.%Y %H:%M")
        if expire_date else "—"
    )
    sub_url = f"{SUB_DOMAIN}/{short_uuid}" if short_uuid else "—"
    text = (
        "🔍 <b>Найден пользователь</b>\n\n"
        f"<b>tg_id:</b> <code>{tg_id}</code>\n"
        f"<b>имя в TG:</b> {html.escape(tg_name)}\n"
        f"<b>panel username:</b> {html.escape(username or '—')}\n"
        f"<b>роль:</b> {html.escape(role)}\n"
        f"<b>подписка до:</b> {html.escape(expire_str)}\n"
        f"<b>ссылка:</b> <code>{html.escape(sub_url)}</code>"
    )
    if not full_uuid:
        text += "\n\n⚠️ Нет аккаунта в Remnawave (токен не активирован)."
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👤 Открыть карточку", callback_data=f"admu:{tg_id}:open")]
        ]
    )
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


# --- Inline mode: автокомплит юзеров для админа ---
# Включается у BotFather: /mybots → @bot → Bot Settings → Inline Mode → Turn on.
# Использование: в любом чате (или прямо в боте) ввести @<bot_username> <запрос>.
# Только админ получит результаты — остальные увидят пустой список с подсказкой.

@dp.inline_query()
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


# --- Admin panel callbacks ---

PAGE_SIZE = 8


async def _send_admin_users_list(
    callback: CallbackQuery,
    page: int,
    *,
    prefer_edit: bool,
    query: Optional[str] = None,
) -> None:
    if query:
        total = await db.count_search_users(query)
        header = f"🔎 <b>Поиск</b>: <code>{html.escape(query)}</code> (найдено: {total})"
    else:
        total = await db.count_users()
        header = f"👥 <b>Пользователи</b> (всего: {total})"

    if total == 0:
        text = header + "\n\n" + ("Ничего не найдено." if query else "Список пользователей пуст.")
        kb_rows: list[list[InlineKeyboardButton]] = []
        if query:
            kb_rows.append([InlineKeyboardButton(text="🔎 Новый поиск", callback_data="admin_users_search")])
            kb_rows.append([InlineKeyboardButton(text="◀️ К списку", callback_data="admin_users:0")])
        kb_rows.append([InlineKeyboardButton(text="◀️ В админ-панель", callback_data="admin_panel")])
        await safe_edit(callback, text, parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
                        prefer_edit=prefer_edit)
        return

    page = max(0, page)
    offset = page * PAGE_SIZE
    if query:
        rows = await db.search_users(query, limit=PAGE_SIZE, offset=offset)
    else:
        rows = await db.list_users(limit=PAGE_SIZE, offset=offset)
    lines = [header]
    buttons: list[list[InlineKeyboardButton]] = []
    for (
        tg_id,
        _uuid,
        _short,
        _username,
        expire_date,
        role,
        tg_username,
        tg_first_name,
        tg_last_name,
    ) in rows:
        marker = "👑" if role == db.ROLE_ADMIN else "👤"
        tg_name = format_tg_name(tg_username, tg_first_name, tg_last_name)
        when = "—"
        if expire_date:
            when = datetime.fromtimestamp(int(expire_date)).strftime("%d.%m.%Y")
        lines.append(
            f"{marker} <code>{tg_id}</code> · {html.escape(tg_name)} · до {when}"
        )
        button_label = f"{marker} {tg_id} · {tg_name}"
        if len(button_label) > 60:
            button_label = button_label[:57] + "…"
        buttons.append(
            [InlineKeyboardButton(text=button_label, callback_data=f"admu:{tg_id}:open")]
        )

    nav_row: list[InlineKeyboardButton] = []
    nav_prefix = "admin_users_qp" if query else "admin_users"
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="◀️", callback_data=f"{nav_prefix}:{page - 1}"))
    if offset + PAGE_SIZE < total:
        nav_row.append(InlineKeyboardButton(text="▶️", callback_data=f"{nav_prefix}:{page + 1}"))
    if nav_row:
        buttons.append(nav_row)
    if query:
        buttons.append([InlineKeyboardButton(text="🔎 Новый поиск", callback_data="admin_users_search")])
        buttons.append([InlineKeyboardButton(text="◀️ К полному списку", callback_data="admin_users:0")])
    else:
        buttons.append([InlineKeyboardButton(text="🔎 Поиск", callback_data="admin_users_search")])
    buttons.append([InlineKeyboardButton(text="◀️ В админ-панель", callback_data="admin_panel")])

    await safe_edit(
        callback,
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        prefer_edit=prefer_edit,
    )


@dp.callback_query(F.data == "admin_panel")
async def cb_admin_panel(callback: CallbackQuery):
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    has_account = bool(await db.get_user(callback.from_user.id))
    await safe_edit(
        callback,
        "🛠 Админская панель",
        parse_mode="HTML",
        reply_markup=main_keyboard_admin(callback.from_user.id, has_account),
        prefer_edit=True,
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("admin_users:"))
async def cb_admin_users(callback: CallbackQuery):
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    try:
        page = int(callback.data.split(":", 1)[1])
    except (IndexError, ValueError):
        page = 0
    await _send_admin_users_list(callback, page, prefer_edit=True)
    await callback.answer()


class AdminSearchStates(StatesGroup):
    waiting_for_query = State()


@dp.callback_query(F.data == "admin_users_search")
async def cb_admin_users_search(callback: CallbackQuery, state: FSMContext):
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    await state.set_state(AdminSearchStates.waiting_for_query)
    await callback.message.answer(
        "🔎 Введите подстроку для поиска: tg_id, @username, имя/фамилия или username "
        "аккаунта в Remnawave (можно частично). /cancel — отменить.",
    )
    await callback.answer()


@dp.message(AdminSearchStates.waiting_for_query)
async def admin_search_capture(message: Message, state: FSMContext):
    if not await auth.is_admin(message.from_user.id):
        await state.clear()
        return
    text = (message.text or "").strip()
    if text == "/cancel":
        await state.clear()
        await message.answer("Отменено.")
        return
    if not text:
        await message.answer("Запрос не может быть пустым. /cancel — отменить.")
        return
    await state.update_data(search_query=text)
    # Не закрываем state — пагинация остаётся завязана на запрос; чтобы выйти, кнопка
    # «◀️ К полному списку» сбросит контекст (через переход на admin_users:0).
    fake_cb = await _make_pseudo_callback(message)
    await _send_admin_users_list(fake_cb, page=0, prefer_edit=False, query=text)


@dp.callback_query(F.data.startswith("admin_users_qp:"), AdminSearchStates.waiting_for_query)
async def cb_admin_users_search_page(callback: CallbackQuery, state: FSMContext):
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    try:
        page = int(callback.data.split(":", 1)[1])
    except (IndexError, ValueError):
        page = 0
    data = await state.get_data()
    query = data.get("search_query")
    if not query:
        await callback.answer("Контекст поиска утерян.", show_alert=True)
        await _send_admin_users_list(callback, page=0, prefer_edit=True)
        return
    await _send_admin_users_list(callback, page=page, prefer_edit=True, query=query)
    await callback.answer()


async def _make_pseudo_callback(message: Message):
    """Хелпер: строит «псевдо-CallbackQuery» для переиспользования _send_admin_users_list,
    который принимает CallbackQuery. Здесь нам нужен только .message и .from_user."""
    class _PseudoCB:
        def __init__(self, msg: Message):
            self.message = msg
            self.from_user = msg.from_user

        async def answer(self, *args, **kwargs):
            return None

    return _PseudoCB(message)


@dp.callback_query(F.data == "admin_issue_token")
async def cb_admin_issue_token(callback: CallbackQuery):
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    raw = await auth.issue_token(
        created_by=callback.from_user.id,
        expire_days=DEFAULT_TOKEN_EXPIRE_DAYS,
        hwid_device_limit=DEFAULT_TOKEN_HWID_LIMIT,
    )
    me = await bot.get_me()
    invite = _build_invite_link(me.username, raw)
    text_lines = [
        "🔑 Новый одноразовый токен:",
        f"<code>{html.escape(raw)}</code>",
        "",
        f"Срок при активации: <b>{DEFAULT_TOKEN_EXPIRE_DAYS}</b> дн., HWID: <b>{DEFAULT_TOKEN_HWID_LIMIT}</b>",
    ]
    if invite:
        text_lines += ["", "Ссылка-приглашение:", f"<code>{html.escape(invite)}</code>"]
    text_lines += [
        "",
        "Чтобы изменить параметры — используйте команду <code>/issue_token [days] [hwid_limit]</code>.",
        "",
        "⚠️ Сохраните токен сейчас — он показывается только один раз.",
    ]
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ В админ-панель", callback_data="admin_panel")]]
    )
    await callback.message.answer("\n".join(text_lines), parse_mode="HTML", reply_markup=kb)
    await callback.answer("Токен выдан")


async def _render_admin_tokens(callback: CallbackQuery, *, prefer_edit: bool) -> None:
    rows = await db.list_active_tokens(limit=20)
    buttons: list[list[InlineKeyboardButton]] = []
    if not rows:
        text = "Активных (неиспользованных) токенов нет."
    else:
        lines = ["📋 <b>Активные токены</b> (хэш-префикс · автор · дни · HWID):"]
        for token_hash, created_by, _created_at, expire_days, hwid_device_limit in rows:
            prefix = token_hash[:12]
            lines.append(
                f"• <code>{prefix}…</code> · автор {created_by} · {expire_days} дн. · HWID {hwid_device_limit}"
            )
            buttons.append([InlineKeyboardButton(
                text=f"✗ Отозвать {prefix}…",
                callback_data=f"revtok:{prefix}",
            )])
        lines.append("")
        lines.append("Также можно отозвать командой: <code>/revoke_token ПРЕФИКС</code>")
        text = "\n".join(lines)
    buttons.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_tokens")])
    buttons.append([InlineKeyboardButton(text="◀️ В админ-панель", callback_data="admin_panel")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await safe_edit(callback, text, parse_mode="HTML", reply_markup=kb, prefer_edit=prefer_edit)


@dp.callback_query(F.data == "admin_tokens")
async def cb_admin_tokens(callback: CallbackQuery):
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    await _render_admin_tokens(callback, prefer_edit=True)
    await callback.answer()


@dp.callback_query(F.data.startswith("revtok:"))
async def cb_revoke_token_inline(callback: CallbackQuery):
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    parts = callback.data.split(":", 1)
    if len(parts) != 2 or not parts[1]:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    prefix = parts[1]
    full_hash = await db.find_token_by_hash_prefix(prefix)
    if not full_hash:
        await callback.answer("Токен не найден или префикс неуникален.", show_alert=True)
        return
    ok = await db.revoke_access_token(full_hash)
    if not ok:
        await callback.answer("Не удалось отозвать (возможно, уже использован/отозван).", show_alert=True)
    else:
        await callback.answer(f"Отозван: {prefix}…")
    await _render_admin_tokens(callback, prefer_edit=True)


# --- User self-service (read-only) ---

async def _ensure_authorized_user(callback: CallbackQuery) -> Optional[tuple]:
    user_data = await db.get_user(callback.from_user.id)
    if not user_data or not user_data[1]:
        await callback.answer(
            "Доступ только по приглашению. Активируйте токен через /redeem.",
            show_alert=True,
        )
        return None
    return user_data


def _format_sub_caption(sub: tuple) -> str:
    """Делает читаемое название из (id, uuid, short_uuid, username, expire_date, label, created_at)."""
    sid, _uuid, _short, username, expire_date, label, _created = sub
    if label:
        head = label
    elif username:
        head = username
    else:
        head = f"#{sid}"
    if expire_date:
        ts = datetime.fromtimestamp(int(expire_date)).strftime("%d.%m.%Y")
        return f"#{sid} · {head} · до {ts}"
    return f"#{sid} · {head}"


async def _ensure_sub_belongs_to_user(callback: CallbackQuery, sub_id: int) -> Optional[tuple]:
    sub = await db.get_subscription(sub_id)
    if not sub or sub[1] != callback.from_user.id:
        await callback.answer("Подписка не найдена.", show_alert=True)
        return None
    return sub


def _user_sub_menu_keyboard(sub_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статус и трафик", callback_data=f"sub:info:{sub_id}")],
            [InlineKeyboardButton(text="📱 Устройства", callback_data=f"sub:dev:{sub_id}")],
            [InlineKeyboardButton(text="📥 Подключить", callback_data=f"sub:conn:{sub_id}")],
            [InlineKeyboardButton(text="◀️ К списку подписок", callback_data="my_subs")],
        ]
    )


@dp.callback_query(F.data == "my_subs")
async def cb_my_subs(callback: CallbackQuery):
    if not (await auth.is_admin(callback.from_user.id) or await auth.is_authorized(callback.from_user.id)):
        await callback.answer("Доступ только по приглашению.", show_alert=True)
        return
    subs = await db.list_subscriptions(callback.from_user.id)
    if not subs:
        await safe_edit(
            callback,
            "У вас пока нет активных подписок. Активируйте токен через /redeem.",
            parse_mode="HTML",
            reply_markup=back_only_keyboard(),
            prefer_edit=True,
        )
        await callback.answer()
        return
    if len(subs) == 1:
        await _render_sub_open(callback, subs[0], prefer_edit=True)
        return
    rows = []
    for sub in subs:
        cap = _format_sub_caption(sub)[:60]
        rows.append([InlineKeyboardButton(text=cap, callback_data=f"sub:open:{sub[0]}")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")])
    await safe_edit(
        callback,
        f"📅 <b>Ваши подписки</b> ({len(subs)})",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        prefer_edit=True,
    )
    await callback.answer()


async def _render_sub_open(callback: CallbackQuery, sub: tuple, *, prefer_edit: bool) -> None:
    sid, uuid, short_uuid, username, expire_date, label, _created = sub
    expire_str = (
        datetime.fromtimestamp(int(expire_date)).strftime("%d.%m.%Y %H:%M")
        if expire_date else "—"
    )
    sub_url = f"{SUB_DOMAIN}/{short_uuid}" if short_uuid else "—"
    text = (
        f"📅 <b>Подписка #{sid}</b>"
        f"{(' · ' + html.escape(label)) if label else ''}\n\n"
        f"<b>Имя в панели:</b> <code>{html.escape(username or '—')}</code>\n"
        f"<b>Действует до:</b> {html.escape(expire_str)}\n"
        f"<b>Ссылка:</b> <code>{html.escape(sub_url)}</code>"
    )
    await safe_edit(
        callback, text, parse_mode="HTML",
        reply_markup=_user_sub_menu_keyboard(sid), prefer_edit=prefer_edit,
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("sub:open:"))
async def cb_sub_open(callback: CallbackQuery):
    sub_id = int(callback.data.split(":", 2)[2])
    sub = await _ensure_sub_belongs_to_user(callback, sub_id)
    if not sub:
        return
    # adapt 9-tuple to 7-tuple for _render_sub_open
    adapted = (sub[0], sub[2], sub[3], sub[4], sub[5], sub[6], sub[8])
    await _render_sub_open(callback, adapted, prefer_edit=True)


@dp.callback_query(F.data.startswith("sub:info:"))
async def cb_sub_info(callback: CallbackQuery):
    sub_id = int(callback.data.split(":", 2)[2])
    sub = await _ensure_sub_belongs_to_user(callback, sub_id)
    if not sub:
        return
    full_uuid = sub[2]
    short_uuid = sub[3]
    expire_timestamp = sub[5]
    expire_date_str = (
        datetime.fromtimestamp(expire_timestamp).strftime("%d.%m.%Y %H:%M")
        if expire_timestamp else "—"
    )
    sub_url = f"{SUB_DOMAIN}/{short_uuid}" if short_uuid else "—"
    info = await api.get_user_info(full_uuid)
    status_text = "Активна ✅"
    limit_text = str(DEFAULT_HWID_DEVICE_LIMIT)
    traffic_lines = ""
    if info and "response" in info:
        api_data = info["response"]
        panel_status = api_data.get("status", "ACTIVE")
        if panel_status != "ACTIVE":
            status_text = "Неактивна ❌"
        limit_text = hwid_limit_caption(api_data)
        traffic_lines = "\n\n" + traffic_summary_markdown(api_data)
    text = (
        f"📊 **Подписка #{sub_id}**\n\n"
        f"**Статус:** {status_text}\n"
        f"**Лимит устройств (HWID):** {limit_text}\n"
        f"**Действует до:** `{expire_date_str}`\n"
        f"{traffic_lines}\n\n"
        f"🔗 **Ссылка:** `{sub_url}`"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"sub:info:{sub_id}")],
            [InlineKeyboardButton(text="◀️ К подписке", callback_data=f"sub:open:{sub_id}")],
        ]
    )
    await safe_edit(callback, text, parse_mode="Markdown", reply_markup=kb, prefer_edit=True)
    await callback.answer()


@dp.callback_query(F.data.startswith("sub:dev:"))
async def cb_sub_devices(callback: CallbackQuery):
    sub_id = int(callback.data.split(":", 2)[2])
    sub = await _ensure_sub_belongs_to_user(callback, sub_id)
    if not sub:
        return
    full_uuid = sub[2]
    text, _devices, _show = await load_devices_text(full_uuid)
    text = f"📱 <b>Устройства подписки #{sub_id}</b>\n\n" + text
    text += "\n\nℹ️ Управление лимитами доступно только администратору."
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"sub:dev:{sub_id}")],
            [InlineKeyboardButton(text="◀️ К подписке", callback_data=f"sub:open:{sub_id}")],
        ]
    )
    await safe_edit(callback, text, parse_mode="HTML", reply_markup=kb, prefer_edit=True)
    await callback.answer()


@dp.callback_query(F.data == "my_settings")
async def cb_my_settings(callback: CallbackQuery):
    user_data = await _ensure_authorized_user(callback)
    if not user_data:
        return
    full_uuid = user_data[1]
    short_uuid = user_data[2]
    expire_timestamp = user_data[4]
    expire_date_str = (
        datetime.fromtimestamp(expire_timestamp).strftime("%d.%m.%Y %H:%M")
        if expire_timestamp
        else "—"
    )
    sub_url = f"{SUB_DOMAIN}/{short_uuid}" if short_uuid else "—"

    info = await api.get_user_info(full_uuid)
    status_text = "Активна ✅"
    limit_text = str(DEFAULT_HWID_DEVICE_LIMIT)
    traffic_lines = ""
    if info and "response" in info:
        api_data = info["response"]
        panel_status = api_data.get("status", "ACTIVE")
        if panel_status != "ACTIVE":
            status_text = "Неактивна ❌"
        limit_text = hwid_limit_caption(api_data)
        traffic_lines = "\n\n" + traffic_summary_markdown(api_data)

    text = (
        "👤 **Ваш профиль VPN/Proxy**\n\n"
        f"**Статус:** {status_text}\n"
        f"**Лимит устройств (HWID):** {limit_text}\n"
        f"**Подписка до:** `{expire_date_str}`\n"
        f"{traffic_lines}\n\n"
        f"🔗 **Ваша ссылка для подключения:**\n`{sub_url}`\n\n"
        "*(Скопируйте ссылку и обновите в приложении)*"
    )
    await callback.message.answer(text, parse_mode="Markdown", reply_markup=back_only_keyboard())
    await callback.answer()


@dp.callback_query(F.data.in_({"my_subscription", "my_subscription_refresh"}))
async def cb_my_subscription(callback: CallbackQuery):
    user_data = await _ensure_authorized_user(callback)
    if not user_data:
        return
    full_uuid = user_data[1]
    text = await load_subscription_text(full_uuid)
    text += (
        "\n\nℹ️ Продление подписки доступно только администратору. "
        "Если срок подходит к концу — обратитесь к администратору."
    )
    await safe_edit(
        callback,
        text,
        parse_mode="HTML",
        reply_markup=subscription_user_keyboard(),
        prefer_edit=callback.data == "my_subscription_refresh",
    )
    await callback.answer("Обновлено" if callback.data == "my_subscription_refresh" else None)


@dp.callback_query(F.data.in_({"my_devices", "my_devices_refresh"}))
async def cb_my_devices(callback: CallbackQuery):
    user_data = await _ensure_authorized_user(callback)
    if not user_data:
        return
    full_uuid = user_data[1]
    text, _devices, _show = await load_devices_text(full_uuid)
    text += (
        "\n\nℹ️ Управление устройствами и лимитами доступно только администратору."
    )
    await safe_edit(
        callback,
        text,
        parse_mode="HTML",
        reply_markup=devices_user_keyboard(),
        prefer_edit=callback.data == "my_devices_refresh",
    )
    await callback.answer("Обновлено" if callback.data == "my_devices_refresh" else None)


# --- Connect / VPN client instructions ---

# Каталог рекомендуемых клиентов по платформам.
# `deeplink_template` — шаблон импорта подписки в клиент. `{sub}` — URL подписки целиком.
# Для клиентов без подтверждённого deep-link оставляем None — будет только инструкция и QR.
CLIENT_CATALOG: dict[str, list[dict]] = {
    "ios": [
        {
            "name": "Happ",
            "stores": [
                ("App Store", "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215"),
            ],
            "deeplink_template": "happ://add/{sub}",
        },
        {
            "name": "V2Box",
            "stores": [
                ("App Store", "https://apps.apple.com/us/app/v2box-v2ray-client/id6446814690"),
            ],
            "deeplink_template": "v2box://install-sub?url={sub}",
        },
        {
            "name": "Streisand",
            "stores": [
                ("App Store", "https://apps.apple.com/us/app/streisand/id6450534064"),
            ],
            "deeplink_template": "streisand://import/{sub}",
        },
        {
            "name": "Shadowrocket",
            "stores": [
                ("App Store", "https://apps.apple.com/us/app/shadowrocket/id932747118"),
            ],
            "deeplink_template": "shadowrocket://add/sub://{sub}",
        },
        {
            "name": "Karing",
            "stores": [
                ("App Store", "https://apps.apple.com/us/app/karing/id6472431552"),
                ("Сайт", "https://karing.app/en/download"),
            ],
            "deeplink_template": "karing://install-config?url={sub}",
        },
    ],
    "android": [
        {
            "name": "Happ",
            "stores": [
                ("Google Play", "https://play.google.com/store/apps/details?id=com.happproxy"),
                ("Сайт", "https://happ.su/"),
            ],
            "deeplink_template": "happ://add/{sub}",
        },
        {
            "name": "v2rayNG",
            "stores": [
                ("Google Play", "https://play.google.com/store/apps/details?id=com.v2ray.ang"),
                ("GitHub", "https://github.com/2dust/v2rayNG/releases"),
            ],
            "deeplink_template": None,
        },
        {
            "name": "Hiddify",
            "stores": [
                ("Google Play", "https://play.google.com/store/apps/details?id=app.hiddify.com"),
                ("GitHub", "https://github.com/hiddify/hiddify-app/releases"),
            ],
            "deeplink_template": "hiddify://install-config?url={sub}",
        },
        {
            "name": "NekoBox",
            "stores": [
                ("GitHub", "https://github.com/MatsuriDayo/NekoBoxForAndroid/releases"),
            ],
            "deeplink_template": None,
        },
        {
            "name": "Karing",
            "stores": [
                ("GitHub", "https://github.com/KaringX/karing/releases"),
                ("Сайт", "https://karing.app/en/download"),
            ],
            "deeplink_template": "karing://install-config?url={sub}",
        },
    ],
    "windows": [
        {
            "name": "Hiddify",
            "stores": [
                ("Сайт", "https://hiddify.com/"),
                ("GitHub", "https://github.com/hiddify/hiddify-app/releases"),
            ],
            "deeplink_template": "hiddify://install-config?url={sub}",
        },
        {
            "name": "v2rayN",
            "stores": [
                ("GitHub", "https://github.com/2dust/v2rayN/releases"),
            ],
            "deeplink_template": None,
        },
        {
            "name": "NekoRay",
            "stores": [
                ("GitHub", "https://github.com/MatsuriDayo/nekoray/releases"),
            ],
            "deeplink_template": None,
        },
        {
            "name": "Karing",
            "stores": [
                ("Сайт", "https://karing.app/en/download"),
                ("GitHub", "https://github.com/KaringX/karing/releases"),
            ],
            "deeplink_template": "karing://install-config?url={sub}",
        },
    ],
    "macos": [
        {
            "name": "Happ",
            "stores": [
                ("App Store", "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215"),
            ],
            "deeplink_template": "happ://add/{sub}",
        },
        {
            "name": "V2Box",
            "stores": [
                ("App Store", "https://apps.apple.com/us/app/v2box-v2ray-client/id6446814690"),
            ],
            "deeplink_template": "v2box://install-sub?url={sub}",
        },
        {
            "name": "Hiddify",
            "stores": [
                ("Сайт", "https://hiddify.com/"),
                ("GitHub", "https://github.com/hiddify/hiddify-app/releases"),
            ],
            "deeplink_template": "hiddify://install-config?url={sub}",
        },
        {
            "name": "FoXray",
            "stores": [
                ("App Store", "https://apps.apple.com/us/app/foxray/id6448898396"),
            ],
            "deeplink_template": None,
        },
        {
            "name": "Karing",
            "stores": [
                ("App Store", "https://apps.apple.com/us/app/karing/id6472431552"),
                ("Сайт", "https://karing.app/en/download"),
            ],
            "deeplink_template": "karing://install-config?url={sub}",
        },
    ],
    "linux": [
        {
            "name": "Hiddify",
            "stores": [
                ("Сайт", "https://hiddify.com/"),
                ("GitHub", "https://github.com/hiddify/hiddify-app/releases"),
            ],
            "deeplink_template": "hiddify://install-config?url={sub}",
        },
        {
            "name": "NekoRay",
            "stores": [
                ("GitHub", "https://github.com/MatsuriDayo/nekoray/releases"),
            ],
            "deeplink_template": None,
        },
        {
            "name": "Karing",
            "stores": [
                ("Сайт", "https://karing.app/en/download"),
                ("GitHub", "https://github.com/KaringX/karing/releases"),
            ],
            "deeplink_template": "karing://install-config?url={sub}",
        },
    ],
}

PLATFORM_TITLES = {
    "ios": "📱 iOS (iPhone/iPad)",
    "android": "🤖 Android",
    "windows": "🪟 Windows",
    "macos": "🍎 macOS",
    "linux": "🐧 Linux",
}


def connect_platform_keyboard(sub_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=PLATFORM_TITLES["ios"], callback_data=f"connect_p:{sub_id}:ios")],
            [InlineKeyboardButton(text=PLATFORM_TITLES["android"], callback_data=f"connect_p:{sub_id}:android")],
            [InlineKeyboardButton(text=PLATFORM_TITLES["windows"], callback_data=f"connect_p:{sub_id}:windows")],
            [InlineKeyboardButton(text=PLATFORM_TITLES["macos"], callback_data=f"connect_p:{sub_id}:macos")],
            [InlineKeyboardButton(text=PLATFORM_TITLES["linux"], callback_data=f"connect_p:{sub_id}:linux")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="connect")],
        ]
    )


async def _show_connect_platform_menu(callback: CallbackQuery, sub_id: int) -> None:
    text = (
        f"📥 <b>Подключение подписки #{sub_id}</b>\n\n"
        "Выберите платформу — пришлю инструкцию, ссылки на клиенты, "
        "deep-link для импорта одной кнопкой и QR-код."
    )
    await safe_edit(
        callback, text, parse_mode="HTML",
        reply_markup=connect_platform_keyboard(sub_id), prefer_edit=True,
    )
    await callback.answer()


@dp.callback_query(F.data == "connect")
async def cb_connect(callback: CallbackQuery):
    if not (await auth.is_admin(callback.from_user.id) or await auth.is_authorized(callback.from_user.id)):
        await callback.answer("Доступ только по приглашению.", show_alert=True)
        return
    subs = await db.list_subscriptions(callback.from_user.id)
    if not subs:
        await safe_edit(
            callback,
            "📥 У вас пока нет активных подписок. Активируйте токен через /redeem.",
            parse_mode="HTML",
            reply_markup=back_only_keyboard(),
            prefer_edit=True,
        )
        await callback.answer()
        return
    if len(subs) == 1:
        await _show_connect_platform_menu(callback, subs[0][0])
        return
    rows = []
    for sub in subs:
        cap = _format_sub_caption(sub)[:60]
        rows.append([InlineKeyboardButton(text=cap, callback_data=f"connect_s:{sub[0]}")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")])
    await safe_edit(
        callback,
        "📥 <b>Подключение</b>\n\nВыберите подписку:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        prefer_edit=True,
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("connect_s:"))
async def cb_connect_pick_sub(callback: CallbackQuery):
    sub_id = int(callback.data.split(":", 1)[1])
    sub = await _ensure_sub_belongs_to_user(callback, sub_id)
    if not sub:
        return
    await _show_connect_platform_menu(callback, sub_id)


@dp.callback_query(F.data.startswith("sub:conn:"))
async def cb_sub_connect(callback: CallbackQuery):
    sub_id = int(callback.data.split(":", 2)[2])
    sub = await _ensure_sub_belongs_to_user(callback, sub_id)
    if not sub:
        return
    await _show_connect_platform_menu(callback, sub_id)


@dp.callback_query(F.data.startswith("connect_p:"))
async def cb_connect_platform(callback: CallbackQuery):
    if not (await auth.is_admin(callback.from_user.id) or await auth.is_authorized(callback.from_user.id)):
        await callback.answer("Доступ только по приглашению.", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    try:
        sub_id = int(parts[1])
    except ValueError:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    platform = parts[2]
    if platform not in CLIENT_CATALOG:
        await callback.answer("Неизвестная платформа.", show_alert=True)
        return

    sub = await _ensure_sub_belongs_to_user(callback, sub_id)
    if not sub:
        return
    short_uuid = sub[3]
    sub_url = f"{SUB_DOMAIN}/{short_uuid}" if short_uuid else ""

    title = PLATFORM_TITLES[platform]
    clients = CLIENT_CATALOG[platform]
    lines = [f"<b>{title}</b>", ""]

    if sub_url:
        lines.append("Ваша ссылка-подписка:")
        lines.append(f"<code>{html.escape(sub_url)}</code>")
        lines.append("")
    else:
        lines.append(
            "<i>У вас пока нет аккаунта в панели — ссылка появится после активации токена.</i>\n"
        )

    copy_buttons: list[list[InlineKeyboardButton]] = []
    for c in clients:
        lines.append(f"<b>• {html.escape(c['name'])}</b>")
        for label, url in c["stores"]:
            lines.append(f"  · <a href=\"{html.escape(url)}\">{html.escape(label)}</a>")
        if sub_url and c.get("deeplink_template"):
            deep = c["deeplink_template"].replace("{sub}", sub_url)
            # `<code>...</code>` long-press копируется во всех клиентах Telegram.
            lines.append(f"  · Импорт-ссылка: <code>{html.escape(deep)}</code>")
            # Дополнительно — кнопка copy_text (Bot API 7.10+) для тапа в один клик.
            copy_buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"📋 Скопировать импорт {c['name']}",
                        copy_text=CopyTextButton(text=deep),
                    )
                ]
            )
        lines.append("")

    lines.append(
        "📌 <b>Как использовать импорт-ссылку</b>:\n"
        "1) Тапните на кнопку «📋 Скопировать импорт …» ниже — ссылка попадёт в буфер обмена.\n"
        "2) Откройте установленный клиент — он автоматически предложит добавить подписку, "
        "либо вручную: «Добавить подписку» / «Add subscription» / «+» → вставьте.\n"
        "📷 QR-код подписки — следующим сообщением (если есть аккаунт)."
    )

    kb_rows: list[list[InlineKeyboardButton]] = list(copy_buttons)
    kb_rows.append([InlineKeyboardButton(text="◀️ К платформам", callback_data=f"sub:conn:{sub_id}")])
    kb_rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_main")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    await callback.message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb,
        disable_web_page_preview=True,
    )

    if sub_url:
        try:
            img = qrcode.make(sub_url)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            await callback.message.answer_photo(
                photo=BufferedInputFile(buf.read(), filename="subscription.png"),
                caption="QR-код подписки. Отсканируйте в выбранном клиенте.",
            )
        except Exception as exc:
            logger.warning("QR generation failed: %s", exc)

    await callback.answer()


@dp.callback_query(F.data == "back_main")
async def cb_back_main(callback: CallbackQuery):
    tg_id = callback.from_user.id
    if await auth.is_admin(tg_id):
        has_account = bool(await db.get_user(tg_id))
        kb = main_keyboard_admin(tg_id, has_account)
        text = "🛠 Админская панель"
    elif await auth.is_authorized(tg_id):
        kb = main_keyboard_user()
        text = (
            f"Привет, {html.escape(callback.from_user.first_name or '')}! "
            "Это бот для управления вашим VPN/Proxy.\nВыберите нужное действие ниже:"
        )
    else:
        await callback.answer("Доступ только по приглашению.", show_alert=True)
        return
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


# --- Admin actions on a specific target user (admu:<tg>:...) ---

def _parse_admu(data: str) -> Optional[tuple[int, Optional[int], str, Optional[str]]]:
    """Парсит admu callback. Возвращает (tg, sub_id, action, arg).

    Форматы:
      admu:<tg>:<action>[:<arg>]            — действие по «текущей» подписке (legacy)
      admu:<tg>:s:<sub_id>:<action>[:<arg>] — действие по конкретной подписке
    """
    parts = data.split(":")
    if len(parts) < 3 or parts[0] != "admu":
        return None
    try:
        tg = int(parts[1])
    except ValueError:
        return None
    if parts[2] == "s":
        if len(parts) < 5:
            return None
        try:
            sub_id = int(parts[3])
        except ValueError:
            return None
        action = parts[4]
        arg = parts[5] if len(parts) >= 6 else None
        return tg, sub_id, action, arg
    action = parts[2]
    arg = parts[3] if len(parts) >= 4 else None
    return tg, None, action, arg


async def _admin_target(callback: CallbackQuery, target_tg: int) -> Optional[tuple]:
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return None
    user_data = await db.get_user(target_tg)
    if not user_data or not user_data[1]:
        await callback.answer("У этого пользователя нет аккаунта в панели.", show_alert=True)
        return None
    return user_data


async def _send_admin_user_card(callback: CallbackQuery, target_tg: int, *, prefer_edit: bool) -> None:
    full = await db.get_user_full(target_tg)
    if not full:
        await safe_edit(
            callback,
            "Пользователь не найден.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="◀️ К списку", callback_data="admin_users:0")]]
            ),
            prefer_edit=prefer_edit,
        )
        return
    (
        _tg,
        full_uuid,
        short_uuid,
        username,
        expire_date,
        role,
        tg_username,
        tg_first_name,
        tg_last_name,
    ) = full
    role = role or db.ROLE_USER
    expire_str = (
        datetime.fromtimestamp(int(expire_date)).strftime("%d.%m.%Y %H:%M")
        if expire_date else "—"
    )
    sub_url = f"{SUB_DOMAIN}/{short_uuid}" if short_uuid else "—"
    tg_name = format_tg_name(tg_username, tg_first_name, tg_last_name)
    has_account = bool(full_uuid)
    text = (
        f"👤 <b>Пользователь</b>\n\n"
        f"<b>tg_id:</b> <code>{target_tg}</code>\n"
        f"<b>имя в TG:</b> {html.escape(tg_name)}\n"
        f"<b>panel username:</b> {html.escape(username or '—')}\n"
        f"<b>роль:</b> {html.escape(role)}\n"
        f"<b>подписка до:</b> {html.escape(expire_str)}\n"
        f"<b>ссылка:</b> <code>{html.escape(sub_url)}</code>\n"
    )
    if not has_account:
        text += "\n⚠️ У пользователя нет аккаунта в Remnawave (токен не активирован)."
    subs = await db.list_subscriptions(target_tg)
    if subs:
        text += f"\n\n<b>Подписок:</b> {len(subs)}"
    rows = []
    for sub in subs:
        cap = _format_sub_caption(sub)[:55]
        rows.append([InlineKeyboardButton(
            text=f"📅 {cap}",
            callback_data=f"admu:{target_tg}:s:{sub[0]}:open",
        )])
    rows.append([InlineKeyboardButton(text="✉️ Написать", callback_data=f"admu:{target_tg}:dm")])
    rows.append([InlineKeyboardButton(text="🗑 Удалить пользователя", callback_data=f"admu:{target_tg}:del")])
    rows.append([InlineKeyboardButton(text="◀️ К списку", callback_data="admin_users:0")])
    rows.append([InlineKeyboardButton(text="🛠 В админ-панель", callback_data="admin_panel")])
    await safe_edit(
        callback,
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        prefer_edit=prefer_edit,
    )


async def _send_admin_subscription(callback: CallbackQuery, target_tg: int, *, prefer_edit: bool) -> None:
    user_data = await _admin_target(callback, target_tg)
    if not user_data:
        return
    full_uuid = user_data[1]
    text = await load_subscription_text(full_uuid)
    text += f"\n\nЦель: <code>tg_id={target_tg}</code>"
    await safe_edit(
        callback,
        text,
        parse_mode="HTML",
        reply_markup=subscription_admin_keyboard(target_tg),
        prefer_edit=prefer_edit,
    )


async def _send_admin_devices(callback: CallbackQuery, target_tg: int, *, prefer_edit: bool) -> None:
    user_data = await _admin_target(callback, target_tg)
    if not user_data:
        return
    full_uuid = user_data[1]
    text, devices, show_limits = await load_devices_text(full_uuid)
    text += f"\n\nЦель: <code>tg_id={target_tg}</code>"
    await safe_edit(
        callback,
        text,
        parse_mode="HTML",
        reply_markup=devices_admin_keyboard(target_tg, len(devices), show_limits),
        prefer_edit=prefer_edit,
    )


def _admin_sub_keyboard(target_tg: int, sub_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="+7 дней", callback_data=f"admu:{target_tg}:s:{sub_id}:ext:7"),
                InlineKeyboardButton(text="+30 дней", callback_data=f"admu:{target_tg}:s:{sub_id}:ext:30"),
            ],
            [InlineKeyboardButton(text="📱 Устройства", callback_data=f"admu:{target_tg}:s:{sub_id}:dev")],
            [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"admu:{target_tg}:s:{sub_id}:open")],
            [InlineKeyboardButton(text="🗑 Удалить эту подписку", callback_data=f"admu:{target_tg}:s:{sub_id}:del")],
            [InlineKeyboardButton(text="◀️ К пользователю", callback_data=f"admu:{target_tg}:open")],
        ]
    )


def _admin_sub_devices_keyboard(target_tg: int, sub_id: int, devices_count: int, show_limits: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(devices_count):
        rows.append([InlineKeyboardButton(
            text=f"🗑 Удалить #{i + 1}",
            callback_data=f"admu:{target_tg}:s:{sub_id}:hw_rm:{i}",
        )])
    rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data=f"admu:{target_tg}:s:{sub_id}:dev")])
    if show_limits:
        rows.append([
            InlineKeyboardButton(text="➕ +1", callback_data=f"admu:{target_tg}:s:{sub_id}:hw_lim:1"),
            InlineKeyboardButton(text="➕ +3", callback_data=f"admu:{target_tg}:s:{sub_id}:hw_lim:3"),
        ])
        rows.append([InlineKeyboardButton(text="♾ Без лимита", callback_data=f"admu:{target_tg}:s:{sub_id}:hw_lim:inf")])
    rows.append([InlineKeyboardButton(text="◀️ К подписке", callback_data=f"admu:{target_tg}:s:{sub_id}:open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _send_admin_sub_open(callback: CallbackQuery, target_tg: int, sub_id: int, *, prefer_edit: bool) -> None:
    sub = await db.get_subscription(sub_id)
    if not sub or sub[1] != target_tg:
        await callback.answer("Подписка не найдена у этого пользователя.", show_alert=True)
        return
    full_uuid = sub[2]
    text = await load_subscription_text(full_uuid)
    expire_str = (
        datetime.fromtimestamp(int(sub[5])).strftime("%d.%m.%Y %H:%M")
        if sub[5] else "—"
    )
    text = (
        f"📅 <b>Подписка #{sub_id}</b> пользователя <code>{target_tg}</code>\n"
        f"<b>panel username:</b> {html.escape(sub[4] or '—')}\n"
        f"<b>uuid:</b> <code>{html.escape(sub[2])}</code>\n"
        f"<b>действует до:</b> {html.escape(expire_str)}\n\n"
    ) + text
    await safe_edit(
        callback,
        text,
        parse_mode="HTML",
        reply_markup=_admin_sub_keyboard(target_tg, sub_id),
        prefer_edit=prefer_edit,
    )


async def _send_admin_sub_devices(callback: CallbackQuery, target_tg: int, sub_id: int, *, prefer_edit: bool) -> None:
    sub = await db.get_subscription(sub_id)
    if not sub or sub[1] != target_tg:
        await callback.answer("Подписка не найдена у этого пользователя.", show_alert=True)
        return
    full_uuid = sub[2]
    text, devices, show_limits = await load_devices_text(full_uuid)
    text = f"📱 <b>Устройства подписки #{sub_id}</b> · tg=<code>{target_tg}</code>\n\n" + text
    await safe_edit(
        callback,
        text,
        parse_mode="HTML",
        reply_markup=_admin_sub_devices_keyboard(target_tg, sub_id, len(devices), show_limits),
        prefer_edit=prefer_edit,
    )


async def _handle_admu_sub(callback: CallbackQuery, target_tg: int, sub_id: int, action: str, arg: Optional[str]) -> None:
    sub = await db.get_subscription(sub_id)
    if not sub or sub[1] != target_tg:
        await callback.answer("Подписка не найдена у этого пользователя.", show_alert=True)
        return
    full_uuid = sub[2]

    if action == "open":
        await _send_admin_sub_open(callback, target_tg, sub_id, prefer_edit=True)
        await callback.answer()
        return

    if action == "ext":
        try:
            days = int(arg or "")
        except ValueError:
            await callback.answer("Некорректные данные.", show_alert=True)
            return
        ok, _ = await api.extend_user_subscription_days(full_uuid, days)
        if ok:
            await sync_local_expire_from_panel(target_tg, full_uuid)
            await _send_admin_sub_open(callback, target_tg, sub_id, prefer_edit=True)
            await callback.answer(f"Подписка продлена на {days} дн.")
        else:
            await callback.answer("Не удалось продлить подписку.", show_alert=True)
        return

    if action == "dev":
        await _send_admin_sub_devices(callback, target_tg, sub_id, prefer_edit=True)
        await callback.answer()
        return

    if action == "hw_lim":
        info = await api.get_user_info(full_uuid)
        if not info or "response" not in info:
            await callback.answer("Не удалось получить данные с панели.", show_alert=True)
            return
        ad = info["response"]
        if arg == "inf":
            if is_hwid_unlimited(ad):
                await callback.answer("Уже без лимита.", show_alert=True)
                return
            if await api.update_hwid_device_limit(full_uuid, HWID_UNLIMITED_SENTINEL):
                await _send_admin_sub_devices(callback, target_tg, sub_id, prefer_edit=True)
                await callback.answer("Лимит снят")
            else:
                await callback.answer("Не удалось обновить лимит.", show_alert=True)
            return
        if is_hwid_unlimited(ad):
            await callback.answer("Уже без лимита.", show_alert=True)
            return
        try:
            delta = int(arg or "")
        except ValueError:
            await callback.answer("Некорректное действие.", show_alert=True)
            return
        cur = effective_hwid_limit(ad)
        new_val = min(cur + delta, MAX_HWID_INCREMENT_CAP)
        if new_val == cur and cur >= MAX_HWID_INCREMENT_CAP:
            await callback.answer(f"Достигнут верхний порог ({MAX_HWID_INCREMENT_CAP}).", show_alert=True)
            return
        if await api.update_hwid_device_limit(full_uuid, new_val):
            await _send_admin_sub_devices(callback, target_tg, sub_id, prefer_edit=True)
            await callback.answer(f"Лимит: {cur} → {new_val}")
        else:
            await callback.answer("Не удалось обновить лимит.", show_alert=True)
        return

    if action == "hw_rm":
        try:
            idx = int(arg or "")
        except ValueError:
            await callback.answer("Некорректные данные.", show_alert=True)
            return
        hw_raw = await api.get_user_hwid_devices(full_uuid)
        devices: list = []
        if hw_raw and "response" in hw_raw:
            devices = sort_hwid_devices(hw_raw["response"].get("devices") or [])
        if idx < 0 or idx >= len(devices):
            await callback.answer("Устройство не найдено. Обновите список.", show_alert=True)
            return
        hwid = devices[idx].get("hwid")
        if not hwid or not await api.delete_user_hwid_device(full_uuid, hwid):
            await callback.answer("Не удалось удалить устройство.", show_alert=True)
            return
        await _send_admin_sub_devices(callback, target_tg, sub_id, prefer_edit=True)
        await callback.answer("Устройство удалено")
        return

    if action == "del":
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text="❌ Подтвердить удаление",
                    callback_data=f"admu:{target_tg}:s:{sub_id}:del_confirm",
                )],
                [InlineKeyboardButton(text="◀️ Отмена", callback_data=f"admu:{target_tg}:s:{sub_id}:open")],
            ]
        )
        warning = (
            f"⚠️ <b>Удалить подписку #{sub_id}</b> у <code>{target_tg}</code>?\n\n"
            f"Аккаунт <code>{html.escape(sub[4] or '—')}</code> будет удалён из Remnawave-панели.\n"
            "Действие необратимо."
        )
        await safe_edit(callback, warning, parse_mode="HTML", reply_markup=kb, prefer_edit=True)
        await callback.answer()
        return

    if action == "del_confirm":
        panel_ok, _err = await api.delete_user(full_uuid)
        await db.delete_subscription(sub_id)
        msg = (
            f"✅ Подписка #{sub_id} удалена."
            if panel_ok
            else f"⚠️ Запись удалена локально, в Remnawave удалить не вышло (uuid <code>{full_uuid}</code>)."
        )
        await safe_edit(
            callback, msg,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ К пользователю", callback_data=f"admu:{target_tg}:open")]
            ]),
            prefer_edit=True,
        )
        await callback.answer()
        return

    await callback.answer("Неизвестное действие.", show_alert=True)


@dp.callback_query(F.data.startswith("admu:"))
async def cb_admu(callback: CallbackQuery, state: FSMContext):
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    parsed = _parse_admu(callback.data)
    if not parsed:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    target_tg, sub_id, action, arg = parsed

    if sub_id is not None:
        await _handle_admu_sub(callback, target_tg, sub_id, action, arg)
        return

    if action == "open":
        await _send_admin_user_card(callback, target_tg, prefer_edit=True)
        await callback.answer()
        return

    if action == "sub":
        await _send_admin_subscription(callback, target_tg, prefer_edit=True)
        await callback.answer()
        return

    if action == "sub_refresh":
        await _send_admin_subscription(callback, target_tg, prefer_edit=True)
        await callback.answer("Обновлено")
        return

    if action == "sub_ext":
        try:
            days = int(arg or "")
        except ValueError:
            await callback.answer("Некорректные данные.", show_alert=True)
            return
        user_data = await _admin_target(callback, target_tg)
        if not user_data:
            return
        full_uuid = user_data[1]
        ok, _ = await api.extend_user_subscription_days(full_uuid, days)
        if ok:
            await sync_local_expire_from_panel(target_tg, full_uuid)
            await _send_admin_subscription(callback, target_tg, prefer_edit=True)
            await callback.answer(f"Подписка продлена на {days} дн.")
        else:
            await callback.answer("Не удалось продлить подписку.", show_alert=True)
        return

    if action == "dev":
        await _send_admin_devices(callback, target_tg, prefer_edit=True)
        await callback.answer()
        return

    if action == "dev_refresh":
        await _send_admin_devices(callback, target_tg, prefer_edit=True)
        await callback.answer("Обновлено")
        return

    if action == "hw_lim":
        user_data = await _admin_target(callback, target_tg)
        if not user_data:
            return
        full_uuid = user_data[1]
        info = await api.get_user_info(full_uuid)
        if not info or "response" not in info:
            await callback.answer("Не удалось получить данные с панели.", show_alert=True)
            return
        ad = info["response"]
        if arg == "inf":
            if is_hwid_unlimited(ad):
                await callback.answer("Уже без лимита.", show_alert=True)
                return
            ok = await api.update_hwid_device_limit(full_uuid, HWID_UNLIMITED_SENTINEL)
            if ok:
                await _send_admin_devices(callback, target_tg, prefer_edit=True)
                await callback.answer("Лимит снят")
            else:
                await callback.answer("Не удалось обновить лимит.", show_alert=True)
            return
        if is_hwid_unlimited(ad):
            await callback.answer("Уже без лимита.", show_alert=True)
            return
        try:
            delta = int(arg or "")
        except ValueError:
            await callback.answer("Некорректное действие.", show_alert=True)
            return
        cur = effective_hwid_limit(ad)
        new_val = min(cur + delta, MAX_HWID_INCREMENT_CAP)
        if new_val == cur and cur >= MAX_HWID_INCREMENT_CAP:
            await callback.answer(f"Достигнут верхний порог ({MAX_HWID_INCREMENT_CAP}).", show_alert=True)
            return
        ok = await api.update_hwid_device_limit(full_uuid, new_val)
        if ok:
            await _send_admin_devices(callback, target_tg, prefer_edit=True)
            await callback.answer(f"Лимит: {cur} → {new_val}")
        else:
            await callback.answer("Не удалось обновить лимит.", show_alert=True)
        return

    if action == "hw_rm":
        user_data = await _admin_target(callback, target_tg)
        if not user_data:
            return
        full_uuid = user_data[1]
        try:
            idx = int(arg or "")
        except ValueError:
            await callback.answer("Некорректные данные.", show_alert=True)
            return
        hw_raw = await api.get_user_hwid_devices(full_uuid)
        devices: list = []
        if hw_raw and "response" in hw_raw:
            devices = sort_hwid_devices(hw_raw["response"].get("devices") or [])
        if idx < 0 or idx >= len(devices):
            await callback.answer("Устройство не найдено. Обновите список.", show_alert=True)
            return
        hwid = devices[idx].get("hwid")
        if not hwid:
            await callback.answer("Не удалось определить устройство.", show_alert=True)
            return
        ok = await api.delete_user_hwid_device(full_uuid, hwid)
        if not ok:
            await callback.answer("Не удалось удалить устройство.", show_alert=True)
            return
        await _send_admin_devices(callback, target_tg, prefer_edit=True)
        await callback.answer("Устройство удалено")
        return

    if action == "dm":
        # DM разрешён только тем, кто реально есть в БД (хотя бы /start или активация токена).
        if not await db.get_user_full(target_tg):
            await callback.answer(
                "Этого юзера нет в БД (он ни разу не запускал бота).", show_alert=True,
            )
            return
        # переводим админа в FSM ожидания текста сообщения этому юзеру
        await state.set_state(AdminDmStates.waiting_for_text)
        await state.update_data(target_tg=target_tg)
        await callback.message.answer(
            f"✉️ Введите текст сообщения для пользователя <code>{target_tg}</code>. "
            "Отправлю от вашего имени. /cancel — отменить.",
            parse_mode="HTML",
        )
        await callback.answer()
        return

    if action == "del":
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="❌ Подтвердить удаление",
                        callback_data=f"admu:{target_tg}:del_confirm",
                    )
                ],
                [InlineKeyboardButton(text="◀️ Отмена", callback_data=f"admu:{target_tg}:open")],
            ]
        )
        full = await db.get_user_full(target_tg)
        if not full:
            await callback.answer("Пользователь не найден.", show_alert=True)
            return
        warning = (
            f"⚠️ <b>Удалить пользователя</b> <code>{target_tg}</code>?\n\n"
            f"Будет удалён аккаунт <code>{html.escape(full[3] or '—')}</code> "
            "из Remnawave-панели и стёрта запись в локальной БД (вместе с его use-записями промокодов).\n\n"
            "Действие необратимо."
        )
        await safe_edit(
            callback, warning, parse_mode="HTML", reply_markup=kb, prefer_edit=True,
        )
        await callback.answer()
        return

    if action == "del_confirm":
        subs = await db.list_subscriptions(target_tg)
        deleted = 0
        failed = 0
        for sub in subs:
            ok, _ = await api.delete_user(sub[1])
            if ok:
                deleted += 1
            else:
                failed += 1
        await db.delete_user(target_tg)
        msg = (
            f"✅ Пользователь <code>{target_tg}</code> удалён.\n"
            f"Подписок удалено в панели: {deleted}, ошибок: {failed}."
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="◀️ К списку", callback_data="admin_users:0")]]
        )
        await safe_edit(callback, msg, parse_mode="HTML", reply_markup=kb, prefer_edit=True)
        await callback.answer()
        return

    await callback.answer("Неизвестное действие.", show_alert=True)


# --- Admin: DM message to a specific user ---

class AdminDmStates(StatesGroup):
    waiting_for_text = State()


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
        # Допускаем DM только тем, кто реально есть в нашей БД (admin или редеемнул токен).
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


# --- Admin: promocodes ---

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


# --- User: promocode redemption ---

@dp.callback_query(F.data == "promo_input")
async def cb_promo_input(callback: CallbackQuery, state: FSMContext):
    if not (await auth.is_authorized(callback.from_user.id) or await auth.is_admin(callback.from_user.id)):
        await callback.answer("Доступ только по приглашению.", show_alert=True)
        return
    await state.set_state(PromoStates.waiting_for_code)
    await callback.message.answer(
        "🎁 Введите промокод одной строкой. /cancel — отменить.",
    )
    await callback.answer()


async def _peek_promocode(code: str, tg_id: int) -> tuple[str, Optional[int]]:
    """Проверяет валидность кода БЕЗ потребления. Возвращает (status, bonus_days|None)."""
    row = await db.get_promocode(code)
    if not row:
        return db.PROMO_NOT_FOUND, None
    _code, bonus_days, max_uses, used_count, _created_by, _created_at, revoked = row
    if revoked:
        return db.PROMO_REVOKED, None
    if max_uses is not None and used_count >= max_uses:
        return db.PROMO_EXHAUSTED, None
    if await db.has_promocode_use(code, tg_id):
        return db.PROMO_ALREADY_USED, None
    return db.PROMO_OK, int(bonus_days)


async def _apply_promo_to_subscription(
    message: Message, code: str, sub_id: int, bonus_days: int
) -> None:
    sub = await db.get_subscription(sub_id)
    if not sub or sub[1] != message.from_user.id:
        await message.answer("❌ Подписка не найдена.")
        return
    full_uuid = sub[2]
    # Атомарно потребляем код
    status, _ = await db.redeem_promocode(code, message.from_user.id)
    if status != db.PROMO_OK:
        # Кто-то опередил между peek и redeem
        await message.answer("❌ Не удалось активировать промокод (возможно, кто-то опередил). Попробуйте ещё раз.")
        return
    ok, _ = await api.extend_user_subscription_days(full_uuid, int(bonus_days))
    if not ok:
        await message.answer(
            "⚠️ Промокод принят, но не удалось продлить подписку в панели. "
            "Сообщите администратору."
        )
        return
    await sync_local_expire_from_panel(message.from_user.id, full_uuid)
    await message.answer(
        f"🎉 Подписка #{sub_id} продлена на <b>{bonus_days}</b> дн.!",
        parse_mode="HTML",
    )


@dp.message(PromoStates.waiting_for_code)
async def promo_capture(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if text == "/cancel":
        await state.clear()
        await message.answer("Отменено.")
        return
    if not text:
        await message.answer("Промокод не может быть пустым. /cancel чтобы выйти.")
        return
    tg_id = message.from_user.id
    status, bonus_days = await _peek_promocode(text, tg_id)
    if status == db.PROMO_NOT_FOUND:
        await state.clear()
        await message.answer("❌ Такого промокода не существует.")
        return
    if status == db.PROMO_REVOKED:
        await state.clear()
        await message.answer("❌ Промокод отозван.")
        return
    if status == db.PROMO_EXHAUSTED:
        await state.clear()
        await message.answer("❌ Лимит использований промокода исчерпан.")
        return
    if status == db.PROMO_ALREADY_USED:
        await state.clear()
        await message.answer("❌ Вы уже активировали этот промокод.")
        return

    subs = await db.list_subscriptions(tg_id)
    if not subs:
        await state.clear()
        await message.answer(
            "✅ Промокод корректный, но у вас ещё нет аккаунта в панели — обратитесь к администратору."
        )
        return
    if len(subs) == 1:
        await state.clear()
        await _apply_promo_to_subscription(message, text, subs[0][0], int(bonus_days or 0))
        return
    # multi-sub: ask which one to extend
    await state.set_state(PromoStates.waiting_for_sub_pick)
    await state.update_data(code=text, bonus_days=int(bonus_days or 0))
    rows = []
    for sub in subs:
        cap = _format_sub_caption(sub)[:60]
        rows.append([InlineKeyboardButton(text=cap, callback_data=f"promo_pick:{sub[0]}")])
    rows.append([InlineKeyboardButton(text="Отменить", callback_data="promo_cancel")])
    await message.answer(
        f"🎁 Промокод даст +{bonus_days} дн. Выберите подписку для продления:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@dp.callback_query(F.data.startswith("promo_pick:"), PromoStates.waiting_for_sub_pick)
async def cb_promo_pick(callback: CallbackQuery, state: FSMContext):
    sub_id = int(callback.data.split(":", 1)[1])
    data = await state.get_data()
    await state.clear()
    code = data.get("code")
    bonus_days = int(data.get("bonus_days") or 0)
    if not code:
        await callback.answer("Состояние истекло. Введите промокод заново.", show_alert=True)
        return
    sub = await db.get_subscription(sub_id)
    if not sub or sub[1] != callback.from_user.id:
        await callback.answer("Подписка не найдена.", show_alert=True)
        return
    await _apply_promo_to_subscription(callback.message, code, sub_id, bonus_days)
    await callback.answer()


@dp.callback_query(F.data == "promo_cancel", PromoStates.waiting_for_sub_pick)
async def cb_promo_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("Отменено. Промокод не активирован.")
    await callback.answer()


# --- Support contacts ---

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


# --- Main ---

DEFAULT_COMMANDS = [
    BotCommand(command="start", description="Запустить бота / открыть меню"),
    BotCommand(command="redeem", description="Активировать токен доступа"),
]

ADMIN_COMMANDS = [
    BotCommand(command="start", description="Открыть меню"),
    BotCommand(command="admin", description="🛠 Админская панель"),
    BotCommand(command="help_admin", description="📖 Гайд для админа"),
    BotCommand(command="whois", description="🔍 Найти пользователя по tg_id или @username"),
    BotCommand(command="issue_token", description="🔑 Выдать новый токен доступа"),
    BotCommand(command="revoke_token", description="🚫 Отозвать токен по префиксу"),
    BotCommand(command="import_users", description="📥 Импорт юзеров tg_<id> из Remnawave"),
    BotCommand(command="issue_promo", description="🎁 Создать промокод"),
    BotCommand(command="revoke_promo", description="🚫 Отозвать промокод"),
    BotCommand(command="list_promos", description="📋 Список промокодов"),
    BotCommand(command="set_support", description="❓ Задать контакты поддержки"),
    BotCommand(command="dm", description="✉️ Написать пользователю"),
    BotCommand(command="redeem", description="Активировать токен доступа"),
]


async def setup_bot_commands(bot_: Bot, admin_ids: set[int]) -> None:
    try:
        await bot_.set_my_commands(DEFAULT_COMMANDS, scope=BotCommandScopeDefault())
    except Exception as exc:
        logger.warning("set_my_commands(default) failed: %s", exc)
    for admin_id in admin_ids:
        try:
            await bot_.set_my_commands(
                ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id)
            )
        except Exception as exc:
            logger.warning("set_my_commands(admin=%s) failed: %s", admin_id, exc)


async def main():
    await db.init_db()
    if ADMIN_TG_IDS:
        await db.bootstrap_admins(ADMIN_TG_IDS)
        logger.info("Bootstrapped admins: %s", sorted(ADMIN_TG_IDS))
    else:
        logger.warning(
            "ADMIN_TG_IDS не задан — админов нет. Задайте переменную окружения ADMIN_TG_IDS."
        )

    await setup_bot_commands(bot, ADMIN_TG_IDS)

    scheduler = AsyncIOScheduler(timezone=SCHEDULER_TIMEZONE)
    scheduler.add_job(
        check_expiring_subscriptions,
        "cron",
        hour=SCHEDULER_CRON_HOUR,
        minute=SCHEDULER_CRON_MINUTE,
        args=[bot],
    )
    scheduler.start()

    logger.info("Бот запущен...")
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
