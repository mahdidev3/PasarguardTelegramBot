#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
02_reseed_tutorials.py

این اسکریپت همه دسته‌ها و آموزش‌های فعلی بخش آموزش‌ها را پاک می‌کند
و نسخه کامل‌تر آموزش‌های اولیه HowTooSee را دوباره می‌چیند.

محل اجرا:
    /home/PasarguardTelegramBot

روش اجرا:
    cd /home/PasarguardTelegramBot
    source .venv/bin/activate
    python scripts/02_reseed_tutorials.py

نکته مهم:
    هر بار این اسکریپت را اجرا کنید، جدول tutorials و tutorial_categories پاک
    و دوباره ساخته می‌شود. فایل/ویس/ویدئوهایی که بعداً دستی به آموزش‌ها وصل کرده‌اید
    هم از رکورد آموزش جدا می‌شوند؛ چون هدف این اسکریپت چیدن متن‌های اولیه از صفر است.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

TEHRAN_TZ = timezone(timedelta(hours=3, minutes=30))


def now_iso() -> str:
    return datetime.now(TEHRAN_TZ).isoformat(timespec="seconds")


def read_dotenv(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        env[key] = value
    return env


def parse_first_admin_id(raw: str | None) -> int | None:
    if not raw:
        return None
    for part in re.split(r"[,\s]+", raw):
        part = part.strip()
        if part.lstrip("-").isdigit():
            return int(part)
    return None


def resolve_db_path(project_dir: Path, db_arg: str | None) -> Path:
    if db_arg:
        db_path = Path(db_arg)
    else:
        env = read_dotenv(project_dir / ".env")
        raw = env.get("DATABASE_PATH") or os.getenv("DATABASE_PATH") or "bot.db"
        db_path = Path(raw)
    if not db_path.is_absolute():
        db_path = project_dir / db_path
    return db_path


def column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS tutorial_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            sort_order INTEGER NOT NULL DEFAULT 100,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_by INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tutorials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id INTEGER,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            media_type TEXT,
            media_file_id TEXT,
            media_file_unique_id TEXT,
            sort_order INTEGER NOT NULL DEFAULT 100,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_by INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )

    category_cols = column_names(conn, "tutorial_categories")
    for col, ddl in [
        ("description", "ALTER TABLE tutorial_categories ADD COLUMN description TEXT"),
        ("sort_order", "ALTER TABLE tutorial_categories ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 100"),
        ("is_active", "ALTER TABLE tutorial_categories ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1"),
        ("created_by", "ALTER TABLE tutorial_categories ADD COLUMN created_by INTEGER"),
        ("created_at", "ALTER TABLE tutorial_categories ADD COLUMN created_at TEXT"),
        ("updated_at", "ALTER TABLE tutorial_categories ADD COLUMN updated_at TEXT"),
    ]:
        if col not in category_cols:
            conn.execute(ddl)

    tutorial_cols = column_names(conn, "tutorials")
    for col, ddl in [
        ("category_id", "ALTER TABLE tutorials ADD COLUMN category_id INTEGER"),
        ("media_type", "ALTER TABLE tutorials ADD COLUMN media_type TEXT"),
        ("media_file_id", "ALTER TABLE tutorials ADD COLUMN media_file_id TEXT"),
        ("media_file_unique_id", "ALTER TABLE tutorials ADD COLUMN media_file_unique_id TEXT"),
        ("sort_order", "ALTER TABLE tutorials ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 100"),
        ("is_active", "ALTER TABLE tutorials ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1"),
        ("created_by", "ALTER TABLE tutorials ADD COLUMN created_by INTEGER"),
        ("created_at", "ALTER TABLE tutorials ADD COLUMN created_at TEXT"),
        ("updated_at", "ALTER TABLE tutorials ADD COLUMN updated_at TEXT"),
    ]:
        if col not in tutorial_cols:
            conn.execute(ddl)
    conn.commit()


