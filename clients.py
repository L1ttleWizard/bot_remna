"""Каталог VPN/прокси-клиентов по платформам и связанные UI-хелперы.

CLIENT_CATALOG: { platform_id → [ {name, stores=[(store_name, url), ...], deeplink_template} ] }
deeplink_template — шаблон импорта подписки в клиент, `{sub}` подставляется URL подписки.
None означает что подтверждённого deep-link нет — пользователь импортирует руками/QR.
"""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


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


def connect_platform_keyboard(sub_id: int, has_multiple: bool = True) -> InlineKeyboardMarkup:
    """Клавиатура выбора платформы."""
    back_cb = "connect" if has_multiple else "back_main"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=PLATFORM_TITLES["ios"], callback_data=f"connect_p:{sub_id}:ios")],
            [InlineKeyboardButton(text=PLATFORM_TITLES["android"], callback_data=f"connect_p:{sub_id}:android")],
            [InlineKeyboardButton(text=PLATFORM_TITLES["windows"], callback_data=f"connect_p:{sub_id}:windows")],
            [InlineKeyboardButton(text=PLATFORM_TITLES["macos"], callback_data=f"connect_p:{sub_id}:macos")],
            [InlineKeyboardButton(text=PLATFORM_TITLES["linux"], callback_data=f"connect_p:{sub_id}:linux")],
            [InlineKeyboardButton(text="📷 Показать QR-код", callback_data=f"connect_qr:{sub_id}")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data=back_cb)],
        ]
    )


def connect_client_keyboard(
    sub_id: int,
    platform: str,
    client: dict,
    sub_url: str,
    is_primary: bool = True,
) -> InlineKeyboardMarkup:
    """Лаконичная клавиатура для конкретного клиента."""
    from aiogram.types import CopyTextButton
    rows: list[list[InlineKeyboardButton]] = []

    # Кнопки магазинов
    store_buttons = [
        InlineKeyboardButton(text=f"📲 Скачать ({label})", url=url)
        for label, url in client.get("stores", [])
    ]
    if store_buttons:
        rows.append(store_buttons)

    # Кнопка быстрого импорта
    if sub_url and client.get("deeplink_template"):
        deep = client["deeplink_template"].replace("{sub}", sub_url)
        rows.append([
            InlineKeyboardButton(
                text=f"🚀 Скопировать импорт {client['name']}",
                copy_text=CopyTextButton(text=deep),
            )
        ])

    # Кнопка копирования подписки и QR
    row3: list[InlineKeyboardButton] = []
    if sub_url:
        from aiogram.types import CopyTextButton
        row3.append(InlineKeyboardButton(text="📋 Скопировать ссылку", copy_text=CopyTextButton(text=sub_url)))
    row3.append(InlineKeyboardButton(text="📷 QR-код", callback_data=f"connect_qr:{sub_id}"))
    rows.append(row3)

    # Переключение на другие клиенты
    clients_for_platform = CLIENT_CATALOG.get(platform, [])
    if len(clients_for_platform) > 1:
        if is_primary:
            rows.append([InlineKeyboardButton(text="⚙️ Другие приложения", callback_data=f"connect_alt:{sub_id}:{platform}")])
        else:
            primary_name = clients_for_platform[0]["name"]
            rows.append([InlineKeyboardButton(text=f"⭐ Рекомендуемое ({primary_name})", callback_data=f"connect_p:{sub_id}:{platform}")])

    # Навигация
    rows.append([
        InlineKeyboardButton(text="◀️ К платформам", callback_data=f"connect_platforms:{sub_id}"),
        InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_main"),
    ])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def connect_alt_keyboard(sub_id: int, platform: str) -> InlineKeyboardMarkup:
    """Клавиатура выбора альтернативных клиентов для платформы."""
    clients_for_platform = CLIENT_CATALOG.get(platform, [])
    rows: list[list[InlineKeyboardButton]] = []

    # Альтернативные клиенты (пропускаем первый рекомендуемый)
    for idx, c in enumerate(clients_for_platform):
        if idx == 0:
            continue
        rows.append([
            InlineKeyboardButton(
                text=f"📱 {c['name']}",
                callback_data=f"connect_client:{sub_id}:{platform}:{idx}",
            )
        ])

    primary_name = clients_for_platform[0]["name"] if clients_for_platform else "рекомендуемому"
    rows.append([InlineKeyboardButton(text=f"◀️ Назад к {primary_name}", callback_data=f"connect_p:{sub_id}:{platform}")])
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_main")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_connect_client_card(
    platform: str,
    client: dict,
    sub_url: str,
    is_primary: bool = True,
) -> str:
    """Формирует понятную 3-шаговую карточку подключения."""
    import html
    platform_title = PLATFORM_TITLES.get(platform, platform)
    client_name = client.get("name", "Клиент")

    lines = [
        f"<b>{platform_title}</b>",
        "",
        "<b>1. Скачайте приложение:</b>",
        f"Рекомендуем <b>{client_name}</b>" if is_primary else f"Приложение: <b>{client_name}</b>",
        "",
        "<b>2. Добавьте подписку:</b>",
    ]

    if sub_url and client.get("deeplink_template"):
        lines.append(
            f"Нажмите кнопку <b>«🚀 Скопировать импорт {client_name}»</b> ниже "
            "и вставьте в приложение, либо используйте ссылку на подписку."
        )
    else:
        lines.append(
            "Скопируйте ссылку на подписку ниже и вставьте её в приложение "
            "(кнопка <b>+</b> / <b>Add</b>)."
        )

    lines.extend([
        "",
        "<b>3. Включите VPN:</b>",
        "В приложении выберите локацию и нажмите подключиться.",
        "",
        "🔗 <b>Ваша ссылка на подписку:</b>",
        f"<code>{html.escape(sub_url)}</code>" if sub_url else "<i>(будет доступна после активации)</i>",
        "<i>(нажмите на ссылку, чтобы скопировать)</i>",
    ])

    return "\n".join(lines)


def format_qr_caption(sub_url: str) -> str:
    """Формирует подпись к QR-коду подписки."""
    import html
    lines = [
        "📷 <b>QR-код вашей подписки</b>",
        "",
        "Отсканируйте этот QR-код в установленном приложении (Happ, V2Box, Hiddify, v2rayNG и др.) для мгновенного добавления.",
        "",
        f"🔗 <b>Ссылка:</b> <code>{html.escape(sub_url)}</code>",
    ]
    return "\n".join(lines)

