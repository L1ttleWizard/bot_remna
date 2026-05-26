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


@pytest.mark.asyncio
async def test_render_sub_open_no_squads():
    """User sub view should not show squads information or buttons."""
    import bot
    from aiogram.types import InlineKeyboardMarkup

    sub = (1, "user-uuid", "short-uuid", "username", 1700000000, "label", 1700000000)

    # Setup CallbackQuery mock
    callback = MagicMock()
    callback.from_user.id = 12345
    callback.message = AsyncMock()
    callback.answer = AsyncMock()

    with patch("bot.safe_edit", AsyncMock()) as mock_safe_edit:
        await bot._render_sub_open(callback, sub, prefer_edit=True)

        callback.answer.assert_called_once()
        mock_safe_edit.assert_called_once()
        text_arg = mock_safe_edit.call_args[0][1]
        reply_markup_arg = mock_safe_edit.call_args[1]["reply_markup"]

        assert "Сквад" not in text_arg
        assert isinstance(reply_markup_arg, InlineKeyboardMarkup)
        
        # Verify no squad buttons are present
        buttons = reply_markup_arg.inline_keyboard
        squad_buttons = [btn for row in buttons for btn in row if btn.callback_data and "squad" in btn.callback_data]
        assert len(squad_buttons) == 0


@pytest.mark.asyncio
async def test_send_admin_sub_squads():
    """Admin squads list renders with correct circles."""
    import bot
    from aiogram.types import InlineKeyboardMarkup

    mock_squads_data = {
        "response": {
            "internalSquads": [
                {"uuid": "squad-1", "name": "Squad Alpha"},
                {"uuid": "squad-2", "name": "Squad Beta"},
            ]
        }
    }
    mock_user_info = {
        "response": {
            "activeInternalSquads": ["squad-1"]
        }
    }

    sub = (1, 12345, "user-uuid", "short-uuid", "username", 1700000000, "label", 12345, 1700000000)

    callback = MagicMock()
    callback.from_user.id = 999  # admin
    callback.message = AsyncMock()
    callback.answer = AsyncMock()

    with patch("bot.db.get_subscription", AsyncMock(return_value=sub)), \
         patch("bot.api.get_internal_squads", AsyncMock(return_value=mock_squads_data)), \
         patch("bot.api.get_user_info", AsyncMock(return_value=mock_user_info)), \
         patch("bot.safe_edit", AsyncMock()) as mock_safe_edit:

        await bot._send_admin_sub_squads(callback, 12345, 1, prefer_edit=True)

        mock_safe_edit.assert_called_once()
        text_arg = mock_safe_edit.call_args[0][1]
        reply_markup_arg = mock_safe_edit.call_args[1]["reply_markup"]

        assert "<b>Текущий сквад:</b> Squad Alpha" in text_arg
        assert isinstance(reply_markup_arg, InlineKeyboardMarkup)

        buttons = reply_markup_arg.inline_keyboard
        squad_buttons = [btn for row in buttons for btn in row if btn.callback_data and "squad_set" in btn.callback_data]
        assert len(squad_buttons) == 2
        assert squad_buttons[0].text == "🟢 Squad Alpha"
        assert squad_buttons[0].callback_data == "admu:12345:s:1:squad_set:squad-1"
        assert squad_buttons[1].text == "🔴 Squad Beta"
        assert squad_buttons[1].callback_data == "admu:12345:s:1:squad_set:squad-2"


@pytest.mark.asyncio
async def test_handle_admu_sub_squads_routing():
    import bot

    callback = MagicMock()
    callback.from_user.id = 999  # admin
    callback.message = AsyncMock()
    callback.answer = AsyncMock()

    mock_sub = (1, 12345, "user-uuid", "short-uuid", "username", 1700000000, "label", 12345, 1700000000)

    with patch("bot.db.get_subscription", AsyncMock(return_value=mock_sub)), \
         patch("bot._send_admin_sub_squads", AsyncMock()) as mock_send_squads:

        await bot._handle_admu_sub(callback, 12345, 1, "squads", None)

        mock_send_squads.assert_called_once_with(callback, 12345, 1, prefer_edit=True)
        callback.answer.assert_called_once()


@pytest.mark.asyncio
async def test_handle_admu_sub_squad_set_success():
    import bot

    callback = MagicMock()
    callback.from_user.id = 999  # admin
    callback.message = AsyncMock()
    callback.answer = AsyncMock()

    mock_sub = (1, 12345, "user-uuid", "short-uuid", "username", 1700000000, "label", 12345, 1700000000)

    with patch("bot.db.get_subscription", AsyncMock(return_value=mock_sub)), \
         patch("bot.api.patch_user", AsyncMock(return_value=True)) as mock_patch, \
         patch("bot._send_admin_sub_squads", AsyncMock()) as mock_send_squads:

        await bot._handle_admu_sub(callback, 12345, 1, "squad_set", "squad-uuid-abc")

        mock_patch.assert_called_once_with({"uuid": "user-uuid", "activeInternalSquads": ["squad-uuid-abc"]})
        callback.answer.assert_called_once_with("Сквад успешно изменен!")
        mock_send_squads.assert_called_once_with(callback, 12345, 1, prefer_edit=True)


@pytest.mark.asyncio
async def test_handle_admu_sub_squad_set_failure():
    import bot

    callback = MagicMock()
    callback.from_user.id = 999  # admin
    callback.message = AsyncMock()
    callback.answer = AsyncMock()

    mock_sub = (1, 12345, "user-uuid", "short-uuid", "username", 1700000000, "label", 12345, 1700000000)

    with patch("bot.db.get_subscription", AsyncMock(return_value=mock_sub)), \
         patch("bot.api.patch_user", AsyncMock(return_value=False)) as mock_patch, \
         patch("bot._send_admin_sub_squads", AsyncMock()) as mock_send_squads:

        await bot._handle_admu_sub(callback, 12345, 1, "squad_set", "squad-uuid-abc")

        mock_patch.assert_called_once_with({"uuid": "user-uuid", "activeInternalSquads": ["squad-uuid-abc"]})
        callback.answer.assert_called_once_with("Не удалось сменить сквад.", show_alert=True)
        mock_send_squads.assert_called_once_with(callback, 12345, 1, prefer_edit=True)
