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
