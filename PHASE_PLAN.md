# HowTooSee Bot Phase Plan

## Current checkpoint: Phase 2

Done in Phase 0:
- Split project structure and kept the legacy runtime stable.

Done in Phase 1:
- PostgreSQL/SQLAlchemy foundation.
- Core models.
- Complete ticket system.
- Numeric second confirmation.
- Ticket bugfixes for category selection, media viewing, assignee labels, and duplicate last-message rendering.

Done in Phase 2:
- Database-backed plan catalog.
- Admin plan management.
- Database-backed data-addon management.
- Legacy buy catalog sync from PostgreSQL.
- Editable text/message templates from admin panel.
- Media/file/video/voice broadcast campaigns.
- URL button support for broadcasts.
- Broadcast preview and numeric confirmation before final send.
- Broadcast delivery recipient tracking.

Next Phase 3:
- CSV/Excel exports.
- Full backup.
- Full restore.
- Usage report inside backup.

Future Phase 4:
- Pasarguard backup/restore adapter.
- Pasarguard remote service create/update/reconcile.
- Data usage/time remaining sync.

## Phase 4.5/4.6 checkpoint

- Connect paid service creation to Pasarguard `create_user_from_template`.
- Store remote username, remote id, template id, subscription URL and sync status in legacy SQLite service rows and PostgreSQL mapping tables.
- Prefer Pasarguard subscription URL in user/admin service displays.
- Connect renewal to Pasarguard template re-apply.
- Connect addon volume changes to Pasarguard data_limit updates.
- Connect service status/delete/refund to safe Pasarguard disable/enable updates.
- Connect link revoke and usage reset to Pasarguard operations.
- Keep `PASARGUARD_DRY_RUN=true` as a safe no-op mode.

## Phase 4.8 — Pasarguard backup/restore checkpoint

Status: implemented in this package.

- Backups now include Pasarguard actual templates/users when `PASARGUARD_ENABLED=true`.
- Backups include desired Pasarguard templates/users derived from bot plans/services.
- Restore upload shows a Pasarguard dry-run reconcile report before confirmation.
- Confirmed restore runs database restore first, then Pasarguard reconcile.
- With `PASARGUARD_DRY_RUN=true`, reconcile only reports actions.
- With `PASARGUARD_DRY_RUN=false`, reconcile creates/updates templates/users and never remote-deletes users.
- Exact non-zero usage restore is reported when the API cannot safely set usage exactly; zero usage is restored with reset.
