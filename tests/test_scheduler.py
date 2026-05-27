import asyncio
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Setup dummy environments
os.environ.setdefault("BOT_TOKEN", "123456:token")
os.environ.setdefault("REMNAWAVE_URL", "https://panel.example.com")
os.environ.setdefault("REMNAWAVE_TOKEN", "test-token")


@pytest.fixture()
def db_module(tmp_path):
    db_file = tmp_path / "test_scheduler_bot.db"
    os.environ["DATABASE_PATH"] = str(db_file)
    import importlib
    if "database" in sys.modules:
        importlib.reload(sys.modules["database"])
    import database
    return database


def test_check_nodes_health_alert(db_module):
    import scheduler
    from aiogram import Bot

    asyncio.run(db_module.init_db())
    # Add a mock admin
    asyncio.run(db_module.add_user(tg_id=12345, uuid="u1", short_uuid="s1", username="admin", expire_date=0, role="admin"))

    bot_mock = MagicMock(spec=Bot)
    bot_mock.send_message = AsyncMock()

    # Mock api.list_nodes
    mock_nodes = [
        {"uuid": "node-1", "name": "Server 1", "isConnected": True, "address": "1.1.1.1", "port": 1234},
        {"uuid": "node-2", "name": "Server 2", "isConnected": False, "address": "2.2.2.2", "port": 5678},
    ]

    with patch("scheduler.api.list_nodes", AsyncMock(return_value=mock_nodes)), \
         patch("scheduler.ADMIN_TG_IDS", {12345}):
        # 1. First check: Save initial status, no alert sent since it's first run
        asyncio.run(scheduler.check_nodes_health(bot_mock))
        bot_mock.send_message.assert_not_called()

        # Let's verify statuses were stored in DB
        status1 = asyncio.run(db_module.get_node_status("node-1"))
        assert status1["was_connected"] is True
        status2 = asyncio.run(db_module.get_node_status("node-2"))
        assert status2["was_connected"] is False

    # 2. Second check: node-1 goes offline, should trigger alert
    mock_nodes_2 = [
        {"uuid": "node-1", "name": "Server 1", "isConnected": False, "address": "1.1.1.1", "port": 1234},
        {"uuid": "node-2", "name": "Server 2", "isConnected": False, "address": "2.2.2.2", "port": 5678},
    ]
    with patch("scheduler.api.list_nodes", AsyncMock(return_value=mock_nodes_2)), \
         patch("scheduler.ADMIN_TG_IDS", {12345}):
        asyncio.run(scheduler.check_nodes_health(bot_mock))
        # bot_mock.send_message should have been called for admin_id=12345
        bot_mock.send_message.assert_called_once()
        assert "Сервер оффлайн!" in bot_mock.send_message.call_args[1]["text"]
        assert "Server 1" in bot_mock.send_message.call_args[1]["text"]

        # Verify node-1 has alerted_down = True
        status1 = asyncio.run(db_module.get_node_status("node-1"))
        assert status1["was_connected"] is False
        assert status1["alerted_down"] is True

    # 3. Third check: node-1 goes online, should trigger recovery alert
    bot_mock.send_message.reset_mock()
    mock_nodes_3 = [
        {"uuid": "node-1", "name": "Server 1", "isConnected": True, "address": "1.1.1.1", "port": 1234},
        {"uuid": "node-2", "name": "Server 2", "isConnected": False, "address": "2.2.2.2", "port": 5678},
    ]
    with patch("scheduler.api.list_nodes", AsyncMock(return_value=mock_nodes_3)), \
         patch("scheduler.ADMIN_TG_IDS", {12345}):
        asyncio.run(scheduler.check_nodes_health(bot_mock))
        bot_mock.send_message.assert_called_once()
        assert "Сервер снова онлайн" in bot_mock.send_message.call_args[1]["text"]
        assert "Server 1" in bot_mock.send_message.call_args[1]["text"]

        # Verify node-1 has alerted_down = False
        status1 = asyncio.run(db_module.get_node_status("node-1"))
        assert status1["was_connected"] is True
        assert status1["alerted_down"] is False


