# Architectural Decision Record: Node Billing Notifications and Digest Toggles in Notification Center

**Date**: 2026-09-26  
**Status**: Accepted  

## Context

Administrators need:
1. Automated warnings before VPN nodes/servers run out of prepaid hosting time (`nextBillingAt` from Remnawave Infra-Billing).
2. The ability to toggle the daily traffic digest (the automated 23:59 cluster report) directly from the bot's Notification Center (`admin_notify_settings`).
3. The ability to toggle and customize node billing alerts (e.g. reminder intervals, manual check) from the bot's Notification Center.

Previously, node billing dates were only visible when inspecting individual node cards in `/nodes` or `/billing`, with no background notifications or expiration tracking. Additionally, the daily 23:59 traffic digest had no user-facing on/off switch in the settings UI.

## Decision

1. **Database Tracking & Deduplication (`database.py`)**:
   - Created SQLite table `billing_notification_log`:
     ```sql
     CREATE TABLE IF NOT EXISTS billing_notification_log (
         billing_uuid TEXT NOT NULL,
         days_left INTEGER NOT NULL,
         billing_date TEXT NOT NULL,
         sent_at INTEGER NOT NULL,
         PRIMARY KEY (billing_uuid, days_left, billing_date)
     );
     ```
   - Composite primary key `(billing_uuid, days_left, billing_date)` prevents duplicate notifications within the same cycle while allowing renewed dates (next month's billing) to seamlessly trigger new notifications.
   - Added automatic cleanup of logs older than 30 days during daily housekeeping.

2. **Automated Expiration Check (`scheduler.py` & `bot.py`)**:
   - Implemented `check_billing_nodes_expiration(bot, target_chat_id=None, force=False)`:
     - Calculates calendar day difference between MSK today and `nextBillingAt`.
     - Alerts admins for days matching `NODE_BILLING_NOTIFY_DAYS_KEY` (default: `7, 3, 1, 0`).
     - Sends rich Telegram alerts with country flag, server name, provider, expiration date, and inline buttons (`[💳 Панель провайдера]` URL and `[📀 Настройки ноды]` callback).
     - Targets `ADMIN_REPORT_CHAT_ID` -> `BACKUP_TG_CHAT_ID` -> `ADMIN_TG_IDS`.
   - Scheduled daily at 10:00 MSK (`BILLING_CHECK_CRON_HOUR`, `BILLING_CHECK_CRON_MINUTE`).

3. **Daily Digest Toggle (`DAILY_REPORT_ENABLED_KEY`)**:
   - `send_daily_traffic_report` checks `DAILY_REPORT_ENABLED_KEY != "0"` before scheduled execution at 23:59 MSK.
   - Manual admin commands (`/daily_report`) and instant buttons bypass this check, ensuring administrators can always generate test reports on demand.

4. **Notification Center Integration (`handlers/admin_notifications.py`)**:
   - Updated main menu `admin_notify_settings` to display live status badges across all categories:
     - 📅 Subscriptions (Client alerts & Admin digest)
     - 🖥 Servers (Node offline & CPU load)
     - 📊 Daily Digest 23:59 (On/Off)
     - 💳 Node Billing (On/Off)
     - 👥 Referrals (On/Off)
   - Added `📊 Дайджест (23:59)` submenu (`admin_notify_digest_menu`) with one-tap toggle and manual send button.
   - Added `💳 Биллинг` submenu (`admin_notify_billing_menu`) with one-tap toggle, reminder days FSM configuration, manual check button (`admin_notify_test_billing`), and a sorted list of upcoming node renewal deadlines.
   - Clarified admin subscription digest button text as «👑 Дайджест админам» to eliminate confusion with the daily traffic report.

## Consequences

- **Visibility**: Administrators receive timely warnings ahead of server renewals (7d, 3d, 1d, 0d), avoiding unexpected server shutdowns.
- **Granular Control**: Both cluster daily digest and server billing alerts can be toggled without editing `.env` or restarting the container.
- **Idempotency**: Notification logging ensures admins receive exactly one alert per configured threshold per billing cycle.
