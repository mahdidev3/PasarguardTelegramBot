# Hotfix: private assigned packages, durable jobs, and backup summaries

## What changed

1. **Assigned-package visibility**
   - The user main menu no longer shows `🎁 پکیج‌های من` unless the user has an actionable package assigned by an admin.
   - The user package list only shows `offered`, `pending_payment`, and `active` packages.
   - Package templates remain admin-only. Public services/plans are unchanged.

2. **Durable periodic jobs**
   - Added `app/services/job_service.py`.
   - Added the `scheduled_jobs` SQLite table with catch-up aware `next_run_at`, `last_run_at`, `last_summary`, and `last_error`.
   - Added default jobs:
     - `receipt_deadline_cleanup`: finalizes expired pending receipt/order windows so they no longer appear in user transactions.
     - `closed_ticket_cleanup`: deletes tickets that have been closed for more than 24 hours.
   - Jobs run after restart if their `next_run_at` passed while the bot was down.

3. **Admin job management**
   - Added `⏱ مدیریت Jobها` to the super-admin panel.
   - Super-admin can:
     - see all jobs,
     - run a job manually,
     - enable/disable a job,
     - edit job interval in minutes.

4. **Backup summary message**
   - Added `render_backup_summary()` in `backup_service.py`.
   - Manual backup now sends the backup ZIP and then a separate summary message.
   - Automatic backup also sends a separate summary message after the ZIP.
   - The summary includes users, services, orders, ticket files, receipt files, package counts, required channels, scheduled jobs, deadlines, and Pasarguard desired/actual counts.

## Safety notes

Expired receipt/order windows are made invisible to users by marking them expired and filtering them from user transaction lists. This keeps an internal audit trail while matching the user-visible requirement that old pending receipt rows disappear from `💳 تراکنش‌های شما`.