def test_check_cpu_load_alert(db_module):
    import scheduler
    from aiogram import Bot

    asyncio.run(db_module.init_db())
    # Add a mock admin
    asyncio.run(db_module.add_user(tg_id=12345, uuid="u1", short_uuid="s1", username="admin", expire_date=0, role="admin"))

    bot_mock = MagicMock(spec=Bot)
    bot_mock.send_message = AsyncMock()

    # Mock settings
    asyncio.run(db_module.set_setting("cpu_notify_enabled", "1"))
    asyncio.run(db_module.set_setting("cpu_threshold", "80"))
    asyncio.run(db_module.set_setting("cpu_sustained_minutes", "2"))

    mock_nodes = [
        {"uuid": "node-1", "name": "Server 1", "isConnected": True, "address": "1.1.1.1", "port": 1234},
    ]

    # Mock api.get_node returns system stats (e.g. cpu=90%)
    mock_get_node_normal = {"response": {"system": {"stats": {"cpu": 45.0}}}}
    mock_get_node_high = {"response": {"system": {"stats": {"cpu": 90.0}}}}

    with patch("scheduler.api.list_nodes", AsyncMock(return_value=mock_nodes)), \
         patch("scheduler.ADMIN_TG_IDS", {12345}):
        # 1. Normal CPU (45% < 80%)
        with patch("scheduler.api.get_node", AsyncMock(return_value=mock_get_node_normal)):
            asyncio.run(scheduler.check_cpu_load(bot_mock))
            bot_mock.send_message.assert_not_called()
            assert asyncio.run(db_module.get_cpu_high("node-1")) is None

        # 2. High CPU first time (90% > 80%)
        with patch("scheduler.api.get_node", AsyncMock(return_value=mock_get_node_high)):
            asyncio.run(scheduler.check_cpu_load(bot_mock))
            bot_mock.send_message.assert_not_called() # No alert yet (need 2 mins sustained)

            cpu_high = asyncio.run(db_module.get_cpu_high("node-1"))
            assert cpu_high is not None
            assert cpu_high["alerted"] is False

            # Manually simulate time passing: set first_high_ts to 3 minutes ago
            fake_first_high = int(time.time()) - 180
            asyncio.run(db_module.clear_cpu_high("node-1"))
            asyncio.run(db_module.upsert_cpu_high("node-1", "Server 1", fake_first_high))

            # 3. High CPU second time (sustained)
            asyncio.run(scheduler.check_cpu_load(bot_mock))
            bot_mock.send_message.assert_called_once()
            assert "Высокая загрузка CPU!" in bot_mock.send_message.call_args[1]["text"]
            assert "90.0%" in bot_mock.send_message.call_args[1]["text"]

            cpu_high = asyncio.run(db_module.get_cpu_high("node-1"))
            assert cpu_high["alerted"] is True

        # 4. CPU drops back to normal, should alert recovery
        bot_mock.send_message.reset_mock()
        with patch("scheduler.api.get_node", AsyncMock(return_value=mock_get_node_normal)):
            asyncio.run(scheduler.check_cpu_load(bot_mock))
            bot_mock.send_message.assert_called_once()
            assert "Загрузка CPU нормализовалась" in bot_mock.send_message.call_args[1]["text"]
            assert asyncio.run(db_module.get_cpu_high("node-1")) is None


def test_check_cpu_load_keys_and_scaling(db_module):
    import scheduler
    from aiogram import Bot

    asyncio.run(db_module.init_db())
    bot_mock = MagicMock(spec=Bot)
    bot_mock.send_message = AsyncMock()

    # Mock settings
    asyncio.run(db_module.set_setting("cpu_notify_enabled", "1"))
    asyncio.run(db_module.set_setting("cpu_threshold", "80"))
    asyncio.run(db_module.set_setting("cpu_sustained_minutes", "2"))

    mock_nodes = [
        {"uuid": "node-1", "name": "Server 1", "isConnected": True, "address": "1.1.1.1", "port": 1234},
    ]

    with patch("scheduler.api.list_nodes", AsyncMock(return_value=mock_nodes)), \
         patch("scheduler.ADMIN_TG_IDS", {12345}):

        # Test cpuUsage key
        mock_payload_cpu_usage = {"response": {"system": {"stats": {"cpuUsage": 95.0}}}}
        with patch("scheduler.api.get_node", AsyncMock(return_value=mock_payload_cpu_usage)):
            asyncio.run(scheduler.check_cpu_load(bot_mock))
            cpu_high = asyncio.run(db_module.get_cpu_high("node-1"))
            assert cpu_high is not None
            asyncio.run(db_module.clear_cpu_high("node-1"))

        # Test cpu_usage key
        mock_payload_cpu_usage_snake = {"response": {"system": {"stats": {"cpu_usage": 91.0}}}}
        with patch("scheduler.api.get_node", AsyncMock(return_value=mock_payload_cpu_usage_snake)):
            asyncio.run(scheduler.check_cpu_load(bot_mock))
            cpu_high = asyncio.run(db_module.get_cpu_high("node-1"))
            assert cpu_high is not None
            asyncio.run(db_module.clear_cpu_high("node-1"))

        # Test cpuPercent key
        mock_payload_cpu_percent = {"response": {"system": {"stats": {"cpuPercent": 85.0}}}}
        with patch("scheduler.api.get_node", AsyncMock(return_value=mock_payload_cpu_percent)):
            asyncio.run(scheduler.check_cpu_load(bot_mock))
            cpu_high = asyncio.run(db_module.get_cpu_high("node-1"))
            assert cpu_high is not None
            asyncio.run(db_module.clear_cpu_high("node-1"))

        # Test scale conversion (0.88 <= 1.0, threshold is 80, should be scaled to 88.0%)
        mock_payload_scale = {"response": {"system": {"stats": {"cpu": 0.88}}}}
        with patch("scheduler.api.get_node", AsyncMock(return_value=mock_payload_scale)):
            asyncio.run(scheduler.check_cpu_load(bot_mock))
            cpu_high = asyncio.run(db_module.get_cpu_high("node-1"))
            assert cpu_high is not None
            asyncio.run(db_module.clear_cpu_high("node-1"))


