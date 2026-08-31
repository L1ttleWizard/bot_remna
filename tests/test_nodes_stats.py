import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from aiogram.types import CallbackQuery
from handlers.admin_analytics import (
    draw_nodes_traffic_text_chart,
    _send_admin_stats_nodes_menu,
    _nodes_stats_keyboard,
    _stats_keyboard,
)

def test_draw_nodes_traffic_text_chart_empty():
    res = draw_nodes_traffic_text_chart([])
    assert res == "Нет данных по трафику нод."

def test_draw_nodes_traffic_text_chart_zero():
    res = draw_nodes_traffic_text_chart([("Node1", 0)])
    assert res == "Трафик на всех нодах равен 0."

def test_draw_nodes_traffic_text_chart_valid():
    data = [
        ("bulgaria_main", 990 * 1024 * 1024 * 1024),
        ("eu_node2", 704 * 1024 * 1024 * 1024),
        ("Yandex_ru", int(17.3 * 1024 * 1024 * 1024)),
    ]
    res = draw_nodes_traffic_text_chart(data)
    assert "bulgaria_main" in res
    assert "eu_node2" in res
    assert "Yandex_ru" in res
    assert "█" in res
    assert "░" in res

@pytest.mark.asyncio
async def test_send_admin_stats_nodes_menu_empty_nodes():
    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = MagicMock()
    callback.from_user.id = 12345
    callback.message = MagicMock()
    callback.message.photo = None
    
    mock_nodes = []
    mock_bw_stats = {
        "categories": ["2026-06-01"],
        "series": [
            {"name": "Node1", "total": 1000000000, "data": [1000000000], "color": "#ff0000"}
        ]
    }
    
    with patch("handlers.admin_analytics.api.list_nodes", AsyncMock(return_value=mock_nodes)), \
         patch("handlers.admin_analytics.api.get_nodes_bandwidth_stats", AsyncMock(return_value=mock_bw_stats)), \
         patch("handlers.admin_analytics.safe_edit", AsyncMock()) as mock_safe_edit:
        
        await _send_admin_stats_nodes_menu(callback, prefer_edit=True)
        
        mock_safe_edit.assert_called_once()
        args, kwargs = mock_safe_edit.call_args
        text = args[1]
        assert "Активных нод: <b>0</b>" in text
        assert "Суммарный онлайн: <b>0 чел.</b>" in text
        assert "Node1" in text
        assert "100.0%" in text
