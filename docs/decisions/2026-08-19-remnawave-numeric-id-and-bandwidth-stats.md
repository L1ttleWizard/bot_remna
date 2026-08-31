# ADR: Migration to Numeric IDs and Resilient Bandwidth Statistics in Remnawave 2026+

## Context
Recent updates to the Remnawave backend panel altered user route contracts from UUID-based parameters to numeric integer IDs (`/api/users/{userId}`, `/api/hwid/devices/{userId}`, `/api/bandwidth-stats/users/{userId}`). Additionally, `GET /api/users` began returning raw lists in certain configurations rather than nested dictionary envelopes.

## Decision
1. **Dynamic Identifier Resolution**: Implemented `api.resolve_user(identifier)` to dynamically map UUID / shortUuid $\to$ numeric ID (`id`) when needed, caching known IDs during batch operations.
2. **Persistent Session & Connection Pooling**: Switched `remnawave_api.py` to a persistent `aiohttp.ClientSession` with SSL context reuse to avoid connection drops during concurrent requests.
3. **Resilient User Listing & Bandwidth Parsing**: Updated `_collect_panel_traffic` and `get_user_usage_range` to handle both dictionary and list response shapes and calculate fallback metrics seamlessly.

## Consequences
- Full backward compatibility with existing SQLite database storing UUIDs.
- Zero downtime or 400 Validation errors when interacting with updated Remnawave endpoints.
- Bandwidth top analytics and HWID device lists display without data loss.
