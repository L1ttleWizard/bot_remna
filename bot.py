import asyncio

import html

import time

from aiogram import Bot, Dispatcher, F

from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from aiogram.filters import CommandStart

from aiogram.exceptions import TelegramBadRequest

from datetime import datetime, timezone

import logging

import sys

from config import (
    BOT_TOKEN,
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

import database as db

from apscheduler.schedulers.asyncio import AsyncIOScheduler

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


def back_only_keyboard() -> InlineKeyboardMarkup:
    """Только «Назад» — для подстраниц (настройки, инструкция и т.д.)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад в главное меню",
                              callback_data="back_main")]
    ])


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


def format_expire_display(iso_str: str | None) -> str:
    if not iso_str:
        return "—"
    s = iso_str.replace("Z", "+00:00") if iso_str.endswith("Z") else iso_str
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


def sort_hwid_devices(devices: list) -> list:

    return sorted(devices or [], key=lambda x: (x.get("createdAt") or ""))


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

        blocks.append(
            f"\n\n<b>{i + 1}.</b> {model}{os_part}\n   Платформа: {pl}")

    return "".join(blocks)


def devices_screen_keyboard(device_count: int, show_limit_buttons: bool) -> InlineKeyboardMarkup:

    rows = []

    for i in range(device_count):

        rows.append(

            [InlineKeyboardButton(
                text=f"🗑 Удалить #{i + 1}", callback_data=f"hw_rm:{i}")]

        )

    rows.append([InlineKeyboardButton(
        text="🔄 Обновить список", callback_data="devices_refresh")])

    if show_limit_buttons:

        rows.append(

            [

                InlineKeyboardButton(text="➕ +1 к лимиту",
                                     callback_data="hw_limit:1"),

                InlineKeyboardButton(text="➕ +3 к лимиту",
                                     callback_data="hw_limit:3"),

            ]

        )

        rows.append([InlineKeyboardButton(
            text="♾ Без лимита устройств", callback_data="hw_limit:inf")])

    rows.append([InlineKeyboardButton(
        text="◀️ Назад в главное меню", callback_data="back_main")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


bot = Bot(token=BOT_TOKEN)

dp = Dispatcher()

api = RemnawaveAPI(base_url=REMNAWAVE_URL, api_token=REMNAWAVE_TOKEN)


async def load_devices_view(full_uuid: str) -> tuple[str, InlineKeyboardMarkup]:
    """Текст (HTML), клавиатура экрана устройств."""

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

    text = format_devices_html(devices, limit_label)

    kb = devices_screen_keyboard(len(devices), show_limit_buttons=show_limits)

    return text, kb


async def send_or_edit_devices_screen(

    callback: CallbackQuery,

    full_uuid: str,

    *,

    prefer_edit: bool,

) -> None:

    text, actions_kb = await load_devices_view(full_uuid)

    markup = actions_kb

    if prefer_edit:

        try:

            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)

            return

        except TelegramBadRequest as e:

            if "message is not modified" in str(e).lower():

                return

            logger.info(
                "edit_text не удался (%s), отправляем новое сообщение", e)

    await callback.message.answer(text, parse_mode="HTML", reply_markup=markup)


def subscription_screen_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="+7 дней", callback_data="sub_extend:7"),
            InlineKeyboardButton(text="+30 дней", callback_data="sub_extend:30"),
        ],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="subscription_refresh")],
        [InlineKeyboardButton(text="◀️ Назад в главное меню", callback_data="back_main")],
    ])


async def load_subscription_view(full_uuid: str) -> tuple[str, InlineKeyboardMarkup]:
    info = await api.get_user_info(full_uuid)
    if not info or "response" not in info:
        return (
            "📅 <b>Управление подпиской</b>\n\nНе удалось загрузить данные с панели.",
            subscription_screen_keyboard(),
        )
    ad = info["response"]
    exp_h = format_expire_display(ad.get("expireAt"))
    text = (
        "📅 <b>Управление подпиской</b>\n\n"
        f"Окончание доступа: <b>{html.escape(exp_h)}</b>\n\n"
        "Нажмите «+7 дней» или «+30 дней» — срок добавится к текущей дате окончания. "
        "Если подписка уже истекла, отсчёт ведётся от сегодняшнего дня."
    )
    return text, subscription_screen_keyboard()


async def send_or_edit_subscription_screen(
    callback: CallbackQuery,
    full_uuid: str,
    *,
    prefer_edit: bool,
) -> None:
    text, kb = await load_subscription_view(full_uuid)
    if prefer_edit:
        try:
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
            return
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                return
            logger.info("subscription edit_text: %s", e)
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)


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


# --- КЛАВИАТУРА ---

def main_keyboard():

    return InlineKeyboardMarkup(inline_keyboard=[

        [InlineKeyboardButton(text="🚀 Получить прокси",
                              callback_data="get_proxy")],

        [InlineKeyboardButton(text="⚙️ Мои настройки",
                              callback_data="my_settings")],

        [InlineKeyboardButton(text="📅 Управление подпиской",
                              callback_data="manage_subscription")],

        [InlineKeyboardButton(text="📱 Управление устройствами",
                              callback_data="manage_devices")],

        [InlineKeyboardButton(text="📖 Инструкция",
                              callback_data="instructions")]

    ])


@dp.message(CommandStart())
async def cmd_start(message: Message):

    await message.answer(

        f"Привет, {message.from_user.first_name}! Это бот для управления вашим VPN/Proxy.\n"

        "Выберите нужное действие ниже:",

        reply_markup=main_keyboard()

    )


@dp.callback_query(F.data == "get_proxy")
async def process_get_proxy(callback: CallbackQuery):

    tg_id = callback.from_user.id

    user_data = await db.get_user(tg_id)

    if user_data:

        await callback.message.answer(

            "У вас уже есть активная подписка! Нажмите 'Мои настройки' для просмотра.",

            reply_markup=main_keyboard(),

        )

        await callback.answer()

        return

    username = f"tg_{tg_id}"

    response = await api.create_user(username=username, expire_days=30, hwid_device_limit=DEFAULT_HWID_DEVICE_LIMIT)

    if response and "response" in response:

        api_data = response["response"]

        sub_url = api_data.get("subscriptionUrl", "")

        full_uuid = api_data.get("uuid", "")

        short_uuid = api_data.get("shortUuid", "")

        expire_time = int(time.time()) + (30 * 24 * 60 * 60)

        await db.add_user(tg_id, full_uuid, short_uuid, username, expire_time)

        await callback.message.answer(

            f"✅ Прокси успешно выдан!\n\n"

            f"🔗 Ваша ссылка на подписку:\n`{sub_url}`\n\n"

            f"Скопируйте эту ссылку и вставьте в приложение.",

            parse_mode="Markdown",

            reply_markup=main_keyboard(),

        )

    else:

        logger.error(f"Неожиданный ответ от панели или ошибка: {response}")

        await callback.message.answer(

            "❌ Ошибка при создании прокси. Обратитесь в поддержку.",

            reply_markup=main_keyboard(),

        )

    await callback.answer()


@dp.callback_query(F.data == "my_settings")
async def process_my_settings(callback: CallbackQuery):

    tg_id = callback.from_user.id

    user_data = await db.get_user(tg_id)

    if not user_data:

        await callback.message.answer(

            "У вас еще нет подписки. Нажмите 'Получить прокси' в главном меню.",

            reply_markup=main_keyboard(),

        )

        await callback.answer()

        return

    full_uuid = user_data[1]

    short_uuid = user_data[2]

    expire_timestamp = user_data[4]

    expire_date_str = datetime.fromtimestamp(
        expire_timestamp).strftime('%d.%m.%Y %H:%M')

    sub_url = f"{SUB_DOMAIN}/{short_uuid}"

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

        f"👤 **Ваш профиль VPN/Proxy**\n\n"

        f"**Статус:** {status_text}\n"

        f"**Лимит устройств (HWID):** {limit_text}\n"

        f"**Подписка до:** `{expire_date_str}`\n"

        f"{traffic_lines}\n\n"

        f"🔗 **Ваша ссылка для подключения:**\n`{sub_url}`\n\n"

        f"*(Скопируйте ссылку и обновите в приложении)*"

    )

    await callback.message.answer(

        text,

        parse_mode="Markdown",

        reply_markup=back_only_keyboard(),

    )

    await callback.answer()


@dp.callback_query(F.data.startswith("hw_limit:"))
async def process_hw_limit(callback: CallbackQuery):

    user_data = await db.get_user(callback.from_user.id)

    if not user_data:

        await callback.answer("Нет подписки.", show_alert=True)

        return

    action = callback.data.split(":", 1)[1]

    full_uuid = user_data[1]

    info = await api.get_user_info(full_uuid)

    if not info or "response" not in info:

        await callback.answer("Не удалось получить данные с панели.", show_alert=True)

        return

    ad = info["response"]

    if action == "inf":

        if is_hwid_unlimited(ad):

            await callback.answer("Уже включён режим без лимита.", show_alert=True)

            return

        ok = await api.update_hwid_device_limit(full_uuid, HWID_UNLIMITED_SENTINEL)

        if ok:

            await send_or_edit_devices_screen(callback, full_uuid, prefer_edit=True)

            await callback.answer("Лимит устройств снят")

        else:

            await callback.answer("Не удалось обновить лимит.", show_alert=True)

        return

    if is_hwid_unlimited(ad):

        await callback.answer("Уже без лимита устройств.", show_alert=True)

        return

    try:

        delta = int(action)

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

        await send_or_edit_devices_screen(callback, full_uuid, prefer_edit=True)

        await callback.answer(f"Лимит: {cur} → {new_val}")

    else:

        await callback.answer("Не удалось обновить лимит.", show_alert=True)


@dp.callback_query(F.data == "manage_devices")
async def process_manage_devices(callback: CallbackQuery):

    user_data = await db.get_user(callback.from_user.id)

    if not user_data:

        await callback.message.answer(

            "У вас еще нет подписки.",

            reply_markup=main_keyboard(),

        )

        await callback.answer()

        return

    full_uuid = user_data[1]

    await send_or_edit_devices_screen(callback, full_uuid, prefer_edit=False)

    await callback.answer()


@dp.callback_query(F.data == "manage_subscription")
async def process_manage_subscription(callback: CallbackQuery):

    user_data = await db.get_user(callback.from_user.id)

    if not user_data:

        await callback.message.answer(

            "У вас еще нет подписки.",

            reply_markup=main_keyboard(),

        )

        await callback.answer()

        return

    full_uuid = user_data[1]

    await send_or_edit_subscription_screen(callback, full_uuid, prefer_edit=False)

    await callback.answer()


@dp.callback_query(F.data == "subscription_refresh")
async def process_subscription_refresh(callback: CallbackQuery):

    user_data = await db.get_user(callback.from_user.id)

    if not user_data:

        await callback.answer("Нет подписки.", show_alert=True)

        return

    await send_or_edit_subscription_screen(callback, user_data[1], prefer_edit=True)

    await callback.answer("Обновлено")


@dp.callback_query(F.data.startswith("sub_extend:"))
async def process_sub_extend(callback: CallbackQuery):

    user_data = await db.get_user(callback.from_user.id)

    if not user_data:

        await callback.answer("Нет подписки.", show_alert=True)

        return

    try:

        days = int(callback.data.split(":", 1)[1])

    except (IndexError, ValueError):

        await callback.answer("Некорректные данные.", show_alert=True)

        return

    tg_id = user_data[0]

    full_uuid = user_data[1]

    ok, _ = await api.extend_user_subscription_days(full_uuid, days)

    if ok:

        await sync_local_expire_from_panel(tg_id, full_uuid)

        await send_or_edit_subscription_screen(callback, full_uuid, prefer_edit=True)

        await callback.answer(f"Подписка продлена на {days} дн.")

    else:

        await callback.answer("Не удалось продлить подписку.", show_alert=True)


@dp.callback_query(F.data == "devices_refresh")
async def process_devices_refresh(callback: CallbackQuery):

    user_data = await db.get_user(callback.from_user.id)

    if not user_data:

        await callback.answer("Нет подписки.", show_alert=True)

        return

    full_uuid = user_data[1]

    await send_or_edit_devices_screen(callback, full_uuid, prefer_edit=True)

    await callback.answer("Список обновлён")


@dp.callback_query(F.data.startswith("hw_rm:"))
async def process_hwid_delete(callback: CallbackQuery):

    user_data = await db.get_user(callback.from_user.id)

    if not user_data:

        await callback.answer("Нет подписки.", show_alert=True)

        return

    try:

        idx = int(callback.data.split(":", 1)[1])

    except (IndexError, ValueError):

        await callback.answer("Некорректные данные.", show_alert=True)

        return

    full_uuid = user_data[1]

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

    await send_or_edit_devices_screen(callback, full_uuid, prefer_edit=True)

    await callback.answer("Устройство удалено")


@dp.callback_query(F.data == "instructions")
async def process_instructions(callback: CallbackQuery):

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
async def process_back_main(callback: CallbackQuery):

    text = (

        f"Привет, {callback.from_user.first_name}! Это бот для управления вашим VPN/Proxy.\n"

        "Выберите нужное действие ниже:"

    )

    try:

        await callback.message.edit_text(text, reply_markup=main_keyboard())

    except TelegramBadRequest:

        await callback.message.answer(text, reply_markup=main_keyboard())

    await callback.answer()


async def main():

    await db.init_db()

    scheduler = AsyncIOScheduler(timezone=SCHEDULER_TIMEZONE)

    scheduler.add_job(

        check_expiring_subscriptions,

        "cron",

        hour=SCHEDULER_CRON_HOUR,

        minute=SCHEDULER_CRON_MINUTE,

        args=[bot],

    )

    scheduler.start()

    print("Бот запущен...")

    try:

        await dp.start_polling(bot)

    finally:

        scheduler.shutdown()


if __name__ == "__main__":

    asyncio.run(main())
