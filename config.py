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


def _parse_admin_ids(raw: str) -> set[int]:
    out: set[int] = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.add(int(chunk))
        except ValueError:
            continue
    return out


# Список Telegram ID администраторов через запятую (например: "12345,67890").
# Эти ID будут получать роль admin при старте бота.
ADMIN_TG_IDS: set[int] = _parse_admin_ids(os.environ.get("ADMIN_TG_IDS", ""))

# Параметры по умолчанию для выдаваемых токенов (если админ не указал явно).
DEFAULT_TOKEN_EXPIRE_DAYS = int(os.environ.get("DEFAULT_TOKEN_EXPIRE_DAYS", "30"))
DEFAULT_TOKEN_HWID_LIMIT = int(os.environ.get("DEFAULT_TOKEN_HWID_LIMIT", "3"))
