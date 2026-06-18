from __future__ import annotations

import html
import logging
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    filters,
)


# =========================
# Config
# =========================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOT_BRAND_NAME = os.getenv("BOT_BRAND_NAME", "PasarGuard").strip()
BOT_DB_PATH = os.getenv("BOT_DB_PATH", "bot.sqlite3").strip()
PUBLIC_SUB_BASE_URL = os.getenv("PUBLIC_SUB_BASE_URL", "https://example.com/sub").strip().rstrip("/")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@support").strip()
SUPPORT_TEXT = os.getenv("SUPPORT_TEXT", "برای ارتباط با پشتیبانی روی دکمه زیر بزنید.").strip()
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper()
ENABLE_DEMO_PAYMENTS = os.getenv("ENABLE_DEMO_PAYMENTS", "true").strip().lower() in {"1", "true", "yes", "on"}
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").replace(";", ",").split(",")
    if x.strip().isdigit()
}

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("pasarguard-bot")


# =========================
# Persian labels
# =========================
BTN_BUY = "🛒 خرید سرویس"
BTN_MY_SERVICES = "📦 سرویس‌های من"
BTN_TEST = "🎁 تست اشتراک"
BTN_TRANSACTIONS = "💳 تراکنش‌ها"
BTN_WALLET = "💰 کیف پول"
BTN_SUPPORT = "📞 پشتیبانی"
BTN_REFERRAL = "💌 معرفی به دوستان"
BTN_ACCOUNT = "📊 اطلاعات حساب شما"
BTN_BACK = "↩️ بازگشت"


@dataclass(frozen=True)
class Plan:
    key: str
    title: str
    data_gb: float
    days: int
    price_toman: int
    category: str


PLAN_CATEGORIES = {
    "ready_1m": "🛍 پلن آماده (یک ماهه)",
    "custom_3m": "🛍 پلن دلخواه (یک تا سه ماهه)",
}

PLANS: dict[str, list[Plan]] = {
    "ready_1m": [
        Plan("r10", "10 گیگابایت", 10, 31, 100_000, "ready_1m"),
        Plan("r20", "20 گیگابایت", 20, 31, 190_000, "ready_1m"),
        Plan("r30", "30 گیگابایت", 30, 31, 270_000, "ready_1m"),
        Plan("r40", "40 گیگابایت", 40, 31, 340_000, "ready_1m"),
        Plan("r50", "50 گیگابایت", 50, 31, 400_000, "ready_1m"),
    ],
    "custom_3m": [
        Plan("c10", "10 گیگابایت", 10, 31, 100_000, "custom_3m"),
        Plan("c20", "20 گیگابایت", 20, 45, 190_000, "custom_3m"),
        Plan("c30", "30 گیگابایت", 30, 60, 270_000, "custom_3m"),
        Plan("c40", "40 گیگابایت", 40, 75, 340_000, "custom_3m"),
        Plan("c50", "50 گیگابایت", 50, 90, 400_000, "custom_3m"),
    ],
}

TEST_PLAN = Plan("test150", "150 مگابایت تست", 0.15, 1, 0, "test")


# =========================
# Helpers
# =========================
def toman(value: int | float) -> str:
    return f"{int(value):,} تومان"


def clean_text(value: str | None) -> str:
    return html.escape((value or "").strip())


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def pretty_date(value: str | None) -> str:
    if not value:
        return "-"
    try:
        dt = datetime.fromisoformat(value)
        return dt.strftime("%Y/%m/%d")
    except Exception:
        return value


def make_token() -> str:
    return secrets.token_urlsafe(10).replace("-", "").replace("_", "")[:16]


def main_menu() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(BTN_BUY)],
        [KeyboardButton(BTN_MY_SERVICES), KeyboardButton(BTN_TEST)],
        [KeyboardButton(BTN_TRANSACTIONS), KeyboardButton(BTN_WALLET), KeyboardButton(BTN_SUPPORT)],
        [KeyboardButton(BTN_REFERRAL), KeyboardButton(BTN_ACCOUNT)],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, input_field_placeholder="یک گزینه را انتخاب کنید…")


