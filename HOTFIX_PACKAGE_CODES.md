# HOTFIX: Package Code / Assigned Package Flow

This build adds an admin-managed package-code system on top of the existing wallet-only payment model.

## Added

- Admin menu: `🎁 کد پکیج‌ها`.
- Package templates with:
  - name
  - package code
  - package price, including free packages
  - description
  - conditions
  - maximum number of subscriptions the buyer can create
  - one or more package plan items
- Package plan items can be added:
  - from existing sales plans
  - from existing sales plans with override for title, traffic, duration, and sub price
  - manually with title, traffic, duration, and sub price
- Assignment flow:
  - Admin chooses a package and a user chat ID.
  - Admin can customize package price, max sub count, description, conditions, and assignment code for that specific user.
  - Admin previews and confirms before sending to the user.
  - The user sees the final package details only; the message does not mention whether it was overridden.
- User flow:
  - New user menu button: `🎁 پکیج‌های من`.
  - User can accept or decline an offered package.
  - Package purchase has no discount-code button.
  - Package purchase uses wallet payment only.
  - After payment, user can create subscriptions from package items until the package sub limit is reached.
  - Created subscriptions are normal services and can be managed from `📦 سرویس‌های من`.
- Admin access:
  - Admins with the `packages` permission can create, list, activate/deactivate, assign, and inspect package assignments and package-created subscriptions.

## Database tables added to the legacy SQLite layer

- `package_templates`
- `package_template_items`
- `user_packages`
- `package_subscriptions`

## SQLite columns added to `services`

- `package_assignment_id`
- `package_item_id`

## Order types added

- `pkg_assign:<user_package_id>` — buying/activating the assigned package shell.
- `pkg_sub:<user_package_id>:<package_item_id>` — creating a subscription from a purchased package.

## Notes

- Package and package-sub orders intentionally do not support coupon codes.
- Wallet top-up is still handled through the existing card-to-card receipt flow.
- For package sub creation, a CatalogPlan key like `pkg_item_<item_id>` is upserted so Pasarguard template creation can work with the existing `ensure_template_for_plan` path.
