# ADR: Trial Duration Extended to 5 Days

## Context
The trial period was originally configured for 3 days. To improve onboarding conversion, provide users sufficient time to experience service stability across weekdays and weekends, and enhance viral sharing, the default trial duration has been increased to 5 days.

## Decision
1. **Config Default**: Changed `DEFAULT_TRIAL_EXPIRE_DAYS` default from `3` to `5` in `config.py`.
2. **Grammar & Localization**: Added `format_days_ru(days: int)` helper in `keyboards.py` to properly handle Russian pluralization ("5 дней" vs "3 дня" vs "1 день").
3. **UI Texts & Buttons**: Updated greeting messages, inline button labels (`[🎁 Получить тест на 5 дней]`), and activation confirmation alerts across `bot.py` and `keyboards.py`.
4. **Docs Sync**: Updated `docs/api.md`, `docs/architecture.md`, and `docs/setup.md` to reflect the 5-day trial period.

## Consequences
- New users claiming a trial automatically receive a 5-day active period with up to 3 HWID devices.
- Fully configurable via `DEFAULT_TRIAL_EXPIRE_DAYS` in environment variables if further tuning is needed.
