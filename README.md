# HowTooSee bot — Phase 0 refactor

This package is the Phase 0 checkpoint. It does **not** add new business
features yet. It prepares the project structure for the next phases while keeping
runtime behavior stable.

## Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env and set BOT_TOKEN / ADMIN_CHAT_IDS
python main.py
```

## What changed

- `main.py` is now a small entrypoint.
- Existing working bot code was moved to `app/legacy_bot.py`.
- Future code boundaries were added:
  - `app/routers/`
  - `app/services/`
  - `app/repositories/`
  - `app/keyboards/`
  - `app/utils/`
  - `app/schemas/`
  - `app/migrations/`

## Important

The bot still uses the same legacy SQLite logic in this phase. The PostgreSQL
migration starts in Phase 1.
