"""
Конфигурация из переменных окружения (12-factor). Секреты не хранятся в коде.
Локально можно положить файл .env (не коммитить) и установить python-dotenv.
"""
from __future__ import annotations

import os
from pathlib import Path


def _load_dotenv_file() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    env_path = Path(__file__).resolve().parent / ".env"
    if env_path.is_file():
        load_dotenv(env_path)


_load_dotenv_file()


def require_env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise RuntimeError(f"Не задана обязательная переменная окружения: {name}")
    return v


# Обязательные
BOT_TOKEN = require_env("BOT_TOKEN")
REMNAWAVE_URL = require_env("REMNAWAVE_URL").rstrip("/")
REMNAWAVE_TOKEN = require_env("REMNAWAVE_TOKEN")
SUB_DOMAIN = require_env("SUB_DOMAIN").rstrip("/")

# Опциональные
DATABASE_PATH = os.environ.get("DATABASE_PATH", "bot_database.db")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_FILE_PATH = os.environ.get("LOG_FILE_PATH", "").strip() or None
SCHEDULER_CRON_HOUR = int(os.environ.get("SCHEDULER_CRON_HOUR", "12"))
SCHEDULER_CRON_MINUTE = int(os.environ.get("SCHEDULER_CRON_MINUTE", "0"))
SCHEDULER_TIMEZONE = os.environ.get("TZ", "Europe/Moscow")
