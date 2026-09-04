import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from aiogram.types import CallbackQuery, Message, User
import database as db
from bot import _issue_trial_for_user, _show_start_menu

@pytest.mark.asyncio
async def test_trial_claim_database_flow():
    test_tg_id = 999111222
    assert await db.has_claimed_trial(test_tg_id) is False
    await db.record_trial_claim(test_tg_id)
    assert await db.has_claimed_trial(test_tg_id) is True


@pytest.mark.asyncio
async def test_issue_trial_success():
    tg_user = MagicMock(spec=User)
    tg_user.id = 888777666
    tg_user.username = "test_trial_user"
    tg_user.first_name = "TrialUser"

    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = tg_user
    callback.answer = AsyncMock()

    with patch("bot.db.count_subscriptions", AsyncMock(return_value=0)), \
         patch("bot.db.has_claimed_trial", AsyncMock(return_value=False)), \
         patch("bot.create_account_for_user", AsyncMock(return_value="https://sub.test/sub/123")), \
         patch("bot.db.record_trial_claim", AsyncMock()) as mock_record, \
         patch("bot.db.get_referral_by_referee", AsyncMock(return_value=None)), \
         patch("bot.safe_edit", AsyncMock()) as mock_safe_edit:

        await _issue_trial_for_user(callback, tg_user, prefer_edit=True)

        mock_record.assert_called_once_with(888777666)
        mock_safe_edit.assert_called_once()
        args, kwargs = mock_safe_edit.call_args
        text = args[1]
        assert "Тестовый доступ" in text or "активирован" in text


@pytest.mark.asyncio
async def test_issue_trial_already_claimed():
    tg_user = MagicMock(spec=User)
    tg_user.id = 888777666
    tg_user.username = "test_trial_user"
    tg_user.first_name = "TrialUser"

    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = tg_user
    callback.answer = AsyncMock()

    with patch("bot.db.count_subscriptions", AsyncMock(return_value=0)), \
         patch("bot.db.has_claimed_trial", AsyncMock(return_value=True)), \
         patch("bot.safe_edit", AsyncMock()) as mock_safe_edit:

        await _issue_trial_for_user(callback, tg_user, prefer_edit=True)

        callback.answer.assert_called_once_with("Пробный период уже был использован.", show_alert=True)
        mock_safe_edit.assert_called_once()
        args, kwargs = mock_safe_edit.call_args
        text = args[1]
        assert "Вы уже использовали пробный период" in text