def chunked(items: list[InlineKeyboardButton], size: int = 2) -> list[list[InlineKeyboardButton]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def get_plan(category: str, plan_key: str) -> Plan | None:
    for plan in PLANS.get(category, []):
        if plan.key == plan_key:
            return plan
    return None


# =========================
# Database
# =========================
class BotDB:
    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        if self.path.parent and str(self.path.parent) != ".":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode = WAL;

                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    telegram_username TEXT,
                    first_name TEXT,
                    balance_toman INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS services (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    plan_name TEXT NOT NULL,
                    plan_type TEXT NOT NULL,
                    data_total_gb REAL NOT NULL,
                    data_left_gb REAL NOT NULL,
                    days_total INTEGER NOT NULL,
                    price_toman INTEGER NOT NULL,
                    sub_name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    sub_token TEXT NOT NULL UNIQUE,
                    subscription_url TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
                );

                CREATE TABLE IF NOT EXISTS transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    service_title TEXT,
                    amount_toman INTEGER NOT NULL,
                    gateway TEXT NOT NULL,
                    tracking_code TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
                );
                """
            )

    def upsert_user(self, tg_user: Any) -> None:
        now = utc_now().isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO users (telegram_id, telegram_username, first_name, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    telegram_username = excluded.telegram_username,
                    first_name = excluded.first_name
                """,
                (tg_user.id, tg_user.username or "", tg_user.first_name or "", now),
            )

    def get_user(self, telegram_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()

    def create_service(self, telegram_id: int, plan: Plan, sub_name: str) -> sqlite3.Row:
        token = make_token()
        created_at = utc_now()
        expires_at = created_at + timedelta(days=plan.days)
        title_prefix = "ShopVMess" if plan.category != "test" else "TestVMess"
        title = f"{title_prefix}_{secrets.randbelow(90000) + 10000}"
        subscription_url = f"{PUBLIC_SUB_BASE_URL}/{token}"
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO services (
                    telegram_id, title, plan_name, plan_type, data_total_gb, data_left_gb,
                    days_total, price_toman, sub_name, status, sub_token, subscription_url,
                    created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
                """,
                (
                    telegram_id,
                    title,
                    plan.title,
                    PLAN_CATEGORIES.get(plan.category, "تست اشتراک"),
                    plan.data_gb,
                    plan.data_gb,
                    plan.days,
                    plan.price_toman,
                    sub_name,
                    token,
                    subscription_url,
                    created_at.isoformat(),
                    expires_at.isoformat(),
                ),
            )
            conn.execute(
                """
                INSERT INTO transactions (telegram_id, status, service_title, amount_toman, gateway, tracking_code, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    telegram_id,
                    "paid" if plan.price_toman == 0 else "paid-demo",
                    title,
                    plan.price_toman,
                    "demo" if plan.price_toman else "free-test",
                    secrets.token_hex(4).upper(),
                    created_at.isoformat(),
                ),
            )
            service_id = cur.lastrowid
            return conn.execute("SELECT * FROM services WHERE id = ?", (service_id,)).fetchone()

    def list_services(self, telegram_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM services WHERE telegram_id = ? ORDER BY id DESC",
                (telegram_id,),
            ).fetchall()

    def get_service(self, service_id: int, telegram_id: int | None = None) -> sqlite3.Row | None:
        with self.connect() as conn:
            if telegram_id is None:
                return conn.execute("SELECT * FROM services WHERE id = ?", (service_id,)).fetchone()
            return conn.execute(
                "SELECT * FROM services WHERE id = ? AND telegram_id = ?",
                (service_id, telegram_id),
            ).fetchone()

    def revoke_service_link(self, service_id: int, telegram_id: int) -> sqlite3.Row | None:
        token = make_token()
        subscription_url = f"{PUBLIC_SUB_BASE_URL}/{token}"
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE services
                SET sub_token = ?, subscription_url = ?
                WHERE id = ? AND telegram_id = ?
                """,
                (token, subscription_url, service_id, telegram_id),
            )
            return conn.execute(
                "SELECT * FROM services WHERE id = ? AND telegram_id = ?",
                (service_id, telegram_id),
            ).fetchone()

    def delete_service(self, service_id: int, telegram_id: int) -> bool:
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM services WHERE id = ? AND telegram_id = ?", (service_id, telegram_id))
            return cur.rowcount > 0

    def list_transactions(self, telegram_id: int, limit: int = 10) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM transactions WHERE telegram_id = ? ORDER BY id DESC LIMIT ?",
                (telegram_id, limit),
            ).fetchall()

    def create_pending_transaction(self, telegram_id: int, amount_toman: int, gateway: str, service_title: str | None = None) -> sqlite3.Row:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO transactions (telegram_id, status, service_title, amount_toman, gateway, tracking_code, created_at)
                VALUES (?, 'pending', ?, ?, ?, ?, ?)
                """,
                (telegram_id, service_title, amount_toman, gateway, secrets.token_hex(4).upper(), utc_now().isoformat()),
            )
            return conn.execute("SELECT * FROM transactions WHERE id = ?", (cur.lastrowid,)).fetchone()

    def counts(self) -> dict[str, int]:
        with self.connect() as conn:
            users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
            services = conn.execute("SELECT COUNT(*) AS c FROM services").fetchone()["c"]
            transactions = conn.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"]
        return {"users": users, "services": services, "transactions": transactions}