CATEGORIES = [
    {"key": "buy_activation", "title": "🛒 خرید و فعال‌سازی سرویس", "description": "از انتخاب پلن تا پرداخت، ارسال رسید و دریافت ساب.", "sort_order": 10},
    {"key": "subscription_management", "title": "🔗 استفاده از ساب و مدیریت سرویس", "description": "کپی ساب، وضعیت سرویس، تمدید، افزایش حجم و نکات مهم جایگزینی سرویس.", "sort_order": 20},
    {"key": "android", "title": "📱 اتصال در اندروید", "description": "دانلود v2rayNG، وارد کردن ساب، تست Real delay، Sort و اتصال.", "sort_order": 30},
    {"key": "windows", "title": "💻 اتصال در ویندوز", "description": "دانلود v2rayN، اضافه کردن ساب، تست پینگ، انتخاب سرور و System Proxy.", "sort_order": 40},
    {"key": "packages", "title": "🎁 پکیج‌های اختصاصی", "description": "پکیج‌هایی که فقط با هماهنگی ادمین برای کاربر فعال می‌شوند.", "sort_order": 50},
    {"key": "payment_wallet", "title": "💳 پرداخت و کیف پول", "description": "کارت‌به‌کارت، رسید، مهلت رسید، شارژ کیف پول و پیگیری پرداخت.", "sort_order": 60},
    {"key": "tickets", "title": "🎫 تیکت و پشتیبانی", "description": "روش درست ثبت تیکت و ارسال اطلاعات لازم برای حل سریع مشکل.", "sort_order": 70},
    {"key": "troubleshooting", "title": "🛠 خطاهای رایج و رفع مشکل", "description": "وقتی وصل نمی‌شود، ساب آپدیت نمی‌شود، کند است یا برنامه خطا می‌دهد.", "sort_order": 80},
    {"key": "rules_security", "title": "🔐 قوانین و نکات مهم", "description": "نگهداری لینک ساب، استفاده درست و نکات امنیتی.", "sort_order": 90},
]


