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
- `GET /api/bandwidth-stats/nodes`: Returns aggregate traffic series across all nodes with params `start`, `end` (returns `{categories: [...], series: [{uuid, name, countryCode, color, total, data}], sparklineData: [...]}`).
- `GET /api/bandwidth-stats/nodes/{nodeUuid}/users`: Returns user traffic ranking for a specific node with params `start`, `end`, `topUsersLimit` (returns `{topUsers: [{color, userId, username, total}]}`).
- `GET /api/system/stats/bandwidth`: System-wide traffic comparisons across 2d, 7d, 30d, current month, and current year.

### 4. Nodes & Squads
- `GET /api/nodes`: Lists all nodes and their status (`isConnected`, `isDisabled`, `address`, `name`).
- `GET /api/internal-squads`: Lists internal squads (profiles) available in the panel.

### 5. Infra-Billing
- `GET /api/infra-billing/providers`: Lists hosting providers (`uuid`, `name`, `loginUrl`, `billingUrl`, `faviconLink`).
- `GET /api/infra-billing/nodes`: Lists node billing records (`uuid`, `nodeUuid`, `providerUuid`, `nextBillingAt`, nested `node` and `provider`).
- `POST /api/infra-billing/nodes`: Creates node-to-provider billing association.
- `PATCH /api/infra-billing/nodes`: Updates billing properties (e.g. `nextBillingAt`).
- `DELETE /api/infra-billing/nodes/{uuid}`: Unlinks a billing association.

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

### Node Billing Notifications Contract
- **Trigger**: Automated schedule at 10:00 MSK (`check_billing_nodes_expiration`) or manual check via `admin_notify_test_billing`.
- **Calculation**: Computes exact calendar day difference between MSK today and `nextBillingAt`.
- **Deduplication**: Recorded in SQLite `billing_notification_log` by composite key `(billing_uuid, days_left, billing_date)`. Renewals with a new billing date start a fresh notification cycle.
- **Payload**: Country flag, node name, hosting provider, formatted expiration date, days remaining badge, direct link to provider control panel, and inline shortcut to node settings.



