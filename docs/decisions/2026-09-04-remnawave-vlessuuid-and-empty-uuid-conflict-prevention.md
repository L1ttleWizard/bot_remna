# ADR: Remnawave vlessUuid Extraction & SQLite Empty UUID Conflict Prevention

## Context
In Remnawave 3.4+, user creation responses contain `vlessUuid` and `shortUuid`, omitting the legacy top-level `uuid` key. When registering users or creating trial accounts, attempting to read `api_data.get("uuid", "")` resulted in an empty string `""`.
Because `subscriptions.uuid` is marked `NOT NULL UNIQUE`, inserting empty UUIDs into `subscriptions` triggered `ON CONFLICT(uuid) DO UPDATE`, which caused newly provisioned subscriptions to overwrite any prior row with an empty UUID rather than creating a row for the new user. As a consequence, new users who activated a trial were recorded in `trial_claims` but ended up with 0 subscriptions in `subscriptions`, causing their subsequent `/start` or `connect` clicks to redirect to the guest / invitation-only screen.

## Decision
1. **Multi-Key UUID Extraction**: Updated `create_account_for_user` and bulk sync in `bot.py` to check `api_data.get("uuid") or api_data.get("vlessUuid") or api_data.get("shortUuid") or str(api_data.get("id"))` with fallback to `uuid.uuid4()`.
2. **Strict Guard on Empty UUIDs**: Added a mandatory `if uuid:` check in `database.add_user` before inserting into `subscriptions`.
3. **TG ID Conflict Update**: Included `tg_id = excluded.tg_id` in `ON CONFLICT(uuid) DO UPDATE SET` to guarantee consistency if an existing subscription changes ownership.
4. **Resolution Fallback**: Added fallback resolution logic in `remnawave_api.py` and `app.py` to resolve by `short_uuid` or `username` via the local SQLite cache whenever a full UUID is provided.
5. **Database Cleanup**: Cleaned up empty UUID rows and repaired disrupted user records in production SQLite.

## Consequences
- Trial activation and user registration now reliably persist non-empty UUIDs and shortUuids in `subscriptions`.
- New users immediately receive their connection guide and subscription details without being redirected to the invitation screen.
- Full backwards-compatibility with both legacy Remnawave panels and Remnawave 3.4+.
