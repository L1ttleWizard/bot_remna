import asyncio
import os
import zipfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram import Bot

# Setup dummy environment to import scheduler
os.environ.setdefault("BOT_TOKEN", "123456:token")
os.environ.setdefault("REMNAWAVE_URL", "https://panel.example.com")
os.environ.setdefault("REMNAWAVE_TOKEN", "test-token")
os.environ.setdefault("SUB_DOMAIN", "sub.example.com")

import scheduler


def test_run_daily_backup_success(tmp_path):
    # Setup temporary DB path
    db_file = tmp_path / "test_backup.db"
    db_file.write_text("dummy database content")
    
    bot_mock = MagicMock(spec=Bot)
    bot_mock.send_document = AsyncMock()
    
    # Patch DB_PATH and config variables
    with patch("database.DB_PATH", str(db_file)), \
         patch("config.BACKUP_TG_CHAT_ID", 987654):
         
         # Execute backup
         success = asyncio.run(scheduler.run_daily_backup(bot_mock))
         
         assert success is True
         bot_mock.send_document.assert_called_once()
         call_args = bot_mock.send_document.call_args[1]
         assert call_args["chat_id"] == 987654
         assert "Резервная копия бота" in call_args["caption"]
         
         # Verify temporary file is cleaned up
         sent_doc = call_args["document"]
         assert not os.path.exists(sent_doc.path)


def test_run_daily_backup_no_chat_id():
    bot_mock = MagicMock(spec=Bot)
    with patch("config.BACKUP_TG_CHAT_ID", None):
        success = asyncio.run(scheduler.run_daily_backup(bot_mock))
        assert success is False
        bot_mock.send_document.assert_not_called()


def test_run_daily_backup_missing_db():
    bot_mock = MagicMock(spec=Bot)
    bot_mock.send_message = AsyncMock()
    
    with patch("database.DB_PATH", "nonexistent_db_path.db"), \
         patch("config.BACKUP_TG_CHAT_ID", 12345):
        success = asyncio.run(scheduler.run_daily_backup(bot_mock))
        assert success is False
        bot_mock.send_message.assert_called_once()
        assert "Ошибка при создании бэкапа" in bot_mock.send_message.call_args[1]["text"]
