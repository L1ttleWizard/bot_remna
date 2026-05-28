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
async def test_clear_state_on_navigation_middleware_clears_state():
    """Verify that ClearStateOnNavigationMiddleware clears the state for navigation callbacks, but not for FSM callbacks."""
    import app
    from aiogram.types import CallbackQuery
    from aiogram.fsm.context import FSMContext

    middleware = app.ClearStateOnNavigationMiddleware()
    handler = AsyncMock()

    # Case 1: Navigation callback (e.g., back_main) -> State should be cleared
    callback = MagicMock(spec=CallbackQuery)
    callback.data = "back_main"
    state = MagicMock(spec=FSMContext)
    state.get_state = AsyncMock(return_value="SomeState")
    state.clear = AsyncMock()

    data = {"state": state}
    await middleware(handler, callback, data)

    state.clear.assert_called_once()
    handler.assert_called_once_with(callback, data)

    # Case 2: FSM callback (e.g., promo:sub:1) -> State should NOT be cleared
    callback_fsm = MagicMock(spec=CallbackQuery)
    callback_fsm.data = "promo:sub:1"
    state_fsm = MagicMock(spec=FSMContext)
    state_fsm.get_state = AsyncMock(return_value="PromoStates:waiting_for_sub_pick")
    state_fsm.clear = AsyncMock()

    data_fsm = {"state": state_fsm}
    handler.reset_mock()
    await middleware(handler, callback_fsm, data_fsm)

    state_fsm.clear.assert_not_called()
    handler.assert_called_once_with(callback_fsm, data_fsm)


def test_user_sub_menu_keyboard_has_multiple():
    """Verify back button in user_sub_menu_keyboard based on has_multiple flag."""
    import keyboards

    # has_multiple=True -> Should go to list of subscriptions (my_subs)
    kb_multi = keyboards.user_sub_menu_keyboard(42, has_multiple=True)
    buttons = kb_multi.inline_keyboard
    back_btn = buttons[-1][0]
    assert back_btn.text == "◀️ К списку подписок"
    assert back_btn.callback_data == "my_subs"

    # has_multiple=False -> Should go to main menu (back_main)
    kb_single = keyboards.user_sub_menu_keyboard(42, has_multiple=False)
    buttons_single = kb_single.inline_keyboard
    back_btn_single = buttons_single[-1][0]
    assert back_btn_single.text == "◀️ Назад в главное меню"
    assert back_btn_single.callback_data == "back_main"


@pytest.mark.asyncio
async def test_render_sub_open_single_sub():
    """Verify that _render_sub_open detects single subscription and renders back_main button."""
    import bot

    callback = MagicMock()
    callback.from_user = MagicMock()
    callback.from_user.id = 12345
    callback.answer = AsyncMock()

    sub = (1, "uuid-A", "short-A", "username-A", 1700000000, "label-A", 1700000000)
    mock_subs = [sub]

    with patch("bot.db.list_subscriptions", AsyncMock(return_value=mock_subs)), \
         patch("bot.safe_edit", AsyncMock()) as mock_safe_edit:

        await bot._render_sub_open(callback, sub, prefer_edit=True)

        mock_safe_edit.assert_called_once()
        reply_markup = mock_safe_edit.call_args[1]["reply_markup"]
        back_btn = reply_markup.inline_keyboard[-1][0]
        
        # Verify it goes to main menu because there is only 1 subscription
        assert back_btn.text == "◀️ Назад в главное меню"
        assert back_btn.callback_data == "back_main"