# =========================
# Bot text builders
# =========================
def welcome_text(user_first_name: str | None = None) -> str:
    name = clean_text(user_first_name) or "دوست عزیز"
    return (
        f"سلام {name} 👋\n\n"
        f"به ربات <b>{clean_text(BOT_BRAND_NAME)}</b> خوش آمدید.\n"
        "از منوی زیر گزینه موردنظرتان را انتخاب کنید."
    )


def buy_text() -> str:
    return "🧩 <b>نوع سرویس را انتخاب کنید:</b>"


def plan_text(category: str) -> str:
    return f"📦 <b>{clean_text(PLAN_CATEGORIES.get(category, 'پلن'))}</b>\n\nپلن موردنظر را انتخاب کنید:"


def payment_text(plan: Plan, sub_name: str) -> str:
    return (
        "💳 <b>روش پرداخت را انتخاب کنید:</b>\n\n"
        f"📊 حجم انتخابی: <b>{clean_text(plan.title)}</b>\n"
        f"⏳ اعتبار: <b>{plan.days} روز</b>\n"
        f"💰 مبلغ: <b>{toman(plan.price_toman)}</b>\n"
        f"🏷 نام اشتراک: <b>{clean_text(sub_name)}</b>\n\n"
        "پس از پرداخت موفق، سرویس به‌صورت خودکار در بخش «سرویس‌های من» نمایش داده می‌شود."
    )


def service_detail_text(service: sqlite3.Row) -> str:
    status_map = {
        "active": "فعال ✅",
        "disabled": "غیرفعال ⛔",
        "frozen": "فریز شده ❄️",
    }
    return (
        "📦 <b>جزئیات سرویس</b>\n\n"
        f"🏷 نام اشتراک: <b>{clean_text(service['title'])}</b>\n"
        f"🛍 پلن: <b>{clean_text(service['plan_type'])}</b>\n"
        f"📊 حجم کل: <b>{service['data_total_gb']:g} گیگابایت</b>\n"
        f"💵 مبلغ پرداختی: <b>{toman(service['price_toman'])}</b>\n"
        f"✅ وضعیت: <b>{status_map.get(service['status'], clean_text(service['status']))}</b>\n"
        f"📅 پایان اعتبار: <b>{pretty_date(service['expires_at'])}</b>\n\n"
        "🔗 <b>ساب لینک:</b>\n"
        f"{clean_text(service['subscription_url'])}\n\n"
        "📊 برای اطلاع از وضعیت سرویس و بررسی، از دکمه‌های زیر استفاده کنید."
    )


def account_text(user: sqlite3.Row, services_count: int) -> str:
    username = f"@{user['telegram_username']}" if user["telegram_username"] else "ثبت نشده"
    return (
        "📊 <b>اطلاعات حساب شما</b>\n\n"
        f"🧾 شناسه کاربری: <code>{user['telegram_id']}</code>\n"
        f"👤 نام کاربری: <b>{clean_text(username)}</b>\n"
        f"💰 موجودی: <b>{toman(user['balance_toman'])}</b>\n"
        f"📦 سرویس‌های فعال: <b>{services_count}</b>"
    )


def transaction_status(status: str) -> str:
    return {
        "paid": "پرداخت شده ✅",
        "paid-demo": "پرداخت دمو ✅",
        "pending": "در انتظار ⏳",
        "failed": "ناموفق ❌",
    }.get(status, status)


# =========================
# Keyboards
# =========================
def buy_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(title, callback_data=f"buy:cat:{key}")]
        for key, title in PLAN_CATEGORIES.items()
    ]
    rows.append([InlineKeyboardButton(BTN_BACK, callback_data="back:main")])
    return InlineKeyboardMarkup(rows)


def plans_keyboard(category: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for plan in PLANS.get(category, []):
        rows.append(
            [
                InlineKeyboardButton(
                    f"{plan.title} - {toman(plan.price_toman)}",
                    callback_data=f"buy:plan:{category}:{plan.key}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(BTN_BACK, callback_data="back:buy")])
    return InlineKeyboardMarkup(rows)


def name_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ نه، نیازی ندارم", callback_data="name:auto")],
            [InlineKeyboardButton(BTN_BACK, callback_data="back:buy")],
        ]
    )


def payment_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🤖 پرداخت با ترمینال / درگاه", callback_data="pay:terminal")],
        [InlineKeyboardButton("🌘 پرداخت با Heleket", callback_data="pay:heleket")],
        [InlineKeyboardButton("🪙 ارز دیجیتال", callback_data="pay:crypto")],
        [InlineKeyboardButton("💰 کیف پول", callback_data="pay:wallet")],
    ]
    if ENABLE_DEMO_PAYMENTS:
        rows.append([InlineKeyboardButton("✅ پرداخت دمو برای تست", callback_data="pay:demo")])
    rows.append([InlineKeyboardButton(BTN_BACK, callback_data="back:buy")])
    return InlineKeyboardMarkup(rows)


def services_keyboard(services: Iterable[sqlite3.Row]) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(clean_text(s["title"]), callback_data=f"svc:view:{s['id']}") for s in services]
    rows = chunked(buttons, 2)
    rows.append([InlineKeyboardButton("🔎 جستجوی اشتراک", callback_data="svc:search")])
    rows.append([InlineKeyboardButton(BTN_BACK, callback_data="back:main")])
    return InlineKeyboardMarkup(rows)


