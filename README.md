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

