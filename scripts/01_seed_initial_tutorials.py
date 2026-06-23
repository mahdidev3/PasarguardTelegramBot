#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
01_seed_initial_tutorials.py

این اسکریپت آموزش‌های اولیه HowTooSee را مستقیم داخل دیتابیس SQLite ربات اضافه/آپدیت می‌کند.

کجا اجرا شود؟
    /home/PasarguardTelegramBot

روش اجرا:
    cd /home/PasarguardTelegramBot
    source .venv/bin/activate
    python scripts/01_seed_initial_tutorials.py

نکته:
    این اسکریپت idempotent است؛ یعنی اگر دوباره اجرا شود، آموزش‌های هم‌نام را آپدیت می‌کند
    و تا حد ممکن آموزش تکراری نمی‌سازد.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import sys
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

    ts = now_iso()
    conn.execute("UPDATE tutorial_categories SET created_at = COALESCE(created_at, ?), updated_at = COALESCE(updated_at, ?)", (ts, ts))
    conn.execute("UPDATE tutorials SET created_at = COALESCE(created_at, ?), updated_at = COALESCE(updated_at, ?)", (ts, ts))
    conn.commit()


CATEGORIES = [
    {
        "key": "buy_activation",
        "title": "🛒 خرید و فعال‌سازی سرویس",
        "description": "آموزش خرید سرویس، پرداخت، ارسال رسید و دریافت لینک اشتراک.",
        "sort_order": 10,
    },
    {
        "key": "subscription_management",
        "title": "🔗 استفاده از ساب و مدیریت سرویس",
        "description": "آموزش کپی کردن لینک ساب، دیدن حجم، تاریخ انقضا، تمدید و افزایش حجم.",
        "sort_order": 20,
    },
    {
        "key": "android",
        "title": "📱 اتصال در اندروید",
        "description": "آموزش نصب برنامه، وارد کردن لینک اشتراک و وصل شدن در گوشی‌های Android.",
        "sort_order": 30,
    },
    {
        "key": "windows",
        "title": "💻 اتصال در ویندوز",
        "description": "آموزش اتصال در لپ‌تاپ و کامپیوترهای Windows.",
        "sort_order": 40,
    },
    {
        "key": "packages",
        "title": "🎁 پکیج‌های اختصاصی",
        "description": "آموزش دریافت، مشاهده و استفاده از پکیج‌هایی که توسط ادمین برای کاربر فعال می‌شوند.",
        "sort_order": 50,
    },
    {
        "key": "payment_wallet",
        "title": "💳 پرداخت و کیف پول",
        "description": "آموزش پرداخت کارت‌به‌کارت، ارسال رسید، شارژ کیف پول و پیگیری پرداخت.",
        "sort_order": 60,
    },
    {
        "key": "tickets",
        "title": "🎫 تیکت و پشتیبانی",
        "description": "آموزش ثبت تیکت، ارسال عکس یا فایل مشکل و پیگیری پاسخ پشتیبانی.",
        "sort_order": 70,
    },
    {
        "key": "troubleshooting",
        "title": "🛠 خطاهای رایج و رفع مشکل",
        "description": "راهنمای حل مشکلات رایج اتصال، آپدیت ساب، کندی، خطای برنامه و قطع شدن سرویس.",
        "sort_order": 80,
    },
    {
        "key": "rules_security",
        "title": "🔐 قوانین و نکات مهم",
        "description": "نکات مهم درباره استفاده درست از سرویس، نگهداری لینک اشتراک و ارتباط با پشتیبانی.",
        "sort_order": 90,
    },
]


