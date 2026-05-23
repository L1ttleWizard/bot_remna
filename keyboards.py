"""Все InlineKeyboardMarkup-конструкторы для бота.

Чистые функции — никаких side-effects, никаких запросов к API/БД.
"""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


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
        [
            InlineKeyboardButton(text="🔑 Выдать токен", callback_data="admin_issue_token"),
            InlineKeyboardButton(text="📋 Активные токены", callback_data="admin_tokens"),
        ],
        [
            InlineKeyboardButton(text="🎁 Промокоды", callback_data="admin_promos"),
            InlineKeyboardButton(text="📊 Аналитика", callback_data="admin_stats"),
        ],
        [
            InlineKeyboardButton(text="🌐 Ноды", callback_data="admin_nodes"),
            InlineKeyboardButton(text="❓ Поддержка", callback_data="admin_support"),
        ],
        [
            InlineKeyboardButton(text="📖 Гайд", callback_data="admin_help"),
            InlineKeyboardButton(text="🔔 Уведомления", callback_data="admin_notify_settings"),
        ],
    ]
    if has_account:
        rows.append(
            [InlineKeyboardButton(text="⚙️ Мой аккаунт", callback_data=f"admu:{tg_id}:open")]
        )
    rows.append([InlineKeyboardButton(text="📥 Подключить", callback_data="connect")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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


def user_sub_menu_keyboard(sub_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📈 Аналитика", callback_data=f"sub:info:{sub_id}"),
                InlineKeyboardButton(text="📱 Устройства", callback_data=f"sub:dev:{sub_id}"),
            ],
            [InlineKeyboardButton(text="📥 Подключить", callback_data=f"sub:conn:{sub_id}")],
            [InlineKeyboardButton(text="◀️ К списку подписок", callback_data="my_subs")],
        ]
    )


def admin_sub_keyboard(target_tg: int, sub_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="+7 дней", callback_data=f"admu:{target_tg}:s:{sub_id}:ext:7"),
                InlineKeyboardButton(text="+30 дней", callback_data=f"admu:{target_tg}:s:{sub_id}:ext:30"),
            ],
            [InlineKeyboardButton(text="♾ Без лимита по времени", callback_data=f"admu:{target_tg}:s:{sub_id}:ext_inf")],
            [InlineKeyboardButton(text="📱 Устройства", callback_data=f"admu:{target_tg}:s:{sub_id}:dev")],
            [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"admu:{target_tg}:s:{sub_id}:open")],
            [InlineKeyboardButton(text="🗑 Удалить эту подписку", callback_data=f"admu:{target_tg}:s:{sub_id}:del")],
            [InlineKeyboardButton(text="◀️ К пользователю", callback_data=f"admu:{target_tg}:open")],
        ]
    )


def admin_sub_devices_keyboard(target_tg: int, sub_id: int, devices_count: int, show_limits: bool) -> InlineKeyboardMarkup:
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
