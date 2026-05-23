import asyncio
import html
import logging
import sys
import time
from datetime import datetime
from typing import Optional

from aiogram import F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import auth
import database as db
from app import (
    AdminBroadcastStates,
    AdminDmStates,
    AdminSearchStates,
    PromoStates,
    api,
    bot,
    dp,
    ensure_authorized_user,
    ensure_sub_belongs_to_user,
    safe_edit,
    sync_local_expire_from_panel,
)
from handlers.admin_analytics import ANALYTICS_PERIOD_DAYS, _analytics_date_range
from handlers import (
    admin_add_node,
    admin_dm,
    admin_nodes,
    admin_notifications,
    admin_promos,
    connect,
    inline_search,
    support,
)

# Регистрация хендлеров происходит при импорте — ссылка нужна, чтобы линтеры
# не выкидывали импорт как неиспользуемый.
_REGISTERED_HANDLERS = (
    admin_add_node,
    admin_dm,
    admin_nodes,
    admin_notifications,
    admin_promos,
    connect,
    inline_search,
    support,
)
from config import (
    ADMIN_TG_IDS,
    DEFAULT_TOKEN_EXPIRE_DAYS,
    DEFAULT_TOKEN_HWID_LIMIT,
    LOG_FILE_PATH,
    LOG_LEVEL,
    SCHEDULER_CRON_HOUR,
    SCHEDULER_CRON_MINUTE,
    SCHEDULER_TIMEZONE,
    SUB_DOMAIN,
)
from formatters import (
    DEFAULT_HWID_DEVICE_LIMIT,
    HWID_UNLIMITED_SENTINEL,
    HWID_UNLIMITED_VALUE,
    MAX_HWID_INCREMENT_CAP,
    REMNAWAVE_USERNAME_MAX_LEN,
    TG_USERNAME_RE,
    build_panel_username,
    draw_text_bar_chart,
    effective_hwid_limit,
    format_devices_html,
    format_expire_display,
    format_sub_caption as _format_sub_caption,
    format_tg_name,
    human_bytes,
    hwid_limit_caption,
    is_hwid_unlimited,
    parse_expire_to_ts as _parse_expire_to_ts,
    sort_hwid_devices,
    traffic_summary_markdown,
)
from keyboards import (
    admin_sub_devices_keyboard as _admin_sub_devices_keyboard,
    admin_sub_keyboard as _admin_sub_keyboard,
    back_only_keyboard,
    devices_admin_keyboard,
    devices_user_keyboard,
    main_keyboard_admin,
    main_keyboard_user,
    subscription_admin_keyboard,
    subscription_user_keyboard,
    user_sub_menu_keyboard as _user_sub_menu_keyboard,
)
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
        candidate = candidate[:REMNAWAVE_USERNAME_MAX_LEN]
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
    "• <code>/stats</code> — короткая сводка по аналитике (быстрый текстовый дайджест).\n"
    "• <code>/cancel</code> — выйти из любого ввода (поиск, DM, промо, импорт и т.п.).\n\n"
    "<b>🔘 Кнопки админ-панели</b> (<code>/admin</code>)\n"
    "• <b>👥 Пользователи</b> — список всех юзеров в БД с пагинацией. У каждой записи кнопка-карточка.\n"
    "  В карточке юзера:\n"
    "    · <i>📅 #X · username · до dd.mm.yyyy</i> — открыть конкретную подписку. "
    "В её меню: <b>+7/+30 дней</b>, <b>♾ Без лимита по времени</b> (ставит дату 2099-12-31), "
    "<b>📱 Устройства</b>, <b>🗑 Удалить эту подписку</b>.\n"
    "    · <b>✉️ Написать</b> — отправить ему DM от вашего имени (FSM-ввод).\n"
    "    · <b>➕ Привязать подписку из Remnawave</b> — выбрать существующего юзера в панели "
    "и добавить его как подписку этому tg_id (запись в панели не меняется).\n"
    "    · <b>🗑 Удалить пользователя</b> — снести все подписки в Remnawave + запись в БД (с подтверждением).\n"
    "    · <b>🔎 Поиск</b> в списке — фильтр по подстроке (tg_id, @username, имя, фамилия, panel-username).\n"
    "• <b>🔑 Выдать токен</b> — выпустить новый токен (≡ <code>/issue_token</code> с дефолтами).\n"
    "• <b>📋 Активные токены</b> — список неиспользованных токенов. У каждого:\n"
    "    · <b>✗ Отозвать ХЭШ…</b> — мгновенно пометить токен как отозванный.\n"
    "    · <b>🔄 Обновить</b> — перерисовать список.\n"
    "• <b>🎁 Промокоды</b> — список последних 20 промо. Создание/отзыв через команды.\n"
    "• <b>📊 Аналитика</b> — сводка по БД и панели (юзеры, подписки, статусы, "
    "трафик за всё время). Подразделы:\n"
    "    · <b>📈 Топ по трафику</b> — топ-10 юзеров панели за <b>последние 30 дней</b> "
    "(тянется через bandwidth-stats), плюс lifetime для контекста.\n"
    "    · <b>⏰ Скоро истекают (7 дн)</b> — список подписок, дата + сколько дней осталось.\n"
    "    · <b>🎁 Промокоды</b> — топ кодов по использованиям, сумма выданных бонус-дней.\n"
    "    · <b>🔑 Токены</b> — issued/redeemed/revoked/active с разбивкой по автору.\n"
    "  В карточке любой подписки у админа показан блок 📈 Статистика "
    "(статус, трафик за 30 дн / период / всё время, HWID, последний онлайн).\n"
    "  У юзера в его меню подписки — кнопка <b>📈 Аналитика</b> с теми же данными.\n"
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
            m = TG_USERNAME_RE.match(username)
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

