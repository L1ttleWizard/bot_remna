import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Setup environment variables before imports
os.environ.setdefault("BOT_TOKEN", "123456:AAA-BBB_ccc-fakefakefakefakefakefakefa")
os.environ.setdefault("REMNAWAVE_URL", "https://panel.example.com")
os.environ.setdefault("REMNAWAVE_TOKEN", "test-token")
os.environ.setdefault("SUB_DOMAIN", "https://sub.example.com")


def test_build_invite_link_format():
    """Verify that invite links start with t.me/ instead of https://t.me/."""
    import bot
    link = bot._build_invite_link("my_test_bot", "token123")
    assert link == "t.me/my_test_bot?start=token123"


@pytest.mark.asyncio
async def test_send_admin_user_card_syncs_subscriptions():
    """Verify that _send_admin_user_card calls sync_local_expire_from_panel for each user subscription."""
    import bot
    from aiogram.types import InlineKeyboardMarkup

    target_tg = 12345
    mock_subs = [
        (1, "uuid-A", "short-A", "username-A", 1700000000, "label-A", 1700000000),
        (2, "uuid-B", "short-B", "username-B", 1700000000, "label-B", 1700000000),
    ]
    mock_full = (
        target_tg,
        "uuid-A",
        "short-A",
        "username-A",
        1700000000,
        "user",
        "tg_user",
        "First",
        "Last",
    )

    callback = MagicMock()
    callback.answer = AsyncMock()

    with patch("bot.db.list_subscriptions", AsyncMock(return_value=mock_subs)) as mock_list, \
         patch("bot.sync_local_expire_from_panel", AsyncMock()) as mock_sync, \
         patch("bot.db.get_user_full", AsyncMock(return_value=mock_full)) as mock_get_full, \
         patch("bot.safe_edit", AsyncMock()) as mock_safe_edit:

        await bot._send_admin_user_card(callback, target_tg, prefer_edit=True)

        mock_list.assert_called()
        assert mock_sync.call_count == 2
        mock_sync.assert_any_call(target_tg, "uuid-A")
        mock_sync.assert_any_call(target_tg, "uuid-B")
        mock_get_full.assert_called_once_with(target_tg)
        mock_safe_edit.assert_called_once()


@pytest.mark.asyncio
async def test_cb_admu_sub_create_manual_success():
    """Verify that clicking 'issue subscription manually' creates the user and card is refreshed."""
    import bot
    from aiogram.fsm.context import FSMContext

    target_tg = 12345
    callback = MagicMock()
    callback.data = f"admu:{target_tg}:sub_create_manual"
    callback.answer = AsyncMock()
    callback.from_user.id = 999  # admin

    mock_full = (
        target_tg,
        "uuid-A",
        "short-A",
        "username-A",
        1700000000,
        "user",
        "tg_user",
        "First",
        "Last",
    )

    state = MagicMock(spec=FSMContext)

    with patch("bot.auth.is_admin", AsyncMock(return_value=True)), \
         patch("bot.db.get_user_full", AsyncMock(return_value=mock_full)), \
         patch("bot.create_account_for_user", AsyncMock(return_value="https://sub.example.com/short-new")) as mock_create, \
         patch("bot._send_admin_user_card", AsyncMock()) as mock_send_card:

        await bot.cb_admu(callback, state)

        mock_create.assert_called_once_with(
            tg_id=target_tg,
            expire_days=bot.DEFAULT_TOKEN_EXPIRE_DAYS,
            hwid_device_limit=bot.DEFAULT_TOKEN_HWID_LIMIT,
            created_by=999,
            tg_username="tg_user",
            tg_first_name="First",
        )
        callback.answer.assert_any_call("✅ Подписка успешно создана и привязана.")
        mock_send_card.assert_called_once_with(callback, target_tg, prefer_edit=True)


@pytest.mark.asyncio
async def test_cb_admu_sub_create_manual_failure():
    """Verify error alert is shown when manual subscription creation fails."""
    import bot
    from aiogram.fsm.context import FSMContext

    target_tg = 12345
    callback = MagicMock()
    callback.data = f"admu:{target_tg}:sub_create_manual"
    callback.answer = AsyncMock()
    callback.from_user.id = 999  # admin

    state = MagicMock(spec=FSMContext)

    with patch("bot.auth.is_admin", AsyncMock(return_value=True)), \
         patch("bot.db.get_user_full", AsyncMock(return_value=None)), \
         patch("bot.create_account_for_user", AsyncMock(return_value=None)) as mock_create, \
         patch("bot._send_admin_user_card", AsyncMock()) as mock_send_card:

        await bot.cb_admu(callback, state)

        mock_create.assert_called_once_with(
            tg_id=target_tg,
            expire_days=bot.DEFAULT_TOKEN_EXPIRE_DAYS,
            hwid_device_limit=bot.DEFAULT_TOKEN_HWID_LIMIT,
            created_by=999,
            tg_username=None,
            tg_first_name=None,
        )
        callback.answer.assert_any_call("❌ Не удалось создать подписку в панели.", show_alert=True)
        mock_send_card.assert_called_once_with(callback, target_tg, prefer_edit=True)


@pytest.mark.asyncio
async def test_cb_my_settings_syncs_expiration():
    """Verify that viewing own settings/profile syncs expiration date."""
    import bot

    user_tg = 12345
    mock_user = (user_tg, "uuid-A", "short-A", "username-A", 1700000000)

    callback = MagicMock()
    callback.from_user.id = user_tg
    callback.message = AsyncMock()
    callback.answer = AsyncMock()

    mock_info = {
        "response": {
            "status": "ACTIVE",
            "hwidDeviceLimit": 3,
            "userTraffic": {"usedTrafficBytes": 0, "lifetimeUsedTrafficBytes": 0},
            "trafficLimitBytes": 0,
        }
    }

    with patch("bot._ensure_authorized_user", AsyncMock(return_value=mock_user)), \
         patch("bot.sync_local_expire_from_panel", AsyncMock()) as mock_sync, \
         patch("bot.db.get_user", AsyncMock(return_value=mock_user)), \
         patch("bot.api.get_user_info", AsyncMock(return_value=mock_info)):

        await bot.cb_my_settings(callback)

        mock_sync.assert_called_once_with(user_tg, "uuid-A")
        callback.message.answer.assert_called_once()
        callback.answer.assert_called_once()


@pytest.mark.asyncio
async def test_cb_my_subscription_syncs_expiration():
    """Verify that viewing subscription syncs expiration date."""
    import bot

    user_tg = 12345
    mock_user = (user_tg, "uuid-A", "short-A", "username-A", 1700000000)

    callback = MagicMock()
    callback.from_user.id = user_tg
    callback.message = AsyncMock()
    callback.answer = AsyncMock()

    with patch("bot._ensure_authorized_user", AsyncMock(return_value=mock_user)), \
         patch("bot.sync_local_expire_from_panel", AsyncMock()) as mock_sync, \
         patch("bot.load_subscription_text", AsyncMock(return_value="mock text")), \
         patch("bot.safe_edit", AsyncMock()) as mock_safe:

        await bot.cb_my_subscription(callback)

        mock_sync.assert_called_once_with(user_tg, "uuid-A")
        mock_safe.assert_called_once()
        callback.answer.assert_called_once()
