import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from handlers.admin_analytics import (
    get_country_flag,
    format_node_display_name,
    _format_user_display,
    collect_daily_traffic_data,
    format_daily_report_text,
)
from services.chart_generator import generate_daily_nodes_distribution_chart
from scheduler import send_daily_traffic_report


def test_get_country_flag():
    assert get_country_flag("AT") == "🇦🇹"
    assert get_country_flag("DE") == "🇩🇪"
    assert get_country_flag("FI") == "🇫🇮"
    assert get_country_flag("US") == "🇺🇸"
    assert get_country_flag(None) == "🌐"
    assert get_country_flag("") == "🌐"
    assert get_country_flag("12") == "🌐"
    assert get_country_flag("USA") == "🌐"


def test_format_node_display_name():
    assert format_node_display_name("Austria", "AT") == "🇦🇹 Austria"
    assert format_node_display_name("🇦🇹 Austria", "AT") == "🇦🇹 Austria"
    assert format_node_display_name("Custom Node", None) == "Custom Node"


def test_format_user_display():
    # Empty / 0 total
    assert _format_user_display(None) == "—"
    assert _format_user_display({"total": 0}) == "—"

    # With tg_username
    u1 = {"username": "tg_123", "tg_id": 123, "tg_username": "durov", "total": 1024 * 1024 * 1024}
    res1 = _format_user_display(u1)
    assert "@durov" in res1
    assert "123" in res1
    assert "1 ГБ" in res1

    # With tg_name but no username
    u2 = {"username": "tg_456", "tg_id": 456, "tg_name": "Ivan Ivanov", "total": 500 * 1024 * 1024}
    res2 = _format_user_display(u2)
    assert "Ivan Ivanov" in res2
    assert "456" in res2

    # Without Telegram data
    u3 = {"username": "alice", "total": 200 * 1024 * 1024}
    res3 = _format_user_display(u3)
    assert "alice" in res3


def test_generate_daily_nodes_distribution_chart_valid():
    nodes_traffic = [
        ("Austria 🇦🇹", 50 * 1024 * 1024 * 1024, "#00F0FF"),
        ("Germany 🇩🇪", 30 * 1024 * 1024 * 1024, "#FF007F"),
        ("Finland 🇫🇮", 20 * 1024 * 1024 * 1024, "#39FF14"),
    ]
    total_bytes = 100 * 1024 * 1024 * 1024
    png_bytes = generate_daily_nodes_distribution_chart(nodes_traffic, total_bytes, "26.09.2026")
    assert isinstance(png_bytes, bytes)
    assert len(png_bytes) > 1000
    assert png_bytes.startswith(b"\x89PNG\r\n\x1a\n")


def test_generate_daily_nodes_distribution_chart_empty():
    png_bytes = generate_daily_nodes_distribution_chart([], 0, "26.09.2026")
    assert isinstance(png_bytes, bytes)
    assert len(png_bytes) > 500
    assert png_bytes.startswith(b"\x89PNG\r\n\x1a\n")


