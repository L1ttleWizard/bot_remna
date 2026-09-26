# Architectural Decision Record: Subscription Listing & Date Parsing Robustness

## Context
Users encountered crashes and unresponsive buttons when attempting to view subscriptions («Сломался список под в боте»).
Root causes:
1. Direct `int(expire_date)` conversions on fields stored as ISO strings (`"2026-10-01T00:00:00Z"`), string floats (`"1780000000.0"`), or ms timestamps in SQLite, which raised uncaught `ValueError` during callback rendering (`format_sub_caption`, `_render_sub_open`, `_send_admin_user_card`, `_send_admin_sub_open`, `_send_admin_users_list`).
2. Fixed-length tuple unpackings in `format_sub_caption` (expected 7 fields) when called on 9-field tuples from `db.get_subscription`.
3. `html.escape(sub[2])` crashing with `AttributeError` when `uuid` was `None`.
4. Overly restrictive `ensure_authorized_user` blocking users in `cb_my_subs` and causing blank alerts instead of displaying the empty subscription state with back navigation.

## Decision
1. **Universal Parsing & Formatting**:
   - Implemented `parse_expire_to_ts(val)` to handle `int`, `float`, ISO 8601 strings, string numbers, and ms timestamps, returning integer UTC timestamps.
   - Implemented `safe_format_expire_date(val, fmt)` which guarantees zero exceptions and returns formatted date strings or `None`.
2. **Polymorphic Subscription Captioning**:
   - `format_sub_caption` now handles 7-tuples, 9-tuples, and dicts interchangeably without throwing `ValueError`.
3. **Defensive Admin Renderers**:
   - Fixed `html.escape(sub[2] or '—')` and date parsing across all admin user and subscription cards.
4. **UX & Authorization Decoupling**:
   - Decoupled `cb_my_subs` from blocking alert dialogs so users without subscriptions see an informational prompt with a back button.
   - Updated `auth.is_authorized` and `ensure_authorized_user` to recognize users with active subscriptions even if primary `uuid` is unset or user is an admin.
5. **Database Auto-Sanitization**:
   - Added startup sanitization in `database.py:init_db()` to automatically convert any non-integer expiration timestamps in SQLite to standard unix timestamps.

## Consequences
- The subscription list in both user mode (`my_subs`) and admin mode (`admin_users`, `_send_admin_sub_open`) is fully resilient against varied date representations.
- Zero crashes on legacy or partially linked subscriptions.
- All unit tests pass cleanly (125/125).
