"""Хендлеры для настройки уведомлений об окончании подписок (для админов)."""
import logging
from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import auth
import database as db
from app import (
    CLIENT_NOTIFY_DAYS_KEY,
    CLIENT_NOTIFY_ENABLED_KEY,
    CLIENT_NOTIFY_TEXT_KEY,
    ADMIN_NOTIFY_DAYS_KEY,
    ADMIN_NOTIFY_ENABLED_KEY,
    ADMIN_NOTIFY_TEXT_KEY,
    AdminNotifyStates,
    dp,
    safe_edit,
)

logger = logging.getLogger(__name__)


async def get_settings_summary() -> tuple[str, InlineKeyboardMarkup]:
    client_enabled = (await db.get_setting(CLIENT_NOTIFY_ENABLED_KEY)) != "0"
    admin_enabled = (await db.get_setting(ADMIN_NOTIFY_ENABLED_KEY)) != "0"
    client_days = (await db.get_setting(CLIENT_NOTIFY_DAYS_KEY)) or "3,1,0"
    admin_days = (await db.get_setting(ADMIN_NOTIFY_DAYS_KEY)) or "3,1,0,-1"
    client_text = await db.get_setting(CLIENT_NOTIFY_TEXT_KEY)
    admin_text = await db.get_setting(ADMIN_NOTIFY_TEXT_KEY)

    client_status = "✅ Включены" if client_enabled else "❌ Выключены"
    admin_status = "✅ Включены" if admin_enabled else "❌ Выключены"

    client_tpl = "✏️ Пользовательский" if client_text else "ℹ️ Стандартный"
    admin_tpl = "✏️ Пользовательский" if admin_text else "ℹ️ Стандартный"

    body = (
        "🔔 <b>Настройка уведомлений об окончании подписок</b>\n\n"
        "👥 <b>Клиентские уведомления:</b>\n"
        f"• Статус: {client_status}\n"
        f"• Дни напоминаний: <code>{client_days}</code>\n"
        f"• Шаблон текста: {client_tpl}\n\n"
        "👑 <b>Сводный дайджест администраторам:</b>\n"
        f"• Статус: {admin_status}\n"
        f"• Дни в дайджесте: <code>{admin_days}</code>\n"
        f"• Шаблон текста: {admin_tpl}\n\n"
        "<i>Поддерживаемые переменные в шаблонах:\n"
        "<code>{sub_id}</code>, <code>{username}</code>, <code>{label}</code>, <code>{days}</code>, <code>{date}</code>,\n"
        "<code>{tg_username}</code>, <code>{tg_first_name}</code>, <code>{tg_last_name}</code>, <code>{full_name}</code>\n"
        "В шаблоне админов также доступна переменная <code>{list}</code>.</i>"
    )

    btn_client_toggle = InlineKeyboardButton(
        text="👥 Клиенты: " + ("Выключить ❌" if client_enabled else "Включить ✅"),
        callback_data="admin_notify_toggle:client",
    )
    btn_admin_toggle = InlineKeyboardButton(
        text="👑 Админы: " + ("Выключить ❌" if admin_enabled else "Включить ✅"),
        callback_data="admin_notify_toggle:admin",
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [btn_client_toggle, btn_admin_toggle],
            [
                InlineKeyboardButton(text="📅 Дни клиентов", callback_data="admin_notify_edit_days:client"),
                InlineKeyboardButton(text="📅 Дни админов", callback_data="admin_notify_edit_days:admin"),
            ],
            [
                InlineKeyboardButton(text="📝 Текст клиентам", callback_data="admin_notify_edit_text:client"),
                InlineKeyboardButton(text="📝 Текст админам", callback_data="admin_notify_edit_text:admin"),
            ],
            [
                InlineKeyboardButton(text="📢 Отправить тест", callback_data="admin_notify_test"),
            ],
            [
                InlineKeyboardButton(text="🛠 В админ-панель", callback_data="admin_panel"),
            ],
        ]
    )
    return body, kb


@dp.callback_query(F.data == "admin_notify_settings")
async def cb_admin_notify_settings(callback: CallbackQuery, state: FSMContext):
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    await state.clear()
    body, kb = await get_settings_summary()
    await safe_edit(callback, body, parse_mode="HTML", reply_markup=kb, prefer_edit=True)
    await callback.answer()