TUTORIALS = [
    {
        "category": "buy_activation",
        "title": "چطور سرویس جدید بخریم؟",
        "sort_order": 10,
        "body": """اگر برای اولین بار می‌خواهید سرویس بگیرید، این آموزش را بخوانید.

مراحل خرید:

1. وارد ربات شوید.
2. از منوی اصلی روی «🛒 خرید سرویس» بزنید.
3. دسته پلن‌ها را انتخاب کنید.
4. پلنی را انتخاب کنید که با مصرف شما مناسب است.
5. اگر ربات نام سرویس خواست، می‌توانید نام انگلیسی ساده وارد کنید یا ساخت خودکار را بزنید.
6. بعد از انتخاب پلن وارد مرحله پرداخت می‌شوید.
7. مبلغ را دقیقاً همان عددی که ربات نمایش می‌دهد کارت‌به‌کارت کنید.
8. رسید را همان‌جا برای ربات بفرستید.
9. بعد از تأیید رسید توسط ادمین فروش، سرویس ساخته می‌شود.
10. لینک اشتراک یا همان ساب برای شما نمایش داده می‌شود.

نکته مهم:
تا وقتی رسید تأیید نشود، سرویس فعال نمی‌شود.

اگر از صفحه پرداخت خارج شدید:
از بخش «💳 تراکنش‌ها» وضعیت سفارش را بررسی کنید. اگر نیاز بود، تیکت بزنید.""",
    },
    {
        "category": "buy_activation",
        "title": "چطور پلن مناسب انتخاب کنیم؟",
        "sort_order": 20,
        "body": """برای انتخاب پلن، اول مصرف خودتان را بسنجید.

اگر استفاده شما سبک است:
وب‌گردی، پیام‌رسان، کارهای روزمره و استفاده کم؛ پلن‌های اقتصادی مناسب‌ترند.

اگر استفاده شما معمولی است:
استفاده روزانه، شبکه‌های اجتماعی، چند ساعت اتصال در روز؛ پلن‌های محبوب انتخاب بهتری هستند.

اگر استفاده شما سنگین است:
دانلود، ویدیو، چند دستگاه یا استفاده زیاد؛ پلن‌های حجیم‌تر بگیرید.

نکته:
بهتر است کمی بیشتر از مصرف واقعی خود پلن بگیرید تا وسط دوره مجبور به خرید حجم اضافه نشوید.""",
    },
    {
        "category": "buy_activation",
        "title": "بعد از پرداخت چه اتفاقی می‌افتد؟",
        "sort_order": 30,
        "body": """بعد از پرداخت، سفارش شما مستقیم فعال نمی‌شود؛ اول رسید بررسی می‌شود.

مراحل بعد از پرداخت:

1. ربات مبلغ و کارت مقصد را نمایش می‌دهد.
2. شما مبلغ دقیق را پرداخت می‌کنید.
3. رسید را به‌صورت عکس یا فایل در همان بخش می‌فرستید.
4. سفارش در وضعیت «در انتظار بررسی رسید» قرار می‌گیرد.
5. ادمین فروش رسید را بررسی می‌کند.
6. اگر درست باشد، پرداخت تأیید می‌شود.
7. بعد از تأیید، سرویس ساخته و لینک ساب ارسال می‌شود.

اگر رسید رد شد:
دلیل رد شدن را بخوانید و اگر نیاز بود تیکت بزنید.

اگر رسید منقضی شد:
یعنی مهلت ارسال یا پیگیری رسید تمام شده است. در این حالت باید از مسیر درست دوباره اقدام کنید یا با پشتیبانی هماهنگ کنید.""",
    },
    {
        "category": "subscription_management",
        "title": "لینک اشتراک یا ساب چیست؟",
        "sort_order": 10,
        "body": """ساب یا لینک اشتراک همان لینکی است که بعد از فعال شدن سرویس دریافت می‌کنید.

این لینک را داخل برنامه‌هایی مثل v2rayNG در اندروید یا v2rayN در ویندوز وارد می‌کنید. برنامه با این لینک، سرورها و اطلاعات اتصال را دریافت می‌کند.

چرا ساب مهم است؟

1. لازم نیست کانفیگ‌ها را یکی‌یکی وارد کنید.
2. اگر سرورها تغییر کنند، با آپدیت ساب اطلاعات جدید دریافت می‌شود.
3. بعد از تمدید یا افزایش حجم، معمولاً کافی است ساب را آپدیت کنید.
4. مدیریت اتصال راحت‌تر می‌شود.

نکته امنیتی:
لینک ساب شخصی است. آن را برای دیگران نفرستید و در گروه، کانال یا سایت منتشر نکنید.""",
    },
    {
        "category": "subscription_management",
        "title": "چطور لینک ساب را کپی کنیم؟",
        "sort_order": 20,
        "body": """برای اتصال به سرویس، اول باید لینک ساب را از ربات بگیرید.

مراحل:

1. وارد ربات شوید.
2. روی «📦 سرویس‌های من» بزنید.
3. سرویس موردنظر را انتخاب کنید.
4. در صفحه سرویس، دکمه «🔗 لینک کامل اشتراک» یا «🌐 پنل اشتراکی» را پیدا کنید.
5. لینک را کپی کنید.
6. حالا به برنامه اتصال بروید و لینک را از Clipboard اضافه کنید.

روش راحت‌تر:
لینک را از ربات کپی کنید، بعد داخل برنامه اتصال گزینه‌هایی مثل «Import from Clipboard» یا «اضافه کردن از کلیپ‌بورد» را بزنید.

اگر لینک نمایش داده نشد:
ممکن است سرویس هنوز در حال فعال‌سازی باشد. چند لحظه صبر کنید و دوباره بررسی کنید. اگر درست نشد، تیکت بزنید.""",
    },
    {
        "category": "subscription_management",
        "title": "تمدید سرویس و افزایش حجم چه فرقی دارند؟",
        "sort_order": 30,
        "body": """این دو گزینه کاربرد متفاوت دارند و خیلی مهم است اشتباه انتخاب نشوند.

تمدید سرویس:
در سیستم ما تمدید یعنی سرویس فعلی با پلن/سرویس جدید جایگزین می‌شود. یعنی اطلاعات سرویس قبلی روی همان سرویس آپدیت می‌شود و عملاً سرویس قبلی از بین می‌رود یا با نسخه جدید جایگزین می‌شود.

پس اگر می‌خواهید سرویس قبلی را نگه دارید و یک سرویس جداگانه داشته باشید، به‌جای تمدید، باید «خرید سرویس جدید» انجام دهید.

افزایش حجم:
فقط حجم سرویس فعلی را بیشتر می‌کند و معمولاً زمان سرویس را بیشتر نمی‌کند.

مثال ساده:

اگر سرویس شما هنوز ۱۵ روز زمان دارد ولی حجم آن تمام شده:
«افزایش حجم» بگیرید.

اگر می‌خواهید پلن جدید بگیرید و سرویس فعلی با پلن جدید جایگزین شود:
«تمدید سرویس» بزنید.

اگر می‌خواهید یک سرویس جدا از سرویس قبلی داشته باشید:
«خرید سرویس جدید» بزنید، نه تمدید.

بعد از تمدید یا افزایش حجم:
داخل برنامه اتصال، ساب را یک بار آپدیت کنید.""",
    },
    {
        "category": "subscription_management",
        "title": "چطور وضعیت حجم و تاریخ سرویس را ببینیم؟",
        "sort_order": 40,
        "body": """برای دیدن وضعیت سرویس:

1. وارد ربات شوید.
2. روی «📦 سرویس‌های من» بزنید.
3. سرویس موردنظر را انتخاب کنید.
4. اطلاعات سرویس را ببینید.

معمولاً این موارد نمایش داده می‌شود:

نام سرویس
نوع پلن
حجم کل
حجم مصرف‌شده
تاریخ پایان
وضعیت سرویس
لینک اشتراک

اگر حجم تمام شده:
بسته افزایش حجم بگیرید.

اگر زمان سرویس تمام شده یا می‌خواهید پلن جایگزین شود:
تمدید سرویس انجام دهید.

اگر می‌خواهید سرویس جداگانه داشته باشید:
خرید سرویس جدید انجام دهید.""",
    },
    {
        "category": "android",
        "title": "دانلود و نصب v2rayNG برای اندروید",
        "sort_order": 10,
        "body": """برای اتصال در گوشی Android، برنامه پیشنهادی v2rayNG است.

لینک دانلود:
https://github.com/2dust/v2rayNG/releases

مراحل دانلود:

1. لینک بالا را باز کنید.
2. وارد صفحه نسخه‌ها یا Releases می‌شوید.
3. آخرین نسخه را انتخاب کنید.
4. کمی پایین‌تر بروید تا بخش «Assets» را ببینید.
5. اگر متن‌های صفحه چینی بود، نگران نباشید؛ بخش Assets معمولاً پایین همان نسخه است.
6. از بخش Assets فایل APK مناسب گوشی را دانلود کنید.

کدام فایل را دانلود کنم؟

برای بیشتر گوشی‌های جدید، معمولاً فایل arm64-v8a یا universal مناسب است.
اگر مطمئن نیستید، نسخه universal را امتحان کنید یا از پشتیبانی بپرسید.

بعد از دانلود:

1. فایل APK را باز کنید.
2. اگر گوشی اجازه نصب نداد، اجازه نصب از مرورگر یا فایل منیجر را فعال کنید.
3. برنامه را نصب کنید.
4. بعد از نصب، برنامه v2rayNG را باز کنید.

نکته امنیتی:
برنامه را از کانال‌ها و سایت‌های ناشناس دانلود نکنید. نسخه‌های دستکاری‌شده ممکن است امن نباشند.""",
    },
    {
        "category": "android",
        "title": "اضافه کردن ساب در v2rayNG با کلیپ‌بورد",
        "sort_order": 20,
        "body": """این روش برای بیشتر کاربران راحت‌تر از وارد کردن دستی لینک است.

قبل از شروع:
از ربات، لینک ساب سرویس خود را کپی کنید.

مراحل اضافه کردن در v2rayNG:

1. وارد برنامه v2rayNG شوید.
2. اگر اولین بار است وارد برنامه می‌شوید، صفحه اصلی خالی یا دارای چند کانفیگ است.
3. از بالای صفحه یا پایین صفحه، علامت «+» یا گزینه اضافه کردن را بزنید.
4. گزینه‌ای شبیه «Import from Clipboard» یا «Import config from Clipboard» یا «اضافه کردن از کلیپ‌بورد» را انتخاب کنید.
5. برنامه لینک کپی‌شده را می‌خواند و یک گروه یا لیست سرور اضافه می‌کند.
6. اگر گروهی به نام import_sub ساخته شد، وارد همان گروه شوید.
7. می‌توانید بعداً اسم گروه را به شماره سرویس خودتان تغییر دهید تا راحت‌تر پیدایش کنید.

نام پیشنهادی گروه:
به‌جای نوشتن HowTooSee، بهتر است شماره سرویس یا نام سرویس خودتان را بزنید. مثلاً:

سرویس 24
یا
howtosee_12345

آیا می‌شود کاری کرد اسم گروه import_sub نشود؟
در بعضی برنامه‌ها بله، اگر موقع اضافه کردن subscription فیلد Name یا Remark داشته باشد، همان‌جا اسم دلخواه بزنید. اما اگر از Import from Clipboard استفاده کنید، بعضی نسخه‌ها خودشان اسم import_sub می‌گذارند و از سمت لینک همیشه قابل کنترل نیست. بهترین کار این است که بعد از اضافه شدن، داخل برنامه اسم گروه را تغییر دهید یا هنگام اضافه کردن دستی، Name را شماره سرویس بگذارید.""",
    },
    {
        "category": "android",
        "title": "اتصال در v2rayNG با Real delay و Sort",
        "sort_order": 30,
        "body": """برای اینکه اتصال بهتر و سریع‌تری داشته باشید، قبل از وصل شدن ساب را آپدیت کنید و بعد سرورها را با Real delay تست کنید.

مرحله 1: آپدیت ساب

1. وارد v2rayNG شوید.
2. وارد گروه ساب خود شوید؛ اگر اسمش import_sub است، همان گروه را باز کنید.
3. منوی سه‌نقطه یا گزینه‌های بالای صفحه را باز کنید.
4. گزینه Update subscription یا Update current group را بزنید.
5. صبر کنید تا سرورها بروزرسانی شوند.

مرحله 2: تست Real delay

1. در همان صفحه‌ای که سرورها را می‌بینید، منوی سه‌نقطه بالای صفحه را باز کنید.
2. گزینه‌ای شبیه «Real delay» یا «Test all configurations real delay» را بزنید.
3. چند ثانیه صبر کنید تا کنار سرورها عدد پینگ یا delay نمایش داده شود.
4. عدد کمتر بهتر است. مثلاً 80ms معمولاً بهتر از 400ms است.

مرحله 3: مرتب‌سازی یا Sort

1. بعد از Real delay، از منوی بالای صفحه گزینه Sort یا مرتب‌سازی را بزنید.
2. سرورها را بر اساس delay مرتب کنید.
3. سروری که بالاتر آمده و delay کمتری دارد را انتخاب کنید.

مرحله 4: اتصال

1. روی سرور اول یا سروری که پینگ خوبی دارد بزنید تا انتخاب شود.
2. پایین صفحه، دکمه اتصال را بزنید؛ معمولاً پایین سمت راست است.
3. در بعضی نسخه‌ها این دکمه کنار آیکون مربع یا به شکل دکمه روشن/خاموش نمایش داده می‌شود.
4. اگر برای اولین بار پیام اجازه VPN آمد، Allow یا OK را بزنید.
5. وقتی علامت اتصال فعال شد، مرورگر یا تلگرام را تست کنید.

نکته مهم برای سرعت بیشتر:
قبل از هر بار اتصال، مخصوصاً وقتی چند روز گذشته یا سرویس را تمدید کرده‌اید، ساب را آپدیت کنید. این کار باعث می‌شود همیشه سرورهای جدیدتر و سالم‌تر را داشته باشید.

اگر وصل نشد:
یک سرور دیگر را انتخاب کنید، دوباره Real delay بزنید و سروری با پینگ بهتر انتخاب کنید.""",
    },
    {
        "category": "windows",
        "title": "دانلود و نصب v2rayN برای ویندوز",
        "sort_order": 10,
        "body": """برای اتصال در Windows، برنامه پیشنهادی v2rayN است.

لینک دانلود:
https://github.com/2dust/v2rayN/releases

مراحل دانلود:

1. لینک بالا را باز کنید.
2. وارد صفحه Releases شوید.
3. آخرین نسخه را انتخاب کنید.
4. پایین همان نسخه، بخش Assets را پیدا کنید.
5. اگر متن‌های صفحه چینی یا انگلیسی بود، نگران نباشید؛ فقط دنبال Assets بگردید.
6. فایل مناسب ویندوز 64bit را دانلود کنید.
7. فایل zip را Extract کنید.
8. برنامه v2rayN را اجرا کنید.

نکته:
معمولاً v2rayN نیاز به نصب مرحله‌ای ندارد. فایل را از حالت فشرده خارج کنید و اجرا کنید.

اگر برنامه باز نشد:
ممکن است نیاز باشد .NET Desktop Runtime نصب کنید. اگر خطا دیدید، اسکرین‌شات خطا را برای پشتیبانی بفرستید.""",
    },
    {
        "category": "windows",
        "title": "اضافه کردن ساب و اتصال در v2rayN ویندوز",
        "sort_order": 20,
        "body": """قبل از شروع، لینک ساب را از ربات کپی کنید.

روش راحت با کلیپ‌بورد:

1. وارد v2rayN شوید.
2. از منوی Subscription یا Subscriptions وارد بخش مدیریت اشتراک شوید.
3. گزینه Add یا افزودن را بزنید.
4. در قسمت Remarks یا Name یک اسم بزنید؛ بهتر است شماره سرویس خودتان را بنویسید.
5. در قسمت URL، لینک ساب را Paste کنید.
6. ذخیره کنید.
7. حالا گزینه Update subscription را بزنید.
8. سرورها داخل لیست نمایش داده می‌شوند.

اگر گزینه Import from Clipboard داشت:
می‌توانید لینک را کپی کنید و از گزینه Import from Clipboard استفاده کنید، ولی بهتر است روش Subscription را انجام دهید تا اسم گروه import_sub نشود.

تست پینگ و انتخاب سرور:

1. همه سرورها را انتخاب کنید یا روی لیست سرورها کلیک کنید.
2. از منو یا راست‌کلیک، گزینه Test servers real delay یا Real delay را بزنید.
3. صبر کنید تا عدد delay نمایش داده شود.
4. ستون delay را Sort کنید تا کمترین عدد بالا بیاید.
5. روی سروری که delay کمتر و وضعیت بهتر دارد کلیک کنید.
6. آن سرور را به‌عنوان سرور فعال انتخاب کنید.

فعال کردن اتصال در ویندوز:

1. از پایین برنامه یا منوی System Proxy، حالت Set system proxy را فعال کنید.
2. بعضی نسخه‌ها در کنار ساعت ویندوز آیکون v2rayN دارند. اگر برنامه را نمی‌بینید، کنار ساعت ویندوز را بررسی کنید.
3. بعد از فعال کردن System Proxy، مرورگر را باز کنید و اتصال را تست کنید.

اگر اینترنت قطع شد:
System Proxy را خاموش کنید، یک سرور دیگر انتخاب کنید، Real delay بزنید و دوباره فعال کنید.""",
    },
    {
        "category": "packages",
        "title": "پکیج اختصاصی چیست؟",
        "sort_order": 10,
        "body": """پکیج اختصاصی بسته‌ای است که برای همه کاربران نمایش داده نمی‌شود.

این پکیج‌ها فقط وقتی نمایش داده می‌شوند که ادمین برای شما فعال کرده باشد.

چه زمانی پکیج اختصاصی می‌گیرید؟

وقتی با ادمین هماهنگ کرده‌اید
وقتی چند سرویس یا چند زیرمجموعه می‌خواهید
وقتی شرایط یا قیمت خاص دارید
وقتی برای شما بسته ویژه تعریف شده است

اگر پکیج نمی‌بینید:
یعنی هنوز برای شما فعال نشده یا شرایط آن تغییر کرده است. در این حالت باید با پشتیبانی هماهنگ کنید.""",
    },
    {
        "category": "packages",
        "title": "چطور پکیج اختصاصی را استفاده کنیم؟",
        "sort_order": 20,
        "body": """اگر ادمین برای شما پکیج اختصاصی فعال کرده باشد، می‌توانید آن را از ربات ببینید.

مراحل:

1. وارد ربات شوید.
2. اگر برای شما پکیج فعال باشد، دکمه «🎁 پکیج‌های من» نمایش داده می‌شود.
3. وارد پکیج شوید.
4. توضیحات و شرایط پکیج را کامل بخوانید.
5. اگر نیاز به پرداخت دارد، پرداخت را انجام دهید.
6. بعد از تأیید، سرویس‌های مربوط به پکیج فعال می‌شوند.

نکته:
پکیج‌ها عمومی نیستند. اگر دکمه پکیج‌های من را نمی‌بینید، یعنی پکیج فعالی برای شما ثبت نشده است.""",
    },
    {
        "category": "payment_wallet",
        "title": "پرداخت کارت‌به‌کارت و ارسال رسید",
        "sort_order": 10,
        "body": """برای خرید سرویس یا شارژ کیف پول، پرداخت کارت‌به‌کارت انجام می‌شود.

مراحل:

1. مبلغ دقیق را از ربات ببینید.
2. شماره کارت نمایش داده‌شده را کپی کنید.
3. دقیقاً همان مبلغ را کارت‌به‌کارت کنید.
4. رسید را به‌صورت عکس یا فایل نگه دارید.
5. رسید را در همان بخش پرداخت برای ربات ارسال کنید.
6. منتظر بررسی ادمین فروش بمانید.

نکات مهم:

مبلغ را کم یا زیاد نکنید.
رسید را جای دیگری نفرستید.
از پایا یا پل استفاده نکنید مگر پشتیبانی گفته باشد.
اگر برنامه پرداخت خطای محدودیت کارت داد، از موبایل‌بانک یا اینترنت‌بانک استفاده کنید.

تا وقتی رسید تأیید نشود، پرداخت نهایی نیست.""",
    },
    {
        "category": "payment_wallet",
        "title": "رسید منقضی یا رد شده یعنی چه؟",
        "sort_order": 20,
        "body": """رسید رد شده:
یعنی ادمین فروش رسید را بررسی کرده ولی مشکلی وجود داشته است. مثلاً مبلغ اشتباه بوده، رسید واضح نبوده یا اطلاعات پرداخت با سفارش هماهنگ نبوده است.

رسید منقضی شده:
یعنی مهلت ارسال یا پیگیری رسید تمام شده است.

چه کار کنم؟

1. پیام ربات را کامل بخوانید.
2. اگر دلیل رد شدن نوشته شده، همان را بررسی کنید.
3. اگر نیاز به راهنمایی دارید، تیکت بزنید.
4. شماره سفارش را برای پشتیبانی بفرستید.

برای جلوگیری از مشکل:
همیشه مبلغ دقیق را پرداخت کنید و رسید واضح بفرستید.""",
    },
    {
        "category": "payment_wallet",
        "title": "کیف پول چیست و چطور شارژ می‌شود؟",
        "sort_order": 30,
        "body": """کیف پول یعنی موجودی داخلی شما در ربات.

با کیف پول می‌توانید بعداً سریع‌تر خرید انجام دهید.

مراحل شارژ کیف پول:

1. وارد بخش کیف پول شوید.
2. گزینه افزایش موجودی را انتخاب کنید.
3. مبلغ موردنظر را وارد کنید.
4. پرداخت کارت‌به‌کارت انجام دهید.
5. رسید را ارسال کنید.
6. بعد از تأیید رسید، مبلغ به کیف پول اضافه می‌شود.

نکته:
شارژ کیف پول هم نیاز به تأیید رسید دارد. تا وقتی رسید تأیید نشود، موجودی اضافه نمی‌شود.""",
    },
    {
        "category": "tickets",
        "title": "چطور تیکت پشتیبانی بزنیم؟",
        "sort_order": 10,
        "body": """اگر مشکلی دارید که با آموزش‌ها حل نمی‌شود، تیکت بزنید.

مراحل ثبت تیکت:

1. وارد ربات شوید.
2. روی «🎫 پشتیبانی / تیکت‌ها» بزنید.
3. تیکت جدید را انتخاب کنید.
4. موضوع مناسب را انتخاب کنید.
5. عنوان کوتاه و واضح بنویسید.
6. مشکل را کامل توضیح دهید.
7. اگر عکس، ویدیو یا فایل لازم است، همان‌جا ارسال کنید.
8. منتظر پاسخ پشتیبانی بمانید.

برای پاسخ سریع‌تر این اطلاعات را بفرستید:

نام سرویس
نوع گوشی یا ویندوز
نام برنامه اتصال
اسکرین‌شات خطا
زمان شروع مشکل
کاری که قبل از مشکل انجام داده‌اید

پیام‌هایی مثل «وصل نمیشه» کافی نیستند. هرچه توضیح کامل‌تر باشد، مشکل سریع‌تر حل می‌شود.""",
    },
    {
        "category": "troubleshooting",
        "title": "اگر سرویس وصل نشد چه کار کنیم؟",
        "sort_order": 10,
        "body": """اگر سرویس وصل نشد، قبل از تیکت زدن این مراحل را انجام دهید.

1. اینترنت اصلی خود را بررسی کنید.
2. ساب را آپدیت کنید.
3. Real delay بزنید.
4. سرورها را Sort کنید.
5. سروری با delay کمتر انتخاب کنید.
6. اتصال را خاموش و روشن کنید.
7. یک سرور دیگر را امتحان کنید.
8. تاریخ و حجم سرویس را در ربات بررسی کنید.

در اندروید:
برنامه را ببندید، اینترنت گوشی را خاموش و روشن کنید، بعد دوباره وصل شوید.

در ویندوز:
System Proxy را خاموش و روشن کنید و اگر لازم بود سرور دیگری انتخاب کنید.

اگر هنوز مشکل حل نشد:
تیکت بزنید و نام سرویس، نوع دستگاه، نام برنامه و اسکرین‌شات خطا را بفرستید.""",
    },
    {
        "category": "troubleshooting",
        "title": "سرورها نمایش داده نمی‌شوند یا ساب آپدیت نمی‌شود",
        "sort_order": 20,
        "body": """اگر لینک ساب را وارد کردید ولی سرورها نمایش داده نشدند:

1. لینک ساب را دوباره از ربات کپی کنید.
2. مطمئن شوید لینک را کامل کپی کرده‌اید.
3. داخل برنامه گزینه Update subscription را بزنید.
4. اینترنت اصلی دستگاه را بررسی کنید.
5. اگر VPN یا proxy دیگری روشن است، خاموش کنید.
6. برنامه را کامل ببندید و دوباره باز کنید.
7. اگر نشد، لینک ساب قبلی را حذف و دوباره اضافه کنید.

بعد از تمدید یا افزایش حجم:
حتماً ساب را آپدیت کنید.

اگر باز هم مشکل بود:
اسکرین‌شات خطا را در تیکت بفرستید.""",
    },
    {
        "category": "rules_security",
        "title": "لینک ساب خود را برای دیگران نفرستید",
        "sort_order": 10,
        "body": """لینک ساب شما شخصی است.

چرا نباید لینک ساب را منتشر کنید؟

1. ممکن است دیگران از حجم شما استفاده کنند.
2. ممکن است سرویس شما دچار اختلال شود.
3. ممکن است امنیت حساب شما پایین بیاید.
4. پیگیری مشکل برای پشتیبانی سخت‌تر می‌شود.

لینک ساب را اینجاها نفرستید:

گروه‌ها
کانال‌ها
سایت‌ها
کامنت‌ها
برای افراد ناشناس

اگر فکر می‌کنید لینک شما دست کسی افتاده:
از بخش سرویس‌های من بررسی کنید و اگر گزینه تغییر لینک دارید، لینک را تغییر دهید. اگر مطمئن نیستید، تیکت بزنید.""",
    },
]