def test_check_cpu_load_load_avg(db_module):
    import scheduler
    from aiogram import Bot

    asyncio.run(db_module.init_db())
    # Add a mock admin
    asyncio.run(db_module.add_user(tg_id=12345, uuid="u1", short_uuid="s1", username="admin", expire_date=0, role="admin"))

    bot_mock = MagicMock(spec=Bot)
    bot_mock.send_message = AsyncMock()

    # Mock settings
    asyncio.run(db_module.set_setting("cpu_notify_enabled", "1"))
    asyncio.run(db_module.set_setting("cpu_threshold", "80"))
    asyncio.run(db_module.set_setting("cpu_sustained_minutes", "2"))

    mock_nodes = [
        {"uuid": "node-1", "name": "Server 1", "isConnected": True, "address": "1.1.1.1", "port": 1234},
    ]

    # Mock api.get_node returns system stats with loadAvg
    # 2 CPUs, normal loads: [0.5, 1.0, 1.5] -> 1m=25%, 5m=50%, 15m=75% (all < 80%)
    mock_get_node_normal = {
        "response": {
            "system": {
                "info": {"cpus": 2},
                "stats": {"loadAvg": [0.5, 1.0, 1.5]}
            }
        }
    }
    # 2 CPUs, high loads: [0.5, 1.8, 1.5] -> 1m=25%, 5m=90%, 15m=75% (5m exceeds 80%)
    mock_get_node_high_5m = {
        "response": {
            "system": {
                "info": {"cpus": 2},
                "stats": {"loadAvg": [0.5, 1.8, 1.5]}
            }
        }
    }

    with patch("scheduler.api.list_nodes", AsyncMock(return_value=mock_nodes)), \
         patch("scheduler.ADMIN_TG_IDS", {12345}):
        
        # 1. Normal load average
        with patch("scheduler.api.get_node", AsyncMock(return_value=mock_get_node_normal)):
            asyncio.run(scheduler.check_cpu_load(bot_mock))
            bot_mock.send_message.assert_not_called()
            assert asyncio.run(db_module.get_cpu_high("node-1")) is None

        # 2. High 5m load average first time (90% > 80%)
        with patch("scheduler.api.get_node", AsyncMock(return_value=mock_get_node_high_5m)):
            asyncio.run(scheduler.check_cpu_load(bot_mock))
            bot_mock.send_message.assert_not_called() # No alert yet (need 2 mins sustained)
            
            cpu_high = asyncio.run(db_module.get_cpu_high("node-1"))
            assert cpu_high is not None
            assert cpu_high["alerted"] is False

            # Simulate 3 minutes passing
            fake_first_high = int(time.time()) - 180
            asyncio.run(db_module.clear_cpu_high("node-1"))
            asyncio.run(db_module.upsert_cpu_high("node-1", "Server 1", fake_first_high))

            # Sustained high 5m CPU -> should alert
            asyncio.run(scheduler.check_cpu_load(bot_mock))
            bot_mock.send_message.assert_called_once()
            alert_text = bot_mock.send_message.call_args[1]["text"]
            assert "Высокая загрузка CPU!" in alert_text
            assert "Загрузка (5 мин): <b>90.0%</b>" in alert_text
            assert "Загрузка (15 мин): <b>75.0%</b>" in alert_text

            cpu_high = asyncio.run(db_module.get_cpu_high("node-1"))
            assert cpu_high["alerted"] is True

        # 3. CPU drops back to normal, should alert recovery
        bot_mock.send_message.reset_mock()
        with patch("scheduler.api.get_node", AsyncMock(return_value=mock_get_node_normal)):
            asyncio.run(scheduler.check_cpu_load(bot_mock))
            bot_mock.send_message.assert_called_once()
            recovery_text = bot_mock.send_message.call_args[1]["text"]
            assert "Загрузка CPU нормализовалась" in recovery_text
            assert "Загрузка (5 мин): <b>50.0%</b>" in recovery_text
            assert "Загрузка (15 мин): <b>75.0%</b>" in recovery_text
            assert asyncio.run(db_module.get_cpu_high("node-1")) is None


