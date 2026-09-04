"""Connect-flow: выбор подписки → выбор платформы → клиенты + deep-link + QR.

Поддерживает:
- `connect` — точка входа из главного меню (если подписок >1, picker).
- `connect_s:<sub_id>` — выбор конкретной подписки в picker.
- `sub:conn:<sub_id>` / `connect_platforms:<sub_id>` — вход/возврат в меню выбора платформы.
- `connect_p:<sub_id>:<platform>` — рендер рекомендуемого клиента для платформы.
- `connect_client:<sub_id>:<platform>:<idx>` — рендер выбранного альтернативного клиента.
- `connect_alt:<sub_id>:<platform>` — список альтернативных клиентов для платформы.
- `connect_qr:<sub_id>` — показ чистого QR-кода подписки.
"""
import io
import logging

import qrcode
from aiogram import F
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

import auth
import database as db
from app import (
    delete_active_bot_messages,
    dp,
    ensure_sub_belongs_to_user,
    safe_edit,
    sync_local_expire_from_panel,
)
from clients import (
    CLIENT_CATALOG,
    PLATFORM_TITLES,
    connect_alt_keyboard,
    connect_client_keyboard,
    connect_platform_keyboard,
    format_connect_client_card,
    format_qr_caption,
)
from config import SUB_DOMAIN
from formatters import format_sub_caption
from keyboards import back_only_keyboard

logger = logging.getLogger(__name__)


async def _show_connect_platform_menu(callback: CallbackQuery, sub_id: int) -> None:
    text = (
        f"📥 <b>Подключение к VPN</b> (Подписка #{sub_id})\n\n"
        "Выберите устройство, на котором планируете использовать VPN:"
    )
    subs = await db.list_subscriptions(callback.from_user.id)
    has_multiple = len(subs) > 1

    await safe_edit(
        callback,
        text,
        parse_mode="HTML",
        reply_markup=connect_platform_keyboard(sub_id, has_multiple),
        prefer_edit=True,
    )
    await callback.answer()


@dp.callback_query(F.data == "connect")
async def cb_connect(callback: CallbackQuery):
    tg_id = callback.from_user.id

    # Sync expire dates from panel first
    subs = await db.list_subscriptions(tg_id)
    if subs:
        import asyncio
        await asyncio.gather(
            *(sync_local_expire_from_panel(tg_id, sub[1]) for sub in subs),
            return_exceptions=True,
        )
        # Refetch fresh subscriptions from DB
        subs = await db.list_subscriptions(tg_id)

    if not subs:
        from config import DEFAULT_TRIAL_EXPIRE_DAYS, TRIAL_ENABLED_DEFAULT
        already_claimed = await db.has_claimed_trial(tg_id)
        if not already_claimed and TRIAL_ENABLED_DEFAULT:
            text = (
                "📥 <b>У вас пока нет активных подписок.</b>\n\n"
                f"Вы можете получить бесплатный тестовый доступ на {DEFAULT_TRIAL_EXPIRE_DAYS} дня "
                "или активировать токен доступа:"
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"🎁 Получить тест на {DEFAULT_TRIAL_EXPIRE_DAYS} дня", callback_data="trial_claim")],
                [InlineKeyboardButton(text="🔑 Ввести токен", callback_data="redeem_prompt")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")],
            ])
        else:
            text = (
                "📥 <b>У вас пока нет активных подписок.</b>\n\n"
                "Активируйте токен доступа через <code>/redeem</code>."
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔑 Ввести токен", callback_data="redeem_prompt")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")],
            ])

        await safe_edit(
            callback,
            text,
            parse_mode="HTML",
            reply_markup=kb,
            prefer_edit=True,
        )
        await callback.answer()
        return

    if len(subs) == 1:
        await _show_connect_platform_menu(callback, subs[0][0])
        return

    rows = []
    for sub in subs:
        cap = format_sub_caption(sub)[:60]
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
    sub = await ensure_sub_belongs_to_user(callback, sub_id)
    if not sub:
        return
    await _show_connect_platform_menu(callback, sub_id)


