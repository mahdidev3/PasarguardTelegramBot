# Hotfix: Durable Deadlines + Backup Recheck

## What was fixed

### 1) Durable deadline cleanup
A new service was added:

- `app/services/deadline_service.py`

It finalizes expired runtime windows from persistent storage, not from in-memory FSM state. This means deadlines still work correctly if the bot is down and later starts again.

The service runs:

- once on startup before polling starts
- every `DEADLINE_CLEANUP_INTERVAL_SECONDS` seconds, default `60`
- before every full backup
- before every automatic backup

### 2) Receipt deadline
`RECEIPT_UPLOAD_WINDOW_MINUTES` now defaults to `30` minutes.

Expired card-to-card receipt windows are now finalized in SQLite:

- `payment_receipts.status = 'expired'`
- linked order becomes `orders.status = 'expired'`
- unsent/unsubmitted receipt file references are removed from `payment_receipt_files`
- legacy receipt file columns are cleared
- no wallet transaction is created unless the receipt is approved
- expired wallet top-up attempts are hidden from the user transaction list

### 3) Numeric confirmation codes
The 5-minute confirmation code flow was already checking `expires_at` at verification time. It is now safer against timezone issues and old unused expired confirmations are cleaned after a retention window.

### 4) Automatic backup catch-up
Automatic backups still use persistent `auto_backup_next_run_at` and now:

- run cleanup before creating backup
- run immediately on startup if `next_run_at` was missed while the bot was down
- keep `next_run_at` unchanged on failure so it retries instead of silently skipping
- use a lock so two auto backups cannot overlap

### 5) Backup coverage rechecked
Backup version was bumped to `4`.

The backup still exports all SQLite tables dynamically and all PostgreSQL tables from SQLAlchemy metadata. Manifest now includes explicit coverage/counts for recent project additions:

- required channels
- package templates
- package template items
- user packages
- package subscriptions
- payment receipts
- receipt files
- deadline events
- admin confirmations
- bot settings

Backups also now run deadline cleanup before the ZIP is created so stale waiting receipts are not archived as actionable transactions.

## Required deployment note
Set this in the production `.env` if it already exists with the old value:

```env
RECEIPT_UPLOAD_WINDOW_MINUTES=30
```

Then restart the service.
