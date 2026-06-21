import asyncio
import html
import json
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
from urllib.parse import quote_plus, urljoin

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
from app.config import settings
from app.utils.line_parser import split_escaped_pipe, pipe_escape_hint
from app.services.text_template_service import render_template_sync
from app.services.ticket_service import deactivate_admin_role, upsert_admin_role
from app.services.plan_service import upsert_plan_from_line
from app.routers.tickets import ticket_router
from app.routers.broadcast import broadcast_router
from app.routers.reports import reports_router
from app.routers.backup import backup_router
from app.routers.pasarguard import pasarguard_router
from app.services.scheduled_backup_service import start_auto_backup_scheduler
from app.services.deadline_service import expire_sqlite_payment_deadlines, run_deadline_cleanup_once
from app.services.job_service import get_job, list_jobs, run_due_jobs_once, run_job_and_record, set_job_enabled, start_job_scheduler, update_job_interval
from app.services.pasarguard_template_service import render_sync_report, sync_plan_templates
from app.services.pasarguard_user_service import (
    apply_template_to_remote_user,
    create_remote_user_for_service,
    ensure_template_for_plan,
    reset_remote_user_usage,
    revoke_remote_subscription,
    set_remote_user_status,
    sync_remote_user_from_local,
    sync_remote_user_from_panel,
    sync_all_remote_users_from_panel,
    render_remote_bulk_sync_report,
    update_remote_user_limit,
)
from app.routers.plans import plans_router
from app.routers.settings import settings_router

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("howtosee-bot")


# -----------------------------
# Config
# -----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "HowToSeeWorld_bot").strip().lstrip("@")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "HowToSeeWorld").strip().lstrip("@")
CHANNEL_LINK = os.getenv("CHANNEL_LINK", f"https://t.me/{CHANNEL_USERNAME}").strip()
BRAND_NAME = os.getenv("BRAND_NAME", "HowToSee | Premium VPN").strip()
DATABASE_PATH = os.getenv("DATABASE_PATH", "bot.db").strip()
SUBSCRIPTION_BASE_URL = os.getenv("SUBSCRIPTION_BASE_URL", "https://example.com/sub").rstrip("/")
REFERRAL_COMMISSION_PERCENT = int(os.getenv("REFERRAL_COMMISSION_PERCENT", "10"))
REFERRED_DISCOUNT_PERCENT = int(os.getenv("REFERRED_DISCOUNT_PERCENT", "5"))
FREE_TEST_MB = int(os.getenv("FREE_TEST_MB", "150"))
WALLET_MIN_TOPUP = int(os.getenv("WALLET_MIN_TOPUP", "50000"))
SERVICE_NAME_PREFIX = os.getenv("SERVICE_NAME_PREFIX", "howtosee_").strip()
ADMIN_CHAT_IDS_RAW = os.getenv("ADMIN_CHAT_IDS", "").strip()
SALES_ADMIN_CHAT_IDS_RAW = os.getenv("SALES_ADMIN_CHAT_IDS", "").strip()


def parse_id_set(value: str) -> set[int]:
    ids: set[int] = set()
    for part in re.split(r"[,\s]+", value or ""):
        part = normalize_digits(part.strip()) if "normalize_digits" in globals() else part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return ids


BOOTSTRAP_SUPER_ADMIN_IDS = parse_id_set(ADMIN_CHAT_IDS_RAW)
SALES_ADMIN_CHAT_IDS = parse_id_set(SALES_ADMIN_CHAT_IDS_RAW)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Create .env from .env.example and set your bot token.")

TEHRAN_TZ = timezone(timedelta(hours=3, minutes=30))
RECEIPT_UPLOAD_WINDOW_MINUTES = int(os.getenv("RECEIPT_UPLOAD_WINDOW_MINUTES", "30"))


# -----------------------------
# Legacy catalog bridge - synced with DB/Pasarguard templates in Phase 4
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


# Legacy catalog bridge. These dictionaries are populated from PostgreSQL during bootstrap
# by app.services.plan_service.sync_legacy_catalog_from_db(). They must exist before
# bootstrap runs because the staged legacy buy flow still reads them directly.
PLANS: dict[str, Plan] = {}
# Dynamic paid plan categories, populated from PostgreSQL.
# key -> {title, description, sort_order, is_active}
PLAN_CATEGORIES: dict[str, dict[str, Any]] = {}
DATA_ADDON_PACKAGES: dict[str, DataAddon] = {}
FREE_TEST_PLANS: dict[str, Plan] = {}

FREE_SERVICE_TYPES: dict[str, dict[str, str]] = {
    "standard": {"title": "🌍 سرویس رایگان استاندارد", "subtitle": "برای تست اتصال روزمره"},
    "speed": {"title": "⚡ سرویس رایگان پرسرعت", "subtitle": "برای تست سرعت و پایداری"},
}


# Small transient state for user-entered service names; durable order data lives in SQLite/PostgreSQL.
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


def gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    """Convert Gregorian date to Jalali/Shamsi without external dependencies."""
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    if gy > 1600:
        jy = 979
        gy -= 1600
    else:
        jy = 0
        gy -= 621
    gy2 = gy + 1 if gm > 2 else gy
    days = (365 * gy) + ((gy2 + 3) // 4) - ((gy2 + 99) // 100) + ((gy2 + 399) // 400) - 80 + gd + g_d_m[gm - 1]
    jy += 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + days // 31
        jd = 1 + days % 31
    else:
        jm = 7 + (days - 186) // 30
        jd = 1 + (days - 186) % 30
    return jy, jm, jd


def parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        raw = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TEHRAN_TZ)
        return dt.astimezone(TEHRAN_TZ)
    except Exception:
        return None


def fmt_jalali_datetime(value: Any) -> str:
    dt = parse_dt(value)
    if not dt:
        return str(value or "-")
    jy, jm, jd = gregorian_to_jalali(dt.year, dt.month, dt.day)
    return f"{jy:04d}/{jm:02d}/{jd:02d} ساعت {dt.hour:02d}:{dt.minute:02d}"


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
    # Keep the generated tail numeric-only. If SERVICE_NAME_PREFIX is empty,
    # the generated service name is only digits. Pasarguard template prefix can
    # still be controlled separately with PASARGUARD_USERNAME_PREFIX.
    suffix = f"{str(telegram_id)[-5:]}{random.randint(10, 99)}"
    return f"{SERVICE_NAME_PREFIX}{suffix}"[:48]


def validate_service_name_input(raw: str) -> tuple[bool, str, str]:
    """Validate the user-entered base name. We do NOT silently clean/fix it."""
    value = (raw or "").strip()
    if not value:
        return False, "", "نام نمی‌تواند خالی باشد. اگر نام دلخواه نمی‌خواهید، از دکمه ساخت خودکار استفاده کنید."
    if SERVICE_NAME_PREFIX and value.lower().startswith(SERVICE_NAME_PREFIX.lower()):
        return False, "", f"لطفاً بخش <code>{SERVICE_NAME_PREFIX}</code> را وارد نکنید؛ ربات خودش آن را اول نام می‌گذارد."
    if len(value) < 3 or len(value) > 20:
        return False, "", "نام باید بین ۳ تا ۲۰ کاراکتر باشد."
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return False, "", "نام فقط می‌تواند شامل حروف انگلیسی، عدد، خط تیره <code>-</code> و آندرلاین <code>_</code> باشد. فاصله و حروف فارسی مجاز نیست."
    return True, f"{SERVICE_NAME_PREFIX}{value}", ""


def normalize_subscription_url(raw: str | None) -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    if value.startswith(("http://", "https://")):
        return value
    base = (settings.pasarguard_base_url or "").rstrip("/")
    if base:
        return urljoin(base + "/", value.lstrip("/"))
    return value


def subscription_link(service: sqlite3.Row) -> str:
    try:
        if "pasarguard_subscription_url" in service.keys() and service["pasarguard_subscription_url"]:
            return normalize_subscription_url(str(service["pasarguard_subscription_url"]))
    except Exception:
        pass
    # Phase 4.10: when Pasarguard is enabled, production services must not expose fake/local subscription URLs.
    if settings.pasarguard_enabled:
        return ""
    return f"{SUBSCRIPTION_BASE_URL}/{service['token']}"


def subscription_link_or_pending_text(service: sqlite3.Row) -> str:
    link = subscription_link(service)
    if link:
        return link
    status = str(service["status"] or "")
    if status in {"provisioning", "provisioning_failed"}:
        return "لینک اشتراک هنوز آماده نشده است."
    return "لینک اشتراک برای این سرویس هنوز ثبت نشده است."


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

    def update_order_service_name(self, order_id: int, telegram_id: int, service_name: str) -> None:
        with closing(self.connect()) as conn:
            conn.execute("UPDATE orders SET service_name = ? WHERE id = ? AND user_telegram_id = ?", (service_name, order_id, telegram_id))
            conn.commit()

    def get_order(self, order_id: int, telegram_id: int) -> Optional[sqlite3.Row]:
        with closing(self.connect()) as conn:
            return conn.execute("SELECT * FROM orders WHERE id = ? AND user_telegram_id = ?", (order_id, telegram_id)).fetchone()

    def create_service(self, telegram_id: int, name: str, plan: Plan, paid_amount: int, is_test: bool = False, status: str = "active") -> int:
        token = make_token()
        expires = datetime.now(TEHRAN_TZ) + timedelta(days=plan.days)
        with closing(self.connect()) as conn:
            cur = conn.execute(
                """
                INSERT INTO services
                (user_telegram_id, name, plan_key, plan_title, data_gb, days, price, paid_amount, token, expires_at, is_test, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (telegram_id, name, plan.key, plan.title, plan.data_gb, plan.days, plan.price, paid_amount, token, expires.isoformat(timespec="seconds"), 1 if is_test else 0, status, now_iso()),
            )
            if is_test and status == "active":
                conn.execute("UPDATE users SET free_test_used = 1 WHERE telegram_id = ?", (telegram_id,))
            elif paid_amount > 0 and status == "active":
                conn.execute("UPDATE users SET first_purchase_done = 1 WHERE telegram_id = ?", (telegram_id,))
            conn.commit()
            return int(cur.lastrowid)

    def set_service_status(self, service_id: int, telegram_id: int, status: str, error: str | None = None) -> None:
        with closing(self.connect()) as conn:
            if error is not None and "pasarguard_sync_error" in [row[1] for row in conn.execute("PRAGMA table_info(services)").fetchall()]:
                conn.execute("UPDATE services SET status = ?, pasarguard_sync_error = ? WHERE id = ? AND user_telegram_id = ?", (status, error, service_id, telegram_id))
            else:
                conn.execute("UPDATE services SET status = ? WHERE id = ? AND user_telegram_id = ?", (status, service_id, telegram_id))
            conn.commit()

    def activate_service_after_provisioning(self, service_id: int, telegram_id: int, *, is_test: bool = False, paid_amount: int = 0) -> None:
        with closing(self.connect()) as conn:
            conn.execute("UPDATE services SET status = 'active', pasarguard_sync_error = NULL WHERE id = ? AND user_telegram_id = ?", (service_id, telegram_id))
            if is_test:
                conn.execute("UPDATE users SET free_test_used = 1 WHERE telegram_id = ?", (telegram_id,))
            elif paid_amount > 0:
                conn.execute("UPDATE users SET first_purchase_done = 1 WHERE telegram_id = ?", (telegram_id,))
            conn.commit()

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

    def list_orders(self, telegram_id: int, limit: int = 7, include_expired: bool = False) -> list[sqlite3.Row]:
        with closing(self.connect()) as conn:
            if include_expired:
                return list(conn.execute("SELECT * FROM orders WHERE user_telegram_id = ? ORDER BY id DESC LIMIT ?", (telegram_id, limit)).fetchall())
            return list(conn.execute("SELECT * FROM orders WHERE user_telegram_id = ? AND COALESCE(status, '') != 'expired' ORDER BY id DESC LIMIT ?", (telegram_id, limit)).fetchall())

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
                display_name TEXT,
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

            CREATE TABLE IF NOT EXISTS required_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                invite_link TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_by INTEGER,
                created_at TEXT NOT NULL,
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
                max_discount_percent INTEGER NOT NULL DEFAULT 100,
                max_discount_amount INTEGER,
                min_order_amount INTEGER NOT NULL DEFAULT 0,
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

            CREATE TABLE IF NOT EXISTS package_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                price INTEGER NOT NULL DEFAULT 0,
                description TEXT,
                conditions TEXT,
                max_subscriptions INTEGER NOT NULL DEFAULT 1,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_by INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS package_template_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                package_id INTEGER NOT NULL,
                source_type TEXT NOT NULL DEFAULT 'manual',
                source_plan_key TEXT,
                item_key TEXT NOT NULL,
                title TEXT NOT NULL,
                data_gb REAL NOT NULL,
                days INTEGER NOT NULL,
                price INTEGER NOT NULL DEFAULT 0,
                sort_order INTEGER NOT NULL DEFAULT 100,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_packages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_telegram_id INTEGER NOT NULL,
                package_id INTEGER NOT NULL,
                code TEXT UNIQUE NOT NULL,
                assigned_by INTEGER,
                price INTEGER NOT NULL DEFAULT 0,
                description TEXT,
                conditions TEXT,
                max_subscriptions INTEGER NOT NULL DEFAULT 1,
                used_subscriptions INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'draft',
                order_id INTEGER,
                created_at TEXT NOT NULL,
                offered_at TEXT,
                purchased_at TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS package_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_package_id INTEGER NOT NULL,
                package_item_id INTEGER NOT NULL,
                service_id INTEGER NOT NULL,
                user_telegram_id INTEGER NOT NULL,
                order_id INTEGER,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS payment_cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                card_number TEXT NOT NULL,
                owner_name TEXT NOT NULL,
                bank_name TEXT,
                note TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS payment_receipts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                user_telegram_id INTEGER NOT NULL,
                card_id INTEGER,
                amount INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'waiting_receipt',
                receipt_file_id TEXT,
                receipt_file_unique_id TEXT,
                receipt_file_type TEXT,
                receipt_message_id INTEGER,
                receipt_chat_id INTEGER,
                user_caption TEXT,
                admin_note TEXT,
                expires_at TEXT,
                submitted_at TEXT,
                reviewed_by INTEGER,
                reviewed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS payment_receipt_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                receipt_id INTEGER NOT NULL,
                order_id INTEGER NOT NULL,
                user_telegram_id INTEGER NOT NULL,
                file_id TEXT NOT NULL,
                file_unique_id TEXT,
                file_type TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                caption TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS deadline_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                details TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(entity_type, entity_id, event_type)
            );
            """
        )
        for table, column, ddl in [
            ("users", "status", "ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"),
            ("users", "locked_reason", "ALTER TABLE users ADD COLUMN locked_reason TEXT"),
            ("users", "locked_notice", "ALTER TABLE users ADD COLUMN locked_notice TEXT"),
            ("users", "admin_note", "ALTER TABLE users ADD COLUMN admin_note TEXT"),
            ("users", "deleted_at", "ALTER TABLE users ADD COLUMN deleted_at TEXT"),
            ("admins", "display_name", "ALTER TABLE admins ADD COLUMN display_name TEXT"),
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
            ("services", "package_assignment_id", "ALTER TABLE services ADD COLUMN package_assignment_id INTEGER"),
            ("services", "package_item_id", "ALTER TABLE services ADD COLUMN package_item_id INTEGER"),
            ("orders", "coupon_code", "ALTER TABLE orders ADD COLUMN coupon_code TEXT"),
            ("orders", "coupon_discount", "ALTER TABLE orders ADD COLUMN coupon_discount INTEGER NOT NULL DEFAULT 0"),
            ("orders", "admin_note", "ALTER TABLE orders ADD COLUMN admin_note TEXT"),
            ("orders", "service_name", "ALTER TABLE orders ADD COLUMN service_name TEXT"),
            ("orders", "receipt_id", "ALTER TABLE orders ADD COLUMN receipt_id INTEGER"),
            ("coupons", "max_discount_amount", "ALTER TABLE coupons ADD COLUMN max_discount_amount INTEGER"),
            ("coupons", "min_order_amount", "ALTER TABLE coupons ADD COLUMN min_order_amount INTEGER NOT NULL DEFAULT 0"),
            ("coupons", "condition_json", "ALTER TABLE coupons ADD COLUMN condition_json TEXT"),
            ("coupons", "condition_label", "ALTER TABLE coupons ADD COLUMN condition_label TEXT"),
            ("payment_cards", "note", "ALTER TABLE payment_cards ADD COLUMN note TEXT"),
            ("payment_receipts", "admin_note", "ALTER TABLE payment_receipts ADD COLUMN admin_note TEXT"),
            ("payment_receipts", "expires_at", "ALTER TABLE payment_receipts ADD COLUMN expires_at TEXT"),
            ("payment_receipts", "submitted_at", "ALTER TABLE payment_receipts ADD COLUMN submitted_at TEXT"),
        ]:
            cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            if column not in cols:
                conn.execute(ddl)
        # Retire the removed service-pause feature: old paused rows are made active again.
        conn.execute("UPDATE services SET status = 'active', locked_reason = NULL WHERE status = ?", ("fro" + "zen",))
        conn.execute("UPDATE coupons SET max_discount_percent = 100 WHERE max_discount_percent IS NULL OR max_discount_percent < 100")

        for admin_id in BOOTSTRAP_SUPER_ADMIN_IDS:
            conn.execute(
                """
                INSERT INTO admins (telegram_id, role, display_name, added_by, is_active, created_at)
                VALUES (?, 'super', ?, NULL, 1, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET role = 'super', is_active = 1, display_name = COALESCE(admins.display_name, excluded.display_name)
                """,
                (admin_id, str(admin_id), now_iso()),
            )
        for admin_id in SALES_ADMIN_CHAT_IDS:
            conn.execute(
                """
                INSERT INTO admins (telegram_id, role, display_name, added_by, is_active, created_at)
                VALUES (?, 'sales', ?, NULL, 1, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET role = CASE WHEN role = 'super' THEN role ELSE 'sales' END, is_active = 1, display_name = COALESCE(admins.display_name, excluded.display_name)
                """,
                (admin_id, str(admin_id), now_iso()),
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
    # Sales may review payments/receipts and see order context only. It must not manage users, wallets, services, or broadcasts.
    "sales": {"dashboard", "orders", "payment_receipts", "packages"},
    "support": {"dashboard", "users", "services", "direct_message", "packages"},
    "marketing": {"dashboard", "broadcast", "coupons", "reports", "packages"},
    "channels": {"dashboard", "channels"},
    "appearance": {"dashboard", "appearance"},
}

ADMIN_ROLE_DESCRIPTIONS: dict[str, str] = {
    "super": "دسترسی کامل به همه بخش‌ها؛ فقط برای مالک/مدیر اصلی.",
    "sales": "فقط مدیریت سفارش‌ها و بررسی رسیدهای کارت‌به‌کارت؛ بدون بلاک/حذف کاربر و بدون تغییر کیف پول.",
    "support": "پشتیبانی کاربران، مشاهده/مدیریت سرویس‌ها و ارسال پیام مستقیم؛ بدون دسترسی مالی حساس.",
    "marketing": "کمپین، پیام همگانی، کد تخفیف و گزارش‌ها؛ بدون دسترسی عملیاتی به کاربران/پرداخت.",
    "appearance": "فقط تغییرات ظاهری ربات، متن‌ها، پیام‌ها، ایموجی‌ها و قالب‌های نمایشی.",
    "channels": "فقط مدیریت کانال‌های عضویت اجباری و لینک‌های بررسی عضویت.",
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


def admin_display_name(row_or_id: Any) -> str:
    """Return the admin display name. If it is empty, use the chat id as the name."""
    try:
        if isinstance(row_or_id, sqlite3.Row):
            if "display_name" in row_or_id.keys() and row_or_id["display_name"]:
                return str(row_or_id["display_name"])
            return str(row_or_id["telegram_id"])
    except Exception:
        pass
    return str(row_or_id or "-")


def get_admin_record(telegram_id: int) -> Optional[sqlite3.Row]:
    with closing(db.connect()) as conn:
        return conn.execute("SELECT * FROM admins WHERE telegram_id = ?", (telegram_id,)).fetchone()


def upsert_admin_local(telegram_id: int, role: str, added_by: int | None, display_name: str | None = None) -> None:
    clean_name = (display_name or str(telegram_id)).strip() or str(telegram_id)
    with closing(db.connect()) as conn:
        conn.execute(
            """
            INSERT INTO admins (telegram_id, role, display_name, added_by, is_active, created_at)
            VALUES (?, ?, ?, ?, 1, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                role = excluded.role,
                display_name = excluded.display_name,
                added_by = COALESCE(excluded.added_by, admins.added_by),
                is_active = 1
            """,
            (telegram_id, role, clean_name, added_by, now_iso()),
        )
        conn.commit()


def set_admin_display_name_local(telegram_id: int, display_name: str) -> None:
    clean_name = (display_name or str(telegram_id)).strip() or str(telegram_id)
    with closing(db.connect()) as conn:
        conn.execute("UPDATE admins SET display_name = ? WHERE telegram_id = ?", (clean_name, telegram_id))
        conn.commit()


def set_admin_active_local(telegram_id: int, is_active: bool) -> None:
    with closing(db.connect()) as conn:
        conn.execute("UPDATE admins SET is_active = ? WHERE telegram_id = ?", (1 if is_active else 0, telegram_id))
        conn.commit()


def set_admin_role_local(telegram_id: int, role: str, changed_by: int | None = None) -> None:
    with closing(db.connect()) as conn:
        conn.execute("UPDATE admins SET role = ?, added_by = COALESCE(?, added_by), is_active = 1 WHERE telegram_id = ?", (role, changed_by, telegram_id))
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
            "UPDATE users SET status = ?, locked_reason = ?, locked_notice = ?, deleted_at = ? WHERE telegram_id = ?",
            (status, reason or None, notice or None, deleted_at, telegram_id),
        )
        conn.commit()


def set_user_note(telegram_id: int, note: str) -> None:
    with closing(db.connect()) as conn:
        conn.execute("UPDATE users SET admin_note = ? WHERE telegram_id = ?", (note, telegram_id))
        conn.commit()


def list_all_users(limit: int = 100000, only_active: bool = False, buyers_only: bool = False, no_purchase: bool = False, include_deleted: bool = False, deleted_only: bool = False) -> list[sqlite3.Row]:
    with closing(db.connect()) as conn:
        where: list[str] = []
        if deleted_only:
            where.append("COALESCE(status, 'active') = 'deleted'")
        elif not include_deleted:
            where.append("COALESCE(status, 'active') != 'deleted'")
        if only_active:
            where.append("COALESCE(status, 'active') = 'active'")
        if buyers_only:
            where.append("first_purchase_done = 1")
        if no_purchase:
            where.append("first_purchase_done = 0")
        sql = "SELECT * FROM users"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
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
    if plan_key.startswith("wallet_topup:"):
        db.add_wallet(telegram_id, int(order["amount"]), "card_topup", f"شارژ کیف پول با تأیید رسید سفارش #{order_id}", admin_id)
    elif plan_key.startswith("pkg_assign:"):
        try:
            user_package_id = int(plan_key.split(":", 1)[1])
            mark_user_package_active(user_package_id, order_id)
        except Exception:
            pass
    elif plan_key in PLANS:
        service_name = (order["service_name"] if row_has(order, "service_name") and order["service_name"] else make_service_name(telegram_id))
        service_id = db.create_service(telegram_id, service_name, PLANS[plan_key], payable, is_test=False, status="provisioning" if settings.pasarguard_enabled else "active")
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
    service_id = db.create_service(telegram_id, make_service_name(telegram_id), plan, paid_amount, is_test=False, status="provisioning" if settings.pasarguard_enabled else "active")
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


def normalize_chat_ref(value: str) -> str:
    raw = normalize_digits(value or "").strip()
    raw = raw.replace("https://t.me/", "@").replace("http://t.me/", "@").replace("t.me/", "@")
    raw = raw.split("?", 1)[0].strip().rstrip("/")
    if raw.startswith("@"):
        return "@" + raw.lstrip("@").strip()
    return raw


def chat_ref_for_api(chat_id: str) -> int | str:
    value = normalize_chat_ref(chat_id)
    return int(value) if re.fullmatch(r"-?\d+", value) else value


def validate_required_channel_ref(value: str) -> tuple[bool, str, str]:
    ref = normalize_chat_ref(value)
    if not ref:
        return False, "", "شناسه کانال نمی‌تواند خالی باشد."
    if ref.startswith("@"):
        if not re.fullmatch(r"@[A-Za-z0-9_]{5,64}", ref):
            return False, "", "یوزرنیم کانال معتبر نیست. مثال: @HowToSeeWorld"
        return True, ref, ""
    if not re.fullmatch(r"-?\d{5,30}", ref):
        return False, "", "برای کانال خصوصی، آیدی عددی مثل -1001234567890 وارد کنید؛ برای کانال عمومی @username وارد کنید."
    return True, ref, ""


def list_required_channels(active_only: bool = False) -> list[sqlite3.Row]:
    with closing(db.connect()) as conn:
        where = "WHERE is_active = 1" if active_only else ""
        return list(conn.execute(f"SELECT * FROM required_channels {where} ORDER BY id DESC").fetchall())


def required_channel_by_id(channel_id: int) -> Optional[sqlite3.Row]:
    with closing(db.connect()) as conn:
        return conn.execute("SELECT * FROM required_channels WHERE id = ?", (channel_id,)).fetchone()


def required_channel_by_ref(chat_id: str) -> Optional[sqlite3.Row]:
    ref = normalize_chat_ref(chat_id)
    with closing(db.connect()) as conn:
        return conn.execute("SELECT * FROM required_channels WHERE chat_id = ?", (ref,)).fetchone()


def upsert_required_channel(chat_id: str, title: str, invite_link: str, admin_id: int) -> None:
    ref = normalize_chat_ref(chat_id)
    title = (title or ref).strip()[:80]
    invite = (invite_link or "").strip() or None
    with closing(db.connect()) as conn:
        conn.execute(
            """
            INSERT INTO required_channels (chat_id, title, invite_link, is_active, created_by, created_at, updated_at)
            VALUES (?, ?, ?, 1, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title = excluded.title,
                invite_link = excluded.invite_link,
                is_active = 1,
                updated_at = excluded.updated_at
            """,
            (ref, title, invite, admin_id, now_iso(), now_iso()),
        )
        conn.commit()
    admin_log(admin_id, "REQUIRED_CHANNEL_UPSERT", "channel", ref, title)


def set_required_channel_active(channel_id: int, active: bool, admin_id: int) -> bool:
    with closing(db.connect()) as conn:
        cur = conn.execute("UPDATE required_channels SET is_active = ?, updated_at = ? WHERE id = ?", (1 if active else 0, now_iso(), channel_id))
        conn.commit()
    if cur.rowcount:
        admin_log(admin_id, "REQUIRED_CHANNEL_ACTIVE", "channel", channel_id, f"active={active}")
        return True
    return False


def delete_required_channel(channel_id: int, admin_id: int) -> bool:
    with closing(db.connect()) as conn:
        cur = conn.execute("DELETE FROM required_channels WHERE id = ?", (channel_id,))
        conn.commit()
    if cur.rowcount:
        admin_log(admin_id, "REQUIRED_CHANNEL_DELETE", "channel", channel_id, "")
        return True
    return False


def required_channel_label(row: sqlite3.Row | dict[str, Any] | None, fallback: str = "") -> str:
    if not row:
        return fallback or "کانال"
    try:
        title = str(row["title"] or "").strip()
        chat_id = str(row["chat_id"] or "").strip()
    except Exception:
        title = str(row.get("title") or "").strip()  # type: ignore[attr-defined]
        chat_id = str(row.get("chat_id") or "").strip()  # type: ignore[attr-defined]
    return title or chat_id or fallback or "کانال"


def chat_member_status_name(member: Any) -> str:
    status = getattr(member, "status", "")
    value = getattr(status, "value", status)
    return str(value).split(".")[-1].lower()


async def is_member_of_channel(bot: Bot, telegram_id: int, chat_id: str) -> bool:
    try:
        member = await bot.get_chat_member(chat_ref_for_api(chat_id), telegram_id)
        status = chat_member_status_name(member)
        if status in {"creator", "administrator", "member"}:
            return True
        if status == "restricted" and bool(getattr(member, "is_member", False)):
            return True
        return False
    except Exception as exc:
        logger.warning("required channel membership check failed: chat=%s user=%s err=%r", chat_id, telegram_id, exc)
        return False


async def missing_required_channels(bot: Bot, telegram_id: int) -> list[sqlite3.Row]:
    missing: list[sqlite3.Row] = []
    for channel in list_required_channels(active_only=True):
        if not await is_member_of_channel(bot, telegram_id, str(channel["chat_id"])):
            missing.append(channel)
    return missing


def required_channels_prompt_text(missing: list[sqlite3.Row] | None = None) -> str:
    channels = missing if missing is not None else list_required_channels(active_only=True)
    text = header("📣 عضویت در کانال‌های الزامی")
    text += "برای استفاده از ربات، ابتدا باید عضو کانال‌های مشخص‌شده شوید و بعد دکمه بررسی عضویت را بزنید.\n\n"
    if channels:
        text += "کانال‌های لازم:\n"
        for ch in channels:
            text += f"• <b>{h(required_channel_label(ch))}</b> <code>{h(ch['chat_id'])}</code>\n"
    else:
        text += "فعلاً کانال الزامی فعال نیست."
    return text


