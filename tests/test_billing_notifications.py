import asyncio
from datetime import datetime, date, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import aiosqlite

import database as db
from app import (
    DAILY_REPORT_ENABLED_KEY,
    NODE_BILLING_NOTIFY_ENABLED_KEY,
    NODE_BILLING_NOTIFY_DAYS_KEY,
)
from scheduler import (
    _days_until_billing,
    check_billing_nodes_expiration,
    send_daily_traffic_report,
    MSK,
)
from handlers.admin_notifications import (
    get_digest_settings_summary,
    get_billing_settings_summary,
    cb_admin_notify_settings,
    cb_admin_notify_digest_menu,
    cb_admin_notify_billing_menu,
    cb_admin_notify_toggle,
    cb_admin_notify_test_billing,
)


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    test_db = str(tmp_path / "test_bot.db")
    monkeypatch.setattr(db, "DB_PATH", test_db)
    asyncio.run(db.init_db())
    return test_db


def test_days_until_billing():
    assert _days_until_billing(None) is None
    assert _days_until_billing("invalid-date") is None

    base_date = date(2026, 9, 26)
    # Today
    assert _days_until_billing("2026-09-26T00:00:00.000Z", base_date) == 0
    # Tomorrow
    assert _days_until_billing("2026-09-27T00:00:00.000Z", base_date) == 1
    # In 7 days
    assert _days_until_billing("2026-10-03T00:00:00.000Z", base_date) == 7
    # Yesterday
    assert _days_until_billing("2026-09-25T00:00:00.000Z", base_date) == -1


def test_database_billing_notifications():
    uuid = "test-node-b-uuid"
    billing_date = "2026-09-27"

    # Initially not sent
    assert asyncio.run(db.was_billing_notification_sent(uuid, 1, billing_date)) is False

    # Mark sent
    asyncio.run(db.mark_billing_notification_sent(uuid, 1, billing_date))
    assert asyncio.run(db.was_billing_notification_sent(uuid, 1, billing_date)) is True

    # Different days_left or different billing_date
    assert asyncio.run(db.was_billing_notification_sent(uuid, 0, billing_date)) is False
    assert asyncio.run(db.was_billing_notification_sent(uuid, 1, "2026-10-27")) is False

    # Cleanup
    asyncio.run(db.cleanup_old_billing_notifications(int(datetime.now().timestamp()) + 100))
    assert asyncio.run(db.was_billing_notification_sent(uuid, 1, billing_date)) is False


def test_check_billing_nodes_expiration_disabled():
    asyncio.run(db.set_setting(NODE_BILLING_NOTIFY_ENABLED_KEY, "0"))
    mock_bot = MagicMock()
    res = asyncio.run(check_billing_nodes_expiration(mock_bot))
    assert res["status"] == "disabled"
    assert res["sent"] == 0
    mock_bot.send_message.assert_not_called()


