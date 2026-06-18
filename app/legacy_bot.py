import asyncio
import html
import logging
import os
import random
import re
import secrets
import sqlite3
import string
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote_plus

from aiogram import Bot, Dispatcher, F, Router
try:
    from aiogram import BaseMiddleware
except Exception:  # aiogram fallback
    from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from dotenv import load_dotenv

from app.bootstrap import bootstrap_phase1
from app.services.text_template_service import render_template_sync
from app.routers.tickets import ticket_router
from app.routers.broadcast import broadcast_router
from app.routers.reports import reports_router
from app.routers.backup import backup_router
from app.routers.pasarguard import pasarguard_router
from app.services.scheduled_backup_service import start_auto_backup_scheduler
from app.services.pasarguard_user_service import (
    apply_template_to_remote_user,
    create_remote_user_for_service,
    reset_remote_user_usage,
    revoke_remote_subscription,
    set_remote_user_status,
    sync_remote_user_from_local,
    update_remote_user_limit,
)
from app.routers.plans import plans_router
from app.routers.settings import settings_router

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("howtoosee-bot")


# -----------------------------
# Config
# -----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "HowToSeeWorld_bot").strip().lstrip("@")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "HowTooSeeWorld").strip().lstrip("@")
CHANNEL_LINK = os.getenv("CHANNEL_LINK", f"https://t.me/{CHANNEL_USERNAME}").strip()
BRAND_NAME = os.getenv("BRAND_NAME", "HowTooSee | Premium VPN").strip()
DATABASE_PATH = os.getenv("DATABASE_PATH", "bot.db").strip()
SUBSCRIPTION_BASE_URL = os.getenv("SUBSCRIPTION_BASE_URL", "https://example.com/sub").rstrip("/")
REFERRAL_COMMISSION_PERCENT = int(os.getenv("REFERRAL_COMMISSION_PERCENT", "10"))
REFERRED_DISCOUNT_PERCENT = int(os.getenv("REFERRED_DISCOUNT_PERCENT", "5"))
FREE_TEST_MB = int(os.getenv("FREE_TEST_MB", "150"))
WALLET_MIN_TOPUP = int(os.getenv("WALLET_MIN_TOPUP", "50000"))
SERVICE_NAME_PREFIX = "howtosee_"
ADMIN_CHAT_IDS_RAW = os.getenv("ADMIN_CHAT_IDS", "").strip()


def parse_id_set(value: str) -> set[int]:
    ids: set[int] = set()
    for part in re.split(r"[,\s]+", value or ""):
        part = normalize_digits(part.strip()) if "normalize_digits" in globals() else part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return ids


BOOTSTRAP_SUPER_ADMIN_IDS = parse_id_set(ADMIN_CHAT_IDS_RAW)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Create .env from .env.example and set your bot token.")

TEHRAN_TZ = timezone(timedelta(hours=3, minutes=30))


# -----------------------------
# Demo catalog - no PasarGuard API yet
# -----------------------------
@dataclass(frozen=True)
class Plan:
    key: str
    title: str
    data_gb: float
    days: int
    price: int
    category: str
    badge: str


@dataclass(frozen=True)
class DataAddon:
    key: str
    title: str
    data_gb: float
    price: int
    badge: str


@dataclass(frozen=True)
class Coupon:
    code: str
    percent: int
    title: str


PLANS: dict[str, Plan] = {
    "m_10": Plan("m_10", "۱۰ گیگابایت | یک‌ماهه", 10, 31, 100_000, "monthly", "اقتصادی"),
    "m_20": Plan("m_20", "۲۰ گیگابایت | یک‌ماهه", 20, 31, 190_000, "monthly", "محبوب"),
    "m_30": Plan("m_30", "۳۰ گیگابایت | یک‌ماهه", 30, 31, 270_000, "monthly", "متعادل"),
    "m_40": Plan("m_40", "۴۰ گیگابایت | یک‌ماهه", 40, 31, 340_000, "monthly", "حرفه‌ای"),
    "m_50": Plan("m_50", "۵۰ گیگابایت | یک‌ماهه", 50, 31, 400_000, "monthly", "پرفروش"),
    "q_60": Plan("q_60", "۶۰ گیگابایت | سه‌ماهه", 60, 93, 540_000, "quarterly", "سه‌ماهه"),
    "q_100": Plan("q_100", "۱۰۰ گیگابایت | سه‌ماهه", 100, 93, 850_000, "quarterly", "پیشنهادی"),
    "q_150": Plan("q_150", "۱۵۰ گیگابایت | سه‌ماهه", 150, 93, 1_180_000, "quarterly", "حجیم"),
}

DATA_ADDON_PACKAGES: dict[str, DataAddon] = {
    "add_5": DataAddon("add_5", "۵ گیگابایت حجم اضافه", 5, 39_000, "شروع اقتصادی"),
    "add_10": DataAddon("add_10", "۱۰ گیگابایت حجم اضافه", 10, 69_000, "انتخاب هوشمند"),
    "add_20": DataAddon("add_20", "۲۰ گیگابایت حجم اضافه", 20, 129_000, "پیشنهادی"),
    "add_50": DataAddon("add_50", "۵۰ گیگابایت حجم اضافه", 50, 299_000, "به‌صرفه‌ترین"),
}

FREE_SERVICE_TYPES: dict[str, dict[str, str]] = {
    "standard": {"title": "🌍 سرویس رایگان استاندارد", "subtitle": "برای تست اتصال روزمره"},
    "speed": {"title": "⚡ سرویس رایگان پرسرعت", "subtitle": "برای تست سرعت و پایداری"},
}

FREE_TEST_PLANS: dict[str, Plan] = {
    "free_standard_150": Plan("free_standard_150", f"{FREE_TEST_MB} مگابایت | رایگان استاندارد", FREE_TEST_MB / 1024, 3, 0, "free:standard", "رایگان"),
    "free_speed_150": Plan("free_speed_150", f"{FREE_TEST_MB} مگابایت | رایگان پرسرعت", FREE_TEST_MB / 1024, 3, 0, "free:speed", "رایگان"),
}

DEMO_COUPONS: dict[str, Coupon] = {
    "HOWTOSEE10": Coupon("HOWTOSEE10", 10, "تخفیف عمومی HowTooSee"),
    "VIP20": Coupon("VIP20", 20, "تخفیف ویژه VIP"),
    "TEST5": Coupon("TEST5", 5, "کد تست پنج درصدی"),
}

# Demo in-memory storage. Later this should be persisted or replaced by payment/order DB fields.
pending_names: dict[int, str] = {}
order_discounts: dict[int, dict[str, Any]] = {}


# -----------------------------
# Text helpers
# -----------------------------
def now_iso() -> str:
    return datetime.now(TEHRAN_TZ).isoformat(timespec="seconds")


def fmt_money(amount: int) -> str:
    return f"{int(amount):,}".replace(",", "٬") + " تومان"


def fmt_number(value: int | float) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{value:,}".replace(",", "٬")


def h(value: Any) -> str:
    return html.escape(str(value or ""))


def header(title: str, subtitle: str = "") -> str:
    sub = f"\n<code>{h(subtitle)}</code>" if subtitle else ""
    return f"<b>{h(title)}</b>{sub}\n\n"


def normalize_digits(value: str) -> str:
    fa = "۰۱۲۳۴۵۶۷۸۹"
    ar = "٠١٢٣٤٥٦٧٨٩"
    for i, ch in enumerate(fa):
        value = value.replace(ch, str(i))
    for i, ch in enumerate(ar):
        value = value.replace(ch, str(i))
    return value


def parse_amount(value: str) -> Optional[int]:
    value = normalize_digits(value).replace(",", "").replace("٬", "").strip()
    value = re.sub(r"[^0-9]", "", value)
    if not value:
        return None
    return int(value)


def make_token() -> str:
    return secrets.token_urlsafe(14).replace("-", "").replace("_", "")[:20]


def make_service_name(telegram_id: int) -> str:
    suffix = str(telegram_id)[-5:]
    return f"{SERVICE_NAME_PREFIX}{suffix}_{random.randint(10, 99)}"


def validate_service_name_input(raw: str) -> tuple[bool, str, str]:
    """Validate the user-entered base name. We do NOT silently clean/fix it."""
    value = (raw or "").strip()
    if not value:
        return False, "", "نام نمی‌تواند خالی باشد. اگر نام دلخواه نمی‌خواهید، از دکمه ساخت خودکار استفاده کنید."
    if value.lower().startswith(SERVICE_NAME_PREFIX):
        return False, "", f"لطفاً بخش <code>{SERVICE_NAME_PREFIX}</code> را وارد نکنید؛ ربات خودش آن را اول نام می‌گذارد."
    if len(value) < 3 or len(value) > 20:
        return False, "", "نام باید بین ۳ تا ۲۰ کاراکتر باشد."
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return False, "", "نام فقط می‌تواند شامل حروف انگلیسی، عدد، خط تیره <code>-</code> و آندرلاین <code>_</code> باشد. فاصله و حروف فارسی مجاز نیست."
    return True, f"{SERVICE_NAME_PREFIX}{value}", ""


def subscription_link(service: sqlite3.Row) -> str:
    try:
        if "pasarguard_subscription_url" in service.keys() and service["pasarguard_subscription_url"]:
            return str(service["pasarguard_subscription_url"])
    except Exception:
        pass
    return f"{SUBSCRIPTION_BASE_URL}/{service['token']}"


# -----------------------------
# Database
# -----------------------------
class DB:
    def __init__(self, path: str) -> None:
        self.path = path
        parent = Path(path).parent
        if parent != Path("."):
            parent.mkdir(parents=True, exist_ok=True)
        self.init()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self) -> None:
        with closing(self.connect()) as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER UNIQUE NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    referral_code TEXT UNIQUE NOT NULL,
                    referred_by_telegram_id INTEGER,
                    wallet_balance INTEGER NOT NULL DEFAULT 0,
                    total_referral_earned INTEGER NOT NULL DEFAULT 0,
                    free_test_used INTEGER NOT NULL DEFAULT 0,
                    first_purchase_done INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS services (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_telegram_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    plan_key TEXT NOT NULL,
                    plan_title TEXT NOT NULL,
                    data_gb REAL NOT NULL,
                    days INTEGER NOT NULL,
                    price INTEGER NOT NULL,
                    paid_amount INTEGER NOT NULL,
                    token TEXT UNIQUE NOT NULL,
                    expires_at TEXT NOT NULL,
                    data_used_mb INTEGER NOT NULL DEFAULT 0,
                    is_test INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_telegram_id INTEGER NOT NULL,
                    service_id INTEGER,
                    plan_key TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    discount_amount INTEGER NOT NULL DEFAULT 0,
                    wallet_used INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    payment_method TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS wallet_transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_telegram_id INTEGER NOT NULL,
                    amount INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    description TEXT NOT NULL,
                    related_user_id INTEGER,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS referrals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    referrer_telegram_id INTEGER NOT NULL,
                    referred_telegram_id INTEGER UNIQUE NOT NULL,
                    rewarded INTEGER NOT NULL DEFAULT 0,
                    first_order_id INTEGER,
                    commission_amount INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    rewarded_at TEXT
                );
                """
            )
            conn.commit()

    def get_user(self, telegram_id: int) -> Optional[sqlite3.Row]:
        with closing(self.connect()) as conn:
            return conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()

    def get_user_by_referral_code(self, code: str) -> Optional[sqlite3.Row]:
        with closing(self.connect()) as conn:
            return conn.execute("SELECT * FROM users WHERE referral_code = ?", (code,)).fetchone()

    def ensure_user(self, telegram_id: int, username: Optional[str], first_name: Optional[str], referred_by_telegram_id: Optional[int] = None) -> sqlite3.Row:
        if referred_by_telegram_id == telegram_id:
            referred_by_telegram_id = None
        user = self.get_user(telegram_id)
        if user:
            with closing(self.connect()) as conn:
                conn.execute("UPDATE users SET username = ?, first_name = ? WHERE telegram_id = ?", (username, first_name, telegram_id))
                conn.commit()
            return self.get_user(telegram_id)  # type: ignore[return-value]

        referral_code = str(telegram_id)
        with closing(self.connect()) as conn:
            conn.execute(
                """
                INSERT INTO users (telegram_id, username, first_name, referral_code, referred_by_telegram_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (telegram_id, username, first_name, referral_code, referred_by_telegram_id, now_iso()),
            )
            if referred_by_telegram_id and referred_by_telegram_id != telegram_id:
                conn.execute(
                    "INSERT OR IGNORE INTO referrals (referrer_telegram_id, referred_telegram_id, created_at) VALUES (?, ?, ?)",
                    (referred_by_telegram_id, telegram_id, now_iso()),
                )
            conn.commit()
        return self.get_user(telegram_id)  # type: ignore[return-value]

    def add_wallet(self, telegram_id: int, amount: int, tx_type: str, description: str, related_user_id: Optional[int] = None) -> None:
        with closing(self.connect()) as conn:
            conn.execute("UPDATE users SET wallet_balance = wallet_balance + ? WHERE telegram_id = ?", (amount, telegram_id))
            if amount > 0 and tx_type == "referral_commission":
                conn.execute("UPDATE users SET total_referral_earned = total_referral_earned + ? WHERE telegram_id = ?", (amount, telegram_id))
            conn.execute(
                """
                INSERT INTO wallet_transactions (user_telegram_id, amount, type, description, related_user_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (telegram_id, amount, tx_type, description, related_user_id, now_iso()),
            )
            conn.commit()

    def create_order(self, telegram_id: int, plan_key: str, amount: int, discount: int, wallet_used: int, status: str, method: str) -> int:
        with closing(self.connect()) as conn:
            cur = conn.execute(
                """
                INSERT INTO orders (user_telegram_id, plan_key, amount, discount_amount, wallet_used, status, payment_method, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (telegram_id, plan_key, amount, discount, wallet_used, status, method, now_iso()),
            )
            conn.commit()
            return int(cur.lastrowid)

    def update_order_discount(self, order_id: int, telegram_id: int, discount: int) -> None:
        with closing(self.connect()) as conn:
            conn.execute("UPDATE orders SET discount_amount = ? WHERE id = ? AND user_telegram_id = ? AND status = 'pending'", (discount, order_id, telegram_id))
            conn.commit()

    def update_order_service(self, order_id: int, service_id: int) -> None:
        with closing(self.connect()) as conn:
            conn.execute("UPDATE orders SET service_id = ? WHERE id = ?", (service_id, order_id))
            conn.commit()

    def get_order(self, order_id: int, telegram_id: int) -> Optional[sqlite3.Row]:
        with closing(self.connect()) as conn:
            return conn.execute("SELECT * FROM orders WHERE id = ? AND user_telegram_id = ?", (order_id, telegram_id)).fetchone()

    def create_service(self, telegram_id: int, name: str, plan: Plan, paid_amount: int, is_test: bool = False) -> int:
        token = make_token()
        expires = datetime.now(TEHRAN_TZ) + timedelta(days=plan.days)
        with closing(self.connect()) as conn:
            cur = conn.execute(
                """
                INSERT INTO services
                (user_telegram_id, name, plan_key, plan_title, data_gb, days, price, paid_amount, token, expires_at, is_test, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (telegram_id, name, plan.key, plan.title, plan.data_gb, plan.days, plan.price, paid_amount, token, expires.isoformat(timespec="seconds"), 1 if is_test else 0, now_iso()),
            )
            if is_test:
                conn.execute("UPDATE users SET free_test_used = 1 WHERE telegram_id = ?", (telegram_id,))
            elif paid_amount > 0:
                conn.execute("UPDATE users SET first_purchase_done = 1 WHERE telegram_id = ?", (telegram_id,))
            conn.commit()
            return int(cur.lastrowid)

    def mark_first_purchase_done(self, telegram_id: int) -> None:
        with closing(self.connect()) as conn:
            conn.execute("UPDATE users SET first_purchase_done = 1 WHERE telegram_id = ?", (telegram_id,))
            conn.commit()

    def add_data_to_service(self, service_id: int, telegram_id: int, data_gb: float) -> None:
        with closing(self.connect()) as conn:
            conn.execute("UPDATE services SET data_gb = data_gb + ?, status = 'active' WHERE id = ? AND user_telegram_id = ?", (data_gb, service_id, telegram_id))
            conn.commit()

    def renew_service(self, service_id: int, telegram_id: int, plan: Plan, paid_amount: int) -> None:
        expires = datetime.now(TEHRAN_TZ) + timedelta(days=plan.days)
        with closing(self.connect()) as conn:
            conn.execute(
                """
                UPDATE services
                SET plan_key = ?, plan_title = ?, data_gb = ?, days = ?, price = ?, paid_amount = ?,
                    expires_at = ?, data_used_mb = 0, is_test = 0, status = 'active'
                WHERE id = ? AND user_telegram_id = ?
                """,
                (plan.key, plan.title, plan.data_gb, plan.days, plan.price, paid_amount, expires.isoformat(timespec="seconds"), service_id, telegram_id),
            )
            if paid_amount > 0:
                conn.execute("UPDATE users SET first_purchase_done = 1 WHERE telegram_id = ?", (telegram_id,))
            conn.commit()

    def get_service(self, service_id: int, telegram_id: Optional[int] = None) -> Optional[sqlite3.Row]:
        with closing(self.connect()) as conn:
            if telegram_id is None:
                return conn.execute("SELECT * FROM services WHERE id = ?", (service_id,)).fetchone()
            return conn.execute("SELECT * FROM services WHERE id = ? AND user_telegram_id = ?", (service_id, telegram_id)).fetchone()

    def list_services(self, telegram_id: int) -> list[sqlite3.Row]:
        with closing(self.connect()) as conn:
            return list(conn.execute("SELECT * FROM services WHERE user_telegram_id = ? ORDER BY id DESC", (telegram_id,)).fetchall())

    def list_orders(self, telegram_id: int, limit: int = 7) -> list[sqlite3.Row]:
        with closing(self.connect()) as conn:
            return list(conn.execute("SELECT * FROM orders WHERE user_telegram_id = ? ORDER BY id DESC LIMIT ?", (telegram_id, limit)).fetchall())

    def list_wallet_transactions(self, telegram_id: int, limit: int = 5) -> list[sqlite3.Row]:
        with closing(self.connect()) as conn:
            return list(conn.execute("SELECT * FROM wallet_transactions WHERE user_telegram_id = ? ORDER BY id DESC LIMIT ?", (telegram_id, limit)).fetchall())

    def referral_stats(self, telegram_id: int) -> dict[str, int]:
        with closing(self.connect()) as conn:
            total = conn.execute("SELECT COUNT(*) AS c FROM referrals WHERE referrer_telegram_id = ?", (telegram_id,)).fetchone()["c"]
            rewarded = conn.execute("SELECT COUNT(*) AS c FROM referrals WHERE referrer_telegram_id = ? AND rewarded = 1", (telegram_id,)).fetchone()["c"]
            earned = conn.execute("SELECT COALESCE(SUM(commission_amount), 0) AS s FROM referrals WHERE referrer_telegram_id = ? AND rewarded = 1", (telegram_id,)).fetchone()["s"]
            return {"total": int(total), "rewarded": int(rewarded), "pending": int(total) - int(rewarded), "earned": int(earned)}

    def reward_referrer_if_needed(self, buyer_telegram_id: int, order_id: int, paid_amount: int) -> Optional[tuple[int, int]]:
        with closing(self.connect()) as conn:
            ref = conn.execute("SELECT * FROM referrals WHERE referred_telegram_id = ? AND rewarded = 0", (buyer_telegram_id,)).fetchone()
            if not ref:
                return None
            referrer_id = int(ref["referrer_telegram_id"])
            if referrer_id == buyer_telegram_id:
                conn.execute("DELETE FROM referrals WHERE referred_telegram_id = ?", (buyer_telegram_id,))
                conn.execute("UPDATE users SET referred_by_telegram_id = NULL WHERE telegram_id = ?", (buyer_telegram_id,))
                conn.commit()
                return None
            commission = int(paid_amount * REFERRAL_COMMISSION_PERCENT / 100)
            if commission <= 0:
                return None
            conn.execute("UPDATE users SET wallet_balance = wallet_balance + ?, total_referral_earned = total_referral_earned + ? WHERE telegram_id = ?", (commission, commission, referrer_id))
            conn.execute(
                "INSERT INTO wallet_transactions (user_telegram_id, amount, type, description, related_user_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (referrer_id, commission, "referral_commission", f"پورسانت خرید اول کاربر {buyer_telegram_id}", buyer_telegram_id, now_iso()),
            )
            conn.execute(
                "UPDATE referrals SET rewarded = 1, first_order_id = ?, commission_amount = ?, rewarded_at = ? WHERE referred_telegram_id = ?",
                (order_id, commission, now_iso(), buyer_telegram_id),
            )
            conn.commit()
            return referrer_id, commission

    def revoke_service_link(self, service_id: int, telegram_id: int) -> str:
        token = make_token()
        with closing(self.connect()) as conn:
            conn.execute("UPDATE services SET token = ? WHERE id = ? AND user_telegram_id = ?", (token, service_id, telegram_id))
            conn.commit()
        return token

    def rename_service(self, service_id: int, telegram_id: int, new_name: str) -> None:
        with closing(self.connect()) as conn:
            conn.execute("UPDATE services SET name = ? WHERE id = ? AND user_telegram_id = ?", (new_name, service_id, telegram_id))
            conn.commit()

    def delete_service(self, service_id: int, telegram_id: int) -> None:
        with closing(self.connect()) as conn:
            conn.execute("UPDATE services SET status = 'deleted' WHERE id = ? AND user_telegram_id = ?", (service_id, telegram_id))
            conn.commit()


# -----------------------------
# Admin / management persistence
# -----------------------------
def ensure_admin_schema() -> None:
    """Create admin tables and migrate older bot.db files safely."""
    with closing(db.connect()) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS admins (
                telegram_id INTEGER PRIMARY KEY,
                role TEXT NOT NULL DEFAULT 'support',
                added_by INTEGER,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS admin_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_telegram_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                target_type TEXT,
                target_id TEXT,
                details TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS coupons (
                code TEXT PRIMARY KEY,
                percent INTEGER NOT NULL,
                title TEXT NOT NULL,
                scope TEXT NOT NULL DEFAULT 'all',
                target_user_ids TEXT,
                usage_limit INTEGER,
                per_user_limit INTEGER NOT NULL DEFAULT 1,
                used_count INTEGER NOT NULL DEFAULT 0,
                stack_with_referral INTEGER NOT NULL DEFAULT 1,
                max_discount_percent INTEGER NOT NULL DEFAULT 40,
                active INTEGER NOT NULL DEFAULT 1,
                expires_at TEXT,
                created_by INTEGER,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS coupon_usages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                user_telegram_id INTEGER NOT NULL,
                order_id INTEGER NOT NULL,
                discount_amount INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(code, order_id)
            );
            """
        )
        for table, column, ddl in [
            ("users", "status", "ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"),
            ("users", "locked_reason", "ALTER TABLE users ADD COLUMN locked_reason TEXT"),
            ("users", "locked_notice", "ALTER TABLE users ADD COLUMN locked_notice TEXT"),
            ("users", "admin_note", "ALTER TABLE users ADD COLUMN admin_note TEXT"),
            ("users", "deleted_at", "ALTER TABLE users ADD COLUMN deleted_at TEXT"),
            ("services", "admin_note", "ALTER TABLE services ADD COLUMN admin_note TEXT"),
            ("services", "locked_reason", "ALTER TABLE services ADD COLUMN locked_reason TEXT"),
            ("services", "pasarguard_user_id", "ALTER TABLE services ADD COLUMN pasarguard_user_id INTEGER"),
            ("services", "pasarguard_username", "ALTER TABLE services ADD COLUMN pasarguard_username TEXT"),
            ("services", "pasarguard_template_id", "ALTER TABLE services ADD COLUMN pasarguard_template_id INTEGER"),
            ("services", "pasarguard_subscription_url", "ALTER TABLE services ADD COLUMN pasarguard_subscription_url TEXT"),
            ("services", "pasarguard_last_sync_at", "ALTER TABLE services ADD COLUMN pasarguard_last_sync_at TEXT"),
            ("services", "pasarguard_last_state_json", "ALTER TABLE services ADD COLUMN pasarguard_last_state_json TEXT"),
            ("services", "pasarguard_sync_status", "ALTER TABLE services ADD COLUMN pasarguard_sync_status TEXT"),
            ("services", "pasarguard_sync_error", "ALTER TABLE services ADD COLUMN pasarguard_sync_error TEXT"),
            ("orders", "coupon_code", "ALTER TABLE orders ADD COLUMN coupon_code TEXT"),
            ("orders", "coupon_discount", "ALTER TABLE orders ADD COLUMN coupon_discount INTEGER NOT NULL DEFAULT 0"),
            ("orders", "admin_note", "ALTER TABLE orders ADD COLUMN admin_note TEXT"),
        ]:
            cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            if column not in cols:
                conn.execute(ddl)
        for admin_id in BOOTSTRAP_SUPER_ADMIN_IDS:
            conn.execute(
                """
                INSERT INTO admins (telegram_id, role, added_by, is_active, created_at)
                VALUES (?, 'super', NULL, 1, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET role = 'super', is_active = 1
                """,
                (admin_id, now_iso()),
            )
        defaults = {
            "bot_locked": "0",
            "bot_lock_message": "🛠 ربات موقتاً در حال بروزرسانی است. لطفاً کمی بعد دوباره تلاش کنید.",
            "broadcast_enabled": "1",
            "free_test_enabled": "1",
            "coupon_enabled": "1",
            "purchase_enabled": "1",
        }
        for key, value in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO bot_settings (key, value, updated_at) VALUES (?, ?, ?)",
                (key, value, now_iso()),
            )
        for code, coupon in DEMO_COUPONS.items():
            conn.execute(
                """
                INSERT OR IGNORE INTO coupons
                (code, percent, title, scope, target_user_ids, usage_limit, per_user_limit, used_count,
                 stack_with_referral, max_discount_percent, active, expires_at, created_by, created_at)
                VALUES (?, ?, ?, 'all', NULL, NULL, 1, 0, 1, 40, 1, NULL, NULL, ?)
                """,
                (code, coupon.percent, coupon.title, now_iso()),
            )
        conn.commit()


