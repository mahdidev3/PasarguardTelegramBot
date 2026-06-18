# HowTooSee / Pasarguard Bot Phase Plan

## Completed
- Phase 0: multi-file architecture cleanup.
- Phase 1: PostgreSQL/SQLAlchemy foundation, database models, complete ticket system, numeric second confirmation.
- Phase 1.1: ticket bugfixes, attachment viewing, attachment cleanup on close.
- Phase 2: DB-managed plans, editable texts, broadcast with media/file/custom buttons.
- Phase 3: CSV/Excel exports, full backup, full restore, usage reports.
- Phase 3.1: scheduled backups, real ticket media backup, ticket media access cleanup on close.
- Phase 4.0-4.4: Pasarguard health/auth client, sync models, template governance, plan-to-template sync.
- Phase 4.5-4.6: real purchase integration via Pasarguard templates, remote user creation, renewal/addon/status/link/reset remote operations.
- Phase 4.7: pull sync usage/status/expire/subscription URL from Pasarguard to bot and template drift detection.
- Phase 4.8: Pasarguard backup/restore checkpoint with actual_state, desired_state, dry-run restore/reconcile.
- Phase 4.9: full Pasarguard admin panel controls: overview, health, template sync, user sync, current-state reconcile, orphan users, sync logs, remote snapshots.

## Next possible hardening after Phase 4.9
- Permission-aware UI based on current admin details if Pasarguard exposes exact permissions.
- Import orphan users into bot records, only after explicit admin confirmation.
- Scheduled Pasarguard reconcile, disabled by default.
- More exact usage restore if Pasarguard adds safe API for setting non-zero usage.

## Project rules
- Use Pasarguard templates for bot-created users.
- Every Pasarguard user created by the bot must have data_limit/traffic configured.
- Bot-managed templates must be recognizable by marker/prefix/name mapping.
- Bot plans and Pasarguard templates must stay synced.
- Bot services and Pasarguard users must stay synced.
- No real remote deletion by default; use disable/suspend unless explicitly changed later.
- Dangerous operations require numeric confirmation.
