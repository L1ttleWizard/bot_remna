# Architectural Decision Record: Remnawave Automated Panel Backup Cron Fix

**Date**: 2026-09-26  
**Status**: Accepted  

## Context

While manual backup of the Remnawave panel via `/opt/rw-backup-restore/backup-restore.sh backup` worked properly and sent archives to the Telegram group (`-1003983882002`), automated scheduled backups were failing to execute. In the Telegram group, only the bot's daily backup was being received.

Inspection of the host system revealed:
1. In `/opt/rw-backup-restore/config.env`, `CRON_TIMES="@daily"` was configured, causing the interactive utility to report auto-send as enabled.
2. Root's system crontab (`crontab -l`) was completely empty (`no crontab for root`). The cron job had either never been successfully installed or was wiped during server migration/update.
3. In `backup-restore.sh`, saving the crontab lacked a mandatory trailing newline required by Debian's `crontab` binary (`new crontab file is missing newline before EOF, can't install.`), which silently failed to register the schedule while still writing `CRON_TIMES` to `config.env`.
4. `BOT_BACKUP_ENABLED="true"` with an empty `BOT_BACKUP_SELECTED` caused an error log (`[ERROR] Неизвестный бот: `) on every run, attempting to duplicate the bot's independent backup mechanism.

## Decision

1. **Root Crontab Installation**:
   - Installed the daily backup schedule into root's crontab with proper environment variables and required trailing newline:
     ```cron
     SHELL=/bin/bash
     PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin
     @daily /opt/rw-backup-restore/backup-restore.sh backup >> /var/log/rw_backup_cron.log 2>&1
     ```
2. **Log Management & Rotation**:
   - Created `/var/log/rw_backup_cron.log` for execution logs.
   - Configured `/etc/logrotate.d/rw-backup` with weekly rotation, 4-week retention, and compression.
3. **Decouple Bot Backup in Remnawave Utility**:
   - Set `BOT_BACKUP_ENABLED="false"` in `/opt/rw-backup-restore/config.env`, ensuring Remnawave's backup utility focuses purely on full PostgreSQL database dumps and `/opt/remnawave` configurations (`remnawave_backup_panel_*.tar.gz`), eliminating harmless bot-check errors.
4. **Symlink Convenience**:
   - Ensured `/usr/local/bin/rw-backup` symlink points to `/opt/rw-backup-restore/backup-restore.sh` for convenient manual triggers.

## Consequences

- Automated backups of the Remnawave panel now trigger every day at midnight (`@daily`) via system cron daemon.
- Complete logs are persisted in `/var/log/rw_backup_cron.log` and automatically rotated.
- The resulting `.tar.gz` archive containing full PostgreSQL database dump (`dump_*.sql.gz`) and `/opt/remnawave` configuration files is uploaded directly to Telegram chat `-1003983882002`.