def service_detail_keyboard(service_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🧩 کیوآر کد", callback_data=f"svc:qr:{service_id}"),
                InlineKeyboardButton("🔄 تغییر لینک", callback_data=f"svc:revoke:{service_id}"),
            ],
            [InlineKeyboardButton("💵 حجم باقی‌مانده", callback_data=f"svc:data:{service_id}")],
            [InlineKeyboardButton("⏳ زمان باقی‌مانده", callback_data=f"svc:time:{service_id}")],
            [InlineKeyboardButton("♻️ تمدید سرویس", callback_data=f"svc:renew:{service_id}")],
            [
                InlineKeyboardButton("🗂 افزایش حجم سرویس", callback_data=f"svc:add_data:{service_id}"),
                InlineKeyboardButton("✨ افزایش زمان سرویس", callback_data=f"svc:add_time:{service_id}"),
            ],
            [
                InlineKeyboardButton("📚 آموزش‌ها", callback_data="help:apps"),
                InlineKeyboardButton("🔗 لینک تکی اشتراک", callback_data=f"svc:single:{service_id}"),
            ],
            [InlineKeyboardButton("⚙️ تنظیمات اشتراک", callback_data=f"svc:settings:{service_id}")],
            [InlineKeyboardButton(BTN_BACK, callback_data="svc:list")],
        ]
    )


def service_settings_keyboard(service_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔁 انتقال اشتراک", callback_data=f"svc:placeholder:{service_id}:transfer")],
            [InlineKeyboardButton("✏️ تغییر نام اشتراک", callback_data=f"svc:placeholder:{service_id}:rename")],
            [InlineKeyboardButton("❄️ فریز اشتراک", callback_data=f"svc:placeholder:{service_id}:freeze")],
            [InlineKeyboardButton("↩️ عودت سرویس", callback_data=f"svc:placeholder:{service_id}:refund")],
            [InlineKeyboardButton("🗑 حذف سرویس", callback_data=f"svc:delete_confirm:{service_id}")],
            [InlineKeyboardButton(BTN_BACK, callback_data=f"svc:view:{service_id}")],
        ]
    )


def test_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("سرویس تست", callback_data="test:type")],
            [InlineKeyboardButton(BTN_BACK, callback_data="back:main")],
        ]
    )


def test_plan_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("150 مگابایت - رایگان", callback_data="test:create")],
            [InlineKeyboardButton(BTN_BACK, callback_data="back:main")],
        ]
    )


# =========================
# Middleware-ish helpers
# =========================
def db_from(context: ContextTypes.DEFAULT_TYPE) -> BotDB:
    return context.application.bot_data["db"]


def ensure_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user:
        db_from(context).upsert_user(update.effective_user)


def user_id(update: Update) -> int:
    if not update.effective_user:
        raise RuntimeError("No effective user")
    return update.effective_user.id


async def safe_edit_or_reply(update: Update, text: str, reply_markup: Any | None = None) -> None:
    if update.callback_query:
        await update.callback_query.edit_message_text(text=text, reply_markup=reply_markup)
    elif update.effective_chat:
        await update.effective_chat.send_message(text=text, reply_markup=reply_markup)