@pytest.mark.asyncio
async def test_collect_daily_traffic_data():
    mock_bw_stats = {
        "categories": ["2026-09-26"],
        "series": [
            {
                "uuid": "uuid-node-1",
                "name": "Austria",
                "countryCode": "AT",
                "color": "#00F0FF",
                "total": 60 * 1024 * 1024 * 1024,
                "data": [60 * 1024 * 1024 * 1024],
            },
            {
                "uuid": "uuid-node-2",
                "name": "Germany",
                "countryCode": "DE",
                "color": "#FF007F",
                "total": 40 * 1024 * 1024 * 1024,
                "data": [40 * 1024 * 1024 * 1024],
            },
        ],
    }

    mock_users_node_1 = {
        "topUsers": [
            {"username": "tg_111", "userId": 1, "total": 25 * 1024 * 1024 * 1024},
            {"username": "tg_222", "userId": 2, "total": 10 * 1024 * 1024 * 1024},
        ]
    }
    mock_users_node_2 = {
        "topUsers": [
            {"username": "tg_111", "userId": 1, "total": 15 * 1024 * 1024 * 1024},
            {"username": "tg_333", "userId": 3, "total": 5 * 1024 * 1024 * 1024},
        ]
    }

    async def mock_get_users(node_uuid, start, end, top_limit=10):
        if node_uuid == "uuid-node-1":
            return mock_users_node_1
        return mock_users_node_2

    with patch("handlers.admin_analytics.api.get_nodes_bandwidth_stats", AsyncMock(return_value=mock_bw_stats)), \
         patch("handlers.admin_analytics.api.get_node_bandwidth_users", side_effect=mock_get_users), \
         patch("handlers.admin_analytics._resolve_tg_user_info", AsyncMock(return_value=(111, "durov", "Pavel"))):

        data = await collect_daily_traffic_data("2026-09-26")

        assert data["date_str"] == "26.09.2026"
        assert data["total_bytes"] == 100 * 1024 * 1024 * 1024
        assert len(data["nodes"]) == 2

        # Check nodes order & percentages
        assert data["nodes"][0]["name"] == "Austria"
        assert data["nodes"][0]["percentage"] == 60.0
        assert data["nodes"][0]["flag"] == "🇦🇹"
        assert data["nodes"][0]["top_user"]["username"] == "tg_111"

        assert data["nodes"][1]["name"] == "Germany"
        assert data["nodes"][1]["percentage"] == 40.0
        assert data["nodes"][1]["flag"] == "🇩🇪"

        # Overall top user: user tg_111 with 25GB + 15GB = 40GB
        assert data["overall_top_user"] is not None
        assert data["overall_top_user"]["username"] == "tg_111"
        assert data["overall_top_user"]["total"] == 40 * 1024 * 1024 * 1024
        assert data["overall_top_user"]["tg_username"] == "durov"


def test_format_daily_report_text():
    report_data = {
        "date_str": "26.09.2026",
        "total_bytes": 100 * 1024 * 1024 * 1024,
        "overall_top_user": {
            "username": "tg_111",
            "tg_id": 111,
            "tg_username": "durov",
            "total": 40 * 1024 * 1024 * 1024,
        },
        "nodes": [
            {
                "name": "Austria",
                "display_name": "🇦🇹 Austria",
                "total_bytes": 60 * 1024 * 1024 * 1024,
                "percentage": 60.0,
                "top_user": {
                    "username": "tg_111",
                    "tg_id": 111,
                    "tg_username": "durov",
                    "total": 25 * 1024 * 1024 * 1024,
                },
            },
            {
                "name": "Germany",
                "display_name": "🇩🇪 Germany",
                "total_bytes": 40 * 1024 * 1024 * 1024,
                "percentage": 40.0,
                "top_user": None,
            },
        ],
    }

    caption, extra = format_daily_report_text(report_data)
    assert extra is None
    assert "Ежедневный отчет по трафику за 26.09.2026" in caption
    assert "100 ГБ" in caption
    assert "@durov" in caption
    assert "🇦🇹 Austria" in caption
    assert "60 ГБ" in caption
    assert "(60.0%)" in caption
    assert "🇩🇪 Germany" in caption


@pytest.mark.asyncio
async def test_send_daily_traffic_report_flow():
    mock_bot = AsyncMock()
    mock_report_data = {
        "date_str": "26.09.2026",
        "raw_date": "2026-09-26",
        "total_bytes": 100 * 1024 * 1024 * 1024,
        "overall_top_user": None,
        "nodes": [],
    }

    with patch("handlers.admin_analytics.collect_daily_traffic_data", AsyncMock(return_value=mock_report_data)), \
         patch("services.chart_generator.generate_daily_nodes_distribution_chart", return_value=b"fake-png-bytes"):

        # 1. Target chat explicitly provided
        ok = await send_daily_traffic_report(mock_bot, target_chat_id=-1001234567)
        assert ok is True
        mock_bot.send_photo.assert_called_once()
        args, kwargs = mock_bot.send_photo.call_args
        assert kwargs["chat_id"] == -1001234567
        assert "Ежедневный отчет" in kwargs["caption"]

        # 2. No recipient provided and config empty -> returns False
        mock_bot.reset_mock()
        with patch("config.ADMIN_REPORT_CHAT_ID", None), \
             patch("config.BACKUP_TG_CHAT_ID", None), \
             patch("config.ADMIN_TG_IDS", set()):
            ok_empty = await send_daily_traffic_report(mock_bot)
            assert ok_empty is False
            mock_bot.send_photo.assert_not_called()

