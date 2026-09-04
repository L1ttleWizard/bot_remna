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


def format_days_ru(days: int) -> str:
    """Склонение слова 'день' в зависимости от числа."""
    if 11 <= (days % 100) <= 14:
        return f"{days} дней"
    rem = days % 10
    if rem == 1:
        return f"{days} день"
    elif 2 <= rem <= 4:
        return f"{days} дня"
    return f"{days} дней"


def welcome_trial_keyboard(trial_days: int = 5) -> InlineKeyboardMarkup:
    days_str = format_days_ru(trial_days)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"🎁 Получить тест на {days_str}", callback_data="trial_claim")],
            [
                InlineKeyboardButton(text="🔑 Ввести токен", callback_data="redeem_prompt"),
                InlineKeyboardButton(text="❓ Поддержка", callback_data="support"),
            ],
        ]
    )


def trial_success_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📥 Подключить (Инструкция / QR)", callback_data="connect")],
            [InlineKeyboardButton(text="🏠 В главное меню", callback_data="back_main")],
        ]
    )


def main_keyboard_user() -> InlineKeyboardMarkup:
    """Меню обычного пользователя — read-only, с поддержкой нескольких подписок."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📅 Мои подписки", callback_data="my_subs")],
            [InlineKeyboardButton(text="📥 Подключить", callback_data="connect")],
            [InlineKeyboardButton(text="🎁 Промокод", callback_data="promo_input")],
            [InlineKeyboardButton(text="👥 Рефералы", callback_data="referrals")],
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
            InlineKeyboardButton(text="📀 Ноды", callback_data="admin_nodes"),
            InlineKeyboardButton(text="💳 Биллинг", callback_data="billing:menu"),
        ],
        [
            InlineKeyboardButton(text="♓ Поддержка", callback_data="admin_support"),
        ],
        [
            InlineKeyboardButton(text="📖 Гайд", callback_data="admin_help"),
            InlineKeyboardButton(text="🔔 Уведомления", callback_data="admin_notify_settings"),
        ],
        [
            InlineKeyboardButton(text="🖥 Команды на сервер", callback_data="admin_server_cmds"),
        ],
        [
            InlineKeyboardButton(text="📦 Создать бэкап", callback_data="admin_make_backup"),
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


def user_sub_menu_keyboard(sub_id: int, has_multiple: bool = True) -> InlineKeyboardMarkup:
    back_btn = (
        InlineKeyboardButton(text="◀️ К списку подписок", callback_data="my_subs")
        if has_multiple
        else InlineKeyboardButton(text="◀️ Назад в главное меню", callback_data="back_main")
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📈 Аналитика", callback_data=f"sub:info:{sub_id}"),
                InlineKeyboardButton(text="📱 Устройства", callback_data=f"sub:dev:{sub_id}"),
            ],
            [
                InlineKeyboardButton(text="🌐 Мои сессии", callback_data=f"sub:conn_view:{sub_id}"),
                InlineKeyboardButton(text="📥 Подключить", callback_data=f"sub:conn:{sub_id}"),
            ],
            [back_btn],
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
            [
                InlineKeyboardButton(text="📱 Устройства", callback_data=f"admu:{target_tg}:s:{sub_id}:dev"),
                InlineKeyboardButton(text="🌐 Сессии", callback_data=f"admu:{target_tg}:s:{sub_id}:conn"),
            ],
            [
                InlineKeyboardButton(text="📋 История запросов", callback_data=f"admu:{target_tg}:s:{sub_id}:req_hist"),
                InlineKeyboardButton(text="👥 Сквады", callback_data=f"admu:{target_tg}:s:{sub_id}:squads"),
            ],
            [InlineKeyboardButton(text="🔌 Сбросить сессии (Кик)", callback_data=f"admu:{target_tg}:s:{sub_id}:drop_confirm")],
            [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"admu:{target_tg}:s:{sub_id}:open")],
            [InlineKeyboardButton(text="🗑 Удалить эту подписку", callback_data=f"admu:{target_tg}:s:{sub_id}:del")],
            [InlineKeyboardButton(text="◀️ К пользователю", callback_data=f"admu:{target_tg}:open")],
        ]
    )


def admin_sub_history_keyboard(target_tg: int, sub_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить историю", callback_data=f"admu:{target_tg}:s:{sub_id}:req_hist")],
            [InlineKeyboardButton(text="◀️ К подписке", callback_data=f"admu:{target_tg}:s:{sub_id}:open")],
        ]
    )


def admin_sub_sessions_keyboard(target_tg: int, sub_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔌 Сбросить все сессии (Кик)", callback_data=f"admu:{target_tg}:s:{sub_id}:drop_confirm")],
            [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"admu:{target_tg}:s:{sub_id}:conn")],
            [InlineKeyboardButton(text="◀️ К подписке", callback_data=f"admu:{target_tg}:s:{sub_id}:open")],
        ]
    )


def admin_sub_drop_confirm_keyboard(target_tg: int, sub_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⚠️ Да, сбросить все сессии", callback_data=f"admu:{target_tg}:s:{sub_id}:drop_do")],
            [InlineKeyboardButton(text="◀️ Отмена", callback_data=f"admu:{target_tg}:s:{sub_id}:open")],
        ]
    )


def user_sub_sessions_keyboard(sub_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔌 Сбросить активные сессии", callback_data=f"sub:drop:{sub_id}")],
            [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"sub:conn_view:{sub_id}")],
            [InlineKeyboardButton(text="◀️ К подписке", callback_data=f"sub:open:{sub_id}")],
        ]
    )


def node_metrics_keyboard(node_uuid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить метрики", callback_data=f"nodes:metrics:{node_uuid}")],
            [InlineKeyboardButton(text="◀️ К карточке ноды", callback_data=f"nodes:card:{node_uuid}")],
        ]
    )


def node_sessions_keyboard(node_uuid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"nodes:conn:{node_uuid}")],
            [InlineKeyboardButton(text="◀️ К карточке ноды", callback_data=f"nodes:card:{node_uuid}")],
        ]
    )


def node_drop_confirm_keyboard(node_uuid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⚠️ Да, сбросить все подключения", callback_data=f"nodes:drop_do:{node_uuid}")],
            [InlineKeyboardButton(text="◀️ Отмена", callback_data=f"nodes:card:{node_uuid}")],
        ]
    )


def node_geocheck_keyboard(node_uuid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Повторить гео-тест", callback_data=f"nodes:geocheck:{node_uuid}")],
            [InlineKeyboardButton(text="◀️ К карточке ноды", callback_data=f"nodes:card:{node_uuid}")],
        ]
    )


def node_bw_users_keyboard(node_uuid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"nodes:bw_users:{node_uuid}")],
            [InlineKeyboardButton(text="◀️ К карточке ноды", callback_data=f"nodes:card:{node_uuid}")],
        ]
    )


def admin_stats_digest_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⏱ За 24 часа", callback_data="admin_stats:digest:24h"),
                InlineKeyboardButton(text="📅 За 7 дней", callback_data="admin_stats:digest:7d"),
                InlineKeyboardButton(text="🗓 За 30 дней", callback_data="admin_stats:digest:30d"),
            ],
            [InlineKeyboardButton(text="◀️ К аналитике", callback_data="admin_stats")],
        ]
    )



def admin_sub_squads_keyboard(
    target_tg: int,
    sub_id: int,
    squads: list,
    active_uuids: set,
) -> InlineKeyboardMarkup:
    """Клавиатура управления сквадами подписки (internal squads)."""
    rows: list[list[InlineKeyboardButton]] = []
    for idx, sq in enumerate(squads):
        uuid = sq.get("uuid") or ""
        name = sq.get("name") or "—"
        icon = "🟢" if uuid in active_uuids else "🔴"
        rows.append([InlineKeyboardButton(
            text=f"{icon} {name}",
            callback_data=f"admu:{target_tg}:s:{sub_id}:sq_s:{idx}",
        )])
    rows.append([InlineKeyboardButton(
        text="◀️ К подписке", callback_data=f"admu:{target_tg}:s:{sub_id}:open",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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


def admin_srr_stats_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📋 Последние запросы", callback_data="admin_stats:srr_recent"),
                InlineKeyboardButton(text="⚙️ Правила SRR", callback_data="admin_stats:srr_rules"),
            ],
            [
                InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_stats:srr"),
                InlineKeyboardButton(text="◀️ К аналитике", callback_data="admin_stats"),
            ],
        ]
    )


def admin_srr_recent_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_stats:srr_recent")],
            [InlineKeyboardButton(text="◀️ К статистике SRR", callback_data="admin_stats:srr")],
        ]
    )


def admin_srr_rules_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_stats:srr_rules")],
            [InlineKeyboardButton(text="◀️ К статистике SRR", callback_data="admin_stats:srr")],
        ]
    )