# `_ensure_authorized_user` / `_ensure_sub_belongs_to_user` живут в app.py
# (нужны нескольким хендлер-модулям). Локальные алиасы — для краткости.
_ensure_authorized_user = ensure_authorized_user
_ensure_sub_belongs_to_user = ensure_sub_belongs_to_user



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
    
    # 7-day range for daily traffic chart
    from datetime import timedelta, timezone
    end_dt = datetime.now(timezone.utc).date()
    dates_7 = [(end_dt - timedelta(days=i)).strftime("%d.%m") for i in range(6, -1, -1)]
    start_7_d = (end_dt - timedelta(days=6)).strftime("%Y-%m-%d")
    end_7_d = end_dt.strftime("%Y-%m-%d")
    
    start_d, end_d = _analytics_date_range()
    info_res, period_res, spark_res = await asyncio.gather(
        api.get_user_info(full_uuid),
        api.get_user_usage_range(full_uuid, start_d, end_d),
        api.get_user_sparkline_traffic(full_uuid, start_7_d, end_7_d),
        return_exceptions=True,
    )
    info = info_res if not isinstance(info_res, BaseException) else None
    period_30 = period_res if not isinstance(period_res, BaseException) else None
    spark_7 = spark_res if not isinstance(spark_res, BaseException) else None
    
    status_text = "Активна ✅"
    limit_text = str(DEFAULT_HWID_DEVICE_LIMIT)
    traffic_lines = ""
    last_online_line = ""
    period_30_line = ""
    hwid_count = 0
    if info and "response" in info:
        api_data = info["response"]
        panel_status = api_data.get("status", "ACTIVE")
        if panel_status != "ACTIVE":
            status_text = f"Неактивна ❌ ({panel_status})"
        limit_text = hwid_limit_caption(api_data)
        traffic_lines = "\n\n" + traffic_summary_markdown(api_data)
        hwid_count = len((api_data.get("hwidDevices") or []))
        last_iso = api_data.get("lastOnlineAt") or ""
        if last_iso:
            last_online_line = f"\n**Последний онлайн:** `{format_expire_display(last_iso)}`"
    if period_30 is not None:
        period_30_line = f"\n**За {ANALYTICS_PERIOD_DAYS} дней:** {human_bytes(int(period_30))}"

    chart_lines = ""
    if spark_7:
        chart_lines = "\n\n📊 **Использование за неделю:**\n```\n" + draw_text_bar_chart(spark_7, dates_7) + "\n```"

    text = (
        f"📈 **Аналитика подписки #{sub_id}**\n\n"
        f"**Статус:** {status_text}\n"
        f"**Действует до:** `{expire_date_str}`\n"
        f"**HWID-устройств:** {hwid_count} / {limit_text}"
        f"{last_online_line}"
        f"{period_30_line}"
        f"{traffic_lines}"
        f"{chart_lines}\n\n"
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
    text, devices, _show = await load_devices_text(full_uuid)
    text = f"📱 <b>Устройства подписки #{sub_id}</b>\n\n" + text
    text += "\n\nℹ️ Управление лимитами доступно только администратору."
    
    inline_keyboard = []
    for i, d in enumerate(devices):
        model = d.get("deviceModel") or f"Устройство #{i + 1}"
        inline_keyboard.append([
            InlineKeyboardButton(
                text=f"🗑 Удалить {model}",
                callback_data=f"sub:dev_rm:{sub_id}:{i}"
            )
        ])
    inline_keyboard.append([InlineKeyboardButton(text="🔄 Обновить", callback_data=f"sub:dev:{sub_id}")])
    inline_keyboard.append([InlineKeyboardButton(text="◀️ К подписке", callback_data=f"sub:open:{sub_id}")])
    
    kb = InlineKeyboardMarkup(inline_keyboard=inline_keyboard)
    await safe_edit(callback, text, parse_mode="HTML", reply_markup=kb, prefer_edit=True)
    await callback.answer()


@dp.callback_query(F.data.startswith("sub:dev_rm:"))
async def cb_sub_device_remove(callback: CallbackQuery):
    parts = callback.data.split(":")
    sub_id = int(parts[2])
    device_idx = int(parts[3])
    sub = await _ensure_sub_belongs_to_user(callback, sub_id)
    if not sub:
        return
    full_uuid = sub[2]
    hw_raw = await api.get_user_hwid_devices(full_uuid)
    devices: list = []
    if hw_raw and "response" in hw_raw:
        devices = sort_hwid_devices(hw_raw["response"].get("devices") or [])
    if device_idx < 0 or device_idx >= len(devices):
        await callback.answer("Устройство не найдено. Обновите список.", show_alert=True)
        return
    hwid = devices[device_idx].get("hwid")
    if not hwid:
        await callback.answer("Не удалось определить устройство.", show_alert=True)
        return
    ok = await api.delete_user_hwid_device(full_uuid, hwid)
    if ok:
        await callback.answer("✅ Устройство успешно удалено!")
    else:
        await callback.answer("❌ Не удалось удалить устройство.", show_alert=True)
    await cb_sub_devices(callback)



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
    rows.append([InlineKeyboardButton(
        text="➕ Привязать подписку из Remnawave",
        callback_data=f"admu:{target_tg}:link:0",
    )])
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


LINK_PICKER_PAGE_SIZE = 10


async def _send_admin_link_picker(
    callback: CallbackQuery,
    target_tg: int,
    page: int,
    *,
    prefer_edit: bool,
) -> None:
    """Постраничный пикер для админа: показывает юзеров из Remnawave-панели,
    которые ещё не привязаны как подписка ни к одному tg_id в БД бота. Тап
    по строке открывает шаг подтверждения привязки."""
    page = max(0, int(page))
    panel_page = await api.list_users(
        size=LINK_PICKER_PAGE_SIZE, start=page * LINK_PICKER_PAGE_SIZE,
    )
    if not panel_page or "response" not in panel_page:
        await safe_edit(
            callback,
            "❌ Не удалось получить список пользователей из панели. Попробуйте позже.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="◀️ К пользователю", callback_data=f"admu:{target_tg}:open"),
            ]]),
            prefer_edit=prefer_edit,
        )
        return
    resp = panel_page["response"]
    total = int(resp.get("total") or 0)
    panel_users = resp.get("users") or []

    rows: list[list[InlineKeyboardButton]] = []
    lines = [
        f"➕ <b>Привязать подписку из Remnawave</b> к <code>{target_tg}</code>",
        f"Всего в панели: <b>{total}</b>. Страница {page + 1}.",
        "",
    ]
    available = 0
    for u in panel_users:
        uuid_v = u.get("uuid") or ""
        if not uuid_v:
            continue
        existing = await db.find_subscription_by_uuid(uuid_v)
        username_p = u.get("username") or "—"
        if existing:
            lines.append(f"  · <s>{html.escape(username_p)}</s> — уже у tg_id <code>{existing[1]}</code>")
            continue
        available += 1
        expire_iso = u.get("expireAt") or ""
        when = "—"
        if expire_iso:
            ts = _parse_expire_to_ts(expire_iso)
            if ts:
                when = datetime.fromtimestamp(ts).strftime("%d.%m.%Y")
        label = f"➕ {username_p[:32]} · до {when}"
        rows.append([InlineKeyboardButton(
            text=label[:64],
            callback_data=f"lnk:{target_tg}:{uuid_v}",
        )])

    if not rows and available == 0 and not panel_users:
        lines.append("Пусто.")

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            text="◀️", callback_data=f"admu:{target_tg}:link:{page - 1}",
        ))
    if (page + 1) * LINK_PICKER_PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(
            text="▶️", callback_data=f"admu:{target_tg}:link:{page + 1}",
        ))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="◀️ К пользователю", callback_data=f"admu:{target_tg}:open")])

    await safe_edit(
        callback,
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        prefer_edit=prefer_edit,
    )


