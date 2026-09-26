# Setup & Deployment Guide

## Requirements
- Python 3.11+
- Docker & Docker Compose
- Remnawave Backend API instance

## Environment Variables (`.env`)
```env
BOT_TOKEN=your_telegram_bot_token
ADMIN_IDS=123456789,987654321
REMNAWAVE_API_URL=https://your-panel-domain.com
REMNAWAVE_API_TOKEN=your_api_jwt_token
DATABASE_PATH=bot_database.db
SUB_DOMAIN=sub.your-domain.com
DEFAULT_TRIAL_EXPIRE_DAYS=5
DEFAULT_TRIAL_HWID_LIMIT=3

# Daily Traffic Report (23:59 MSK)
ADMIN_REPORT_CHAT_ID=-1003983882002  # Target group/channel ID (defaults to BACKUP_TG_CHAT_ID or ADMIN_IDS)
DAILY_REPORT_CRON_HOUR=23            # Hour in SCHEDULER_TIMEZONE (default: 23)
DAILY_REPORT_CRON_MINUTE=59          # Minute in SCHEDULER_TIMEZONE (default: 59)

# Automated Backups
BACKUP_TG_CHAT_ID=-1003983882002
BACKUP_CRON_HOUR=1
BACKUP_CRON_MINUTE=0
```

## Running with Docker Compose
```bash
docker compose up -d --build
```

## Running Locally for Development
```bash
python -m venv .venv
# On Windows:
.venv\Scripts\activate
# On Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt
python bot.py
```

## Running Tests
```bash
pytest
```
