# اسکریپت‌های آموزش‌ها

## 02_reseed_tutorials.py

این اسکریپت همه دسته‌ها و آموزش‌های فعلی را پاک می‌کند و آموزش‌های اولیه اصلاح‌شده را از صفر می‌چیند.

```bash
cd /home/PasarguardTelegramBot
sudo systemctl stop pasarguardtelegrambot
source .venv/bin/activate
python scripts/02_reseed_tutorials.py
sudo systemctl start pasarguardtelegrambot
sudo journalctl -u pasarguardtelegrambot -f
```

تست بدون ذخیره:

```bash
python scripts/02_reseed_tutorials.py --dry-run
```

بدون بک‌آپ:

```bash
python scripts/02_reseed_tutorials.py --no-backup
```

نکته: چون این اسکریپت آموزش‌ها را از صفر می‌چیند، فایل/ویس/ویدئویی که قبلاً دستی به آموزش‌ها وصل کرده باشید از رکورد جدید جدا می‌شود.