# =========================
# Commands and menu handlers
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_user(update, context)
    context.user_data.clear()
    await update.effective_chat.send_message(
        welcome_text(update.effective_user.first_name if update.effective_user else None),
        reply_markup=main_menu(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_user(update, context)
    await update.effective_chat.send_message(
        "راهنما 🧭\n\n"
        "از دکمه‌های منوی اصلی استفاده کنید.\n"
        "در این نسخه هنوز اتصال به API پنل فعال نشده و بخش پرداخت/تحویل به‌صورت ساختار اولیه آماده شده است.",
        reply_markup=main_menu(),
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_user(update, context)
    uid = user_id(update)
    if uid not in ADMIN_IDS:
        await update.effective_chat.send_message("⛔ شما دسترسی ادمین ندارید.")
        return
    counts = db_from(context).counts()
    await update.effective_chat.send_message(
        "📊 <b>آمار ربات</b>\n\n"
        f"👥 کاربران: <b>{counts['users']}</b>\n"
        f"📦 سرویس‌ها: <b>{counts['services']}</b>\n"
        f"💳 تراکنش‌ها: <b>{counts['transactions']}</b>"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_user(update, context)
    text = (update.message.text or "").strip() if update.message else ""

    if context.user_data.get("state") == "awaiting_sub_name":
        await receive_sub_name(update, context, text)
        return

    if text == BTN_BUY:
        await show_buy(update, context)
    elif text == BTN_MY_SERVICES:
        await show_services(update, context)
    elif text == BTN_TEST:
        await show_test(update, context)
    elif text == BTN_TRANSACTIONS:
        await show_transactions(update, context)
    elif text == BTN_WALLET:
        await show_wallet(update, context)
    elif text == BTN_SUPPORT:
        await show_support(update, context)
    elif text == BTN_REFERRAL:
        await show_referral(update, context)
    elif text == BTN_ACCOUNT:
        await show_account(update, context)
    else:
        await update.effective_chat.send_message("لطفاً از منوی زیر انتخاب کنید 👇", reply_markup=main_menu())


async def show_buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    if update.callback_query:
        await update.callback_query.edit_message_text(buy_text(), reply_markup=buy_keyboard())
    else:
        await update.effective_chat.send_message(buy_text(), reply_markup=buy_keyboard())


async def show_services(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_user(update, context)
    services = db_from(context).list_services(user_id(update))
    if not services:
        text = "📦 <b>سرویس‌های من</b>\n\nهنوز سرویسی برای شما ثبت نشده است."
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🛒 خرید سرویس", callback_data="back:buy")], [InlineKeyboardButton(BTN_BACK, callback_data="back:main")]]
        )
    else:
        text = "📌 برای دیدن مشخصات سرویس، روی آن بزنید."
        markup = services_keyboard(services)
    await safe_edit_or_reply(update, text, markup)


async def show_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await safe_edit_or_reply(update, "🧩 <b>نوع سرویس تست را انتخاب کنید:</b>", test_keyboard())


async def show_transactions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_user(update, context)
    rows = db_from(context).list_transactions(user_id(update), limit=10)
    if not rows:
        text = "💳 <b>تراکنش‌ها</b>\n\nهنوز تراکنشی برای شما ثبت نشده است."
    else:
        parts = ["🧾 <b>آخرین تراکنش‌های شما:</b>\n"]
        for idx, tx in enumerate(rows, start=1):
            title = tx["service_title"] or "افزایش موجودی"
            parts.append(
                f"\n<b>{idx}.</b> {transaction_status(tx['status'])}\n"
                f"📦 اشتراک: <b>{clean_text(title)}</b>\n"
                f"💰 مبلغ: <b>{toman(tx['amount_toman'])}</b>\n"
                f"🏦 درگاه: <b>{clean_text(tx['gateway'])}</b>\n"
                f"🏷 شناسه: <code>{clean_text(tx['tracking_code'])}</code>\n"
                f"📅 تاریخ: <b>{pretty_date(tx['created_at'])}</b>"
            )
        text = "\n".join(parts)
    await safe_edit_or_reply(update, text, InlineKeyboardMarkup([[InlineKeyboardButton(BTN_BACK, callback_data="back:main")]]))


async def show_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_user(update, context)
    user = db_from(context).get_user(user_id(update))
    balance = user["balance_toman"] if user else 0
    text = (
        "💰 <b>کیف پول</b>\n\n"
        f"موجودی فعلی شما: <b>{toman(balance)}</b>\n\n"
        "برای افزایش موجودی، روی دکمه زیر بزنید."
    )
    markup = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ افزایش موجودی", callback_data="wallet:add")],
            [InlineKeyboardButton(BTN_BACK, callback_data="back:main")],
        ]
    )
    await safe_edit_or_reply(update, text, markup)


async def show_support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = f"📞 <b>پشتیبانی</b>\n\n{clean_text(SUPPORT_TEXT)}"
    rows = []
    if SUPPORT_USERNAME.startswith("@"):
        rows.append([InlineKeyboardButton("ارسال پیام به پشتیبانی", url=f"https://t.me/{SUPPORT_USERNAME[1:]}")])
    rows.append([InlineKeyboardButton(BTN_BACK, callback_data="back:main")])
    await safe_edit_or_reply(update, text, InlineKeyboardMarkup(rows))


async def show_referral(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = user_id(update)
    bot_username = context.application.bot_data.get("bot_username", "")
    link = f"https://t.me/{bot_username}?start=ref_{uid}" if bot_username else f"ref_{uid}"
    await safe_edit_or_reply(
        update,
        "💌 <b>معرفی به دوستان</b>\n\n"
        "لینک اختصاصی شما:\n"
        f"{clean_text(link)}\n\n"
        "سیستم پورسانت در نسخه بعدی به این بخش وصل می‌شود.",
        InlineKeyboardMarkup([[InlineKeyboardButton(BTN_BACK, callback_data="back:main")]]),
    )


async def show_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_user(update, context)
    db = db_from(context)
    user = db.get_user(user_id(update))
    services = db.list_services(user_id(update))
    await safe_edit_or_reply(
        update,
        account_text(user, len(services)) if user else "حساب شما هنوز ثبت نشده است.",
        InlineKeyboardMarkup([[InlineKeyboardButton(BTN_BACK, callback_data="back:main")]]),
    )


# =========================
# Callback handlers
# =========================
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_user(update, context)
    query = update.callback_query
    if not query:
        return
    await query.answer()
    data = query.data or ""

    if data == "back:main":
        context.user_data.clear()
        await query.edit_message_text("به منوی اصلی برگشتید ✅")
        await query.message.reply_text("از منوی زیر انتخاب کنید:", reply_markup=main_menu())
        return

    if data == "back:buy":
        await show_buy(update, context)
        return

    if data == "menu:transactions":
        await show_transactions(update, context)
        return

    if data.startswith("buy:cat:"):
        category = data.split(":", 2)[2]
        await query.edit_message_text(plan_text(category), reply_markup=plans_keyboard(category))
        return

    if data.startswith("buy:plan:"):
        _, _, category, plan_key = data.split(":", 3)
        plan = get_plan(category, plan_key)
        if not plan:
            await query.edit_message_text("پلن انتخاب‌شده معتبر نیست.", reply_markup=buy_keyboard())
            return
        context.user_data["pending_plan"] = {
            "category": category,
            "plan_key": plan_key,
        }
        context.user_data["state"] = "awaiting_sub_name"
        await query.edit_message_text(
            "🏷 <b>اسم دلخواه اشتراک</b>\n"
            f"📦 پلن: <b>{clean_text(plan.title)}</b>\n\n"
            "در صورت نیاز، نام دلخواهتان را وارد کنید یا از دکمه زیر برای ساخت خودکار اسم استفاده کنید.",
            reply_markup=name_keyboard(),
        )
        return

    if data == "name:auto":
        await set_payment_step(update, context, sub_name=f"ShopVMess_{secrets.randbelow(90000) + 10000}")
        return

    if data.startswith("pay:"):
        await handle_payment(update, context, data.split(":", 1)[1])
        return

    if data == "svc:list":
        await show_services(update, context)
        return

    if data.startswith("svc:view:"):
        await show_service_detail(update, context, int(data.rsplit(":", 1)[1]))
        return

    if data.startswith("svc:settings:"):
        await show_service_settings(update, context, int(data.rsplit(":", 1)[1]))
        return

    if data.startswith("svc:revoke:"):
        await revoke_link(update, context, int(data.rsplit(":", 1)[1]))
        return

    if data.startswith("svc:data:"):
        await show_service_data(update, context, int(data.rsplit(":", 1)[1]))
        return

    if data.startswith("svc:time:"):
        await show_service_time(update, context, int(data.rsplit(":", 1)[1]))
        return

    if data.startswith("svc:single:"):
        await show_single_link(update, context, int(data.rsplit(":", 1)[1]))
        return

    if data.startswith("svc:qr:"):
        await query.edit_message_text(
            "🧩 تولید QR Code در این نسخه هنوز فعال نشده است.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(BTN_BACK, callback_data=f"svc:view:{data.rsplit(':', 1)[1]}")]]),
        )
        return

    if data.startswith("svc:renew:") or data.startswith("svc:add_data:") or data.startswith("svc:add_time:"):
        service_id = int(data.rsplit(":", 1)[1])
        await query.edit_message_text(
            "این بخش آماده اتصال به منطق تمدید/افزایش حجم است و فعلاً فقط UI آن ساخته شده است.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(BTN_BACK, callback_data=f"svc:view:{service_id}")]]),
        )
        return

    if data.startswith("svc:placeholder:"):
        parts = data.split(":")
        service_id = int(parts[2])
        await query.edit_message_text(
            "این قابلیت فعلاً به‌صورت نمایشی آماده شده و بعداً به منطق اصلی وصل می‌شود.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(BTN_BACK, callback_data=f"svc:settings:{service_id}")]]),
        )
        return

    if data.startswith("svc:delete_confirm:"):
        service_id = int(data.rsplit(":", 1)[1])
        await query.edit_message_text(
            "آیا از حذف این سرویس مطمئن هستید؟",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("بله، حذف شود 🗑", callback_data=f"svc:delete:{service_id}")],
                    [InlineKeyboardButton(BTN_BACK, callback_data=f"svc:settings:{service_id}")],
                ]
            ),
        )
        return

    if data.startswith("svc:delete:"):
        service_id = int(data.rsplit(":", 1)[1])
        ok = db_from(context).delete_service(service_id, user_id(update))
        await query.edit_message_text(
            "سرویس حذف شد ✅" if ok else "سرویس پیدا نشد.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📦 سرویس‌های من", callback_data="svc:list")]]),
        )
        return

    if data == "svc:search":
        await query.edit_message_text(
            "🔎 جستجوی اشتراک در نسخه بعدی فعال می‌شود.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(BTN_BACK, callback_data="svc:list")]]),
        )
        return

    if data == "test:type":
        await query.edit_message_text("🎁 <b>پلن تست را انتخاب کنید:</b>", reply_markup=test_plan_keyboard())
        return

    if data == "test:create":
        service = db_from(context).create_service(user_id(update), TEST_PLAN, "Free Test")
        await query.edit_message_text(
            "🎁 سرویس تست برای شما ساخته شد ✅\n\n" + service_detail_text(service),
            reply_markup=service_detail_keyboard(service["id"]),
        )
        return

    if data == "wallet:add":
        await query.edit_message_text(
            "➕ افزایش موجودی فعلاً به درگاه پرداخت وصل نشده است.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(BTN_BACK, callback_data="back:main")]]),
        )
        return

    if data == "help:apps":
        await query.edit_message_text(
            "📚 <b>آموزش‌ها</b>\n\n"
            "در نسخه نهایی می‌توانید آموزش Android، iOS، Windows و macOS را اینجا قرار دهید.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(BTN_BACK, callback_data="svc:list")]]),
        )
        return

    await query.edit_message_text("این گزینه هنوز آماده نشده است.")


