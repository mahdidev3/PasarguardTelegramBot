# Package Admin Management Hotfix

## What changed

- Added full admin management for assigned user packages.
- Admins can now open a user's assigned packages from the user profile.
- Admins can view all assigned packages from the package admin panel.
- The package assignment list is now clickable instead of plain text.
- Each assigned package now has a management page with:
  - package details
  - user chat id
  - current status
  - created subscription count
  - subscription/service list
  - user profile shortcut
- Admins can revoke/remove an assigned package from a user in four modes:
  - revoke package only + notify user
  - revoke package only silently
  - revoke package and disable/delete created subscriptions + notify user
  - revoke package and disable/delete created subscriptions silently
- When a package is revoked but subscriptions are kept, created services remain untouched.
- When subscriptions are removed, service rows are marked `deleted`, not physically removed, and Pasarguard status sync is attempted.
- Pending unpaid package orders are cancelled when the package is revoked.
- Revoked packages are hidden from the user's `🎁 پکیج‌های من` view.

## Safety

Services and assignment records are not physically deleted from the database. This preserves auditability and backup safety.

## Validation

`python3 -m compileall -q app main.py` passed.
