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
