# Callback split hotfix

Fixed admin callback parsers whose handlers expected 4 colon-separated fields while keyboards generated 3 fields.

Affected handlers in `app/legacy_bot.py`:

- `adm_svc_status:<service_id>:<status>`
- `adm_svc_data:<service_id>:<gb>`
- `adm_svc_days:<service_id>:<days>`
- `adm_manual_service_plan:<uid>:<plan_key>`

The handlers now parse with `split(":", 2)` and return a safe Telegram alert instead of crashing when malformed callback data arrives.
