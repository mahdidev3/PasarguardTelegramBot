# HowTooSee Bot — Phase 2

Run:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
python main.py
```

Phase 2 adds:

- DB-backed plan management from admin panel.
- Data-addon management.
- Sync of active PostgreSQL plans into the legacy buying flow.
- Editable text templates from admin panel.
- Broadcast campaigns with text/photo/video/document/voice/audio and URL buttons.
- Broadcast preview and numeric confirmation before final send.

Admin entry is still not `/admin`. Admins see `👑 پنل مدیریت` in the main menu.

Important:

- `DATABASE_URL` is required.
- A fresh PostgreSQL DB is expected.
- `bot.db` is not part of the new data architecture and is not preserved.
- Real Pasarguard API connection is intentionally not added yet.

PostgreSQL quick setup:

```bash
sudo apt update
sudo apt install postgresql postgresql-contrib
sudo -u postgres createuser pasarguard_telegram_bot
sudo -u postgres createdb pasarguard_telegram_bot
sudo -u postgres psql
```

Inside psql:

```sql
ALTER USER pasarguard_telegram_bot WITH PASSWORD 'YOUR_STRONG_PASSWORD';
GRANT ALL PRIVILEGES ON DATABASE pasarguard_telegram_bot TO pasarguard_telegram_bot;
\q
```

### Phase 4.5/4.6 notes

This checkpoint starts real service-user integration. Keep `PASARGUARD_DRY_RUN=true` for the first run. With dry-run on, Telegram purchase flows keep working but no remote Pasarguard user is created. After template sync works, set `PASARGUARD_DRY_RUN=false` to create/modify remote users.

### Phase 4.8 Pasarguard checkpoint backup/restore

When `PASARGUARD_ENABLED=true`, complete backups contain:

- `external/pasarguard_desired_templates.jsonl`
- `external/pasarguard_desired_state.jsonl`
- `external/pasarguard_actual_templates.jsonl`
- `external/pasarguard_actual_users.jsonl`
- `external/pasarguard_summary.json`

During restore, the bot first shows a dry-run reconcile report. After numeric confirmation, it restores the bot databases and then reconciles Pasarguard. Keep `PASARGUARD_DRY_RUN=true` for report-only restore testing. Set it to `false` only when you want the bot to actually create/update remote templates/users.
