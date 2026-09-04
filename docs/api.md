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
- **Issuance**: Automatically provisions a 3-day subscription with 2 HWID devices in Remnawave and links it to `tg_id`.
- **Referral Tie-in**: If a pending referral was recorded (`referee_id == tg_id`), reward is granted upon trial activation.