@dp.callback_query(F.data.startswith("sub:conn:"))
@dp.callback_query(F.data.startswith("connect_platforms:"))
async def cb_sub_connect(callback: CallbackQuery):
    parts = callback.data.split(":")
    sub_id = int(parts[-1])
    sub = await ensure_sub_belongs_to_user(callback, sub_id)
    if not sub:
        return
    await _show_connect_platform_menu(callback, sub_id)


@dp.callback_query(F.data.startswith("connect_p:"))
async def cb_connect_platform(callback: CallbackQuery):
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

    sub = await ensure_sub_belongs_to_user(callback, sub_id)
    if not sub:
        return
    short_uuid = sub[3]
    sub_url = f"{SUB_DOMAIN}/{short_uuid}" if short_uuid else ""

    primary_client = CLIENT_CATALOG[platform][0]
    text = format_connect_client_card(platform, primary_client, sub_url, is_primary=True)
    kb = connect_client_keyboard(sub_id, platform, primary_client, sub_url, is_primary=True)

    await safe_edit(
        callback,
        text,
        parse_mode="HTML",
        reply_markup=kb,
        prefer_edit=True,
        disable_web_page_preview=True,
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("connect_client:"))
async def cb_connect_client(callback: CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    try:
        sub_id = int(parts[1])
        platform = parts[2]
        client_idx = int(parts[3])
    except ValueError:
        await callback.answer("Некорректные данные.", show_alert=True)
        return

    if platform not in CLIENT_CATALOG or client_idx >= len(CLIENT_CATALOG[platform]):
        await callback.answer("Клиент не найден.", show_alert=True)
        return

    sub = await ensure_sub_belongs_to_user(callback, sub_id)
    if not sub:
        return
    short_uuid = sub[3]
    sub_url = f"{SUB_DOMAIN}/{short_uuid}" if short_uuid else ""

    client = CLIENT_CATALOG[platform][client_idx]
    is_primary = client_idx == 0
    text = format_connect_client_card(platform, client, sub_url, is_primary=is_primary)
    kb = connect_client_keyboard(sub_id, platform, client, sub_url, is_primary=is_primary)

    await safe_edit(
        callback,
        text,
        parse_mode="HTML",
        reply_markup=kb,
        prefer_edit=True,
        disable_web_page_preview=True,
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("connect_alt:"))
async def cb_connect_alt(callback: CallbackQuery):
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

    sub = await ensure_sub_belongs_to_user(callback, sub_id)
    if not sub:
        return

    platform_title = PLATFORM_TITLES.get(platform, platform)
    text = (
        f"📱 <b>Другие приложения для {platform_title}</b>\n\n"
        "Выберите приложение из списка ниже:"
    )
    kb = connect_alt_keyboard(sub_id, platform)

    await safe_edit(
        callback,
        text,
        parse_mode="HTML",
        reply_markup=kb,
        prefer_edit=True,
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("connect_qr:"))
async def cb_connect_qr(callback: CallbackQuery):
    parts = callback.data.split(":")
    try:
        sub_id = int(parts[1])
    except (IndexError, ValueError):
        await callback.answer("Некорректные данные.", show_alert=True)
        return

    sub = await ensure_sub_belongs_to_user(callback, sub_id)
    if not sub:
        return
    short_uuid = sub[3]
    sub_url = f"{SUB_DOMAIN}/{short_uuid}" if short_uuid else ""

    if not sub_url:
        await callback.answer("Ссылка на подписку отсутствует.", show_alert=True)
        return

    tg_id = callback.from_user.id
    try:
        img = qrcode.make(sub_url)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="◀️ К выбору платформы", callback_data=f"connect_platforms:{sub_id}")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_main")],
            ]
        )

        await delete_active_bot_messages(callback.bot, tg_id)
        try:
            await callback.message.delete()
        except Exception:
            pass

        await callback.bot.send_photo(
            chat_id=tg_id,
            photo=BufferedInputFile(buf.read(), filename="subscription.png"),
            caption=format_qr_caption(sub_url),
            parse_mode="HTML",
            reply_markup=kb,
        )
    except Exception as exc:
        logger.error("QR generation failed: %s", exc)
        await callback.answer("Не удалось сгенерировать QR-код.", show_alert=True)
        return

    await callback.answer()