@dp.callback_query(F.data.startswith("lnk:"))
async def cb_admu_link_pick(callback: CallbackQuery):
    """lnk:<tg>:<uuid> — шаг подтверждения привязки."""
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    try:
        target_tg = int(parts[1])
    except ValueError:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    uuid_v = parts[2]
    info = await api.get_user_info(uuid_v)
    if not info or "response" not in info:
        await callback.answer("Не удалось получить данные из панели.", show_alert=True)
        return
    panel_user = info["response"]
    panel_username = panel_user.get("username") or "—"
    short_uuid = panel_user.get("shortUuid") or ""
    expire_iso = panel_user.get("expireAt") or ""
    expire_h = format_expire_display(expire_iso) if expire_iso else "—"
    sub_url = f"{SUB_DOMAIN}/{short_uuid}" if short_uuid else "—"

    existing = await db.find_subscription_by_uuid(uuid_v)
    if existing:
        await callback.answer(
            f"Эта подписка уже привязана к tg_id {existing[1]}.", show_alert=True,
        )
        return

    text = (
        "➕ <b>Привязать подписку</b>\n\n"
        f"К tg_id: <code>{target_tg}</code>\n"
        f"Из Remnawave:\n"
        f"  · username: <code>{html.escape(panel_username)}</code>\n"
        f"  · uuid: <code>{html.escape(uuid_v)}</code>\n"
        f"  · до: <b>{html.escape(expire_h)}</b>\n"
        f"  · ссылка: <code>{html.escape(sub_url)}</code>\n\n"
        "Запись в панели не изменится — добавится только связь в БД бота."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✅ Подтвердить привязку",
            callback_data=f"lnkok:{target_tg}:{uuid_v}",
        )],
        [InlineKeyboardButton(
            text="◀️ Назад",
            callback_data=f"admu:{target_tg}:link:0",
        )],
    ])
    await safe_edit(callback, text, parse_mode="HTML", reply_markup=kb, prefer_edit=True)
    await callback.answer()