async def receive_sub_name(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    if text in {BTN_BUY, BTN_MY_SERVICES, BTN_TEST, BTN_TRANSACTIONS, BTN_WALLET, BTN_SUPPORT, BTN_REFERRAL, BTN_ACCOUNT}:
        context.user_data.clear()
        await handle_text(update, context)
        return

    if not text:
        await update.effective_chat.send_message("لطفاً یک نام معتبر وارد کنید یا گزینه ساخت خودکار را بزنید.")
        return

    if len(text) > 32:
        await update.effective_chat.send_message("نام اشتراک بهتر است حداکثر ۳۲ کاراکتر باشد.")
        return

    await set_payment_step(update, context, sub_name=text)


async def set_payment_step(update: Update, context: ContextTypes.DEFAULT_TYPE, sub_name: str) -> None:
    pending = context.user_data.get("pending_plan") or {}
    category = pending.get("category")
    plan_key = pending.get("plan_key")
    plan = get_plan(category, plan_key) if category and plan_key else None
    if not plan:
        context.user_data.clear()
        await safe_edit_or_reply(update, "پلن انتخاب‌شده پیدا نشد. لطفاً دوباره تلاش کنید.", buy_keyboard())
        return

    context.user_data["pending_sub_name"] = sub_name
    context.user_data["state"] = "awaiting_payment"
    await safe_edit_or_reply(update, payment_text(plan, sub_name), payment_keyboard())


async def handle_payment(update: Update, context: ContextTypes.DEFAULT_TYPE, method: str) -> None:
    query = update.callback_query
    pending = context.user_data.get("pending_plan") or {}
    sub_name = context.user_data.get("pending_sub_name") or f"ShopVMess_{secrets.randbelow(90000) + 10000}"
    plan = get_plan(pending.get("category"), pending.get("plan_key"))
    if not plan:
        await query.edit_message_text("سفارش شما پیدا نشد. لطفاً دوباره خرید را شروع کنید.", reply_markup=buy_keyboard())
        return

    db = db_from(context)

    if method == "wallet":
        user = db.get_user(user_id(update))
        balance = user["balance_toman"] if user else 0
        if balance < plan.price_toman:
            await query.edit_message_text(
                "💰 موجودی کیف پول شما کافی نیست.\n\n"
                f"مبلغ سفارش: <b>{toman(plan.price_toman)}</b>\n"
                f"موجودی شما: <b>{toman(balance)}</b>",
                reply_markup=payment_keyboard(),
            )
            return

    if method == "demo":
        service = db.create_service(user_id(update), plan, sub_name)
        context.user_data.clear()
        await query.edit_message_text(
            "✅ پرداخت دمو موفق بود و سرویس ساخته شد.\n\n" + service_detail_text(service),
            reply_markup=service_detail_keyboard(service["id"]),
        )
        return

    gateway_titles = {
        "terminal": "terminal",
        "heleket": "heleket",
        "crypto": "crypto",
        "wallet": "wallet",
    }
    tx = db.create_pending_transaction(
        telegram_id=user_id(update),
        amount_toman=plan.price_toman,
        gateway=gateway_titles.get(method, method),
    )
    await query.edit_message_text(
        "🧾 سفارش شما ثبت شد.\n\n"
        "در این نسخه، پرداخت واقعی هنوز وصل نشده است.\n"
        f"وضعیت تراکنش: <b>{transaction_status(tx['status'])}</b>\n"
        f"شناسه پیگیری: <code>{clean_text(tx['tracking_code'])}</code>",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("💳 تراکنش‌ها", callback_data="menu:transactions")],
                [InlineKeyboardButton(BTN_BACK, callback_data="back:main")],
            ]
        ),
    )
    context.user_data.clear()