@dp.callback_query(F.data.startswith("admin_notify_toggle:"))
async def cb_admin_notify_toggle(callback: CallbackQuery):
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    target = callback.data.split(":")[1]
    if target == "client":
        cur = (await db.get_setting(CLIENT_NOTIFY_ENABLED_KEY)) != "0"
        await db.set_setting(CLIENT_NOTIFY_ENABLED_KEY, "0" if cur else "1")
    else:
        cur = (await db.get_setting(ADMIN_NOTIFY_ENABLED_KEY)) != "0"
        await db.set_setting(ADMIN_NOTIFY_ENABLED_KEY, "0" if cur else "1")

    body, kb = await get_settings_summary()
    await safe_edit(callback, body, parse_mode="HTML", reply_markup=kb, prefer_edit=True)
    await callback.answer("Настройки изменены.")


@dp.callback_query(F.data.startswith("admin_notify_edit_days:"))
async def cb_admin_notify_edit_days(callback: CallbackQuery, state: FSMContext):
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    target = callback.data.split(":")[1]
    await state.update_data(target=target)
    await state.set_state(AdminNotifyStates.waiting_for_days)

    body = (
        "📅 <b>Настройка дней напоминаний</b>\n\n"
        "Введите через запятую дни до окончания подписки, в которые нужно отправлять уведомления.\n"
        "Например: <code>3, 1, 0</code> (за 3 дня, за 1 день, и в день окончания).\n"
        "Для администраторов можно использовать отрицательные значения, например <code>-1</code> для вчера истекших подписок.\n\n"
        "Для отмены отправьте /cancel."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Отмена", callback_data="admin_notify_settings")],
        ]
    )
    await safe_edit(callback, body, parse_mode="HTML", reply_markup=kb, prefer_edit=True)
    await callback.answer()


@dp.message(AdminNotifyStates.waiting_for_days)
async def process_notify_days(message: Message, state: FSMContext):
    if not await auth.is_admin(message.from_user.id):
        return
    
    text = (message.text or "").strip()
    if text.startswith("/"):
        if text == "/cancel":
            await state.clear()
            body, kb = await get_settings_summary()
            await message.answer(body, parse_mode="HTML", reply_markup=kb)
            return
        await message.answer("Пожалуйста, введите список чисел через запятую или отправьте /cancel.")
        return

    try:
        days = [int(x.strip()) for x in text.split(",") if x.strip()]
        if not days:
            raise ValueError()
    except ValueError:
        await message.answer("⚠️ Неверный формат. Введите числа через запятую (например: 3, 1, 0).")
        return

    data = await state.get_data()
    target = data.get("target")
    days_str = ",".join(str(d) for d in sorted(days, reverse=True))

    if target == "client":
        await db.set_setting(CLIENT_NOTIFY_DAYS_KEY, days_str)
    else:
        await db.set_setting(ADMIN_NOTIFY_DAYS_KEY, days_str)

    await state.clear()
    body, kb = await get_settings_summary()
    await message.answer(f"✅ Дни для {target} успешно сохранены: <code>{days_str}</code>", parse_mode="HTML")
    await message.answer(body, parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data.startswith("admin_notify_edit_text:"))
async def cb_admin_notify_edit_text(callback: CallbackQuery, state: FSMContext):
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    target = callback.data.split(":")[1]
    await state.update_data(target=target)
    await state.set_state(AdminNotifyStates.waiting_for_text)

    current_val = await db.get_setting(CLIENT_NOTIFY_TEXT_KEY if target == "client" else ADMIN_NOTIFY_TEXT_KEY)
    
    body = (
        f"📝 <b>Редактирование текста для {target}</b>\n\n"
        f"Текущий текст:\n<pre>{current_val if current_val else 'используется стандартный'}</pre>\n\n"
        "Отправьте новый HTML-текст шаблона. В тексте можно использовать переменные:\n"
        "<code>{sub_id}</code>, <code>{username}</code>, <code>{label}</code>, <code>{days}</code>, <code>{date}</code>,\n"
        "<code>{tg_username}</code>, <code>{tg_first_name}</code>, <code>{tg_last_name}</code>, <code>{full_name}</code>\n"
    )
    if target == "admin":
        body += "Для админов обязательно используйте переменную <code>{list}</code>, в которую подставится список подписок.\n"

    body += (
        "\nЧтобы сбросить на стандартный текст, отправьте слово <code>дефолт</code>.\n"
        "Для отмены отправьте /cancel."
    )
    
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Отмена", callback_data="admin_notify_settings")],
        ]
    )
    await safe_edit(callback, body, parse_mode="HTML", reply_markup=kb, prefer_edit=True)
    await callback.answer()


