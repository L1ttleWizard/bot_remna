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


def test_connect_platform_keyboard_back_button():
    """Verify back button target in connect_platform_keyboard based on has_multiple flag."""
    import clients

    # has_multiple=True -> Should go to sub list picker (connect)
    kb_multi = clients.connect_platform_keyboard(1, has_multiple=True)
    back_btn = kb_multi.inline_keyboard[-1][0]
    assert back_btn.text == "◀️ Назад"
    assert back_btn.callback_data == "connect"

    # has_multiple=False -> Should go to main menu (back_main)
    kb_single = clients.connect_platform_keyboard(1, has_multiple=False)
    back_btn_single = kb_single.inline_keyboard[-1][0]
    assert back_btn_single.text == "◀️ Назад"
    assert back_btn_single.callback_data == "back_main"


@pytest.mark.asyncio
async def test_show_connect_platform_menu_single_sub():
    """Verify that _show_connect_platform_menu detects single subscription and configures back button properly."""
    from handlers import connect
    from aiogram.types import CallbackQuery

    callback = MagicMock()
    callback.from_user = MagicMock()
    callback.from_user.id = 12345
    callback.answer = AsyncMock()

    mock_subs = [
        (1, "uuid-A", "short-A", "username-A", 1700000000, "label-A", 1700000000)
    ]

    with patch("handlers.connect.db.list_subscriptions", AsyncMock(return_value=mock_subs)), \
         patch("handlers.connect.safe_edit", AsyncMock()) as mock_safe_edit:

        await connect._show_connect_platform_menu(callback, 1)

        mock_safe_edit.assert_called_once()
        reply_markup = mock_safe_edit.call_args[1]["reply_markup"]
        back_btn = reply_markup.inline_keyboard[-1][0]
        
        # Verify it goes to main menu because there is only 1 subscription
        assert back_btn.callback_data == "back_main"


def test_inline_query_has_wildcard_state_filter():
    """Verify that the inline query handler is registered with a wildcard StateFilter."""
    from handlers import inline_search
    from aiogram.filters import StateFilter

    # Get the inline search handler registered on dp
    # dp.inline_query.handlers is a list of registered handlers/filters
    found_wildcard = False
    for handler in inline_search.dp.inline_query.handlers:
        # Check if the filters contain a StateFilter with "*" state
        for filter_obj in handler.filters:
            actual_filter = getattr(filter_obj, "callback", filter_obj)
            if isinstance(actual_filter, StateFilter):
                if any(s == "*" for s in getattr(actual_filter, "states", [])):
                    found_wildcard = True
                    break
    
    assert found_wildcard is True, "Inline query handler is missing StateFilter('*') filter"


@pytest.mark.asyncio
async def test_track_bot_message_and_delete():
    """Verify track_bot_message registers message IDs and delete_active_bot_messages deletes them."""
    import app
    from aiogram.exceptions import TelegramBadRequest

    tg_id = 99999
    app.active_bot_messages.clear()

    app.track_bot_message(tg_id, 101)
    app.track_bot_message(tg_id, 102)
    assert app.active_bot_messages[tg_id] == [101, 102]

    mock_bot = AsyncMock()
    # Mock delete_message to succeed for 101 and fail for 102 to verify error handling
    async def mock_delete(chat_id, message_id):
        if message_id == 102:
            raise TelegramBadRequest(message="message to delete not found", method=None)
        return True
    mock_bot.delete_message = AsyncMock(side_effect=mock_delete)

    await app.delete_active_bot_messages(mock_bot, tg_id)
    assert mock_bot.delete_message.call_count == 2
    assert app.active_bot_messages[tg_id] == []


@pytest.mark.asyncio
async def test_bot_call_hook_tracks_private_chat_messages():
    """Verify that Bot.__call__ hook automatically tracks bot-sent messages in private chats."""
    import app
    from aiogram import Bot
    from aiogram.types import Message, Chat
    import datetime

    app.active_bot_messages.clear()
    
    # Create a real/mocked Message object returned by original call
    chat = Chat(id=12345, type="private")
    message = Message(
        message_id=505,
        date=datetime.datetime.now(datetime.timezone.utc),
        chat=chat,
    )

    # Patch original_call to return our message
    with patch("app.original_call", AsyncMock(return_value=message)):
        test_bot = Bot(token="123456:AAA-BBB_ccc-fakefakefakefakefakefakefa")
        res = await test_bot.send_message(chat_id=12345, text="Hello")
        assert res.message_id == 505
        assert app.active_bot_messages[12345] == [505]


@pytest.mark.asyncio
async def test_clean_chat_user_message_middleware():
    """Verify CleanChatUserMessageMiddleware deletes incoming user message in private chats."""
    import app
    from aiogram.types import Message, Chat

    middleware = app.CleanChatUserMessageMiddleware()
    handler = AsyncMock()

    chat = Chat(id=12345, type="private")
    message = MagicMock(spec=Message)
    message.chat = chat
    message.delete = AsyncMock()

    data = {}
    await middleware(handler, message, data)
    
    message.delete.assert_called_once()
    handler.assert_called_once_with(message, data)


@pytest.mark.asyncio
async def test_clean_chat_bot_message_middleware():
    """Verify CleanChatBotMessageMiddleware deletes old bot messages before handler runs."""
    import app
    from aiogram.types import Message, Chat

    middleware = app.CleanChatBotMessageMiddleware()
    handler = AsyncMock()

    chat = Chat(id=12345, type="private")
    message = MagicMock(spec=Message)
    message.chat = chat
    message.from_user = MagicMock()
    message.from_user.id = 12345
    message.bot = AsyncMock()

    # Place a dummy tracked message to verify it gets deleted
    app.active_bot_messages[12345] = [808]

    data = {}
    await middleware(handler, message, data)

    # Message 808 should be deleted
    message.bot.delete_message.assert_called_once_with(chat_id=12345, message_id=808)
    assert app.active_bot_messages[12345] == []
    handler.assert_called_once_with(message, data)


@pytest.mark.asyncio
async def test_bot_call_hook_ignores_document_messages():
    """Verify that Bot.__call__ hook does NOT track bot-sent messages containing a document."""
    import app
    from aiogram import Bot
    from aiogram.types import Message, Chat, Document
    import datetime

    app.active_bot_messages.clear()
    
    # Create a Message object with a document
    chat = Chat(id=12345, type="private")
    doc = Document(file_id="dummy_file_id", file_unique_id="dummy_uniq_id")
    message = Message(
        message_id=606,
        date=datetime.datetime.now(datetime.timezone.utc),
        chat=chat,
        document=doc,
    )

    # Patch original_call to return our document message
    with patch("app.original_call", AsyncMock(return_value=message)):
        test_bot = Bot(token="123456:AAA-BBB_ccc-fakefakefakefakefakefakefa")
        res = await test_bot.send_message(chat_id=12345, text="Here is a doc")
        assert res.message_id == 606
        # Should NOT be tracked because it has a document
        assert 12345 not in app.active_bot_messages or app.active_bot_messages[12345] == []

