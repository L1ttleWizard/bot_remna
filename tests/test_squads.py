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
async def test_render_sub_open_with_squads():
    import bot
    from aiogram.types import InlineKeyboardMarkup

    # Mock API responses
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

    sub = (1, "user-uuid", "short-uuid", "username", 1700000000, "label", 1700000000)

    # Setup CallbackQuery mock
    callback = MagicMock()
    callback.from_user.id = 12345
    callback.message = AsyncMock()
    callback.answer = AsyncMock()

    with patch("bot.api.get_internal_squads", AsyncMock(return_value=mock_squads_data)) as mock_get_sq, \
         patch("bot.api.get_user_info", AsyncMock(return_value=mock_user_info)) as mock_get_usr, \
         patch("bot.safe_edit", AsyncMock()) as mock_safe_edit:
        
        await bot._render_sub_open(callback, sub, prefer_edit=True)

        mock_get_sq.assert_called_once()
        mock_get_usr.assert_called_once_with("user-uuid")
        callback.answer.assert_called_once()

        # Check safe_edit calls
        mock_safe_edit.assert_called_once()
        call_args = mock_safe_edit.call_args[0]
        text_arg = call_args[1]
        reply_markup_arg = mock_safe_edit.call_args[1]["reply_markup"]

        assert "Squad Alpha" in text_arg
        assert isinstance(reply_markup_arg, InlineKeyboardMarkup)
        
        # Verify squad buttons in reply markup
        buttons = reply_markup_arg.inline_keyboard
        # Find squad buttons (should contain callback starting with sub:squad:)
        squad_buttons = [btn for row in buttons for btn in row if btn.callback_data and btn.callback_data.startswith("sub:squad:")]
        assert len(squad_buttons) == 2
        assert squad_buttons[0].text == "🟢 Squad Alpha"
        assert squad_buttons[0].callback_data == "sub:squad:1:squad-1"
        assert squad_buttons[1].text == "🔴 Squad Beta"
        assert squad_buttons[1].callback_data == "sub:squad:1:squad-2"


@pytest.mark.asyncio
async def test_render_sub_open_api_error_graceful():
    import bot
    from aiogram.types import InlineKeyboardMarkup

    sub = (1, "user-uuid", "short-uuid", "username", 1700000000, "label", 1700000000)

    # Setup CallbackQuery mock
    callback = MagicMock()
    callback.from_user.id = 12345
    callback.message = AsyncMock()
    callback.answer = AsyncMock()

    # api returns exceptions or None
    with patch("bot.api.get_internal_squads", AsyncMock(return_value=Exception("network error"))), \
         patch("bot.api.get_user_info", AsyncMock(return_value=None)), \
         patch("bot.safe_edit", AsyncMock()) as mock_safe_edit:
        
        await bot._render_sub_open(callback, sub, prefer_edit=True)

        mock_safe_edit.assert_called_once()
        text_arg = mock_safe_edit.call_args[0][1]
        reply_markup_arg = mock_safe_edit.call_args[1]["reply_markup"]

        # Text should not mention squads since it failed
        assert "Сквад" not in text_arg
        assert isinstance(reply_markup_arg, InlineKeyboardMarkup)
        
        # Verify no squad buttons are present
        buttons = reply_markup_arg.inline_keyboard
        squad_buttons = [btn for row in buttons for btn in row if btn.callback_data and btn.callback_data.startswith("sub:squad:")]
        assert len(squad_buttons) == 0


@pytest.mark.asyncio
async def test_cb_sub_squad_success():
    import bot

    callback = MagicMock()
    callback.data = "sub:squad:1:squad-uuid-123"
    callback.from_user.id = 12345
    callback.message = AsyncMock()
    callback.answer = AsyncMock()

    mock_sub = (1, 12345, "user-uuid", "short-uuid", "username", 1700000000, "label", 12345, 1700000000)

    with patch("bot._ensure_sub_belongs_to_user", AsyncMock(return_value=mock_sub)) as mock_ensure, \
         patch("bot.api.patch_user", AsyncMock(return_value=True)) as mock_patch, \
         patch("bot._render_sub_open", AsyncMock()) as mock_render:
        
        await bot.cb_sub_squad(callback)

        mock_ensure.assert_called_once_with(callback, 1)
        mock_patch.assert_called_once_with({"uuid": "user-uuid", "activeInternalSquads": ["squad-uuid-123"]})
        callback.answer.assert_called_once_with("Сквад успешно изменен!")
        mock_render.assert_called_once_with(callback, (1, "user-uuid", "short-uuid", "username", 1700000000, "label", 1700000000), prefer_edit=True)


@pytest.mark.asyncio
async def test_cb_sub_squad_failure():
    import bot

    callback = MagicMock()
    callback.data = "sub:squad:1:squad-uuid-123"
    callback.from_user.id = 12345
    callback.message = AsyncMock()
    callback.answer = AsyncMock()

    mock_sub = (1, 12345, "user-uuid", "short-uuid", "username", 1700000000, "label", 12345, 1700000000)

    with patch("bot._ensure_sub_belongs_to_user", AsyncMock(return_value=mock_sub)) as mock_ensure, \
         patch("bot.api.patch_user", AsyncMock(return_value=False)) as mock_patch, \
         patch("bot._render_sub_open", AsyncMock()) as mock_render:
        
        await bot.cb_sub_squad(callback)

        mock_ensure.assert_called_once_with(callback, 1)
        mock_patch.assert_called_once_with({"uuid": "user-uuid", "activeInternalSquads": ["squad-uuid-123"]})
        callback.answer.assert_called_once_with("Не удалось сменить сквад.", show_alert=True)
        # Should still refresh even on failure to show current state
        mock_render.assert_called_once_with(callback, (1, "user-uuid", "short-uuid", "username", 1700000000, "label", 1700000000), prefer_edit=True)