def setting_get(key: str, default: str = "") -> str:
    with closing(db.connect()) as conn:
        row = conn.execute("SELECT value FROM bot_settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default


def setting_set(key: str, value: str) -> None:
    with closing(db.connect()) as conn:
        conn.execute(
            """
            INSERT INTO bot_settings (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, value, now_iso()),
        )
        conn.commit()


ADMIN_ROLE_PERMISSIONS: dict[str, set[str]] = {
    "super": {"*"},
    "sales": {"dashboard", "users", "services", "orders", "wallet", "manual_service", "broadcast"},
    "support": {"dashboard", "users", "services", "orders", "direct_message"},
    "marketing": {"dashboard", "broadcast", "coupons", "reports"},
}


def admin_role(telegram_id: int) -> Optional[str]:
    if telegram_id in BOOTSTRAP_SUPER_ADMIN_IDS:
        return "super"
    with closing(db.connect()) as conn:
        row = conn.execute("SELECT role FROM admins WHERE telegram_id = ? AND is_active = 1", (telegram_id,)).fetchone()
        return str(row["role"]) if row else None


def is_admin_id(telegram_id: Optional[int]) -> bool:
    return bool(telegram_id and admin_role(int(telegram_id)))


def admin_has(telegram_id: int, permission: str) -> bool:
    role = admin_role(telegram_id)
    if not role:
        return False
    perms = ADMIN_ROLE_PERMISSIONS.get(role, set())
    return "*" in perms or permission in perms


def require_admin_id(telegram_id: int, permission: str = "dashboard") -> bool:
    return is_admin_id(telegram_id) and admin_has(telegram_id, permission)


def admin_log(admin_id: int, action: str, target_type: str = "", target_id: Any = "", details: str = "") -> None:
    with closing(db.connect()) as conn:
        conn.execute(
            "INSERT INTO admin_logs (admin_telegram_id, action, target_type, target_id, details, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (admin_id, action, target_type, str(target_id or ""), details, now_iso()),
        )
        conn.commit()


def row_has(row: Optional[sqlite3.Row], key: str) -> bool:
    return bool(row is not None and key in row.keys())


def db_count(query: str, params: tuple[Any, ...] = ()) -> int:
    with closing(db.connect()) as conn:
        row = conn.execute(query, params).fetchone()
        if row is None:
            return 0
        return int(row[0])


def db_sum(query: str, params: tuple[Any, ...] = ()) -> int:
    with closing(db.connect()) as conn:
        row = conn.execute(query, params).fetchone()
        if row is None or row[0] is None:
            return 0
        return int(row[0])


def find_users_admin(query: str, limit: int = 8) -> list[sqlite3.Row]:
    q = normalize_digits((query or "").strip()).lstrip("@")
    with closing(db.connect()) as conn:
        if q.isdigit():
            return list(conn.execute("SELECT * FROM users WHERE telegram_id = ? LIMIT ?", (int(q), limit)).fetchall())
        like = f"%{q}%"
        return list(conn.execute("SELECT * FROM users WHERE username LIKE ? OR first_name LIKE ? ORDER BY id DESC LIMIT ?", (like, like, limit)).fetchall())


def get_user_admin(telegram_id: int) -> Optional[sqlite3.Row]:
    return db.get_user(telegram_id)


def update_user_status(telegram_id: int, status: str, reason: str = "", notice: str = "") -> None:
    with closing(db.connect()) as conn:
        deleted_at = now_iso() if status == "deleted" else None
        conn.execute(
            "UPDATE users SET status = ?, locked_reason = ?, locked_notice = ?, deleted_at = COALESCE(?, deleted_at) WHERE telegram_id = ?",
            (status, reason or None, notice or None, deleted_at, telegram_id),
        )
        conn.commit()


def set_user_note(telegram_id: int, note: str) -> None:
    with closing(db.connect()) as conn:
        conn.execute("UPDATE users SET admin_note = ? WHERE telegram_id = ?", (note, telegram_id))
        conn.commit()


def list_all_users(limit: int = 100000, only_active: bool = False, buyers_only: bool = False, no_purchase: bool = False) -> list[sqlite3.Row]:
    with closing(db.connect()) as conn:
        where = ["COALESCE(status, 'active') != 'deleted'"]
        if only_active:
            where.append("COALESCE(status, 'active') = 'active'")
        if buyers_only:
            where.append("first_purchase_done = 1")
        if no_purchase:
            where.append("first_purchase_done = 0")
        sql = "SELECT * FROM users WHERE " + " AND ".join(where) + " ORDER BY id DESC LIMIT ?"
        return list(conn.execute(sql, (limit,)).fetchall())


def get_order_any(order_id: int) -> Optional[sqlite3.Row]:
    with closing(db.connect()) as conn:
        return conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()


def list_orders_admin(limit: int = 20, status: Optional[str] = None) -> list[sqlite3.Row]:
    with closing(db.connect()) as conn:
        if status:
            return list(conn.execute("SELECT * FROM orders WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit)).fetchall())
        return list(conn.execute("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,)).fetchall())


def set_order_paid_admin(order_id: int, admin_id: int, method: str = "تأیید دستی ادمین") -> Optional[int]:
    order = get_order_any(order_id)
    if not order or order["status"] == "paid":
        return None
    telegram_id = int(order["user_telegram_id"])
    plan_key = str(order["plan_key"])
    payable = max(int(order["amount"]) - int(order["discount_amount"]), 0)
    service_id: Optional[int] = None
    if plan_key in PLANS:
        service_name = make_service_name(telegram_id)
        service_id = db.create_service(telegram_id, service_name, PLANS[plan_key], payable, is_test=False)
    elif plan_key.startswith("addon:"):
        _, package_key, service_id_s = plan_key.split(":")
        pkg = DATA_ADDON_PACKAGES.get(package_key)
        service_id = int(service_id_s)
        if pkg:
            db.add_data_to_service(service_id, telegram_id, pkg.data_gb)
    elif plan_key.startswith("renew:"):
        _, pkey, service_id_s = plan_key.split(":")
        service_id = int(service_id_s)
        if pkey in PLANS:
            db.renew_service(service_id, telegram_id, PLANS[pkey], payable)
    with closing(db.connect()) as conn:
        conn.execute("UPDATE orders SET status = 'paid', payment_method = ?, service_id = COALESCE(?, service_id) WHERE id = ?", (method, service_id, order_id))
        conn.commit()
    finalize_coupon_usage(order_id, telegram_id)
    admin_log(admin_id, "ORDER_MARK_PAID", "order", order_id, f"method={method}")
    return service_id


def admin_update_service_status(service_id: int, status: str, reason: str = "") -> None:
    with closing(db.connect()) as conn:
        conn.execute("UPDATE services SET status = ?, locked_reason = ? WHERE id = ?", (status, reason or None, service_id))
        conn.commit()


def admin_add_days_to_service(service_id: int, days: int) -> None:
    service = db.get_service(service_id)
    if not service:
        return
    expires = datetime.fromisoformat(service["expires_at"])
    base = max(expires, datetime.now(TEHRAN_TZ))
    new_expires = base + timedelta(days=days)
    with closing(db.connect()) as conn:
        conn.execute("UPDATE services SET expires_at = ?, status = 'active' WHERE id = ?", (new_expires.isoformat(timespec="seconds"), service_id))
        conn.commit()


def admin_add_data_to_service(service_id: int, data_gb: float) -> None:
    service = db.get_service(service_id)
    if not service:
        return
    db.add_data_to_service(service_id, int(service["user_telegram_id"]), data_gb)


def create_manual_service_for_user(telegram_id: int, plan_key: str, paid_amount: int, admin_id: int) -> Optional[int]:
    user = db.get_user(telegram_id)
    plan = PLANS.get(plan_key)
    if not user or not plan:
        return None
    order_id = db.create_order(telegram_id, plan_key, plan.price, max(plan.price - paid_amount, 0), 0, "paid", "ساخت دستی ادمین")
    service_id = db.create_service(telegram_id, make_service_name(telegram_id), plan, paid_amount, is_test=False)
    db.update_order_service(order_id, service_id)
    admin_log(admin_id, "MANUAL_SERVICE_CREATE", "user", telegram_id, f"plan={plan_key}, paid={paid_amount}, order={order_id}, service={service_id}")
    return service_id


def coupon_row(code: str) -> Optional[sqlite3.Row]:
    with closing(db.connect()) as conn:
        return conn.execute("SELECT * FROM coupons WHERE code = ?", (code.upper(),)).fetchone()


def coupon_usage_count(code: str, telegram_id: Optional[int] = None) -> int:
    with closing(db.connect()) as conn:
        if telegram_id is None:
            row = conn.execute("SELECT COUNT(*) AS c FROM coupon_usages WHERE code = ?", (code.upper(),)).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) AS c FROM coupon_usages WHERE code = ? AND user_telegram_id = ?", (code.upper(), telegram_id)).fetchone()
        return int(row["c"] if row else 0)


def validate_coupon_for_order(code: str, telegram_id: int, order: sqlite3.Row) -> tuple[Optional[sqlite3.Row], str]:
    if setting_get("coupon_enabled", "1") != "1":
        return None, "سیستم کد تخفیف فعلاً غیرفعال است."
    code = (code or "").strip().upper()
    row = coupon_row(code)
    if not row or not int(row["active"]):
        return None, "این کد تخفیف معتبر نیست یا غیرفعال شده است."
    if row["expires_at"]:
        try:
            if datetime.fromisoformat(str(row["expires_at"])) < datetime.now(TEHRAN_TZ):
                return None, "مهلت استفاده از این کد تخفیف تمام شده است."
        except ValueError:
            pass
    usage_limit = row["usage_limit"]
    if usage_limit is not None and int(row["used_count"]) >= int(usage_limit):
        return None, "ظرفیت استفاده از این کد تخفیف تکمیل شده است."
    if int(row["per_user_limit"] or 1) <= coupon_usage_count(code, telegram_id):
        return None, "شما قبلاً از این کد تخفیف استفاده کرده‌اید."
    scope = str(row["scope"] or "all")
    targets = [x.strip() for x in str(row["target_user_ids"] or "").split(",") if x.strip()]
    if scope in {"user", "users"} and str(telegram_id) not in targets:
        return None, "این کد تخفیف مخصوص حساب شما نیست."
    if scope == "first_purchase":
        user = db.get_user(telegram_id)
        if user and int(user["first_purchase_done"]):
            return None, "این کد فقط برای خرید اول قابل استفاده است."
    return row, ""


def save_order_coupon(order_id: int, telegram_id: int, code: str, coupon_discount: int, total_discount: int) -> None:
    with closing(db.connect()) as conn:
        conn.execute(
            "UPDATE orders SET coupon_code = ?, coupon_discount = ?, discount_amount = ? WHERE id = ? AND user_telegram_id = ? AND status = 'pending'",
            (code.upper(), coupon_discount, total_discount, order_id, telegram_id),
        )
        conn.commit()


def finalize_coupon_usage(order_id: int, telegram_id: int) -> None:
    order = db.get_order(order_id, telegram_id)
    if not order or not row_has(order, "coupon_code") or not order["coupon_code"]:
        return
    code = str(order["coupon_code"]).upper()
    discount = int(order["coupon_discount"] or 0)
    with closing(db.connect()) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO coupon_usages (code, user_telegram_id, order_id, discount_amount, created_at) VALUES (?, ?, ?, ?, ?)",
            (code, telegram_id, order_id, discount, now_iso()),
        )
        conn.execute("UPDATE coupons SET used_count = (SELECT COUNT(*) FROM coupon_usages WHERE code = ?) WHERE code = ?", (code, code))
        conn.commit()


def create_coupon_admin(code: str, percent: int, title: str, scope: str, target_user_ids: str, usage_limit: Optional[int], expires_days: Optional[int], admin_id: int) -> None:
    expires_at = None
    if expires_days and expires_days > 0:
        expires_at = (datetime.now(TEHRAN_TZ) + timedelta(days=expires_days)).isoformat(timespec="seconds")
    with closing(db.connect()) as conn:
        conn.execute(
            """
            INSERT INTO coupons
            (code, percent, title, scope, target_user_ids, usage_limit, per_user_limit, used_count,
             stack_with_referral, max_discount_percent, active, expires_at, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, 0, 1, 40, 1, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                percent = excluded.percent,
                title = excluded.title,
                scope = excluded.scope,
                target_user_ids = excluded.target_user_ids,
                usage_limit = excluded.usage_limit,
                active = 1,
                expires_at = excluded.expires_at
            """,
            (code.upper(), percent, title, scope, target_user_ids or None, usage_limit, expires_at, admin_id, now_iso()),
        )
        conn.commit()
    admin_log(admin_id, "COUPON_UPSERT", "coupon", code.upper(), f"percent={percent}, scope={scope}, limit={usage_limit}, expires_days={expires_days}")


def disable_coupon_admin(code: str, admin_id: int) -> bool:
    with closing(db.connect()) as conn:
        cur = conn.execute("UPDATE coupons SET active = 0 WHERE code = ?", (code.upper(),))
        conn.commit()
    if cur.rowcount:
        admin_log(admin_id, "COUPON_DISABLE", "coupon", code.upper(), "")
        return True
    return False


db = DB(DATABASE_PATH)
ensure_admin_schema()
router = Router()


# -----------------------------
# FSM
# -----------------------------
class BuyStates(StatesGroup):
    waiting_name = State()


class RenameStates(StatesGroup):
    waiting_new_name = State()


class CouponStates(StatesGroup):
    waiting_code = State()


class WalletStates(StatesGroup):
    waiting_amount = State()


class AdminStates(StatesGroup):
    waiting_user_search = State()
    waiting_wallet_amount = State()
    waiting_wallet_reason = State()
    waiting_direct_message = State()
    waiting_broadcast_message = State()
    waiting_bot_lock_message = State()
    waiting_coupon_line = State()
    waiting_disable_coupon = State()
    waiting_add_admin_line = State()
    waiting_user_note = State()
    waiting_manual_service_amount = State()


# -----------------------------
# Keyboards
# -----------------------------
def main_menu_kb(telegram_id: Optional[int] = None) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="🛒 خرید سرویس")],
        [KeyboardButton(text="📦 سرویس‌های من"), KeyboardButton(text="🎁 سرویس رایگان")],
        [KeyboardButton(text="💳 تراکنش‌ها"), KeyboardButton(text="💰 کیف پول")],
        [KeyboardButton(text="💎 معرفی به دوستان"), KeyboardButton(text="📊 اطلاعات حساب")],
        [KeyboardButton(text="🎫 پشتیبانی / تیکت‌ها")],
    ]
    if is_admin_id(telegram_id):
        rows.append([KeyboardButton(text="👑 پنل مدیریت")])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        input_field_placeholder="یک گزینه را انتخاب کنید…",
    )


def inline(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])


def back_home_kb() -> InlineKeyboardMarkup:
    return inline([[("🏠 منوی اصلی", "home")]])


def buy_type_kb() -> InlineKeyboardMarkup:
    return inline([
        [("🛍 پلن‌های آماده یک‌ماهه", "buy_cat:monthly")],
        [("💎 پلن‌های حرفه‌ای سه‌ماهه", "buy_cat:quarterly")],
        [("🎁 سرویس رایگان", "free_test_menu")],
        [("🏠 منوی اصلی", "home")],
    ])


def plans_kb(category: str) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    for p in PLANS.values():
        if p.category == category:
            rows.append([(f"{p.title} — {fmt_money(p.price)}", f"plan:{p.key}")])
    rows.append([("⬅️ بازگشت", "buy"), ("🏠 منوی اصلی", "home")])
    return inline(rows)


def free_service_type_kb() -> InlineKeyboardMarkup:
    rows = [[(v["title"], f"free_type:{k}")] for k, v in FREE_SERVICE_TYPES.items()]
    rows.append([("⬅️ بازگشت", "buy"), ("🏠 منوی اصلی", "home")])
    return inline(rows)


def free_packages_kb(service_type: str) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    for p in FREE_TEST_PLANS.values():
        if p.category == f"free:{service_type}":
            rows.append([(f"🎁 {p.title} — رایگان", f"free_plan:{p.key}")])
    rows.append([("⬅️ بازگشت", "free_test_menu"), ("🏠 منوی اصلی", "home")])
    return inline(rows)


def name_prompt_kb(back_callback: str) -> InlineKeyboardMarkup:
    return inline([
        [("✅ رد، نام خودکار بساز", "auto_name")],
        [("⬅️ بازگشت", back_callback), ("🏠 منوی اصلی", "home")],
    ])


def order_payment_kb(order_id: int, payable: int, wallet_balance: int, back_callback: str = "buy", back_text: str = "⬅️ انتخاب پلن دیگر") -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    rows.append([("🎟 کد تخفیف دارم", f"coupon_start:{order_id}")])
    if wallet_balance >= payable and payable > 0:
        rows.append([("💰 پرداخت با کیف پول", f"pay_wallet:{order_id}")])
    rows.append([("🤖 ثبت پرداخت موفق دمو", f"pay_demo:{order_id}")])
    rows.append([(back_text, back_callback), ("🏠 منوی اصلی", "home")])
    return inline(rows)


def coupon_cancel_kb(order_id: int) -> InlineKeyboardMarkup:
    return inline([[("❌ انصراف", f"pay_page:{order_id}"), ("🏠 منوی اصلی", "home")]])


def services_kb(services: list[sqlite3.Row]) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    active = [s for s in services if s["status"] != "deleted"]
    for s in active[:20]:
        tag = "🎁" if s["is_test"] else "📦"
        rows.append([(f"{tag} {s['name']}", f"service:{s['id']}")])
    rows.append([("🛒 خرید سرویس جدید", "buy")])
    rows.append([("🏠 منوی اصلی", "home")])
    return inline(rows)


def service_details_kb(service: sqlite3.Row) -> InlineKeyboardMarkup:
    sid = int(service["id"])
    if service["is_test"]:
        return inline([
            [("🔗 لینک اشتراک", f"sub_link:{sid}")],
            [("⬅️ سرویس‌های من", "my_services"), ("🏠 منوی اصلی", "home")],
        ])
    return inline([
        [("🔗 لینک اشتراک", f"sub_link:{sid}"), ("🔄 تغییر لینک", f"revoke:{sid}")],
        [("♻️ تمدید سرویس", f"renew_warn:{sid}"), ("📈 افزایش حجم", f"addon_menu:{sid}")],
        [("⚙️ تنظیمات اشتراک", f"svc_settings:{sid}")],
        [("⬅️ سرویس‌های من", "my_services"), ("🏠 منوی اصلی", "home")],
    ])


def addon_packages_kb(service_id: int) -> InlineKeyboardMarkup:
    rows = [[(f"📈 {pkg.title} — {fmt_money(pkg.price)}", f"addon_pkg:{service_id}:{pkg.key}")] for pkg in DATA_ADDON_PACKAGES.values()]
    rows.append([("⬅️ جزئیات سرویس", f"service:{service_id}"), ("🏠 منوی اصلی", "home")])
    return inline(rows)


def renew_type_kb(service_id: int) -> InlineKeyboardMarkup:
    return inline([
        [("🛍 پلن‌های آماده یک‌ماهه", f"renew_cat:{service_id}:monthly")],
        [("💎 پلن‌های حرفه‌ای سه‌ماهه", f"renew_cat:{service_id}:quarterly")],
        [("⬅️ جزئیات سرویس", f"service:{service_id}"), ("🏠 منوی اصلی", "home")],
    ])


def renew_plans_kb(service_id: int, category: str) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    for p in PLANS.values():
        if p.category == category:
            rows.append([(f"♻️ {p.title} — {fmt_money(p.price)}", f"renew_plan:{service_id}:{p.key}")])
    rows.append([("⬅️ بازگشت", f"renew_menu:{service_id}"), ("🏠 منوی اصلی", "home")])
    return inline(rows)


def service_settings_kb(service_id: int) -> InlineKeyboardMarkup:
    return inline([
        [("✏️ تغییر نام اشتراک", f"rename:{service_id}")],
        [("❄️ فریز اشتراک", f"soon:freeze")],
        [("↩️ عودت سرویس", f"refund_ask:{service_id}")],
        [("🗑 حذف سرویس", f"delete_ask:{service_id}")],
        [("⬅️ جزئیات سرویس", f"service:{service_id}"), ("🏠 منوی اصلی", "home")],
    ])


def delete_confirm_kb(service_id: int) -> InlineKeyboardMarkup:
    return inline([[("✅ بله، حذف شود", f"delete_yes:{service_id}")], [("❌ منصرف شدم", f"svc_settings:{service_id}")]])


def refund_confirm_kb(service_id: int) -> InlineKeyboardMarkup:
    return inline([[("✅ تأیید عودت", f"refund_yes:{service_id}")], [("❌ منصرف شدم", f"svc_settings:{service_id}")]])


def wallet_kb() -> InlineKeyboardMarkup:
    return inline([[("➕ افزایش موجودی", "wallet_topup")], [("🏠 منوی اصلی", "home")]])


def wallet_amount_kb() -> InlineKeyboardMarkup:
    return inline([[("❌ انصراف", "wallet"), ("🏠 منوی اصلی", "home")]])


def referral_kb(invite_link: str, invite_text: str) -> InlineKeyboardMarkup:
    share_url = "https://t.me/share/url?url=" + quote_plus(invite_link) + "&text=" + quote_plus(invite_text)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 پورسانت چطوریه؟", callback_data="ref_how")],
        [InlineKeyboardButton(text="🔗 لینک و متن دعوت آماده", callback_data="ref_invite")],
        [InlineKeyboardButton(text="📊 آمار دعوت‌های من", callback_data="ref_stats")],
        [InlineKeyboardButton(text="📤 اشتراک‌گذاری دعوت", url=share_url)],
        [InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="home")],
    ])


def referral_back_kb(invite_link: str, invite_text: str) -> InlineKeyboardMarkup:
    share_url = "https://t.me/share/url?url=" + quote_plus(invite_link) + "&text=" + quote_plus(invite_text)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 ارسال برای دوست", url=share_url)],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="ref_menu"), InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="home")],
    ])


# -----------------------------
# Admin keyboards / text
# -----------------------------
def admin_home_kb(admin_id: int) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = [
        [("👥 مدیریت کاربران", "adm_users"), ("📦 مدیریت سرویس‌ها", "adm_services")],
        [("🧾 سفارش‌ها", "adm_orders"), ("💰 کیف پول کاربران", "adm_wallet_start")],
        [("🎫 تیکت‌ها", "adm_tickets"), ("🎟 کدهای تخفیف", "adm_coupons")],
        [("📢 پیام همگانی", "adm_broadcast"), ("📦 مدیریت پلن‌ها", "adm_plans")],
        [("✏️ تغییر متن‌ها", "adm_texts"), ("🔒 قفل بات", "adm_bot_lock")],
        [("📊 گزارش‌ها", "adm_reports"), ("🗄 بک‌آپ/ریستور", "adm_backup")],
        [("🔌 Pasarguard", "adm_pasarguard")],
    ]
    if admin_has(admin_id, "*"):
        rows.append([("👮 مدیریت ادمین‌ها", "adm_admins"), ("📜 لاگ ادمین‌ها", "adm_logs")])
    rows.append([("🏠 منوی اصلی کاربر", "home")])
    return inline(rows)


def admin_back_kb(back: str = "adm_home") -> InlineKeyboardMarkup:
    return inline([[('⬅️ بازگشت', back), ('👑 منوی ادمین', 'adm_home')]])


def admin_users_kb() -> InlineKeyboardMarkup:
    return inline([
        [("🔎 جستجوی کاربر", "adm_user_search")],
        [("🆕 آخرین کاربران", "adm_recent_users"), ("🛒 کاربران خریدار", "adm_buyer_users")],
        [("👑 منوی ادمین", "adm_home")],
    ])


def admin_user_result_kb(users: list[sqlite3.Row]) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    for u in users:
        label = f"👤 {u['telegram_id']}"
        if u["username"]:
            label += f" | @{u['username']}"
        rows.append([(label, f"adm_user:{u['telegram_id']}")])
    rows.append([("⬅️ بازگشت", "adm_users"), ("👑 منوی ادمین", "adm_home")])
    return inline(rows)


def admin_user_kb(user: sqlite3.Row) -> InlineKeyboardMarkup:
    uid = int(user["telegram_id"])
    status = str(user["status"] if row_has(user, "status") else "active")
    lock_btn = ("🔓 باز کردن کاربر", f"adm_user_unlock:{uid}") if status != "active" else ("🔒 قفل با اطلاع", f"adm_user_lock_notify:{uid}")
    return inline([
        [("📦 سرویس‌های کاربر", f"adm_user_services:{uid}"), ("🧾 سفارش‌ها", f"adm_user_orders:{uid}")],
        [("💰 تغییر کیف پول", f"adm_user_wallet:{uid}"), ("✉️ پیام مستقیم", f"adm_user_msg:{uid}")],
        [lock_btn, ("🔕 قفل بی‌صدا", f"adm_user_lock_silent:{uid}")],
        [("🚫 بلاک با اطلاع", f"adm_user_ban_notify:{uid}"), ("🚫 بلاک بی‌صدا", f"adm_user_ban_silent:{uid}")],
        [("🗑 حذف کاربر", f"adm_user_delete:{uid}"), ("🎁 ریست تست رایگان", f"adm_user_reset_free:{uid}")],
        [("📝 یادداشت ادمین", f"adm_user_note:{uid}"), ("➕ ساخت سرویس دستی", f"adm_manual_service:{uid}")],
        [("⬅️ کاربران", "adm_users"), ("👑 منوی ادمین", "adm_home")],
    ])


def admin_service_list_kb(services: list[sqlite3.Row], back: str) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    for s in services[:25]:
        icon = "🟢" if s["status"] == "active" else "⛔"
        rows.append([(f"{icon} #{s['id']} | {s['name']}", f"adm_service:{s['id']}")])
    rows.append([("⬅️ بازگشت", back), ("👑 منوی ادمین", "adm_home")])
    return inline(rows)


def admin_service_kb(service: sqlite3.Row) -> InlineKeyboardMarkup:
    sid = int(service["id"])
    uid = int(service["user_telegram_id"])
    status = str(service["status"])
    lock_btn = ("🔓 فعال کردن سرویس", f"adm_svc_status:{sid}:active") if status != "active" else ("🔒 قفل سرویس", f"adm_svc_status:{sid}:suspended")
    return inline([
        [("🔗 تغییر لینک", f"adm_svc_revoke:{sid}"), lock_btn],
        [("➕ ۵ گیگ", f"adm_svc_data:{sid}:5"), ("➕ ۱۰ گیگ", f"adm_svc_data:{sid}:10"), ("➕ ۲۰ گیگ", f"adm_svc_data:{sid}:20")],
        [("⏳ ۷ روز", f"adm_svc_days:{sid}:7"), ("⏳ ۳۰ روز", f"adm_svc_days:{sid}:30"), ("🧹 ریست مصرف", f"adm_svc_reset:{sid}")],
        [("🗑 حذف سرویس", f"adm_svc_status:{sid}:deleted")],
        [("⬅️ سرویس‌های کاربر", f"adm_user_services:{uid}"), ("👑 منوی ادمین", "adm_home")],
    ])


def admin_orders_kb() -> InlineKeyboardMarkup:
    return inline([
        [("⏳ در انتظار پرداخت", "adm_orders_pending"), ("✅ پرداخت‌شده", "adm_orders_paid")],
        [("🧾 آخرین سفارش‌ها", "adm_orders_latest")],
        [("👑 منوی ادمین", "adm_home")],
    ])


def admin_order_list_kb(orders: list[sqlite3.Row], back: str = "adm_orders") -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    for o in orders:
        rows.append([(f"#{o['id']} | {o['status']} | {fmt_money(max(int(o['amount']) - int(o['discount_amount']), 0))}", f"adm_order:{o['id']}")])
    rows.append([("⬅️ بازگشت", back), ("👑 منوی ادمین", "adm_home")])
    return inline(rows)


def admin_order_kb(order: sqlite3.Row) -> InlineKeyboardMarkup:
    oid = int(order["id"])
    uid = int(order["user_telegram_id"])
    rows: list[list[tuple[str, str]]] = []
    if order["status"] != "paid":
        rows.append([("✅ تأیید دستی پرداخت", f"adm_order_pay:{oid}")])
    rows.append([("👤 پروفایل کاربر", f"adm_user:{uid}"), ("🧾 سفارش‌های کاربر", f"adm_user_orders:{uid}")])
    rows.append([("⬅️ سفارش‌ها", "adm_orders"), ("👑 منوی ادمین", "adm_home")])
    return inline(rows)


def admin_coupon_kb() -> InlineKeyboardMarkup:
    return inline([
        [("➕ ساخت/ویرایش کد", "adm_coupon_add"), ("⛔ غیرفعال کردن کد", "adm_coupon_disable")],
        [("📋 کدهای فعال", "adm_coupon_list")],
        [("👑 منوی ادمین", "adm_home")],
    ])


def admin_bot_lock_kb() -> InlineKeyboardMarkup:
    locked = setting_get("bot_locked", "0") == "1"
    toggle = ("🔓 باز کردن بات", "adm_bot_unlock") if locked else ("🔒 قفل کردن بات", "adm_bot_lock_on")
    return inline([
        [toggle],
        [("✏️ تغییر پیام قفل", "adm_bot_lock_msg")],
        [("👑 منوی ادمین", "adm_home")],
    ])


def admin_admins_kb() -> InlineKeyboardMarkup:
    return inline([
        [("➕ اضافه کردن ادمین", "adm_admin_add"), ("📋 لیست ادمین‌ها", "adm_admin_list")],
        [("👑 منوی ادمین", "adm_home")],
    ])


def admin_manual_service_plans_kb(uid: int) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    for p in PLANS.values():
        rows.append([(f"{p.title} — {fmt_money(p.price)}", f"adm_manual_service_plan:{uid}:{p.key}")])
    rows.append([("⬅️ پروفایل کاربر", f"adm_user:{uid}"), ("👑 منوی ادمین", "adm_home")])
    return inline(rows)


def admin_dashboard_text(admin_id: int) -> str:
    today = datetime.now(TEHRAN_TZ).date().isoformat()
    total_users = db_count("SELECT COUNT(*) FROM users WHERE COALESCE(status, 'active') != 'deleted'")
    active_services = db_count("SELECT COUNT(*) FROM services WHERE status = 'active'")
    pending_orders = db_count("SELECT COUNT(*) FROM orders WHERE status = 'pending'")
    orders_today = db_count("SELECT COUNT(*) FROM orders WHERE created_at >= ?", (today,))
    sales_today = db_sum("SELECT COALESCE(SUM(amount - discount_amount), 0) FROM orders WHERE status = 'paid' AND created_at >= ?", (today,))
    coupons_active = db_count("SELECT COUNT(*) FROM coupons WHERE active = 1")
    role = admin_role(admin_id) or "unknown"
    locked = "قفل 🔒" if setting_get("bot_locked", "0") == "1" else "باز 🔓"
    return (
        header("👑 پنل مدیریت", f"سطح دسترسی: {role}")
        + f"👥 کاربران کل: <b>{fmt_number(total_users)}</b>\n"
        + f"📦 سرویس‌های فعال: <b>{fmt_number(active_services)}</b>\n"
        + f"🧾 سفارش‌های در انتظار: <b>{fmt_number(pending_orders)}</b>\n"
        + f"💳 سفارش‌های امروز: <b>{fmt_number(orders_today)}</b>\n"
        + f"💰 فروش امروز: <b>{fmt_money(sales_today)}</b>\n"
        + f"🎟 کدهای فعال: <b>{fmt_number(coupons_active)}</b>\n"
        + f"🔒 وضعیت بات: <b>{locked}</b>\n\n"
        + "از دکمه‌های زیر یک بخش را انتخاب کنید."
    )


def admin_user_text(user: sqlite3.Row) -> str:
    services = [s for s in db.list_services(int(user["telegram_id"])) if s["status"] != "deleted"]
    orders = db.list_orders(int(user["telegram_id"]), 5)
    stats = db.referral_stats(int(user["telegram_id"]))
    status = user["status"] if row_has(user, "status") else "active"
    username = f"@{user['username']}" if user["username"] else "ثبت نشده"
    note = user["admin_note"] if row_has(user, "admin_note") and user["admin_note"] else "ندارد"
    return (
        header("👤 پروفایل کاربر", str(user["telegram_id"]))
        + f"👤 یوزرنیم: <b>{h(username)}</b>\n"
        + f"🪪 نام: <b>{h(user['first_name'] or 'ثبت نشده')}</b>\n"
        + f"📅 عضویت: <code>{h(user['created_at'][:10])}</code>\n"
        + f"⛔ وضعیت: <b>{h(status)}</b>\n"
        + f"💰 کیف پول: <b>{fmt_money(int(user['wallet_balance']))}</b>\n"
        + f"📦 سرویس‌های غیرحذف‌شده: <b>{fmt_number(len(services))}</b>\n"
        + f"🧾 سفارش‌های اخیر: <b>{fmt_number(len(orders))}</b>\n"
        + f"💎 دعوت موفق: <b>{fmt_number(stats['rewarded'])}</b>\n"
        + f"🎁 تست رایگان: <b>{'استفاده شده' if user['free_test_used'] else 'استفاده نشده'}</b>\n"
        + f"📝 یادداشت: <i>{h(note)}</i>"
    )


def admin_service_text(service: sqlite3.Row) -> str:
    expires = datetime.fromisoformat(service["expires_at"])
    days_left = max((expires - datetime.now(TEHRAN_TZ)).days, 0)
    used_gb = int(service["data_used_mb"]) / 1024
    left_gb = max(float(service["data_gb"]) - used_gb, 0)
    return (
        header("📦 مدیریت سرویس", service["name"])
        + f"🆔 سرویس: <code>{service['id']}</code>\n"
        + f"👤 کاربر: <code>{service['user_telegram_id']}</code>\n"
        + f"🟢 وضعیت: <b>{h(service['status'])}</b>\n"
        + f"🏷 پلن: <b>{h(service['plan_title'])}</b>\n"
        + f"📊 کل حجم: <b>{fmt_number(float(service['data_gb']))} GB</b>\n"
        + f"📉 مصرف‌شده: <b>{fmt_number(round(used_gb, 2))} GB</b>\n"
        + f"🔋 باقی‌مانده: <b>{fmt_number(round(left_gb, 2))} GB</b>\n"
        + f"⏳ باقی‌مانده زمانی: <b>{fmt_number(days_left)} روز</b>\n"
        + f"💳 پرداختی: <b>{fmt_money(int(service['paid_amount']))}</b>\n"
        + f"🔗 لینک: <code>{h(subscription_link(service))}</code>"
    )


def admin_order_text(order: sqlite3.Row) -> str:
    coupon = order["coupon_code"] if row_has(order, "coupon_code") and order["coupon_code"] else "ندارد"
    return (
        header("🧾 جزئیات سفارش", f"#{order['id']}")
        + f"👤 کاربر: <code>{order['user_telegram_id']}</code>\n"
        + f"📦 پلن/نوع: <code>{h(order['plan_key'])}</code>\n"
        + f"💰 مبلغ اصلی: <b>{fmt_money(int(order['amount']))}</b>\n"
        + f"🎟 تخفیف: <b>{fmt_money(int(order['discount_amount']))}</b>\n"
        + f"✅ قابل پرداخت: <b>{fmt_money(max(int(order['amount']) - int(order['discount_amount']), 0))}</b>\n"
        + f"📌 وضعیت: <b>{h(order['status'])}</b>\n"
        + f"💳 روش: <b>{h(order['payment_method'])}</b>\n"
        + f"🎟 کد: <b>{h(coupon)}</b>\n"
        + f"📅 تاریخ: <code>{h(order['created_at'])}</code>"
    )



# -----------------------------
# Message builders
# -----------------------------
def welcome_text(first_name: Optional[str] = None) -> str:
    name_part = f"، {h(first_name)}" if first_name else ""
    fallback = (
        f"🌍 <b>{h(BRAND_NAME)}</b>\n"
        f"<code>See beyond limits</code>\n\n"
        f"سلام{name_part} 👋\n"
        f"به ربات رسمی <b>{h(BRAND_NAME)}</b> خوش آمدید.\n\n"
        "اینجا می‌تونید سرویس VPN بخرید، سرویس رایگان بگیرید، سرویس‌هاتون رو مدیریت کنید و با معرفی دوستان اعتبار کیف پول بگیرید.\n\n"
        "🔹 سرورهای پرسرعت و پایدار\n"
        "🔹 ترافیک امن و اختصاصی\n"
        "🔹 مناسب ایرانسل، همراه اول و مخابرات\n\n"
        f"📣 کانال رسمی: <a href=\"{h(CHANNEL_LINK)}\">@{h(CHANNEL_USERNAME)}</a>"
    )
    return render_template_sync(
        "welcome.body",
        fallback,
        first_name=h(first_name or ""),
        first_name_part=name_part,
        brand_name=h(BRAND_NAME),
        channel_link=h(CHANNEL_LINK),
        channel_username=h(CHANNEL_USERNAME),
    )


def menu_text() -> str:
    return render_template_sync("menu.main", header("🏠 منوی اصلی") + "یکی از گزینه‌های پایین را انتخاب کنید.")


def buy_text() -> str:
    return render_template_sync("buy.intro", header("🛒 خرید سرویس", "نوع سرویس را انتخاب کنید") + "پلن‌های آماده برای شروع سریع مناسب‌اند.\nپلن‌های سه‌ماهه برای استفاده پایدار و اقتصادی‌تر پیشنهاد می‌شوند.")


def plan_category_text(category: str) -> str:
    if category == "monthly":
        return header("🛍 پلن‌های آماده یک‌ماهه", "فعال‌سازی سریع و ساده") + "یکی از حجم‌های زیر را انتخاب کنید:"
    return header("💎 پلن‌های حرفه‌ای سه‌ماهه", "اقتصادی‌تر برای استفاده بلندمدت") + "یکی از پلن‌های سه‌ماهه زیر را انتخاب کنید:"


def free_service_text() -> str:
    return render_template_sync("free.intro", header("🎁 سرویس رایگان", "اول نوع سرویس را انتخاب کنید") + "برای تست کیفیت، یک سرویس رایگان محدود می‌توانید فعال کنید.\nاین سرویس فقط یک‌بار برای هر حساب قابل دریافت است.")


def free_package_text(service_type: str) -> str:
    item = FREE_SERVICE_TYPES.get(service_type, FREE_SERVICE_TYPES["standard"])
    return header("🎁 انتخاب پکیج رایگان", item["title"]) + f"{h(item['subtitle'])}\n\nپکیج موردنظر را انتخاب کنید:"


def plan_summary_text(plan: Plan, user: sqlite3.Row) -> tuple[str, int, int, int, int]:
    referral_discount = 0
    if user["referred_by_telegram_id"] and not user["first_purchase_done"]:
        referral_discount = int(plan.price * REFERRED_DISCOUNT_PERCENT / 100)
    total_discount = referral_discount
    payable = max(plan.price - total_discount, 0)
    text = (
        header("🧾 خلاصه سفارش", plan.title)
        + f"🏷 نوع پلن: <b>{h(plan.title)}</b>\n"
        + f"📦 حجم: <b>{fmt_number(plan.data_gb)} گیگابایت</b>\n"
        + f"⏳ اعتبار: <b>{fmt_number(plan.days)} روز</b>\n"
        + f"💳 مبلغ اصلی: <b>{fmt_money(plan.price)}</b>\n"
    )
    if referral_discount:
        text += (
            f"🎁 تخفیف دعوت: <b>{fmt_money(referral_discount)}</b>\n"
            f"<i>این تخفیف چون با لینک دعوت وارد ربات شدید، روی خرید اول شما اعمال شده است.</i>\n"
        )
    text += f"✅ مبلغ قابل پرداخت: <b>{fmt_money(payable)}</b>\n\n"
    text += (
        "یک نام دلخواه برای اشتراک وارد کنید یا دکمه ساخت خودکار را بزنید.\n\n"
        f"قانون نام‌گذاری:\n"
        f"• ربات خودش ابتدای نام را <code>{SERVICE_NAME_PREFIX}</code> می‌گذارد\n"
        "• فقط حروف انگلیسی، عدد، خط تیره و آندرلاین مجاز است\n"
        "• طول نام دلخواه باید بین ۳ تا ۲۰ کاراکتر باشد"
    )
    return text, plan.price, referral_discount, total_discount, payable


def order_discount_details(order_id: int, order: Optional[sqlite3.Row]) -> dict[str, Any]:
    cached = order_discounts.get(order_id)
    if cached:
        return cached
    if order and row_has(order, "coupon_discount"):
        coupon_discount = int(order["coupon_discount"] or 0)
        referral_discount = max(int(order["discount_amount"] or 0) - coupon_discount, 0)
        return {"referral": referral_discount, "coupon": coupon_discount, "coupon_code": order["coupon_code"]}
    return {"referral": 0, "coupon": 0, "coupon_code": None}


def discount_lines(details: dict[str, Any]) -> str:
    text = ""
    if int(details.get("referral", 0)) > 0:
        text += f"🎁 تخفیف دعوت دوستان: <b>{fmt_money(int(details['referral']))}</b>\n"
        text += "<i>به‌خاطر ورود از لینک دعوت، این تخفیف روی خرید اول شما اعمال شده است.</i>\n"
    if int(details.get("coupon", 0)) > 0:
        text += f"🎟 کد تخفیف {h(details.get('coupon_code'))}: <b>{fmt_money(int(details['coupon']))}</b>\n"
    return text


def payment_text(plan: Plan, service_name: str, order_id: int, amount: int, discount: int, wallet_balance: int) -> str:
    details = order_discount_details(order_id, get_order_any(order_id))
    payable = max(amount - discount, 0)
    text = header("💳 انتخاب روش پرداخت", service_name)
    text += f"📦 پلن: <b>{h(plan.title)}</b>\n"
    text += f"💰 مبلغ اصلی: <b>{fmt_money(amount)}</b>\n"
    text += discount_lines(details)
    text += f"✅ قابل پرداخت: <b>{fmt_money(payable)}</b>\n"
    text += f"💼 موجودی کیف پول: <b>{fmt_money(wallet_balance)}</b>\n\n"
    text += "فعلاً درگاه واقعی وصل نشده؛ برای تست جریان ربات از گزینه پرداخت دمو استفاده کنید."
    return text


def render_order_payment_text(order: sqlite3.Row, user: sqlite3.Row) -> tuple[str, str, int]:
    plan_key = str(order["plan_key"])
    amount = int(order["amount"])
    discount = int(order["discount_amount"])
    payable = max(amount - discount, 0)
    details = order_discount_details(int(order["id"]), order)

    if plan_key.startswith("addon:"):
        _, pkg_key, service_id_s = plan_key.split(":")
        service = db.get_service(int(service_id_s), int(user["telegram_id"]))
        pkg = DATA_ADDON_PACKAGES[pkg_key]
        text = header("💳 پرداخت افزایش حجم", service["name"] if service else "سرویس")
        text += f"📈 بسته انتخابی: <b>{h(pkg.title)}</b>\n📊 حجم اضافه: <b>{fmt_number(pkg.data_gb)} گیگابایت</b>\n⏳ افزایش زمان: <b>ندارد</b>\n💰 مبلغ اصلی: <b>{fmt_money(amount)}</b>\n"
        text += discount_lines(details)
        text += f"✅ قابل پرداخت: <b>{fmt_money(payable)}</b>\n💼 موجودی کیف پول: <b>{fmt_money(int(user['wallet_balance']))}</b>\n\nفعلاً درگاه واقعی وصل نشده؛ برای تست جریان ربات از گزینه پرداخت دمو استفاده کنید."
        return text, f"addon_menu:{service_id_s}", payable

    if plan_key.startswith("renew:"):
        _, pkey, service_id_s = plan_key.split(":")
        service = db.get_service(int(service_id_s), int(user["telegram_id"]))
        plan = PLANS[pkey]
        text = header("💳 پرداخت تمدید سرویس", service["name"] if service else "سرویس")
        text += f"♻️ پلن تمدید: <b>{h(plan.title)}</b>\n📊 حجم جدید: <b>{fmt_number(plan.data_gb)} گیگابایت</b>\n⏳ اعتبار جدید: <b>{fmt_number(plan.days)} روز</b>\n💰 مبلغ اصلی: <b>{fmt_money(amount)}</b>\n"
        text += discount_lines(details)
        text += f"✅ قابل پرداخت: <b>{fmt_money(payable)}</b>\n💼 موجودی کیف پول: <b>{fmt_money(int(user['wallet_balance']))}</b>\n\n⚠️ با پرداخت، حجم مصرف‌شده صفر می‌شود و زمان سرویس مطابق پلن جدید ریست می‌شود."
        return text, f"renew_menu:{service_id_s}", payable

    plan = PLANS[plan_key]
    service_name = pending_names.get(int(order["id"]), make_service_name(int(user["telegram_id"])))
    return payment_text(plan, service_name, int(order["id"]), amount, discount, int(user["wallet_balance"])), f"buy_cat:{plan.category}", payable


def addon_text(service: sqlite3.Row) -> str:
    return header("📈 افزایش حجم سرویس", service["name"]) + "یکی از بسته‌های حجم اضافه را انتخاب کنید.\n\n✅ فقط حجم سرویس بیشتر می‌شود.\n⏳ زمان پایان سرویس هیچ تغییری نمی‌کند.\n💡 بسته‌های زیر نسبت به خرید سرویس جدید، به‌صرفه‌تر طراحی شده‌اند."


def renew_text(service: sqlite3.Row) -> str:
    return header("♻️ تمدید سرویس", service["name"]) + "نوع پلن تمدید را انتخاب کنید.\n\n⚠️ با تمدید، حجم و زمان سرویس شما مطابق پلن جدید ریست می‌شود.\nاگر فقط حجم بیشتری می‌خواهید، از گزینه «افزایش حجم» استفاده کنید."


def renew_category_text(service: sqlite3.Row, category: str) -> str:
    return plan_category_text(category) + "\n\n" + f"♻️ تمدید برای سرویس: <b>{h(service['name'])}</b>"


def service_text(service: sqlite3.Row) -> str:
    expires = datetime.fromisoformat(service["expires_at"])
    days_left = max((expires - datetime.now(TEHRAN_TZ)).days, 0)
    used_gb = int(service["data_used_mb"]) / 1024
    left_gb = max(float(service["data_gb"]) - used_gb, 0)
    link = subscription_link(service)
    status_label = "فعال ✅" if service["status"] == "active" else "غیرفعال ⛔"
    type_label = "\n🎁 نوع: <b>سرویس رایگان</b>" if service["is_test"] else ""
    return (
        header("📦 جزئیات سرویس", service["name"])
        + f"🟢 وضعیت: <b>{status_label}</b>{type_label}\n"
        + f"🏷 پلن: <b>{h(service['plan_title'])}</b>\n"
        + f"📊 حجم باقی‌مانده: <b>{fmt_number(round(left_gb, 2))} گیگابایت</b>\n"
        + f"⏳ زمان باقی‌مانده: <b>{fmt_number(days_left)} روز</b>\n"
        + f"💳 مبلغ پرداختی: <b>{fmt_money(int(service['paid_amount']))}</b>\n\n"
        + f"🔗 لینک اشتراک:\n<code>{h(link)}</code>\n\n"
        + f"📊 برای بررسی وضعیت سرویس، حجم مصرفی و زمان باقی‌مانده، از این بخش استفاده کنید:\n<a href=\"{h(link)}\">پنل کاربری اشتراک</a> ❗️\n\n"
        + "برای مدیریت سرویس از دکمه‌های زیر استفاده کنید."
    )


def sub_link_text(service: sqlite3.Row) -> str:
    link = subscription_link(service)
    return (
        header("🔗 لینک اشتراک", service["name"])
        + f"<code>{h(link)}</code>\n\n"
        + "برای کپی کردن، روی لینک بالا لمس کنید.\n\n"
        + f"📊 وضعیت سرویس، حجم مصرفی و زمان باقی‌مانده را می‌توانید از اینجا ببینید:\n<a href=\"{h(link)}\">پنل کاربری اشتراک</a> ❗️"
    )


def refund_text(service: sqlite3.Row) -> str:
    return (
        header("↩️ عودت سرویس", service["name"])
        + "این گزینه فقط برای اشتراک‌هایی قابل انجام است که هنوز استفاده از آن‌ها شروع نشده باشد.\n\n"
        + "اگر تأیید کنید:\n"
        + f"✅ مبلغ پرداخت‌شده، یعنی <b>{fmt_money(int(service['paid_amount']))}</b>، به کیف پول شما برمی‌گردد.\n"
        + "✅ اشتراک از حساب کاربری شما حذف می‌شود.\n\n"
        + "بعد از شروع مصرف سرویس، عودت به‌صورت خودکار قابل انجام نیست."
    )


def wallet_text(user: sqlite3.Row) -> str:
    txs = db.list_wallet_transactions(int(user["telegram_id"]))
    text = header("💰 کیف پول")
    text += f"موجودی فعلی شما: <b>{fmt_money(int(user['wallet_balance']))}</b>\n"
    text += f"درآمد معرفی دوستان: <b>{fmt_money(int(user['total_referral_earned']))}</b>\n\n"
    if txs:
        text += "آخرین گردش‌ها:\n"
        for tx in txs:
            sign = "+" if int(tx["amount"]) > 0 else ""
            text += f"• {sign}{fmt_money(int(tx['amount']))} — {h(tx['description'])}\n"
    else:
        text += "هنوز گردش مالی در کیف پول ثبت نشده است."
    return text


def wallet_amount_prompt() -> str:
    return header("➕ افزایش موجودی کیف پول") + f"مبلغ دلخواه را به تومان وارد کنید.\n\nحداقل مبلغ واریز: <b>{fmt_money(WALLET_MIN_TOPUP)}</b>\n\nمثلاً: <code>100000</code>"


def referral_invite_link(user: sqlite3.Row) -> str:
    return f"https://t.me/{BOT_USERNAME}?start=ref_{user['referral_code']}"


def referral_invite_text(invite_link: str) -> str:
    return (
        f"🌍 {BRAND_NAME}\n"
        "دنیا را بدون محدودیت ببین…\n\n"
        "⚡ سرعت بالا و پایدار\n"
        "🔐 اتصال امن و اختصاصی\n"
        "📡 مناسب ایرانسل، همراه اول و مخابرات\n"
        "🎁 با لینک من وارد شو و برای خرید اولت تخفیف بگیر.\n\n"
        f"شروع از اینجا 👇\n{invite_link}\n\n"
        f"📣 کانال رسمی: @{CHANNEL_USERNAME}"
    )


def referral_menu_text(user: sqlite3.Row) -> str:
    stats = db.referral_stats(int(user["telegram_id"]))
    invite_link = referral_invite_link(user)
    return (
        header("💎 معرفی به دوستان", "هر معرفی موفق = اعتبار کیف پول")
        + "دوستت با لینک اختصاصی تو وارد ربات میشه؛ وقتی اولین خریدش رو انجام بده، پورسانت به کیف پولت اضافه میشه.\n\n"
        + f"💰 پورسانت شما: <b>{REFERRAL_COMMISSION_PERCENT}٪ از خرید اول دوست</b>\n"
        + f"🎁 تخفیف دوست شما: <b>{REFERRED_DISCOUNT_PERCENT}٪ برای خرید اول</b>\n"
        + "🔒 پورسانت فقط بعد از خرید موفق پرداخت میشه؛ بنابراین هم برای مشتری جذابه، هم برای سیستم منصفانه و قابل ادامه‌ست.\n\n"
        + f"📊 دعوت‌ها: <b>{fmt_number(stats['total'])}</b>\n"
        + f"✅ خرید موفق: <b>{fmt_number(stats['rewarded'])}</b>\n"
        + f"💼 درآمد معرفی: <b>{fmt_money(stats['earned'])}</b>\n\n"
        + f"🔗 لینک اختصاصی شما:\n<code>{h(invite_link)}</code>"
    )


def referral_how_text() -> str:
    return (
        header("💰 پورسانت معرفی چطوریه؟", "ساده، شفاف و به‌صرفه")
        + "۱) لینک اختصاصی خودت رو برای دوستت می‌فرستی.\n"
        + "۲) دوستت از طریق لینک تو وارد ربات میشه.\n"
        + f"۳) دوستت برای خرید اول، <b>{REFERRED_DISCOUNT_PERCENT}٪ تخفیف</b> می‌گیره.\n"
        + f"۴) بعد از پرداخت موفق دوستت، <b>{REFERRAL_COMMISSION_PERCENT}٪ مبلغ خرید اول</b> به کیف پول تو اضافه میشه.\n\n"
        + "✅ دوستت با تخفیف شروع می‌کنه\n✅ تو اعتبار واقعی برای خرید یا تمدید می‌گیری\n✅ پرداخت پورسانت فقط بعد از خرید موفق انجام میشه\n\n"
        + "نمونه: اگر دوستت پلن ۴۰۰٬۰۰۰ تومانی بخره، ۴۰٬۰۰۰ تومان اعتبار به کیف پولت اضافه میشه."
    )


def account_text(user: sqlite3.Row) -> str:
    services = [s for s in db.list_services(int(user["telegram_id"])) if s["status"] != "deleted"]
    stats = db.referral_stats(int(user["telegram_id"]))
    username = f"@{user['username']}" if user["username"] else "ثبت نشده"
    return (
        header("📊 اطلاعات حساب شما")
        + f"🧾 شناسه کاربری: <code>{user['telegram_id']}</code>\n"
        + f"👤 یوزرنیم: <b>{h(username)}</b>\n"
        + f"💰 موجودی کیف پول: <b>{fmt_money(int(user['wallet_balance']))}</b>\n"
        + f"📦 سرویس‌های فعال: <b>{fmt_number(len(services))}</b>\n"
        + f"💎 دعوت‌های موفق: <b>{fmt_number(stats['rewarded'])}</b>\n"
        + f"🎁 درآمد معرفی: <b>{fmt_money(stats['earned'])}</b>\n"
        + f"📅 عضویت: <code>{h(user['created_at'][:10])}</code>"
    )


# -----------------------------
# User helpers
# -----------------------------
def parse_referrer_from_text(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return None
    payload = parts[1].strip()
    if payload.startswith("ref_"):
        code = payload.replace("ref_", "", 1)
        referrer = db.get_user_by_referral_code(code)
        if referrer:
            return int(referrer["telegram_id"])
    return None


def ensure_from_message(message: Message) -> sqlite3.Row:
    user = message.from_user
    if not user:
        raise RuntimeError("No Telegram user in message")
    referrer = parse_referrer_from_text(message.text)
    if referrer == user.id:
        referrer = None
    return db.ensure_user(user.id, user.username, user.first_name, referrer)


def ensure_from_callback(callback: CallbackQuery) -> sqlite3.Row:
    user = callback.from_user
    return db.ensure_user(user.id, user.username, user.first_name)


async def edit_or_answer(callback: CallbackQuery, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None) -> None:
    try:
        if callback.message:
            await callback.message.edit_text(text, reply_markup=reply_markup, disable_web_page_preview=True)
        else:
            await callback.answer(text)
    except Exception:
        if callback.message:
            await callback.message.answer(text, reply_markup=reply_markup, disable_web_page_preview=True)
    await callback.answer()


async def show_order_payment(target: Message | CallbackQuery, telegram_id: int, order_id: int) -> None:
    user = db.get_user(telegram_id)
    order = db.get_order(order_id, telegram_id)
    if not user or not order:
        if isinstance(target, CallbackQuery):
            await target.answer("سفارش پیدا نشد.", show_alert=True)
        return
    text, back_callback, payable = render_order_payment_text(order, user)
    markup = order_payment_kb(order_id, payable, int(user["wallet_balance"]), back_callback, "⬅️ بازگشت")
    if isinstance(target, CallbackQuery):
        await edit_or_answer(target, text, markup)
    else:
        await target.answer(text, reply_markup=markup)



# -----------------------------
# Global access guards
# -----------------------------
def user_block_message(user_row: sqlite3.Row) -> Optional[str]:
    status = str(user_row["status"] if row_has(user_row, "status") else "active")
    if status == "active":
        return None
    notice = user_row["locked_notice"] if row_has(user_row, "locked_notice") else None
    if notice:
        return str(notice)
    if status == "locked":
        return "⛔ حساب شما موقتاً محدود شده است. برای پیگیری با پشتیبانی تماس بگیرید."
    if status == "banned":
        return "🚫 دسترسی شما به ربات محدود شده است."
    if status == "deleted":
        return "این حساب از سیستم حذف شده است."
    return None


class AccessGuardMiddleware(BaseMiddleware):
    async def __call__(self, handler: Any, event: Any, data: dict[str, Any]) -> Any:
        from_user = getattr(event, "from_user", None)
        if from_user is None:
            return await handler(event, data)
        telegram_id = int(from_user.id)
        if is_admin_id(telegram_id):
            return await handler(event, data)
        if setting_get("bot_locked", "0") == "1":
            msg = setting_get("bot_lock_message", "🛠 ربات موقتاً در حال بروزرسانی است.")
            if isinstance(event, CallbackQuery):
                await event.answer(msg, show_alert=True)
            elif isinstance(event, Message):
                await event.answer(msg)
            return None
        user_row = db.get_user(telegram_id)
        if user_row:
            block_msg = user_block_message(user_row)
            if block_msg:
                if isinstance(event, CallbackQuery):
                    await event.answer(block_msg, show_alert=True)
                elif isinstance(event, Message):
                    await event.answer(block_msg)
                return None
        return await handler(event, data)


def is_purchase_open() -> bool:
    return setting_get("purchase_enabled", "1") == "1"


def is_free_test_open() -> bool:
    return setting_get("free_test_enabled", "1") == "1"


# -----------------------------
# Routes
# -----------------------------
@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    before = db.get_user(message.from_user.id) if message.from_user else None
    referrer_id = parse_referrer_from_text(message.text)
    user = ensure_from_message(message)
    if referrer_id and referrer_id != int(user["telegram_id"]) and before is None:
        await message.answer(
            "🎁 شما با لینک دعوت وارد شدید.\n"
            f"برای خرید اول، <b>{REFERRED_DISCOUNT_PERCENT}٪ تخفیف</b> روی سفارش شما اعمال می‌شود.\n\n"
            "این تخفیف هنگام انتخاب پلن، داخل خلاصه سفارش نمایش داده می‌شود.",
            reply_markup=main_menu_kb(message.from_user.id if message.from_user else None),
        )
    await message.answer(welcome_text(message.from_user.first_name if message.from_user else None), reply_markup=main_menu_kb(message.from_user.id if message.from_user else None), disable_web_page_preview=True)


@router.message(Command("menu"))
async def menu_cmd(message: Message, state: FSMContext) -> None:
    await state.clear()
    ensure_from_message(message)
    await message.answer(menu_text(), reply_markup=main_menu_kb(message.from_user.id if message.from_user else None))


@router.callback_query(F.data == "home")
async def home_cb(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    ensure_from_callback(callback)
    if callback.message:
        await callback.message.answer(menu_text(), reply_markup=main_menu_kb(callback.from_user.id if callback.from_user else None))
    await callback.answer()


@router.message(F.text == "🛒 خرید سرویس")
async def buy_msg(message: Message, state: FSMContext) -> None:
    await state.clear()
    ensure_from_message(message)
    if not is_purchase_open() and not is_admin_id(message.from_user.id if message.from_user else None):
        await message.answer("🛒 خرید سرویس فعلاً غیرفعال است.", reply_markup=back_home_kb())
        return
    await message.answer(buy_text(), reply_markup=buy_type_kb())


@router.callback_query(F.data == "buy")
async def buy_cb(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    ensure_from_callback(callback)
    if not is_purchase_open() and not is_admin_id(callback.from_user.id):
        await callback.answer("خرید سرویس فعلاً غیرفعال است.", show_alert=True)
        return
    await edit_or_answer(callback, buy_text(), buy_type_kb())


@router.callback_query(F.data.startswith("buy_cat:"))
async def buy_cat(callback: CallbackQuery) -> None:
    category = callback.data.split(":", 1)[1]
    ensure_from_callback(callback)
    await edit_or_answer(callback, plan_category_text(category), plans_kb(category))


@router.callback_query(F.data.startswith("plan:"))
async def select_plan(callback: CallbackQuery, state: FSMContext) -> None:
    user = ensure_from_callback(callback)
    plan_key = callback.data.split(":", 1)[1]
    plan = PLANS.get(plan_key)
    if not plan:
        await callback.answer("پلن پیدا نشد.", show_alert=True)
        return
    text, amount, referral_discount, total_discount, payable = plan_summary_text(plan, user)
    await state.set_state(BuyStates.waiting_name)
    await state.update_data(plan_key=plan.key, amount=amount, referral_discount=referral_discount, total_discount=total_discount, back_callback=f"buy_cat:{plan.category}")
    await edit_or_answer(callback, text, name_prompt_kb(f"buy_cat:{plan.category}"))


@router.callback_query(F.data == "auto_name")
async def auto_name(callback: CallbackQuery, state: FSMContext) -> None:
    user = ensure_from_callback(callback)
    data = await state.get_data()
    plan_key = data.get("plan_key")
    if not plan_key or plan_key not in PLANS:
        await callback.answer("سفارش پیدا نشد. دوباره پلن را انتخاب کنید.", show_alert=True)
        return
    await create_pending_order_and_show_payment(callback, state, make_service_name(int(user["telegram_id"])))


@router.message(BuyStates.waiting_name)
async def custom_name(message: Message, state: FSMContext) -> None:
    ensure_from_message(message)
    ok, name, error = validate_service_name_input(message.text or "")
    if not ok:
        await message.answer("❌ نام واردشده معتبر نیست.\n\n" + error + "\n\nلطفاً دوباره وارد کنید یا از دکمه ساخت خودکار استفاده کنید.")
        return
    await create_pending_order_and_show_payment(message, state, name)


async def create_pending_order_and_show_payment(target: Message | CallbackQuery, state: FSMContext, service_name: str) -> None:
    data = await state.get_data()
    plan = PLANS[data["plan_key"]]
    tg_id = target.from_user.id
    user = db.get_user(tg_id) or db.ensure_user(tg_id, target.from_user.username, target.from_user.first_name)
    amount = int(data.get("amount", plan.price))
    referral_discount = int(data.get("referral_discount", 0))
    order_id = db.create_order(tg_id, plan.key, amount, referral_discount, 0, "pending", "none")
    pending_names[order_id] = service_name
    order_discounts[order_id] = {"referral": referral_discount, "coupon": 0, "coupon_code": None}
    await state.clear()
    await show_order_payment(target, int(user["telegram_id"]), order_id)


@router.callback_query(F.data.startswith("pay_page:"))
async def pay_page(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    user = ensure_from_callback(callback)
    order_id = int(callback.data.split(":", 1)[1])
    await show_order_payment(callback, int(user["telegram_id"]), order_id)


@router.callback_query(F.data.startswith("coupon_start:"))
async def coupon_start(callback: CallbackQuery, state: FSMContext) -> None:
    user = ensure_from_callback(callback)
    order_id = int(callback.data.split(":", 1)[1])
    order = db.get_order(order_id, int(user["telegram_id"]))
    if not order or order["status"] != "pending":
        await callback.answer("سفارش پیدا نشد یا قبلاً پرداخت شده است.", show_alert=True)
        return
    await state.set_state(CouponStates.waiting_code)
    await state.update_data(order_id=order_id)
    text = header("🎟 کد تخفیف") + "کد تخفیف را وارد کنید.\n\nکدهای دمو برای تست:\n<code>HOWTOSEE10</code>\n<code>VIP20</code>\n<code>TEST5</code>"
    await edit_or_answer(callback, text, coupon_cancel_kb(order_id))


@router.message(CouponStates.waiting_code)
async def coupon_finish(message: Message, state: FSMContext) -> None:
    user = ensure_from_message(message)
    data = await state.get_data()
    order_id = int(data["order_id"])
    order = db.get_order(order_id, int(user["telegram_id"]))
    if not order or order["status"] != "pending":
        await state.clear()
        await message.answer("این سفارش پیدا نشد یا قبلاً پرداخت شده است.", reply_markup=back_home_kb())
        return
    code = (message.text or "").strip().upper()
    coupon = DEMO_COUPONS.get(code)
    if not coupon:
        await message.answer("❌ این کد تخفیف معتبر نیست.\nلطفاً دوباره وارد کنید یا انصراف را بزنید.", reply_markup=coupon_cancel_kb(order_id))
        return

    amount = int(order["amount"])
    details = order_discounts.get(order_id, {"referral": int(order["discount_amount"]), "coupon": 0, "coupon_code": None})
    referral_discount = int(details.get("referral", 0))
    base_for_coupon = max(amount - referral_discount, 0)
    coupon_discount = int(base_for_coupon * coupon.percent / 100)
    total_discount = min(referral_discount + coupon_discount, int(amount * 0.40))
    order_discounts[order_id] = {"referral": referral_discount, "coupon": coupon_discount, "coupon_code": coupon.code}
    db.update_order_discount(order_id, int(user["telegram_id"]), total_discount)
    await state.clear()
    await message.answer(header("✅ کد تخفیف اعمال شد") + f"کد <code>{h(coupon.code)}</code> با موفقیت روی سفارش اعمال شد.")
    await show_order_payment(message, int(user["telegram_id"]), order_id)


@router.callback_query(F.data.startswith("pay_demo:"))
async def pay_demo(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    await complete_order(callback, int(user["telegram_id"]), int(callback.data.split(":", 1)[1]), "پرداخت دمو", use_wallet=False)


@router.callback_query(F.data.startswith("pay_wallet:"))
async def pay_wallet(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    await complete_order(callback, int(user["telegram_id"]), int(callback.data.split(":", 1)[1]), "کیف پول", use_wallet=True)


async def complete_order(callback: CallbackQuery, telegram_id: int, order_id: int, method: str, use_wallet: bool) -> None:
    order = db.get_order(order_id, telegram_id)
    if not order or order["status"] != "pending":
        await callback.answer("این سفارش پیدا نشد یا قبلاً پرداخت شده است.", show_alert=True)
        return
    plan_key = str(order["plan_key"])
    if plan_key.startswith("addon:"):
        await complete_addon_order(callback, telegram_id, order, method, use_wallet)
        return
    if plan_key.startswith("renew:"):
        await complete_renew_order(callback, telegram_id, order, method, use_wallet)
        return

    plan = PLANS[plan_key]
    payable = max(int(order["amount"]) - int(order["discount_amount"]), 0)
    user = db.get_user(telegram_id)
    if not user:
        await callback.answer("حساب کاربری پیدا نشد.", show_alert=True)
        return
    wallet_used = 0
    if use_wallet:
        if int(user["wallet_balance"]) < payable:
            await callback.answer("موجودی کیف پول کافی نیست.", show_alert=True)
            return
        db.add_wallet(telegram_id, -payable, "wallet_payment", f"پرداخت سفارش #{order_id}")
        wallet_used = payable

    service_name = pending_names.get(order_id) or make_service_name(telegram_id)
    service_id = db.create_service(telegram_id, service_name, plan, payable, is_test=False)
    db.update_order_service(order_id, service_id)
    with closing(db.connect()) as conn:
        conn.execute("UPDATE orders SET status = 'paid', payment_method = ?, wallet_used = ? WHERE id = ?", (method, wallet_used, order_id))
        conn.commit()
    finalize_coupon_usage(order_id, telegram_id)
    reward = db.reward_referrer_if_needed(telegram_id, order_id, payable)
    service = db.get_service(service_id, telegram_id)
    extra = "\n\n💎 پورسانت معرفی با موفقیت برای معرف شما ثبت شد." if reward else ""
    await edit_or_answer(callback, header("✅ سفارش با موفقیت فعال شد", service_name) + f"سرویس شما آماده استفاده است.\n\n{service_text(service)}{extra}", service_details_kb(service))


async def complete_addon_order(callback: CallbackQuery, telegram_id: int, order: sqlite3.Row, method: str, use_wallet: bool) -> None:
    _, package_key, service_id_s = str(order["plan_key"]).split(":")
    service_id = int(service_id_s)
    pkg = DATA_ADDON_PACKAGES.get(package_key)
    service = db.get_service(service_id, telegram_id)
    if not pkg or not service or service["status"] == "deleted":
        await callback.answer("سرویس یا بسته حجم پیدا نشد.", show_alert=True)
        return
    if service["is_test"]:
        await callback.answer("افزایش حجم برای سرویس رایگان فعال نیست.", show_alert=True)
        return
    payable = max(int(order["amount"]) - int(order["discount_amount"]), 0)
    user = db.get_user(telegram_id)
    if not user:
        return
    wallet_used = 0
    if use_wallet:
        if int(user["wallet_balance"]) < payable:
            await callback.answer("موجودی کیف پول کافی نیست.", show_alert=True)
            return
        db.add_wallet(telegram_id, -payable, "wallet_payment", f"پرداخت افزایش حجم سفارش #{order['id']}")
        wallet_used = payable
    db.add_data_to_service(service_id, telegram_id, pkg.data_gb)
    if payable > 0:
        db.mark_first_purchase_done(telegram_id)
    db.update_order_service(int(order["id"]), service_id)
    with closing(db.connect()) as conn:
        conn.execute("UPDATE orders SET status = 'paid', payment_method = ?, wallet_used = ? WHERE id = ?", (method, wallet_used, int(order["id"])))
        conn.commit()
    finalize_coupon_usage(int(order["id"]), telegram_id)
    reward = db.reward_referrer_if_needed(telegram_id, int(order["id"]), payable)
    service = db.get_service(service_id, telegram_id)
    extra = "\n\n💎 پورسانت معرفی با موفقیت برای معرف شما ثبت شد." if reward else ""
    await edit_or_answer(callback, header("✅ حجم سرویس افزایش یافت", service["name"]) + f"بسته <b>{h(pkg.title)}</b> به سرویس شما اضافه شد.\n⏳ زمان پایان سرویس تغییری نکرد.\n\n" + service_text(service) + extra, service_details_kb(service))


async def complete_renew_order(callback: CallbackQuery, telegram_id: int, order: sqlite3.Row, method: str, use_wallet: bool) -> None:
    _, plan_key, service_id_s = str(order["plan_key"]).split(":")
    service_id = int(service_id_s)
    plan = PLANS.get(plan_key)
    service = db.get_service(service_id, telegram_id)
    if not plan or not service or service["status"] == "deleted":
        await callback.answer("سرویس یا پلن تمدید پیدا نشد.", show_alert=True)
        return
    if service["is_test"]:
        await callback.answer("تمدید برای سرویس رایگان فعال نیست.", show_alert=True)
        return
    payable = max(int(order["amount"]) - int(order["discount_amount"]), 0)
    user = db.get_user(telegram_id)
    if not user:
        return
    wallet_used = 0
    if use_wallet:
        if int(user["wallet_balance"]) < payable:
            await callback.answer("موجودی کیف پول کافی نیست.", show_alert=True)
            return
        db.add_wallet(telegram_id, -payable, "wallet_payment", f"پرداخت تمدید سفارش #{order['id']}")
        wallet_used = payable
    db.renew_service(service_id, telegram_id, plan, payable)
    db.update_order_service(int(order["id"]), service_id)
    with closing(db.connect()) as conn:
        conn.execute("UPDATE orders SET status = 'paid', payment_method = ?, wallet_used = ? WHERE id = ?", (method, wallet_used, int(order["id"])))
        conn.commit()
    finalize_coupon_usage(int(order["id"]), telegram_id)
    reward = db.reward_referrer_if_needed(telegram_id, int(order["id"]), payable)
    service = db.get_service(service_id, telegram_id)
    extra = "\n\n💎 پورسانت معرفی با موفقیت برای معرف شما ثبت شد." if reward else ""
    await edit_or_answer(callback, header("✅ سرویس با موفقیت تمدید شد", service["name"]) + "حجم مصرف‌شده صفر شد و زمان سرویس طبق پلن جدید تنظیم شد.\n\n" + service_text(service) + extra, service_details_kb(service))


@router.message(F.text == "📦 سرویس‌های من")
async def my_services_msg(message: Message) -> None:
    user = ensure_from_message(message)
    await show_my_services(message, int(user["telegram_id"]))


@router.callback_query(F.data == "my_services")
async def my_services_cb(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    services = db.list_services(int(user["telegram_id"]))
    active = [s for s in services if s["status"] != "deleted"]
    text = header("📦 سرویس‌های من") + ("هنوز سرویس فعالی ندارید. از بخش خرید سرویس می‌توانید اولین سرویس خود را بسازید." if not active else "برای دیدن جزئیات، یکی از سرویس‌ها را انتخاب کنید:")
    await edit_or_answer(callback, text, services_kb(services))


async def show_my_services(message: Message, telegram_id: int) -> None:
    services = db.list_services(telegram_id)
    active = [s for s in services if s["status"] != "deleted"]
    text = header("📦 سرویس‌های من") + ("هنوز سرویس فعالی ندارید. از بخش خرید سرویس می‌توانید اولین سرویس خود را بسازید." if not active else "برای دیدن جزئیات، یکی از سرویس‌ها را انتخاب کنید:")
    await message.answer(text, reply_markup=services_kb(services))


@router.callback_query(F.data.startswith("service:"))
async def service_details(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    service = db.get_service(int(callback.data.split(":", 1)[1]), int(user["telegram_id"]))
    if not service or service["status"] == "deleted":
        await callback.answer("سرویس پیدا نشد.", show_alert=True)
        return
    await edit_or_answer(callback, service_text(service), service_details_kb(service))


@router.callback_query(F.data.startswith("addon_menu:"))
async def addon_menu(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    service_id = int(callback.data.split(":", 1)[1])
    service = db.get_service(service_id, int(user["telegram_id"]))
    if not service or service["status"] == "deleted":
        await callback.answer("سرویس پیدا نشد.", show_alert=True)
        return
    if service["is_test"]:
        await callback.answer("افزایش حجم برای سرویس رایگان فعال نیست.", show_alert=True)
        return
    await edit_or_answer(callback, addon_text(service), addon_packages_kb(service_id))


@router.callback_query(F.data.startswith("addon_pkg:"))
async def addon_package_selected(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    _, service_id_s, package_key = callback.data.split(":")
    service_id = int(service_id_s)
    service = db.get_service(service_id, int(user["telegram_id"]))
    pkg = DATA_ADDON_PACKAGES.get(package_key)
    if not service or service["status"] == "deleted" or not pkg:
        await callback.answer("سرویس یا بسته حجم پیدا نشد.", show_alert=True)
        return
    if service["is_test"]:
        await callback.answer("افزایش حجم برای سرویس رایگان فعال نیست.", show_alert=True)
        return
    order_id = db.create_order(int(user["telegram_id"]), f"addon:{package_key}:{service_id}", pkg.price, 0, 0, "pending", "none")
    order_discounts[order_id] = {"referral": 0, "coupon": 0, "coupon_code": None}
    await show_order_payment(callback, int(user["telegram_id"]), order_id)


@router.callback_query(F.data.startswith("renew_warn:"))
async def renew_warn(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    service_id = int(callback.data.split(":", 1)[1])
    service = db.get_service(service_id, int(user["telegram_id"]))
    if not service or service["status"] == "deleted":
        await callback.answer("سرویس پیدا نشد.", show_alert=True)
        return
    if service["is_test"]:
        await callback.answer("تمدید برای سرویس رایگان فعال نیست.", show_alert=True)
        return
    await callback.answer("⚠️ توجه!\nبا تمدید، حجم و زمان سرویس شما ریست می‌شود.\nاگر فقط حجم بیشتری می‌خواهید، از افزایش حجم استفاده کنید ✅", show_alert=True)
    if callback.message:
        await callback.message.edit_text(renew_text(service), reply_markup=renew_type_kb(service_id), disable_web_page_preview=True)


@router.callback_query(F.data.startswith("renew_menu:"))
async def renew_menu(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    service_id = int(callback.data.split(":", 1)[1])
    service = db.get_service(service_id, int(user["telegram_id"]))
    if not service or service["status"] == "deleted":
        await callback.answer("سرویس پیدا نشد.", show_alert=True)
        return
    if service["is_test"]:
        await callback.answer("تمدید برای سرویس رایگان فعال نیست.", show_alert=True)
        return
    await edit_or_answer(callback, renew_text(service), renew_type_kb(service_id))


@router.callback_query(F.data.startswith("renew_cat:"))
async def renew_category(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    _, service_id_s, category = callback.data.split(":")
    service_id = int(service_id_s)
    service = db.get_service(service_id, int(user["telegram_id"]))
    if not service or service["status"] == "deleted":
        await callback.answer("سرویس پیدا نشد.", show_alert=True)
        return
    await edit_or_answer(callback, renew_category_text(service, category), renew_plans_kb(service_id, category))


@router.callback_query(F.data.startswith("renew_plan:"))
async def renew_plan_selected(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    _, service_id_s, plan_key = callback.data.split(":")
    service_id = int(service_id_s)
    service = db.get_service(service_id, int(user["telegram_id"]))
    plan = PLANS.get(plan_key)
    if not service or service["status"] == "deleted" or not plan:
        await callback.answer("سرویس یا پلن تمدید پیدا نشد.", show_alert=True)
        return
    if service["is_test"]:
        await callback.answer("تمدید برای سرویس رایگان فعال نیست.", show_alert=True)
        return
    referral_discount = 0
    if user["referred_by_telegram_id"] and not user["first_purchase_done"]:
        referral_discount = int(plan.price * REFERRED_DISCOUNT_PERCENT / 100)
    order_id = db.create_order(int(user["telegram_id"]), f"renew:{plan_key}:{service_id}", plan.price, referral_discount, 0, "pending", "none")
    order_discounts[order_id] = {"referral": referral_discount, "coupon": 0, "coupon_code": None}
    await show_order_payment(callback, int(user["telegram_id"]), order_id)


@router.callback_query(F.data.startswith("sub_link:"))
async def sub_link(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    service = db.get_service(int(callback.data.split(":", 1)[1]), int(user["telegram_id"]))
    if not service:
        await callback.answer("سرویس پیدا نشد.", show_alert=True)
        return
    await callback.answer("لینک اشتراک آماده شد.")
    if callback.message:
        await callback.message.answer(sub_link_text(service), reply_markup=inline([[("⬅️ جزئیات سرویس", f"service:{service['id']}"), ("🏠 منوی اصلی", "home")]]), disable_web_page_preview=True)


@router.callback_query(F.data.startswith("revoke:"))
async def revoke(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    service_id = int(callback.data.split(":", 1)[1])
    service = db.get_service(service_id, int(user["telegram_id"]))
    if not service:
        await callback.answer("سرویس پیدا نشد.", show_alert=True)
        return
    token = db.revoke_service_link(service_id, int(user["telegram_id"]))
    service = db.get_service(service_id, int(user["telegram_id"]))
    link = f"{SUBSCRIPTION_BASE_URL}/{token}"
    await edit_or_answer(callback, header("🔄 لینک اشتراک تغییر کرد", service["name"]) + f"لینک قبلی دیگر قابل استفاده نیست.\n\nلینک جدید:\n<code>{h(link)}</code>\n\n<a href=\"{h(link)}\">پنل کاربری اشتراک</a> ❗️", service_details_kb(service))


@router.callback_query(F.data.startswith("svc_settings:"))
async def service_settings(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    service_id = int(callback.data.split(":", 1)[1])
    service = db.get_service(service_id, int(user["telegram_id"]))
    if not service:
        await callback.answer("سرویس پیدا نشد.", show_alert=True)
        return
    if service["is_test"]:
        await callback.answer("تنظیمات پیشرفته برای سرویس رایگان فعال نیست.", show_alert=True)
        return
    await edit_or_answer(callback, header("⚙️ تنظیمات اشتراک", service["name"]) + "گزینه موردنظر را انتخاب کنید:", service_settings_kb(service_id))


@router.callback_query(F.data.startswith("soon:"))
async def soon(callback: CallbackQuery) -> None:
    await callback.answer("این بخش فعلاً در نسخه نمایشی فعال نیست.", show_alert=True)


@router.callback_query(F.data.startswith("rename:"))
async def rename_start(callback: CallbackQuery, state: FSMContext) -> None:
    user = ensure_from_callback(callback)
    service_id = int(callback.data.split(":", 1)[1])
    service = db.get_service(service_id, int(user["telegram_id"]))
    if not service:
        await callback.answer("سرویس پیدا نشد.", show_alert=True)
        return
    if service["is_test"]:
        await callback.answer("تغییر نام برای سرویس رایگان فعال نیست.", show_alert=True)
        return
    await state.set_state(RenameStates.waiting_new_name)
    await state.update_data(service_id=service_id)
    await edit_or_answer(callback, header("✏️ تغییر نام اشتراک", service["name"]) + f"نام جدید را بدون <code>{SERVICE_NAME_PREFIX}</code> وارد کنید.\n\nفقط حروف انگلیسی، عدد، خط تیره و آندرلاین مجاز است.\nطول نام: ۳ تا ۲۰ کاراکتر", inline([[("❌ لغو", f"svc_settings:{service_id}"), ("🏠 منوی اصلی", "home")]]))


@router.message(RenameStates.waiting_new_name)
async def rename_finish(message: Message, state: FSMContext) -> None:
    user = ensure_from_message(message)
    data = await state.get_data()
    service_id = int(data["service_id"])
    service = db.get_service(service_id, int(user["telegram_id"]))
    if not service or service["is_test"]:
        await state.clear()
        await message.answer("این عملیات برای این سرویس قابل انجام نیست.", reply_markup=back_home_kb())
        return
    ok, name, error = validate_service_name_input(message.text or "")
    if not ok:
        await message.answer("❌ نام واردشده معتبر نیست.\n\n" + error + "\n\nلطفاً دوباره وارد کنید یا لغو را بزنید.")
        return
    db.rename_service(service_id, int(user["telegram_id"]), name)
    await state.clear()
    service = db.get_service(service_id, int(user["telegram_id"]))
    await message.answer(header("✅ نام اشتراک تغییر کرد", name) + "تغییر با موفقیت ذخیره شد.", reply_markup=service_details_kb(service))


@router.callback_query(F.data.startswith("refund_ask:"))
async def refund_ask(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    service_id = int(callback.data.split(":", 1)[1])
    service = db.get_service(service_id, int(user["telegram_id"]))
    if not service or service["status"] == "deleted":
        await callback.answer("سرویس پیدا نشد.", show_alert=True)
        return
    if service["is_test"]:
        await callback.answer("عودت برای سرویس رایگان وجود ندارد.", show_alert=True)
        return
    await edit_or_answer(callback, refund_text(service), refund_confirm_kb(service_id))


@router.callback_query(F.data.startswith("refund_yes:"))
async def refund_yes(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    service_id = int(callback.data.split(":", 1)[1])
    service = db.get_service(service_id, int(user["telegram_id"]))
    if not service or service["status"] == "deleted":
        await callback.answer("سرویس پیدا نشد.", show_alert=True)
        return
    if service["is_test"] or int(service["paid_amount"]) <= 0:
        await callback.answer("این سرویس قابل عودت نیست.", show_alert=True)
        return
    if int(service["data_used_mb"]) > 0:
        await callback.answer("این سرویس قبلاً استفاده شده و عودت خودکار برای آن فعال نیست.", show_alert=True)
        return
    amount = int(service["paid_amount"])
    remote_result = await set_remote_user_status(db, service, "deleted")
    db.add_wallet(int(user["telegram_id"]), amount, "refund", f"عودت سرویس {service['name']}")
    db.delete_service(service_id, int(user["telegram_id"]))
    await edit_or_answer(callback, header("✅ عودت انجام شد") + f"مبلغ <b>{fmt_money(amount)}</b> به کیف پول شما برگشت و سرویس از حساب شما حذف شد." + remote_result.notice(), inline([[("💰 کیف پول", "wallet")], [("🏠 منوی اصلی", "home")]]))


@router.callback_query(F.data.startswith("delete_ask:"))
async def delete_ask(callback: CallbackQuery) -> None:
    service_id = int(callback.data.split(":", 1)[1])
    ensure_from_callback(callback)
    await edit_or_answer(callback, header("🗑 حذف سرویس") + "آیا مطمئن هستید؟ این کار فقط سرویس را از لیست شما حذف می‌کند و در نسخه نمایشی برگشت‌پذیر نیست.", delete_confirm_kb(service_id))


@router.callback_query(F.data.startswith("delete_yes:"))
async def delete_yes(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    service_id = int(callback.data.split(":", 1)[1])
    service = db.get_service(service_id, int(user["telegram_id"]))
    remote_result = await set_remote_user_status(db, service, "deleted") if service else None
    db.delete_service(service_id, int(user["telegram_id"]))
    await edit_or_answer(callback, header("✅ سرویس حذف شد") + "سرویس از لیست شما حذف شد." + (remote_result.notice() if remote_result else ""), back_home_kb())


@router.message(F.text == "🎁 سرویس رایگان")
async def free_test_msg(message: Message) -> None:
    user = ensure_from_message(message)
    if not is_free_test_open() and not is_admin_id(message.from_user.id if message.from_user else None):
        await message.answer("🎁 سرویس رایگان فعلاً غیرفعال است.", reply_markup=back_home_kb())
        return
    await show_free_service_menu(message, int(user["telegram_id"]))


@router.callback_query(F.data == "free_test_menu")
async def free_test_menu_cb(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    await show_free_service_menu(callback.message, int(user["telegram_id"]), edit_callback=callback)


async def show_free_service_menu(message: Message, telegram_id: int, edit_callback: Optional[CallbackQuery] = None) -> None:
    user = db.get_user(telegram_id)
    if user and user["free_test_used"]:
        text = header("🎁 سرویس رایگان") + "شما قبلاً سرویس رایگان خود را دریافت کرده‌اید.\n\nبرای ادامه استفاده، یکی از پلن‌های اصلی را انتخاب کنید."
        markup = inline([[("🛒 خرید سرویس", "buy")], [("🏠 منوی اصلی", "home")]])
        if edit_callback:
            await edit_or_answer(edit_callback, text, markup)
        else:
            await message.answer(text, reply_markup=markup)
        return
    if edit_callback:
        await edit_or_answer(edit_callback, free_service_text(), free_service_type_kb())
    else:
        await message.answer(free_service_text(), reply_markup=free_service_type_kb())


@router.callback_query(F.data.startswith("free_type:"))
async def free_type_selected(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    if user["free_test_used"]:
        await callback.answer("شما قبلاً سرویس رایگان خود را دریافت کرده‌اید.", show_alert=True)
        return
    service_type = callback.data.split(":", 1)[1]
    if service_type not in FREE_SERVICE_TYPES:
        await callback.answer("نوع سرویس پیدا نشد.", show_alert=True)
        return
    await edit_or_answer(callback, free_package_text(service_type), free_packages_kb(service_type))


@router.callback_query(F.data.startswith("free_plan:"))
async def free_plan_selected(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    telegram_id = int(user["telegram_id"])
    if user["free_test_used"]:
        await callback.answer("شما قبلاً سرویس رایگان خود را دریافت کرده‌اید.", show_alert=True)
        return
    plan_key = callback.data.split(":", 1)[1]
    test_plan = FREE_TEST_PLANS.get(plan_key)
    if not test_plan:
        await callback.answer("پکیج رایگان پیدا نشد.", show_alert=True)
        return
    service_name = f"{SERVICE_NAME_PREFIX}free_{str(telegram_id)[-5:]}"[:32]
    service_id = db.create_service(telegram_id, service_name, test_plan, 0, is_test=True)
    service = db.get_service(service_id, telegram_id)
    await edit_or_answer(callback, header("🎁 سرویس رایگان فعال شد", service_name) + "این سرویس برای بررسی کیفیت اتصال ساخته شده است.\n\n" + service_text(service), service_details_kb(service))


@router.message(F.text == "💳 تراکنش‌ها")
async def transactions(message: Message) -> None:
    user = ensure_from_message(message)
    orders = db.list_orders(int(user["telegram_id"]))
    text = header("💳 تراکنش‌های شما")
    if not orders:
        text += "هنوز تراکنشی ثبت نشده است."
    else:
        for i, order in enumerate(orders, 1):
            status = "پرداخت شده ✅" if order["status"] == "paid" else "در انتظار ⏳"
            payable = max(int(order["amount"]) - int(order["discount_amount"]), 0)
            text += f"{i}. <b>{status}</b>\n   🧾 شماره: <code>{order['id']}</code>\n   💰 مبلغ: <b>{fmt_money(payable)}</b>\n   📅 تاریخ: <code>{h(order['created_at'][:10])}</code>\n\n"
    await message.answer(text, reply_markup=back_home_kb())


@router.message(F.text == "💰 کیف پول")
async def wallet_msg(message: Message) -> None:
    user = ensure_from_message(message)
    await message.answer(wallet_text(user), reply_markup=wallet_kb())


@router.callback_query(F.data == "wallet")
async def wallet_cb(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    user = ensure_from_callback(callback)
    await edit_or_answer(callback, wallet_text(user), wallet_kb())


@router.callback_query(F.data == "wallet_topup")
async def wallet_topup(callback: CallbackQuery, state: FSMContext) -> None:
    ensure_from_callback(callback)
    await state.set_state(WalletStates.waiting_amount)
    await edit_or_answer(callback, wallet_amount_prompt(), wallet_amount_kb())


@router.message(WalletStates.waiting_amount)
async def wallet_topup_amount(message: Message, state: FSMContext) -> None:
    user = ensure_from_message(message)
    amount = parse_amount(message.text or "")
    if amount is None:
        await message.answer("❌ مبلغ معتبر نیست. فقط عدد وارد کنید.\nمثلاً: <code>100000</code>", reply_markup=wallet_amount_kb())
        return
    if amount < WALLET_MIN_TOPUP:
        await message.answer(f"❌ مبلغ واردشده کمتر از حداقل واریز است.\nحداقل واریز: <b>{fmt_money(WALLET_MIN_TOPUP)}</b>", reply_markup=wallet_amount_kb())
        return
    db.add_wallet(int(user["telegram_id"]), amount, "demo_topup", "افزایش موجودی دمو")
    await state.clear()
    user = db.get_user(int(user["telegram_id"]))
    await message.answer(header("✅ کیف پول شارژ شد") + f"مبلغ <b>{fmt_money(amount)}</b> به کیف پول نمایشی شما اضافه شد.\n\nموجودی جدید: <b>{fmt_money(int(user['wallet_balance']))}</b>", reply_markup=wallet_kb())


@router.message(F.text == "📊 اطلاعات حساب")
async def account(message: Message) -> None:
    user = ensure_from_message(message)
    await message.answer(account_text(user), reply_markup=back_home_kb())


@router.message(F.text == "💎 معرفی به دوستان")
async def referral_menu_msg(message: Message) -> None:
    user = ensure_from_message(message)
    invite_link = referral_invite_link(user)
    invite_text = referral_invite_text(invite_link)
    await message.answer(referral_menu_text(user), reply_markup=referral_kb(invite_link, invite_text), disable_web_page_preview=True)


@router.callback_query(F.data == "ref_menu")
async def referral_menu_cb(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    invite_link = referral_invite_link(user)
    invite_text = referral_invite_text(invite_link)
    await edit_or_answer(callback, referral_menu_text(user), referral_kb(invite_link, invite_text))


@router.callback_query(F.data == "ref_how")
async def referral_how(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    invite_link = referral_invite_link(user)
    invite_text = referral_invite_text(invite_link)
    await edit_or_answer(callback, referral_how_text(), referral_back_kb(invite_link, invite_text))


@router.callback_query(F.data == "ref_invite")
async def referral_invite(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    invite_link = referral_invite_link(user)
    invite_text = referral_invite_text(invite_link)
    text = header("🔗 لینک و متن دعوت آماده") + "متن زیر برای ارسال به دوستان آماده است:\n\n" + f"<code>{h(invite_text)}</code>"
    await edit_or_answer(callback, text, referral_back_kb(invite_link, invite_text))


@router.callback_query(F.data == "ref_stats")
async def referral_stats(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    stats = db.referral_stats(int(user["telegram_id"]))
    invite_link = referral_invite_link(user)
    invite_text = referral_invite_text(invite_link)
    text = header("📊 آمار دعوت‌های من") + f"👥 کل ثبت‌نام با لینک شما: <b>{fmt_number(stats['total'])}</b>\n✅ خرید موفق دوستان: <b>{fmt_number(stats['rewarded'])}</b>\n⏳ در انتظار خرید: <b>{fmt_number(stats['pending'])}</b>\n💰 درآمد کل معرفی: <b>{fmt_money(stats['earned'])}</b>\n\nپورسانت‌ها بعد از اولین خرید موفق هر دوست، خودکار به کیف پول اضافه می‌شوند."
    await edit_or_answer(callback, text, referral_back_kb(invite_link, invite_text))



# -----------------------------
# Admin routes (no /admin command; hidden menu button only)
# -----------------------------
async def show_admin_home(target: Message | CallbackQuery, admin_id: int) -> None:
    text = admin_dashboard_text(admin_id)
    markup = admin_home_kb(admin_id)
    if isinstance(target, CallbackQuery):
        await edit_or_answer(target, text, markup)
    else:
        await target.answer(text, reply_markup=markup)


@router.message(F.text == "👑 پنل مدیریت")
async def admin_panel_msg(message: Message, state: FSMContext) -> None:
    await state.clear()
    ensure_from_message(message)
    if not require_admin_id(message.from_user.id if message.from_user else 0, "dashboard"):
        await message.answer("دسترسی مدیریت برای شما فعال نیست.", reply_markup=main_menu_kb(message.from_user.id if message.from_user else None))
        return
    await show_admin_home(message, int(message.from_user.id))


@router.callback_query(F.data == "adm_home")
async def admin_home_cb(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if not require_admin_id(callback.from_user.id, "dashboard"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    await show_admin_home(callback, int(callback.from_user.id))


@router.callback_query(F.data == "adm_users")
async def admin_users(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if not require_admin_id(callback.from_user.id, "users"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    await edit_or_answer(callback, header("👥 مدیریت کاربران") + "کاربر را جستجو کنید یا از لیست‌های آماده استفاده کنید.", admin_users_kb())


@router.callback_query(F.data == "adm_user_search")
async def admin_user_search_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "users"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_user_search)
    await edit_or_answer(callback, header("🔎 جستجوی کاربر") + "چت‌آیدی، یوزرنیم، یا نام کاربر را وارد کنید.", admin_back_kb("adm_users"))


@router.message(AdminStates.waiting_user_search)
async def admin_user_search_finish(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "users"):
        await message.answer("دسترسی ندارید.")
        return
    users = find_users_admin(message.text or "")
    await state.clear()
    if not users:
        await message.answer(header("🔎 نتیجه جستجو") + "کاربری پیدا نشد.", reply_markup=admin_back_kb("adm_users"))
        return
    await message.answer(header("🔎 نتیجه جستجو") + f"{fmt_number(len(users))} کاربر پیدا شد:", reply_markup=admin_user_result_kb(users))


@router.callback_query(F.data.in_({"adm_recent_users", "adm_buyer_users"}))
async def admin_user_lists(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "users"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    users = list_all_users(limit=10, buyers_only=callback.data == "adm_buyer_users")
    title = "🆕 آخرین کاربران" if callback.data == "adm_recent_users" else "🛒 کاربران خریدار"
    await edit_or_answer(callback, header(title) + "برای مدیریت، یکی را انتخاب کنید.", admin_user_result_kb(users))


@router.callback_query(F.data.startswith("adm_user:"))
async def admin_user_profile(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if not require_admin_id(callback.from_user.id, "users"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    uid = int(callback.data.split(":", 1)[1])
    user = get_user_admin(uid)
    if not user:
        await callback.answer("کاربر پیدا نشد.", show_alert=True)
        return
    await edit_or_answer(callback, admin_user_text(user), admin_user_kb(user))


@router.callback_query(F.data.startswith("adm_user_lock_notify:"))
async def admin_user_lock_notify(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "users"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    uid = int(callback.data.split(":", 1)[1])
    notice = "⛔ حساب شما موقتاً محدود شده است. برای پیگیری با پشتیبانی تماس بگیرید."
    update_user_status(uid, "locked", "locked by admin", notice)
    admin_log(callback.from_user.id, "USER_LOCK_NOTIFY", "user", uid, notice)
    try:
        await callback.bot.send_message(uid, notice)
    except Exception:
        pass
    user = get_user_admin(uid)
    await edit_or_answer(callback, header("✅ کاربر قفل شد") + "کاربر با اطلاع‌رسانی قفل شد.", admin_user_kb(user))


@router.callback_query(F.data.startswith("adm_user_lock_silent:"))
async def admin_user_lock_silent(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "users"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    uid = int(callback.data.split(":", 1)[1])
    update_user_status(uid, "locked", "silent lock by admin", "")
    admin_log(callback.from_user.id, "USER_LOCK_SILENT", "user", uid, "")
    user = get_user_admin(uid)
    await edit_or_answer(callback, header("✅ کاربر قفل شد") + "کاربر بدون اطلاع‌رسانی قفل شد.", admin_user_kb(user))


@router.callback_query(F.data.startswith("adm_user_unlock:"))
async def admin_user_unlock(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "users"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    uid = int(callback.data.split(":", 1)[1])
    update_user_status(uid, "active", "", "")
    admin_log(callback.from_user.id, "USER_UNLOCK", "user", uid, "")
    user = get_user_admin(uid)
    await edit_or_answer(callback, header("✅ کاربر باز شد") + "دسترسی کاربر دوباره فعال شد.", admin_user_kb(user))


@router.callback_query(F.data.startswith("adm_user_ban_notify:"))
async def admin_user_ban_notify(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "users"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    uid = int(callback.data.split(":", 1)[1])
    notice = "🚫 دسترسی شما به ربات محدود شده است."
    update_user_status(uid, "banned", "banned by admin", notice)
    admin_log(callback.from_user.id, "USER_BAN_NOTIFY", "user", uid, "")
    try:
        await callback.bot.send_message(uid, notice)
    except Exception:
        pass
    user = get_user_admin(uid)
    await edit_or_answer(callback, header("✅ کاربر بلاک شد") + "کاربر با اطلاع‌رسانی بلاک شد.", admin_user_kb(user))


@router.callback_query(F.data.startswith("adm_user_ban_silent:"))
async def admin_user_ban_silent(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "users"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    uid = int(callback.data.split(":", 1)[1])
    update_user_status(uid, "banned", "silent ban by admin", "")
    admin_log(callback.from_user.id, "USER_BAN_SILENT", "user", uid, "")
    user = get_user_admin(uid)
    await edit_or_answer(callback, header("✅ کاربر بلاک شد") + "کاربر بدون اطلاع‌رسانی بلاک شد.", admin_user_kb(user))


@router.callback_query(F.data.startswith("adm_user_delete:"))
async def admin_user_delete(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "users"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    uid = int(callback.data.split(":", 1)[1])
    update_user_status(uid, "deleted", "deleted by admin", "")
    admin_log(callback.from_user.id, "USER_DELETE", "user", uid, "soft delete")
    await edit_or_answer(callback, header("🗑 کاربر حذف شد") + "کاربر به‌صورت نرم از سیستم حذف شد و در گزارش‌ها باقی می‌ماند.", admin_back_kb("adm_users"))


@router.callback_query(F.data.startswith("adm_user_reset_free:"))
async def admin_user_reset_free(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "users"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    uid = int(callback.data.split(":", 1)[1])
    with closing(db.connect()) as conn:
        conn.execute("UPDATE users SET free_test_used = 0 WHERE telegram_id = ?", (uid,))
        conn.commit()
    admin_log(callback.from_user.id, "USER_RESET_FREE_TEST", "user", uid, "")
    user = get_user_admin(uid)
    await edit_or_answer(callback, header("🎁 تست رایگان ریست شد") + "کاربر دوباره می‌تواند سرویس رایگان دریافت کند.", admin_user_kb(user))


@router.callback_query(F.data.startswith("adm_user_wallet:"))
async def admin_wallet_start_for_user(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "wallet"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    uid = int(callback.data.split(":", 1)[1])
    await state.set_state(AdminStates.waiting_wallet_amount)
    await state.update_data(target_user_id=uid)
    await edit_or_answer(callback, header("💰 تغییر کیف پول") + "مبلغ را با علامت وارد کنید.\nمثلاً: <code>+100000</code> یا <code>-50000</code>", admin_back_kb(f"adm_user:{uid}"))


@router.message(AdminStates.waiting_wallet_amount)
async def admin_wallet_amount_finish(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "wallet"):
        await message.answer("دسترسی ندارید.")
        return
    raw = normalize_digits(message.text or "").replace("٬", "").replace(",", "").strip()
    if not re.fullmatch(r"[+-]?\d+", raw):
        await message.answer("❌ مبلغ معتبر نیست. مثال: <code>+100000</code>")
        return
    await state.update_data(wallet_amount=int(raw))
    await state.set_state(AdminStates.waiting_wallet_reason)
    await message.answer("دلیل تغییر کیف پول را وارد کنید. این دلیل در تراکنش ثبت می‌شود.")


@router.message(AdminStates.waiting_wallet_reason)
async def admin_wallet_reason_finish(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "wallet"):
        await message.answer("دسترسی ندارید.")
        return
    data = await state.get_data()
    uid = int(data["target_user_id"])
    amount = int(data["wallet_amount"])
    reason = (message.text or "اصلاح دستی ادمین").strip()
    db.add_wallet(uid, amount, "admin_adjustment", reason, message.from_user.id if message.from_user else None)
    admin_log(message.from_user.id, "WALLET_ADJUST", "user", uid, f"amount={amount}, reason={reason}")
    await state.clear()
    user = get_user_admin(uid)
    await message.answer(header("✅ کیف پول تغییر کرد") + f"مبلغ <b>{fmt_money(amount)}</b> ثبت شد.\nموجودی جدید: <b>{fmt_money(int(user['wallet_balance']))}</b>", reply_markup=admin_user_kb(user))


@router.callback_query(F.data == "adm_wallet_start")
async def admin_wallet_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "wallet"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_user_search)
    await edit_or_answer(callback, header("💰 کیف پول کاربران") + "اول کاربر را با چت‌آیدی یا یوزرنیم جستجو کنید، سپس گزینه تغییر کیف پول را بزنید.", admin_back_kb("adm_home"))


@router.callback_query(F.data.startswith("adm_user_msg:"))
async def admin_direct_msg_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "direct_message") and not require_admin_id(callback.from_user.id, "users"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    uid = int(callback.data.split(":", 1)[1])
    await state.set_state(AdminStates.waiting_direct_message)
    await state.update_data(target_user_id=uid)
    await edit_or_answer(callback, header("✉️ پیام مستقیم") + "متن پیام را وارد کنید. پیام از طرف ربات برای کاربر ارسال می‌شود.", admin_back_kb(f"adm_user:{uid}"))


@router.message(AdminStates.waiting_direct_message)
async def admin_direct_msg_finish(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "direct_message") and not require_admin_id(message.from_user.id if message.from_user else 0, "users"):
        await message.answer("دسترسی ندارید.")
        return
    data = await state.get_data()
    uid = int(data["target_user_id"])
    text = message.html_text or message.text or ""
    try:
        await message.bot.send_message(uid, text)
        admin_log(message.from_user.id, "DIRECT_MESSAGE", "user", uid, text[:500])
        await state.clear()
        user = get_user_admin(uid)
        await message.answer(header("✅ پیام ارسال شد") + "پیام مستقیم برای کاربر ارسال شد.", reply_markup=admin_user_kb(user))
    except Exception as e:
        await message.answer(f"❌ ارسال پیام ناموفق بود: <code>{h(e)}</code>")


@router.callback_query(F.data.startswith("adm_user_note:"))
async def admin_user_note_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "users"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    uid = int(callback.data.split(":", 1)[1])
    await state.set_state(AdminStates.waiting_user_note)
    await state.update_data(target_user_id=uid)
    await edit_or_answer(callback, header("📝 یادداشت ادمین") + "یادداشت داخلی این کاربر را وارد کنید.", admin_back_kb(f"adm_user:{uid}"))


@router.message(AdminStates.waiting_user_note)
async def admin_user_note_finish(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "users"):
        await message.answer("دسترسی ندارید.")
        return
    data = await state.get_data()
    uid = int(data["target_user_id"])
    note = message.text or ""
    set_user_note(uid, note)
    admin_log(message.from_user.id, "USER_NOTE", "user", uid, note[:500])
    await state.clear()
    user = get_user_admin(uid)
    await message.answer(header("✅ یادداشت ذخیره شد"), reply_markup=admin_user_kb(user))


@router.callback_query(F.data.startswith("adm_user_services:"))
async def admin_user_services(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "services") and not require_admin_id(callback.from_user.id, "users"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    uid = int(callback.data.split(":", 1)[1])
    services = db.list_services(uid)
    await edit_or_answer(callback, header("📦 سرویس‌های کاربر", str(uid)) + ("سرویسی پیدا نشد." if not services else "یکی از سرویس‌ها را انتخاب کنید."), admin_service_list_kb(services, f"adm_user:{uid}"))


@router.callback_query(F.data == "adm_services")
async def admin_services(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "services"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    with closing(db.connect()) as conn:
        services = list(conn.execute("SELECT * FROM services ORDER BY id DESC LIMIT 20").fetchall())
    await edit_or_answer(callback, header("📦 مدیریت سرویس‌ها") + "آخرین سرویس‌ها:", admin_service_list_kb(services, "adm_home"))


@router.callback_query(F.data.startswith("adm_service:"))
async def admin_service_details(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "services") and not require_admin_id(callback.from_user.id, "users"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    sid = int(callback.data.split(":", 1)[1])
    service = db.get_service(sid)
    if not service:
        await callback.answer("سرویس پیدا نشد.", show_alert=True)
        return
    await edit_or_answer(callback, admin_service_text(service), admin_service_kb(service))


@router.callback_query(F.data.startswith("adm_svc_status:"))
async def admin_service_status(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "services"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    _, _, sid_s, status = callback.data.split(":")
    sid = int(sid_s)
    admin_update_service_status(sid, status, f"set by admin {callback.from_user.id}")
    admin_log(callback.from_user.id, "SERVICE_STATUS", "service", sid, status)
    service = db.get_service(sid)
    remote_result = await set_remote_user_status(db, service, status) if service else None
    service = db.get_service(sid)
    await edit_or_answer(callback, header("✅ وضعیت سرویس تغییر کرد") + admin_service_text(service) + (remote_result.notice() if remote_result else ""), admin_service_kb(service))


@router.callback_query(F.data.startswith("adm_svc_revoke:"))
async def admin_service_revoke(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "services"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    sid = int(callback.data.split(":", 1)[1])
    service = db.get_service(sid)
    if not service:
        await callback.answer("سرویس پیدا نشد.", show_alert=True)
        return
    db.revoke_service_link(sid, int(service["user_telegram_id"]))
    service = db.get_service(sid)
    remote_result = await revoke_remote_subscription(db, service)
    admin_log(callback.from_user.id, "SERVICE_REVOKE_LINK", "service", sid, "")
    service = db.get_service(sid)
    await edit_or_answer(callback, header("✅ لینک سرویس تغییر کرد") + admin_service_text(service) + remote_result.notice(), admin_service_kb(service))


@router.callback_query(F.data.startswith("adm_svc_data:"))
async def admin_service_add_data(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "services"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    _, _, sid_s, gb_s = callback.data.split(":")
    sid = int(sid_s)
    gb = float(gb_s)
    admin_add_data_to_service(sid, gb)
    admin_log(callback.from_user.id, "SERVICE_ADD_DATA", "service", sid, f"+{gb}GB")
    service = db.get_service(sid)
    remote_result = await update_remote_user_limit(db, service) if service else None
    service = db.get_service(sid)
    await edit_or_answer(callback, header("✅ حجم اضافه شد") + admin_service_text(service) + (remote_result.notice() if remote_result else ""), admin_service_kb(service))


@router.callback_query(F.data.startswith("adm_svc_days:"))
async def admin_service_add_days(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "services"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    _, _, sid_s, days_s = callback.data.split(":")
    sid = int(sid_s)
    days = int(days_s)
    admin_add_days_to_service(sid, days)
    admin_log(callback.from_user.id, "SERVICE_ADD_DAYS", "service", sid, f"+{days} days")
    service = db.get_service(sid)
    remote_result = await sync_remote_user_from_local(db, service) if service else None
    service = db.get_service(sid)
    await edit_or_answer(callback, header("✅ زمان اضافه شد") + admin_service_text(service) + (remote_result.notice() if remote_result else ""), admin_service_kb(service))


@router.callback_query(F.data.startswith("adm_svc_reset:"))
async def admin_service_reset_usage(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "services"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    sid = int(callback.data.split(":", 1)[1])
    with closing(db.connect()) as conn:
        conn.execute("UPDATE services SET data_used_mb = 0 WHERE id = ?", (sid,))
        conn.commit()
    admin_log(callback.from_user.id, "SERVICE_RESET_USAGE", "service", sid, "")
    service = db.get_service(sid)
    remote_result = await reset_remote_user_usage(db, service) if service else None
    service = db.get_service(sid)
    await edit_or_answer(callback, header("✅ مصرف ریست شد") + admin_service_text(service) + (remote_result.notice() if remote_result else ""), admin_service_kb(service))


@router.callback_query(F.data.startswith("adm_manual_service:"))
async def admin_manual_service_start(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "manual_service"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    uid = int(callback.data.split(":", 1)[1])
    await edit_or_answer(callback, header("➕ ساخت سرویس دستی") + "پلن موردنظر را انتخاب کنید. بعد از انتخاب پلن، مبلغ پرداخت‌شده را می‌پرسیم.", admin_manual_service_plans_kb(uid))


@router.callback_query(F.data.startswith("adm_manual_service_plan:"))
async def admin_manual_service_plan(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "manual_service"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    _, _, uid_s, plan_key = callback.data.split(":")
    uid = int(uid_s)
    if plan_key not in PLANS:
        await callback.answer("پلن پیدا نشد.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_manual_service_amount)
    await state.update_data(target_user_id=uid, plan_key=plan_key)
    plan = PLANS[plan_key]
    await edit_or_answer(callback, header("💳 مبلغ پرداخت‌شده") + f"پلن: <b>{h(plan.title)}</b>\nقیمت اصلی: <b>{fmt_money(plan.price)}</b>\n\nمبلغ پرداخت‌شده را وارد کنید. برای هدیه عدد <code>0</code> بزنید.", admin_back_kb(f"adm_user:{uid}"))


@router.message(AdminStates.waiting_manual_service_amount)
async def admin_manual_service_amount_finish(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "manual_service"):
        await message.answer("دسترسی ندارید.")
        return
    amount = parse_amount(message.text or "")
    if amount is None:
        await message.answer("❌ مبلغ معتبر نیست. فقط عدد وارد کنید.")
        return
    data = await state.get_data()
    uid = int(data["target_user_id"])
    plan_key = str(data["plan_key"])
    service_id = create_manual_service_for_user(uid, plan_key, amount, message.from_user.id if message.from_user else 0)
    await state.clear()
    if not service_id:
        await message.answer("❌ ساخت سرویس ناموفق بود.", reply_markup=admin_back_kb(f"adm_user:{uid}"))
        return
    service = db.get_service(service_id)
    remote_result = await create_remote_user_for_service(db, service, order_id=None)
    service = db.get_service(service_id)
    try:
        await message.bot.send_message(uid, header("✅ سرویس شما فعال شد") + service_text(service) + remote_result.notice(), reply_markup=service_details_kb(service))
    except Exception:
        pass
    await message.answer(header("✅ سرویس دستی ساخته شد") + admin_service_text(service) + remote_result.notice(), reply_markup=admin_service_kb(service))


@router.callback_query(F.data.startswith("adm_user_orders:"))
async def admin_user_orders(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "orders") and not require_admin_id(callback.from_user.id, "users"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    uid = int(callback.data.split(":", 1)[1])
    orders = db.list_orders(uid, 20)
    await edit_or_answer(callback, header("🧾 سفارش‌های کاربر", str(uid)) + ("سفارشی پیدا نشد." if not orders else "یکی از سفارش‌ها را انتخاب کنید."), admin_order_list_kb(orders, f"adm_user:{uid}"))


@router.callback_query(F.data == "adm_orders")
async def admin_orders(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "orders"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    await edit_or_answer(callback, header("🧾 مدیریت سفارش‌ها") + "یک دسته را انتخاب کنید.", admin_orders_kb())


@router.callback_query(F.data.in_({"adm_orders_pending", "adm_orders_paid", "adm_orders_latest"}))
async def admin_orders_list(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "orders"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    status = None
    title = "آخرین سفارش‌ها"
    if callback.data == "adm_orders_pending":
        status = "pending"
        title = "سفارش‌های در انتظار"
    elif callback.data == "adm_orders_paid":
        status = "paid"
        title = "سفارش‌های پرداخت‌شده"
    orders = list_orders_admin(20, status)
    await edit_or_answer(callback, header("🧾 " + title) + ("سفارشی پیدا نشد." if not orders else "برای جزئیات انتخاب کنید."), admin_order_list_kb(orders, "adm_orders"))


@router.callback_query(F.data.startswith("adm_order:"))
async def admin_order_details(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "orders"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    oid = int(callback.data.split(":", 1)[1])
    order = get_order_any(oid)
    if not order:
        await callback.answer("سفارش پیدا نشد.", show_alert=True)
        return
    await edit_or_answer(callback, admin_order_text(order), admin_order_kb(order))


@router.callback_query(F.data.startswith("adm_order_pay:"))
async def admin_order_pay(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "orders"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    oid = int(callback.data.split(":", 1)[1])
    service_id = set_order_paid_admin(oid, callback.from_user.id)
    order = get_order_any(oid)
    if service_id:
        service = db.get_service(service_id)
        try:
            await callback.bot.send_message(int(order["user_telegram_id"]), header("✅ پرداخت شما تأیید شد") + service_text(service), reply_markup=service_details_kb(service))
        except Exception:
            pass
    await edit_or_answer(callback, header("✅ سفارش تأیید شد") + admin_order_text(order), admin_order_kb(order))


@router.callback_query(F.data == "adm_broadcast")
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "broadcast"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_broadcast_message)
    await edit_or_answer(callback, header("📢 پیام همگانی") + "متن پیام همگانی را وارد کنید.\n\nبعد از ارسال متن، ربات اول پیش‌نمایش و دکمه تأیید را نشان می‌دهد.", admin_back_kb("adm_home"))


@router.message(AdminStates.waiting_broadcast_message)
async def admin_broadcast_preview(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "broadcast"):
        await message.answer("دسترسی ندارید.")
        return
    text = message.html_text or message.text or ""
    await state.update_data(broadcast_text=text)
    targets = list_all_users(only_active=True)
    await message.answer(
        header("📢 پیش‌نمایش پیام همگانی")
        + text
        + f"\n\n👥 تعداد مخاطب فعال: <b>{fmt_number(len(targets))}</b>\nآیا ارسال شود؟",
        reply_markup=inline([[('✅ ارسال به کاربران فعال', 'adm_broadcast_send:active')], [('🧪 ارسال تست به خودم', 'adm_broadcast_test')], [('❌ لغو', 'adm_home')]]),
    )


@router.callback_query(F.data == "adm_broadcast_test")
async def admin_broadcast_test(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "broadcast"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    data = await state.get_data()
    text = data.get("broadcast_text")
    if not text:
        await callback.answer("پیام پیدا نشد.", show_alert=True)
        return
    await callback.bot.send_message(callback.from_user.id, str(text))
    await callback.answer("پیام تست ارسال شد.", show_alert=True)


@router.callback_query(F.data.startswith("adm_broadcast_send:"))
async def admin_broadcast_send(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "broadcast"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    data = await state.get_data()
    text = data.get("broadcast_text")
    if not text:
        await callback.answer("پیام پیدا نشد.", show_alert=True)
        return
    users = list_all_users(only_active=True)
    ok = 0
    fail = 0
    await callback.answer("ارسال شروع شد...", show_alert=True)
    for u in users:
        try:
            await callback.bot.send_message(int(u["telegram_id"]), str(text))
            ok += 1
            await asyncio.sleep(0.035)
        except Exception:
            fail += 1
    await state.clear()
    admin_log(callback.from_user.id, "BROADCAST", "users", "active", f"ok={ok}, fail={fail}, text={str(text)[:500]}")
    await edit_or_answer(callback, header("✅ پیام همگانی ارسال شد") + f"موفق: <b>{fmt_number(ok)}</b>\nناموفق: <b>{fmt_number(fail)}</b>", admin_home_kb(callback.from_user.id))


@router.callback_query(F.data == "adm_bot_lock")
async def admin_bot_lock(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "*"):
        await callback.answer("فقط سوپر ادمین اجازه دارد.", show_alert=True)
        return
    locked = "قفل 🔒" if setting_get("bot_locked", "0") == "1" else "باز 🔓"
    await edit_or_answer(callback, header("🔒 قفل بات") + f"وضعیت فعلی: <b>{locked}</b>\n\nپیام قفل فعلی:\n<code>{h(setting_get('bot_lock_message'))}</code>", admin_bot_lock_kb())


@router.callback_query(F.data == "adm_bot_lock_on")
async def admin_bot_lock_on(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "*"):
        await callback.answer("فقط سوپر ادمین اجازه دارد.", show_alert=True)
        return
    setting_set("bot_locked", "1")
    admin_log(callback.from_user.id, "BOT_LOCK", "bot", "global", "")
    await edit_or_answer(callback, header("🔒 بات قفل شد") + "از این لحظه فقط ادمین‌ها می‌توانند از ربات استفاده کنند.", admin_bot_lock_kb())


@router.callback_query(F.data == "adm_bot_unlock")
async def admin_bot_unlock(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "*"):
        await callback.answer("فقط سوپر ادمین اجازه دارد.", show_alert=True)
        return
    setting_set("bot_locked", "0")
    admin_log(callback.from_user.id, "BOT_UNLOCK", "bot", "global", "")
    await edit_or_answer(callback, header("🔓 بات باز شد") + "کاربران عادی دوباره می‌توانند از ربات استفاده کنند.", admin_bot_lock_kb())


@router.callback_query(F.data == "adm_bot_lock_msg")
async def admin_bot_lock_msg_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "*"):
        await callback.answer("فقط سوپر ادمین اجازه دارد.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_bot_lock_message)
    await edit_or_answer(callback, header("✏️ تغییر پیام قفل") + "پیام جدیدی که کاربران هنگام قفل بودن بات می‌بینند را وارد کنید.", admin_back_kb("adm_bot_lock"))


@router.message(AdminStates.waiting_bot_lock_message)
async def admin_bot_lock_msg_finish(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "*"):
        await message.answer("دسترسی ندارید.")
        return
    text = message.html_text or message.text or ""
    setting_set("bot_lock_message", text)
    admin_log(message.from_user.id, "BOT_LOCK_MESSAGE", "bot", "global", text[:500])
    await state.clear()
    await message.answer(header("✅ پیام قفل ذخیره شد") + f"<code>{h(text)}</code>", reply_markup=admin_bot_lock_kb())


@router.callback_query(F.data == "adm_coupons")
async def admin_coupons(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "coupons"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    await edit_or_answer(callback, header("🎟 مدیریت کد تخفیف") + "کد عمومی، اختصاصی یا خرید اول بسازید و مصرف آن را کنترل کنید.", admin_coupon_kb())


@router.callback_query(F.data == "adm_coupon_add")
async def admin_coupon_add(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "coupons"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_coupon_line)
    sample = "VIP30 | 30 | all | 100 | 30\nUSER20 | 20 | users:123456,987654 | 10 | 7\nFIRST15 | 15 | first_purchase | 200 | 14"
    await edit_or_answer(callback, header("➕ ساخت/ویرایش کد") + "فرمت را دقیق وارد کنید:\n<code>CODE | percent | scope | usage_limit | expires_days</code>\n\nScope می‌تواند <code>all</code>، <code>first_purchase</code> یا <code>users:ID1,ID2</code> باشد.\n\nنمونه:\n<code>" + h(sample) + "</code>", admin_back_kb("adm_coupons"))


@router.message(AdminStates.waiting_coupon_line)
async def admin_coupon_add_finish(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "coupons"):
        await message.answer("دسترسی ندارید.")
        return
    parts = [p.strip() for p in (message.text or "").split("|")]
    if len(parts) < 5:
        await message.answer("❌ فرمت اشتباه است. نمونه: <code>VIP30 | 30 | all | 100 | 30</code>")
        return
    code = parts[0].upper()
    percent = parse_amount(parts[1])
    scope_raw = parts[2]
    usage_limit = parse_amount(parts[3])
    expires_days = parse_amount(parts[4])
    if not code or percent is None or percent <= 0 or percent > 90:
        await message.answer("❌ کد یا درصد معتبر نیست.")
        return
    scope = scope_raw
    targets = ""
    if scope_raw.startswith("users:"):
        scope = "users"
        targets = scope_raw.replace("users:", "", 1).replace(" ", "")
    if scope not in {"all", "users", "first_purchase"}:
        await message.answer("❌ Scope معتبر نیست. از all، first_purchase یا users:ID1,ID2 استفاده کنید.")
        return
    create_coupon_admin(code, int(percent), f"کد تخفیف {code}", scope, targets, usage_limit, expires_days, message.from_user.id if message.from_user else 0)
    await state.clear()
    await message.answer(header("✅ کد تخفیف ذخیره شد") + f"کد <code>{h(code)}</code> با تخفیف <b>{percent}%</b> فعال شد.", reply_markup=admin_coupon_kb())


@router.callback_query(F.data == "adm_coupon_disable")
async def admin_coupon_disable_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "coupons"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_disable_coupon)
    await edit_or_answer(callback, header("⛔ غیرفعال کردن کد") + "کد تخفیف را وارد کنید.", admin_back_kb("adm_coupons"))


@router.message(AdminStates.waiting_disable_coupon)
async def admin_coupon_disable_finish(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "coupons"):
        await message.answer("دسترسی ندارید.")
        return
    code = (message.text or "").strip().upper()
    ok = disable_coupon_admin(code, message.from_user.id if message.from_user else 0)
    await state.clear()
    await message.answer((header("✅ کد غیرفعال شد") if ok else header("❌ کد پیدا نشد")) + f"<code>{h(code)}</code>", reply_markup=admin_coupon_kb())


@router.callback_query(F.data == "adm_coupon_list")
async def admin_coupon_list(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "coupons"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    with closing(db.connect()) as conn:
        coupons = list(conn.execute("SELECT * FROM coupons ORDER BY created_at DESC LIMIT 20").fetchall())
    text = header("📋 کدهای تخفیف")
    if not coupons:
        text += "کدی ثبت نشده است."
    else:
        for c in coupons:
            active = "✅" if int(c["active"]) else "⛔"
            limit = c["usage_limit"] if c["usage_limit"] is not None else "∞"
            text += f"{active} <code>{h(c['code'])}</code> — {c['percent']}٪ — مصرف: {fmt_number(int(c['used_count']))}/{h(limit)} — scope: <code>{h(c['scope'])}</code>\n"
    await edit_or_answer(callback, text, admin_coupon_kb())


@router.callback_query(F.data == "adm_reports_legacy")
async def admin_reports_legacy(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "reports") and not require_admin_id(callback.from_user.id, "dashboard"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    today = datetime.now(TEHRAN_TZ).date().isoformat()
    week = (datetime.now(TEHRAN_TZ) - timedelta(days=7)).date().isoformat()
    month = (datetime.now(TEHRAN_TZ) - timedelta(days=30)).date().isoformat()
    total_users = db_count("SELECT COUNT(*) FROM users")
    users_today = db_count("SELECT COUNT(*) FROM users WHERE created_at >= ?", (today,))
    active_services = db_count("SELECT COUNT(*) FROM services WHERE status = 'active'")
    sales_today = db_sum("SELECT COALESCE(SUM(amount - discount_amount), 0) FROM orders WHERE status = 'paid' AND created_at >= ?", (today,))
    sales_week = db_sum("SELECT COALESCE(SUM(amount - discount_amount), 0) FROM orders WHERE status = 'paid' AND created_at >= ?", (week,))
    sales_month = db_sum("SELECT COALESCE(SUM(amount - discount_amount), 0) FROM orders WHERE status = 'paid' AND created_at >= ?", (month,))
    coupon_usages = db_count("SELECT COUNT(*) FROM coupon_usages")
    rewarded_refs = db_count("SELECT COUNT(*) FROM referrals WHERE rewarded = 1")
    text = (
        header("📊 گزارش‌ها")
        + f"👥 کاربران کل: <b>{fmt_number(total_users)}</b>\n"
        + f"🆕 کاربران امروز: <b>{fmt_number(users_today)}</b>\n"
        + f"📦 سرویس‌های فعال: <b>{fmt_number(active_services)}</b>\n"
        + f"💰 فروش امروز: <b>{fmt_money(sales_today)}</b>\n"
        + f"💰 فروش ۷ روز: <b>{fmt_money(sales_week)}</b>\n"
        + f"💰 فروش ۳۰ روز: <b>{fmt_money(sales_month)}</b>\n"
        + f"🎟 مصرف کد تخفیف: <b>{fmt_number(coupon_usages)}</b>\n"
        + f"💎 دعوت موفق: <b>{fmt_number(rewarded_refs)}</b>"
    )
    await edit_or_answer(callback, text, admin_back_kb("adm_home"))


@router.callback_query(F.data == "adm_admins")
async def admin_admins(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "*"):
        await callback.answer("فقط سوپر ادمین اجازه دارد.", show_alert=True)
        return
    await edit_or_answer(callback, header("👮 مدیریت ادمین‌ها") + "ادمین جدید اضافه کنید یا لیست ادمین‌ها را ببینید.", admin_admins_kb())


@router.callback_query(F.data == "adm_admin_add")
async def admin_admin_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "*"):
        await callback.answer("فقط سوپر ادمین اجازه دارد.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_add_admin_line)
    await edit_or_answer(callback, header("➕ اضافه کردن ادمین") + "فرمت:\n<code>chat_id | role</code>\n\nRole: <code>super</code>، <code>sales</code>، <code>support</code>، <code>marketing</code>", admin_back_kb("adm_admins"))


@router.message(AdminStates.waiting_add_admin_line)
async def admin_admin_add_finish(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "*"):
        await message.answer("دسترسی ندارید.")
        return
    parts = [p.strip() for p in (message.text or "").split("|")]
    if len(parts) < 2:
        await message.answer("❌ فرمت اشتباه است. نمونه: <code>123456789 | support</code>")
        return
    uid_s = normalize_digits(parts[0])
    role = parts[1].lower()
    if not uid_s.isdigit() or role not in ADMIN_ROLE_PERMISSIONS:
        await message.answer("❌ چت‌آیدی یا سطح دسترسی معتبر نیست.")
        return
    uid = int(uid_s)
    with closing(db.connect()) as conn:
        conn.execute(
            """
            INSERT INTO admins (telegram_id, role, added_by, is_active, created_at)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET role = excluded.role, is_active = 1
            """,
            (uid, role, message.from_user.id if message.from_user else None, now_iso()),
        )
        conn.commit()
    admin_log(message.from_user.id, "ADMIN_UPSERT", "admin", uid, f"role={role}")
    await state.clear()
    await message.answer(header("✅ ادمین ذخیره شد") + f"چت‌آیدی <code>{uid}</code> با سطح <b>{h(role)}</b> فعال شد.", reply_markup=admin_admins_kb())


@router.callback_query(F.data == "adm_admin_list")
async def admin_admin_list(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "*"):
        await callback.answer("فقط سوپر ادمین اجازه دارد.", show_alert=True)
        return
    with closing(db.connect()) as conn:
        rows = list(conn.execute("SELECT * FROM admins ORDER BY created_at DESC LIMIT 50").fetchall())
    text = header("📋 لیست ادمین‌ها")
    for a in rows:
        active = "✅" if int(a["is_active"]) else "⛔"
        text += f"{active} <code>{a['telegram_id']}</code> — <b>{h(a['role'])}</b> — {h(a['created_at'][:10])}\n"
    await edit_or_answer(callback, text, admin_admins_kb())


@router.callback_query(F.data == "adm_logs")
async def admin_logs(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "*"):
        await callback.answer("فقط سوپر ادمین اجازه دارد.", show_alert=True)
        return
    with closing(db.connect()) as conn:
        rows = list(conn.execute("SELECT * FROM admin_logs ORDER BY id DESC LIMIT 20").fetchall())
    text = header("📜 آخرین لاگ‌های ادمین")
    if not rows:
        text += "لاگی ثبت نشده است."
    else:
        for r in rows:
            text += f"#{r['id']} | <code>{r['admin_telegram_id']}</code> | <b>{h(r['action'])}</b> | {h(r['target_type'])}:{h(r['target_id'])} | <code>{h(r['created_at'][:16])}</code>\n"
    await edit_or_answer(callback, text, admin_back_kb("adm_home"))


@router.message()
async def unknown(message: Message, state: FSMContext) -> None:
    ensure_from_message(message)
    await message.answer("گزینه موردنظر را از منوی پایین انتخاب کنید 👇", reply_markup=main_menu_kb(message.from_user.id if message.from_user else None))


async def main() -> None:
    await bootstrap_phase1()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.middleware(AccessGuardMiddleware())
    dp.callback_query.middleware(AccessGuardMiddleware())
    # Staged routers must be included before the legacy catch-all router.
    dp.include_router(ticket_router)
    dp.include_router(plans_router)
    dp.include_router(settings_router)
    dp.include_router(broadcast_router)
    dp.include_router(reports_router)
    dp.include_router(backup_router)
    dp.include_router(pasarguard_router)
    dp.include_router(router)
    auto_backup_task = start_auto_backup_scheduler(bot)
    logger.info("Bot started: @%s", BOT_USERNAME)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        auto_backup_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