@dp.message(AdminNotifyStates.waiting_for_text)
async def process_notify_text(message: Message, state: FSMContext):
    if not await auth.is_admin(message.from_user.id):
        return
    
    text = (message.text or "").strip()
    if text.startswith("/"):
        if text == "/cancel":
            await state.clear()
            body, kb = await get_settings_summary()
            await message.answer(body, parse_mode="HTML", reply_markup=kb)
            return
        await message.answer("Пожалуйста, отправьте текст шаблона или /cancel.")
        return

    data = await state.get_data()
    target = data.get("target")

    if text.lower() == "дефолт":
        key = CLIENT_NOTIFY_TEXT_KEY if target == "client" else ADMIN_NOTIFY_TEXT_KEY
        await db.set_setting(key, None)
        await state.clear()
        body, kb = await get_settings_summary()
        await message.answer("✅ Шаблон сброшен на стандартный.")
        await message.answer(body, parse_mode="HTML", reply_markup=kb)
        return

    # Проверка на наличие {list} в админском шаблоне
    if target == "admin" and "{list}" not in text:
        await message.answer("⚠️ Ошибка: В шаблоне для администраторов должна присутствовать переменная <code>{list}</code> для списка подписок.")
        return

    # Попытка проверить, что разметка HTML корректна (попробовать отправить тестовое сообщение пользователю)
    try:
        test_msg = await message.answer(f"⏳ Проверка разметки...\n\n{text}", parse_mode="HTML")
        await test_msg.delete()
    except Exception as e:
        await message.answer(f"⚠️ Ошибка разметки HTML: <code>{e}</code>. Пожалуйста, исправьте теги.")
        return

    key = CLIENT_NOTIFY_TEXT_KEY if target == "client" else ADMIN_NOTIFY_TEXT_KEY
    await db.set_setting(key, text)

    await state.clear()
    body, kb = await get_settings_summary()
    await message.answer("✅ Шаблон успешно сохранен.")
    await message.answer(body, parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data == "admin_notify_test")
async def cb_admin_notify_test(callback: CallbackQuery):
    if not await auth.is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return

    # Импортируем из scheduler, чтобы избежать дублирования логики
    from scheduler import format_client_notification, build_admin_digest_text
    
    mock_sub = {
        "tg_id": callback.from_user.id,
        "sub_id": 777,
        "username": "test_vpn_user",
        "label": "Мой Телефон",
        "expire_date": int(callback.message.date.timestamp() + 3 * 24 * 3600),
        "days_left": 3,
        "tg_username": callback.from_user.username or "username",
        "tg_first_name": callback.from_user.first_name or "Имя",
        "tg_last_name": callback.from_user.last_name or "Фамилия",
    }

    # Отправим тестовое для клиентов
    try:
        client_tpl = await db.get_setting(CLIENT_NOTIFY_TEXT_KEY)
        client_msg = format_client_notification(
            client_tpl,
            days_left=mock_sub["days_left"],
            sub_id=mock_sub["sub_id"],
            username=mock_sub["username"],
            label=mock_sub["label"],
            expire_date=mock_sub["expire_date"],
            tg_username=mock_sub["tg_username"],
            tg_first_name=mock_sub["tg_first_name"],
            tg_last_name=mock_sub["tg_last_name"],
        )
        await callback.message.answer(
            f"📱 <b>Тестовое уведомление для КЛИЕНТА:</b>\n\n{client_msg}",
            parse_mode="HTML",
        )
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка отправки тестового клиенту: {e}")

    # Отправим тестовый дайджест
    try:
        admin_tpl = await db.get_setting(ADMIN_NOTIFY_TEXT_KEY)
        mock_subs_list = [
            (callback.from_user.id, 777, mock_sub["expire_date"], "user", "Мой Телефон", "test_vpn_user", callback.from_user.username, callback.from_user.first_name, callback.from_user.last_name),
            (callback.from_user.id, 888, int(callback.message.date.timestamp() - 1 * 24 * 3600), "user", "Рабочий ПК", "expired_user", "expired_tg", "Иван", "Петров"),
        ]
        
        formatted_subs = []
        for tg_id, sub_id, exp_date, role, label, username, tg_user, first_n, last_n in mock_subs_list:
            import time
            days_left = (exp_date - int(time.time())) // (24 * 3600)
            formatted_subs.append((sub_id, username, label, tg_user, first_n, last_n, days_left, exp_date))

        digest_msg = build_admin_digest_text(admin_tpl, formatted_subs)
        await callback.message.answer(
            f"👑 <b>Тестовый ДАЙДЖЕСТ для администратора:</b>\n\n{digest_msg}",
            parse_mode="HTML",
        )
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка отправки тестового админу: {e}")

    await callback.answer("Тестовые сообщения отправлены!")