def required_channels_user_kb(channels: list[sqlite3.Row] | None = None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for ch in (channels if channels is not None else list_required_channels(active_only=True)):
        link = str(ch["invite_link"] or "").strip()
        label = f"📣 عضویت در {required_channel_label(ch)}"
        if link.startswith(("http://", "https://", "tg://")):
            rows.append([InlineKeyboardButton(text=label, url=link)])
    rows.append([InlineKeyboardButton(text="✅ بررسی دوباره عضویت", callback_data="check_required_channels")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def required_channels_admin_kb() -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = [[("➕ افزودن کانال", "adm_reqch_add")]]
    for ch in list_required_channels(active_only=False)[:30]:
        active = int(ch["is_active"] or 0) == 1
        icon = "✅" if active else "⛔"
        rows.append([(f"{icon} {required_channel_label(ch)[:28]}", f"adm_reqch_view:{ch['id']}")])
    rows.append([("👑 منوی ادمین", "adm_home")])
    return inline(rows)


def required_channel_admin_view_kb(channel_id: int) -> InlineKeyboardMarkup:
    ch = required_channel_by_id(channel_id)
    active = bool(ch and int(ch["is_active"] or 0))
    toggle = ("⛔ غیرفعال کردن", f"adm_reqch_toggle:{channel_id}:0") if active else ("✅ فعال کردن", f"adm_reqch_toggle:{channel_id}:1")
    return inline([
        [toggle, ("🗑 حذف", f"adm_reqch_delete:{channel_id}")],
        [("⬅️ کانال‌های الزامی", "adm_required_channels"), ("👑 منوی ادمین", "adm_home")],
    ])


def coupon_usage_generic_error() -> str:
    return "این کد منقضی شده، برای شما نیست یا قابل استفاده نیست."


def safe_json_loads(value: Any, default: Any) -> Any:
    try:
        if not value:
            return default
        return json.loads(str(value))
    except Exception:
        return default


def empty_coupon_condition() -> dict[str, Any]:
    return {"version": 1, "groups": []}


def coupon_condition_groups(condition: dict[str, Any] | None) -> list[list[dict[str, Any]]]:
    if not condition:
        return []
    groups = condition.get("groups") or []
    clean: list[list[dict[str, Any]]] = []
    for group in groups:
        if isinstance(group, list):
            clauses = [c for c in group if isinstance(c, dict) and c.get("type")]
            if clauses:
                clean.append(clauses)
    return clean


def coupon_clause_label(clause: dict[str, Any]) -> str:
    ctype = str(clause.get("type") or "")
    if ctype == "first_purchase":
        label = "خرید اول کاربر"
    elif ctype == "channel_member":
        label = f"عضویت در کانال «{clause.get('title') or clause.get('chat_id') or 'کانال'}»"
    elif ctype == "user_ids":
        ids = clause.get("ids") or []
        label = f"چت‌آیدی داخل لیست {len(ids)} نفره"
    elif ctype == "admin_roles":
        roles = ", ".join(clause.get("roles") or [])
        label = f"ادمین با نقش‌های: {roles or 'نامشخص'}"
    else:
        label = "شرط ناشناخته"
    return f"نه ({label})" if clause.get("negate") else label


def render_coupon_condition_label(condition: dict[str, Any] | None) -> str:
    groups = coupon_condition_groups(condition)
    if not groups:
        return "بدون شرط خاص؛ همه کاربران مجاز هستند."
    parts: list[str] = []
    for group in groups:
        parts.append("(" + " و ".join(coupon_clause_label(c) for c in group) + ")")
    return " یا ".join(parts)


def add_coupon_clause(condition: dict[str, Any], clause: dict[str, Any], mode: str = "and") -> dict[str, Any]:
    groups = coupon_condition_groups(condition)
    if mode == "or" or not groups:
        groups.append([clause])
    else:
        groups[-1].append(clause)
    return {"version": 1, "groups": groups}


def negate_last_coupon_clause(condition: dict[str, Any]) -> dict[str, Any]:
    groups = coupon_condition_groups(condition)
    if groups and groups[-1]:
        groups[-1][-1]["negate"] = not bool(groups[-1][-1].get("negate"))
    return {"version": 1, "groups": groups}


def coupon_condition_preview_text(condition: dict[str, Any] | None) -> str:
    return header("🧩 شرایط استفاده از کد تخفیف") + "نمایش منطقی شرط‌ها:\n\n" + f"<b>{h(render_coupon_condition_label(condition))}</b>"


def coupon_condition_builder_kb() -> InlineKeyboardMarkup:
    return inline([
        [("➕ افزودن شرط AND", "adm_coupon_cond_add:and"), ("➕ افزودن گروه OR", "adm_coupon_cond_add:or")],
        [("🔁 NOT برای آخرین شرط", "adm_coupon_cond_negate"), ("🧹 پاک کردن شروط", "adm_coupon_cond_clear")],
        [("✅ ادامه ساخت کد", "adm_coupon_cond_done")],
        [("⬅️ بازگشت", "adm_coupons"), ("👑 منوی ادمین", "adm_home")],
    ])


def coupon_condition_type_kb() -> InlineKeyboardMarkup:
    return inline([
        [("🆕 خرید اول", "adm_coupon_cond_type:first_purchase")],
        [("📣 عضو یک کانال خاص", "adm_coupon_cond_type:channel_member")],
        [("👥 داخل لیست چت‌آیدی", "adm_coupon_cond_type:user_ids")],
        [("👮 نوع ادمین", "adm_coupon_cond_type:admin_roles")],
        [("⬅️ شرایط کد", "adm_coupon_condition_builder"), ("👑 منوی ادمین", "adm_home")],
    ])


def coupon_condition_channel_select_kb() -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    for ch in list_required_channels(active_only=True)[:30]:
        rows.append([(required_channel_label(ch)[:45], f"adm_coupon_cond_channel:{ch['id']}")])
    rows.append([("⬅️ انتخاب نوع شرط", "adm_coupon_cond_type_back"), ("👑 منوی ادمین", "adm_home")])
    return inline(rows)


def coupon_condition_admin_roles_kb(selected: list[str] | None = None) -> InlineKeyboardMarkup:
    selected_set = set(selected or [])
    rows: list[list[tuple[str, str]]] = []
    for role in ADMIN_ROLE_PERMISSIONS.keys():
        icon = "✅" if role in selected_set else "☐"
        rows.append([(f"{icon} {role}", f"adm_coupon_cond_role_toggle:{role}")])
    rows.append([("✅ ثبت نقش‌ها", "adm_coupon_cond_role_done")])
    rows.append([("⬅️ انتخاب نوع شرط", "adm_coupon_cond_type_back"), ("👑 منوی ادمین", "adm_home")])
    return inline(rows)


async def coupon_clause_matches(clause: dict[str, Any], telegram_id: int, bot: Bot | None) -> bool:
    ctype = str(clause.get("type") or "")
    result = False
    if ctype == "first_purchase":
        user = db.get_user(telegram_id)
        result = bool(user and int(user["first_purchase_done"] or 0) == 0)
    elif ctype == "channel_member":
        result = bool(bot and await is_member_of_channel(bot, telegram_id, str(clause.get("chat_id") or "")))
    elif ctype == "user_ids":
        result = str(telegram_id) in {str(x) for x in (clause.get("ids") or [])}
    elif ctype == "admin_roles":
        role = admin_role(telegram_id)
        result = bool(role and role in set(clause.get("roles") or []))
    if clause.get("negate"):
        return not result
    return result


async def coupon_conditions_match(condition: dict[str, Any] | None, telegram_id: int, bot: Bot | None) -> bool:
    groups = coupon_condition_groups(condition)
    if not groups:
        return True
    for group in groups:
        ok = True
        for clause in group:
            if not await coupon_clause_matches(clause, telegram_id, bot):
                ok = False
                break
        if ok:
            return True
    return False


async def validate_coupon_for_order(code: str, telegram_id: int, order: sqlite3.Row, bot: Bot | None = None) -> tuple[Optional[sqlite3.Row], str]:
    if setting_get("coupon_enabled", "1") != "1":
        return None, coupon_usage_generic_error()
    code = (code or "").strip().upper()
    row = coupon_row(code)
    if not row or not int(row["active"]):
        return None, coupon_usage_generic_error()
    if row["expires_at"]:
        try:
            if datetime.fromisoformat(str(row["expires_at"])) < datetime.now(TEHRAN_TZ):
                return None, coupon_usage_generic_error()
        except ValueError:
            pass
    usage_limit = row["usage_limit"]
    if usage_limit is not None and int(row["used_count"]) >= int(usage_limit):
        return None, coupon_usage_generic_error()
    if int(row["per_user_limit"] or 1) <= coupon_usage_count(code, telegram_id):
        return None, coupon_usage_generic_error()
    min_order_amount = int(row["min_order_amount"] if row_has(row, "min_order_amount") and row["min_order_amount"] is not None else 0)
    if min_order_amount and int(order["amount"]) < min_order_amount:
        return None, coupon_usage_generic_error()

    condition = safe_json_loads(row["condition_json"] if row_has(row, "condition_json") else None, None)
    if condition:
        if not await coupon_conditions_match(condition, telegram_id, bot):
            return None, coupon_usage_generic_error()
    else:
        # Backward compatibility for old coupons created before this hotfix.
        scope = str(row["scope"] or "all")
        targets = [x.strip() for x in str(row["target_user_ids"] or "").split(",") if x.strip()]
        if scope in {"user", "users"} and str(telegram_id) not in targets:
            return None, coupon_usage_generic_error()
        if scope == "first_purchase":
            user = db.get_user(telegram_id)
            if user and int(user["first_purchase_done"]):
                return None, coupon_usage_generic_error()
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


def mark_order_terminal(order_id: int, *, status: str, method: str, wallet_used: int = 0, service_id: int | None = None, admin_note: str | None = None) -> None:
    with closing(db.connect()) as conn:
        conn.execute(
            """
            UPDATE orders
            SET status = ?, payment_method = ?, wallet_used = ?, service_id = COALESCE(?, service_id), admin_note = COALESCE(?, admin_note)
            WHERE id = ?
            """,
            (status, method, wallet_used, service_id, admin_note, order_id),
        )
        conn.commit()


def _remote_result_is_production_ready(result: Any) -> bool:
    if not settings.pasarguard_enabled:
        return True
    return bool(result and result.ok and result.applied and result.subscription_url)


def _remote_failure_text(result: Any) -> str:
    if result is None:
        return "Pasarguard نتیجه‌ای برنگرداند."
    return str(getattr(result, "error", None) or getattr(result, "message", None) or "فعال‌سازی remote ناموفق بود.")


async def provision_service_or_mark_failed(service_id: int, telegram_id: int, *, order_id: int | None, is_test: bool, paid_amount: int) -> tuple[bool, Any, sqlite3.Row | None]:
    service = db.get_service(service_id, telegram_id)
    if not service:
        return False, None, None
    result = await create_remote_user_for_service(db, service, order_id=order_id)
    service = db.get_service(service_id, telegram_id) or service
    if _remote_result_is_production_ready(result):
        db.activate_service_after_provisioning(service_id, telegram_id, is_test=is_test, paid_amount=paid_amount)
        if result and getattr(result, "applied", False):
            # Pull once so usage/expire/subscription_url exactly match panel response.
            fresh = db.get_service(service_id, telegram_id)
            if fresh and 'pasarguard_username' in fresh.keys() and fresh['pasarguard_username']:
                await sync_remote_user_from_panel(db, fresh)
        return True, result, db.get_service(service_id, telegram_id)
    error = _remote_failure_text(result)
    db.set_service_status(service_id, telegram_id, "provisioning_failed", error)
    return False, result, db.get_service(service_id, telegram_id)


def refund_wallet_payment_if_needed(telegram_id: int, amount: int, order_id: int, reason: str) -> None:
    if amount > 0:
        db.add_wallet(telegram_id, amount, "wallet_refund", f"برگشت پرداخت سفارش #{order_id}: {reason[:80]}")


def create_coupon_admin(
    code: str,
    percent: int,
    title: str,
    scope: str,
    target_user_ids: str,
    usage_limit: Optional[int],
    expires_days: Optional[int],
    admin_id: int,
    *,
    per_user_limit: int = 1,
    max_discount_percent: int = 100,
    max_discount_amount: Optional[int] = None,
    min_order_amount: int = 0,
    stack_with_referral: int = 1,
    condition_json: str | None = None,
    condition_label: str | None = None,
) -> None:
    expires_at = None
    if expires_days and expires_days > 0:
        expires_at = (datetime.now(TEHRAN_TZ) + timedelta(days=expires_days)).isoformat(timespec="seconds")
    with closing(db.connect()) as conn:
        conn.execute(
            """
            INSERT INTO coupons
            (code, percent, title, scope, target_user_ids, usage_limit, per_user_limit, used_count,
             stack_with_referral, max_discount_percent, max_discount_amount, min_order_amount, condition_json, condition_label, active, expires_at, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                percent = excluded.percent,
                title = excluded.title,
                scope = excluded.scope,
                target_user_ids = excluded.target_user_ids,
                usage_limit = excluded.usage_limit,
                per_user_limit = excluded.per_user_limit,
                stack_with_referral = excluded.stack_with_referral,
                max_discount_percent = excluded.max_discount_percent,
                max_discount_amount = excluded.max_discount_amount,
                min_order_amount = excluded.min_order_amount,
                condition_json = excluded.condition_json,
                condition_label = excluded.condition_label,
                active = 1,
                expires_at = excluded.expires_at
            """,
            (
                code.upper(), percent, title, scope, target_user_ids or None, usage_limit,
                max(int(per_user_limit), 1), 1 if stack_with_referral else 0,
                min(max(int(max_discount_percent), 0), 100), max_discount_amount, max(int(min_order_amount), 0),
                condition_json, condition_label, expires_at, admin_id, now_iso(),
            ),
        )
        conn.commit()
    admin_log(admin_id, "COUPON_UPSERT", "coupon", code.upper(), f"percent={percent}, scope={scope}, limit={usage_limit}, per_user={per_user_limit}, min={min_order_amount}, max_amount={max_discount_amount}, expires_days={expires_days}")


def disable_coupon_admin(code: str, admin_id: int) -> bool:
    with closing(db.connect()) as conn:
        cur = conn.execute("UPDATE coupons SET active = 0 WHERE code = ?", (code.upper(),))
        conn.commit()
    if cur.rowcount:
        admin_log(admin_id, "COUPON_DISABLE", "coupon", code.upper(), "")
        return True
    return False


def update_coupon_condition_admin(code: str, condition: dict[str, Any], admin_id: int) -> bool:
    condition_label = render_coupon_condition_label(condition)
    with closing(db.connect()) as conn:
        cur = conn.execute(
            "UPDATE coupons SET condition_json = ?, condition_label = ? WHERE code = ?",
            (json.dumps(condition, ensure_ascii=False), condition_label, code.upper()),
        )
        conn.commit()
    if cur.rowcount:
        admin_log(admin_id, "COUPON_CONDITION_UPDATE", "coupon", code.upper(), condition_label[:500])
        return True
    return False


# -----------------------------
# Card-to-card payment helpers
# -----------------------------
def normalize_card_number(value: str) -> str:
    return re.sub(r"\D+", "", normalize_digits(value or ""))


def format_card_number(card_number: str) -> str:
    digits = normalize_card_number(card_number)
    return "-".join(digits[i:i + 4] for i in range(0, len(digits), 4)) if digits else ""


def list_payment_cards(active_only: bool = False) -> list[sqlite3.Row]:
    with closing(db.connect()) as conn:
        if active_only:
            return list(conn.execute("SELECT * FROM payment_cards WHERE is_active = 1 ORDER BY id DESC").fetchall())
        return list(conn.execute("SELECT * FROM payment_cards ORDER BY id DESC").fetchall())


def get_payment_card(card_id: int) -> Optional[sqlite3.Row]:
    with closing(db.connect()) as conn:
        return conn.execute("SELECT * FROM payment_cards WHERE id = ?", (card_id,)).fetchone()


def choose_payment_card() -> Optional[sqlite3.Row]:
    cards = list_payment_cards(active_only=True)
    if not cards:
        return None
    return random.choice(cards)


def add_payment_card_admin(card_number: str, owner_name: str, bank_name: str = "", note: str = "", active: int = 1) -> int:
    digits = normalize_card_number(card_number)
    with closing(db.connect()) as conn:
        cur = conn.execute(
            """
            INSERT INTO payment_cards (card_number, owner_name, bank_name, note, is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (digits, owner_name.strip(), bank_name.strip(), note.strip(), 1 if active else 0, now_iso(), now_iso()),
        )
        conn.commit()
        return int(cur.lastrowid)


def toggle_payment_card_admin(card_id: int) -> bool:
    with closing(db.connect()) as conn:
        row = conn.execute("SELECT is_active FROM payment_cards WHERE id = ?", (card_id,)).fetchone()
        if not row:
            return False
        conn.execute("UPDATE payment_cards SET is_active = ?, updated_at = ? WHERE id = ?", (0 if int(row["is_active"]) else 1, now_iso(), card_id))
        conn.commit()
        return True


def delete_payment_card_admin(card_id: int) -> bool:
    with closing(db.connect()) as conn:
        cur = conn.execute("DELETE FROM payment_cards WHERE id = ?", (card_id,))
        conn.commit()
        return bool(cur.rowcount)


def payment_card_label(card: sqlite3.Row) -> str:
    status = "فعال" if int(card["is_active"] or 0) else "غیرفعال"
    bank = f" | {card['bank_name']}" if card["bank_name"] else ""
    return f"#{card['id']} | {format_card_number(card['card_number'])} | {card['owner_name']}{bank} | {status}"


def payment_cards_text() -> str:
    cards = list_payment_cards(False)
    text = header("💳 روش کارت‌به‌کارت")
    text += "از این بخش می‌توانید چند شماره کارت ثبت کنید. هنگام پرداخت، ربات یکی از کارت‌های فعال را به‌صورت تصادفی به کاربر نمایش می‌دهد.\n\n"
    text += "ادمین رسیدها به نقش <b>sales</b> ارسال می‌شود. اگر می‌خواهی مستقیم چت‌آیدی خاصی بگیرد، در env مقدار <code>SALES_ADMIN_CHAT_IDS</code> را بگذار.\n\n"
    if not cards:
        text += "هنوز کارتی ثبت نشده است."
    else:
        for card in cards:
            text += f"• <code>{h(payment_card_label(card))}</code>\n"
    return text


def payment_cards_kb() -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = [[("➕ افزودن کارت", "adm_card_add")]]
    for card in list_payment_cards(False)[:20]:
        action = "⛔ غیرفعال" if int(card["is_active"] or 0) else "✅ فعال"
        rows.append([(f"{action} #{card['id']}", f"adm_card_toggle:{card['id']}"), (f"🗑 حذف #{card['id']}", f"adm_card_delete:{card['id']}")])
    rows.append([("👑 منوی ادمین", "adm_home")])
    return inline(rows)


def create_or_reset_payment_receipt(order: sqlite3.Row, card: sqlite3.Row, amount: int) -> int:
    expires_at = (datetime.now(TEHRAN_TZ) + timedelta(minutes=RECEIPT_UPLOAD_WINDOW_MINUTES)).isoformat(timespec="seconds")
    with closing(db.connect()) as conn:
        existing = conn.execute(
            "SELECT * FROM payment_receipts WHERE order_id = ? AND status = 'waiting_receipt' ORDER BY id DESC LIMIT 1",
            (int(order["id"]),),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE payment_receipts
                SET card_id = ?, amount = ?, status = 'waiting_receipt', expires_at = ?, submitted_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (int(card["id"]), amount, expires_at, now_iso(), int(existing["id"])),
            )
            rid = int(existing["id"])
        else:
            cur = conn.execute(
                """
                INSERT INTO payment_receipts (order_id, user_telegram_id, card_id, amount, status, expires_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'waiting_receipt', ?, ?, ?)
                """,
                (int(order["id"]), int(order["user_telegram_id"]), int(card["id"]), amount, expires_at, now_iso(), now_iso()),
            )
            rid = int(cur.lastrowid)
        conn.execute("UPDATE orders SET receipt_id = ?, payment_method = ? WHERE id = ?", (rid, "کارت به کارت", int(order["id"])))
        conn.commit()
        return rid


def get_receipt(receipt_id: int) -> Optional[sqlite3.Row]:
    with closing(db.connect()) as conn:
        return conn.execute("SELECT * FROM payment_receipts WHERE id = ?", (receipt_id,)).fetchone()


def get_receipt_by_order(order_id: int) -> Optional[sqlite3.Row]:
    with closing(db.connect()) as conn:
        return conn.execute("SELECT * FROM payment_receipts WHERE order_id = ? ORDER BY id DESC LIMIT 1", (order_id,)).fetchone()


def receipt_deadline_expired(receipt: sqlite3.Row) -> bool:
    if not row_has(receipt, "expires_at") or not receipt["expires_at"]:
        return False
    deadline = parse_dt(str(receipt["expires_at"]))
    if not deadline:
        return False
    expired = deadline <= datetime.now(TEHRAN_TZ)
    if expired:
        # Make expiration durable immediately. This is important when the bot was
        # down during the payment window: the next interaction must not revive an
        # old receipt/order.
        try:
            expire_sqlite_payment_deadlines()
        except Exception:
            logger.exception("failed to persist expired receipt deadline")
    return expired


def list_receipt_files(receipt_id: int) -> list[sqlite3.Row]:
    with closing(db.connect()) as conn:
        return list(conn.execute("SELECT * FROM payment_receipt_files WHERE receipt_id = ? ORDER BY id ASC", (receipt_id,)).fetchall())


def receipt_file_count(receipt_id: int) -> int:
    with closing(db.connect()) as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM payment_receipt_files WHERE receipt_id = ?", (receipt_id,)).fetchone()
        return int(row["c"] if row else 0)


def set_receipt_user_note(receipt_id: int, note: str) -> None:
    with closing(db.connect()) as conn:
        conn.execute("UPDATE payment_receipts SET user_caption = ?, updated_at = ? WHERE id = ?", (note.strip(), now_iso(), receipt_id))
        conn.commit()


def add_receipt_file(receipt_id: int, message: Message, file_type: str, file_id: str, file_unique_id: str | None, caption: str | None) -> None:
    with closing(db.connect()) as conn:
        receipt = conn.execute("SELECT * FROM payment_receipts WHERE id = ?", (receipt_id,)).fetchone()
        if not receipt:
            return
        conn.execute(
            """
            INSERT INTO payment_receipt_files
            (receipt_id, order_id, user_telegram_id, file_id, file_unique_id, file_type, message_id, chat_id, caption, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                receipt_id,
                int(receipt["order_id"]),
                int(receipt["user_telegram_id"]),
                file_id,
                file_unique_id,
                file_type,
                int(message.message_id),
                int(message.chat.id),
                caption or "",
                now_iso(),
            ),
        )
        # Keep legacy single-file columns updated for compatibility with old rows/admin screens.
        conn.execute(
            """
            UPDATE payment_receipts
            SET receipt_file_id = ?, receipt_file_unique_id = ?, receipt_file_type = ?,
                receipt_message_id = ?, receipt_chat_id = ?,
                user_caption = COALESCE(NULLIF(?, ''), user_caption), updated_at = ?
            WHERE id = ?
            """,
            (file_id, file_unique_id, file_type, int(message.message_id), int(message.chat.id), caption or "", now_iso(), receipt_id),
        )
        conn.commit()


def attach_receipt_file(receipt_id: int, message: Message, file_type: str, file_id: str, file_unique_id: str | None, caption: str | None) -> None:
    add_receipt_file(receipt_id, message, file_type, file_id, file_unique_id, caption)


def submit_payment_receipt(receipt_id: int) -> bool:
    with closing(db.connect()) as conn:
        receipt = conn.execute("SELECT * FROM payment_receipts WHERE id = ?", (receipt_id,)).fetchone()
        if not receipt:
            return False
        count = conn.execute("SELECT COUNT(*) AS c FROM payment_receipt_files WHERE receipt_id = ?", (receipt_id,)).fetchone()
        has_new_files = bool(count and int(count["c"]) > 0)
        has_legacy_file = bool(receipt["receipt_chat_id"] and receipt["receipt_message_id"])
        if not has_new_files and not has_legacy_file:
            return False
        if str(receipt["status"]) != "waiting_receipt":
            return False
        conn.execute(
            "UPDATE payment_receipts SET status = 'receipt_pending', submitted_at = ?, updated_at = ? WHERE id = ?",
            (now_iso(), now_iso(), receipt_id),
        )
        conn.execute("UPDATE orders SET status = 'receipt_pending', payment_method = 'کارت به کارت', receipt_id = ? WHERE id = ?", (receipt_id, int(receipt["order_id"])))
        conn.commit()
        return True


def receipt_upload_kb(receipt_id: int, order_id: int, can_submit: bool) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    if can_submit:
        rows.append([("✅ ثبت تراکنش و ارسال برای فروش", f"receipt_submit:{receipt_id}")])
    rows.append([("❌ لغو ارسال رسید", f"pay_page:{order_id}"), ("🏠 منوی اصلی", "home")])
    return inline(rows)


def update_receipt_review(receipt_id: int, status: str, admin_id: int, note: str = "") -> None:
    with closing(db.connect()) as conn:
        receipt = conn.execute("SELECT * FROM payment_receipts WHERE id = ?", (receipt_id,)).fetchone()
        if not receipt:
            return
        conn.execute(
            """
            UPDATE payment_receipts
            SET status = ?, admin_note = ?, reviewed_by = ?, reviewed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, note or "", admin_id, now_iso(), now_iso(), receipt_id),
        )
        if status == "rejected":
            conn.execute("UPDATE orders SET status = 'payment_rejected', admin_note = COALESCE(?, admin_note) WHERE id = ?", (note or None, int(receipt["order_id"])))
        conn.commit()


def payment_receipt_summary_for_order(order_id: int) -> str:
    receipt = get_receipt_by_order(order_id)
    if not receipt:
        return ""
    card = get_payment_card(int(receipt["card_id"])) if receipt["card_id"] else None
    status_map = {
        "waiting_receipt": "در انتظار ارسال رسید",
        "receipt_pending": "رسید در انتظار بررسی",
        "approved": "رسید تأیید شده",
        "rejected": "رسید رد شده",
        "approved_provisioning_failed": "رسید تأیید شد، فعال‌سازی نیازمند بررسی",
    }
    text = "\n\n🧾 <b>رسید کارت‌به‌کارت</b>\n"
    text += f"وضعیت رسید: <b>{h(status_map.get(str(receipt['status']), receipt['status']))}</b>\n"
    text += f"مبلغ رسید: <b>{fmt_money(int(receipt['amount']))}</b>\n"
    text += f"تعداد فایل‌های رسید: <b>{fmt_number(receipt_file_count(int(receipt['id'])))}</b>\n"
    if row_has(receipt, "expires_at") and receipt["expires_at"] and str(receipt["status"]) == "waiting_receipt":
        text += f"مهلت ارسال رسید: <code>{h(fmt_jalali_datetime(receipt['expires_at']))}</code>\n"
    if card:
        text += f"کارت مقصد: <code>{h(format_card_number(card['card_number']))}</code> — {h(card['owner_name'])}\n"
    if receipt["admin_note"]:
        text += f"یادداشت بررسی: <code>{h(receipt['admin_note'])}</code>\n"
    return text


def sales_admin_ids() -> set[int]:
    ids: set[int] = set(SALES_ADMIN_CHAT_IDS)
    with closing(db.connect()) as conn:
        rows = conn.execute("SELECT telegram_id FROM admins WHERE is_active = 1 AND role = 'sales'").fetchall()
    ids.update(int(r["telegram_id"]) for r in rows)
    return ids


def receipt_admin_kb(receipt_id: int, order_id: int) -> InlineKeyboardMarkup:
    return inline([
        [("🖼 مشاهده رسید", f"adm_receipt_view:{receipt_id}")],
        [("✅ تأیید رسید", f"adm_receipt_approve:{receipt_id}"), ("❌ رد رسید", f"adm_receipt_reject:{receipt_id}")],
        [("🧾 جزئیات سفارش", f"adm_order:{order_id}"), ("👑 منوی ادمین", "adm_home")],
    ])


def card_payment_instructions(order: sqlite3.Row, card: sqlite3.Row, payable: int) -> str:
    return (
        header("💳 پرداخت کارت‌به‌کارت", f"سفارش #{order['id']}")
        + f"لطفاً مبلغ را <b>دقیقاً و بدون هیچ تغییری</b> واریز کنید:\n"
        + f"💰 مبلغ دقیق: <b>{fmt_money(payable)}</b>\n\n"
        + f"💳 شماره کارت:\n<code>{h(format_card_number(card['card_number']))}</code>\n"
        + f"👤 به نام: <b>{h(card['owner_name'])}</b>\n"
        + (f"🏦 بانک: <b>{h(card['bank_name'])}</b>\n" if card["bank_name"] else "")
        + (f"📝 توضیح کارت: {h(card['note'])}\n" if row_has(card, "note") and card["note"] else "")
        + "\n📌 توجه: در برخی اپلیکیشن‌های پرداخت مانند آپ ممکن است هنگام انتقال، خطای محدودیت کارت نمایش داده شود. در این شرایط از همراه‌بانک یا اینترنت‌بانک خود استفاده کنید.\n"
        + "🚫 لطفاً از انتقال وجه از طریق پایا یا پل خودداری کنید.\n"
        + "⚠️ مبلغ را گرد نکنید، کمتر یا بیشتر نزنید و توضیح اضافه لازم نیست.\n\n"
        + f"بعد از پرداخت، تا <b>{RECEIPT_UPLOAD_WINDOW_MINUTES} دقیقه</b> فرصت دارید یک یا چند عکس/فایل رسید را همین‌جا بفرستید.\n"
        + "بعد از ارسال همه رسیدها و توضیحات، دکمه <b>ثبت تراکنش</b> را بزنید تا رسیدها برای ادمین فروش ارسال شود."
    )


def receipt_pending_user_text(order: sqlite3.Row) -> str:
    return (
        header("🧾 رسید شما ثبت شد", f"سفارش #{order['id']}")
        + "رسید پرداخت شما برای ادمین فروش ارسال شد و سفارش در صف بررسی قرار گرفت.\n\n"
        + "پس از تأیید، سرویس به‌صورت خودکار فعال می‌شود و لینک اشتراک برای شما ارسال خواهد شد.\n"
        + "اگر رسید نیاز به اصلاح داشته باشد، نتیجه همراه با توضیح برایتان ارسال می‌شود."
    )


def receipt_notify_admin_text(order: sqlite3.Row, receipt: sqlite3.Row) -> str:
    user = db.get_user(int(order["user_telegram_id"]))
    username = f"@{user['username']}" if user and user["username"] else "ندارد"
    submitted = receipt["submitted_at"] if row_has(receipt, "submitted_at") and receipt["submitted_at"] else receipt["updated_at"]
    note = receipt["user_caption"] if row_has(receipt, "user_caption") and receipt["user_caption"] else ""
    return (
        header("🧾 رسید جدید کارت‌به‌کارت", f"Receipt #{receipt['id']}")
        + f"سفارش: <code>#{order['id']}</code>\n"
        + f"کاربر: <code>{order['user_telegram_id']}</code> | {h(username)}\n"
        + f"مبلغ قابل پرداخت: <b>{fmt_money(max(int(order['amount']) - int(order['discount_amount']), 0))}</b>\n"
        + f"نوع سفارش: <code>{h(order['plan_key'])}</code>\n"
        + f"زمان ثبت رسید: <code>{h(fmt_jalali_datetime(submitted))}</code>\n"
        + f"تعداد فایل‌ها: <b>{fmt_number(receipt_file_count(int(receipt['id'])))}</b>\n"
        + (f"توضیح کاربر: <code>{h(note)}</code>\n" if note else "")
        + "\nدر این پیام فقط خلاصه رسید آمده است. برای دریافت عکس/فایل‌ها، دکمه «مشاهده رسید» را بزنید."
    )


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


class CardPaymentStates(StatesGroup):
    waiting_receipt = State()
    waiting_receipt_bundle = State()


class AdminStates(StatesGroup):
    waiting_user_search = State()
    waiting_wallet_amount = State()
    waiting_wallet_reason = State()
    waiting_direct_message = State()
    waiting_broadcast_message = State()
    waiting_bot_lock_message = State()
    waiting_coupon_line = State()  # legacy fallback only
    waiting_coupon_code = State()
    waiting_coupon_percent = State()
    waiting_coupon_users = State()
    waiting_coupon_usage_limit = State()
    waiting_coupon_per_user_limit = State()
    waiting_coupon_min_order = State()
    waiting_coupon_max_amount = State()
    waiting_coupon_expires = State()
    waiting_coupon_condition_users = State()
    waiting_coupon_edit_condition_code = State()
    waiting_required_channel_id = State()
    waiting_required_channel_title = State()
    waiting_required_channel_link = State()
    waiting_disable_coupon = State()
    waiting_add_admin_line = State()  # legacy fallback only
    waiting_add_admin_chat_id = State()
    waiting_add_admin_role = State()
    waiting_add_admin_name = State()
    waiting_edit_admin_name = State()
    waiting_user_note = State()
    waiting_manual_service_amount = State()
    waiting_card_line = State()  # legacy fallback only
    waiting_card_number = State()
    waiting_card_owner = State()
    waiting_card_bank = State()
    waiting_card_note = State()
    waiting_card_active = State()
    waiting_payment_review_note = State()
    waiting_package_name = State()
    waiting_package_price = State()
    waiting_package_code = State()
    waiting_package_description = State()
    waiting_package_conditions = State()
    waiting_package_max_subs = State()
    waiting_package_manual_item = State()  # legacy fallback only
    waiting_package_manual_title = State()
    waiting_package_manual_data = State()
    waiting_package_manual_days = State()
    waiting_package_manual_price = State()
    waiting_package_sales_override = State()  # legacy fallback only
    waiting_package_sales_price = State()
    waiting_package_sales_edit_title = State()
    waiting_package_sales_edit_data = State()
    waiting_package_sales_edit_days = State()
    waiting_package_assign_user = State()
    waiting_package_assign_custom = State()  # legacy fallback only
    waiting_package_custom_price = State()
    waiting_package_custom_max_subs = State()
    waiting_package_custom_description = State()
    waiting_package_custom_conditions = State()
    waiting_package_custom_code = State()
    waiting_job_interval = State()


# -----------------------------
# Keyboards
# -----------------------------

def has_visible_user_packages(telegram_id: Optional[int]) -> bool:
    if telegram_id is None:
        return False
    try:
        with closing(db.connect()) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS c FROM user_packages
                WHERE user_telegram_id = ?
                  AND status IN ('offered', 'pending_payment', 'active')
                """,
                (int(telegram_id),),
            ).fetchone()
            return bool(row and int(row["c"] or 0) > 0)
    except Exception:
        return False

def main_menu_kb(telegram_id: Optional[int] = None) -> ReplyKeyboardMarkup:
    service_row = [KeyboardButton(text="📦 سرویس‌های من")]
    # Users must never see assigned-package entry points unless an admin has
    # actually assigned at least one actionable package to them. Public services
    # and public plans remain visible for everyone.
    if has_visible_user_packages(telegram_id):
        service_row.append(KeyboardButton(text="🎁 پکیج‌های من"))
    rows = [
        [KeyboardButton(text="🛒 خرید سرویس")],
        service_row,
        [KeyboardButton(text="🎁 سرویس رایگان")],
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


def paid_plan_categories() -> list[tuple[str, str, str]]:
    used = {p.category for p in PLANS.values() if not str(p.category).startswith("free:")}
    items: list[tuple[int, str, str, str]] = []
    for key, meta in PLAN_CATEGORIES.items():
        if key in used and bool(meta.get("is_active", True)):
            items.append((int(meta.get("sort_order", 100)), key, str(meta.get("title") or key), str(meta.get("description") or "")))
    # If a plan uses a category that has no category row yet, still expose it.
    for key in sorted(used - set(PLAN_CATEGORIES.keys())):
        items.append((999, key, key, ""))
    items.sort(key=lambda item: (item[0], item[1]))
    return [(key, title, description) for _sort, key, title, description in items]


def buy_type_kb() -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    for key, title, _description in paid_plan_categories():
        rows.append([(title, f"buy_cat:{key}")])
    rows.append([("🎁 سرویس رایگان", "free_test_menu")])
    rows.append([("🏠 منوی اصلی", "home")])
    return inline(rows)


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


def order_payment_kb(
    order_id: int,
    payable: int,
    wallet_balance: int,
    back_callback: str = "buy",
    back_text: str = "⬅️ انتخاب پلن دیگر",
    *,
    allow_wallet: bool = True,
    allow_coupon: bool = True,
    is_wallet_topup: bool = False,
) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    if allow_coupon:
        rows.append([("🎟 کد تخفیف دارم", f"coupon_start:{order_id}")])
    if is_wallet_topup:
        rows.append([("💳 پرداخت", f"pay_methods:{order_id}")])
    elif allow_wallet and wallet_balance >= payable and wallet_balance >= 0:
        label = "✅ فعال‌سازی سفارش رایگان" if payable <= 0 else "💰 پرداخت و فعال‌سازی از کیف پول"
        rows.append([(label, f"pay_wallet:{order_id}")])
    rows.append([(back_text, back_callback), ("🏠 منوی اصلی", "home")])
    return inline(rows)


def payment_methods_kb(order_id: int) -> InlineKeyboardMarkup:
    return inline([
        [("💳 کارت‌به‌کارت", f"pay_card:{order_id}")],
        [("⬅️ بازگشت به بررسی پرداخت", f"pay_page:{order_id}"), ("🏠 منوی اصلی", "home")],
    ])


def wallet_shortfall(payable: int, wallet_balance: int) -> int:
    if wallet_balance < 0:
        return max(payable - wallet_balance, -wallet_balance, WALLET_MIN_TOPUP)
    return max(payable - wallet_balance, WALLET_MIN_TOPUP)


def insufficient_wallet_kb(order_id: int, suggested_amount: int, back_callback: str) -> InlineKeyboardMarkup:
    return inline([
        [(f"➕ شارژ پیشنهادی کیف پول: {fmt_money(suggested_amount)}", f"wallet_topup_for:{order_id}:{suggested_amount}")],
        [("💰 ورود به کیف پول", "wallet")],
        [("⬅️ بازگشت", back_callback), ("🏠 منوی اصلی", "home")],
    ])


def transactions_kb(orders: list[sqlite3.Row]) -> InlineKeyboardMarkup:
    try:
        expire_sqlite_payment_deadlines()
    except Exception:
        logger.exception("failed to cleanup transaction deadlines before rendering keyboard")
    rows: list[list[tuple[str, str]]] = []
    for order in orders[:20]:
        if str(order["status"]) in {"pending", "payment_rejected"} and str(order["plan_key"]).startswith("wallet_topup:"):
            receipt = get_receipt_by_order(int(order["id"]))
            if not receipt or (str(receipt["status"]) == "waiting_receipt" and not receipt_deadline_expired(receipt)):
                rows.append([(f"🧾 ثبت رسید تراکنش #{order['id']}", f"tx_receipt:{order['id']}")])
    rows.append([("💰 کیف پول", "wallet"), ("🏠 منوی اصلی", "home")])
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
    keyboard: list[list[InlineKeyboardButton]] = []
    link = subscription_link(service)
    if link:
        keyboard.append([InlineKeyboardButton(text="🌐 پنل اشتراکی", url=link)])
    if service["is_test"]:
        keyboard.append([InlineKeyboardButton(text="🔗 لینک کامل اشتراک", callback_data=f"sub_link:{sid}")])
        keyboard.append([InlineKeyboardButton(text="⬅️ سرویس‌های من", callback_data="my_services"), InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="home")])
        return InlineKeyboardMarkup(inline_keyboard=keyboard)
    keyboard.append([InlineKeyboardButton(text="🔗 لینک کامل اشتراک", callback_data=f"sub_link:{sid}"), InlineKeyboardButton(text="🔄 تغییر لینک", callback_data=f"revoke:{sid}")])
    keyboard.append([InlineKeyboardButton(text="♻️ تمدید سرویس", callback_data=f"renew_warn:{sid}"), InlineKeyboardButton(text="📈 افزایش حجم", callback_data=f"addon_menu:{sid}")])
    keyboard.append([InlineKeyboardButton(text="⚙️ تنظیمات اشتراک", callback_data=f"svc_settings:{sid}")])
    keyboard.append([InlineKeyboardButton(text="⬅️ سرویس‌های من", callback_data="my_services"), InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def addon_packages_kb(service_id: int) -> InlineKeyboardMarkup:
    rows = [[(f"📈 {pkg.title} — {fmt_money(pkg.price)}", f"addon_pkg:{service_id}:{pkg.key}")] for pkg in DATA_ADDON_PACKAGES.values()]
    rows.append([("⬅️ جزئیات سرویس", f"service:{service_id}"), ("🏠 منوی اصلی", "home")])
    return inline(rows)


def renew_type_kb(service_id: int) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    for key, title, _description in paid_plan_categories():
        rows.append([(title, f"renew_cat:{service_id}:{key}")])
    rows.append([("⬅️ جزئیات سرویس", f"service:{service_id}"), ("🏠 منوی اصلی", "home")])
    return inline(rows)


def renew_plans_kb(service_id: int, category: str) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    for p in PLANS.values():
        if p.category == category:
            rows.append([(f"♻️ {p.title} — {fmt_money(p.price)}", f"renew_plan:{service_id}:{p.key}")])
    rows.append([("⬅️ بازگشت", f"renew_menu:{service_id}"), ("🏠 منوی اصلی", "home")])
    return inline(rows)


def service_settings_kb(service: sqlite3.Row) -> InlineKeyboardMarkup:
    service_id = int(service["id"])
    return inline([
        [("✏️ تغییر نام اشتراک", f"rename:{service_id}")],
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
        [("🧾 سفارش‌ها", "adm_orders"), ("💳 روش‌های پرداخت", "adm_payments")],
        [("💰 کیف پول کاربران", "adm_wallet_start"), ("🎁 کد پکیج‌ها", "adm_packages")],
        [("🎫 تیکت‌ها", "adm_tickets"), ("🎟 کدهای تخفیف", "adm_coupons")],
        [("📢 پیام همگانی", "adm_broadcast"), ("📦 مدیریت پلن‌ها", "adm_plans")],
        [("📊 گزارش‌ها", "adm_reports"), ("🗄 بک‌آپ/ریستور", "adm_backup")],
        [("🔌 Pasarguard", "adm_pasarguard")],
    ]
    if admin_has(admin_id, "channels"):
        rows.append([("📣 عضویت اجباری", "adm_required_channels")])
    appearance_row: list[tuple[str, str]] = []
    if admin_has(admin_id, "appearance"):
        appearance_row.append(("🎨 تغییرات ظاهری ربات", "adm_texts"))
    if admin_has(admin_id, "*"):
        appearance_row.append(("🔒 قفل بات", "adm_bot_lock"))
    if appearance_row:
        rows.append(appearance_row)
    if admin_has(admin_id, "*"):
        rows.append([("⏱ مدیریت Jobها", "adm_jobs"), ("👮 مدیریت ادمین‌ها", "adm_admins")])
        rows.append([("📜 لاگ ادمین‌ها", "adm_logs")])
    rows.append([("🏠 منوی اصلی کاربر", "home")])
    return inline(rows)


def admin_back_kb(back: str = "adm_home") -> InlineKeyboardMarkup:
    return inline([[('⬅️ بازگشت', back), ('👑 منوی ادمین', 'adm_home')]])





def _job_minutes(row: sqlite3.Row) -> int:
    try:
        return max(1, int(row["interval_seconds"] or 60) // 60)
    except Exception:
        return 1


def admin_jobs_text() -> str:
    jobs = list_jobs()
    text = header("⏱ مدیریت Jobها")
    text += "این Jobها به صورت دوره‌ای اجرا می‌شوند و بعد از خاموشی/ری‌استارت بات هم catch-up دارند. یعنی اگر زمان اجرای Job در زمان خاموشی گذشته باشد، بعد از بالا آمدن بات اجرا می‌شود.\n\n"
    for job in jobs:
        status = "فعال ✅" if int(job["enabled"] or 0) else "غیرفعال ⛔"
        text += f"• <b>{h(job['title'])}</b> — {status}\n"
        text += f"  ⏱ فاصله اجرا: هر <b>{fmt_number(_job_minutes(job))}</b> دقیقه\n"
        text += f"  🕒 آخرین اجرا: <code>{h(fmt_jalali_datetime(job['last_run_at']) if job['last_run_at'] else 'ندارد')}</code>\n"
        text += f"  ⏭ اجرای بعدی: <code>{h(fmt_jalali_datetime(job['next_run_at']) if job['next_run_at'] else 'در اولین فرصت')}</code>\n"
        if job['last_summary']:
            text += f"  📊 آخرین نتیجه: <code>{h(job['last_summary'])}</code>\n"
        if job['last_error']:
            text += f"  ⚠️ خطا: <code>{h(job['last_error'])}</code>\n"
        text += "\n"
    return text


def admin_jobs_kb() -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    for job in list_jobs():
        icon = "✅" if int(job["enabled"] or 0) else "⛔"
        rows.append([(f"{icon} {job['title']}", f"adm_job_view:{job['job_key']}")])
    rows.append([("👑 منوی ادمین", "adm_home")])
    return inline(rows)


def admin_job_text(job: sqlite3.Row) -> str:
    text = header("⏱ جزئیات Job", job["title"])
    text += f"🔑 کلید: <code>{h(job['job_key'])}</code>\n"
    text += f"وضعیت: <b>{'فعال ✅' if int(job['enabled'] or 0) else 'غیرفعال ⛔'}</b>\n"
    text += f"⏱ فاصله اجرا: هر <b>{fmt_number(_job_minutes(job))}</b> دقیقه\n"
    text += f"🕒 آخرین اجرا: <code>{h(fmt_jalali_datetime(job['last_run_at']) if job['last_run_at'] else 'ندارد')}</code>\n"
    text += f"⏭ اجرای بعدی: <code>{h(fmt_jalali_datetime(job['next_run_at']) if job['next_run_at'] else 'در اولین فرصت')}</code>\n"
    if job['last_status']:
        text += f"📌 آخرین وضعیت: <code>{h(job['last_status'])}</code>\n"
    if job['last_summary']:
        text += f"📊 آخرین نتیجه: <code>{h(job['last_summary'])}</code>\n"
    if job['last_error']:
        text += f"⚠️ آخرین خطا: <code>{h(job['last_error'])}</code>\n"
    text += f"\n📝 توضیح:\n{h(job['description'])}"
    return text


def admin_job_kb(job_key: str, enabled: bool) -> InlineKeyboardMarkup:
    toggle_text = "⛔ غیرفعال کردن" if enabled else "✅ فعال کردن"
    return inline([
        [("▶️ اجرای دستی الان", f"adm_job_run:{job_key}"), ("⏱ تغییر فاصله", f"adm_job_interval:{job_key}")],
        [(toggle_text, f"adm_job_toggle:{job_key}")],
        [("⬅️ لیست Jobها", "adm_jobs"), ("👑 منوی ادمین", "adm_home")],
    ])

def admin_users_kb() -> InlineKeyboardMarkup:
    return inline([
        [("🔎 جستجوی کاربر", "adm_user_search")],
        [("🆕 آخرین کاربران", "adm_recent_users"), ("🛒 کاربران خریدار", "adm_buyer_users")],
        [("🗑 کاربران حذف‌شده", "adm_deleted_users")],
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
    if status == "deleted":
        return inline([
            [("♻️ بازگردانی کاربر", f"adm_user_restore:{uid}")],
            [("🧾 سفارش‌ها", f"adm_user_orders:{uid}"), ("📦 سرویس‌های کاربر", f"adm_user_services:{uid}")],
            [("🎁 پکیج‌های کاربر", f"adm_user_packages:{uid}"), ("📝 یادداشت ادمین", f"adm_user_note:{uid}")],
            [("⬅️ کاربران", "adm_users"), ("👑 منوی ادمین", "adm_home")],
        ])

    status_row = (
        [("✅ رفع بلاک با اطلاع", f"adm_user_unban_notify:{uid}"), ("✅ رفع بلاک بی‌صدا", f"adm_user_unban_silent:{uid}")]
        if status in {"banned", "locked"} else
        [("🚫 بلاک با اطلاع", f"adm_user_ban_notify:{uid}"), ("🚫 بلاک بی‌صدا", f"adm_user_ban_silent:{uid}")]
    )
    return inline([
        [("📦 سرویس‌های کاربر", f"adm_user_services:{uid}"), ("🧾 سفارش‌ها", f"adm_user_orders:{uid}")],
        [("💰 تغییر کیف پول", f"adm_user_wallet:{uid}"), ("✉️ پیام مستقیم", f"adm_user_msg:{uid}")],
        status_row,
        [("🗑 حذف کاربر", f"adm_user_delete:{uid}"), ("🎁 ریست تست رایگان", f"adm_user_reset_free:{uid}")],
        [("📝 یادداشت ادمین", f"adm_user_note:{uid}"), ("➕ ساخت سرویس دستی", f"adm_manual_service:{uid}")],
        [("🎁 اختصاص پکیج", f"adm_pkg_assign_user:{uid}"), ("🎁 پکیج‌های کاربر", f"adm_user_packages:{uid}")],
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
        [("🔄 Sync از Pasarguard", f"adm_svc_pull:{sid}")],
        [("➕ ۵ گیگ", f"adm_svc_data:{sid}:5"), ("➕ ۱۰ گیگ", f"adm_svc_data:{sid}:10"), ("➕ ۲۰ گیگ", f"adm_svc_data:{sid}:20")],
        [("⏳ ۷ روز", f"adm_svc_days:{sid}:7"), ("⏳ ۳۰ روز", f"adm_svc_days:{sid}:30"), ("🧹 ریست مصرف", f"adm_svc_reset:{sid}")],
        [("🗑 حذف سرویس", f"adm_svc_status:{sid}:deleted")],
        [("⬅️ سرویس‌های کاربر", f"adm_user_services:{uid}"), ("👑 منوی ادمین", "adm_home")],
    ])


def admin_orders_kb() -> InlineKeyboardMarkup:
    return inline([
        [("⏳ در انتظار پرداخت", "adm_orders_pending"), ("🧾 رسیدهای در انتظار", "adm_orders_receipts")],
        [("✅ پرداخت‌شده", "adm_orders_paid"), ("🧾 آخرین سفارش‌ها", "adm_orders_latest")],
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
    receipt = get_receipt_by_order(oid)
    if receipt and receipt["status"] == "receipt_pending":
        rows.append([("🖼 مشاهده رسید", f"adm_receipt_view:{receipt['id']}"), ("✅ تأیید رسید", f"adm_receipt_approve:{receipt['id']}")])
        rows.append([("❌ رد رسید", f"adm_receipt_reject:{receipt['id']}")])
    elif order["status"] not in {"paid", "rejected", "payment_rejected"}:
        rows.append([("✅ تأیید دستی پرداخت", f"adm_order_pay:{oid}")])
    rows.append([("👤 پروفایل کاربر", f"adm_user:{uid}"), ("🧾 سفارش‌های کاربر", f"adm_user_orders:{uid}")])
    rows.append([("⬅️ سفارش‌ها", "adm_orders"), ("👑 منوی ادمین", "adm_home")])
    return inline(rows)


def admin_coupon_kb() -> InlineKeyboardMarkup:
    return inline([
        [("➕ ساخت/ویرایش کد", "adm_coupon_add"), ("🧩 ویرایش شرایط کد", "adm_coupon_edit_conditions")],
        [("⛔ غیرفعال کردن کد", "adm_coupon_disable"), ("📋 کدهای فعال", "adm_coupon_list")],
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


def admin_role_select_kb(back: str = "adm_admins") -> InlineKeyboardMarkup:
    rows = [[(f"{role} — {ADMIN_ROLE_DESCRIPTIONS.get(role, '')[:28]}", f"adm_admin_role:{role}")] for role in ADMIN_ROLE_PERMISSIONS.keys()]
    rows.append([("⬅️ بازگشت", back), ("👑 منوی ادمین", "adm_home")])
    return inline(rows)


def admin_edit_role_select_kb(uid: int) -> InlineKeyboardMarkup:
    rows = [[(f"{role} — {ADMIN_ROLE_DESCRIPTIONS.get(role, '')[:28]}", f"adm_admin_edit_role:{uid}:{role}")] for role in ADMIN_ROLE_PERMISSIONS.keys()]
    rows.append([("⬅️ بازگشت", f"adm_admin_view:{uid}"), ("👑 منوی ادمین", "adm_home")])
    return inline(rows)


def admin_name_skip_kb() -> InlineKeyboardMarkup:
    return inline([
        [("استفاده از چت‌آیدی به عنوان نام", "adm_admin_name_skip")],
        [("⬅️ بازگشت", "adm_admin_add"), ("👑 منوی ادمین", "adm_home")],
    ])


def admin_list_kb(rows_data: list[sqlite3.Row]) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    for a in rows_data[:50]:
        status = "✅" if int(a["is_active"]) else "⛔"
        rows.append([(f"{status} {admin_display_name(a)} — {a['role']}", f"adm_admin_view:{a['telegram_id']}")])
    rows.append([("⬅️ بازگشت", "adm_admins"), ("👑 منوی ادمین", "adm_home")])
    return inline(rows)


def admin_detail_kb(admin_row: sqlite3.Row, viewer_id: int) -> InlineKeyboardMarkup:
    uid = int(admin_row["telegram_id"])
    active = int(admin_row["is_active"]) == 1
    rows: list[list[tuple[str, str]]] = [
        [("✏️ تغییر نام", f"adm_admin_edit_name:{uid}"), ("👮 تغییر نقش", f"adm_admin_change_role:{uid}")],
    ]
    if active:
        rows.append([("🗑 حذف/غیرفعال کردن ادمین", f"adm_admin_delete_ask:{uid}")])
    else:
        rows.append([("✅ فعال‌سازی دوباره", f"adm_admin_restore:{uid}")])
    rows.append([("⬅️ لیست ادمین‌ها", "adm_admin_list"), ("👑 منوی ادمین", "adm_home")])
    return inline(rows)


def admin_delete_confirm_kb(uid: int) -> InlineKeyboardMarkup:
    return inline([
        [("✅ بله، حذف/غیرفعال شود", f"adm_admin_delete_do:{uid}")],
        [("❌ لغو", f"adm_admin_view:{uid}"), ("👑 منوی ادمین", "adm_home")],
    ])


def coupon_scope_select_kb() -> InlineKeyboardMarkup:
    return inline([
        [("همه کاربران", "adm_coupon_scope:all")],
        [("فقط خرید اول", "adm_coupon_scope:first_purchase")],
        [("فقط کاربران مشخص", "adm_coupon_scope:users")],
        [("⬅️ بازگشت", "adm_coupons"), ("👑 منوی ادمین", "adm_home")],
    ])


def card_active_select_kb() -> InlineKeyboardMarkup:
    return inline([
        [("✅ فعال باشد", "adm_card_active:1"), ("⛔ غیرفعال باشد", "adm_card_active:0")],
        [("⬅️ بازگشت", "adm_payments"), ("👑 منوی ادمین", "adm_home")],
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
    if status == "locked":
        status = "banned"
    username = f"@{user['username']}" if user["username"] else "ثبت نشده"
    note = user["admin_note"] if row_has(user, "admin_note") and user["admin_note"] else "ندارد"
    return (
        header("👤 پروفایل کاربر", str(user["telegram_id"]))
        + f"👤 نام کاربری: <b>{h(username)}</b>\n"
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
        + (f"🔌 Pasarguard: <code>{h(service['pasarguard_username'])}</code> | <b>{h(service['pasarguard_sync_status'] or 'local')}</b>\n" if 'pasarguard_username' in service.keys() and service['pasarguard_username'] else "")
        + (f"⚠️ خطای sync: <code>{h(service['pasarguard_sync_error'])}</code>\n" if 'pasarguard_sync_error' in service.keys() and service['pasarguard_sync_error'] else "")
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
        + payment_receipt_summary_for_order(int(order["id"]))
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
    meta = PLAN_CATEGORIES.get(category, {})
    title = str(meta.get("title") or category)
    description = str(meta.get("description") or "پلن موردنظر را انتخاب کنید")
    return header(title, description) + "یکی از پلن‌های زیر را انتخاب کنید:"


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
    prefix_rule = f"• ربات خودش ابتدای نام را <code>{SERVICE_NAME_PREFIX}</code> می‌گذارد\n" if SERVICE_NAME_PREFIX else "• پیشوند داخلی برای نام سرویس غیرفعال است\n"
    text += (
        "یک نام دلخواه برای اشتراک وارد کنید یا دکمه ساخت خودکار را بزنید.\n\n"
        f"قانون نام‌گذاری:\n"
        + prefix_rule +
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
    text += "پرداخت مستقیم حذف شده است. اگر موجودی کیف پول کافی باشد، سفارش از کیف پول فعال می‌شود؛ اگر کافی نباشد باید ابتدا کیف پول را شارژ کنید."
    return text


def render_order_payment_text(order: sqlite3.Row, user: sqlite3.Row) -> tuple[str, str, int]:
    plan_key = str(order["plan_key"])
    amount = int(order["amount"])
    discount = int(order["discount_amount"])
    payable = max(amount - discount, 0)
    details = order_discount_details(int(order["id"]), order)

    if plan_key.startswith("wallet_topup:"):
        text = header("💳 بررسی شارژ کیف پول", f"سفارش #{order['id']}")
        text += f"💰 مبلغ شارژ کیف پول: <b>{fmt_money(amount)}</b>\n"
        text += discount_lines(details)
        text += f"✅ قابل پرداخت: <b>{fmt_money(payable)}</b>\n\n"
        text += "اگر کد تخفیف دارید، ابتدا آن را اعمال کنید. سپس دکمه پرداخت را بزنید و در مرحله بعد روش پرداخت را انتخاب کنید."
        return text, "wallet", payable

    if plan_key.startswith("pkg_assign:"):
        user_package_id = int(plan_key.split(":", 1)[1])
        user_package = user_package_by_id(user_package_id, int(user["telegram_id"]))
        text = header("💳 پرداخت پکیج اختصاصی", f"سفارش #{order['id']}")
        if user_package:
            text += user_package_text(user_package) + "\n"
        text += f"✅ قابل پرداخت: <b>{fmt_money(payable)}</b>\n"
        text += f"💼 موجودی کیف پول: <b>{fmt_money(int(user['wallet_balance']))}</b>\n\n"
        text += "این سفارش کد تخفیف ندارد و فقط از کیف پول پرداخت می‌شود."
        return text, f"pkg_view:{user_package_id}", payable

    if plan_key.startswith("pkg_sub:"):
        _tag, user_package_id_s, item_id_s = plan_key.split(":")
        user_package_id = int(user_package_id_s)
        item = package_item_by_id(int(item_id_s))
        text = header("💳 ساخت ساب از پکیج", f"سفارش #{order['id']}")
        if item:
            text += f"📦 ساب انتخابی: <b>{h(item['title'])}</b>\n"
            text += f"📊 حجم: <b>{fmt_number(float(item['data_gb']))} گیگابایت</b>\n"
            text += f"⏳ اعتبار: <b>{fmt_number(int(item['days']))} روز</b>\n"
        text += f"✅ قابل پرداخت: <b>{fmt_money(payable)}</b>\n"
        text += f"💼 موجودی کیف پول: <b>{fmt_money(int(user['wallet_balance']))}</b>\n\n"
        text += "این سفارش کد تخفیف ندارد و فقط از کیف پول پرداخت می‌شود."
        return text, f"pkg_view:{user_package_id}", payable

    if plan_key.startswith("addon:"):
        _, pkg_key, service_id_s = plan_key.split(":")
        service = db.get_service(int(service_id_s), int(user["telegram_id"]))
        pkg = DATA_ADDON_PACKAGES[pkg_key]
        text = header("💳 پرداخت افزایش حجم", service["name"] if service else "سرویس")
        text += f"📈 بسته انتخابی: <b>{h(pkg.title)}</b>\n📊 حجم اضافه: <b>{fmt_number(pkg.data_gb)} گیگابایت</b>\n⏳ افزایش زمان: <b>ندارد</b>\n💰 مبلغ اصلی: <b>{fmt_money(amount)}</b>\n"
        text += discount_lines(details)
        text += f"✅ قابل پرداخت: <b>{fmt_money(payable)}</b>\n💼 موجودی کیف پول: <b>{fmt_money(int(user['wallet_balance']))}</b>\n\nدر حال حاضر روش فعال پرداخت، کارت‌به‌کارت است. بعد از ارسال رسید، سفارش شما برای ادمین فروش ارسال می‌شود و پس از تأیید، سرویس فعال خواهد شد."
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
    status_map = {
        "active": "فعال ✅",
        "suspended": "غیرفعال ⛔",
        "locked": "قفل‌شده 🔒",
        "provisioning": "در حال فعال‌سازی 🔄",
        "provisioning_failed": "فعال‌سازی ناموفق ⚠️",
        "deleted": "حذف‌شده 🗑",
    }
    status_label = status_map.get(str(service["status"] or ""), h(service["status"]))
    type_label = "\n🎁 نوع: <b>سرویس رایگان</b>" if service["is_test"] else ""
    link_block = (
        f"🔗 لینک کامل اشتراک:\n<code>{h(link)}</code>\n\n"
        f'📊 برای بررسی وضعیت سرویس، حجم مصرفی و زمان باقی‌مانده، از این بخش استفاده کنید:\n<a href="{h(link)}">پنل اشتراکی</a> ❗️\n\n'
        if link else
        "🔗 لینک اشتراک:\n<code>در انتظار آماده‌سازی لینک اشتراک</code>\n\n"
    )
    error_line = (
        "⚠️ فعال‌سازی سرویس کامل نشده است. لطفاً از بخش پشتیبانی یک تیکت ثبت کنید و مشکل را کامل توضیح دهید.\n\n"
        if 'pasarguard_sync_error' in service.keys() and service['pasarguard_sync_error'] else ""
    )
    return (
        header("📦 جزئیات سرویس", service["name"])
        + f"🟢 وضعیت: <b>{status_label}</b>{type_label}\n"
        + f"🏷 پلن: <b>{h(service['plan_title'])}</b>\n"
        + f"📊 حجم باقی‌مانده: <b>{fmt_number(round(left_gb, 2))} گیگابایت</b>\n"
        + f"⏳ زمان باقی‌مانده: <b>{fmt_number(days_left)} روز</b>\n"
        + f"💳 مبلغ پرداختی: <b>{fmt_money(int(service['paid_amount']))}</b>\n\n"
        + error_line
        + link_block
        + "برای مدیریت سرویس از دکمه‌های زیر استفاده کنید."
    )


def sub_link_text(service: sqlite3.Row) -> str:
    link = subscription_link(service)
    if not link:
        return header("🔗 لینک اشتراک", service["name"]) + "هنوز لینک اشتراک برای این سرویس هنوز ثبت نشده است. لطفاً از جزئیات سرویس گزینه sync را بزنید یا با پشتیبانی تماس بگیرید."
    return (
        header("🔗 لینک کامل اشتراک", service["name"])
        + f"<code>{h(link)}</code>\n\n"
        + "برای کپی کردن، روی لینک بالا لمس کنید.\n\n"
        + f'📊 وضعیت سرویس، حجم مصرفی و زمان باقی‌مانده را می‌توانید از اینجا ببینید:\n<a href="{h(link)}">پنل اشتراکی</a> ❗️'
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


def wallet_amount_prompt(suggested_amount: int | None = None) -> str:
    text = header("➕ افزایش موجودی کیف پول")
    text += "شما در این مرحله <b>کیف پول خودتان را شارژ می‌کنید</b>. بعد از تأیید رسید توسط ادمین فروش، موجودی به کیف پول اضافه می‌شود و سپس می‌توانید خرید/تمدید/افزایش حجم را از کیف پول انجام دهید.\n\n"
    text += f"حداقل مبلغ واریز: <b>{fmt_money(WALLET_MIN_TOPUP)}</b>\n"
    if suggested_amount and suggested_amount > 0:
        text += f"مبلغ پیشنهادی برای سفارش انتخاب‌شده: <b>{fmt_money(suggested_amount)}</b>\n"
    text += "\nمبلغ دلخواه را به تومان وارد کنید. مثلاً: <code>100000</code>"
    return text


# -----------------------------
# Package-code / assigned packages
# -----------------------------
def package_plan_key(item_id: int) -> str:
    return f"pkg_item_{int(item_id)}"


def random_package_code(prefix: str = "PKG") -> str:
    return f"{prefix}_{secrets.token_hex(3).upper()}"


def validate_package_name(value: str) -> tuple[bool, str]:
    value = (value or "").strip()
    if len(value) < 3 or len(value) > 80:
        return False, "نام پکیج باید بین ۳ تا ۸۰ کاراکتر باشد."
    return True, value


def validate_package_code(value: str) -> tuple[bool, str]:
    code = normalize_digits(value or "").strip().upper()
    code = re.sub(r"\s+", "", code)
    if not re.fullmatch(r"[A-Z0-9_-]{3,32}", code):
        return False, "کد پکیج باید ۳ تا ۳۲ کاراکتر و فقط شامل حروف انگلیسی، عدد، خط تیره یا آندرلاین باشد."
    with closing(db.connect()) as conn:
        exists = conn.execute("SELECT id FROM package_templates WHERE code = ?", (code,)).fetchone()
    if exists:
        return False, "این کد قبلاً برای یک پکیج ثبت شده است."
    return True, code


def parse_positive_amount(value: str, *, allow_zero: bool = True) -> tuple[bool, int, str]:
    amount = parse_amount(value or "")
    if amount is None:
        return False, 0, "مبلغ معتبر نیست. فقط عدد به تومان وارد کنید."
    if amount < 0 or (amount == 0 and not allow_zero):
        return False, 0, "مبلغ باید مثبت باشد."
    return True, int(amount), ""


def validate_long_text(value: str, field: str, *, max_len: int = 1200, allow_empty: bool = True) -> tuple[bool, str]:
    value = (value or "").strip()
    if value == "-" and allow_empty:
        value = ""
    if not value and not allow_empty:
        return False, f"{field} نمی‌تواند خالی باشد."
    if len(value) > max_len:
        return False, f"{field} نباید بیشتر از {max_len} کاراکتر باشد."
    return True, value


def parse_max_subscriptions(value: str) -> tuple[bool, int, str]:
    raw = normalize_digits(value or "").strip()
    if not raw.isdigit():
        return False, 0, "تعداد ساب باید عدد صحیح باشد."
    count = int(raw)
    if count < 1 or count > 100:
        return False, 0, "تعداد ساب باید بین ۱ تا ۱۰۰ باشد."
    return True, count, ""


def validate_package_item_title(value: str) -> tuple[bool, str]:
    title = (value or "").strip()
    if len(title) < 2 or len(title) > 80:
        return False, "عنوان ساب باید بین ۲ تا ۸۰ کاراکتر باشد."
    return True, title


def parse_package_data_gb(value: str) -> tuple[bool, float, str]:
    raw = normalize_digits(value or "").strip().replace(",", ".")
    try:
        data_gb = float(raw)
    except Exception:
        return False, 0.0, "حجم باید عدد معتبر باشد؛ مثلا 30 یا 1.5"
    if data_gb <= 0 or data_gb > 10000:
        return False, 0.0, "حجم باید بزرگ‌تر از صفر و حداکثر ۱۰۰۰۰ گیگ باشد."
    return True, data_gb, ""


def parse_package_days(value: str) -> tuple[bool, int, str]:
    raw = normalize_digits(value or "").strip()
    if not raw.isdigit():
        return False, 0, "روز اعتبار باید عدد صحیح باشد."
    days = int(raw)
    if days <= 0 or days > 3650:
        return False, 0, "روز اعتبار باید بین ۱ تا ۳۶۵۰ باشد."
    return True, days, ""


def validate_user_package_code(value: str, user_package_id: int) -> tuple[bool, str]:
    code = re.sub(r"\s+", "", normalize_digits(value or "").strip().upper())
    if not re.fullmatch(r"[A-Z0-9_-]{3,40}", code):
        return False, "کد اختصاصی باید ۳ تا ۴۰ کاراکتر و فقط شامل حروف انگلیسی، عدد، خط تیره یا آندرلاین باشد."
    with closing(db.connect()) as conn:
        exists = conn.execute("SELECT id FROM user_packages WHERE code = ? AND id != ?", (code, user_package_id)).fetchone()
    if exists:
        return False, "این کد اختصاصی قبلاً برای کاربر دیگری ثبت شده است."
    return True, code


def package_item_preview_text(item: dict[str, Any], *, title: str = "👀 پیش‌نمایش ساب داخل پکیج") -> str:
    return (
        header(title)
        + f"🧩 عنوان ساب: <b>{h(item.get('title'))}</b>\n"
        + f"📦 حجم: <b>{fmt_number(float(item.get('data_gb', 0)))}</b> گیگ\n"
        + f"⏳ مدت: <b>{fmt_number(int(item.get('days', 0)))}</b> روز\n"
        + f"💰 قیمت ساب: <b>{'رایگان' if int(item.get('price', 0)) == 0 else fmt_money(int(item.get('price', 0)))}</b>\n\n"
        + "در صورت نیاز یکی از فیلدها را با دکمه‌های زیر ویرایش کنید؛ در غیر این صورت ثبت آیتم را بزنید."
    )


# Package item creation is handled step-by-step with buttons; no multi-field pipe input is used.


def package_by_id(package_id: int) -> Optional[sqlite3.Row]:
    with closing(db.connect()) as conn:
        return conn.execute("SELECT * FROM package_templates WHERE id = ?", (package_id,)).fetchone()


def package_item_by_id(item_id: int) -> Optional[sqlite3.Row]:
    with closing(db.connect()) as conn:
        return conn.execute("SELECT * FROM package_template_items WHERE id = ?", (item_id,)).fetchone()


def package_items(package_id: int) -> list[sqlite3.Row]:
    with closing(db.connect()) as conn:
        return list(conn.execute("SELECT * FROM package_template_items WHERE package_id = ? ORDER BY sort_order, id", (package_id,)).fetchall())


def list_package_templates(active_only: bool = False, limit: int = 50) -> list[sqlite3.Row]:
    with closing(db.connect()) as conn:
        where = "WHERE is_active = 1" if active_only else ""
        return list(conn.execute(f"SELECT * FROM package_templates {where} ORDER BY id DESC LIMIT ?", (limit,)).fetchall())


def user_package_by_id(user_package_id: int, telegram_id: int | None = None) -> Optional[sqlite3.Row]:
    with closing(db.connect()) as conn:
        if telegram_id is None:
            return conn.execute("SELECT * FROM user_packages WHERE id = ?", (user_package_id,)).fetchone()
        return conn.execute("SELECT * FROM user_packages WHERE id = ? AND user_telegram_id = ?", (user_package_id, telegram_id)).fetchone()


def list_user_packages(telegram_id: int, include_old: bool = False) -> list[sqlite3.Row]:
    with closing(db.connect()) as conn:
        if include_old:
            return list(conn.execute("SELECT * FROM user_packages WHERE user_telegram_id = ? ORDER BY id DESC", (telegram_id,)).fetchall())
        return list(conn.execute("SELECT * FROM user_packages WHERE user_telegram_id = ? AND status IN ('offered','pending_payment','active') ORDER BY id DESC", (telegram_id,)).fetchall())


def count_package_subscriptions(user_package_id: int) -> int:
    with closing(db.connect()) as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM package_subscriptions WHERE user_package_id = ?", (user_package_id,)).fetchone()
        return int(row["c"] if row else 0)


def list_package_subscriptions(user_package_id: int) -> list[sqlite3.Row]:
    with closing(db.connect()) as conn:
        return list(conn.execute("SELECT * FROM package_subscriptions WHERE user_package_id = ? ORDER BY id DESC", (user_package_id,)).fetchall())


def list_user_package_assignments(package_id: int | None = None, user_telegram_id: int | None = None, limit: int = 50) -> list[sqlite3.Row]:
    """List package assignments for admin management.

    This is an admin-only view and intentionally includes revoked/cancelled/declined
    assignments too, so support can audit and manage old packages.
    """
    where: list[str] = []
    params: list[Any] = []
    if package_id is not None:
        where.append("package_id = ?")
        params.append(int(package_id))
    if user_telegram_id is not None:
        where.append("user_telegram_id = ?")
        params.append(int(user_telegram_id))
    sql = "SELECT * FROM user_packages"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    with closing(db.connect()) as conn:
        return list(conn.execute(sql, tuple(params)).fetchall())


def package_subscription_details(user_package_id: int) -> list[tuple[sqlite3.Row, Optional[sqlite3.Row], Optional[sqlite3.Row]]]:
    """Return subscription rows with their service and package item for admin pages."""
    details: list[tuple[sqlite3.Row, Optional[sqlite3.Row], Optional[sqlite3.Row]]] = []
    for sub in list_package_subscriptions(user_package_id):
        service = db.get_service(int(sub["service_id"]))
        item = package_item_by_id(int(sub["package_item_id"]))
        details.append((sub, service, item))
    return details


def user_package_status_label(status: str) -> str:
    return {
        "draft": "پیش‌نویس",
        "offered": "پیشنهاد شده",
        "pending_payment": "در انتظار پرداخت",
        "active": "فعال",
        "declined": "رد شده توسط کاربر",
        "cancelled": "لغوشده",
        "revoked": "پس‌گرفته‌شده از کاربر",
    }.get(str(status), str(status))


def revoke_user_package_local(user_package_id: int, *, delete_subscriptions: bool, admin_id: int) -> list[sqlite3.Row]:
    """Revoke an assigned package. If requested, mark its created services as deleted.

    Services are not physically removed from the database; they are marked deleted so
    audit/backup remains safe and Pasarguard can be disabled by the caller.
    """
    affected_services: list[sqlite3.Row] = []
    with closing(db.connect()) as conn:
        up = conn.execute("SELECT * FROM user_packages WHERE id = ?", (user_package_id,)).fetchone()
        if not up:
            return []
        conn.execute(
            "UPDATE user_packages SET status = 'revoked', updated_at = ? WHERE id = ?",
            (now_iso(), user_package_id),
        )
        if up["order_id"]:
            conn.execute(
                "UPDATE orders SET status = 'cancelled', admin_note = COALESCE(admin_note, ?) WHERE id = ? AND status != 'paid'",
                (f"package revoked by admin {admin_id}", int(up["order_id"])),
            )
        if delete_subscriptions:
            rows = list(conn.execute("SELECT service_id FROM package_subscriptions WHERE user_package_id = ?", (user_package_id,)).fetchall())
            for row in rows:
                service = conn.execute("SELECT * FROM services WHERE id = ?", (int(row["service_id"]),)).fetchone()
                if service:
                    affected_services.append(service)
                    conn.execute(
                        "UPDATE services SET status = 'deleted', locked_reason = ? WHERE id = ?",
                        (f"package revoked by admin {admin_id}", int(service["id"])),
                    )
        conn.commit()
    return affected_services


def create_package_template(data: dict[str, Any], items: list[dict[str, Any]], admin_id: int) -> int:
    with closing(db.connect()) as conn:
        cur = conn.execute(
            """
            INSERT INTO package_templates (code, name, price, description, conditions, max_subscriptions, is_active, created_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (data["code"], data["name"], int(data["price"]), data.get("description") or "", data.get("conditions") or "", int(data["max_subscriptions"]), admin_id, now_iso(), now_iso()),
        )
        package_id = int(cur.lastrowid)
        for idx, item in enumerate(items, start=1):
            conn.execute(
                """
                INSERT INTO package_template_items (package_id, source_type, source_plan_key, item_key, title, data_gb, days, price, sort_order, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (package_id, item.get("source_type") or "manual", item.get("source_plan_key") or "", f"pkg{package_id}_{idx}", item["title"], float(item["data_gb"]), int(item["days"]), int(item["price"]), idx * 10, now_iso()),
            )
        conn.commit()
    return package_id


def create_user_package_assignment(package_id: int, telegram_id: int, admin_id: int, *, price: int | None = None, description: str | None = None, conditions: str | None = None, max_subscriptions: int | None = None, code: str | None = None, status: str = "draft") -> int:
    package = package_by_id(package_id)
    if not package:
        raise ValueError("package not found")
    assignment_code = (code or f"{package['code']}-{telegram_id % 100000}-{secrets.token_hex(2).upper()}").upper()
    with closing(db.connect()) as conn:
        cur = conn.execute(
            """
            INSERT INTO user_packages (user_telegram_id, package_id, code, assigned_by, price, description, conditions, max_subscriptions, used_subscriptions, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                telegram_id,
                package_id,
                assignment_code,
                admin_id,
                int(package["price"] if price is None else price),
                package["description"] if description is None else description,
                package["conditions"] if conditions is None else conditions,
                int(package["max_subscriptions"] if max_subscriptions is None else max_subscriptions),
                status,
                now_iso(),
                now_iso(),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def update_user_package(user_package_id: int, **values: Any) -> None:
    allowed = {"price", "description", "conditions", "max_subscriptions", "status", "order_id", "offered_at", "purchased_at", "used_subscriptions", "code"}
    keys = [k for k in values.keys() if k in allowed]
    if not keys:
        return
    assignments = ", ".join(f"{k} = ?" for k in keys) + ", updated_at = ?"
    params = [values[k] for k in keys] + [now_iso(), user_package_id]
    with closing(db.connect()) as conn:
        conn.execute(f"UPDATE user_packages SET {assignments} WHERE id = ?", params)
        conn.commit()


def mark_user_package_active(user_package_id: int, order_id: int | None = None) -> None:
    update_user_package(user_package_id, status="active", order_id=order_id, purchased_at=now_iso())


def record_package_subscription(user_package_id: int, item_id: int, service_id: int, telegram_id: int, order_id: int | None) -> None:
    with closing(db.connect()) as conn:
        conn.execute(
            "INSERT INTO package_subscriptions (user_package_id, package_item_id, service_id, user_telegram_id, order_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_package_id, item_id, service_id, telegram_id, order_id, now_iso()),
        )
        conn.execute("UPDATE user_packages SET used_subscriptions = used_subscriptions + 1, updated_at = ? WHERE id = ?", (now_iso(), user_package_id))
        conn.execute("UPDATE services SET package_assignment_id = ?, package_item_id = ? WHERE id = ?", (user_package_id, item_id, service_id))
        conn.commit()


def package_text(package: sqlite3.Row, *, include_items: bool = True) -> str:
    items = package_items(int(package["id"])) if include_items else []
    text = header("🎁 پکیج", package["name"])
    text += f"🧾 کد پکیج: <code>{h(package['code'])}</code>\n"
    text += f"💰 قیمت پک: <b>{'رایگان' if int(package['price']) == 0 else fmt_money(int(package['price']))}</b>\n"
    text += f"🔢 تعداد ساب قابل ساخت: <b>{fmt_number(int(package['max_subscriptions']))}</b>\n"
    if package["description"]:
        text += f"\n📝 توضیحات:\n{h(package['description'])}\n"
    if package["conditions"]:
        text += f"\n📌 شرایط:\n{h(package['conditions'])}\n"
    if items:
        text += "\n📦 پلن‌های داخل پک:\n"
        for item in items:
            text += f"• {h(item['title'])} — {fmt_number(float(item['data_gb']))}GB / {fmt_number(int(item['days']))} روز — ساب: {'رایگان' if int(item['price']) == 0 else fmt_money(int(item['price']))}\n"
    return text


def user_package_text(user_package: sqlite3.Row, *, user_view: bool = True) -> str:
    package = package_by_id(int(user_package["package_id"]))
    items = package_items(int(user_package["package_id"])) if package else []
    title = package["name"] if package else f"پکیج #{user_package['package_id']}"
    text = header("🎁 پکیج اختصاصی شما" if user_view else "🎁 پکیج اختصاصی کاربر", title)
    text += f"🧾 کد اختصاصی: <code>{h(user_package['code'])}</code>\n"
    text += f"📌 وضعیت: <b>{h(user_package_status_label(str(user_package['status'])))}</b>\n"
    text += f"💰 قیمت پک: <b>{'رایگان' if int(user_package['price']) == 0 else fmt_money(int(user_package['price']))}</b>\n"
    text += f"🔢 تعداد ساب قابل ساخت: <b>{fmt_number(int(user_package['max_subscriptions']))}</b>\n"
    text += f"✅ ساب‌های ساخته‌شده: <b>{fmt_number(count_package_subscriptions(int(user_package['id'])))}</b>\n"
    if user_package["description"]:
        text += f"\n📝 توضیحات:\n{h(user_package['description'])}\n"
    if user_package["conditions"]:
        text += f"\n📌 شرایط:\n{h(user_package['conditions'])}\n"
    if items:
        text += "\n📦 پلن‌هایی که بعد از خرید پک می‌توانید از آن‌ها ساب بسازید:\n"
        for item in items:
            text += f"• {h(item['title'])} — {fmt_number(float(item['data_gb']))}GB / {fmt_number(int(item['days']))} روز — قیمت ساب: {'رایگان' if int(item['price']) == 0 else fmt_money(int(item['price']))}\n"
    return text


def admin_packages_kb() -> InlineKeyboardMarkup:
    return inline([
        [("➕ ساخت پکیج جدید", "adm_pkg_new"), ("📋 لیست پکیج‌ها", "adm_pkg_list")],
        [("🎯 اختصاص پکیج به کاربر", "adm_pkg_assign_start")],
        [("👥 همه پکیج‌های اختصاصی", "adm_userpkg_all")],
        [("👑 منوی ادمین", "adm_home")],
    ])


def admin_package_list_kb(packages: list[sqlite3.Row], back: str = "adm_packages") -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    for pkg in packages[:30]:
        active = "✅" if int(pkg["is_active"]) else "⛔"
        rows.append([(f"{active} {pkg['name']} | {pkg['code']}", f"adm_pkg:{pkg['id']}")])
    rows.append([("⬅️ بازگشت", back), ("👑 منوی ادمین", "adm_home")])
    return inline(rows)


def admin_package_assign_select_kb(packages: list[sqlite3.Row], back: str = "adm_packages") -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    for pkg in packages[:30]:
        rows.append([(f"🎁 {pkg['name']} | {pkg['code']}", f"adm_pkg_assign_pick:{pkg['id']}")])
    rows.append([("⬅️ بازگشت", back), ("👑 منوی ادمین", "adm_home")])
    return inline(rows)


def admin_package_kb(package_id: int) -> InlineKeyboardMarkup:
    package = package_by_id(package_id)
    toggle = "⛔ غیرفعال کردن" if package and int(package["is_active"]) else "✅ فعال کردن"
    return inline([
        [("🎯 اختصاص به کاربر", f"adm_pkg_assign:{package_id}"), (toggle, f"adm_pkg_toggle:{package_id}")],
        [("👥 اختصاص‌های این پکیج", f"adm_pkg_assignments:{package_id}")],
        [("⬅️ لیست پکیج‌ها", "adm_pkg_list"), ("👑 منوی ادمین", "adm_home")],
    ])


def package_item_add_kb() -> InlineKeyboardMarkup:
    return inline([
        [("➕ انتخاب از پلن‌های فروش", "adm_pkg_item_sales")],
        [("✍️ اضافه کردن دستی", "adm_pkg_item_manual")],
        [("✅ پایان و ذخیره پکیج", "adm_pkg_item_finish")],
        [("❌ لغو", "adm_packages"), ("👑 منوی ادمین", "adm_home")],
    ])


def package_sales_plan_kb() -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    for p in PLANS.values():
        if not str(p.category).startswith("free:"):
            rows.append([(f"{p.title} — {fmt_money(p.price)}", f"adm_pkg_pick_sales:{p.key}")])
    rows.append([("⬅️ بازگشت", "adm_pkg_add_items"), ("👑 منوی ادمین", "adm_home")])
    return inline(rows)


def admin_package_manual_item_review_kb() -> InlineKeyboardMarkup:
    return inline([
        [("✅ ثبت این آیتم", "adm_pkg_manual_save")],
        [("✏️ تغییر عنوان", "adm_pkg_manual_edit:title"), ("📦 تغییر حجم", "adm_pkg_manual_edit:data")],
        [("⏳ تغییر مدت", "adm_pkg_manual_edit:days"), ("💰 تغییر قیمت", "adm_pkg_manual_edit:price")],
        [("⬅️ افزودن آیتم‌ها", "adm_pkg_add_items"), ("👑 منوی ادمین", "adm_home")],
    ])


def admin_package_sales_item_review_kb() -> InlineKeyboardMarkup:
    return inline([
        [("✅ ثبت این آیتم", "adm_pkg_sales_save")],
        [("✏️ تغییر عنوان", "adm_pkg_sales_edit:title"), ("📦 تغییر حجم", "adm_pkg_sales_edit:data")],
        [("⏳ تغییر مدت", "adm_pkg_sales_edit:days"), ("💰 تغییر قیمت", "adm_pkg_sales_edit:price")],
        [("🔄 بازگردانی به مشخصات پلن پایه", "adm_pkg_sales_reset")],
        [("⬅️ افزودن آیتم‌ها", "adm_pkg_add_items"), ("👑 منوی ادمین", "adm_home")],
    ])


def admin_user_package_custom_kb(user_package_id: int) -> InlineKeyboardMarkup:
    return inline([
        [("💰 تغییر قیمت پک", f"adm_userpkg_edit:{user_package_id}:price"), ("🔢 تغییر تعداد ساب", f"adm_userpkg_edit:{user_package_id}:max_subs")],
        [("📝 تغییر توضیح", f"adm_userpkg_edit:{user_package_id}:description"), ("📌 تغییر شرایط", f"adm_userpkg_edit:{user_package_id}:conditions")],
        [("🧾 تغییر کد اختصاصی", f"adm_userpkg_edit:{user_package_id}:code")],
        [("↩️ پیش‌نمایش", f"adm_userpkg_preview:{user_package_id}"), ("👑 منوی ادمین", "adm_home")],
    ])


def admin_package_assignment_preview_kb(user_package_id: int) -> InlineKeyboardMarkup:
    return inline([
        [("✅ ارسال پیشنهاد برای کاربر", f"adm_userpkg_send:{user_package_id}")],
        [("✏️ کاستوم برای این کاربر", f"adm_userpkg_custom:{user_package_id}")],
        [("❌ لغو اختصاص", f"adm_userpkg_cancel:{user_package_id}"), ("👑 منوی ادمین", "adm_home")],
    ])


def admin_user_package_manage_kb(user_package: sqlite3.Row) -> InlineKeyboardMarkup:
    upid = int(user_package["id"])
    uid = int(user_package["user_telegram_id"])
    package_id = int(user_package["package_id"])
    status = str(user_package["status"])
    rows: list[list[tuple[str, str]]] = []
    if status == "draft":
        rows.append([("✅ ارسال پیشنهاد برای کاربر", f"adm_userpkg_send:{upid}")])
        rows.append([("✏️ کاستوم برای این کاربر", f"adm_userpkg_custom:{upid}")])
        rows.append([("❌ لغو پیش‌نویس", f"adm_userpkg_cancel:{upid}")])
    rows.append([("🧩 ساب‌های این پکیج", f"adm_userpkg_subs:{upid}"), ("👤 پروفایل کاربر", f"adm_user:{uid}")])
    if status in {"offered", "pending_payment", "active"}:
        rows.append([("🚫 گرفتن پک با اطلاع", f"adm_userpkg_revoke:{upid}:keep:notify")])
        rows.append([("🚫 گرفتن پک بی‌صدا", f"adm_userpkg_revoke:{upid}:keep:silent")])
        rows.append([("🗑 گرفتن پک + حذف ساب‌ها با اطلاع", f"adm_userpkg_revoke:{upid}:delete:notify")])
        rows.append([("🗑 گرفتن پک + حذف ساب‌ها بی‌صدا", f"adm_userpkg_revoke:{upid}:delete:silent")])
    rows.append([("⬅️ اختصاص‌های این پکیج", f"adm_pkg_assignments:{package_id}"), ("👑 منوی ادمین", "adm_home")])
    return inline(rows)


def admin_user_package_list_kb(assignments: list[sqlite3.Row], back: str = "adm_packages") -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    icon_map = {
        "draft": "📝",
        "offered": "🆕",
        "pending_payment": "💳",
        "active": "✅",
        "declined": "❌",
        "cancelled": "🚫",
        "revoked": "⛔",
    }
    for up in assignments[:50]:
        package = package_by_id(int(up["package_id"]))
        title = package["name"] if package else f"پکیج #{up['package_id']}"
        icon = icon_map.get(str(up["status"]), "🎁")
        label = f"{icon} #{up['id']} | {title} | کاربر {up['user_telegram_id']}"
        rows.append([(label[:64], f"adm_userpkg:{up['id']}")])
    rows.append([("⬅️ بازگشت", back), ("👑 منوی ادمین", "adm_home")])
    return inline(rows)


def admin_user_package_subs_kb(user_package_id: int, details: list[tuple[sqlite3.Row, Optional[sqlite3.Row], Optional[sqlite3.Row]]]) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    for sub, service, item in details[:50]:
        if service:
            title = item["title"] if item else service["name"]
            rows.append([(f"📦 سرویس #{service['id']} | {title} | {service['status']}"[:64], f"adm_service:{service['id']}")])
    rows.append([("⬅️ مدیریت پکیج اختصاصی", f"adm_userpkg:{user_package_id}"), ("👑 منوی ادمین", "adm_home")])
    return inline(rows)


def user_packages_kb(packages: list[sqlite3.Row]) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    for up in packages[:20]:
        status_icon = {"offered": "🆕", "pending_payment": "💳", "active": "✅", "declined": "❌"}.get(str(up["status"]), "🎁")
        pkg = package_by_id(int(up["package_id"]))
        title = pkg["name"] if pkg else f"پکیج #{up['package_id']}"
        rows.append([(f"{status_icon} {title} | {up['code']}", f"pkg_view:{up['id']}")])
    rows.append([("🏠 منوی اصلی", "home")])
    return inline(rows)


def user_package_view_kb(user_package: sqlite3.Row) -> InlineKeyboardMarkup:
    status = str(user_package["status"])
    upid = int(user_package["id"])
    rows: list[list[tuple[str, str]]] = []
    if status == "offered":
        rows.append([("✅ تأیید و رفتن به پرداخت", f"pkg_accept:{upid}"), ("❌ رد کردن", f"pkg_decline:{upid}")])
    elif status == "pending_payment" and user_package["order_id"]:
        rows.append([("💳 ادامه پرداخت", f"pay_page:{user_package['order_id']}")])
    elif status == "active":
        rows.append([("➕ ساخت ساب از این پک", f"pkg_subs:{upid}")])
    rows.append([("⬅️ پکیج‌های من", "my_packages"), ("🏠 منوی اصلی", "home")])
    return inline(rows)


def package_sub_items_kb(user_package_id: int) -> InlineKeyboardMarkup:
    up = user_package_by_id(user_package_id)
    rows: list[list[tuple[str, str]]] = []
    if up:
        for item in package_items(int(up["package_id"])):
            rows.append([(f"➕ {item['title']} — {'رایگان' if int(item['price']) == 0 else fmt_money(int(item['price']))}", f"pkg_make_sub:{user_package_id}:{item['id']}")])
    rows.append([("⬅️ برگشت به پکیج", f"pkg_view:{user_package_id}"), ("🏠 منوی اصلی", "home")])
    return inline(rows)


async def ensure_package_item_catalog_plan(item: sqlite3.Row, admin_id: int = 0) -> tuple[bool, str]:
    key = package_plan_key(int(item["id"]))
    # Use the package item price as CatalogPlan price too; the actual order amount is also stored in orders.
    line = f"{key}|{item['title']}|{float(item['data_gb'])}|{int(item['days'])}|{int(item['price'])}|package|🎁 پکیج"
    ok, msg = await upsert_plan_from_line(line, admin_id)
    return ok, msg

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
        + f"👤 نام کاربری: <b>{h(username)}</b>\n"
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


async def order_activation_preflight(order: sqlite3.Row, telegram_id: int) -> tuple[bool, str]:
    """Prevent taking payments when real activation is known to fail."""
    plan_key = str(order["plan_key"])
    # Wallet topups and buying the package shell itself do not create a remote Pasarguard user.
    if plan_key.startswith("wallet_topup:") or plan_key.startswith("pkg_assign:"):
        return True, ""
    if not settings.pasarguard_enabled:
        return True, ""
    if settings.pasarguard_dry_run:
        return False, "فعال‌سازی خودکار سرویس موقتاً آماده نیست. لطفاً کمی بعد دوباره تلاش کنید یا با پشتیبانی تماس بگیرید."
    if plan_key.startswith("pkg_sub:"):
        try:
            _tag, user_package_id_s, item_id_s = plan_key.split(":")
            user_package = user_package_by_id(int(user_package_id_s), telegram_id)
            item = package_item_by_id(int(item_id_s))
        except Exception:
            return False, "اطلاعات ساب داخل پکیج معتبر نیست."
        if not user_package or str(user_package["status"]) != "active":
            return False, "این پکیج هنوز فعال نیست یا متعلق به این حساب نیست."
        if count_package_subscriptions(int(user_package["id"])) >= int(user_package["max_subscriptions"]):
            return False, "ظرفیت ساخت ساب برای این پکیج تکمیل شده است."
        if not item or int(item["package_id"]) != int(user_package["package_id"]):
            return False, "پلن انتخاب‌شده داخل این پکیج پیدا نشد."
        ok, msg = await ensure_package_item_catalog_plan(item)
        if not ok:
            return False, msg
        template_id, _plan, error = await ensure_template_for_plan(package_plan_key(int(item["id"])))
        if error or not template_id:
            return False, error or "template مرتبط با ساب پکیج هنوز آماده نیست."
        return True, ""
    if plan_key.startswith("addon:") or plan_key.startswith("renew:"):
        try:
            service_id = int(plan_key.split(":")[-1])
        except Exception:
            return False, "شناسه سرویس داخل سفارش معتبر نیست."
        service = db.get_service(service_id, telegram_id)
        if not service or service["status"] == "deleted":
            return False, "سرویس مقصد برای این سفارش پیدا نشد."
        if 'pasarguard_username' not in service.keys() or not service['pasarguard_username']:
            return False, "این سرویس هنوز برای عملیات خودکار آماده نیست. لطفاً با پشتیبانی تماس بگیرید."
        if plan_key.startswith("addon:"):
            return True, ""
        renew_plan_key = plan_key.split(":")[1]
        template_id, _plan, error = await ensure_template_for_plan(renew_plan_key)
        if error or not template_id:
            return False, error or "تنظیمات پلن تمدید هنوز کامل نشده است. لطفاً با پشتیبانی تماس بگیرید."
        return True, ""
    template_id, _plan, error = await ensure_template_for_plan(plan_key)
    if error or not template_id:
        return False, error or "تنظیمات این پلن هنوز کامل نشده است. لطفاً با پشتیبانی تماس بگیرید."
    return True, ""


async def show_order_payment(target: Message | CallbackQuery, telegram_id: int, order_id: int) -> None:
    user = db.get_user(telegram_id)
    order = db.get_order(order_id, telegram_id)
    if not user or not order:
        if isinstance(target, CallbackQuery):
            await target.answer("سفارش پیدا نشد.", show_alert=True)
        return
    text, back_callback, payable = render_order_payment_text(order, user)
    ok, reason = await order_activation_preflight(order, telegram_id)
    plan_key = str(order["plan_key"])
    is_wallet_topup = plan_key.startswith("wallet_topup:")
    wallet_balance = int(user["wallet_balance"] or 0)
    if not ok:
        text += "\n\n⚠️ <b>این سفارش فعلاً قابل پرداخت نیست.</b>\n" + h(reason)
        markup = inline([[('🎫 پشتیبانی', 'ticket_new')], [("⬅️ بازگشت", back_callback), ("🏠 منوی اصلی", "home")]])
    elif not is_wallet_topup and payable > 0 and (wallet_balance < payable or wallet_balance < 0):
        suggested = wallet_shortfall(payable, wallet_balance)
        shortage = max(payable - wallet_balance, 0)
        if wallet_balance < 0:
            text += (
                "\n\n⚠️ <b>موجودی کیف پول شما منفی است.</b>\n"
                f"موجودی فعلی: <b>{fmt_money(wallet_balance)}</b>\n"
                f"برای انجام این سفارش حداقل باید <b>{fmt_money(suggested)}</b> کیف پول را شارژ کنید. "
                f"حداقل شارژ کیف پول <b>{fmt_money(WALLET_MIN_TOPUP)}</b> است."
            )
        else:
            text += (
                "\n\n💰 <b>پرداخت این سفارش فقط از کیف پول انجام می‌شود.</b>\n"
                f"موجودی فعلی شما: <b>{fmt_money(wallet_balance)}</b>\n"
                f"کمبود موجودی: <b>{fmt_money(shortage)}</b>\n"
                f"حداقل شارژ کیف پول <b>{fmt_money(WALLET_MIN_TOPUP)}</b> است؛ برای ادامه پیشنهاد می‌شود <b>{fmt_money(suggested)}</b> شارژ کنید."
            )
        markup = insufficient_wallet_kb(order_id, suggested, back_callback)
    else:
        markup = order_payment_kb(
            order_id,
            payable,
            wallet_balance,
            back_callback,
            "⬅️ بازگشت",
            allow_wallet=not is_wallet_topup,
            allow_coupon=not plan_key.startswith("pkg_"),
            is_wallet_topup=is_wallet_topup,
        )
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
    if status in {"locked", "banned"}:
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

        # Required-channel gate: every non-admin use of the bot is checked.
        # The retry button itself is allowed to reach its handler so it can re-check and refresh the message.
        callback_data = str(getattr(event, "data", "") or "") if isinstance(event, CallbackQuery) else ""
        if callback_data != "check_required_channels":
            active_channels = list_required_channels(active_only=True)
            if active_channels:
                bot = data.get("bot") or getattr(event, "bot", None)
                if bot is not None:
                    missing = await missing_required_channels(bot, telegram_id)
                    if missing:
                        text = required_channels_prompt_text(missing)
                        markup = required_channels_user_kb(missing)
                        if isinstance(event, CallbackQuery):
                            if event.message:
                                try:
                                    await event.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
                                except Exception:
                                    await event.message.answer(text, reply_markup=markup, disable_web_page_preview=True)
                            await event.answer("برای استفاده از ربات، اول عضویت کانال‌ها را کامل کنید.", show_alert=True)
                        elif isinstance(event, Message):
                            await event.answer(text, reply_markup=markup, disable_web_page_preview=True)
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
    db.update_order_service_name(order_id, tg_id, service_name)
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
    text = header("🎟 کد تخفیف") + "کد تخفیفی که از پشتیبانی یا کمپین دریافت کرده‌اید را وارد کنید."
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
    coupon_row_obj, error = await validate_coupon_for_order(code, int(user["telegram_id"]), order, getattr(message, "bot", None))
    if not coupon_row_obj:
        await message.answer(f"❌ {h(error or 'این کد تخفیف معتبر نیست.')}\nلطفاً دوباره وارد کنید یا انصراف را بزنید.", reply_markup=coupon_cancel_kb(order_id))
        return

    amount = int(order["amount"])
    details = order_discounts.get(order_id, {"referral": int(order["discount_amount"]), "coupon": 0, "coupon_code": None})
    referral_discount = int(details.get("referral", 0))
    if not int(coupon_row_obj["stack_with_referral"] or 1):
        referral_discount = 0
    base_for_coupon = max(amount - referral_discount, 0)
    coupon_discount = int(base_for_coupon * int(coupon_row_obj["percent"]) / 100)
    if row_has(coupon_row_obj, "max_discount_amount") and coupon_row_obj["max_discount_amount"] is not None:
        coupon_discount = min(coupon_discount, int(coupon_row_obj["max_discount_amount"]))
    max_discount_percent = int(coupon_row_obj["max_discount_percent"] if row_has(coupon_row_obj, "max_discount_percent") and coupon_row_obj["max_discount_percent"] is not None else 100)
    max_total_discount = int(amount * min(max_discount_percent, 100) / 100)
    total_discount = min(referral_discount + coupon_discount, max_total_discount)
    coupon_discount = max(total_discount - referral_discount, 0)
    order_discounts[order_id] = {"referral": referral_discount, "coupon": coupon_discount, "coupon_code": code}
    save_order_coupon(order_id, int(user["telegram_id"]), code, coupon_discount, total_discount)
    await state.clear()
    await message.answer(header("✅ کد تخفیف اعمال شد") + f"کد <code>{h(code)}</code> با موفقیت روی سفارش اعمال شد.")
    await show_order_payment(message, int(user["telegram_id"]), order_id)


@router.callback_query(F.data.startswith("pay_demo:"))
async def pay_demo(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    await complete_order(callback, int(user["telegram_id"]), int(callback.data.split(":", 1)[1]), "پرداخت دمو", use_wallet=False)


@router.callback_query(F.data.startswith("pay_wallet:"))
async def pay_wallet(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    await complete_order(callback, int(user["telegram_id"]), int(callback.data.split(":", 1)[1]), "کیف پول", use_wallet=True)


async def notify_sales_admins_about_receipt(bot: Bot, receipt_id: int) -> None:
    receipt = get_receipt(receipt_id)
    if not receipt:
        return
    order = get_order_any(int(receipt["order_id"]))
    if not order:
        return
    targets = sales_admin_ids()
    if not targets:
        logger.warning("No sales admins found for receipt %s", receipt_id)
        return
    for admin_id in targets:
        try:
            await bot.send_message(admin_id, receipt_notify_admin_text(order, receipt), reply_markup=receipt_admin_kb(receipt_id, int(order["id"])))
        except Exception as exc:
            logger.warning("Failed to notify sales admin %s about receipt %s: %s", admin_id, receipt_id, exc)


@router.callback_query(F.data.startswith("pay_methods:"))
async def pay_methods(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    user = ensure_from_callback(callback)
    telegram_id = int(user["telegram_id"])
    order_id = int(callback.data.split(":", 1)[1])
    order = db.get_order(order_id, telegram_id)
    if not order or order["status"] not in {"pending", "payment_rejected"}:
        await callback.answer("این سفارش پیدا نشد یا قابل پرداخت نیست.", show_alert=True)
        return
    if not str(order["plan_key"]).startswith("wallet_topup:"):
        await callback.answer("روش‌های پرداخت مستقیم برای خرید سرویس حذف شده است.", show_alert=True)
        await show_order_payment(callback, telegram_id, order_id)
        return
    payable = max(int(order["amount"]) - int(order["discount_amount"]), 0)
    text = header("💳 انتخاب روش پرداخت", f"سفارش #{order_id}")
    text += f"✅ مبلغ قابل پرداخت: <b>{fmt_money(payable)}</b>\n\n"
    text += "لطفاً یکی از روش‌های پرداخت را انتخاب کنید. فعلاً فقط کارت‌به‌کارت فعال است."
    await edit_or_answer(callback, text, payment_methods_kb(order_id))


@router.callback_query(F.data.startswith("pay_card:"))
async def pay_card_start(callback: CallbackQuery, state: FSMContext) -> None:
    user = ensure_from_callback(callback)
    telegram_id = int(user["telegram_id"])
    order_id = int(callback.data.split(":", 1)[1])
    order = db.get_order(order_id, telegram_id)
    if not order or order["status"] not in {"pending", "payment_rejected"}:
        await callback.answer("این سفارش پیدا نشد یا قابل پرداخت نیست.", show_alert=True)
        return
    if not str(order["plan_key"]).startswith("wallet_topup:"):
        await callback.answer("پرداخت مستقیم حذف شده است؛ ابتدا کیف پول را شارژ کنید.", show_alert=True)
        await show_order_payment(callback, telegram_id, order_id)
        return
    payable = max(int(order["amount"]) - int(order["discount_amount"]), 0)
    card = choose_payment_card()
    if not card:
        await edit_or_answer(
            callback,
            header("💳 کارت‌به‌کارت موقتاً غیرفعال است")
            + "در حال حاضر شماره کارت فعالی برای شارژ کیف پول ثبت نشده است. لطفاً کمی بعد دوباره تلاش کنید یا با پشتیبانی تماس بگیرید.",
            inline([[('🎫 پشتیبانی', 'ticket_new')], [('⬅️ بازگشت', f'pay_page:{order_id}'), ('🏠 منوی اصلی', 'home')]]),
        )
        return
    receipt_id = create_or_reset_payment_receipt(order, card, payable)
    receipt = get_receipt(receipt_id)
    await state.set_state(CardPaymentStates.waiting_receipt_bundle)
    await state.update_data(order_id=order_id, receipt_id=receipt_id)
    text = card_payment_instructions(order, card, payable)
    if receipt and row_has(receipt, "expires_at") and receipt["expires_at"]:
        text += f"\n\n⏳ مهلت ثبت تراکنش: <code>{h(fmt_jalali_datetime(receipt['expires_at']))}</code>"
    await edit_or_answer(callback, text, receipt_upload_kb(receipt_id, order_id, receipt_file_count(receipt_id) > 0))


@router.message(CardPaymentStates.waiting_receipt_bundle)
async def card_receipt_received(message: Message, state: FSMContext) -> None:
    user = ensure_from_message(message)
    data = await state.get_data()
    order_id = int(data.get("order_id", 0))
    receipt_id = int(data.get("receipt_id", 0))
    order = db.get_order(order_id, int(user["telegram_id"]))
    receipt = get_receipt(receipt_id)
    if not order or not receipt or order["status"] not in {"pending", "payment_rejected"}:
        await state.clear()
        await message.answer("این سفارش دیگر قابل ثبت رسید نیست.", reply_markup=back_home_kb())
        return
    if receipt_deadline_expired(receipt):
        await state.clear()
        await message.answer(
            header("⏳ مهلت ثبت رسید تمام شد")
            + f"برای این تراکنش فقط {RECEIPT_UPLOAD_WINDOW_MINUTES} دقیقه فرصت ارسال رسید وجود داشت. لطفاً از بخش کیف پول یک شارژ جدید ثبت کنید.",
            reply_markup=wallet_kb(),
        )
        return
    text = normalize_digits((message.text or "").strip())
    if text in {"لغو", "انصراف", "cancel", "/cancel"}:
        await state.clear()
        await message.answer("ارسال رسید لغو شد. تراکنش هنوز برای بررسی ارسال نشده است.", reply_markup=back_home_kb())
        return
    file_type = ""
    file_id = ""
    unique_id = ""
    if message.photo:
        photo = message.photo[-1]
        file_type = "photo"
        file_id = photo.file_id
        unique_id = photo.file_unique_id
    elif message.document:
        file_type = "document"
        file_id = message.document.file_id
        unique_id = message.document.file_unique_id
    if file_id:
        add_receipt_file(receipt_id, message, file_type, file_id, unique_id, message.caption)
        count = receipt_file_count(receipt_id)
        await message.answer(
            header("✅ رسید اضافه شد")
            + f"تا اینجا <b>{fmt_number(count)}</b> فایل رسید ثبت شده است. اگر رسید دیگری دارید بفرستید؛ در پایان دکمه ثبت تراکنش را بزنید.",
            reply_markup=receipt_upload_kb(receipt_id, order_id, True),
        )
        return
    if text:
        set_receipt_user_note(receipt_id, text)
        count = receipt_file_count(receipt_id)
        await message.answer(
            header("📝 توضیح تراکنش ذخیره شد")
            + ("حالا عکس/فایل رسید را بفرستید، یا اگر قبلاً رسیدها را فرستاده‌اید دکمه ثبت تراکنش را بزنید."),
            reply_markup=receipt_upload_kb(receipt_id, order_id, count > 0),
        )
        return
    await message.answer("برای این تراکنش عکس/فایل رسید یا توضیح متنی بفرستید. بعد از تکمیل، دکمه ثبت تراکنش را بزنید.", reply_markup=receipt_upload_kb(receipt_id, order_id, receipt_file_count(receipt_id) > 0))


@router.callback_query(F.data.startswith("receipt_submit:"))
async def receipt_submit(callback: CallbackQuery, state: FSMContext) -> None:
    user = ensure_from_callback(callback)
    receipt_id = int(callback.data.split(":", 1)[1])
    receipt = get_receipt(receipt_id)
    if not receipt or int(receipt["user_telegram_id"]) != int(user["telegram_id"]):
        await callback.answer("رسید پیدا نشد.", show_alert=True)
        return
    order = db.get_order(int(receipt["order_id"]), int(user["telegram_id"]))
    if not order or order["status"] not in {"pending", "payment_rejected"}:
        await callback.answer("این تراکنش دیگر قابل ثبت نیست.", show_alert=True)
        return
    if receipt_deadline_expired(receipt):
        await state.clear()
        await edit_or_answer(
            callback,
            header("⏳ مهلت ثبت رسید تمام شد")
            + f"مهلت {RECEIPT_UPLOAD_WINDOW_MINUTES} دقیقه‌ای این تراکنش تمام شده است. لطفاً از بخش کیف پول، شارژ جدید ثبت کنید.",
            wallet_kb(),
        )
        return
    if receipt_file_count(receipt_id) <= 0 and not (receipt["receipt_chat_id"] and receipt["receipt_message_id"]):
        await callback.answer("ابتدا حداقل یک عکس یا فایل رسید ارسال کنید.", show_alert=True)
        return
    if not submit_payment_receipt(receipt_id):
        await callback.answer("ثبت تراکنش انجام نشد. لطفاً دوباره بررسی کنید.", show_alert=True)
        return
    await state.clear()
    order = get_order_any(int(receipt["order_id"])) or order
    await edit_or_answer(callback, receipt_pending_user_text(order), inline([[('💳 تراکنش‌ها', 'tx_list')], [('🏠 منوی اصلی', 'home')]]))
    await notify_sales_admins_about_receipt(callback.bot, receipt_id)



async def complete_package_assignment_order(callback: CallbackQuery, telegram_id: int, order: sqlite3.Row, method: str, use_wallet: bool) -> None:
    try:
        user_package_id = int(str(order["plan_key"]).split(":", 1)[1])
    except Exception:
        await callback.answer("شناسه پکیج معتبر نیست.", show_alert=True)
        return
    user_package = user_package_by_id(user_package_id, telegram_id)
    if not user_package or str(user_package["status"]) not in {"offered", "pending_payment"}:
        await callback.answer("این پکیج قابل پرداخت نیست.", show_alert=True)
        return
    payable = max(int(order["amount"]) - int(order["discount_amount"]), 0)
    user = db.get_user(telegram_id)
    if not user:
        await callback.answer("حساب کاربری پیدا نشد.", show_alert=True)
        return
    wallet_used = 0
    if use_wallet:
        if int(user["wallet_balance"]) < payable or int(user["wallet_balance"]) < 0:
            await callback.answer("موجودی کیف پول کافی نیست یا منفی است. ابتدا کیف پول را شارژ کنید.", show_alert=True)
            return
        if payable > 0:
            db.add_wallet(telegram_id, -payable, "wallet_payment", f"پرداخت پکیج اختصاصی سفارش #{order['id']}")
        wallet_used = payable
    mark_user_package_active(user_package_id, int(order["id"]))
    mark_order_terminal(int(order["id"]), status="paid", method=method, wallet_used=wallet_used)
    user_package = user_package_by_id(user_package_id, telegram_id) or user_package
    await edit_or_answer(
        callback,
        header("✅ پکیج فعال شد") + "پکیج اختصاصی شما فعال شد. حالا می‌توانید از پلن‌های داخل آن ساب بسازید.\n\n" + user_package_text(user_package),
        user_package_view_kb(user_package),
    )


async def complete_package_sub_order(callback: CallbackQuery, telegram_id: int, order: sqlite3.Row, method: str, use_wallet: bool) -> None:
    try:
        _tag, user_package_id_s, item_id_s = str(order["plan_key"]).split(":")
        user_package_id = int(user_package_id_s)
        item_id = int(item_id_s)
    except Exception:
        await callback.answer("اطلاعات ساب معتبر نیست.", show_alert=True)
        return
    user_package = user_package_by_id(user_package_id, telegram_id)
    item = package_item_by_id(item_id)
    if not user_package or str(user_package["status"]) != "active":
        await callback.answer("این پکیج فعال نیست.", show_alert=True)
        return
    if not item or int(item["package_id"]) != int(user_package["package_id"]):
        await callback.answer("این پلن داخل پکیج شما وجود ندارد.", show_alert=True)
        return
    if count_package_subscriptions(user_package_id) >= int(user_package["max_subscriptions"]):
        await callback.answer("ظرفیت ساخت ساب برای این پکیج تکمیل شده است.", show_alert=True)
        return
    payable = max(int(order["amount"]) - int(order["discount_amount"]), 0)
    user = db.get_user(telegram_id)
    if not user:
        await callback.answer("حساب کاربری پیدا نشد.", show_alert=True)
        return
    wallet_used = 0
    if use_wallet:
        if int(user["wallet_balance"]) < payable or int(user["wallet_balance"]) < 0:
            await callback.answer("موجودی کیف پول کافی نیست یا منفی است. ابتدا کیف پول را شارژ کنید.", show_alert=True)
            return
        if payable > 0:
            db.add_wallet(telegram_id, -payable, "wallet_payment", f"پرداخت ساب پکیج سفارش #{order['id']}")
        wallet_used = payable
    ok, msg = await ensure_package_item_catalog_plan(item, callback.from_user.id)
    if not ok:
        refund_wallet_payment_if_needed(telegram_id, wallet_used, int(order["id"]), msg)
        await edit_or_answer(callback, header("⚠️ ساخت ساب ناموفق بود") + h(msg), user_package_view_kb(user_package))
        return
    plan = Plan(
        key=package_plan_key(item_id),
        title=str(item["title"]),
        data_gb=float(item["data_gb"]),
        days=int(item["days"]),
        price=int(item["price"]),
        category="package",
        badge="🎁 پکیج",
    )
    service_name = make_service_name(telegram_id)
    service_id = db.create_service(telegram_id, service_name, plan, payable, is_test=False, status="provisioning" if settings.pasarguard_enabled else "active")
    db.update_order_service(int(order["id"]), service_id)
    ok, remote_result, service = await provision_service_or_mark_failed(service_id, telegram_id, order_id=int(order["id"]), is_test=False, paid_amount=payable)
    if not ok:
        error = _remote_failure_text(remote_result)
        refund_wallet_payment_if_needed(telegram_id, wallet_used, int(order["id"]), error)
        mark_order_terminal(int(order["id"]), status="provisioning_failed", method=method, wallet_used=0, service_id=service_id, admin_note=error)
        await edit_or_answer(
            callback,
            header("⚠️ ساخت ساب ناموفق بود", service_name)
            + "فعال‌سازی ساب کامل نشد. اگر مبلغی از کیف پول کم شده باشد، برگشت داده شد.\n\n"
            + "جزئیات خطا در پنل ادمین قابل بررسی است.",
            inline([[('🎫 پشتیبانی', 'ticket_new')], [("🎁 پکیج‌های من", "my_packages"), ("🏠 منوی اصلی", "home")]]),
        )
        return
    record_package_subscription(user_package_id, item_id, service_id, telegram_id, int(order["id"]))
    mark_order_terminal(int(order["id"]), status="paid", method=method, wallet_used=wallet_used, service_id=service_id)
    service = db.get_service(service_id, telegram_id)
    await edit_or_answer(callback, header("✅ ساب پکیج ساخته شد", service_name) + service_text(service), service_details_kb(service))

async def complete_order(callback: CallbackQuery, telegram_id: int, order_id: int, method: str, use_wallet: bool) -> None:
    order = db.get_order(order_id, telegram_id)
    if not order or order["status"] != "pending":
        await callback.answer("این سفارش پیدا نشد یا قبلاً پرداخت شده است.", show_alert=True)
        return
    plan_key = str(order["plan_key"])
    if plan_key.startswith("wallet_topup:"):
        await callback.answer("شارژ کیف پول فقط از طریق کارت‌به‌کارت و تأیید رسید انجام می‌شود.", show_alert=True)
        return
    if plan_key.startswith("pkg_assign:"):
        await complete_package_assignment_order(callback, telegram_id, order, method, use_wallet)
        return
    if plan_key.startswith("pkg_sub:"):
        await complete_package_sub_order(callback, telegram_id, order, method, use_wallet)
        return
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
        if int(user["wallet_balance"]) < payable or int(user["wallet_balance"]) < 0:
            await callback.answer("موجودی کیف پول کافی نیست یا منفی است. ابتدا کیف پول را شارژ کنید.", show_alert=True)
            return
        db.add_wallet(telegram_id, -payable, "wallet_payment", f"پرداخت سفارش #{order_id}")
        wallet_used = payable

    service_name = pending_names.get(order_id) or make_service_name(telegram_id)
    service_id = db.create_service(telegram_id, service_name, plan, payable, is_test=False, status="provisioning" if settings.pasarguard_enabled else "active")
    db.update_order_service(order_id, service_id)

    ok, remote_result, service = await provision_service_or_mark_failed(service_id, telegram_id, order_id=order_id, is_test=False, paid_amount=payable)
    if not ok:
        error = _remote_failure_text(remote_result)
        refund_wallet_payment_if_needed(telegram_id, wallet_used, order_id, error)
        mark_order_terminal(order_id, status="provisioning_failed", method=method, wallet_used=0, service_id=service_id, admin_note=error)
        await edit_or_answer(
            callback,
            header("⚠️ پرداخت ثبت شد ولی فعال‌سازی ناموفق بود", service_name)
            + "فعال‌سازی سرویس تکمیل نشد، چون لینک اشتراک آماده نشد.\n"
            + "اگر با کیف پول پرداخت کرده باشید، مبلغ به کیف پول برگشته است.\n\n"
            + "تیم پشتیبانی می‌تواند جزئیات خطا را در پنل ادمین بررسی کند.",
            inline([[('🎫 ارتباط با پشتیبانی', 'ticket_new')], [('📦 سرویس‌های من', 'my_services'), ('🏠 منوی اصلی', 'home')]]),
        )
        return

    mark_order_terminal(order_id, status="paid", method=method, wallet_used=wallet_used, service_id=service_id)
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
        if int(user["wallet_balance"]) < payable or int(user["wallet_balance"]) < 0:
            await callback.answer("موجودی کیف پول کافی نیست یا منفی است. ابتدا کیف پول را شارژ کنید.", show_alert=True)
            return
        db.add_wallet(telegram_id, -payable, "wallet_payment", f"پرداخت افزایش حجم سفارش #{order['id']}")
        wallet_used = payable

    db.add_data_to_service(service_id, telegram_id, pkg.data_gb)
    service = db.get_service(service_id, telegram_id)
    remote_result = await update_remote_user_limit(db, service)
    if settings.pasarguard_enabled and not (remote_result and remote_result.ok and remote_result.applied):
        error = _remote_failure_text(remote_result)
        db.set_service_status(service_id, telegram_id, "provisioning_failed", error)
        refund_wallet_payment_if_needed(telegram_id, wallet_used, int(order["id"]), error)
        mark_order_terminal(int(order["id"]), status="provisioning_failed", method=method, wallet_used=0, service_id=service_id, admin_note=error)
        await edit_or_answer(callback, header("⚠️ افزایش حجم ناموفق بود", service["name"]) + "افزایش حجم روی سرویس تکمیل نشد و تغییری برای کاربر فعال نشد.\n\n" + "تیم پشتیبانی می‌تواند جزئیات خطا را در پنل ادمین بررسی کند.", service_details_kb(db.get_service(service_id, telegram_id)))
        return

    if payable > 0:
        db.mark_first_purchase_done(telegram_id)
    db.update_order_service(int(order["id"]), service_id)
    mark_order_terminal(int(order["id"]), status="paid", method=method, wallet_used=wallet_used, service_id=service_id)
    finalize_coupon_usage(int(order["id"]), telegram_id)
    reward = db.reward_referrer_if_needed(telegram_id, int(order["id"]), payable)
    service = db.get_service(service_id, telegram_id)
    if service and 'pasarguard_username' in service.keys() and service['pasarguard_username']:
        await sync_remote_user_from_panel(db, service)
        service = db.get_service(service_id, telegram_id) or service
    extra = "\n\n💎 پورسانت معرفی با موفقیت برای معرف شما ثبت شد." if reward else ""
    await edit_or_answer(callback, header("✅ حجم سرویس افزایش یافت", service["name"]) + f"بسته <b>{h(pkg.title)}</b> به سرویس واقعی شما اضافه شد.\n⏳ زمان پایان سرویس تغییری نکرد.\n\n" + service_text(service) + extra, service_details_kb(service))


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
        if int(user["wallet_balance"]) < payable or int(user["wallet_balance"]) < 0:
            await callback.answer("موجودی کیف پول کافی نیست یا منفی است. ابتدا کیف پول را شارژ کنید.", show_alert=True)
            return
        db.add_wallet(telegram_id, -payable, "wallet_payment", f"پرداخت تمدید سفارش #{order['id']}")
        wallet_used = payable

    db.renew_service(service_id, telegram_id, plan, payable)
    service = db.get_service(service_id, telegram_id)
    remote_result = await apply_template_to_remote_user(db, service, order_id=int(order["id"]))
    if settings.pasarguard_enabled and not (remote_result and remote_result.ok and remote_result.applied):
        error = _remote_failure_text(remote_result)
        db.set_service_status(service_id, telegram_id, "provisioning_failed", error)
        refund_wallet_payment_if_needed(telegram_id, wallet_used, int(order["id"]), error)
        mark_order_terminal(int(order["id"]), status="provisioning_failed", method=method, wallet_used=0, service_id=service_id, admin_note=error)
        await edit_or_answer(callback, header("⚠️ تمدید ناموفق بود", service["name"]) + "تمدید سرویس تکمیل نشد و تغییری برای کاربر فعال نشد.\n\n" + "تیم پشتیبانی می‌تواند جزئیات خطا را در پنل ادمین بررسی کند.", service_details_kb(db.get_service(service_id, telegram_id)))
        return

    db.update_order_service(int(order["id"]), service_id)
    mark_order_terminal(int(order["id"]), status="paid", method=method, wallet_used=wallet_used, service_id=service_id)
    finalize_coupon_usage(int(order["id"]), telegram_id)
    reward = db.reward_referrer_if_needed(telegram_id, int(order["id"]), payable)
    service = db.get_service(service_id, telegram_id)
    if service and 'pasarguard_username' in service.keys() and service['pasarguard_username']:
        await reset_remote_user_usage(db, service)
        await sync_remote_user_from_panel(db, service)
        service = db.get_service(service_id, telegram_id) or service
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
    text = header("📦 سرویس‌های من") + ("هنوز سرویس فعالی ندارید. از بخش خرید سرویس می‌توانید اولین سرویس خود را دریافت کنید" if not active else "برای دیدن جزئیات، یکی از سرویس‌ها را انتخاب کنید:")
    await edit_or_answer(callback, text, services_kb(services))


async def show_my_services(message: Message, telegram_id: int) -> None:
    services = db.list_services(telegram_id)
    active = [s for s in services if s["status"] != "deleted"]
    text = header("📦 سرویس‌های من") + ("هنوز سرویس فعالی ندارید. از بخش خرید سرویس می‌توانید اولین سرویس خود را دریافت کنید" if not active else "برای دیدن جزئیات، یکی از سرویس‌ها را انتخاب کنید:")
    await message.answer(text, reply_markup=services_kb(services))


@router.callback_query(F.data.startswith("service:"))
async def service_details(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    service = db.get_service(int(callback.data.split(":", 1)[1]), int(user["telegram_id"]))
    if not service or service["status"] == "deleted":
        await callback.answer("سرویس پیدا نشد.", show_alert=True)
        return
    # Phase 4.7: pull latest usage/status/expire/subscription_url from Pasarguard when available.
    remote_result = await sync_remote_user_from_panel(db, service) if 'pasarguard_username' in service.keys() and service['pasarguard_username'] else None
    service = db.get_service(int(service['id']), int(user["telegram_id"])) or service
    notice = "\n\n⚠️ بروزرسانی لحظه‌ای وضعیت سرویس فعلاً ممکن نیست؛ اطلاعات نمایش‌داده‌شده ممکن است کمی قدیمی باشد." if remote_result and not remote_result.skipped and not remote_result.ok else ""
    await edit_or_answer(callback, service_text(service) + notice, service_details_kb(service))


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
    remote_result = await revoke_remote_subscription(db, service)
    if settings.pasarguard_enabled and not (remote_result and remote_result.ok and remote_result.applied):
        error = _remote_failure_text(remote_result)
        await edit_or_answer(callback, header("⚠️ تغییر لینک ناموفق بود", service["name"]) + "تغییر لینک تکمیل نشد. لطفاً کمی بعد دوباره تلاش کنید یا با پشتیبانی تماس بگیرید.\n\n" + "تیم پشتیبانی می‌تواند جزئیات خطا را در پنل ادمین بررسی کند.", service_details_kb(service))
        return
    if not settings.pasarguard_enabled:
        token = db.revoke_service_link(service_id, int(user["telegram_id"]))
    service = db.get_service(service_id, int(user["telegram_id"]))
    link = subscription_link(service)
    await edit_or_answer(callback, header("🔄 لینک اشتراک تغییر کرد", service["name"]) + f"لینک قبلی دیگر قابل استفاده نیست.\n\nلینک جدید:\n<code>{h(link or 'لینک اشتراک ثبت نشده است')}</code>", service_details_kb(service))


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
    await edit_or_answer(callback, header("⚙️ تنظیمات اشتراک", service["name"]) + "گزینه موردنظر را انتخاب کنید:", service_settings_kb(service))


@router.callback_query(F.data.startswith("soon:"))
async def soon(callback: CallbackQuery) -> None:
    await callback.answer("این بخش فعلاً فعال نیست.", show_alert=True)


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
    prefix_note = f"نام جدید را بدون <code>{SERVICE_NAME_PREFIX}</code> وارد کنید." if SERVICE_NAME_PREFIX else "نام جدید را وارد کنید."
    await edit_or_answer(callback, header("✏️ تغییر نام اشتراک", service["name"]) + f"{prefix_note}\n\nفقط حروف انگلیسی، عدد، خط تیره و آندرلاین مجاز است.\nطول نام: ۳ تا ۲۰ کاراکتر", inline([[("❌ لغو", f"svc_settings:{service_id}"), ("🏠 منوی اصلی", "home")]]))



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
    await edit_or_answer(callback, header("✅ عودت انجام شد") + f"مبلغ <b>{fmt_money(amount)}</b> به کیف پول شما برگشت و سرویس از حساب شما حذف شد.", inline([[("💰 کیف پول", "wallet")], [("🏠 منوی اصلی", "home")]]))


@router.callback_query(F.data.startswith("delete_ask:"))
async def delete_ask(callback: CallbackQuery) -> None:
    service_id = int(callback.data.split(":", 1)[1])
    ensure_from_callback(callback)
    await edit_or_answer(callback, header("🗑 حذف سرویس") + "آیا مطمئن هستید؟ این کار سرویس را از لیست شما حذف می‌کند و دسترسی آن غیرفعال می‌شود.", delete_confirm_kb(service_id))


@router.callback_query(F.data.startswith("delete_yes:"))
async def delete_yes(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    service_id = int(callback.data.split(":", 1)[1])
    service = db.get_service(service_id, int(user["telegram_id"]))
    remote_result = await set_remote_user_status(db, service, "deleted") if service else None
    db.delete_service(service_id, int(user["telegram_id"]))
    await edit_or_answer(callback, header("✅ سرویس حذف شد") + "سرویس از لیست شما حذف شد.", back_home_kb())


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
    service_name = make_service_name(telegram_id)
    service_id = db.create_service(telegram_id, service_name, test_plan, 0, is_test=True, status="provisioning" if settings.pasarguard_enabled else "active")
    ok, remote_result, service = await provision_service_or_mark_failed(service_id, telegram_id, order_id=None, is_test=True, paid_amount=0)
    if not ok:
        error = _remote_failure_text(remote_result)
        await edit_or_answer(
            callback,
            header("⚠️ فعال‌سازی سرویس رایگان ناموفق بود", service_name)
            + "فعال‌سازی سرویس رایگان تکمیل نشد و لینک اشتراک آماده نشد.\n\n"
            + "تیم پشتیبانی می‌تواند جزئیات خطا را در پنل ادمین بررسی کند.\n\n"
            + "لطفاً کمی بعد دوباره امتحان کنید یا با پشتیبانی تماس بگیرید.",
            inline([[('🎫 پشتیبانی', 'ticket_new')], [('🏠 منوی اصلی', 'home')]]),
        )
        return
    await edit_or_answer(callback, header("🎁 سرویس رایگان فعال شد", service_name) + "این سرویس برای بررسی کیفیت اتصال آماده است.\n\n" + service_text(service), service_details_kb(service))


def transactions_text_for_user(user: sqlite3.Row) -> tuple[str, list[sqlite3.Row]]:
    try:
        expire_sqlite_payment_deadlines()
    except Exception:
        logger.exception("failed to cleanup transaction deadlines before rendering list")
    orders = db.list_orders(int(user["telegram_id"]))
    text = header("💳 تراکنش‌های شما")
    if not orders:
        text += "هنوز تراکنشی ثبت نشده است."
    else:
        for i, order in enumerate(orders, 1):
            status_map = {
                "paid": "پرداخت شده ✅",
                "pending": "در انتظار ⏳",
                "provisioning": "در حال فعال‌سازی 🔄",
                "provisioning_failed": "فعال‌سازی ناموفق ⚠️",
                "receipt_pending": "رسید در انتظار تأیید 🧾",
                "payment_rejected": "رسید رد شد ❌",
                "expired": "منقضی شده ⏳",
                "rejected": "رد شده ❌",
            }
            status = status_map.get(str(order["status"]), h(order["status"]))
            payable = max(int(order["amount"]) - int(order["discount_amount"]), 0)
            text += f"{i}. <b>{status}</b>\n   🧾 شماره: <code>{order['id']}</code>\n   💰 مبلغ: <b>{fmt_money(payable)}</b>\n   📅 تاریخ: <code>{h(fmt_jalali_datetime(order['created_at']))}</code>\n"
            receipt = get_receipt_by_order(int(order["id"]))
            if receipt and str(receipt["status"]) == "waiting_receipt" and row_has(receipt, "expires_at") and receipt["expires_at"]:
                text += f"   ⏳ مهلت ارسال رسید: <code>{h(fmt_jalali_datetime(receipt['expires_at']))}</code>\n"
            text += "\n"
    return text, orders


@router.message(F.text == "💳 تراکنش‌ها")
async def transactions(message: Message) -> None:
    user = ensure_from_message(message)
    text, orders = transactions_text_for_user(user)
    await message.answer(text, reply_markup=transactions_kb(orders))


@router.callback_query(F.data == "tx_list")
async def transactions_cb(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    user = ensure_from_callback(callback)
    text, orders = transactions_text_for_user(user)
    await edit_or_answer(callback, text, transactions_kb(orders))


@router.callback_query(F.data.startswith("tx_receipt:"))
async def transaction_receipt_start(callback: CallbackQuery, state: FSMContext) -> None:
    user = ensure_from_callback(callback)
    order_id = int(callback.data.split(":", 1)[1])
    order = db.get_order(order_id, int(user["telegram_id"]))
    if not order or order["status"] not in {"pending", "payment_rejected"}:
        await callback.answer("این تراکنش قابل ثبت رسید نیست.", show_alert=True)
        return
    if not str(order["plan_key"]).startswith("wallet_topup:"):
        await callback.answer("برای خرید سرویس ابتدا کیف پول را شارژ کنید.", show_alert=True)
        await show_order_payment(callback, int(user["telegram_id"]), order_id)
        return
    receipt = get_receipt_by_order(order_id)
    if receipt and str(receipt["status"]) == "waiting_receipt" and receipt_deadline_expired(receipt):
        await callback.answer("مهلت ارسال رسید این تراکنش تمام شده است؛ یک شارژ جدید ثبت کنید.", show_alert=True)
        return
    card = choose_payment_card()
    if not card:
        await edit_or_answer(callback, header("💳 کارت‌به‌کارت موقتاً غیرفعال است") + "در حال حاضر کارت فعالی ثبت نشده است.", inline([[('🎫 پشتیبانی', 'ticket_new')], [('🏠 منوی اصلی', 'home')]]))
        return
    payable = max(int(order["amount"]) - int(order["discount_amount"]), 0)
    receipt_id = int(receipt["id"]) if receipt and str(receipt["status"]) == "waiting_receipt" else create_or_reset_payment_receipt(order, card, payable)
    receipt = get_receipt(receipt_id)
    await state.set_state(CardPaymentStates.waiting_receipt_bundle)
    await state.update_data(order_id=order_id, receipt_id=receipt_id)
    text = card_payment_instructions(order, card, payable)
    if receipt and row_has(receipt, "expires_at") and receipt["expires_at"]:
        text += f"\n\n⏳ مهلت ثبت تراکنش: <code>{h(fmt_jalali_datetime(receipt['expires_at']))}</code>"
    await edit_or_answer(callback, text, receipt_upload_kb(receipt_id, order_id, receipt_file_count(receipt_id) > 0))


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
    await state.update_data(suggested_amount=None, source_order_id=None)
    await edit_or_answer(callback, wallet_amount_prompt(), wallet_amount_kb())


@router.callback_query(F.data.startswith("wallet_topup_for:"))
async def wallet_topup_for_order(callback: CallbackQuery, state: FSMContext) -> None:
    user = ensure_from_callback(callback)
    parts = (callback.data or "").split(":")
    if len(parts) < 3:
        await callback.answer("درخواست شارژ معتبر نیست.", show_alert=True)
        return
    order_id = int(parts[1])
    suggested = max(parse_amount(parts[2]) or WALLET_MIN_TOPUP, WALLET_MIN_TOPUP)
    order = db.get_order(order_id, int(user["telegram_id"]))
    if not order or order["status"] not in {"pending", "payment_rejected"}:
        await callback.answer("سفارش پیدا نشد یا دیگر قابل پرداخت نیست.", show_alert=True)
        return
    if str(order["plan_key"]).startswith("wallet_topup:"):
        await callback.answer("این سفارش خودش شارژ کیف پول است.", show_alert=True)
        return
    await state.set_state(WalletStates.waiting_amount)
    await state.update_data(suggested_amount=suggested, source_order_id=order_id)
    await edit_or_answer(callback, wallet_amount_prompt(suggested), wallet_amount_kb())


@router.message(WalletStates.waiting_amount)
async def wallet_topup_amount(message: Message, state: FSMContext) -> None:
    user = ensure_from_message(message)
    data = await state.get_data()
    suggested = data.get("suggested_amount")
    amount = parse_amount(message.text or "")
    if amount is None:
        await message.answer("❌ مبلغ معتبر نیست. فقط عدد وارد کنید.\nمثلاً: <code>100000</code>", reply_markup=wallet_amount_kb())
        return
    if amount < WALLET_MIN_TOPUP:
        await message.answer(f"❌ مبلغ واردشده کمتر از حداقل واریز است.\nحداقل واریز: <b>{fmt_money(WALLET_MIN_TOPUP)}</b>", reply_markup=wallet_amount_kb())
        return
    if suggested and int(suggested) > amount:
        await message.answer(
            f"⚠️ مبلغی که وارد کردید از مبلغ پیشنهادی کمتر است.\n"
            f"مبلغ پیشنهادی برای سفارش انتخاب‌شده: <b>{fmt_money(int(suggested))}</b>\n"
            "اگر با همین مبلغ ادامه دهید ممکن است بعد از تأیید رسید، هنوز موجودی کافی برای خرید نداشته باشید.",
            reply_markup=wallet_amount_kb(),
        )
        return
    order_id = db.create_order(int(user["telegram_id"]), f"wallet_topup:{amount}", amount, 0, 0, "pending", "none")
    await state.clear()
    await show_order_payment(message, int(user["telegram_id"]), order_id)



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



@router.callback_query(F.data == "adm_jobs")
async def admin_jobs_home(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if not require_admin_id(callback.from_user.id, "*"):
        await callback.answer("فقط سوپرادمین می‌تواند Jobها را مدیریت کند.", show_alert=True)
        return
    await edit_or_answer(callback, admin_jobs_text(), admin_jobs_kb())


@router.callback_query(F.data.startswith("adm_job_view:"))
async def admin_job_view(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "*"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    job_key = callback.data.split(":", 1)[1]
    job = get_job(job_key)
    if not job:
        await callback.answer("Job پیدا نشد.", show_alert=True)
        return
    await edit_or_answer(callback, admin_job_text(job), admin_job_kb(job_key, bool(int(job["enabled"] or 0))))


@router.callback_query(F.data.startswith("adm_job_toggle:"))
async def admin_job_toggle(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "*"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    job_key = callback.data.split(":", 1)[1]
    job = get_job(job_key)
    if not job:
        await callback.answer("Job پیدا نشد.", show_alert=True)
        return
    set_job_enabled(job_key, not bool(int(job["enabled"] or 0)))
    job = get_job(job_key)
    await edit_or_answer(callback, admin_job_text(job), admin_job_kb(job_key, bool(int(job["enabled"] or 0))))


@router.callback_query(F.data.startswith("adm_job_run:"))
async def admin_job_run_now(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "*"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    job_key = callback.data.split(":", 1)[1]
    if not get_job(job_key):
        await callback.answer("Job پیدا نشد.", show_alert=True)
        return
    await callback.answer("Job در حال اجراست…", show_alert=False)
    try:
        result = await run_job_and_record(job_key)
        admin_log(callback.from_user.id, "JOB_RUN_MANUAL", "job", job_key, str(result))
        job = get_job(job_key)
        await edit_or_answer(callback, header("✅ Job اجرا شد") + admin_job_text(job), admin_job_kb(job_key, bool(int(job["enabled"] or 0))))
    except Exception as exc:
        job = get_job(job_key)
        await edit_or_answer(callback, header("❌ اجرای Job ناموفق بود") + f"خطا: <code>{h(exc)}</code>\n\n" + (admin_job_text(job) if job else ""), admin_back_kb("adm_jobs"))


@router.callback_query(F.data.startswith("adm_job_interval:"))
async def admin_job_interval_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "*"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    job_key = callback.data.split(":", 1)[1]
    job = get_job(job_key)
    if not job:
        await callback.answer("Job پیدا نشد.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_job_interval)
    await state.update_data(job_key=job_key)
    await edit_or_answer(
        callback,
        header("⏱ تغییر فاصله اجرای Job", job["title"])
        + "فاصله جدید را به دقیقه وارد کنید. حداقل ۱ دقیقه است.\n\n"
        + f"فاصله فعلی: <b>{fmt_number(_job_minutes(job))}</b> دقیقه",
        admin_back_kb(f"adm_job_view:{job_key}"),
    )


@router.message(AdminStates.waiting_job_interval)
async def admin_job_interval_finish(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "*"):
        await message.answer("دسترسی ندارید.")
        return
    data = await state.get_data()
    job_key = str(data.get("job_key") or "")
    raw = normalize_digits(message.text or "").strip()
    if not raw.isdigit() or int(raw) < 1:
        await message.answer("❌ فاصله باید عدد دقیقه و حداقل ۱ باشد.")
        return
    minutes = int(raw)
    if not update_job_interval(job_key, minutes):
        await state.clear()
        await message.answer("Job پیدا نشد.", reply_markup=admin_back_kb("adm_jobs"))
        return
    admin_log(message.from_user.id if message.from_user else 0, "JOB_INTERVAL_UPDATE", "job", job_key, f"minutes={minutes}")
    await state.clear()
    job = get_job(job_key)
    await message.answer(header("✅ فاصله Job تغییر کرد") + admin_job_text(job), reply_markup=admin_job_kb(job_key, bool(int(job["enabled"] or 0))))


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
    await edit_or_answer(callback, header("🔎 جستجوی کاربر") + "چت‌آیدی، نام کاربری، یا نام کاربر را وارد کنید.", admin_back_kb("adm_users"))


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


@router.callback_query(F.data.in_({"adm_recent_users", "adm_buyer_users", "adm_deleted_users"}))
async def admin_user_lists(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "users"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    if callback.data == "adm_deleted_users":
        users = list_all_users(limit=20, deleted_only=True)
        title = "🗑 کاربران حذف‌شده"
    else:
        users = list_all_users(limit=10, buyers_only=callback.data == "adm_buyer_users")
        title = "🆕 آخرین کاربران" if callback.data == "adm_recent_users" else "🛒 کاربران خریدار"
    await edit_or_answer(callback, header(title) + ("کاربری پیدا نشد." if not users else "برای مدیریت، یکی را انتخاب کنید."), admin_user_result_kb(users))


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
    # Backward compatibility for old inline buttons: old "lock" now means "block".
    await admin_user_ban_notify(callback)


@router.callback_query(F.data.startswith("adm_user_lock_silent:"))
async def admin_user_lock_silent(callback: CallbackQuery) -> None:
    # Backward compatibility for old inline buttons: old "lock" now means "block".
    await admin_user_ban_silent(callback)


@router.callback_query(F.data.startswith("adm_user_unlock:"))
async def admin_user_unlock(callback: CallbackQuery) -> None:
    # Backward compatibility for old inline buttons.
    if not require_admin_id(callback.from_user.id, "users"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    uid = int(callback.data.split(":", 1)[1])
    update_user_status(uid, "active", "", "")
    admin_log(callback.from_user.id, "USER_UNBLOCK_COMPAT", "user", uid, "")
    user = get_user_admin(uid)
    await edit_or_answer(callback, header("✅ بلاک کاربر برداشته شد") + "دسترسی کاربر دوباره فعال شد.", admin_user_kb(user))


@router.callback_query(F.data.startswith("adm_user_unban_notify:"))
async def admin_user_unban_notify(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "users"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    uid = int(callback.data.split(":", 1)[1])
    notice = "✅ دسترسی شما به ربات دوباره فعال شد."
    update_user_status(uid, "active", "", "")
    admin_log(callback.from_user.id, "USER_UNBAN_NOTIFY", "user", uid, notice)
    try:
        await callback.bot.send_message(uid, notice)
    except Exception:
        pass
    user = get_user_admin(uid)
    await edit_or_answer(callback, header("✅ بلاک کاربر برداشته شد") + "کاربر با اطلاع‌رسانی از بلاک خارج شد.", admin_user_kb(user))


@router.callback_query(F.data.startswith("adm_user_unban_silent:"))
async def admin_user_unban_silent(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "users"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    uid = int(callback.data.split(":", 1)[1])
    update_user_status(uid, "active", "", "")
    admin_log(callback.from_user.id, "USER_UNBAN_SILENT", "user", uid, "")
    user = get_user_admin(uid)
    await edit_or_answer(callback, header("✅ بلاک کاربر برداشته شد") + "کاربر بدون اطلاع‌رسانی از بلاک خارج شد.", admin_user_kb(user))


@router.callback_query(F.data.startswith("adm_user_restore:"))
async def admin_user_restore(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "users"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    uid = int(callback.data.split(":", 1)[1])
    update_user_status(uid, "active", "restored by admin", "")
    admin_log(callback.from_user.id, "USER_RESTORE", "user", uid, "soft delete restored")
    user = get_user_admin(uid)
    await edit_or_answer(callback, header("♻️ کاربر بازگردانی شد") + "کاربر دوباره فعال شد و به لیست‌های عادی برگشت.", admin_user_kb(user))


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
    await edit_or_answer(callback, header("💰 کیف پول کاربران") + "اول کاربر را با چت‌آیدی یا نام کاربری جستجو کنید، سپس گزینه تغییر کیف پول را بزنید.", admin_back_kb("adm_home"))


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
    remote_result = await sync_remote_user_from_panel(db, service) if 'pasarguard_username' in service.keys() and service['pasarguard_username'] else None
    service = db.get_service(sid) or service
    notice = remote_result.notice() if remote_result and not remote_result.skipped and not remote_result.ok else ""
    await edit_or_answer(callback, admin_service_text(service) + notice, admin_service_kb(service))


@router.callback_query(F.data.startswith("adm_svc_pull:"))
async def admin_service_pull_from_pasarguard(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "services") and not require_admin_id(callback.from_user.id, "users"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    sid = int(callback.data.split(":", 1)[1])
    service = db.get_service(sid)
    if not service:
        await callback.answer("سرویس پیدا نشد.", show_alert=True)
        return
    await callback.answer("در حال sync از Pasarguard…", show_alert=False)
    remote_result = await sync_remote_user_from_panel(db, service)
    admin_log(callback.from_user.id, "SERVICE_PULL_FROM_PASARGUARD", "service", sid, remote_result.error or remote_result.message)
    service = db.get_service(sid) or service
    await edit_or_answer(callback, header("🔄 نتیجه Sync از Pasarguard") + admin_service_text(service) + remote_result.notice(), admin_service_kb(service))


@router.callback_query(F.data == "adm_pg_users_pull")
async def admin_pasarguard_pull_all_services(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "services") and not require_admin_id(callback.from_user.id, "*"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    await callback.answer("در حال sync سرویس‌ها از Pasarguard…", show_alert=False)
    report = await sync_all_remote_users_from_panel(db, limit=500)
    # Also run a read-only template reconcile so manual edits/deletes in the panel are visible from this button.
    template_report = await sync_plan_templates(callback.from_user.id, dry_run=True)
    admin_log(
        callback.from_user.id,
        "PASARGUARD_PULL_ALL_USERS",
        "pasarguard",
        "users",
        f"synced={report.synced}; changed={report.changed}; failed={report.failed}; skipped={report.skipped}; template_actions={template_report.action_count}",
    )
    text = header("🔄 نتیجه Sync سرویس‌ها و Templateها از Pasarguard")
    text += "<b>سرویس‌ها / Userها:</b>\n" + f"<pre>{h(render_remote_bulk_sync_report(report))}</pre>"
    text += "\n<b>Templateها / پلن‌ها:</b>\n" + f"<pre>{h(render_sync_report(template_report))}</pre>"
    text += "\n<i>بخش Templateها فقط dry-run است؛ اگر template حذف/تغییر شده باشد اینجا گزارش می‌شود، اما برای ساخت/اصلاح واقعی باید «اعمال Sync Templateها» را بزنید.</i>"
    await edit_or_answer(callback, text, admin_back_kb("adm_pasarguard"))


@router.callback_query(F.data.startswith("adm_svc_status:"))
async def admin_service_status(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "services"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    try:
        _, sid_s, status = callback.data.split(":", 2)
        sid = int(sid_s)
    except (ValueError, TypeError):
        await callback.answer("درخواست نامعتبر است.", show_alert=True)
        return
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
    try:
        _, sid_s, gb_s = callback.data.split(":", 2)
        sid = int(sid_s)
        gb = float(gb_s)
    except (ValueError, TypeError):
        await callback.answer("درخواست نامعتبر است.", show_alert=True)
        return
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
    try:
        _, sid_s, days_s = callback.data.split(":", 2)
        sid = int(sid_s)
        days = int(days_s)
    except (ValueError, TypeError):
        await callback.answer("درخواست نامعتبر است.", show_alert=True)
        return
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
    try:
        _, uid_s, plan_key = callback.data.split(":", 2)
        uid = int(uid_s)
    except (ValueError, TypeError):
        await callback.answer("درخواست نامعتبر است.", show_alert=True)
        return
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
    ok, remote_result, service = await provision_service_or_mark_failed(service_id, uid, order_id=None, is_test=False, paid_amount=amount)
    try:
        if ok:
            await message.bot.send_message(uid, header("✅ سرویس شما فعال شد") + service_text(service), reply_markup=service_details_kb(service))
        else:
            await message.bot.send_message(uid, header("⚠️ سرویس دستی ساخته شد اما فعال‌سازی ناموفق بود") + service_text(service), reply_markup=service_details_kb(service))
    except Exception:
        pass
    await message.answer((header("✅ سرویس دستی ساخته شد") if ok else header("⚠️ سرویس دستی نیازمند بررسی است")) + admin_service_text(service) + (remote_result.notice() if remote_result else ""), reply_markup=admin_service_kb(service))


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
    if not require_admin_id(callback.from_user.id, "orders") and not require_admin_id(callback.from_user.id, "payment_receipts"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    await edit_or_answer(callback, header("🧾 مدیریت سفارش‌ها") + "یک دسته را انتخاب کنید.", admin_orders_kb())


@router.callback_query(F.data.in_({"adm_orders_pending", "adm_orders_receipts", "adm_orders_paid", "adm_orders_latest"}))
async def admin_orders_list(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "orders") and not require_admin_id(callback.from_user.id, "payment_receipts"):
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
    elif callback.data == "adm_orders_receipts":
        status = "receipt_pending"
        title = "رسیدهای در انتظار تأیید"
    orders = list_orders_admin(20, status)
    await edit_or_answer(callback, header("🧾 " + title) + ("سفارشی پیدا نشد." if not orders else "برای جزئیات انتخاب کنید."), admin_order_list_kb(orders, "adm_orders"))


@router.callback_query(F.data.startswith("adm_order:"))
async def admin_order_details(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "orders") and not require_admin_id(callback.from_user.id, "payment_receipts"):
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
    remote_notice = ""
    if service_id:
        service = db.get_service(service_id)
        if service and (('pasarguard_username' not in service.keys()) or (not service['pasarguard_username'])):
            ok, remote_result, service = await provision_service_or_mark_failed(service_id, int(order["user_telegram_id"]), order_id=oid, is_test=False, paid_amount=max(int(order["amount"]) - int(order["discount_amount"]), 0))
            remote_notice = remote_result.notice() if remote_result else ""
            if not ok:
                mark_order_terminal(oid, status="provisioning_failed", method="تأیید دستی ادمین", service_id=service_id, admin_note=_remote_failure_text(remote_result))
                order = get_order_any(oid)
        try:
            await callback.bot.send_message(int(order["user_telegram_id"]), header("✅ پرداخت شما تأیید شد") + service_text(service), reply_markup=service_details_kb(service))
        except Exception:
            pass
    await edit_or_answer(callback, header("✅ سفارش بررسی شد") + admin_order_text(order) + remote_notice, admin_order_kb(order))


@router.callback_query(F.data == "adm_payments")
async def admin_payments(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if not require_admin_id(callback.from_user.id, "orders"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    await edit_or_answer(callback, payment_cards_text(), payment_cards_kb())


@router.callback_query(F.data == "adm_card_add")
async def admin_card_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "orders"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_card_number)
    await state.update_data(card_number=None, owner_name=None, bank_name="", note="")
    await edit_or_answer(
        callback,
        header("➕ افزودن کارت پرداخت", "مرحله ۱ از ۵")
        + "شماره کارت ۱۶ رقمی را وارد کنید. فقط عدد پذیرفته می‌شود و همان لحظه اعتبارسنجی می‌شود.",
        admin_back_kb("adm_payments"),
    )


@router.message(AdminStates.waiting_card_number)
async def admin_card_number_step(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "orders"):
        await message.answer("دسترسی ندارید.")
        return
    card_number = normalize_card_number(message.text or "")
    if len(card_number) != 16:
        await message.answer("❌ شماره کارت باید دقیقاً ۱۶ رقم باشد. دوباره وارد کنید.")
        return
    await state.update_data(card_number=card_number)
    await state.set_state(AdminStates.waiting_card_owner)
    await message.answer(header("👤 نام صاحب کارت", "مرحله ۲ از ۵") + "نام صاحب کارت را دقیقاً مثل نام بانکی وارد کنید.", reply_markup=admin_back_kb("adm_payments"))


@router.message(AdminStates.waiting_card_owner)
async def admin_card_owner_step(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "orders"):
        await message.answer("دسترسی ندارید.")
        return
    owner = (message.text or "").strip()
    if len(owner) < 2:
        await message.answer("❌ نام صاحب کارت خیلی کوتاه است.")
        return
    await state.update_data(owner_name=owner)
    await state.set_state(AdminStates.waiting_card_bank)
    await message.answer(header("🏦 نام بانک", "مرحله ۳ از ۵") + "نام بانک را وارد کنید. اگر نمی‌خواهید نمایش داده شود، علامت <code>-</code> بفرستید.", reply_markup=admin_back_kb("adm_payments"))


@router.message(AdminStates.waiting_card_bank)
async def admin_card_bank_step(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "orders"):
        await message.answer("دسترسی ندارید.")
        return
    bank = (message.text or "").strip()
    await state.update_data(bank_name="" if bank == "-" else bank)
    await state.set_state(AdminStates.waiting_card_note)
    await message.answer(header("📝 توضیح کارت", "مرحله ۴ از ۵") + "توضیح اختیاری برای کاربر یا تیم فروش وارد کنید. برای خالی گذاشتن <code>-</code> بفرستید.", reply_markup=admin_back_kb("adm_payments"))


@router.message(AdminStates.waiting_card_note)
async def admin_card_note_step(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "orders"):
        await message.answer("دسترسی ندارید.")
        return
    note = (message.text or "").strip()
    await state.update_data(note="" if note == "-" else note)
    await state.set_state(AdminStates.waiting_card_active)
    await message.answer(header("✅ وضعیت کارت", "مرحله ۵ از ۵") + "مشخص کنید کارت از همین حالا برای کاربران نمایش داده شود یا نه.", reply_markup=card_active_select_kb())


@router.callback_query(F.data.startswith("adm_card_active:"))
async def admin_card_active_finish(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "orders"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    data = await state.get_data()
    active = 1 if callback.data.endswith(":1") else 0
    card_number = str(data.get("card_number") or "")
    owner_name = str(data.get("owner_name") or "")
    if len(card_number) != 16 or not owner_name:
        await callback.answer("اطلاعات کارت ناقص است. دوباره شروع کنید.", show_alert=True)
        await state.clear()
        return
    card_id = add_payment_card_admin(card_number, owner_name, str(data.get("bank_name") or ""), str(data.get("note") or ""), active)
    admin_log(callback.from_user.id, "PAYMENT_CARD_ADD", "payment_card", card_id, owner_name)
    await state.clear()
    await edit_or_answer(callback, header("✅ کارت ثبت شد") + payment_cards_text(), payment_cards_kb())


@router.callback_query(F.data.startswith("adm_card_toggle:"))
async def admin_card_toggle(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "orders"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    card_id = int(callback.data.split(":", 1)[1])
    if toggle_payment_card_admin(card_id):
        admin_log(callback.from_user.id, "PAYMENT_CARD_TOGGLE", "payment_card", card_id, "")
        await edit_or_answer(callback, payment_cards_text(), payment_cards_kb())
    else:
        await callback.answer("کارت پیدا نشد.", show_alert=True)


@router.callback_query(F.data.startswith("adm_card_delete:"))
async def admin_card_delete(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "orders"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    card_id = int(callback.data.split(":", 1)[1])
    if delete_payment_card_admin(card_id):
        admin_log(callback.from_user.id, "PAYMENT_CARD_DELETE", "payment_card", card_id, "")
        await edit_or_answer(callback, payment_cards_text(), payment_cards_kb())
    else:
        await callback.answer("کارت پیدا نشد.", show_alert=True)


@router.callback_query(F.data.startswith("adm_receipt_view:"))
async def admin_receipt_view(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "payment_receipts"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    receipt_id = int(callback.data.split(":", 1)[1])
    receipt = get_receipt(receipt_id)
    if not receipt:
        await callback.answer("رسید پیدا نشد.", show_alert=True)
        return
    order = get_order_any(int(receipt["order_id"]))
    await callback.answer("در حال ارسال رسید…")
    files = list_receipt_files(receipt_id)
    sent = 0
    for file_row in files:
        try:
            await callback.bot.copy_message(callback.from_user.id, int(file_row["chat_id"]), int(file_row["message_id"]))
            sent += 1
        except Exception as exc:
            logger.warning("Failed to copy receipt file %s for receipt %s: %s", file_row["id"], receipt_id, exc)
    if not sent and receipt["receipt_chat_id"] and receipt["receipt_message_id"]:
        try:
            await callback.bot.copy_message(callback.from_user.id, int(receipt["receipt_chat_id"]), int(receipt["receipt_message_id"]))
            sent += 1
        except Exception as exc:
            await callback.bot.send_message(callback.from_user.id, f"ارسال فایل رسید ناموفق بود: <code>{h(exc)}</code>")
    if not sent:
        await callback.bot.send_message(callback.from_user.id, "برای این رسید فایل قابل ارسال پیدا نشد.")
    if order:
        await callback.bot.send_message(callback.from_user.id, receipt_notify_admin_text(order, receipt), reply_markup=receipt_admin_kb(receipt_id, int(order["id"])))


@router.callback_query(F.data.startswith("adm_receipt_approve:"))
async def admin_receipt_approve_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "payment_receipts"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    receipt_id = int(callback.data.split(":", 1)[1])
    receipt = get_receipt(receipt_id)
    if not receipt or receipt["status"] != "receipt_pending":
        await callback.answer("این رسید قابل تأیید نیست.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_payment_review_note)
    await state.update_data(receipt_id=receipt_id, action="approve")
    await edit_or_answer(callback, header("✅ تأیید رسید") + "اگر توضیحی برای کاربر دارید بفرستید. اگر توضیح لازم نیست، بنویسید: <code>-</code>", admin_back_kb(f"adm_order:{receipt['order_id']}"))


@router.callback_query(F.data.startswith("adm_receipt_reject:"))
async def admin_receipt_reject_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "payment_receipts"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    receipt_id = int(callback.data.split(":", 1)[1])
    receipt = get_receipt(receipt_id)
    if not receipt or receipt["status"] != "receipt_pending":
        await callback.answer("این رسید قابل رد نیست.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_payment_review_note)
    await state.update_data(receipt_id=receipt_id, action="reject")
    await edit_or_answer(callback, header("❌ رد رسید") + "دلیل رد رسید را برای کاربر بنویسید. مثال: مبلغ واریزی با مبلغ سفارش یکسان نیست، رسید خوانا نیست، یا کارت مقصد اشتباه است.", admin_back_kb(f"adm_order:{receipt['order_id']}"))


@router.message(AdminStates.waiting_payment_review_note)
async def admin_receipt_review_finish(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "payment_receipts"):
        await message.answer("دسترسی ندارید.")
        return
    data = await state.get_data()
    receipt_id = int(data.get("receipt_id", 0))
    action = str(data.get("action") or "")
    note = (message.text or "").strip()
    if note == "-":
        note = ""
    receipt = get_receipt(receipt_id)
    if not receipt or receipt["status"] != "receipt_pending":
        await state.clear()
        await message.answer("این رسید دیگر قابل بررسی نیست.", reply_markup=admin_back_kb("adm_orders_receipts"))
        return
    order = get_order_any(int(receipt["order_id"]))
    if not order:
        await state.clear()
        await message.answer("سفارش مربوط به رسید پیدا نشد.", reply_markup=admin_back_kb("adm_orders_receipts"))
        return
    admin_id = int(message.from_user.id if message.from_user else 0)
    if action == "reject":
        update_receipt_review(receipt_id, "rejected", admin_id, note)
        try:
            user_text = header("❌ رسید پرداخت تأیید نشد", f"سفارش #{order['id']}")
            user_text += "رسید ارسالی شما توسط ادمین فروش بررسی شد و تأیید نشد.\n\n"
            if note:
                user_text += f"📝 توضیح ادمین:\n{h(note)}\n\n"
            user_text += "می‌توانید دوباره از بخش خرید، سفارش جدید ثبت کنید یا برای پیگیری با پشتیبانی در ارتباط باشید."
            await message.bot.send_message(int(order["user_telegram_id"]), user_text, reply_markup=inline([[('🎫 پشتیبانی', 'ticket_new')], [('🏠 منوی اصلی', 'home')]]))
        except Exception:
            pass
        admin_log(admin_id, "PAYMENT_RECEIPT_REJECT", "receipt", receipt_id, note)
        await state.clear()
        refreshed = get_order_any(int(order["id"])) or order
        await message.answer(header("❌ رسید رد شد") + admin_order_text(refreshed), reply_markup=admin_order_kb(refreshed))
        return

    service_id = set_order_paid_admin(int(order["id"]), admin_id, method="کارت به کارت")
    refreshed = get_order_any(int(order["id"])) or order
    is_wallet_topup = str(order["plan_key"]).startswith("wallet_topup:")
    service = db.get_service(service_id) if service_id else None
    provisioning_ok = True
    if is_wallet_topup:
        update_receipt_review(receipt_id, "approved", admin_id, note)
    elif service_id and service and (('pasarguard_username' not in service.keys()) or (not service['pasarguard_username'])):
        ok, remote_result, service = await provision_service_or_mark_failed(service_id, int(order["user_telegram_id"]), order_id=int(order["id"]), is_test=False, paid_amount=max(int(order["amount"]) - int(order["discount_amount"]), 0))
        provisioning_ok = ok
        if not ok:
            mark_order_terminal(int(order["id"]), status="provisioning_failed", method="کارت به کارت", service_id=service_id, admin_note=_remote_failure_text(remote_result))
            update_receipt_review(receipt_id, "approved_provisioning_failed", admin_id, note)
        else:
            update_receipt_review(receipt_id, "approved", admin_id, note)
    else:
        update_receipt_review(receipt_id, "approved", admin_id, note)
    refreshed = get_order_any(int(order["id"])) or refreshed
    try:
        if is_wallet_topup:
            user_row = db.get_user(int(order["user_telegram_id"]))
            user_text = header("✅ شارژ کیف پول تأیید شد", f"سفارش #{order['id']}")
            user_text += f"رسید پرداخت شما تأیید شد و مبلغ <b>{fmt_money(int(order['amount']))}</b> به کیف پول شما اضافه شد.\n"
            if user_row:
                user_text += f"موجودی فعلی کیف پول: <b>{fmt_money(int(user_row['wallet_balance']))}</b>\n"
            if note:
                user_text += f"\n📝 توضیح ادمین:\n{h(note)}\n"
            await message.bot.send_message(int(order["user_telegram_id"]), user_text, reply_markup=inline([[('💰 کیف پول', 'wallet')], [('🛒 خرید سرویس', 'buy'), ('🏠 منوی اصلی', 'home')]]))
        elif provisioning_ok and service:
            user_text = header("✅ پرداخت شما تأیید شد", f"سفارش #{order['id']}")
            user_text += "رسید پرداخت شما تأیید شد و سرویس شما فعال است.\n"
            if note:
                user_text += f"\n📝 توضیح ادمین:\n{h(note)}\n"
            user_text += "\n" + service_text(service)
            await message.bot.send_message(int(order["user_telegram_id"]), user_text, reply_markup=service_details_kb(service))
        else:
            user_text = header("✅ رسید تأیید شد، فعال‌سازی در حال پیگیری است", f"سفارش #{order['id']}")
            user_text += "رسید پرداخت شما تأیید شد، اما فعال‌سازی سرویس نیاز به بررسی پشتیبانی دارد. تیم فروش موضوع را پیگیری می‌کند."
            if note:
                user_text += f"\n\n📝 توضیح ادمین:\n{h(note)}"
            await message.bot.send_message(int(order["user_telegram_id"]), user_text, reply_markup=inline([[('🎫 پشتیبانی', 'ticket_new')], [('🏠 منوی اصلی', 'home')]]))
    except Exception:
        pass
    admin_log(admin_id, "PAYMENT_RECEIPT_APPROVE", "receipt", receipt_id, f"provisioning_ok={provisioning_ok}; note={note}")
    await state.clear()
    await message.answer(header("✅ رسید بررسی شد") + admin_order_text(refreshed), reply_markup=admin_order_kb(refreshed))


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


@router.callback_query(F.data == "adm_required_channels")
async def admin_required_channels(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "channels"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    text = header("📣 عضویت اجباری")
    text += "اینجا کانال‌هایی را تعیین می‌کنید که همه کاربران غیرادمین برای استفاده از ربات باید عضو آن‌ها باشند.\n\n"
    text += "نکته: برای چک با آیدی عددی کانال خصوصی، بات باید داخل کانال باشد و دسترسی لازم داشته باشد. برای دکمه عضویت، لینک دعوت را هم ثبت کنید."
    await edit_or_answer(callback, text, required_channels_admin_kb())


@router.callback_query(F.data == "adm_reqch_add")
async def admin_required_channel_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "channels"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_required_channel_id)
    await state.update_data(required_channel={})
    await edit_or_answer(
        callback,
        header("➕ افزودن کانال الزامی", "مرحله ۱")
        + "شناسه کانال را وارد کنید.\n\n"
        + "برای کانال عمومی: <code>@ChannelUsername</code>\n"
        + "برای کانال خصوصی: آیدی عددی مثل <code>-1001234567890</code>\n\n"
        + "بله، فقط با همین آیدی عددی هم می‌شود عضویت را چک کرد، به شرطی که بات داخل کانال باشد و دسترسی لازم داشته باشد.",
        admin_back_kb("adm_required_channels"),
    )


@router.message(AdminStates.waiting_required_channel_id)
async def admin_required_channel_id_step(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "channels"):
        await message.answer("دسترسی ندارید.")
        return
    ok, ref, err = validate_required_channel_ref(message.text or "")
    if not ok:
        await message.answer(f"❌ {h(err)}")
        return
    await state.update_data(required_channel={"chat_id": ref})
    await state.set_state(AdminStates.waiting_required_channel_title)
    await message.answer(header("عنوان نمایشی کانال", "مرحله ۲") + "عنوانی که کاربر در پیام عضویت می‌بیند را وارد کنید. مثال: <code>کانال رسمی HowToSee</code>", reply_markup=admin_back_kb("adm_required_channels"))


@router.message(AdminStates.waiting_required_channel_title)
async def admin_required_channel_title_step(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "channels"):
        await message.answer("دسترسی ندارید.")
        return
    title = (message.text or "").strip()
    if len(title) < 2 or len(title) > 80:
        await message.answer("❌ عنوان باید بین ۲ تا ۸۰ کاراکتر باشد.")
        return
    data = await state.get_data()
    ch = dict(data.get("required_channel") or {})
    ch["title"] = title
    await state.update_data(required_channel=ch)
    await state.set_state(AdminStates.waiting_required_channel_link)
    await message.answer(
        header("لینک عضویت", "مرحله ۳")
        + "لینک عمومی یا لینک دعوت کانال را وارد کنید. اگر فعلاً لینک نمی‌خواهید، <code>-</code> بفرستید.\n\n"
        + "برای کانال خصوصی، بدون لینک دعوت کاربر دکمه عضویت نمی‌بیند، ولی چک عضویت با آیدی همچنان ممکن است.",
        reply_markup=admin_back_kb("adm_required_channels"),
    )


@router.message(AdminStates.waiting_required_channel_link)
async def admin_required_channel_link_step(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "channels"):
        await message.answer("دسترسی ندارید.")
        return
    raw = (message.text or "").strip()
    link = "" if raw == "-" else raw
    if link and not link.startswith(("http://", "https://", "tg://")):
        await message.answer("❌ لینک باید با http:// یا https:// یا tg:// شروع شود. برای بدون لینک <code>-</code> بفرستید.")
        return
    data = await state.get_data()
    ch = dict(data.get("required_channel") or {})
    if not ch.get("chat_id") or not ch.get("title"):
        await state.clear()
        await message.answer("اطلاعات کانال ناقص بود. دوباره شروع کنید.", reply_markup=required_channels_admin_kb())
        return
    upsert_required_channel(str(ch["chat_id"]), str(ch["title"]), link, message.from_user.id if message.from_user else 0)
    await state.clear()
    await message.answer(header("✅ کانال الزامی ذخیره شد") + f"کانال <b>{h(ch['title'])}</b> ثبت و فعال شد.", reply_markup=required_channels_admin_kb())


@router.callback_query(F.data.startswith("adm_reqch_view:"))
async def admin_required_channel_view(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "channels"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    channel_id = int(callback.data.split(":", 1)[1])
    ch = required_channel_by_id(channel_id)
    if not ch:
        await callback.answer("کانال پیدا نشد.", show_alert=True)
        return
    active = "فعال ✅" if int(ch["is_active"] or 0) else "غیرفعال ⛔"
    text = header("📣 کانال الزامی", required_channel_label(ch))
    text += f"شناسه: <code>{h(ch['chat_id'])}</code>\n"
    text += f"وضعیت: <b>{active}</b>\n"
    text += f"لینک عضویت: <code>{h(ch['invite_link'] or 'ثبت نشده')}</code>\n"
    await edit_or_answer(callback, text, required_channel_admin_view_kb(channel_id))


@router.callback_query(F.data.startswith("adm_reqch_toggle:"))
async def admin_required_channel_toggle(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "channels"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    try:
        _tag, channel_id_s, active_s = callback.data.split(":")
        channel_id = int(channel_id_s)
        active = active_s == "1"
    except Exception:
        await callback.answer("اطلاعات معتبر نیست.", show_alert=True)
        return
    set_required_channel_active(channel_id, active, callback.from_user.id)
    ch = required_channel_by_id(channel_id)
    if not ch:
        await callback.answer("کانال پیدا نشد.", show_alert=True)
        return
    status_text = "فعال ✅" if int(ch["is_active"] or 0) else "غیرفعال ⛔"
    text = header("📣 کانال الزامی", required_channel_label(ch))
    text += f"شناسه: <code>{h(ch['chat_id'])}</code>\n"
    text += f"وضعیت: <b>{status_text}</b>\n"
    text += f"لینک عضویت: <code>{h(ch['invite_link'] or 'ثبت نشده')}</code>\n"
    await edit_or_answer(callback, text, required_channel_admin_view_kb(channel_id))


@router.callback_query(F.data.startswith("adm_reqch_delete:"))
async def admin_required_channel_delete(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "channels"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    try:
        channel_id = int(callback.data.split(":", 1)[1])
    except Exception:
        await callback.answer("اطلاعات معتبر نیست.", show_alert=True)
        return
    delete_required_channel(channel_id, callback.from_user.id)
    await edit_or_answer(callback, header("🗑 کانال حذف شد"), required_channels_admin_kb())


@router.callback_query(F.data == "check_required_channels")
async def check_required_channels_callback(callback: CallbackQuery) -> None:
    if not callback.from_user:
        return
    if is_admin_id(callback.from_user.id):
        await callback.answer("ادمین‌ها از این چک مستثنی هستند.", show_alert=True)
        return
    missing = await missing_required_channels(callback.bot, int(callback.from_user.id))
    if missing:
        await edit_or_answer(callback, required_channels_prompt_text(missing), required_channels_user_kb(missing))
        return
    await edit_or_answer(callback, header("✅ عضویت تأیید شد") + "اکنون می‌توانید از ربات استفاده کنید.", None)


@router.callback_query(F.data == "adm_coupons")
async def admin_coupons(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "coupons"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    await edit_or_answer(callback, header("🎟 مدیریت کد تخفیف") + "کد عمومی، اختصاصی یا خرید اول بسازید و مصرف آن را کنترل کنید.", admin_coupon_kb())


@router.callback_query(F.data == "adm_coupon_edit_conditions")
async def admin_coupon_edit_conditions_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "coupons"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_coupon_edit_condition_code)
    await edit_or_answer(callback, header("🧩 ویرایش شرایط کد") + "کد تخفیفی که می‌خواهید شرایطش را تغییر دهید وارد کنید.", admin_back_kb("adm_coupons"))


@router.message(AdminStates.waiting_coupon_edit_condition_code)
async def admin_coupon_edit_conditions_code(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "coupons"):
        await message.answer("دسترسی ندارید.")
        return
    code = (message.text or "").strip().upper()
    row = coupon_row(code)
    if not row:
        await message.answer("❌ این کد تخفیف پیدا نشد.")
        return
    condition = safe_json_loads(row["condition_json"] if row_has(row, "condition_json") else None, empty_coupon_condition())
    if not condition:
        condition = empty_coupon_condition()
    await state.set_state(None)
    await state.update_data(coupon={"code": code, "conditions": condition}, coupon_edit_existing_code=code, coupon_cond_add_mode="and")
    await message.answer(coupon_condition_preview_text(condition), reply_markup=coupon_condition_builder_kb())


@router.callback_query(F.data == "adm_coupon_add")
async def admin_coupon_add(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "coupons"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_coupon_code)
    await state.update_data(coupon={})
    await edit_or_answer(callback, header("➕ ساخت/ویرایش کد", "مرحله ۱") + "کد تخفیف را وارد کنید. فقط حروف انگلیسی، عدد، خط تیره و آندرلاین؛ مثل <code>VIP100</code>.", admin_back_kb("adm_coupons"))


@router.message(AdminStates.waiting_coupon_code)
async def admin_coupon_code_step(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "coupons"):
        await message.answer("دسترسی ندارید.")
        return
    code = (message.text or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9_-]{3,40}", code):
        await message.answer("❌ کد باید ۳ تا ۴۰ کاراکتر و فقط شامل A-Z، عدد، - یا _ باشد.")
        return
    await state.update_data(coupon={"code": code})
    await state.set_state(AdminStates.waiting_coupon_percent)
    await message.answer(header("درصد تخفیف", "مرحله ۲") + "درصد تخفیف را از ۱ تا ۱۰۰ وارد کنید. ۱۰۰٪ برای کدهای رایگان/هدیه مجاز است.", reply_markup=admin_back_kb("adm_coupons"))


@router.message(AdminStates.waiting_coupon_percent)
async def admin_coupon_percent_step(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "coupons"):
        await message.answer("دسترسی ندارید.")
        return
    percent = parse_amount(message.text or "")
    if percent is None or percent < 1 or percent > 100:
        await message.answer("❌ درصد باید عددی بین ۱ تا ۱۰۰ باشد.")
        return
    data = await state.get_data()
    coupon = dict(data.get("coupon") or {})
    coupon["percent"] = int(percent)
    coupon["conditions"] = empty_coupon_condition()
    await state.update_data(coupon=coupon, coupon_cond_add_mode="and")
    await message.answer(coupon_condition_preview_text(coupon["conditions"]), reply_markup=coupon_condition_builder_kb())


@router.callback_query(F.data == "adm_coupon_condition_builder")
async def admin_coupon_condition_builder(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "coupons"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    data = await state.get_data()
    coupon = dict(data.get("coupon") or {})
    condition = coupon.get("conditions") or empty_coupon_condition()
    await edit_or_answer(callback, coupon_condition_preview_text(condition), coupon_condition_builder_kb())


@router.callback_query(F.data.startswith("adm_coupon_cond_add:"))
async def admin_coupon_condition_add(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "coupons"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    mode = callback.data.split(":", 1)[1]
    await state.update_data(coupon_cond_add_mode="or" if mode == "or" else "and")
    await edit_or_answer(callback, header("افزودن شرط") + "نوع شرط را انتخاب کنید.", coupon_condition_type_kb())


@router.callback_query(F.data == "adm_coupon_cond_type_back")
async def admin_coupon_condition_type_back(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "coupons"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    await edit_or_answer(callback, header("افزودن شرط") + "نوع شرط را انتخاب کنید.", coupon_condition_type_kb())


@router.callback_query(F.data.startswith("adm_coupon_cond_type:"))
async def admin_coupon_condition_type(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "coupons"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    ctype = callback.data.split(":", 1)[1]
    data = await state.get_data()
    coupon = dict(data.get("coupon") or {})
    condition = coupon.get("conditions") or empty_coupon_condition()
    mode = str(data.get("coupon_cond_add_mode") or "and")
    if ctype == "first_purchase":
        condition = add_coupon_clause(condition, {"type": "first_purchase"}, mode)
        coupon["conditions"] = condition
        await state.update_data(coupon=coupon)
        await edit_or_answer(callback, coupon_condition_preview_text(condition), coupon_condition_builder_kb())
        return
    if ctype == "channel_member":
        channels = list_required_channels(active_only=True)
        if not channels:
            await callback.answer("ابتدا از بخش عضویت اجباری، حداقل یک کانال فعال ثبت کنید.", show_alert=True)
            return
        await edit_or_answer(callback, header("انتخاب کانال") + "یکی از کانال‌های ثبت‌شده را انتخاب کنید.", coupon_condition_channel_select_kb())
        return
    if ctype == "user_ids":
        await state.set_state(AdminStates.waiting_coupon_condition_users)
        await edit_or_answer(callback, header("لیست چت‌آیدی") + "چت‌آیدی کاربران مجاز را با کاما، فاصله یا خط جدید وارد کنید. مثال:\n<code>123456 987654</code>", admin_back_kb("adm_coupon_condition_builder"))
        return
    if ctype == "admin_roles":
        await state.update_data(coupon_role_selection=[])
        await edit_or_answer(callback, header("نوع ادمین") + "نقش‌های ادمین مجاز برای این شرط را انتخاب کنید.", coupon_condition_admin_roles_kb([]))
        return
    await callback.answer("نوع شرط معتبر نیست.", show_alert=True)


@router.callback_query(F.data.startswith("adm_coupon_cond_channel:"))
async def admin_coupon_condition_channel(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "coupons"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    try:
        channel_id = int(callback.data.split(":", 1)[1])
    except Exception:
        await callback.answer("کانال معتبر نیست.", show_alert=True)
        return
    ch = required_channel_by_id(channel_id)
    if not ch or not int(ch["is_active"] or 0):
        await callback.answer("کانال پیدا نشد یا غیرفعال است.", show_alert=True)
        return
    data = await state.get_data()
    coupon = dict(data.get("coupon") or {})
    condition = coupon.get("conditions") or empty_coupon_condition()
    mode = str(data.get("coupon_cond_add_mode") or "and")
    condition = add_coupon_clause(condition, {"type": "channel_member", "chat_id": str(ch["chat_id"]), "title": required_channel_label(ch)}, mode)
    coupon["conditions"] = condition
    await state.update_data(coupon=coupon)
    await edit_or_answer(callback, coupon_condition_preview_text(condition), coupon_condition_builder_kb())


@router.message(AdminStates.waiting_coupon_condition_users)
async def admin_coupon_condition_users(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "coupons"):
        await message.answer("دسترسی ندارید.")
        return
    ids = [normalize_digits(x) for x in re.split(r"[,\s]+", message.text or "") if normalize_digits(x).isdigit()]
    ids = list(dict.fromkeys(ids))
    if not ids:
        await message.answer("❌ حداقل یک چت‌آیدی معتبر وارد کنید.")
        return
    data = await state.get_data()
    coupon = dict(data.get("coupon") or {})
    condition = coupon.get("conditions") or empty_coupon_condition()
    mode = str(data.get("coupon_cond_add_mode") or "and")
    condition = add_coupon_clause(condition, {"type": "user_ids", "ids": ids}, mode)
    coupon["conditions"] = condition
    await state.set_state(None)
    await state.update_data(coupon=coupon)
    await message.answer(coupon_condition_preview_text(condition), reply_markup=coupon_condition_builder_kb())


@router.callback_query(F.data.startswith("adm_coupon_cond_role_toggle:"))
async def admin_coupon_condition_role_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "coupons"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    role = callback.data.split(":", 1)[1]
    if role not in ADMIN_ROLE_PERMISSIONS:
        await callback.answer("نقش معتبر نیست.", show_alert=True)
        return
    data = await state.get_data()
    selected = list(data.get("coupon_role_selection") or [])
    if role in selected:
        selected.remove(role)
    else:
        selected.append(role)
    await state.update_data(coupon_role_selection=selected)
    await edit_or_answer(callback, header("نوع ادمین") + "نقش‌های ادمین مجاز برای این شرط را انتخاب کنید.", coupon_condition_admin_roles_kb(selected))


@router.callback_query(F.data == "adm_coupon_cond_role_done")
async def admin_coupon_condition_role_done(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "coupons"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    data = await state.get_data()
    selected = list(data.get("coupon_role_selection") or [])
    if not selected:
        await callback.answer("حداقل یک نقش را انتخاب کنید.", show_alert=True)
        return
    coupon = dict(data.get("coupon") or {})
    condition = coupon.get("conditions") or empty_coupon_condition()
    mode = str(data.get("coupon_cond_add_mode") or "and")
    condition = add_coupon_clause(condition, {"type": "admin_roles", "roles": selected}, mode)
    coupon["conditions"] = condition
    await state.update_data(coupon=coupon, coupon_role_selection=[])
    await edit_or_answer(callback, coupon_condition_preview_text(condition), coupon_condition_builder_kb())


@router.callback_query(F.data == "adm_coupon_cond_negate")
async def admin_coupon_condition_negate(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "coupons"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    data = await state.get_data()
    coupon = dict(data.get("coupon") or {})
    condition = coupon.get("conditions") or empty_coupon_condition()
    if not coupon_condition_groups(condition):
        await callback.answer("هنوز شرطی ثبت نشده است.", show_alert=True)
        return
    condition = negate_last_coupon_clause(condition)
    coupon["conditions"] = condition
    await state.update_data(coupon=coupon)
    await edit_or_answer(callback, coupon_condition_preview_text(condition), coupon_condition_builder_kb())


@router.callback_query(F.data == "adm_coupon_cond_clear")
async def admin_coupon_condition_clear(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "coupons"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    data = await state.get_data()
    coupon = dict(data.get("coupon") or {})
    coupon["conditions"] = empty_coupon_condition()
    await state.update_data(coupon=coupon)
    await edit_or_answer(callback, coupon_condition_preview_text(coupon["conditions"]), coupon_condition_builder_kb())


@router.callback_query(F.data == "adm_coupon_cond_done")
async def admin_coupon_condition_done(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "coupons"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    data = await state.get_data()
    coupon = dict(data.get("coupon") or {})
    condition = coupon.get("conditions") or empty_coupon_condition()
    edit_code = data.get("coupon_edit_existing_code")
    if edit_code:
        update_coupon_condition_admin(str(edit_code), condition, callback.from_user.id)
        await state.clear()
        await edit_or_answer(
            callback,
            header("✅ شرایط کد ذخیره شد")
            + f"کد: <code>{h(edit_code)}</code>\n\nشرایط جدید:\n<b>{h(render_coupon_condition_label(condition))}</b>",
            admin_coupon_kb(),
        )
        return
    await state.set_state(AdminStates.waiting_coupon_usage_limit)
    await edit_or_answer(callback, header("سقف مصرف کل", "مرحله بعد") + "حداکثر تعداد مصرف کل را وارد کنید. برای نامحدود <code>-</code> بفرستید.", admin_back_kb("adm_coupons"))


@router.message(AdminStates.waiting_coupon_users)
async def admin_coupon_users_step(message: Message, state: FSMContext) -> None:
    # Compatibility for old unfinished FSM sessions.
    await admin_coupon_condition_users(message, state)


@router.message(AdminStates.waiting_coupon_usage_limit)
async def admin_coupon_usage_limit_step(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "coupons"):
        await message.answer("دسترسی ندارید.")
        return
    raw = (message.text or "").strip()
    value = None if raw == "-" else parse_amount(raw)
    if raw != "-" and (value is None or value < 1):
        await message.answer("❌ عدد معتبر وارد کنید یا برای نامحدود <code>-</code> بفرستید.")
        return
    data = await state.get_data(); coupon = dict(data.get("coupon") or {})
    coupon["usage_limit"] = value
    await state.update_data(coupon=coupon)
    await state.set_state(AdminStates.waiting_coupon_per_user_limit)
    await message.answer(header("سقف مصرف هر کاربر") + "هر کاربر چند بار بتواند از این کد استفاده کند؟ معمولاً <code>1</code> مناسب است.", reply_markup=admin_back_kb("adm_coupons"))


@router.message(AdminStates.waiting_coupon_per_user_limit)
async def admin_coupon_per_user_limit_step(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "coupons"):
        await message.answer("دسترسی ندارید.")
        return
    value = parse_amount(message.text or "")
    if value is None or value < 1:
        await message.answer("❌ یک عدد مثبت وارد کنید.")
        return
    data = await state.get_data(); coupon = dict(data.get("coupon") or {})
    coupon["per_user_limit"] = int(value)
    await state.update_data(coupon=coupon)
    await state.set_state(AdminStates.waiting_coupon_min_order)
    await message.answer(header("حداقل مبلغ خرید") + "اگر کد فقط برای خریدهای بالای یک مبلغ است، آن مبلغ را وارد کنید. برای بدون محدودیت <code>0</code> بفرستید.", reply_markup=admin_back_kb("adm_coupons"))


@router.message(AdminStates.waiting_coupon_min_order)
async def admin_coupon_min_order_step(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "coupons"):
        await message.answer("دسترسی ندارید.")
        return
    value = parse_amount(message.text or "")
    if value is None:
        await message.answer("❌ مبلغ معتبر نیست. برای بدون محدودیت <code>0</code> بفرستید.")
        return
    data = await state.get_data(); coupon = dict(data.get("coupon") or {})
    coupon["min_order_amount"] = int(value)
    await state.update_data(coupon=coupon)
    await state.set_state(AdminStates.waiting_coupon_max_amount)
    await message.answer(header("سقف مبلغ تخفیف") + "اگر مثلاً کد ۲۰٪ حداکثر تا ۳۰ هزار تومان باشد، <code>30000</code> وارد کنید. برای بدون سقف <code>-</code> بفرستید.", reply_markup=admin_back_kb("adm_coupons"))


@router.message(AdminStates.waiting_coupon_max_amount)
async def admin_coupon_max_amount_step(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "coupons"):
        await message.answer("دسترسی ندارید.")
        return
    raw = (message.text or "").strip()
    value = None if raw == "-" else parse_amount(raw)
    if raw != "-" and value is None:
        await message.answer("❌ مبلغ معتبر نیست یا برای بدون سقف <code>-</code> بفرستید.")
        return
    data = await state.get_data(); coupon = dict(data.get("coupon") or {})
    coupon["max_discount_amount"] = value
    await state.update_data(coupon=coupon)
    await state.set_state(AdminStates.waiting_coupon_expires)
    await message.answer(header("انقضا") + "تعداد روزهای اعتبار کد را وارد کنید. برای بدون تاریخ انقضا <code>-</code> بفرستید.", reply_markup=admin_back_kb("adm_coupons"))


@router.message(AdminStates.waiting_coupon_expires)
async def admin_coupon_expires_step(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "coupons"):
        await message.answer("دسترسی ندارید.")
        return
    raw = (message.text or "").strip()
    expires_days = None if raw == "-" else parse_amount(raw)
    if raw != "-" and (expires_days is None or expires_days < 1):
        await message.answer("❌ تعداد روز معتبر وارد کنید یا برای بدون انقضا <code>-</code> بفرستید.")
        return
    data = await state.get_data(); coupon = dict(data.get("coupon") or {})
    condition = coupon.get("conditions") or empty_coupon_condition()
    condition_label = render_coupon_condition_label(condition)
    create_coupon_admin(
        coupon["code"], int(coupon["percent"]), f"کد تخفیف {coupon['code']}", "custom", "",
        coupon.get("usage_limit"), expires_days, message.from_user.id if message.from_user else 0,
        per_user_limit=int(coupon.get("per_user_limit", 1)),
        max_discount_percent=100,
        max_discount_amount=coupon.get("max_discount_amount"),
        min_order_amount=int(coupon.get("min_order_amount", 0)),
        condition_json=json.dumps(condition, ensure_ascii=False),
        condition_label=condition_label,
    )
    await state.clear()
    await message.answer(
        header("✅ کد تخفیف ذخیره شد")
        + f"کد <code>{h(coupon['code'])}</code> با تخفیف <b>{coupon['percent']}٪</b> فعال شد.\n"
        + f"شرایط استفاده:\n<b>{h(condition_label)}</b>\n"
        + f"حداقل خرید: <b>{fmt_money(int(coupon.get('min_order_amount', 0)))}</b>\n"
        + (f"سقف مبلغ تخفیف: <b>{fmt_money(int(coupon['max_discount_amount']))}</b>\n" if coupon.get("max_discount_amount") else "سقف مبلغ تخفیف: <b>ندارد</b>\n"),
        reply_markup=admin_coupon_kb(),
    )


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
            cond = c["condition_label"] if row_has(c, "condition_label") and c["condition_label"] else render_coupon_condition_label(safe_json_loads(c["condition_json"] if row_has(c, "condition_json") else None, None))
            text += f"{active} <code>{h(c['code'])}</code> — {c['percent']}٪ — مصرف: {fmt_number(int(c['used_count']))}/{h(limit)}\n"
            text += f"   شرط: <code>{h(cond)}</code>\n"
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
    await edit_or_answer(
        callback,
        header("👮 مدیریت ادمین‌ها")
        + "اینجا می‌توانید ادمین اضافه کنید، برای ادمین‌ها نام نمایشی بگذارید، نقششان را ویرایش کنید یا آن‌ها را حذف/غیرفعال کنید.\n\n"
        + "اگر نامی برای ادمین ثبت نشود، همان چت‌آیدی به عنوان نام نمایش داده می‌شود.",
        admin_admins_kb(),
    )


@router.callback_query(F.data == "adm_admin_add")
async def admin_admin_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "*"):
        await callback.answer("فقط سوپر ادمین اجازه دارد.", show_alert=True)
        return
    await state.clear()
    await state.set_state(AdminStates.waiting_add_admin_chat_id)
    await edit_or_answer(callback, header("➕ اضافه کردن ادمین", "مرحله ۱") + "چت‌آیدی عددی ادمین جدید را وارد کنید. اگر چت‌آیدی خودش را نمی‌داند، از ربات‌هایی مثل userinfobot می‌تواند بگیرد.", admin_back_kb("adm_admins"))


@router.message(AdminStates.waiting_add_admin_chat_id)
async def admin_admin_chat_id_step(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "*"):
        await message.answer("دسترسی ندارید.")
        return
    uid_s = normalize_digits((message.text or "").strip())
    if not uid_s.lstrip("-").isdigit():
        await message.answer("❌ چت‌آیدی باید عددی باشد.")
        return
    await state.update_data(target_admin_id=int(uid_s))
    await state.set_state(AdminStates.waiting_add_admin_role)
    role_text = header("سطح دسترسی", "مرحله ۲")
    for role, desc in ADMIN_ROLE_DESCRIPTIONS.items():
        role_text += f"• <code>{h(role)}</code>: {h(desc)}\n"
    role_text += "\nیک نقش را از دکمه‌ها انتخاب کنید."
    await message.answer(role_text, reply_markup=admin_role_select_kb())


@router.callback_query(F.data.startswith("adm_admin_role:"))
async def admin_admin_role_step(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "*"):
        await callback.answer("فقط سوپر ادمین اجازه دارد.", show_alert=True)
        return
    role = callback.data.split(":", 1)[1]
    if role not in ADMIN_ROLE_PERMISSIONS:
        await callback.answer("نقش معتبر نیست.", show_alert=True)
        return
    data = await state.get_data()
    uid = int(data.get("target_admin_id", 0))
    if not uid:
        await callback.answer("چت‌آیدی ثبت نشده است؛ دوباره شروع کنید.", show_alert=True)
        return
    await state.update_data(target_admin_role=role)
    await state.set_state(AdminStates.waiting_add_admin_name)
    await edit_or_answer(
        callback,
        header("نام ادمین", "مرحله ۳")
        + f"برای ادمین <code>{uid}</code> یک نام نمایشی وارد کنید.\n\n"
        + "اگر نام نمی‌خواهید، دکمه زیر را بزنید تا همان چت‌آیدی به عنوان نام ذخیره شود.",
        admin_name_skip_kb(),
    )


async def finish_admin_add(message_or_callback: Any, state: FSMContext, display_name: str | None) -> None:
    data = await state.get_data()
    uid = int(data.get("target_admin_id", 0))
    role = str(data.get("target_admin_role") or "")
    actor_id = int(message_or_callback.from_user.id)
    if not uid or role not in ADMIN_ROLE_PERMISSIONS:
        await state.clear()
        if isinstance(message_or_callback, CallbackQuery):
            await message_or_callback.answer("اطلاعات ناقص است؛ دوباره شروع کنید.", show_alert=True)
        else:
            await message_or_callback.answer("اطلاعات ناقص است؛ دوباره شروع کنید.")
        return
    clean_name = (display_name or str(uid)).strip() or str(uid)
    upsert_admin_local(uid, role, actor_id, clean_name)
    try:
        await upsert_admin_role(uid, role, actor_id, clean_name)
    except Exception as exc:
        logger.warning("Failed to sync admin %s to PostgreSQL role table: %s", uid, exc)
    admin_log(actor_id, "ADMIN_UPSERT", "admin", uid, f"role={role}, name={clean_name}")
    await state.clear()
    text = (
        header("✅ ادمین ذخیره شد")
        + f"نام: <b>{h(clean_name)}</b>\n"
        + f"چت‌آیدی: <code>{uid}</code>\n"
        + f"سطح: <b>{h(role)}</b>\n\n"
        + h(ADMIN_ROLE_DESCRIPTIONS.get(role, ""))
    )
    if isinstance(message_or_callback, CallbackQuery):
        await edit_or_answer(message_or_callback, text, admin_admins_kb())
    else:
        await message_or_callback.answer(text, reply_markup=admin_admins_kb())


@router.message(AdminStates.waiting_add_admin_name)
async def admin_admin_name_step(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "*"):
        await message.answer("دسترسی ندارید.")
        return
    name = (message.text or "").strip()
    if len(name) > 80:
        await message.answer("❌ نام ادمین حداکثر می‌تواند ۸۰ کاراکتر باشد.")
        return
    await finish_admin_add(message, state, name)


@router.callback_query(F.data == "adm_admin_name_skip")
async def admin_admin_name_skip(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "*"):
        await callback.answer("فقط سوپر ادمین اجازه دارد.", show_alert=True)
        return
    data = await state.get_data()
    uid = int(data.get("target_admin_id", 0) or 0)
    await finish_admin_add(callback, state, str(uid) if uid else None)


@router.callback_query(F.data == "adm_admin_list")
async def admin_admin_list(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "*"):
        await callback.answer("فقط سوپر ادمین اجازه دارد.", show_alert=True)
        return
    with closing(db.connect()) as conn:
        rows = list(conn.execute("SELECT * FROM admins ORDER BY is_active DESC, created_at DESC LIMIT 50").fetchall())
    text = header("📋 لیست ادمین‌ها")
    if not rows:
        text += "ادمینی ثبت نشده است."
    else:
        text += "برای ویرایش یا حذف، روی ادمین بزنید.\n\n"
        for a in rows:
            active = "✅ فعال" if int(a["is_active"]) else "⛔ غیرفعال"
            text += f"{active} — <b>{h(admin_display_name(a))}</b> — <code>{a['telegram_id']}</code> — نقش: <b>{h(a['role'])}</b>\n"
    await edit_or_answer(callback, text, admin_list_kb(rows))


@router.callback_query(F.data.startswith("adm_admin_view:"))
async def admin_admin_view(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "*"):
        await callback.answer("فقط سوپر ادمین اجازه دارد.", show_alert=True)
        return
    uid_s = callback.data.split(":", 1)[1]
    if not uid_s.lstrip("-").isdigit():
        await callback.answer("شناسه معتبر نیست.", show_alert=True)
        return
    row = get_admin_record(int(uid_s))
    if not row:
        await callback.answer("ادمین پیدا نشد.", show_alert=True)
        return
    active = "✅ فعال" if int(row["is_active"]) else "⛔ غیرفعال"
    bootstrap_note = "\n⚠️ این ادمین از ENV به عنوان سوپرادمین اصلی تعریف شده و با ری‌استارت دوباره فعال می‌شود." if int(row["telegram_id"]) in BOOTSTRAP_SUPER_ADMIN_IDS else ""
    text = (
        header("👮 جزئیات ادمین")
        + f"نام: <b>{h(admin_display_name(row))}</b>\n"
        + f"چت‌آیدی: <code>{row['telegram_id']}</code>\n"
        + f"نقش: <b>{h(row['role'])}</b>\n"
        + f"وضعیت: <b>{active}</b>\n"
        + f"تاریخ اضافه‌شدن: <code>{h(row['created_at'][:16])}</code>\n"
        + bootstrap_note
    )
    await edit_or_answer(callback, text, admin_detail_kb(row, callback.from_user.id))


@router.callback_query(F.data.startswith("adm_admin_edit_name:"))
async def admin_admin_edit_name_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "*"):
        await callback.answer("فقط سوپر ادمین اجازه دارد.", show_alert=True)
        return
    uid = int(callback.data.split(":", 1)[1])
    row = get_admin_record(uid)
    if not row:
        await callback.answer("ادمین پیدا نشد.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_edit_admin_name)
    await state.update_data(edit_admin_id=uid)
    await edit_or_answer(
        callback,
        header("✏️ تغییر نام ادمین")
        + f"نام فعلی: <b>{h(admin_display_name(row))}</b>\n"
        + f"چت‌آیدی: <code>{uid}</code>\n\n"
        + "نام جدید را بفرستید. اگر نام را خالی یا فقط خط تیره <code>-</code> بفرستید، همان چت‌آیدی به عنوان نام ذخیره می‌شود.",
        admin_back_kb(f"adm_admin_view:{uid}"),
    )


@router.message(AdminStates.waiting_edit_admin_name)
async def admin_admin_edit_name_finish(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "*"):
        await message.answer("دسترسی ندارید.")
        return
    data = await state.get_data()
    uid = int(data.get("edit_admin_id", 0) or 0)
    row = get_admin_record(uid)
    if not row:
        await state.clear()
        await message.answer("ادمین پیدا نشد.", reply_markup=admin_admins_kb())
        return
    raw = (message.text or "").strip()
    name = str(uid) if raw in {"", "-"} else raw
    if len(name) > 80:
        await message.answer("❌ نام ادمین حداکثر می‌تواند ۸۰ کاراکتر باشد.")
        return
    set_admin_display_name_local(uid, name)
    try:
        await upsert_admin_role(uid, str(row["role"]), message.from_user.id if message.from_user else None, name)
    except Exception as exc:
        logger.warning("Failed to sync admin display name %s to PostgreSQL: %s", uid, exc)
    admin_log(message.from_user.id if message.from_user else 0, "ADMIN_NAME_UPDATE", "admin", uid, f"name={name}")
    await state.clear()
    await message.answer(header("✅ نام ادمین تغییر کرد") + f"نام جدید: <b>{h(name)}</b>\nچت‌آیدی: <code>{uid}</code>", reply_markup=admin_detail_kb(get_admin_record(uid), message.from_user.id if message.from_user else 0))


@router.callback_query(F.data.startswith("adm_admin_change_role:"))
async def admin_admin_change_role(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "*"):
        await callback.answer("فقط سوپر ادمین اجازه دارد.", show_alert=True)
        return
    uid = int(callback.data.split(":", 1)[1])
    row = get_admin_record(uid)
    if not row:
        await callback.answer("ادمین پیدا نشد.", show_alert=True)
        return
    if uid in BOOTSTRAP_SUPER_ADMIN_IDS:
        await callback.answer("نقش سوپرادمین اصلی که در ENV تعریف شده قابل تغییر نیست.", show_alert=True)
        return
    text = header("👮 تغییر نقش ادمین") + f"ادمین: <b>{h(admin_display_name(row))}</b>\nچت‌آیدی: <code>{uid}</code>\nنقش فعلی: <b>{h(row['role'])}</b>\n\nنقش جدید را انتخاب کنید."
    await edit_or_answer(callback, text, admin_edit_role_select_kb(uid))


@router.callback_query(F.data.startswith("adm_admin_edit_role:"))
async def admin_admin_edit_role_finish(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "*"):
        await callback.answer("فقط سوپر ادمین اجازه دارد.", show_alert=True)
        return
    try:
        _, uid_s, role = callback.data.split(":", 2)
        uid = int(uid_s)
    except Exception:
        await callback.answer("اطلاعات نقش معتبر نیست.", show_alert=True)
        return
    row = get_admin_record(uid)
    if not row:
        await callback.answer("ادمین پیدا نشد.", show_alert=True)
        return
    if uid in BOOTSTRAP_SUPER_ADMIN_IDS:
        await callback.answer("نقش سوپرادمین اصلی که در ENV تعریف شده قابل تغییر نیست.", show_alert=True)
        return
    if role not in ADMIN_ROLE_PERMISSIONS:
        await callback.answer("نقش معتبر نیست.", show_alert=True)
        return
    set_admin_role_local(uid, role, callback.from_user.id)
    try:
        await upsert_admin_role(uid, role, callback.from_user.id, admin_display_name(row))
    except Exception as exc:
        logger.warning("Failed to sync admin role %s to PostgreSQL: %s", uid, exc)
    admin_log(callback.from_user.id, "ADMIN_ROLE_UPDATE", "admin", uid, f"role={role}")
    await edit_or_answer(callback, header("✅ نقش ادمین تغییر کرد") + f"ادمین: <b>{h(admin_display_name(row))}</b>\nچت‌آیدی: <code>{uid}</code>\nنقش جدید: <b>{h(role)}</b>", admin_detail_kb(get_admin_record(uid), callback.from_user.id))


@router.callback_query(F.data.startswith("adm_admin_delete_ask:"))
async def admin_admin_delete_ask(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "*"):
        await callback.answer("فقط سوپر ادمین اجازه دارد.", show_alert=True)
        return
    uid = int(callback.data.split(":", 1)[1])
    row = get_admin_record(uid)
    if not row:
        await callback.answer("ادمین پیدا نشد.", show_alert=True)
        return
    if uid == callback.from_user.id:
        await callback.answer("نمی‌توانید دسترسی خودتان را حذف کنید.", show_alert=True)
        return
    if uid in BOOTSTRAP_SUPER_ADMIN_IDS:
        await callback.answer("این سوپرادمین از ENV تعریف شده و از داخل پنل حذف نمی‌شود.", show_alert=True)
        return
    text = header("🗑 حذف/غیرفعال کردن ادمین") + f"آیا مطمئن هستید می‌خواهید دسترسی <b>{h(admin_display_name(row))}</b> با چت‌آیدی <code>{uid}</code> را حذف/غیرفعال کنید؟"
    await edit_or_answer(callback, text, admin_delete_confirm_kb(uid))


@router.callback_query(F.data.startswith("adm_admin_delete_do:"))
async def admin_admin_delete_do(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "*"):
        await callback.answer("فقط سوپر ادمین اجازه دارد.", show_alert=True)
        return
    uid = int(callback.data.split(":", 1)[1])
    row = get_admin_record(uid)
    if not row:
        await callback.answer("ادمین پیدا نشد.", show_alert=True)
        return
    if uid == callback.from_user.id:
        await callback.answer("نمی‌توانید دسترسی خودتان را حذف کنید.", show_alert=True)
        return
    if uid in BOOTSTRAP_SUPER_ADMIN_IDS:
        await callback.answer("این سوپرادمین از ENV تعریف شده و از داخل پنل حذف نمی‌شود.", show_alert=True)
        return
    set_admin_active_local(uid, False)
    try:
        await deactivate_admin_role(uid)
    except Exception as exc:
        logger.warning("Failed to deactivate admin %s in PostgreSQL: %s", uid, exc)
    admin_log(callback.from_user.id, "ADMIN_DEACTIVATE", "admin", uid, f"name={admin_display_name(row)}")
    await edit_or_answer(callback, header("✅ ادمین حذف/غیرفعال شد") + f"ادمین <b>{h(admin_display_name(row))}</b> دیگر به پنل مدیریت دسترسی ندارد.", admin_admins_kb())


@router.callback_query(F.data.startswith("adm_admin_restore:"))
async def admin_admin_restore(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "*"):
        await callback.answer("فقط سوپر ادمین اجازه دارد.", show_alert=True)
        return
    uid = int(callback.data.split(":", 1)[1])
    row = get_admin_record(uid)
    if not row:
        await callback.answer("ادمین پیدا نشد.", show_alert=True)
        return
    set_admin_active_local(uid, True)
    try:
        await upsert_admin_role(uid, str(row["role"]), callback.from_user.id, admin_display_name(row))
    except Exception as exc:
        logger.warning("Failed to restore admin %s in PostgreSQL: %s", uid, exc)
    admin_log(callback.from_user.id, "ADMIN_RESTORE", "admin", uid, f"role={row['role']}")
    await edit_or_answer(callback, header("✅ ادمین فعال شد") + f"ادمین <b>{h(admin_display_name(row))}</b> دوباره فعال شد.", admin_detail_kb(get_admin_record(uid), callback.from_user.id))


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



# -----------------------------
# Package-code admin/user handlers
# -----------------------------
@router.callback_query(F.data == "adm_packages")
async def admin_packages(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if not require_admin_id(callback.from_user.id, "packages"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    text = header("🎁 مدیریت کد پکیج‌ها")
    text += "اینجا می‌توانید پکیج مستقل از پلن‌های فروش بسازید، برای کاربر خاص اختصاص دهید، و بعد از خرید، ساخت ساب‌های داخل پک را مدیریت کنید."
    await edit_or_answer(callback, text, admin_packages_kb())


@router.callback_query(F.data == "adm_pkg_list")
async def admin_package_list(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "packages"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    packages = list_package_templates(False)
    await edit_or_answer(callback, header("📋 لیست پکیج‌ها") + ("پکیجی ثبت نشده است." if not packages else "یکی را انتخاب کنید."), admin_package_list_kb(packages))


@router.callback_query(F.data.startswith("adm_pkg:") )
async def admin_package_details(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "packages"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    package_id = int(callback.data.split(":", 1)[1])
    package = package_by_id(package_id)
    if not package:
        await callback.answer("پکیج پیدا نشد.", show_alert=True)
        return
    await edit_or_answer(callback, package_text(package), admin_package_kb(package_id))


@router.callback_query(F.data.startswith("adm_pkg_toggle:"))
async def admin_package_toggle(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "packages"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    package_id = int(callback.data.split(":", 1)[1])
    package = package_by_id(package_id)
    if not package:
        await callback.answer("پکیج پیدا نشد.", show_alert=True)
        return
    new_active = 0 if int(package["is_active"]) else 1
    with closing(db.connect()) as conn:
        conn.execute("UPDATE package_templates SET is_active = ?, updated_at = ? WHERE id = ?", (new_active, now_iso(), package_id))
        conn.commit()
    admin_log(callback.from_user.id, "PACKAGE_TOGGLE", "package", package_id, f"active={new_active}")
    package = package_by_id(package_id)
    await edit_or_answer(callback, header("✅ وضعیت پکیج تغییر کرد") + package_text(package), admin_package_kb(package_id))


@router.callback_query(F.data == "adm_pkg_new")
async def admin_package_new_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "packages"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    await state.set_state(AdminStates.waiting_package_name)
    await state.update_data(pkg_items=[])
    await edit_or_answer(callback, header("➕ ساخت پکیج", "مرحله ۱") + "نام پکیج را وارد کنید. مثال: <code>پکیج ویژه دانشجو</code>", admin_back_kb("adm_packages"))


@router.message(AdminStates.waiting_package_name)
async def admin_package_name_step(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "packages"):
        await message.answer("دسترسی ندارید.")
        return
    ok, value = validate_package_name(message.text or "")
    if not ok:
        await message.answer("❌ " + value)
        return
    await state.update_data(pkg_name=value)
    await state.set_state(AdminStates.waiting_package_price)
    await message.answer(header("💰 قیمت پکیج", "مرحله ۲") + "قیمت خود پک را به تومان وارد کنید. برای رایگان بودن <code>0</code> بفرستید.", reply_markup=admin_back_kb("adm_packages"))


@router.message(AdminStates.waiting_package_price)
async def admin_package_price_step(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "packages"):
        await message.answer("دسترسی ندارید.")
        return
    ok, amount, err = parse_positive_amount(message.text or "", allow_zero=True)
    if not ok:
        await message.answer("❌ " + err)
        return
    await state.update_data(pkg_price=amount)
    await state.set_state(AdminStates.waiting_package_code)
    await message.answer(
        header("🧾 کد پکیج", "مرحله ۳")
        + "کد پکیج را وارد کنید یا از دکمه ساخت خودکار استفاده کنید. مثال: <code>STUDENT_01</code>",
        reply_markup=inline([[('🎲 ساخت کد خودکار', 'adm_pkg_code_random')], [('⬅️ بازگشت', 'adm_packages'), ('👑 منوی ادمین', 'adm_home')]]),
    )


@router.callback_query(F.data == "adm_pkg_code_random")
async def admin_package_code_random(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "packages"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    for _ in range(20):
        code = random_package_code()
        ok, _value = validate_package_code(code)
        if ok:
            await state.update_data(pkg_code=code)
            await state.set_state(AdminStates.waiting_package_description)
            await edit_or_answer(callback, header("📝 توضیحات پکیج", "مرحله ۴") + f"کد ساخته شد: <code>{h(code)}</code>\n\nتوضیحات پکیج را وارد کنید. برای خالی گذاشتن <code>-</code> بفرستید.", admin_back_kb("adm_packages"))
            return
    await callback.answer("ساخت کد خودکار ناموفق بود؛ دستی وارد کنید.", show_alert=True)


@router.message(AdminStates.waiting_package_code)
async def admin_package_code_step(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "packages"):
        await message.answer("دسترسی ندارید.")
        return
    ok, value = validate_package_code(message.text or "")
    if not ok:
        await message.answer("❌ " + value)
        return
    await state.update_data(pkg_code=value)
    await state.set_state(AdminStates.waiting_package_description)
    await message.answer(header("📝 توضیحات پکیج", "مرحله ۴") + "توضیحات پکیج را وارد کنید. برای خالی گذاشتن <code>-</code> بفرستید.", reply_markup=admin_back_kb("adm_packages"))


@router.message(AdminStates.waiting_package_description)
async def admin_package_description_step(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "packages"):
        await message.answer("دسترسی ندارید.")
        return
    ok, value = validate_long_text(message.text or "", "توضیحات", allow_empty=True)
    if not ok:
        await message.answer("❌ " + value)
        return
    await state.update_data(pkg_description=value)
    await state.set_state(AdminStates.waiting_package_conditions)
    await message.answer(header("📌 شرایط پکیج", "مرحله ۵") + "شرایط و قوانین این پکیج را وارد کنید. برای خالی گذاشتن <code>-</code> بفرستید.", reply_markup=admin_back_kb("adm_packages"))


@router.message(AdminStates.waiting_package_conditions)
async def admin_package_conditions_step(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "packages"):
        await message.answer("دسترسی ندارید.")
        return
    ok, value = validate_long_text(message.text or "", "شرایط", allow_empty=True)
    if not ok:
        await message.answer("❌ " + value)
        return
    await state.update_data(pkg_conditions=value)
    await state.set_state(AdminStates.waiting_package_max_subs)
    await message.answer(header("🔢 تعداد ساب", "مرحله ۶") + "حداکثر تعداد ساب‌هایی که از این پک می‌شود ساخت را وارد کنید. مثال: <code>3</code>", reply_markup=admin_back_kb("adm_packages"))


@router.message(AdminStates.waiting_package_max_subs)
async def admin_package_max_subs_step(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "packages"):
        await message.answer("دسترسی ندارید.")
        return
    ok, count, err = parse_max_subscriptions(message.text or "")
    if not ok:
        await message.answer("❌ " + err)
        return
    await state.update_data(pkg_max_subscriptions=count)
    await state.set_state(None)
    await message.answer(header("📦 پلن‌های داخل پکیج", "مرحله ۷") + "حالا پلن‌های داخل پک را یکی‌یکی اضافه کنید. می‌توانید از پلن‌های فروش انتخاب کنید یا پلن دستی بسازید.", reply_markup=package_item_add_kb())


@router.callback_query(F.data == "adm_pkg_add_items")
async def admin_package_add_items_menu(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "packages"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    data = await state.get_data()
    items = data.get("pkg_items") or []
    await edit_or_answer(callback, header("📦 پلن‌های داخل پکیج") + f"تعداد آیتم‌های اضافه‌شده: <b>{fmt_number(len(items))}</b>", package_item_add_kb())


@router.callback_query(F.data == "adm_pkg_item_sales")
async def admin_package_item_sales(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "packages"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    await edit_or_answer(callback, header("➕ انتخاب از پلن‌های فروش") + "پلن پایه را انتخاب کنید؛ در مرحله بعد قیمت ساب و تغییرات احتمالی را می‌دهید.", package_sales_plan_kb())


@router.callback_query(F.data.startswith("adm_pkg_pick_sales:"))
async def admin_package_pick_sales(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "packages"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    plan_key = callback.data.split(":", 1)[1]
    plan = PLANS.get(plan_key)
    if not plan:
        await callback.answer("پلن پیدا نشد.", show_alert=True)
        return
    item = {
        "source_type": "sales_plan",
        "source_plan_key": plan.key,
        "title": plan.title,
        "data_gb": plan.data_gb,
        "days": plan.days,
        "price": plan.price,
    }
    await state.update_data(pkg_sales_plan_key=plan_key, pkg_sales_item=item)
    await state.set_state(AdminStates.waiting_package_sales_price)
    text = header("💰 قیمت ساب داخل پکیج")
    text += f"پلن پایه: <b>{h(plan.title)}</b> — {fmt_number(plan.data_gb)}GB / {fmt_number(plan.days)} روز\n"
    text += f"قیمت فعلی پلن فروش: <b>{fmt_money(plan.price)}</b>\n\n"
    text += "قیمت همین ساب داخل پکیج را وارد کنید. برای رایگان بودن <code>0</code> بفرستید. بعد از این مرحله می‌توانید عنوان، حجم و مدت را با دکمه جدا ویرایش کنید."
    await edit_or_answer(callback, text, inline([[('✅ استفاده از قیمت فعلی پلن', 'adm_pkg_sales_use_plan_price')], [('⬅️ بازگشت', 'adm_pkg_item_sales'), ('👑 منوی ادمین', 'adm_home')]]))


@router.callback_query(F.data == "adm_pkg_sales_use_plan_price")
async def admin_package_sales_use_plan_price(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "packages"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    data = await state.get_data()
    item = dict(data.get("pkg_sales_item") or {})
    plan = PLANS.get(str(data.get("pkg_sales_plan_key") or ""))
    if not item or not plan:
        await callback.answer("پلن پایه پیدا نشد؛ دوباره انتخاب کنید.", show_alert=True)
        return
    item["price"] = int(plan.price)
    await state.update_data(pkg_sales_item=item)
    await state.set_state(None)
    await edit_or_answer(callback, package_item_preview_text(item, title="👀 پیش‌نمایش آیتم از پلن فروش"), admin_package_sales_item_review_kb())


@router.message(AdminStates.waiting_package_sales_price)
async def admin_package_sales_price_step(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "packages"):
        await message.answer("دسترسی ندارید.")
        return
    ok, price, err = parse_positive_amount(message.text or "", allow_zero=True)
    if not ok:
        await message.answer("❌ " + err)
        return
    data = await state.get_data()
    item = dict(data.get("pkg_sales_item") or {})
    if not item:
        await state.set_state(None)
        await message.answer("اطلاعات پلن پایه پیدا نشد؛ دوباره انتخاب کنید.", reply_markup=package_item_add_kb())
        return
    item["price"] = price
    if data.get("pkg_sales_edit_field") == "price":
        await state.update_data(pkg_sales_item=item, pkg_sales_edit_field=None)
    else:
        await state.update_data(pkg_sales_item=item)
    await state.set_state(None)
    await message.answer(package_item_preview_text(item, title="👀 پیش‌نمایش آیتم از پلن فروش"), reply_markup=admin_package_sales_item_review_kb())


@router.callback_query(F.data.startswith("adm_pkg_sales_edit:"))
async def admin_package_sales_edit(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "packages"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    field = callback.data.split(":", 1)[1]
    data = await state.get_data()
    if not data.get("pkg_sales_item"):
        await callback.answer("آیتمی برای ویرایش وجود ندارد.", show_alert=True)
        return
    await state.update_data(pkg_sales_edit_field=field)
    if field == "title":
        await state.set_state(AdminStates.waiting_package_sales_edit_title)
        await edit_or_answer(callback, header("✏️ تغییر عنوان ساب") + "عنوان جدید را وارد کنید.", admin_back_kb("adm_pkg_add_items"))
    elif field == "data":
        await state.set_state(AdminStates.waiting_package_sales_edit_data)
        await edit_or_answer(callback, header("📦 تغییر حجم ساب") + "حجم جدید را به گیگ وارد کنید. مثال: <code>30</code>", admin_back_kb("adm_pkg_add_items"))
    elif field == "days":
        await state.set_state(AdminStates.waiting_package_sales_edit_days)
        await edit_or_answer(callback, header("⏳ تغییر مدت ساب") + "مدت جدید را به روز وارد کنید. مثال: <code>30</code>", admin_back_kb("adm_pkg_add_items"))
    elif field == "price":
        await state.set_state(AdminStates.waiting_package_sales_price)
        await edit_or_answer(callback, header("💰 تغییر قیمت ساب") + "قیمت جدید را به تومان وارد کنید. برای رایگان بودن <code>0</code> بفرستید.", admin_back_kb("adm_pkg_add_items"))
    else:
        await callback.answer("فیلد معتبر نیست.", show_alert=True)


@router.message(AdminStates.waiting_package_sales_edit_title)
async def admin_package_sales_edit_title_step(message: Message, state: FSMContext) -> None:
    ok, title = validate_package_item_title(message.text or "")
    if not ok:
        await message.answer("❌ " + title)
        return
    data = await state.get_data()
    item = dict(data.get("pkg_sales_item") or {})
    item["title"] = title
    item["source_type"] = "sales_plan_override"
    await state.update_data(pkg_sales_item=item, pkg_sales_edit_field=None)
    await state.set_state(None)
    await message.answer(package_item_preview_text(item, title="👀 پیش‌نمایش آیتم از پلن فروش"), reply_markup=admin_package_sales_item_review_kb())


@router.message(AdminStates.waiting_package_sales_edit_data)
async def admin_package_sales_edit_data_step(message: Message, state: FSMContext) -> None:
    ok, data_gb, err = parse_package_data_gb(message.text or "")
    if not ok:
        await message.answer("❌ " + err)
        return
    data = await state.get_data()
    item = dict(data.get("pkg_sales_item") or {})
    item["data_gb"] = data_gb
    item["source_type"] = "sales_plan_override"
    await state.update_data(pkg_sales_item=item, pkg_sales_edit_field=None)
    await state.set_state(None)
    await message.answer(package_item_preview_text(item, title="👀 پیش‌نمایش آیتم از پلن فروش"), reply_markup=admin_package_sales_item_review_kb())


@router.message(AdminStates.waiting_package_sales_edit_days)
async def admin_package_sales_edit_days_step(message: Message, state: FSMContext) -> None:
    ok, days, err = parse_package_days(message.text or "")
    if not ok:
        await message.answer("❌ " + err)
        return
    data = await state.get_data()
    item = dict(data.get("pkg_sales_item") or {})
    item["days"] = days
    item["source_type"] = "sales_plan_override"
    await state.update_data(pkg_sales_item=item, pkg_sales_edit_field=None)
    await state.set_state(None)
    await message.answer(package_item_preview_text(item, title="👀 پیش‌نمایش آیتم از پلن فروش"), reply_markup=admin_package_sales_item_review_kb())


@router.callback_query(F.data == "adm_pkg_sales_reset")
async def admin_package_sales_reset(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "packages"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    data = await state.get_data()
    plan = PLANS.get(str(data.get("pkg_sales_plan_key") or ""))
    item = dict(data.get("pkg_sales_item") or {})
    if not plan or not item:
        await callback.answer("پلن پایه پیدا نشد.", show_alert=True)
        return
    price = int(item.get("price", plan.price))
    item = {"source_type": "sales_plan", "source_plan_key": plan.key, "title": plan.title, "data_gb": plan.data_gb, "days": plan.days, "price": price}
    await state.update_data(pkg_sales_item=item)
    await edit_or_answer(callback, package_item_preview_text(item, title="👀 پیش‌نمایش آیتم از پلن فروش"), admin_package_sales_item_review_kb())


@router.callback_query(F.data == "adm_pkg_sales_save")
async def admin_package_sales_save(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "packages"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    data = await state.get_data()
    item = dict(data.get("pkg_sales_item") or {})
    if not item or "price" not in item:
        await callback.answer("آیتم کامل نیست.", show_alert=True)
        return
    items = list(data.get("pkg_items") or [])
    items.append(item)
    await state.update_data(pkg_items=items, pkg_sales_item=None, pkg_sales_plan_key=None, pkg_sales_edit_field=None)
    await state.set_state(None)
    await edit_or_answer(callback, header("✅ آیتم اضافه شد") + f"{h(item['title'])} — قیمت ساب: {'رایگان' if int(item['price']) == 0 else fmt_money(int(item['price']))}\n\nآیتم بعدی را اضافه کنید یا پکیج را ذخیره کنید.", package_item_add_kb())


@router.callback_query(F.data == "adm_pkg_item_manual")
async def admin_package_item_manual(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "packages"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    await state.update_data(pkg_manual_item={}, pkg_manual_edit_field=None)
    await state.set_state(AdminStates.waiting_package_manual_title)
    await edit_or_answer(callback, header("✍️ ساخت ساب دستی", "مرحله ۱") + "عنوان ساب داخل پکیج را وارد کنید. مثال: <code>ساب دانشجویی</code>", admin_back_kb("adm_pkg_add_items"))


@router.message(AdminStates.waiting_package_manual_title)
async def admin_package_manual_title_step(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "packages"):
        await message.answer("دسترسی ندارید.")
        return
    ok, title = validate_package_item_title(message.text or "")
    if not ok:
        await message.answer("❌ " + title)
        return
    data = await state.get_data()
    item = dict(data.get("pkg_manual_item") or {})
    item.update({"source_type": "manual", "source_plan_key": "", "title": title})
    if data.get("pkg_manual_edit_field") == "title":
        await state.update_data(pkg_manual_item=item, pkg_manual_edit_field=None)
        await state.set_state(None)
        await message.answer(package_item_preview_text(item, title="👀 پیش‌نمایش آیتم دستی"), reply_markup=admin_package_manual_item_review_kb())
    else:
        await state.update_data(pkg_manual_item=item)
        await state.set_state(AdminStates.waiting_package_manual_data)
        await message.answer(header("📦 حجم ساب", "مرحله ۲") + "حجم ساب را به گیگ وارد کنید. مثال: <code>30</code>", reply_markup=admin_back_kb("adm_pkg_add_items"))


@router.message(AdminStates.waiting_package_manual_data)
async def admin_package_manual_data_step(message: Message, state: FSMContext) -> None:
    ok, data_gb, err = parse_package_data_gb(message.text or "")
    if not ok:
        await message.answer("❌ " + err)
        return
    data = await state.get_data()
    item = dict(data.get("pkg_manual_item") or {})
    item["data_gb"] = data_gb
    if data.get("pkg_manual_edit_field") == "data":
        await state.update_data(pkg_manual_item=item, pkg_manual_edit_field=None)
        await state.set_state(None)
        await message.answer(package_item_preview_text(item, title="👀 پیش‌نمایش آیتم دستی"), reply_markup=admin_package_manual_item_review_kb())
    else:
        await state.update_data(pkg_manual_item=item)
        await state.set_state(AdminStates.waiting_package_manual_days)
        await message.answer(header("⏳ مدت ساب", "مرحله ۳") + "مدت اعتبار را به روز وارد کنید. مثال: <code>30</code>", reply_markup=admin_back_kb("adm_pkg_add_items"))


@router.message(AdminStates.waiting_package_manual_days)
async def admin_package_manual_days_step(message: Message, state: FSMContext) -> None:
    ok, days, err = parse_package_days(message.text or "")
    if not ok:
        await message.answer("❌ " + err)
        return
    data = await state.get_data()
    item = dict(data.get("pkg_manual_item") or {})
    item["days"] = days
    if data.get("pkg_manual_edit_field") == "days":
        await state.update_data(pkg_manual_item=item, pkg_manual_edit_field=None)
        await state.set_state(None)
        await message.answer(package_item_preview_text(item, title="👀 پیش‌نمایش آیتم دستی"), reply_markup=admin_package_manual_item_review_kb())
    else:
        await state.update_data(pkg_manual_item=item)
        await state.set_state(AdminStates.waiting_package_manual_price)
        await message.answer(header("💰 قیمت ساب", "مرحله ۴") + "قیمت این ساب داخل پکیج را به تومان وارد کنید. برای رایگان بودن <code>0</code> بفرستید.", reply_markup=admin_back_kb("adm_pkg_add_items"))


@router.message(AdminStates.waiting_package_manual_price)
async def admin_package_manual_price_step(message: Message, state: FSMContext) -> None:
    ok, price, err = parse_positive_amount(message.text or "", allow_zero=True)
    if not ok:
        await message.answer("❌ " + err)
        return
    data = await state.get_data()
    item = dict(data.get("pkg_manual_item") or {})
    item["price"] = price
    await state.update_data(pkg_manual_item=item, pkg_manual_edit_field=None)
    await state.set_state(None)
    await message.answer(package_item_preview_text(item, title="👀 پیش‌نمایش آیتم دستی"), reply_markup=admin_package_manual_item_review_kb())


@router.callback_query(F.data.startswith("adm_pkg_manual_edit:"))
async def admin_package_manual_edit(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "packages"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    field = callback.data.split(":", 1)[1]
    data = await state.get_data()
    if not data.get("pkg_manual_item"):
        await callback.answer("آیتم دستی برای ویرایش وجود ندارد.", show_alert=True)
        return
    await state.update_data(pkg_manual_edit_field=field)
    if field == "title":
        await state.set_state(AdminStates.waiting_package_manual_title)
        await edit_or_answer(callback, header("✏️ تغییر عنوان ساب") + "عنوان جدید را وارد کنید.", admin_back_kb("adm_pkg_add_items"))
    elif field == "data":
        await state.set_state(AdminStates.waiting_package_manual_data)
        await edit_or_answer(callback, header("📦 تغییر حجم ساب") + "حجم جدید را به گیگ وارد کنید.", admin_back_kb("adm_pkg_add_items"))
    elif field == "days":
        await state.set_state(AdminStates.waiting_package_manual_days)
        await edit_or_answer(callback, header("⏳ تغییر مدت ساب") + "مدت جدید را به روز وارد کنید.", admin_back_kb("adm_pkg_add_items"))
    elif field == "price":
        await state.set_state(AdminStates.waiting_package_manual_price)
        await edit_or_answer(callback, header("💰 تغییر قیمت ساب") + "قیمت جدید را به تومان وارد کنید. برای رایگان بودن <code>0</code> بفرستید.", admin_back_kb("adm_pkg_add_items"))
    else:
        await callback.answer("فیلد معتبر نیست.", show_alert=True)


@router.callback_query(F.data == "adm_pkg_manual_save")
async def admin_package_manual_save(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "packages"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    data = await state.get_data()
    item = dict(data.get("pkg_manual_item") or {})
    for key in ("title", "data_gb", "days", "price"):
        if key not in item:
            await callback.answer("آیتم دستی هنوز کامل نیست.", show_alert=True)
            return
    items = list(data.get("pkg_items") or [])
    items.append(item)
    await state.update_data(pkg_items=items, pkg_manual_item=None, pkg_manual_edit_field=None)
    await state.set_state(None)
    await edit_or_answer(callback, header("✅ آیتم دستی اضافه شد") + f"{h(item['title'])} — قیمت ساب: {'رایگان' if int(item['price']) == 0 else fmt_money(int(item['price']))}\n\nآیتم بعدی را اضافه کنید یا پکیج را ذخیره کنید.", package_item_add_kb())


@router.callback_query(F.data == "adm_pkg_item_finish")
async def admin_package_finish(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "packages"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    data = await state.get_data()
    items = list(data.get("pkg_items") or [])
    if not items:
        await callback.answer("حداقل یک پلن/ساب باید داخل پکیج اضافه شود.", show_alert=True)
        return
    required = ["pkg_name", "pkg_price", "pkg_code", "pkg_max_subscriptions"]
    if any(k not in data for k in required):
        await callback.answer("اطلاعات پکیج ناقص است؛ دوباره بسازید.", show_alert=True)
        await state.clear()
        return
    pkg_data = {
        "name": data["pkg_name"],
        "price": int(data["pkg_price"]),
        "code": data["pkg_code"],
        "description": data.get("pkg_description") or "",
        "conditions": data.get("pkg_conditions") or "",
        "max_subscriptions": int(data["pkg_max_subscriptions"]),
    }
    package_id = create_package_template(pkg_data, items, callback.from_user.id)
    admin_log(callback.from_user.id, "PACKAGE_CREATE", "package", package_id, f"code={pkg_data['code']}; items={len(items)}")
    await state.clear()
    package = package_by_id(package_id)
    await edit_or_answer(callback, header("✅ پکیج ساخته شد") + package_text(package), admin_package_kb(package_id))


@router.callback_query(F.data == "adm_pkg_assign_start")
async def admin_package_assign_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "packages"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_package_assign_user)
    await state.update_data(assign_package_id=None)
    await edit_or_answer(callback, header("🎯 اختصاص پکیج") + "چت‌آیدی کاربر را وارد کنید.", admin_back_kb("adm_packages"))


@router.callback_query(F.data.startswith("adm_pkg_assign_pick:"))
async def admin_package_assign_pick(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "packages"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    data = await state.get_data()
    uid = int(data.get("assign_user_id", 0))
    package_id = int(callback.data.split(":", 1)[1])
    if not uid or not db.get_user(uid):
        await callback.answer("کاربر مقصد مشخص نیست؛ دوباره شروع کنید.", show_alert=True)
        return
    assignment_id = create_user_package_assignment(package_id, uid, callback.from_user.id, status="draft")
    up = user_package_by_id(assignment_id)
    await edit_or_answer(callback, header("👀 پیش‌نمایش اختصاص پکیج") + user_package_text(up, user_view=False), admin_package_assignment_preview_kb(assignment_id))


@router.callback_query(F.data.startswith("adm_pkg_assign_user:"))
async def admin_package_assign_from_user(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "packages"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    uid = int(callback.data.split(":", 1)[1])
    packages = list_package_templates(True)
    await state.set_state(None)
    await state.update_data(assign_user_id=uid)
    await edit_or_answer(callback, header("🎯 انتخاب پکیج", str(uid)) + "پکیجی که می‌خواهید به این کاربر اختصاص دهید را انتخاب کنید.", admin_package_assign_select_kb(packages, f"adm_user:{uid}"))


@router.callback_query(F.data.startswith("adm_pkg_assign:"))
async def admin_package_assign_specific(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "packages"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    package_id = int(callback.data.split(":", 1)[1])
    await state.set_state(AdminStates.waiting_package_assign_user)
    await state.update_data(assign_package_id=package_id)
    await edit_or_answer(callback, header("🎯 اختصاص پکیج") + "چت‌آیدی کاربر را وارد کنید.", admin_back_kb(f"adm_pkg:{package_id}"))


@router.message(AdminStates.waiting_package_assign_user)
async def admin_package_assign_user_step(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "packages"):
        await message.answer("دسترسی ندارید.")
        return
    raw = normalize_digits(message.text or "").strip()
    if not raw.lstrip("-").isdigit():
        await message.answer("❌ چت‌آیدی معتبر نیست.")
        return
    uid = int(raw)
    user = db.get_user(uid)
    if not user:
        await message.answer("❌ این کاربر هنوز در دیتابیس ربات ثبت نشده است. اول باید ربات را start کرده باشد.")
        return
    data = await state.get_data()
    package_id = data.get("assign_package_id")
    packages = list_package_templates(True)
    await state.set_state(None)
    await state.update_data(assign_user_id=uid)
    if package_id:
        assignment_id = create_user_package_assignment(int(package_id), uid, message.from_user.id, status="draft")
        up = user_package_by_id(assignment_id)
        await message.answer(header("👀 پیش‌نمایش اختصاص پکیج") + user_package_text(up, user_view=False), reply_markup=admin_package_assignment_preview_kb(assignment_id))
    else:
        await message.answer(header("🎯 انتخاب پکیج", str(uid)) + "پکیجی که می‌خواهید به این کاربر اختصاص دهید را انتخاب کنید.", reply_markup=admin_package_assign_select_kb(packages, "adm_packages"))


@router.callback_query(F.data.startswith("adm_userpkg_custom:"))
async def admin_userpkg_custom(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "packages"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    user_package_id = int(callback.data.split(":", 1)[1])
    up = user_package_by_id(user_package_id)
    if not up or str(up["status"]) != "draft":
        await callback.answer("این اختصاص قابل ویرایش نیست.", show_alert=True)
        return
    await state.clear()
    await edit_or_answer(
        callback,
        header("✏️ کاستوم پکیج برای همین کاربر")
        + user_package_text(up, user_view=False)
        + "\nهر بخشی را که لازم است با دکمه جدا ویرایش کنید. هیچ فرمت چندبخشی لازم نیست.",
        admin_user_package_custom_kb(user_package_id),
    )


@router.callback_query(F.data.startswith("adm_userpkg_edit:"))
async def admin_userpkg_edit_field(callback: CallbackQuery, state: FSMContext) -> None:
    if not require_admin_id(callback.from_user.id, "packages"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    try:
        _tag, upid_s, field = callback.data.split(":")
        upid = int(upid_s)
    except Exception:
        await callback.answer("اطلاعات ویرایش معتبر نیست.", show_alert=True)
        return
    up = user_package_by_id(upid)
    if not up or str(up["status"]) != "draft":
        await callback.answer("این اختصاص قابل ویرایش نیست.", show_alert=True)
        return
    await state.update_data(custom_user_package_id=upid)
    if field == "price":
        await state.set_state(AdminStates.waiting_package_custom_price)
        await edit_or_answer(callback, header("💰 تغییر قیمت پک") + "قیمت نهایی پک برای همین کاربر را به تومان وارد کنید. برای رایگان بودن <code>0</code> بفرستید.", admin_back_kb(f"adm_userpkg_custom:{upid}"))
    elif field == "max_subs":
        await state.set_state(AdminStates.waiting_package_custom_max_subs)
        await edit_or_answer(callback, header("🔢 تغییر تعداد ساب") + "تعداد ساب قابل ساخت برای همین کاربر را وارد کنید. مثال: <code>5</code>", admin_back_kb(f"adm_userpkg_custom:{upid}"))
    elif field == "description":
        await state.set_state(AdminStates.waiting_package_custom_description)
        await edit_or_answer(callback, header("📝 تغییر توضیح پک") + "توضیح نهایی برای همین کاربر را بفرستید. برای خالی‌کردن توضیح <code>-</code> بفرستید.", admin_back_kb(f"adm_userpkg_custom:{upid}"))
    elif field == "conditions":
        await state.set_state(AdminStates.waiting_package_custom_conditions)
        await edit_or_answer(callback, header("📌 تغییر شرایط پک") + "شرایط نهایی برای همین کاربر را بفرستید. برای خالی‌کردن شرایط <code>-</code> بفرستید.", admin_back_kb(f"adm_userpkg_custom:{upid}"))
    elif field == "code":
        await state.set_state(AdminStates.waiting_package_custom_code)
        await edit_or_answer(callback, header("🧾 تغییر کد اختصاصی") + "کد اختصاصی این کاربر را وارد کنید. فقط حروف انگلیسی، عدد، خط تیره و آندرلاین مجاز است.", admin_back_kb(f"adm_userpkg_custom:{upid}"))
    else:
        await callback.answer("فیلد معتبر نیست.", show_alert=True)


async def _finish_userpkg_custom_message(message: Message, state: FSMContext, user_package_id: int, title: str = "✅ تغییر ذخیره شد") -> None:
    await state.clear()
    up = user_package_by_id(user_package_id)
    await message.answer(header(title) + user_package_text(up, user_view=False), reply_markup=admin_user_package_custom_kb(user_package_id))


@router.message(AdminStates.waiting_package_custom_price)
async def admin_userpkg_custom_price_step(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "packages"):
        await message.answer("دسترسی ندارید.")
        return
    data = await state.get_data()
    upid = int(data.get("custom_user_package_id", 0))
    ok, amount, err = parse_positive_amount(message.text or "", allow_zero=True)
    if not ok:
        await message.answer("❌ " + err)
        return
    update_user_package(upid, price=amount)
    await _finish_userpkg_custom_message(message, state, upid)


@router.message(AdminStates.waiting_package_custom_max_subs)
async def admin_userpkg_custom_max_subs_step(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "packages"):
        await message.answer("دسترسی ندارید.")
        return
    data = await state.get_data()
    upid = int(data.get("custom_user_package_id", 0))
    ok, count, err = parse_max_subscriptions(message.text or "")
    if not ok:
        await message.answer("❌ " + err)
        return
    up = user_package_by_id(upid)
    used = count_package_subscriptions(upid) if up else 0
    if count < used:
        await message.answer(f"❌ این کاربر همین حالا {fmt_number(used)} ساب ساخته؛ تعداد جدید نمی‌تواند کمتر از مقدار استفاده‌شده باشد.")
        return
    update_user_package(upid, max_subscriptions=count)
    await _finish_userpkg_custom_message(message, state, upid)


@router.message(AdminStates.waiting_package_custom_description)
async def admin_userpkg_custom_description_step(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "packages"):
        await message.answer("دسترسی ندارید.")
        return
    data = await state.get_data()
    upid = int(data.get("custom_user_package_id", 0))
    ok, value = validate_long_text(message.text or "", "توضیح", allow_empty=True)
    if not ok:
        await message.answer("❌ " + value)
        return
    update_user_package(upid, description=value)
    await _finish_userpkg_custom_message(message, state, upid)


@router.message(AdminStates.waiting_package_custom_conditions)
async def admin_userpkg_custom_conditions_step(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "packages"):
        await message.answer("دسترسی ندارید.")
        return
    data = await state.get_data()
    upid = int(data.get("custom_user_package_id", 0))
    ok, value = validate_long_text(message.text or "", "شرایط", allow_empty=True)
    if not ok:
        await message.answer("❌ " + value)
        return
    update_user_package(upid, conditions=value)
    await _finish_userpkg_custom_message(message, state, upid)


@router.message(AdminStates.waiting_package_custom_code)
async def admin_userpkg_custom_code_step(message: Message, state: FSMContext) -> None:
    if not require_admin_id(message.from_user.id if message.from_user else 0, "packages"):
        await message.answer("دسترسی ندارید.")
        return
    data = await state.get_data()
    upid = int(data.get("custom_user_package_id", 0))
    ok, value = validate_user_package_code(message.text or "", upid)
    if not ok:
        await message.answer("❌ " + value)
        return
    update_user_package(upid, code=value)
    await _finish_userpkg_custom_message(message, state, upid)


@router.callback_query(F.data.startswith("adm_userpkg_preview:"))
async def admin_userpkg_preview(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "packages"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    upid = int(callback.data.split(":", 1)[1])
    up = user_package_by_id(upid)
    if not up:
        await callback.answer("اختصاص پیدا نشد.", show_alert=True)
        return
    await edit_or_answer(callback, header("👀 پیش‌نمایش اختصاص پکیج") + user_package_text(up, user_view=False), admin_package_assignment_preview_kb(upid))


@router.callback_query(F.data.startswith("adm_userpkg_cancel:"))
async def admin_userpkg_cancel(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "packages"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    upid = int(callback.data.split(":", 1)[1])
    update_user_package(upid, status="cancelled")
    await edit_or_answer(callback, header("❌ اختصاص لغو شد"), admin_packages_kb())


@router.callback_query(F.data.startswith("adm_userpkg_send:"))
async def admin_userpkg_send(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "packages"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    upid = int(callback.data.split(":", 1)[1])
    up = user_package_by_id(upid)
    if not up or str(up["status"]) != "draft":
        await callback.answer("این پکیج قابل ارسال نیست.", show_alert=True)
        return
    update_user_package(upid, status="offered", offered_at=now_iso())
    up = user_package_by_id(upid)
    try:
        await callback.bot.send_message(int(up["user_telegram_id"]), header("🎁 یک پکیج اختصاصی برای شما درنظر گرفته شد") + user_package_text(up), reply_markup=user_package_view_kb(up))
    except Exception as exc:
        logger.warning("Failed to send package offer %s: %s", upid, exc)
        await callback.answer("ارسال پیام به کاربر ناموفق بود؛ شاید ربات را بلاک کرده باشد.", show_alert=True)
        return
    admin_log(callback.from_user.id, "PACKAGE_ASSIGN_SEND", "user_package", upid, f"user={up['user_telegram_id']}")
    await edit_or_answer(callback, header("✅ پیشنهاد پکیج ارسال شد") + user_package_text(up, user_view=False), admin_back_kb("adm_packages"))


@router.callback_query(F.data == "adm_userpkg_all")
async def admin_all_user_packages(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "packages"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    rows = list_user_package_assignments(limit=50)
    text = header("👥 همه پکیج‌های اختصاصی")
    if not rows:
        text += "هنوز هیچ پکیج اختصاصی برای کاربران ثبت نشده است."
    else:
        text += "هر مورد را برای مدیریت کامل انتخاب کنید."
    await edit_or_answer(callback, text, admin_user_package_list_kb(rows, "adm_packages"))


@router.callback_query(F.data.startswith("adm_user_packages:"))
async def admin_user_packages(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "packages"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    uid = int(callback.data.split(":", 1)[1])
    rows = list_user_package_assignments(user_telegram_id=uid, limit=50)
    text = header("🎁 پکیج‌های اختصاصی کاربر", str(uid))
    if not rows:
        text += "برای این کاربر هنوز پکیج اختصاصی ثبت نشده است."
    else:
        text += "هر پکیج را برای مدیریت، گرفتن از کاربر یا مشاهده ساب‌ها انتخاب کنید."
    await edit_or_answer(callback, text, admin_user_package_list_kb(rows, f"adm_user:{uid}"))


@router.callback_query(F.data.startswith("adm_userpkg:"))
async def admin_user_package_detail(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "packages"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    upid = int(callback.data.split(":", 1)[1])
    up = user_package_by_id(upid)
    if not up:
        await callback.answer("پکیج اختصاصی پیدا نشد.", show_alert=True)
        return
    text = header("🛠 مدیریت پکیج اختصاصی") + user_package_text(up, user_view=False)
    text += "\nاز اینجا می‌توانید پک را از کاربر بگیرید، ساب‌های ساخته‌شده را ببینید، یا در صورت نیاز ساب‌ها را هم حذف/غیرفعال کنید."
    await edit_or_answer(callback, text, admin_user_package_manage_kb(up))


@router.callback_query(F.data.startswith("adm_userpkg_subs:"))
async def admin_user_package_subscriptions(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "packages"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    upid = int(callback.data.split(":", 1)[1])
    up = user_package_by_id(upid)
    if not up:
        await callback.answer("پکیج اختصاصی پیدا نشد.", show_alert=True)
        return
    details = package_subscription_details(upid)
    text = header("🧩 ساب‌های ساخته‌شده از پکیج")
    text += user_package_text(up, user_view=False) + "\n"
    if not details:
        text += "\nبرای این پکیج هنوز هیچ سابی ساخته نشده است."
    else:
        text += "\n📦 ساب‌ها / سرویس‌ها:\n"
        for sub, service, item in details:
            if service:
                item_title = item["title"] if item else "آیتم نامشخص"
                text += f"• سرویس #{service['id']} | {h(item_title)} | وضعیت: <b>{h(service['status'])}</b> | نام: <code>{h(service['name'])}</code>\n"
            else:
                text += f"• رکورد ساب #{sub['id']} | سرویس حذف/ناموجود #{sub['service_id']}\n"
    await edit_or_answer(callback, text, admin_user_package_subs_kb(upid, details))


@router.callback_query(F.data.startswith("adm_userpkg_revoke:"))
async def admin_user_package_revoke(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "packages"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    try:
        _prefix, upid_s, action, notify_mode = callback.data.split(":", 3)
        upid = int(upid_s)
    except Exception:
        await callback.answer("درخواست نامعتبر است.", show_alert=True)
        return
    up = user_package_by_id(upid)
    if not up:
        await callback.answer("پکیج اختصاصی پیدا نشد.", show_alert=True)
        return
    if str(up["status"]) not in {"offered", "pending_payment", "active"}:
        await callback.answer("این پکیج در وضعیت قابل پس‌گرفتن نیست.", show_alert=True)
        return
    delete_subs = action == "delete"
    affected_before = revoke_user_package_local(upid, delete_subscriptions=delete_subs, admin_id=callback.from_user.id)
    remote_notices: list[str] = []
    if delete_subs:
        for old_service in affected_before:
            service = db.get_service(int(old_service["id"]))
            if service:
                try:
                    result = await set_remote_user_status(db, service, "deleted")
                    if result:
                        remote_notices.append(result.notice())
                except Exception as exc:
                    remote_notices.append(f"\n⚠️ Pasarguard service #{old_service['id']}: {h(exc)}")
    up_after = user_package_by_id(upid) or up
    if notify_mode == "notify":
        try:
            if delete_subs:
                msg = header("⛔ پکیج اختصاصی شما غیرفعال شد") + "این پکیج از حساب شما برداشته شد و ساب‌های ساخته‌شده از این پکیج هم غیرفعال شدند."
            else:
                msg = header("⛔ پکیج اختصاصی شما برداشته شد") + "این پکیج دیگر برای شما قابل استفاده نیست؛ اما ساب‌هایی که قبلاً از آن ساخته‌اید دست‌نخورده باقی می‌مانند."
            await callback.bot.send_message(int(up["user_telegram_id"]), msg)
        except Exception as exc:
            logger.warning("Failed to notify user package revoke %s: %s", upid, exc)
            await callback.answer("پک پس گرفته شد، اما اطلاع‌رسانی به کاربر ناموفق بود.", show_alert=True)
    admin_log(callback.from_user.id, "USER_PACKAGE_REVOKE", "user_package", upid, f"delete_subs={delete_subs}; notify={notify_mode}")
    text = header("✅ پکیج از کاربر گرفته شد") + user_package_text(up_after, user_view=False)
    if delete_subs:
        text += f"\n🗑 تعداد ساب/سرویس غیرفعال‌شده: <b>{fmt_number(len(affected_before))}</b>"
    else:
        text += "\n📦 ساب‌های ساخته‌شده قبلی دست‌نخورده باقی ماندند."
    if remote_notices:
        text += "\n" + "".join(remote_notices[:8])
    await edit_or_answer(callback, text, admin_user_package_manage_kb(up_after))


@router.callback_query(F.data.startswith("adm_pkg_assignments:"))
async def admin_package_assignments(callback: CallbackQuery) -> None:
    if not require_admin_id(callback.from_user.id, "packages"):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    package_id = int(callback.data.split(":", 1)[1])
    rows = list_user_package_assignments(package_id=package_id, limit=50)
    text = header("👥 اختصاص‌های پکیج")
    if not rows:
        text += "هنوز برای این پکیج اختصاصی ثبت نشده است."
    else:
        text += "هر مورد را برای مدیریت کامل، مشاهده ساب‌ها یا گرفتن پک از کاربر انتخاب کنید."
    await edit_or_answer(callback, text, admin_user_package_list_kb(rows, f"adm_pkg:{package_id}"))


@router.message(F.text == "🎁 پکیج‌های من")
async def my_packages_msg(message: Message) -> None:
    user = ensure_from_message(message)
    packages = list_user_packages(int(user["telegram_id"]), include_old=False)
    if not packages:
        await message.answer("فعلاً هیچ پکیج اختصاصی فعالی برای شما ثبت نشده است.", reply_markup=main_menu_kb(int(user["telegram_id"])))
        return
    await message.answer(header("🎁 پکیج‌های من") + "یکی از پکیج‌ها را انتخاب کنید.", reply_markup=user_packages_kb(packages))


@router.callback_query(F.data == "my_packages")
async def my_packages_cb(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    packages = list_user_packages(int(user["telegram_id"]), include_old=False)
    if not packages:
        await edit_or_answer(callback, "فعلاً هیچ پکیج اختصاصی فعالی برای شما ثبت نشده است.", back_home_kb())
        return
    await edit_or_answer(callback, header("🎁 پکیج‌های من") + "یکی از پکیج‌ها را انتخاب کنید.", user_packages_kb(packages))


@router.callback_query(F.data.startswith("pkg_view:"))
async def user_package_view(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    upid = int(callback.data.split(":", 1)[1])
    up = user_package_by_id(upid, int(user["telegram_id"]))
    if not up or str(up["status"]) not in {"offered", "pending_payment", "active"}:
        await callback.answer("این پکیج دیگر برای شما فعال یا قابل مشاهده نیست.", show_alert=True)
        return
    await edit_or_answer(callback, user_package_text(up), user_package_view_kb(up))


@router.callback_query(F.data.startswith("pkg_decline:"))
async def user_package_decline(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    upid = int(callback.data.split(":", 1)[1])
    up = user_package_by_id(upid, int(user["telegram_id"]))
    if not up or str(up["status"]) != "offered":
        await callback.answer("این پیشنهاد قابل رد کردن نیست.", show_alert=True)
        return
    update_user_package(upid, status="declined")
    up = user_package_by_id(upid, int(user["telegram_id"]))
    await edit_or_answer(callback, header("❌ پکیج رد شد") + "این پیشنهاد برای شما رد شد.", user_package_view_kb(up))


@router.callback_query(F.data.startswith("pkg_accept:"))
async def user_package_accept(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    telegram_id = int(user["telegram_id"])
    upid = int(callback.data.split(":", 1)[1])
    up = user_package_by_id(upid, telegram_id)
    if not up or str(up["status"]) not in {"offered", "pending_payment"}:
        await callback.answer("این پکیج قابل تأیید نیست.", show_alert=True)
        return
    if up["order_id"]:
        await show_order_payment(callback, telegram_id, int(up["order_id"]))
        return
    order_id = db.create_order(telegram_id, f"pkg_assign:{upid}", int(up["price"]), 0, 0, "pending", "none")
    update_user_package(upid, status="pending_payment", order_id=order_id)
    await show_order_payment(callback, telegram_id, order_id)


@router.callback_query(F.data.startswith("pkg_subs:"))
async def user_package_subs(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    upid = int(callback.data.split(":", 1)[1])
    up = user_package_by_id(upid, int(user["telegram_id"]))
    if not up or str(up["status"]) != "active":
        await callback.answer("این پکیج فعال نیست.", show_alert=True)
        return
    used = count_package_subscriptions(upid)
    if used >= int(up["max_subscriptions"]):
        await callback.answer("ظرفیت ساخت ساب برای این پکیج تکمیل شده است.", show_alert=True)
        return
    await edit_or_answer(callback, header("➕ ساخت ساب از پکیج") + f"استفاده‌شده: <b>{fmt_number(used)}</b> از <b>{fmt_number(int(up['max_subscriptions']))}</b>\n\nیکی از پلن‌های داخل پک را انتخاب کنید.", package_sub_items_kb(upid))


@router.callback_query(F.data.startswith("pkg_make_sub:"))
async def user_package_make_sub(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    telegram_id = int(user["telegram_id"])
    try:
        _tag, upid_s, item_id_s = callback.data.split(":")
        upid = int(upid_s)
        item_id = int(item_id_s)
    except Exception:
        await callback.answer("اطلاعات معتبر نیست.", show_alert=True)
        return
    up = user_package_by_id(upid, telegram_id)
    item = package_item_by_id(item_id)
    if not up or str(up["status"]) != "active":
        await callback.answer("این پکیج فعال نیست.", show_alert=True)
        return
    if not item or int(item["package_id"]) != int(up["package_id"]):
        await callback.answer("این پلن داخل پکیج شما نیست.", show_alert=True)
        return
    if count_package_subscriptions(upid) >= int(up["max_subscriptions"]):
        await callback.answer("ظرفیت ساخت ساب برای این پکیج تکمیل شده است.", show_alert=True)
        return
    order_id = db.create_order(telegram_id, f"pkg_sub:{upid}:{item_id}", int(item["price"]), 0, 0, "pending", "none")
    await show_order_payment(callback, telegram_id, order_id)

@router.message()
async def unknown(message: Message, state: FSMContext) -> None:
    ensure_from_message(message)
    await message.answer("گزینه موردنظر را از منوی پایین انتخاب کنید 👇", reply_markup=main_menu_kb(message.from_user.id if message.from_user else None))


async def main() -> None:
    await bootstrap_phase1()
    # Startup catch-up: deadlines and scheduled jobs that became due while the bot was down
    # must be finalized before users/admins interact with stale orders, receipts or tickets.
    await run_deadline_cleanup_once()
    await run_due_jobs_once()
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
    job_scheduler_task = start_job_scheduler()
    logger.info("Bot started: @%s", BOT_USERNAME)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        auto_backup_task.cancel()
        job_scheduler_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
