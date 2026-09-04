# ADR: Streamlined Connect Flow & 3-Device HWID Limit

## Context
The previous connection flow displayed all available VPN clients (up to 5 per platform) with multiple raw URLs, deep links, copy buttons, and an unprompted QR photo in a single, overloaded message. This caused cognitive overload and confusion for end users. Additionally, device limits needed standardization to 3 devices across trial and standard provisions.

## Decision
1. **Curated 3-Step Guide per Platform**: Present 1 primary recommended client (e.g., Happ for iOS/Android/macOS, Hiddify for Windows/Linux) structured in 3 clear steps (Install -> Import -> Connect).
2. **Action-Oriented Inline Controls**:
   - Direct store install buttons (App Store / Google Play / GitHub).
   - One-tap clipboard copy for import deep-links and subscription URLs.
   - Dedicated clean QR-code viewer (connect_qr).
3. **Expandable Alternative Clients Menu**: Alternative clients (V2Box, Streisand, Karing, Shadowrocket, NekoRay, v2rayNG) are accessible on-demand under [⚙️ Другие приложения] without cluttering the primary view.
4. **Standardized 3-Device HWID Limit**: Set DEFAULT_TRIAL_HWID_LIMIT = 3 and DEFAULT_TOKEN_HWID_LIMIT = 3.

## Consequences
- Significant improvement in onboarding conversion and user clarity.
- Clean message lifecycle and reduced chat clutter in Telegram.
- Advanced users retain access to alternative clients.
