# ADR: Free 3-Day Trial Self-Registration

## Context
To grow the user base, allow viral sharing in groups/channels, and enable new users to instantly test the VPN without requiring an administrator to manually issue one-time invite tokens.

## Decision
1. **Self-Service Trial Button**: Any unregistered user opening the bot is greeted with an instant trial activation button `[🎁 Получить тест на 3 дня]`.
2. **Abuse Prevention**: Added `trial_claims` table in SQLite to track Telegram user IDs that have claimed a trial, ensuring 1 trial per account.
3. **Automated Remnawave Provisioning**: Automatically provisions a 3-day account with a default 3 HWID devices limit in the Remnawave backend panel.
4. **Seamless Connection Hand-off**: Upon trial claim, the user is directly presented with the `connect` flow (deep-links for Happ/Streisand/V2Ray/v2rayN/Clash, QR codes, manual key copy).
5. **Referral Reward Integration**: When a referred user activates their trial, the referrer immediately receives their +7 days referral bonus.

## Consequences
- Immediate reduction in admin friction for onboarding new users.
- Safe from multi-claim abuse per Telegram user ID.
- Existing token redemption (`/redeem`) remains fully supported as a fallback/alternative.
