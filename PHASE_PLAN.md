# Phase Plan — HowTooSee/Pasarguard Bot

## Done
- Phase 0: multi-file architecture cleanup.
- Phase 1: PostgreSQL/SQLAlchemy foundation, DB models, ticket system, numeric confirmation.
- Phase 1.1: ticket file display/fix cleanup.
- Phase 2: DB-backed plans/texts/broadcasts.
- Phase 3: CSV/XLSX reports, full backup/restore, usage reports.
- Phase 3.1: scheduled backups, real ticket-file backups, ticket file cleanup after close.
- Phase 4.0–4.4: Pasarguard client, sync models, template governance, plan-template sync.
- Phase 4.5–4.6: remote user creation from template and service operations.
- Phase 4.7: pull-sync usage/status/expire/subscription_url from Pasarguard.
- Phase 4.8: Pasarguard backup/restore actual_state and desired_state.
- Phase 4.9: full Pasarguard admin panel.
- Phase 4.10: user-flow realization; payment remains demo, activation/provisioning is real.

## Phase 4.10 scope
- Payment methods remain demo/manual for now.
- After payment, service activation must provision a real Pasarguard user from the plan template.
- If Pasarguard is enabled and provisioning fails, the service is not activated as a fake/local service.
- Free trial services also provision real Pasarguard users from free templates.
- Renew/addon flows push changes to Pasarguard before reporting success.
- Subscription URLs shown to users must come from Pasarguard when Pasarguard is enabled.
- Coupon validation uses the database/admin coupon system, not hardcoded demo coupons.
- Admin mark-paid/manual service flows also go through real provisioning.

## Next likely work
- Hardening after real server tests.
- Better rollback for failed renew/addon local state.
- Orphan import tools.
- Scheduled reconcile if needed.

## Phase 4.10.1 Hotfix

- Fixed legacy catalog bridge globals (`PLANS`, `FREE_TEST_PLANS`, `DATA_ADDON_PACKAGES`, `FREE_SERVICE_TYPES`) that were accidentally removed during Phase 4.10 user-flow refactor.
- The buy plan/category flow can now read the DB-synced plan dictionaries again after bootstrap.


### Phase 4.10.1 — User-flow polish before next hardening step

- Normalize Pasarguard relative subscription paths to full URLs.
- Make `پنل اشتراکی` clickable in service detail/subscription views.
- Remove plan-key username suffix from Pasarguard templates.
- Make generated service-name tail numeric-only and env-controlled.
- Add dynamic plan category management to admin panel and user buy/renew menus.

## Phase 4.10.2 — Card-to-card Payment Review

- Customer-facing Pasarguard/internal technical messages are removed from user flows.
- Payment method page now exposes card-to-card payment instead of demo-paid button.
- Multiple payment cards can be configured in the admin panel; an active card is selected randomly for each payment attempt.
- Users must upload a receipt photo/document; non-receipt messages are rejected until they send a receipt or cancel.
- Receipts are stored, copied to sales admins, and orders move to `receipt_pending`.
- Sales admins can view, approve, or reject receipts and add a note for either outcome.
- Approval triggers the real provisioning path; rejection notifies the user with the admin note.
