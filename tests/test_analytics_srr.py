import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from aiogram.types import InlineKeyboardMarkup

import bot
from handlers.admin_analytics import (
    _send_admin_stats_srr,
    _send_admin_stats_srr_recent,
    _send_admin_stats_srr_rules,
)


@pytest.mark.asyncio
async def test_send_admin_stats_srr_renders_correctly():
    callback = MagicMock()
    callback.from_user.id = 821295533
    callback.answer = AsyncMock()

    mock_stats = {
        "byParsedApp": [
            {"app": "Happ", "count": 283},
            {"app": "INCY", "count": 48},
            {"app": "Karing", "count": 47},
        ],
        "hourlyRequestStats": [
            {"dateTime": "2026-08-31T20:00:00.000Z", "requestCount": 10},
            {"dateTime": "2026-08-31T21:00:00.000Z", "requestCount": 25},
            {"dateTime": "2026-08-31T22:00:00.000Z", "requestCount": 5},
        ]
    }
    mock_history = {
        "total": 378,
        "records": [
            {"srrRuleName": "Mihomo Clients", "srrResponseType": "MIHOMO", "userAgent": "Happ", "requestIp": "1.1.1.1", "requestAt": "2026-08-31T22:00:00Z"},
            {"srrRuleName": "Sing-box clients", "srrResponseType": "SINGBOX", "userAgent": "Hiddify", "requestIp": "2.2.2.2", "requestAt": "2026-08-31T21:00:00Z"},
        ]
    }

    with patch("handlers.admin_analytics.api.get_subscription_request_history_stats", AsyncMock(return_value=mock_stats)), \
         patch("handlers.admin_analytics.api.get_all_subscription_request_history", AsyncMock(return_value=mock_history)), \
         patch("handlers.admin_analytics.safe_edit", AsyncMock()) as mock_safe_edit:

        await _send_admin_stats_srr(callback, prefer_edit=True)

        mock_safe_edit.assert_called_once()
        text = mock_safe_edit.call_args[0][1]
        reply_markup = mock_safe_edit.call_args[1]["reply_markup"]

        assert "Статистика запросов подписок и правил SRR" in text
        assert "Happ" in text
        assert "Mihomo Clients" in text
        assert "MIHOMO" in text
        assert isinstance(reply_markup, InlineKeyboardMarkup)


@pytest.mark.asyncio
async def test_send_admin_stats_srr_recent_renders_correctly():
    callback = MagicMock()
    callback.from_user.id = 821295533
    callback.answer = AsyncMock()

    mock_history = {
        "total": 2,
        "records": [
            {
                "requestAt": "2026-08-31T22:00:00Z",
                "userAgent": "Happ/3.22.1/Android",
                "srrRuleName": "Fallback Base64",
                "srrResponseType": "XRAY_BASE64",
                "requestIp": "172.30.0.1",
                "userId": 19,
            }
        ]
    }

    with patch("handlers.admin_analytics.api.get_all_subscription_request_history", AsyncMock(return_value=mock_history)), \
         patch("handlers.admin_analytics.safe_edit", AsyncMock()) as mock_safe_edit:

        await _send_admin_stats_srr_recent(callback, prefer_edit=True)

        mock_safe_edit.assert_called_once()
        text = mock_safe_edit.call_args[0][1]
        assert "Последние 1 запросов подписок" in text
        assert "Happ/3.22.1/Android" in text
        assert "user=<code>19</code>" in text
        assert "Fallback Base64" in text


@pytest.mark.asyncio
async def test_send_admin_stats_srr_rules_renders_correctly():
    callback = MagicMock()
    callback.from_user.id = 821295533
    callback.answer = AsyncMock()

    mock_settings = {
        "response": {
            "customResponseHeaders": {
                "profile-update-interval": "12",
                "announce": "Ротация серверов раз в месяц",
            },
            "responseRules": {
                "rules": [
                    {"name": "Mihomo Clients", "responseType": "MIHOMO", "enabled": True, "conditions": [{}]},
                    {"name": "Sing-box clients", "responseType": "SINGBOX", "enabled": True, "conditions": [{}]},
                ]
            }
        }
    }

    with patch("handlers.admin_analytics.api.get_subscription_settings", AsyncMock(return_value=mock_settings)), \
         patch("handlers.admin_analytics.safe_edit", AsyncMock()) as mock_safe_edit:

        await _send_admin_stats_srr_rules(callback, prefer_edit=True)

        mock_safe_edit.assert_called_once()
        text = mock_safe_edit.call_args[0][1]
        assert "Настройки маршрутизации" in text
        assert "Интервал авто-обновления" in text
        assert "12ч" in text
        assert "Mihomo Clients" in text
        assert "Sing-box clients" in text