async def show_service_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, service_id: int) -> None:
    service = db_from(context).get_service(service_id, user_id(update))
    if not service:
        await safe_edit_or_reply(update, "سرویس پیدا نشد.", InlineKeyboardMarkup([[InlineKeyboardButton(BTN_BACK, callback_data="svc:list")]]))
        return
    await safe_edit_or_reply(update, service_detail_text(service), service_detail_keyboard(service_id))


async def show_service_settings(update: Update, context: ContextTypes.DEFAULT_TYPE, service_id: int) -> None:
    service = db_from(context).get_service(service_id, user_id(update))
    if not service:
        await safe_edit_or_reply(update, "سرویس پیدا نشد.", InlineKeyboardMarkup([[InlineKeyboardButton(BTN_BACK, callback_data="svc:list")]]))
        return
    await safe_edit_or_reply(
        update,
        "⚙️ <b>تنظیمات اشتراک</b>\n\n"
        f"🏷 اشتراک: <b>{clean_text(service['title'])}</b>\n"
        f"🧾 شماره سفارش: <code>{service['id']}</code>\n\n"
        "گزینه موردنظر را انتخاب کنید:",
        service_settings_keyboard(service_id),
    )


async def revoke_link(update: Update, context: ContextTypes.DEFAULT_TYPE, service_id: int) -> None:
    service = db_from(context).revoke_service_link(service_id, user_id(update))
    if not service:
        await safe_edit_or_reply(update, "سرویس پیدا نشد.", InlineKeyboardMarkup([[InlineKeyboardButton(BTN_BACK, callback_data="svc:list")]]))
        return
    await safe_edit_or_reply(
        update,
        "🔄 لینک اشتراک تغییر کرد ✅\n\n" + service_detail_text(service),
        service_detail_keyboard(service_id),
    )