TUTORIALS = [
    {
        "category": "buy_activation",
        "title": "چطور سرویس جدید بخریم؟",
        "sort_order": 10,
        "body": """اگر برای اولین بار می‌خواهید سرویس بگیرید، از این آموزش شروع کنید.

مراحل خرید سرویس:

1. وارد ربات شوید و از منوی اصلی روی «خرید سرویس» بزنید.
2. دسته پلن‌ها را انتخاب کنید.
3. پلنی را انتخاب کنید که با مصرف شما مناسب‌تر است.
4. اگر ربات از شما نام سرویس خواست، می‌توانید یک نام ساده انگلیسی وارد کنید یا از ساخت خودکار استفاده کنید.
5. بعد از انتخاب پلن، وارد مرحله پرداخت می‌شوید.
6. مبلغ را دقیقاً همان عددی که ربات نمایش می‌دهد کارت‌به‌کارت کنید.
7. رسید پرداخت را در همان بخش برای ربات ارسال کنید.
8. بعد از تأیید رسید توسط ادمین فروش، سرویس شما ساخته و لینک اشتراک نمایش داده می‌شود.

نکته مهم:
تا وقتی رسید شما تأیید نشود، سرویس فعال نمی‌شود. پس بعد از پرداخت، حتماً رسید را ارسال کنید.

اگر اشتباهی از مرحله پرداخت خارج شدید:
از بخش سفارش‌ها یا پرداخت‌ها وضعیت سفارش خود را بررسی کنید. اگر نیاز بود، برای پشتیبانی تیکت بزنید.""",
    },
    {
        "category": "buy_activation",
        "title": "چطور پلن مناسب انتخاب کنیم؟",
        "sort_order": 20,
        "body": """برای انتخاب پلن، اول ببینید مصرف شما چقدر است.

اگر فقط برای پیام‌رسان‌ها، وب‌گردی سبک و کارهای روزمره استفاده می‌کنید:
پلن‌های کم‌حجم‌تر برای شما مناسب‌تر هستند.

اگر زیاد ویدیو می‌بینید، فایل دانلود می‌کنید یا استفاده روزانه سنگین دارید:
بهتر است پلن‌های حجیم‌تر انتخاب کنید.

اگر نمی‌خواهید هر ماه درگیر تمدید باشید:
پلن‌های چندماهه انتخاب بهتری هستند.

تفاوت کلی پلن‌ها:

پلن اقتصادی:
برای مصرف سبک و قیمت پایین‌تر مناسب است.

پلن محبوب:
برای استفاده معمولی و روزانه پیشنهاد می‌شود.

پلن حجیم:
برای مصرف زیاد، چند دستگاه یا استفاده طولانی‌تر مناسب است.

نکته مهم:
پلنی را انتخاب کنید که کمی بیشتر از مصرف واقعی شما باشد. این کار باعث می‌شود وسط دوره مجبور به خرید حجم اضافه نشوید.""",
    },
    {
        "category": "buy_activation",
        "title": "بعد از پرداخت چه اتفاقی می‌افتد؟",
        "sort_order": 30,
        "body": """بعد از اینکه پرداخت را انجام دادید، باید رسید را برای ربات بفرستید.

مراحل بعد از پرداخت:

1. ربات اطلاعات کارت و مبلغ دقیق را نمایش می‌دهد.
2. مبلغ را دقیقاً همان مقدار پرداخت کنید.
3. رسید پرداخت را به‌صورت عکس یا فایل برای ربات ارسال کنید.
4. سفارش شما در وضعیت «در انتظار بررسی رسید» قرار می‌گیرد.
5. ادمین فروش رسید را بررسی می‌کند.
6. اگر رسید درست باشد، پرداخت تأیید می‌شود.
7. بعد از تأیید، سرویس ساخته می‌شود.
8. لینک اشتراک یا همان ساب برای شما نمایش داده می‌شود.

اگر رسید رد شود:
دلیل رد شدن برای شما نمایش داده می‌شود. بعد از آن می‌توانید مشکل را بررسی کنید و در صورت نیاز با پشتیبانی ارتباط بگیرید.

اگر رسید منقضی شود:
یعنی در مهلت تعیین‌شده رسید ارسال نشده یا پیگیری نشده است. در این حالت باید دوباره پرداخت یا پیگیری را از مسیر درست انجام دهید.

نکته مهم:
رسید را در همان مرحله پرداخت ارسال کنید. ارسال رسید در جای اشتباه ممکن است باعث تأخیر در بررسی شود.""",
    },
    {
        "category": "subscription_management",
        "title": "لینک اشتراک یا ساب چیست؟",
        "sort_order": 10,
        "body": """ساب یا لینک اشتراک، لینکی است که بعد از فعال شدن سرویس به شما داده می‌شود.

شما این لینک را داخل برنامه‌هایی مثل v2rayNG در اندروید یا v2rayN در ویندوز وارد می‌کنید. بعد از وارد کردن ساب، برنامه به‌صورت خودکار سرورها را دریافت می‌کند.

ساب چه فایده‌ای دارد؟

1. لازم نیست کانفیگ‌ها را دستی وارد کنید.
2. اگر سرورها تغییر کنند، با بروزرسانی ساب اطلاعات جدید دریافت می‌شود.
3. مدیریت اتصال راحت‌تر می‌شود.
4. برای تمدید یا افزایش حجم، معمولاً نیازی به ساخت کانفیگ جدید ندارید.

نکته مهم:
لینک ساب شخصی است. آن را برای دیگران نفرستید و داخل کانال‌ها، سایت‌ها یا گروه‌ها منتشر نکنید.

اگر لینک ساب را گم کردید:
از بخش «سرویس‌های من» وارد سرویس خود شوید و دوباره لینک اشتراک را دریافت کنید.""",
    },
    {
        "category": "subscription_management",
        "title": "چطور لینک ساب را کپی کنیم؟",
        "sort_order": 20,
        "body": """برای اتصال به سرویس، اول باید لینک ساب خود را کپی کنید.

مراحل دریافت لینک ساب:

1. وارد ربات شوید.
2. از منوی اصلی روی «سرویس‌های من» بزنید.
3. سرویس موردنظر خود را انتخاب کنید.
4. در صفحه سرویس، دنبال بخش «لینک اشتراک» یا «پنل اشتراکی» بگردید.
5. روی دکمه مربوط به لینک اشتراک بزنید.
6. لینک را کپی کنید.
7. حالا لینک را داخل برنامه اتصال وارد کنید.

نکته مهم:
اگر لینک اشتراک آماده نبود، ممکن است سرویس هنوز در حال فعال‌سازی باشد. چند لحظه صبر کنید و دوباره بررسی کنید.

اگر بعد از چند دقیقه لینک نمایش داده نشد:
از بخش تیکت‌ها با پشتیبانی ارتباط بگیرید و شماره سفارش یا نام سرویس را ارسال کنید.""",
    },
    {
        "category": "subscription_management",
        "title": "چطور وضعیت حجم و تاریخ سرویس را ببینیم؟",
        "sort_order": 30,
        "body": """برای اینکه بدانید سرویس شما چقدر حجم دارد یا چه زمانی تمام می‌شود، از بخش سرویس‌های من استفاده کنید.

مراحل بررسی وضعیت سرویس:

1. وارد ربات شوید.
2. روی «سرویس‌های من» بزنید.
3. سرویس موردنظر را انتخاب کنید.
4. اطلاعات سرویس را ببینید.

در صفحه سرویس معمولاً این اطلاعات نمایش داده می‌شود:

نام سرویس
نوع پلن
حجم کل سرویس
حجم مصرف‌شده
تاریخ پایان سرویس
وضعیت سرویس
لینک اشتراک

اگر حجم شما تمام شده باشد:
می‌توانید از بخش «افزایش حجم» بسته حجم اضافه تهیه کنید.

اگر زمان سرویس تمام شده باشد:
باید سرویس را تمدید کنید.

نکته مهم:
افزایش حجم با تمدید فرق دارد. افزایش حجم فقط حجم سرویس را بیشتر می‌کند، اما تمدید زمان سرویس را افزایش می‌دهد.""",
    },
    {
        "category": "subscription_management",
        "title": "تمدید سرویس و افزایش حجم چه فرقی دارند؟",
        "sort_order": 40,
        "body": """این دو گزینه شبیه هم نیستند و هر کدام کاربرد جداگانه دارند.

تمدید سرویس:
زمان سرویس شما را بیشتر می‌کند. معمولاً وقتی تاریخ پایان سرویس نزدیک است، باید تمدید انجام دهید.

افزایش حجم:
فقط حجم سرویس را بیشتر می‌کند. افزایش حجم معمولاً زمان سرویس را بیشتر نمی‌کند.

مثال ساده:

اگر سرویس شما هنوز ۱۵ روز زمان دارد ولی حجم آن تمام شده:
باید «افزایش حجم» بگیرید.

اگر حجم دارید ولی تاریخ سرویس تمام شده:
باید «تمدید» انجام دهید.

اگر هم حجم تمام شده و هم زمان سرویس گذشته:
بهتر است سرویس را تمدید کنید یا از پشتیبانی راهنمایی بگیرید.

نکته مهم:
بعد از تمدید یا افزایش حجم، داخل برنامه اتصال یک بار ساب را بروزرسانی کنید.""",
    },
    {
        "category": "android",
        "title": "دانلود و نصب v2rayNG برای اندروید",
        "sort_order": 10,
        "body": """برای اتصال در گوشی‌های Android می‌توانید از برنامه v2rayNG استفاده کنید.

لینک پیشنهادی دانلود:
https://github.com/2dust/v2rayNG/releases

روش کلی نصب:

1. وارد لینک بالا شوید.
2. آخرین نسخه برنامه را پیدا کنید.
3. فایل APK مناسب گوشی خود را دانلود کنید.
4. اگر گوشی اجازه نصب نداد، نصب از منابع ناشناس را برای مرورگر یا فایل منیجر فعال کنید.
5. برنامه را نصب و اجرا کنید.

نکته مهم:
برنامه را از منبع نامعتبر دانلود نکنید. نسخه‌های دستکاری‌شده ممکن است امن نباشند.

اگر نمی‌دانید کدام فایل را دانلود کنید:
معمولاً نسخه universal یا arm64-v8a برای اکثر گوشی‌های جدید مناسب است. اگر مطمئن نیستید، از پشتیبانی راهنمایی بگیرید.""",
    },
    {
        "category": "android",
        "title": "اتصال در اندروید با v2rayNG",
        "sort_order": 20,
        "body": """برای اتصال در گوشی Android، بعد از نصب v2rayNG این مراحل را انجام دهید.

مراحل اتصال:

1. از ربات، لینک اشتراک یا ساب خود را کپی کنید.
2. وارد برنامه v2rayNG شوید.
3. منوی برنامه را باز کنید.
4. وارد بخش Subscription یا اشتراک شوید.
5. گزینه افزودن subscription را بزنید.
6. یک نام دلخواه بنویسید؛ مثلاً HowTooSee.
7. لینک ساب را در بخش آدرس وارد کنید.
8. ذخیره کنید.
9. گزینه Update subscription یا بروزرسانی اشتراک را بزنید.
10. بعد از نمایش سرورها، یکی از آن‌ها را انتخاب کنید.
11. روی دکمه اتصال بزنید.

اگر وصل نشدید:
یک سرور دیگر را انتخاب کنید و دوباره امتحان کنید.

اگر باز هم مشکل داشتید:
از بخش تیکت‌ها پیام بدهید و اسکرین‌شات خطا را ارسال کنید.""",
    },
    {
        "category": "android",
        "title": "بروزرسانی ساب در v2rayNG",
        "sort_order": 30,
        "body": """گاهی لازم است ساب خود را بروزرسانی کنید تا آخرین سرورها و تغییرات برای شما نمایش داده شود.

چه زمانی باید ساب را بروزرسانی کنیم؟

اگر سرورها وصل نمی‌شوند
اگر سرویس را تمدید کرده‌اید
اگر حجم اضافه خریده‌اید
اگر پشتیبانی گفته ساب را آپدیت کنید
اگر سرورها قدیمی یا خالی نمایش داده می‌شوند

مراحل بروزرسانی:

1. وارد برنامه v2rayNG شوید.
2. منوی برنامه را باز کنید.
3. وارد بخش Subscription شوید.
4. گزینه Update subscription یا بروزرسانی اشتراک را بزنید.
5. چند لحظه صبر کنید.
6. بعد از بروزرسانی، یکی از سرورها را انتخاب کنید و وصل شوید.

اگر بروزرسانی خطا داد:
اینترنت گوشی روشن باشد.
لینک ساب را درست وارد کرده باشید.
برنامه به اینترنت دسترسی داشته باشد.
اگر فیلترشکن دیگری روشن است، آن را خاموش کنید و دوباره امتحان کنید.""",
    },
    {
        "category": "windows",
        "title": "دانلود و نصب v2rayN برای ویندوز",
        "sort_order": 10,
        "body": """برای اتصال در Windows می‌توانید از برنامه v2rayN استفاده کنید.

لینک پیشنهادی دانلود:
https://github.com/2dust/v2rayN/releases

روش کلی نصب:

1. وارد لینک بالا شوید.
2. آخرین نسخه v2rayN را دانلود کنید.
3. برای ویندوز معمولاً فایل windows-64 مناسب است.
4. فایل zip را از حالت فشرده خارج کنید.
5. برنامه v2rayN را اجرا کنید.

نکته مهم:
برنامه v2rayN معمولاً به نصب پیچیده نیاز ندارد. فایل را Extract کنید و برنامه را اجرا کنید.

اگر Windows Defender یا آنتی‌ویروس هشدار داد:
از دانلود بودن فایل از منبع رسمی مطمئن شوید. اگر از جای دیگری دانلود کرده‌اید، فایل را پاک کنید و از لینک رسمی استفاده کنید.""",
    },
    {
        "category": "windows",
        "title": "اتصال در ویندوز با v2rayN",
        "sort_order": 20,
        "body": """برای اتصال در Windows بعد از اجرای v2rayN این مراحل را انجام دهید.

مراحل اتصال:

1. از ربات، لینک اشتراک یا ساب خود را کپی کنید.
2. وارد برنامه v2rayN شوید.
3. بخش Subscription یا Subscriptions را باز کنید.
4. یک subscription جدید اضافه کنید.
5. لینک ساب را وارد کنید.
6. گزینه Update subscription را بزنید.
7. بعد از نمایش سرورها، یکی از سرورها را انتخاب کنید.
8. گزینه System Proxy را فعال کنید.
9. مرورگر را باز کنید و اتصال را تست کنید.

نکته مهم:
در ویندوز فقط انتخاب سرور کافی نیست. معمولاً باید System Proxy هم فعال باشد تا مرورگر و برنامه‌ها از اتصال استفاده کنند.

اگر اینترنت قطع شد:
System Proxy را خاموش کنید، یک سرور دیگر انتخاب کنید و دوباره وصل شوید.""",
    },
    {
        "category": "packages",
        "title": "پکیج اختصاصی چیست؟",
        "sort_order": 10,
        "body": """پکیج اختصاصی یعنی بسته‌ای که برای همه کاربران نمایش داده نمی‌شود و فقط ادمین آن را برای یک کاربر مشخص فعال می‌کند.

پکیج اختصاصی چه زمانی استفاده می‌شود؟

وقتی کاربر نیاز خاصی دارد
وقتی چند سرویس یا چند زیرمجموعه لازم دارد
وقتی قیمت یا شرایط متفاوت است
وقتی ادمین با کاربر هماهنگ کرده است

نکته مهم:
پکیج‌های اختصاصی مثل پلن‌های عمومی نیستند. اگر برای شما پکیج تعریف نشده باشد، چیزی در این بخش نمی‌بینید.

برای دریافت پکیج اختصاصی:
باید با ادمین یا پشتیبانی هماهنگ کنید. بعد از فعال شدن پکیج، می‌توانید آن را از ربات مشاهده و استفاده کنید.""",
    },
    {
        "category": "packages",
        "title": "چطور پکیج اختصاصی را استفاده کنیم؟",
        "sort_order": 20,
        "body": """اگر ادمین برای شما پکیج اختصاصی فعال کرده باشد، از داخل ربات می‌توانید آن را ببینید.

مراحل استفاده:

1. وارد ربات شوید.
2. بخش مربوط به پکیج‌ها یا سرویس‌های اختصاصی را باز کنید.
3. پکیج فعال خود را انتخاب کنید.
4. شرایط و توضیحات پکیج را کامل بخوانید.
5. اگر نیاز به پرداخت داشت، پرداخت را انجام دهید.
6. بعد از تأیید، سرویس‌های مربوط به پکیج برای شما فعال می‌شوند.

نکته مهم:
اگر پکیج را نمی‌بینید، یعنی هنوز برای شما فعال نشده یا مهلت/شرایط آن تغییر کرده است.

در این حالت:
از بخش تیکت‌ها پیام بدهید و موضوع را با پشتیبانی مطرح کنید.""",
    },
    {
        "category": "payment_wallet",
        "title": "پرداخت کارت‌به‌کارت و ارسال رسید",
        "sort_order": 10,
        "body": """برای خرید سرویس یا شارژ کیف پول، ممکن است پرداخت کارت‌به‌کارت انجام دهید.

مراحل پرداخت:

1. مبلغ دقیق را از ربات ببینید.
2. شماره کارت نمایش داده‌شده را کپی کنید.
3. دقیقاً همان مبلغ را کارت‌به‌کارت کنید.
4. رسید پرداخت را به‌صورت عکس یا فایل ذخیره کنید.
5. رسید را در همان بخش پرداخت برای ربات ارسال کنید.
6. منتظر بررسی ادمین فروش بمانید.

نکات مهم:

مبلغ را کم یا زیاد نکنید.
رسید را در جای اشتباه نفرستید.
از پایا یا پل استفاده نکنید مگر پشتیبانی گفته باشد.
اگر برنامه پرداخت خطای محدودیت کارت داد، از موبایل‌بانک یا اینترنت‌بانک استفاده کنید.

تا وقتی رسید تأیید نشود، پرداخت نهایی نیست.""",
    },
    {
        "category": "payment_wallet",
        "title": "رسید منقضی یا رد شده یعنی چه؟",
        "sort_order": 20,
        "body": """گاهی سفارش یا رسید شما ممکن است رد یا منقضی شود.

رسید رد شده:
یعنی ادمین فروش رسید را بررسی کرده ولی مشکلی وجود داشته است. مثلاً مبلغ اشتباه بوده، رسید واضح نبوده یا اطلاعات پرداخت با سفارش هماهنگ نبوده است.

رسید منقضی شده:
یعنی مهلت ارسال یا پیگیری رسید تمام شده است.

در این حالت چه کار کنم؟

1. پیام ربات را کامل بخوانید.
2. اگر دلیل رد شدن نوشته شده، همان مورد را بررسی کنید.
3. اگر نیاز به راهنمایی دارید، تیکت بزنید.
4. شماره سفارش را برای پشتیبانی ارسال کنید.

نکته مهم:
برای جلوگیری از رد شدن رسید، همیشه مبلغ دقیق را پرداخت کنید و رسید واضح ارسال کنید.""",
    },
    {
        "category": "payment_wallet",
        "title": "کیف پول چیست و چطور شارژ می‌شود؟",
        "sort_order": 30,
        "body": """کیف پول یعنی موجودی داخلی شما در ربات.

با کیف پول می‌توانید بعداً سریع‌تر خرید انجام دهید، بدون اینکه برای هر خرید دوباره پرداخت جداگانه انجام دهید.

مراحل شارژ کیف پول:

1. وارد بخش کیف پول شوید.
2. گزینه افزایش موجودی را انتخاب کنید.
3. مبلغ موردنظر را وارد کنید.
4. پرداخت کارت‌به‌کارت انجام دهید.
5. رسید را ارسال کنید.
6. بعد از تأیید رسید، مبلغ به کیف پول شما اضافه می‌شود.

نکته مهم:
شارژ کیف پول هم نیاز به تأیید رسید دارد. یعنی تا وقتی ادمین فروش رسید را تأیید نکند، موجودی اضافه نمی‌شود.""",
    },
    {
        "category": "tickets",
        "title": "چطور تیکت پشتیبانی بزنیم؟",
        "sort_order": 10,
        "body": """اگر مشکلی دارید که با آموزش‌ها حل نمی‌شود، از بخش تیکت‌ها با پشتیبانی ارتباط بگیرید.

مراحل ثبت تیکت:

1. وارد ربات شوید.
2. روی «تیکت» یا «پشتیبانی» بزنید.
3. موضوع مناسب را انتخاب کنید.
4. عنوان کوتاه و واضح بنویسید.
5. مشکل را کامل توضیح دهید.
6. اگر عکس، ویدیو یا فایل لازم است، همان‌جا ارسال کنید.
7. منتظر پاسخ پشتیبانی بمانید.

برای اینکه سریع‌تر جواب بگیرید، این اطلاعات را بفرستید:

نام سرویس
نوع گوشی یا ویندوز
نام برنامه اتصال
اسکرین‌شات خطا
زمان شروع مشکل
کاری که قبل از مشکل انجام داده‌اید

نکته مهم:
پیام‌های کوتاه مثل «وصل نمیشه» کافی نیستند. هرچه توضیح کامل‌تر باشد، مشکل سریع‌تر حل می‌شود.""",
    },
    {
        "category": "troubleshooting",
        "title": "اگر سرویس وصل نشد چه کار کنیم؟",
        "sort_order": 10,
        "body": """اگر سرویس وصل نشد، قبل از ارسال تیکت این مراحل را انجام دهید.

مرحله 1:
اینترنت اصلی خود را بررسی کنید. بدون سرویس هم باید اینترنت داشته باشید.

مرحله 2:
یک سرور دیگر را انتخاب کنید و دوباره وصل شوید.

مرحله 3:
ساب را بروزرسانی کنید.

مرحله 4:
برنامه را کامل ببندید و دوباره باز کنید.

مرحله 5:
اگر در اندروید هستید، یک بار اینترنت گوشی را خاموش و روشن کنید.

مرحله 6:
اگر در ویندوز هستید، System Proxy را خاموش و دوباره روشن کنید.

مرحله 7:
تاریخ انقضا و حجم سرویس را در ربات بررسی کنید.

اگر هنوز مشکل حل نشد:
از بخش تیکت‌ها پیام بدهید و این موارد را ارسال کنید:

نام سرویس
نوع گوشی یا ویندوز
نام برنامه‌ای که استفاده می‌کنید
اسکرین‌شات خطا
اینکه مشکل از چه زمانی شروع شده است""",
    },
    {
        "category": "troubleshooting",
        "title": "سرورها نمایش داده نمی‌شوند یا ساب آپدیت نمی‌شود",
        "sort_order": 20,
        "body": """اگر لینک ساب را وارد کردید ولی سرورها نمایش داده نشدند، این موارد را بررسی کنید:

1. لینک ساب را دوباره از ربات کپی کنید.
2. مطمئن شوید لینک را کامل کپی کرده‌اید.
3. داخل برنامه گزینه Update subscription را بزنید.
4. اینترنت اصلی گوشی یا سیستم را بررسی کنید.
5. اگر فیلترشکن یا پروکسی دیگری روشن است، خاموش کنید.
6. برنامه را ببندید و دوباره باز کنید.
7. یک بار لینک ساب قبلی را حذف و دوباره اضافه کنید.

اگر هنوز مشکل حل نشد:
از بخش تیکت‌ها پیام بدهید و اسکرین‌شات خطا را ارسال کنید.

نکته مهم:
بعد از تمدید، افزایش حجم یا تغییر سرویس، بهتر است ساب را یک بار بروزرسانی کنید.""",
    },
    {
        "category": "rules_security",
        "title": "لینک ساب خود را برای دیگران نفرستید",
        "sort_order": 10,
        "body": """لینک ساب شما شخصی است و مخصوص سرویس خودتان ساخته شده است.

چرا نباید لینک ساب را منتشر کنم؟

1. ممکن است دیگران از حجم شما استفاده کنند.
2. ممکن است سرویس شما دچار اختلال شود.
3. ممکن است امنیت حساب شما پایین بیاید.
4. ممکن است مدیریت سرویس برای پشتیبانی سخت‌تر شود.

لینک ساب را کجا نفرستیم؟

داخل گروه‌ها
داخل کانال‌ها
برای افراد ناشناس
در سایت‌ها
در کامنت‌ها یا پیام‌های عمومی

اگر فکر می‌کنید لینک شما دست کسی افتاده:
از بخش سرویس‌های من، سرویس را بررسی کنید و اگر گزینه ریست لینک یا بروزرسانی لینک دارید از آن استفاده کنید. اگر مطمئن نیستید، تیکت بزنید.""",
    },
]


