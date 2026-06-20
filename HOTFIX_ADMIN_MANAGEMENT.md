# Hotfix: Admin management editing/removal/display names

## What changed

- Added `display_name` for admins.
- If an admin display name is empty, the bot uses the chat ID as the visible name.
- Admin add flow is now:
  1. chat ID
  2. role selection by buttons
  3. display name, or button to use chat ID as the name
- Admin list now shows each admin as a clickable row.
- Admin detail screen now supports:
  - edit display name
  - change role
  - delete/deactivate admin
  - restore inactive admin
- Self-removal is blocked.
- ENV bootstrap super-admin removal/role-change is blocked because it is restored on restart from `ADMIN_CHAT_IDS`.
- SQLite and PostgreSQL admin tables are both patched/synced.

## Database changes

SQLite legacy table `admins` now has:

```sql
ALTER TABLE admins ADD COLUMN display_name TEXT;
```

PostgreSQL runtime patch now has:

```sql
ALTER TABLE IF EXISTS admins ADD COLUMN IF NOT EXISTS display_name VARCHAR(120);
```

## Validation

Compiled with:

```bash
python3 -m compileall -q app main.py
```