async def show_service_data(update: Update, context: ContextTypes.DEFAULT_TYPE, service_id: int) -> None:
    service = db_from(context).get_service(service_id, user_id(update))
    if not service:
        await safe_edit_or_reply(update, "سرویس پیدا نشد.")
        return
    await safe_edit_or_reply(
        update,
        f"💵 حجم باقی‌مانده: <b>{service['data_left_gb']:g} گیگابایت</b>",
        InlineKeyboardMarkup([[InlineKeyboardButton(BTN_BACK, callback_data=f"svc:view:{service_id}")]]),
    )


async def show_service_time(update: Update, context: ContextTypes.DEFAULT_TYPE, service_id: int) -> None:
    service = db_from(context).get_service(service_id, user_id(update))
    if not service:
        await safe_edit_or_reply(update, "سرویس پیدا نشد.")
        return
    try:
        expires_at = datetime.fromisoformat(service["expires_at"])
        days_left = max(0, (expires_at - utc_now()).days)
    except Exception:
        days_left = service["days_total"]
    await safe_edit_or_reply(
        update,
        f"⏳ زمان باقی‌مانده: <b>{days_left} روز</b>",
        InlineKeyboardMarkup([[InlineKeyboardButton(BTN_BACK, callback_data=f"svc:view:{service_id}")]]),
    )


async def show_single_link(update: Update, context: ContextTypes.DEFAULT_TYPE, service_id: int) -> None:
    service = db_from(context).get_service(service_id, user_id(update))
    if not service:
        await safe_edit_or_reply(update, "سرویس پیدا نشد.")
        return
    await safe_edit_or_reply(
        update,
        "🔗 <b>لینک تکی اشتراک</b>\n\n"
        f"{clean_text(service['subscription_url'])}",
        InlineKeyboardMarkup([[InlineKeyboardButton(BTN_BACK, callback_data=f"svc:view:{service_id}")]]),
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat:
        await update.effective_chat.send_message("یک خطای غیرمنتظره رخ داد. لطفاً دوباره تلاش کنید.")


async def post_init(application: Application) -> None:
    me = await application.bot.get_me()
    application.bot_data["bot_username"] = me.username or ""
    await application.bot.set_my_commands(
        [
            ("start", "شروع ربات"),
            ("help", "راهنما"),
            ("stats", "آمار ادمین"),
        ]
    )
    logger.info("Bot started as @%s", me.username)


async def menu_callback_bridge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Reserved for future menu aliases.
    pass


def build_app() -> Application:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is missing. Create .env from .env.example and set BOT_TOKEN.")

    db = BotDB(BOT_DB_PATH)
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .defaults(Defaults(parse_mode=ParseMode.HTML))
        .post_init(post_init)
        .build()
    )
    app.bot_data["db"] = db

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)
    return app


def main() -> None:
    app = build_app()
    logger.info("Polling started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