def upsert_category(conn: sqlite3.Connection, data: dict[str, Any], admin_id: int | None) -> int:
    ts = now_iso()
    row = conn.execute("SELECT id FROM tutorial_categories WHERE title = ?", (data["title"],)).fetchone()

    if row:
        category_id = int(row["id"])
        conn.execute(
            """
            UPDATE tutorial_categories
            SET description = ?, sort_order = ?, is_active = 1, updated_at = ?
            WHERE id = ?
            """,
            (data["description"], int(data["sort_order"]), ts, category_id),
        )
        return category_id

    cur = conn.execute(
        """
        INSERT INTO tutorial_categories
        (title, description, sort_order, is_active, created_by, created_at, updated_at)
        VALUES (?, ?, ?, 1, ?, ?, ?)
        """,
        (data["title"], data["description"], int(data["sort_order"]), admin_id, ts, ts),
    )
    return int(cur.lastrowid)


def upsert_tutorial(conn: sqlite3.Connection, category_id: int, data: dict[str, Any], admin_id: int | None) -> tuple[int, str]:
    ts = now_iso()

    # اول سعی می‌کنیم آموزش هم‌نام را داخل همین دسته پیدا کنیم.
    row = conn.execute(
        "SELECT id FROM tutorials WHERE title = ? AND category_id = ?",
        (data["title"], category_id),
    ).fetchone()

    # اگر قبلاً قبل از دسته‌بندی ساخته شده بود، با عنوان پیدا می‌کنیم و به دسته درست منتقل می‌کنیم.
    if not row:
        row = conn.execute("SELECT id FROM tutorials WHERE title = ? ORDER BY id ASC LIMIT 1", (data["title"],)).fetchone()

    if row:
        tutorial_id = int(row["id"])
        conn.execute(
            """
            UPDATE tutorials
            SET category_id = ?, body = ?, sort_order = ?, is_active = 1, updated_at = ?
            WHERE id = ?
            """,
            (category_id, data["body"].strip(), int(data["sort_order"]), ts, tutorial_id),
        )
        return tutorial_id, "updated"

    cur = conn.execute(
        """
        INSERT INTO tutorials
        (category_id, title, body, media_type, media_file_id, media_file_unique_id,
         sort_order, is_active, created_by, created_at, updated_at)
        VALUES (?, ?, ?, NULL, NULL, NULL, ?, 1, ?, ?, ?)
        """,
        (category_id, data["title"], data["body"].strip(), int(data["sort_order"]), admin_id, ts, ts),
    )
    return int(cur.lastrowid), "created"


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed HowTooSee initial tutorial categories and tutorials.")
    parser.add_argument("--project-dir", default="/home/PasarguardTelegramBot", help="مسیر پروژه. پیش‌فرض: /home/PasarguardTelegramBot")
    parser.add_argument("--db", default=None, help="مسیر bot.db. اگر ندهید از .env یا bot.db استفاده می‌شود.")
    parser.add_argument("--admin-id", type=int, default=None, help="chat id ادمین ثبت‌کننده. اگر ندهید از ADMIN_CHAT_IDS داخل .env برداشته می‌شود.")
    parser.add_argument("--no-backup", action="store_true", help="قبل از تغییرات بک‌آپ نگیرد.")
    parser.add_argument("--dry-run", action="store_true", help="فقط تست کند و چیزی ذخیره نکند.")
    args = parser.parse_args()

    project_dir = Path(args.project_dir).resolve()
    if not project_dir.exists():
        print(f"❌ مسیر پروژه پیدا نشد: {project_dir}")
        return 2

    dotenv = read_dotenv(project_dir / ".env")
    admin_id = args.admin_id or parse_first_admin_id(dotenv.get("ADMIN_CHAT_IDS") or os.getenv("ADMIN_CHAT_IDS"))

    db_path = resolve_db_path(project_dir, args.db)
    if not db_path.exists():
        print(f"⚠️ دیتابیس پیدا نشد و ساخته می‌شود: {db_path}")

    db_path.parent.mkdir(parents=True, exist_ok=True)

    if db_path.exists() and not args.no_backup and not args.dry_run:
        backup_path = db_path.with_suffix(db_path.suffix + f".tutorial_seed_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        shutil.copy2(db_path, backup_path)
        print(f"✅ بک‌آپ دیتابیس ساخته شد: {backup_path}")

    print(f"📍 project_dir = {project_dir}")
    print(f"📍 db_path     = {db_path}")
    print(f"📍 admin_id    = {admin_id or '-'}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    try:
        with closing(conn):
            ensure_schema(conn)

            category_ids: dict[str, int] = {}
            for cat in CATEGORIES:
                category_ids[cat["key"]] = upsert_category(conn, cat, admin_id)

            created = 0
            updated = 0
            for tutorial in TUTORIALS:
                category_id = category_ids[tutorial["category"]]
                _, action = upsert_tutorial(conn, category_id, tutorial, admin_id)
                if action == "created":
                    created += 1
                else:
                    updated += 1

            if args.dry_run:
                conn.rollback()
                print("🧪 dry-run انجام شد؛ چیزی ذخیره نشد.")
            else:
                conn.commit()

            total_cats = conn.execute("SELECT COUNT(*) AS c FROM tutorial_categories").fetchone()["c"]
            total_tuts = conn.execute("SELECT COUNT(*) AS c FROM tutorials").fetchone()["c"]

            print("✅ عملیات تمام شد.")
            print(f"📂 دسته‌ها: {total_cats}")
            print(f"📘 آموزش جدید: {created}")
            print(f"🔄 آموزش آپدیت‌شده: {updated}")
            print(f"📚 کل آموزش‌ها: {total_tuts}")

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


