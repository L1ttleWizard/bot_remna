# API Contracts & Remnawave Integration

## Overview

The bot interacts with the Remnawave Panel API using bearer tokens. In Remnawave backend 2026+, user identification has been migrated to numeric IDs (`userId` / `id`), while preserving UUIDs as secondary metadata.

## Remnawave Endpoints

### 1. User Management
- `GET /api/users`: Fetches paginated user list. Returns `{"response": [...]}` or `{"response": {"users": [...], "total": ...}}`.
- `GET /api/users/{userId}`: Gets full user profile by numeric ID.
- `POST /api/users/resolve`: Resolves identifier to `{id, shortUuid, username}`. Panel accepts exactly one of `{"id": int}`, `{"shortUuid": str}`, or `{"username": str}` (passing `uuid` or `vlessUuid` results in 400 Bad Request; resolving UUIDs falls back to local database lookups for `shortUuid` or `username`).
- `POST /api/users`: Creates a new user with `username`, `expireAt`, `status`, `hwidDeviceLimit`, and optional `activeInternalSquads`. Returns envelope containing `id`, `shortUuid`, `vlessUuid`, `trafficLimitBytes`, `hwidDeviceLimit`.
- `PATCH /api/users`: Updates user attributes (e.g. `expireAt`, `hwidDeviceLimit`, `activeInternalSquads`). Requires `id` or `uuid`.
- `DELETE /api/users/{userId}`: Deletes user by numeric ID.

### 2. HWID Device Management
- `GET /api/hwid/devices/{userId}`: Lists active HWID devices for a user.
- `POST /api/hwid/devices/delete`: Deletes a device by payload `{"userId": int, "hwid": str}`.

### 3. Bandwidth Statistics
- `GET /api/bandwidth-stats/users/{userId}`: Returns user traffic history with params `start`, `end`, `topNodesLimit`.
- `GET /api/bandwidth-stats/nodes`: Returns aggregate traffic series across all nodes.

### 4. Nodes & Squads
- `GET /api/nodes`: Lists all nodes and their status (`isConnected`, `isDisabled`, `address`, `name`).
- `GET /api/internal-squads`: Lists internal squads (profiles) available in the panel.

## Bot Internal Contracts

### Self-Registration & Trial Flow
- **Trigger**: `/start`, `/trial`, deep link `?start=trial`, or `trial_claim` inline button.
- **Eligibility Check**: `db.has_claimed_trial(tg_id) == False` and `db.count_subscriptions(tg_id) == 0`.
- **Issuance**: Automatically provisions a 5-day subscription with 3 HWID devices in Remnawave and links it to `tg_id`.
- **Referral Tie-in**: If a pending referral was recorded (`referee_id == tg_id`), reward is granted upon trial activation.

### Admin Subscription Linking Flow (`admu:<tg>:link:<page>`)
- **Picker**: Fetches users from `GET /api/users`. In Remnawave 3.4+, records contain `vlessUuid`, `shortUuid`, and `id`.
- **Deduplication**: Filters out accounts already associated with any `tg_id` via `db.find_subscription_by_any(...)`.
- **Identifier**: Uses `shortUuid` (or numeric `id`) in callback data to avoid Telegram's 64-byte payload constraint and guarantee instant resolution via `GET /api/users/{id}` or `POST /api/users/resolve`.
- **Persistence**: Upon confirmation (`lnkok:<tg>:<ident>`), inserts into `subscriptions` using `vlessUuid` (as primary UUID), `shortUuid`, `username`, and `expireAt`.

### Subscription Listing & Date Standardization (`my_subs`, `format_sub_caption`)
- **Universal Date Parsing**: All subscription and expiration fields in SQLite and API envelopes (`expire_date`, `expireAt`) are parsed through `parse_expire_to_ts(val)` and formatted via `safe_format_expire_date(val, fmt)`.
- **Supported Formats**: Integer/float seconds, millisecond timestamps (`> 100_000_000_000`), string timestamps, and ISO 8601 strings (`2026-10-01T00:00:00Z`).
- **Tuple Polymorphism**: Formatters and renderers safely accept 7-tuples (from `list_subscriptions`: `id, uuid, short_uuid, username, expire_date, label, created_at`), 9-tuples (from `get_subscription`), and dicts.
- **Authorization Decoupling**: User subscription listing (`cb_my_subs`) queries `list_subscriptions(tg_id)` directly. Unregistered users receive the redemption instruction screen with a Back button instead of an alert popup.



