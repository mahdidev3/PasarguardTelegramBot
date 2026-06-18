# HowTooSee Bot — Phase 1 ticket bugfix

This package keeps the Phase 1 architecture and fixes the first ticket-system bugs reported during testing.

## Phase 1 contents

- PostgreSQL / SQLAlchemy foundation.
- Core SQLAlchemy models.
- Complete ticket system foundation.
- Numeric second-confirmation service.
- User ticket menu: `🎫 پشتیبانی / تیکت‌ها`.
- Admin ticket menu inside the existing admin panel.

## Fixes in this package

- Fixed the new-ticket category flow so choosing service/order/wallet/general continues reliably to the subject step.
- Added clearer ticket instructions telling users to send description + media/file/voice/video in one message when possible.
- Fixed duplicate first user message in admin ticket view.
- Added admin file viewer: admin can open the file list and click a specific photo/video/document/voice/audio to receive it in Telegram.
- Changed ticket assignee display:
  - Admin view shows role + chat ID.
  - User view shows only the role label, not the admin chat ID.

## PostgreSQL setup

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

## Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
python main.py
```
