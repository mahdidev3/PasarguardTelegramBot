
# HowTooSee/Pasarguard Bot — Phase 4.10

This checkpoint turns user-facing service flows into real Pasarguard-backed flows while keeping payment itself in demo/manual mode.

## What changed

- New purchases: payment can still be demo, but service activation now provisions a real Pasarguard user from the synced template.
- Free service: no longer local-only when Pasarguard is enabled; it creates a real remote user from a free template.
- Renew: applies the selected plan template to the existing remote user and resets usage.
- Add volume: updates the remote `data_limit` instead of only changing local data.
- Subscription link: when Pasarguard is enabled, the bot does not expose local/fake subscription URLs.
- Coupons: user flow now validates DB/admin-created coupon records and no longer uses hardcoded demo coupons.
- Admin mark-paid/manual service: also triggers provisioning and reports failures.

## Important production rules

Set these for real activation:

```env
PASARGUARD_ENABLED=true
PASARGUARD_DRY_RUN=false
PASARGUARD_TEMPLATE_GROUP_IDS=<real group ids>
```

If `PASARGUARD_ENABLED=true` and provisioning fails, the service will stay in `provisioning_failed` and no fake link will be shown.

Payment gateways/card-to-card receipt flows are intentionally still demo/manual and will be handled later.

## Phase 4.10.1 Hotfix

- Fixed legacy catalog bridge globals (`PLANS`, `FREE_TEST_PLANS`, `DATA_ADDON_PACKAGES`, `FREE_SERVICE_TYPES`) that were accidentally removed during Phase 4.10 user-flow refactor.
- The buy plan/category flow can now read the DB-synced plan dictionaries again after bootstrap.


## Phase 4.10.1 — Subscription Link, Username, Dynamic Categories Hotfix

This patch fixes three production user-flow issues before continuing:

- Subscription URLs returned by Pasarguard are normalized to a full URL using `PASARGUARD_BASE_URL` when the API returns a relative path such as `/sub/...`.
- Service details and the subscription page now expose a clickable `پنل اشتراکی` link/button that points to the full subscription URL.
- Pasarguard templates no longer append the plan key to usernames, so new users will not get suffixes like `_m_10`. Operators can still set a global suffix with `PASARGUARD_USERNAME_SUFFIX`, but the default is empty.
- Auto-generated service-name tails are numeric-only. `SERVICE_NAME_PREFIX` is now read from env, so it can be set to an empty string if fully numeric service names are desired.
- Plan categories are now dynamic. Admins can create/edit/enable/disable categories from the plan management panel, and user buy/renew menus are generated from database categories instead of hardcoded `monthly`/`quarterly` buttons.

Recommended env for no extra username prefixes/suffixes:

```env
SERVICE_NAME_PREFIX=
PASARGUARD_USERNAME_PREFIX=
PASARGUARD_USERNAME_SUFFIX=
```

After changing template username prefix/suffix rules, run:

`👑 پنل مدیریت → 🔌 Pasarguard → 🧪 Dry-run سینک Templateها`

Then apply template sync after reviewing the diff.

### Phase 4.10.2 — Card-to-card payments

New admin menu:

`👑 پنل مدیریت → 💳 روش‌های پرداخت`

Add cards with:

`card_number | owner_name | bank_name | note | active`

Example:

`6037991234567890 | علی رضایی | ملی | کارت فروش اصلی | 1`

Receipt notifications go to admins with role `sales` and `super`. You can also set specific sales recipients in `.env`:

`SALES_ADMIN_CHAT_IDS=123456789,987654321`

The old demo payment button is no longer displayed in the user payment page. The payment itself is manual/card-to-card, while activation after approval uses the real provisioning flow.






