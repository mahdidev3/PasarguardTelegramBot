# Development phases

The project will move forward only after the user tests each phase and confirms.

## Phase 0 — Architecture cleanup

- Keep runtime behavior stable.
- Move current single-file implementation to `app/legacy_bot.py`.
- Keep `python main.py` as the entrypoint.
- Add package folders for routers, services, repositories, keyboards, utils, schemas and migrations.
- Prepare clear boundaries for the next phases.

## Phase 1

- PostgreSQL migration.
- Database models.
- Complete ticket system.
- Numeric second confirmation for dangerous admin actions.

## Phase 2

- Full plan management from database.
- Editable texts/messages from admin panel.
- Broadcast system with photo/file/custom buttons.

## Phase 3

- CSV/Excel exports.
- Full backup.
- Full restore.
- Usage report in backups.

## Phase 4 — Future Pasarguard integration

- Pasarguard backup adapter.
- Pasarguard restore/reconcile adapter.
- Create/update remote services.
- Sync data usage.
