# ADR: Remnawave 3.4+ Subscription Linking Support

## Context
When administrators attempted to link an existing Remnawave account to a Telegram user via "➕ Привязать подписку из Remnawave", the picker displayed 0 users or failed with "Не удалось получить данные из панели".
Investigation revealed:
1. In Remnawave 3.4+, `GET /api/users` returns user objects with `vlessUuid`, `shortUuid`, and `id`, while `uuid` is `None`. The picker evaluated `u.get("uuid") or ""` which resulted in an empty string and skipped all users.
2. Passing raw `vlessUuid` in callback queries failed `api.get_user_info` because `/api/users/resolve` does not support `vlessUuid`, and unlinked users are not yet present in the local database.
3. Subscription deduplication only checked `subscriptions.uuid`, missing accounts bound by `short_uuid` or `username`.

## Decision
1. **Multi-Key Extraction in Picker**: In `_send_admin_link_picker`, read `uuid_v = u.get("uuid") or u.get("vlessUuid") or u.get("shortUuid") or str(u.get("id"))`.
2. **Compact & Resolvable Identifiers**: Use `shortUuid` (or numeric `id`) in `lnk:<tg>:<ident>` callback data, ensuring it remains well under Telegram's 64-byte callback limit and resolves immediately via Remnawave API.
3. **Robust Resolution Fallbacks**: In `remnawave_api.py`, added a fallback searching `list_users` for `vlessUuid` when an unlinked full UUID is passed.
4. **Multi-Column Lookup**: Added `find_subscription_by_any` in `database.py` and updated `find_subscription_by_uuid` to match by `uuid`, `short_uuid`, or `username`.
5. **Subscription Insertion**: On confirmation, extract and save `vlessUuid` as primary `uuid`, along with `shortUuid` and `username`.

## Consequences
- Administrators can seamlessly view unlinked Remnawave users and bind them to any Telegram account.
- Already linked users are properly marked and disabled in the picker.
- Backwards compatible with older and newer Remnawave versions.