@dp.callback_query(F.data.startswith("lnkok:"))
async def cb_admu_link_confirm(callback: CallbackQuery):
    """lnkok:<tg>:<uuid> — финальная привязка."""
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    try:
        target_tg = int(parts[1])
    except ValueError:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    uuid_v = parts[2]
    if await db.find_subscription_by_uuid(uuid_v):
        await callback.answer("Эта подписка уже привязана.", show_alert=True)
        await _send_admin_user_card(callback, target_tg, prefer_edit=True)
        return
    info = await api.get_user_info(uuid_v)
    if not info or "response" not in info:
        await callback.answer("Не удалось получить данные из панели.", show_alert=True)
        return
    panel_user = info["response"]
    panel_username = panel_user.get("username") or ""
    short_uuid = panel_user.get("shortUuid") or ""
    expire_ts = _parse_expire_to_ts(panel_user.get("expireAt"))
    # Гарантируем, что у tg-юзера есть запись в users (для FK / отображения).
    if not await db.get_user_full(target_tg):
        await db.add_user(
            tg_id=target_tg,
            uuid="", short_uuid="", username="",
            expire_date=0,
            created_by=callback.from_user.id,
        )
    sub_id = await db.add_subscription(
        target_tg,
        uuid=uuid_v,
        short_uuid=short_uuid,
        username=panel_username,
        expire_date=expire_ts,
        created_by=callback.from_user.id,
    )
    await callback.answer(f"✅ Привязано (sub #{sub_id}).")
    await _send_admin_user_card(callback, target_tg, prefer_edit=True)


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

    # 7-day range for daily traffic chart
    from datetime import timedelta, timezone
    end_dt = datetime.now(timezone.utc).date()
    dates_7 = [(end_dt - timedelta(days=i)).strftime("%d.%m") for i in range(6, -1, -1)]
    start_7_d = (end_dt - timedelta(days=6)).strftime("%Y-%m-%d")
    end_7_d = end_dt.strftime("%Y-%m-%d")

    # Аналитика: статус / трафик / онлайн / HWID + sparkline — параллельно.
    start_d, end_d = _analytics_date_range()
    info_res, period_res, spark_res = await asyncio.gather(
        api.get_user_info(full_uuid),
        api.get_user_usage_range(full_uuid, start_d, end_d),
        api.get_user_sparkline_traffic(full_uuid, start_7_d, end_7_d),
        return_exceptions=True,
    )
    info = info_res if not isinstance(info_res, BaseException) else None
    period_30 = period_res if not isinstance(period_res, BaseException) else None
    spark_7 = spark_res if not isinstance(spark_res, BaseException) else None
    stats_block = ""
    if info and "response" in info:
        ad = info["response"]
        ut = ad.get("userTraffic") or {}
        used = int(ut.get("usedTrafficBytes") or 0)
        life = int(ut.get("lifetimeUsedTrafficBytes") or 0)
        tlim = ad.get("trafficLimitBytes")
        lim_txt = "без лимита"
        if tlim is not None and int(tlim) > 0:
            lim_txt = human_bytes(int(tlim))
        status = ad.get("status") or "—"
        last_iso = ad.get("lastOnlineAt") or ""
        last_h = format_expire_display(last_iso) if last_iso else "—"
        hwid_lim = hwid_limit_caption(ad)
        hwid_count = len((ad.get("hwidDevices") or []))
        period_30_txt = (
            human_bytes(int(period_30)) if period_30 is not None else "—"
        )

        chart_lines = ""
        if spark_7:
            chart_lines = (
                "\n\n📊 <b>Использование за неделю:</b>\n"
                f"<pre>{html.escape(draw_text_bar_chart(spark_7, dates_7))}</pre>"
            )

        stats_block = (
            "\n📈 <b>Статистика</b>\n"
            f"  · Статус: <b>{html.escape(status)}</b>\n"
            f"  · Трафик за {ANALYTICS_PERIOD_DAYS} дн: "
            f"<b>{html.escape(period_30_txt)}</b>\n"
            f"  · Трафик (текущий период): <b>{html.escape(human_bytes(used))}</b> / "
            f"{html.escape(lim_txt)}\n"
            f"  · Трафик (за всё время): <b>{html.escape(human_bytes(life))}</b>\n"
            f"  · HWID-устройств: <b>{hwid_count}</b> / {html.escape(hwid_lim)}\n"
            f"  · Последний онлайн: <b>{html.escape(last_h)}</b>"
            f"{chart_lines}\n"
        )

    text = (
        f"📅 <b>Подписка #{sub_id}</b> пользователя <code>{target_tg}</code>\n"
        f"<b>panel username:</b> {html.escape(sub[4] or '—')}\n"
        f"<b>uuid:</b> <code>{html.escape(sub[2])}</code>\n"
        f"<b>действует до:</b> {html.escape(expire_str)}\n\n"
    ) + text + stats_block
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

    if action == "ext_inf":
        ok, _ = await api.set_user_expire_unlimited(full_uuid)
        if ok:
            await sync_local_expire_from_panel(target_tg, full_uuid)
            await _send_admin_sub_open(callback, target_tg, sub_id, prefer_edit=True)
            await callback.answer("♾ Без лимита по времени")
        else:
            await callback.answer("Не удалось снять лимит времени.", show_alert=True)
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
            if await api.update_hwid_device_limit(full_uuid, HWID_UNLIMITED_VALUE):
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

    if action == "link":
        try:
            page = int(arg or "0")
        except ValueError:
            page = 0
        await _send_admin_link_picker(callback, target_tg, page, prefer_edit=True)
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
            ok = await api.update_hwid_device_limit(full_uuid, HWID_UNLIMITED_VALUE)
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
    BotCommand(command="broadcast", description="📢 Массовая рассылка сообщений"),
    BotCommand(command="stats", description="📊 Аналитика и сводка"),
    BotCommand(command="redeem", description="Активировать токен доступа"),
]


async def setup_bot_commands(bot_, admin_ids: set[int]) -> None:
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
