# Architectural Decision Record: Daily 23:59 Admin Traffic Report with Distribution Chart

**Date**: 2026-09-26  
**Status**: Accepted  

## Context

Administrators need daily visibility into cluster-wide traffic consumption at the close of each day (23:59 MSK), covering:
1. Total cluster traffic for the day.
2. Visual distribution chart of traffic across all active nodes.
3. Node-by-node breakdown of traffic (volume, percentage of cluster total, country flag).
4. Top user by traffic on each individual node.
5. Overall #1 top user by traffic across the entire cluster for that day.

Previously, traffic data was only accessible through manual navigation into the `/stats` menu or 30-day aggregate sparklines, without automated daily reporting, per-node user rankings, or Telegram channel dispatch.

## Decision

1. **Automated Daily Trigger (23:59 MSK)**:
   - Configured APScheduler cron job in `bot.py` invoking `scheduler.send_daily_traffic_report` at `hour=23`, `minute=59` in `SCHEDULER_TIMEZONE` (`Europe/Moscow`).
   - Configured fallback hierarchy for report recipient: `ADMIN_REPORT_CHAT_ID` -> `BACKUP_TG_CHAT_ID` (`-1003983882002`) -> individual `ADMIN_TG_IDS`.

2. **Data Aggregation via Parallel Panel Requests**:
   - `collect_daily_traffic_data(raw_date)` queries `GET /api/bandwidth-stats/nodes?start=YYYY-MM-DD&end=YYYY-MM-DD` for the date.
   - For all active nodes in the cluster, queries `GET /api/bandwidth-stats/nodes/{nodeUuid}/users?start=YYYY-MM-DD&end=YYYY-MM-DD&topUsersLimit=10` concurrently using `asyncio.gather`.
   - Node-level top user is identified as the highest-traffic user for that node.
   - Overall cluster top user is calculated by aggregating user consumption across all nodes.
   - Users are enriched with Telegram details (`@username`, full name, Telegram user ID) through SQLite lookups (`subscriptions` and `users` tables).

3. **Cyberpunk Donut Distribution Chart (`services/chart_generator.py`)**:
   - Implemented `generate_daily_nodes_distribution_chart` using Matplotlib in dark cyberpunk style (`#121214` background, neon palette: cyan, magenta, lime, amber, purple).
   - Embedded total daily traffic KPI callout in the center donut cutout.
   - Sanitized emoji flags from Matplotlib labels using regex to eliminate font missing glyph warnings.

4. **Message Formatting & Photo Delivery**:
   - Implemented `format_daily_report_text(report_data)` returning formatted HTML caption and optional follow-up message if total characters exceed Telegram's 1024-character photo caption limit.
   - Exposed manual on-demand trigger via `/daily_report` command (supports `/daily_report [send] [YYYY-MM-DD]`) and inline button in the nodes analytics keyboard (`admin_stats:daily_report_now`).

## Consequences

- **Performance**: Fetching node stats and user rankings concurrently for 8-10 nodes executes in ~200-300 ms on the local network.
- **Reliability**: If any individual node fails or is offline, `asyncio.gather(*, return_exceptions=True)` safely processes remaining nodes without aborting the report.
- **Delivery**: If `ADMIN_REPORT_CHAT_ID` is not explicitly configured, it seamlessly falls back to the existing backup channel (`BACKUP_TG_CHAT_ID`), preventing silent delivery failures.
