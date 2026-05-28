"""Connect-flow: выбор подписки → выбор платформы → клиенты + deep-link + QR.

Поддерживает:
- `connect` — точка входа из главного меню (если подписок >1, picker).
- `connect_s:<sub_id>` — выбор конкретной подписки в picker.
- `sub:conn:<sub_id>` — вход из меню конкретной подписки.
- `connect_p:<sub_id>:<platform>` — рендер платформы с клиентами + QR + кнопка copy_text.
"""
import html
import io
import logging

import qrcode
from aiogram import F
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

import auth
import database as db
from app import dp, ensure_sub_belongs_to_user, safe_edit, sync_local_expire_from_panel
from clients import CLIENT_CATALOG, PLATFORM_TITLES, connect_platform_keyboard
from config import SUB_DOMAIN
from formatters import format_sub_caption
from keyboards import back_only_keyboard

logger = logging.getLogger(__name__)


async def _show_connect_platform_menu(callback: CallbackQuery, sub_id: int) -> None:
    text = (
        f"📥 <b>Подключение подписки #{sub_id}</b>\n\n"
        "Выберите платформу — пришлю инструкцию, ссылки на клиенты, "
        "deep-link для импорта одной кнопкой и QR-код."
    )
    subs = await db.list_subscriptions(callback.from_user.id)
    has_multiple = len(subs) > 1

    await safe_edit(
        callback, text, parse_mode="HTML",
        reply_markup=connect_platform_keyboard(sub_id, has_multiple), prefer_edit=True,
    )
    await callback.answer()


@dp.callback_query(F.data == "connect")
async def cb_connect(callback: CallbackQuery):
    if not (await auth.is_admin(callback.from_user.id) or await auth.is_authorized(callback.from_user.id)):
        await callback.answer("Доступ только по приглашению.", show_alert=True)
        return
    
    # Sync expire dates from panel first
    subs = await db.list_subscriptions(callback.from_user.id)
    if subs:
        import asyncio
        await asyncio.gather(
            *(sync_local_expire_from_panel(callback.from_user.id, sub[1]) for sub in subs),
            return_exceptions=True,
        )
        # Refetch fresh subscriptions from DB
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
async def cb_sub_connect(callback: CallbackQuery):
    sub_id = int(callback.data.split(":", 2)[2])
    sub = await ensure_sub_belongs_to_user(callback, sub_id)
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

    sub = await ensure_sub_belongs_to_user(callback, sub_id)
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
