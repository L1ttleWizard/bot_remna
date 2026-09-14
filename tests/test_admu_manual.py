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
         patch("bot.api.get_user_info", AsyncMock(return_value=mock_info)), \
         patch("bot.safe_edit", AsyncMock()) as mock_safe:

        await bot.cb_my_settings(callback)

        mock_sync.assert_called_once_with(user_tg, "uuid-A")
        mock_safe.assert_called_once()
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


@pytest.mark.asyncio
async def test_admin_link_picker_remnawave_vless_uuid():
    """Verify that _send_admin_link_picker shows users having vlessUuid and shortUuid without uuid."""
    import bot
    from aiogram.types import InlineKeyboardMarkup

    target_tg = 12345
    callback = MagicMock()
    callback.answer = AsyncMock()

    mock_panel_page = {
        "response": {
            "total": 2,
            "users": [
                {
                    "id": 101,
                    "vlessUuid": "11111111-2222-3333-4444-555555555555",
                    "shortUuid": "short_user_1",
                    "username": "user_one",
                    "expireAt": "2026-10-01T00:00:00Z",
                },
                {
                    "id": 102,
                    "vlessUuid": "22222222-3333-4444-5555-666666666666",
                    "shortUuid": "short_user_2",
                    "username": "user_two",
                    "expireAt": "2026-10-02T00:00:00Z",
                },
            ],
        }
    }

    with patch("bot.api.list_users", AsyncMock(return_value=mock_panel_page)), \
         patch("bot.db.find_subscription_by_any", AsyncMock(return_value=None)), \
         patch("bot.safe_edit", AsyncMock()) as mock_safe:

        await bot._send_admin_link_picker(callback, target_tg, page=0, prefer_edit=True)

        mock_safe.assert_called_once()
        args, kwargs = mock_safe.call_args
        text = args[1]
        reply_markup = kwargs.get("reply_markup") or args[2]

        assert "Всего в панели: <b>2</b>" in text
        # Both users should be available in rows
        buttons = [btn for row in reply_markup.inline_keyboard for btn in row]
        callback_datas = [btn.callback_data for btn in buttons]
        assert f"lnk:{target_tg}:short_user_1" in callback_datas
        assert f"lnk:{target_tg}:short_user_2" in callback_datas


@pytest.mark.asyncio
async def test_cb_admu_link_confirm_success():
    """Verify that confirming linking saves vlessUuid and shortUuid to database."""
    import bot

    target_tg = 777888
    callback = MagicMock()
    callback.from_user.id = 999  # admin
    callback.data = f"lnkok:{target_tg}:short_user_1"
    callback.answer = AsyncMock()

    mock_info = {
        "response": {
            "id": 101,
            "vlessUuid": "11111111-2222-3333-4444-555555555555",
            "shortUuid": "short_user_1",
            "username": "user_one",
            "expireAt": "2026-10-01T00:00:00Z",
        }
    }

    with patch("bot.auth.is_admin", AsyncMock(return_value=True)), \
         patch("bot.api.get_user_info", AsyncMock(return_value=mock_info)), \
         patch("bot.db.find_subscription_by_any", AsyncMock(return_value=None)), \
         patch("bot.db.get_user_full", AsyncMock(return_value=None)), \
         patch("bot.db.upsert_tg_profile", AsyncMock()) as mock_upsert, \
         patch("bot.db.add_subscription", AsyncMock(return_value=42)) as mock_add_sub, \
         patch("bot._send_admin_user_card", AsyncMock()) as mock_card:

        await bot.cb_admu_link_confirm(callback)

        mock_upsert.assert_called_once_with(target_tg, tg_username=None, tg_first_name=None, tg_last_name=None)
        mock_add_sub.assert_called_once()
        _, kwargs = mock_add_sub.call_args
        assert kwargs["uuid"] == "11111111-2222-3333-4444-555555555555"
        assert kwargs["short_uuid"] == "short_user_1"
        assert kwargs["username"] == "user_one"
        assert kwargs["created_by"] == 999
        callback.answer.assert_called_once_with("✅ Привязано (sub #42).")
        mock_card.assert_called_once_with(callback, target_tg, prefer_edit=True)

