# HowTooSee / Pasarguard Bot — Phase 4.9

This checkpoint adds the full Pasarguard admin panel controls on top of Phase 4.8.

## Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
python main.py
```

## Pasarguard admin panel

Telegram admin menu:

```text
👑 پنل مدیریت → 🔌 Pasarguard
```

Includes:

- 📊 داشبورد Pasarguard
- 📡 تست اتصال
- 🧪 Dry-run سینک Templateها
- ✅ اعمال Sync Templateها
- 🔄 Sync سرویس‌ها از پنل
- 🧪 Dry-run Reconcile فعلی
- ✅ اعمال Reconcile فعلی
- 🧭 Userهای Orphan
- 📜 لاگ Sync
- 🗃 Snapshotها

`Reconcile فعلی` compares the current bot database desired state with the live Pasarguard panel. It does not require a backup file. The apply action still requires numeric confirmation and never performs real remote deletion.

## Safety

Keep dry-run enabled until you trust the report:

```env
PASARGUARD_DRY_RUN=true
```

When applying real template/user changes:

```env
PASARGUARD_DRY_RUN=false
```
