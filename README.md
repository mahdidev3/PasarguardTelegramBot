# HowTooSee Bot — Phase 1

Phase 1 keeps the legacy bot behavior available, but adds the new PostgreSQL
foundation, a complete ticket system, and numeric second-confirmation support.

## What changed in Phase 1

- New async SQLAlchemy/PostgreSQL layer in `app/database.py`
- Full SQLAlchemy schema in `app/models.py`
- Ticket tables: `tickets`, `ticket_messages`, `ticket_attachments`, `ticket_admin_notes`, `ticket_events`
- Numeric confirmation table: `admin_confirmations`
- New ticket router in `app/routers/tickets.py`
- New ticket keyboards in `app/keyboards/tickets.py`
- Ticket service layer in `app/services/ticket_service.py`
- Confirmation service in `app/services/confirmation_service.py`
- Admin audit service in `app/services/admin_audit_service.py`
- Legacy `main.py` entrypoint still works: `python main.py`
- User menu now contains `🎫 پشتیبانی / تیکت‌ها`
- Admin panel now contains `🎫 تیکت‌ها`

## PostgreSQL setup on Ubuntu

```bash
sudo apt update
sudo apt install postgresql postgresql-contrib
sudo -u postgres createuser pasarguard_bot
sudo -u postgres createdb pasarguard_bot
sudo -u postgres psql
```

Inside `psql`:

```sql
ALTER USER pasarguard_bot WITH PASSWORD 'YOUR_STRONG_PASSWORD';
GRANT ALL PRIVILEGES ON DATABASE pasarguard_bot TO pasarguard_bot;
\q
```

Set `.env`:

```env
DATABASE_URL=postgresql+asyncpg://pasarguard_bot:YOUR_STRONG_PASSWORD@127.0.0.1:5432/pasarguard_bot
ADMIN_CHAT_IDS=123456789
BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
```

## Install and run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
python main.py
```

On startup, Phase 1 creates the PostgreSQL tables automatically with
`Base.metadata.create_all()` so you can test quickly on a fresh database.
Alembic scaffolding is present in `app/migrations/` for the next hardening pass.

## Ticket flow

User side:

```text
🎫 پشتیبانی / تیکت‌ها
├── 🎫 تیکت جدید
├── 📂 تیکت‌های باز من
└── ✅ بسته‌شده‌ها
```

Admin side:

```text
👑 پنل مدیریت
└── 🎫 تیکت‌ها
    ├── 🆕 جدید
    ├── 📂 همه باز
    ├── 👤 تیکت‌های من
    ├── ⏳ منتظر کاربر
    └── ✅ بسته‌شده‌ها
```

Ticket messages support text, photo, document, video, voice and audio metadata.
The file itself is not downloaded; Telegram `file_id` is stored for later use.

## Numeric confirmation

The confirmation service is now available for dangerous admin actions. In this
phase it is wired into admin ticket close: closing a ticket from the admin panel
requires a 6-digit code that expires after 5 minutes.

Later phases will reuse the same service for restore, broadcast, delete user,
delete service, wallet decrease, and bot lock.