def reset_tutorials(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM tutorials")
    conn.execute("DELETE FROM tutorial_categories")
    try:
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('tutorials', 'tutorial_categories')")
    except Exception:
        pass


def insert_category(conn: sqlite3.Connection, data: dict[str, Any], admin_id: int | None) -> int:
    ts = now_iso()
    cur = conn.execute(
        """
        INSERT INTO tutorial_categories
        (title, description, sort_order, is_active, created_by, created_at, updated_at)
        VALUES (?, ?, ?, 1, ?, ?, ?)
        """,
        (data["title"], data["description"], int(data["sort_order"]), admin_id, ts, ts),
    )
    return int(cur.lastrowid)


def insert_tutorial(conn: sqlite3.Connection, category_id: int, data: dict[str, Any], admin_id: int | None) -> int:
    ts = now_iso()
    cur = conn.execute(
        """
        INSERT INTO tutorials
        (category_id, title, body, media_type, media_file_id, media_file_unique_id,
         sort_order, is_active, created_by, created_at, updated_at)
        VALUES (?, ?, ?, NULL, NULL, NULL, ?, 1, ?, ?, ?)
        """,
        (category_id, data["title"], data["body"].strip(), int(data["sort_order"]), admin_id, ts, ts),
    )
    return int(cur.lastrowid)


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset and seed HowTooSee tutorial categories/tutorials.")
    parser.add_argument("--project-dir", default="/home/PasarguardTelegramBot")
    parser.add_argument("--db", default=None)
    parser.add_argument("--admin-id", type=int, default=None)
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    project_dir = Path(args.project_dir).resolve()
    dotenv = read_dotenv(project_dir / ".env")
    admin_id = args.admin_id or parse_first_admin_id(dotenv.get("ADMIN_CHAT_IDS") or os.getenv("ADMIN_CHAT_IDS"))
    db_path = resolve_db_path(project_dir, args.db)

    if not db_path.exists():
        print(f"❌ دیتابیس پیدا نشد: {db_path}")
        return 2

    if not args.no_backup and not args.dry_run:
        backup_path = db_path.with_suffix(db_path.suffix + f".tutorial_reseed_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        shutil.copy2(db_path, backup_path)
        print(f"✅ بک‌آپ ساخته شد: {backup_path}")

    print(f"📍 project_dir = {project_dir}")
    print(f"📍 db_path     = {db_path}")
    print(f"📍 admin_id    = {admin_id or '-'}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    try:
        with closing(conn):
            ensure_schema(conn)
            reset_tutorials(conn)

            category_ids: dict[str, int] = {}
            for cat in CATEGORIES:
                category_ids[cat["key"]] = insert_category(conn, cat, admin_id)

            for tut in TUTORIALS:
                insert_tutorial(conn, category_ids[tut["category"]], tut, admin_id)

            if args.dry_run:
                conn.rollback()
                print("🧪 dry-run انجام شد؛ چیزی ذخیره نشد.")
            else:
                conn.commit()

            print("✅ آموزش‌ها از صفر چیده شدند.")
            print(f"📂 دسته‌ها: {len(CATEGORIES)}")
            print(f"📘 آموزش‌ها: {len(TUTORIALS)}")

    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        print(f"❌ خطا: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
