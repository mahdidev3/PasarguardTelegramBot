# Hotfix: Required channels + coupon condition builder

## Added

- Required-channel membership gate for all non-admin users.
- Admin panel section: 📣 عضویت اجباری.
- Admin can add channels by @username or numeric chat ID such as -1001234567890.
- Invite link is optional, but recommended for private channels so users can join from the bot.
- Membership is checked on every message/callback before normal bot flow continues.
- Coupon condition builder replaced the old fixed scope model for newly created coupons.
- Coupon conditions are built with buttons, not pipe-separated input.
- Supported coupon condition clauses:
  - first purchase users
  - membership in a selected required channel
  - users from a chat-id list
  - admins with selected roles
- Conditions can be composed visually with AND groups, OR groups, and NOT on the last clause.
- Existing coupon conditions can be edited from the admin panel with 🧩 ویرایش شرایط کد.
- User-facing coupon failure message is generic and does not reveal the exact failed condition.

## Important Telegram limitation

Checking membership by numeric channel ID is possible only if the bot is a member/admin of that channel and Telegram allows `getChatMember` for it. For private channels, numeric ID is enough for checking, but not enough for the user to join; add an invite link too.

## Test

`python3 -m compileall -q app main.py` passes.