def test_check_billing_nodes_expiration_flow():
    asyncio.run(db.set_setting(NODE_BILLING_NOTIFY_ENABLED_KEY, "1"))
    asyncio.run(db.set_setting(NODE_BILLING_NOTIFY_DAYS_KEY, "7,3,1,0"))

    now_msk = datetime.now(MSK)
    tomorrow_iso = (now_msk + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00.000Z")
    far_iso = (now_msk + timedelta(days=20)).strftime("%Y-%m-%dT00:00:00.000Z")

    mock_billing_nodes = [
        {
            "uuid": "b-node-1",
            "nextBillingAt": tomorrow_iso,
            "node": {"name": "omixochitl", "countryCode": "NL"},
            "provider": {"name": "Phylex", "loginUrl": "https://phylex.net/login"},
        },
        {
            "uuid": "b-node-2",
            "nextBillingAt": far_iso,
            "node": {"name": "kamala", "countryCode": "AT"},
            "provider": {"name": "netcup"},
        },
    ]

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()

    with patch("app.api.list_billing_nodes", new_callable=AsyncMock) as mock_api:
        mock_api.return_value = mock_billing_nodes
        with patch("config.ADMIN_REPORT_CHAT_ID", -1003983882002):
            res = asyncio.run(check_billing_nodes_expiration(mock_bot))

            assert res["status"] == "ok"
            assert res["sent"] == 1
            assert res["checked"] == 2
            assert mock_bot.send_message.call_count == 1

            call_kwargs = mock_bot.send_message.call_args.kwargs
            assert call_kwargs["chat_id"] == -1003983882002
            assert "omixochitl" in call_kwargs["text"]
            assert "Phylex" in call_kwargs["text"]
            assert "Завтра" in call_kwargs["text"] or "завтра" in call_kwargs["text"]

            # Second run: duplicate notification should not be sent
            res_2 = asyncio.run(check_billing_nodes_expiration(mock_bot))
            assert res_2["sent"] == 0
            assert mock_bot.send_message.call_count == 1  # No new call


def test_send_daily_traffic_report_disabled():
    asyncio.run(db.set_setting(DAILY_REPORT_ENABLED_KEY, "0"))
    mock_bot = MagicMock()
    # Automated run without explicit target_chat_id or report_date
    res = asyncio.run(send_daily_traffic_report(mock_bot))
    assert res is False


def test_admin_notifications_ui():
    # 1. Main menu
    cb = MagicMock()
    cb.from_user.id = 123
    cb.message.chat.id = 123
    cb.message.message_id = 456
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()

    state = MagicMock()
    state.clear = AsyncMock()

    with patch("auth.is_admin", new_callable=AsyncMock) as mock_admin:
        mock_admin.return_value = True
        with patch("handlers.admin_notifications.safe_edit", new_callable=AsyncMock) as mock_edit:
            asyncio.run(cb_admin_notify_settings(cb, state))
            assert mock_edit.called
            text = mock_edit.call_args.args[1]
            kb = mock_edit.call_args.kwargs["reply_markup"]
            assert "Дайджест 23:59" in text
            assert "Биллинг нод" in text

            btn_data = [btn.callback_data for row in kb.inline_keyboard for btn in row]
            assert "admin_notify_digest_menu" in btn_data
            assert "admin_notify_billing_menu" in btn_data

    # 2. Digest menu summary and toggle
    body, kb = asyncio.run(get_digest_settings_summary())
    assert "Настройка ежедневного дайджеста трафика" in body
    btn_data = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "admin_notify_toggle:daily_report" in btn_data

    cb.data = "admin_notify_toggle:daily_report"
    with patch("auth.is_admin", new_callable=AsyncMock) as mock_admin:
        mock_admin.return_value = True
        with patch("handlers.admin_notifications.safe_edit", new_callable=AsyncMock):
            asyncio.run(cb_admin_notify_toggle(cb))
            val = asyncio.run(db.get_setting(DAILY_REPORT_ENABLED_KEY))
            assert val == "0"
            asyncio.run(cb_admin_notify_toggle(cb))
            val2 = asyncio.run(db.get_setting(DAILY_REPORT_ENABLED_KEY))
            assert val2 == "1"

    # 3. Billing menu summary and toggle
    with patch("handlers.admin_notifications.api.list_billing_nodes", new_callable=AsyncMock) as mock_api:
        mock_api.return_value = []
        body, kb = asyncio.run(get_billing_settings_summary())
        assert "Настройка уведомлений биллинга нод" in body
        btn_data = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        assert "admin_notify_toggle:billing" in btn_data
        assert "admin_notify_edit_days:billing" in btn_data
        assert "admin_notify_test_billing" in btn_data

    cb.data = "admin_notify_toggle:billing"
    with patch("auth.is_admin", new_callable=AsyncMock) as mock_admin:
        mock_admin.return_value = True
        with patch("handlers.admin_notifications.api.list_billing_nodes", new_callable=AsyncMock) as mock_api:
            mock_api.return_value = []
            with patch("handlers.admin_notifications.safe_edit", new_callable=AsyncMock):
                asyncio.run(cb_admin_notify_toggle(cb))
                val = asyncio.run(db.get_setting(NODE_BILLING_NOTIFY_ENABLED_KEY))
                assert val == "0"
                asyncio.run(cb_admin_notify_toggle(cb))
                val2 = asyncio.run(db.get_setting(NODE_BILLING_NOTIFY_ENABLED_KEY))
                assert val2 == "1"
