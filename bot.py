import asyncio
import html
import logging
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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
    """Меню обычного пользователя — без управления подпиской/устройствами."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⚙️ Мои настройки", callback_data="my_settings")],
            [InlineKeyboardButton(text="📅 Моя подписка", callback_data="my_subscription")],
            [InlineKeyboardButton(text="📱 Мои устройства", callback_data="my_devices")],
            [InlineKeyboardButton(text="📖 Инструкция", callback_data="instructions")],
        ]
    )


def main_keyboard_admin(tg_id: int, has_account: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users:0")],
        [InlineKeyboardButton(text="🔑 Выдать токен", callback_data="admin_issue_token")],
        [InlineKeyboardButton(text="📋 Активные токены", callback_data="admin_tokens")],
    ]
    if has_account:
        rows.append(
            [InlineKeyboardButton(text="⚙️ Мой аккаунт", callback_data=f"admu:{tg_id}:open")]
        )
    rows.append([InlineKeyboardButton(text="📖 Инструкция", callback_data="instructions")])
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
dp = Dispatcher()
api = RemnawaveAPI(base_url=REMNAWAVE_URL, api_token=REMNAWAVE_TOKEN)


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
    await db.update_user_expire(tg_id, int(dt.timestamp()))


async def create_account_for_user(
    tg_id: int,
    *,
    expire_days: int,
    hwid_device_limit: int,
    created_by: Optional[int] = None,
) -> Optional[str]:
    """Создаёт аккаунт в Remnawave, кладёт запись в БД. Возвращает subscriptionUrl."""
    username = f"tg_{tg_id}"
    response = await api.create_user(
        username=username,
        expire_days=expire_days,
        hwid_device_limit=hwid_device_limit,
    )
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

    # Если уже авторизован — не даём активировать второй раз.
    if await auth.is_authorized(tg_id):
        await message.answer(
            "У вас уже есть активный доступ. Откройте меню командой /start.",
        )
        return

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

    await message.answer(
        "✅ Доступ активирован!\n\n"
        f"🔗 Ваша ссылка на подписку:\n<code>{html.escape(sub_url)}</code>\n\n"
        "Скопируйте ссылку и вставьте в приложение.",
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


_TG_USERNAME_RE = re.compile(r"^tg_(\d+)$")


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


# --- Admin panel callbacks ---

PAGE_SIZE = 8


async def _send_admin_users_list(callback: CallbackQuery, page: int, *, prefer_edit: bool) -> None:
    total = await db.count_users()
    if total == 0:
        text = "Список пользователей пуст."
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="◀️ В админ-панель", callback_data="admin_panel")]]
        )
        await safe_edit(callback, text, parse_mode="HTML", reply_markup=kb, prefer_edit=prefer_edit)
        return

    page = max(0, page)
    offset = page * PAGE_SIZE
    rows = await db.list_users(limit=PAGE_SIZE, offset=offset)
    lines = [f"👥 <b>Пользователи</b> (всего: {total})"]
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
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="◀️", callback_data=f"admin_users:{page - 1}"))
    if offset + PAGE_SIZE < total:
        nav_row.append(InlineKeyboardButton(text="▶️", callback_data=f"admin_users:{page + 1}"))
    if nav_row:
        buttons.append(nav_row)
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


@dp.callback_query(F.data == "admin_tokens")
async def cb_admin_tokens(callback: CallbackQuery):
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    rows = await db.list_active_tokens(limit=20)
    if not rows:
        text = "Активных (неиспользованных) токенов нет."
    else:
        lines = ["📋 <b>Активные токены</b> (хэш-префикс · автор · дни · HWID):"]
        for token_hash, created_by, _created_at, expire_days, hwid_device_limit in rows:
            lines.append(
                f"• <code>{token_hash[:12]}…</code> · автор {created_by} · {expire_days} дн. · HWID {hwid_device_limit}"
            )
        lines.append("")
        lines.append("Отозвать: <code>/revoke_token ПРЕФИКС</code>")
        text = "\n".join(lines)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ В админ-панель", callback_data="admin_panel")]]
    )
    await safe_edit(callback, text, parse_mode="HTML", reply_markup=kb, prefer_edit=True)
    await callback.answer()


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


@dp.callback_query(F.data == "instructions")
async def cb_instructions(callback: CallbackQuery):
    text = (
        "**Как настроить прокси:**\n\n"
        "**Для iOS (iPhone):**\n"
        "1. Скачайте приложение Shadowrocket или V2Box.\n"
        "2. Скопируйте вашу ссылку подписки.\n"
        "3. В приложении нажмите '+' и выберите 'Subscribe'. Вставьте ссылку.\n\n"
        "**Для Android:**\n"
        "1. Скачайте v2rayNG.\n"
        "2. Откройте меню (три полоски) -> 'Подписка'. Нажмите '+' и вставьте ссылку.\n"
        "3. Нажмите 'Обновить подписку'."
    )
    await callback.message.answer(text, parse_mode="Markdown", reply_markup=back_only_keyboard())
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

def _parse_admu(data: str) -> Optional[tuple[int, str, Optional[str]]]:
    """admu:<tg>:<action>[:<arg>] -> (tg, action, arg)."""
    parts = data.split(":")
    if len(parts) < 3 or parts[0] != "admu":
        return None
    try:
        tg = int(parts[1])
    except ValueError:
        return None
    action = parts[2]
    arg = parts[3] if len(parts) >= 4 else None
    return tg, action, arg


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
    rows = []
    if has_account:
        rows.append([InlineKeyboardButton(text="📅 Подписка", callback_data=f"admu:{target_tg}:sub")])
        rows.append([InlineKeyboardButton(text="📱 Устройства", callback_data=f"admu:{target_tg}:dev")])
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


@dp.callback_query(F.data.startswith("admu:"))
async def cb_admu(callback: CallbackQuery):
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    parsed = _parse_admu(callback.data)
    if not parsed:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    target_tg, action, arg = parsed

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

    await callback.answer("Неизвестное действие.", show_alert=True)


# --- Main ---

DEFAULT_COMMANDS = [
    BotCommand(command="start", description="Запустить бота / открыть меню"),
    BotCommand(command="redeem", description="Активировать токен доступа"),
]

ADMIN_COMMANDS = [
    BotCommand(command="start", description="Открыть меню"),
    BotCommand(command="admin", description="🛠 Админская панель"),
    BotCommand(command="whois", description="🔍 Найти пользователя по tg_id или @username"),
    BotCommand(command="issue_token", description="🔑 Выдать новый токен доступа"),
    BotCommand(command="revoke_token", description="🚫 Отозвать токен по префиксу"),
    BotCommand(command="import_users", description="📥 Импорт юзеров tg_<id> из Remnawave"),
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
