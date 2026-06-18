import asyncio
import html
import logging
import os
import random
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

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
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


SERVICE_NAME_PREFIX = "howtosee_"


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
    "standard": {
        "title": "🌍 سرویس رایگان استاندارد",
        "subtitle": "برای تست اتصال روزمره",
    },
    "speed": {
        "title": "⚡ سرویس رایگان پرسرعت",
        "subtitle": "برای تست سرعت و پایداری",
    },
}


FREE_TEST_PLANS: dict[str, Plan] = {
    "free_standard_150": Plan("free_standard_150", f"{FREE_TEST_MB} مگابایت | رایگان استاندارد", FREE_TEST_MB / 1024, 3, 0, "free:standard", "رایگان"),
    "free_speed_150": Plan("free_speed_150", f"{FREE_TEST_MB} مگابایت | رایگان پرسرعت", FREE_TEST_MB / 1024, 3, 0, "free:speed", "رایگان"),
}


# -----------------------------
# Text helpers
# -----------------------------
def now_iso() -> str:
    return datetime.now(TEHRAN_TZ).isoformat(timespec="seconds")


def fmt_money(amount: int) -> str:
    return f"{amount:,}".replace(",", "٬") + " تومان"


def fmt_number(value: int | float) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{value:,}".replace(",", "٬")


def clean_username(value: str) -> str:
    allowed = string.ascii_letters + string.digits + "_-"
    cleaned = "".join(ch for ch in value.strip() if ch in allowed)
    return cleaned[:32]


def service_name_with_prefix(raw: str) -> str:
    cleaned = clean_username(raw)
    if not cleaned:
        return ""
    if cleaned.lower().startswith(SERVICE_NAME_PREFIX):
        return cleaned[:32]
    return f"{SERVICE_NAME_PREFIX}{cleaned}"[:32]


def make_token() -> str:
    return secrets.token_urlsafe(14).replace("-", "").replace("_", "")[:20]


def make_service_name(telegram_id: int) -> str:
    suffix = str(telegram_id)[-5:]
    return f"{SERVICE_NAME_PREFIX}{suffix}_{random.randint(10, 99)}"


def h(value: Any) -> str:
    return html.escape(str(value or ""))


def header(title: str, subtitle: str = "") -> str:
    sub = f"\n<code>{h(subtitle)}</code>" if subtitle else ""
    return f"<b>{h(title)}</b>{sub}\n\n"


def divider() -> str:
    return "━━━━━━━━━━━━━━\n"


# -----------------------------
# Database
# -----------------------------
class DB:
    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True) if Path(path).parent != Path(".") else None
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

    def ensure_user(
        self,
        telegram_id: int,
        username: Optional[str],
        first_name: Optional[str],
        referred_by_telegram_id: Optional[int] = None,
    ) -> sqlite3.Row:
        # Anti self-referral: a user must never be saved as their own referrer.
        if referred_by_telegram_id == telegram_id:
            referred_by_telegram_id = None

        user = self.get_user(telegram_id)
        if user:
            with closing(self.connect()) as conn:
                conn.execute(
                    "UPDATE users SET username = ?, first_name = ? WHERE telegram_id = ?",
                    (username, first_name, telegram_id),
                )
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
                    """
                    INSERT OR IGNORE INTO referrals (referrer_telegram_id, referred_telegram_id, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (referred_by_telegram_id, telegram_id, now_iso()),
                )
            conn.commit()
        return self.get_user(telegram_id)  # type: ignore[return-value]

    def add_wallet(self, telegram_id: int, amount: int, tx_type: str, description: str, related_user_id: Optional[int] = None) -> None:
        with closing(self.connect()) as conn:
            conn.execute(
                "UPDATE users SET wallet_balance = wallet_balance + ? WHERE telegram_id = ?",
                (amount, telegram_id),
            )
            if amount > 0 and tx_type == "referral_commission":
                conn.execute(
                    "UPDATE users SET total_referral_earned = total_referral_earned + ? WHERE telegram_id = ?",
                    (amount, telegram_id),
                )
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

    def update_order_service(self, order_id: int, service_id: int) -> None:
        with closing(self.connect()) as conn:
            conn.execute("UPDATE orders SET service_id = ? WHERE id = ?", (service_id, order_id))
            conn.commit()

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
                (
                    telegram_id,
                    name,
                    plan.key,
                    plan.title,
                    plan.data_gb,
                    plan.days,
                    plan.price,
                    paid_amount,
                    token,
                    expires.isoformat(timespec="seconds"),
                    1 if is_test else 0,
                    now_iso(),
                ),
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
            conn.execute(
                """
                UPDATE services
                SET data_gb = data_gb + ?, status = 'active'
                WHERE id = ? AND user_telegram_id = ?
                """,
                (data_gb, service_id, telegram_id),
            )
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
                (
                    plan.key,
                    plan.title,
                    plan.data_gb,
                    plan.days,
                    plan.price,
                    paid_amount,
                    expires.isoformat(timespec="seconds"),
                    service_id,
                    telegram_id,
                ),
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
            return list(conn.execute(
                "SELECT * FROM services WHERE user_telegram_id = ? ORDER BY id DESC",
                (telegram_id,),
            ).fetchall())

    def list_orders(self, telegram_id: int, limit: int = 7) -> list[sqlite3.Row]:
        with closing(self.connect()) as conn:
            return list(conn.execute(
                "SELECT * FROM orders WHERE user_telegram_id = ? ORDER BY id DESC LIMIT ?",
                (telegram_id, limit),
            ).fetchall())

    def list_wallet_transactions(self, telegram_id: int, limit: int = 5) -> list[sqlite3.Row]:
        with closing(self.connect()) as conn:
            return list(conn.execute(
                "SELECT * FROM wallet_transactions WHERE user_telegram_id = ? ORDER BY id DESC LIMIT ?",
                (telegram_id, limit),
            ).fetchall())

    def referral_stats(self, telegram_id: int) -> dict[str, int]:
        with closing(self.connect()) as conn:
            total_invited = conn.execute(
                "SELECT COUNT(*) AS c FROM referrals WHERE referrer_telegram_id = ?",
                (telegram_id,),
            ).fetchone()["c"]
            rewarded = conn.execute(
                "SELECT COUNT(*) AS c FROM referrals WHERE referrer_telegram_id = ? AND rewarded = 1",
                (telegram_id,),
            ).fetchone()["c"]
            pending = total_invited - rewarded
            earned = conn.execute(
                "SELECT COALESCE(SUM(commission_amount), 0) AS s FROM referrals WHERE referrer_telegram_id = ? AND rewarded = 1",
                (telegram_id,),
            ).fetchone()["s"]
            return {"total": int(total_invited), "rewarded": int(rewarded), "pending": int(pending), "earned": int(earned)}

    def has_referral_rewarded(self, referred_telegram_id: int) -> bool:
        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT rewarded FROM referrals WHERE referred_telegram_id = ?",
                (referred_telegram_id,),
            ).fetchone()
            return bool(row and row["rewarded"])

    def reward_referrer_if_needed(self, buyer_telegram_id: int, order_id: int, paid_amount: int) -> Optional[tuple[int, int]]:
        with closing(self.connect()) as conn:
            ref = conn.execute(
                "SELECT * FROM referrals WHERE referred_telegram_id = ? AND rewarded = 0",
                (buyer_telegram_id,),
            ).fetchone()
            if not ref:
                return None
            referrer_id = int(ref["referrer_telegram_id"])

            # Defense-in-depth: even if a bad/self-referral row is inserted manually,
            # never pay commission to the buyer themselves.
            if referrer_id == buyer_telegram_id:
                conn.execute("DELETE FROM referrals WHERE referred_telegram_id = ?", (buyer_telegram_id,))
                conn.execute("UPDATE users SET referred_by_telegram_id = NULL WHERE telegram_id = ?", (buyer_telegram_id,))
                conn.commit()
                return None

            commission = int(paid_amount * REFERRAL_COMMISSION_PERCENT / 100)
            if commission <= 0:
                return None
            conn.execute(
                "UPDATE users SET wallet_balance = wallet_balance + ?, total_referral_earned = total_referral_earned + ? WHERE telegram_id = ?",
                (commission, commission, referrer_id),
            )
            conn.execute(
                """
                INSERT INTO wallet_transactions (user_telegram_id, amount, type, description, related_user_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    referrer_id,
                    commission,
                    "referral_commission",
                    f"پورسانت خرید اول کاربر {buyer_telegram_id}",
                    buyer_telegram_id,
                    now_iso(),
                ),
            )
            conn.execute(
                """
                UPDATE referrals
                SET rewarded = 1, first_order_id = ?, commission_amount = ?, rewarded_at = ?
                WHERE referred_telegram_id = ?
                """,
                (order_id, commission, now_iso(), buyer_telegram_id),
            )
            conn.commit()
            return referrer_id, commission

    def revoke_service_link(self, service_id: int, telegram_id: int) -> str:
        token = make_token()
        with closing(self.connect()) as conn:
            conn.execute(
                "UPDATE services SET token = ? WHERE id = ? AND user_telegram_id = ?",
                (token, service_id, telegram_id),
            )
            conn.commit()
        return token

    def rename_service(self, service_id: int, telegram_id: int, new_name: str) -> None:
        with closing(self.connect()) as conn:
            conn.execute(
                "UPDATE services SET name = ? WHERE id = ? AND user_telegram_id = ?",
                (new_name, service_id, telegram_id),
            )
            conn.commit()

    def delete_service(self, service_id: int, telegram_id: int) -> None:
        with closing(self.connect()) as conn:
            conn.execute("UPDATE services SET status = 'deleted' WHERE id = ? AND user_telegram_id = ?", (service_id, telegram_id))
            conn.commit()


db = DB(DATABASE_PATH)
router = Router()


# -----------------------------
# FSM
# -----------------------------
class BuyStates(StatesGroup):
    waiting_name = State()


class RenameStates(StatesGroup):
    waiting_new_name = State()


# -----------------------------
# Keyboards
# -----------------------------
def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🛒 خرید سرویس")],
            [KeyboardButton(text="📦 سرویس‌های من"), KeyboardButton(text="🎁 سرویس رایگان")],
            [KeyboardButton(text="💳 تراکنش‌ها"), KeyboardButton(text="💰 کیف پول")],
            [KeyboardButton(text="💎 معرفی به دوستان"), KeyboardButton(text="📊 اطلاعات حساب")],
        ],
        resize_keyboard=True,
        input_field_placeholder="یک گزینه را انتخاب کنید…",
    )


def inline(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
            for row in rows
        ]
    )


def back_home_kb() -> InlineKeyboardMarkup:
    return inline([[('🏠 منوی اصلی', 'home')]])


def buy_type_kb() -> InlineKeyboardMarkup:
    return inline([
        [("🛍 پلن‌های آماده یک‌ماهه", "buy_cat:monthly")],
        [("💎 پلن‌های حرفه‌ای سه‌ماهه", "buy_cat:quarterly")],
        [("🎁 سرویس رایگان", "free_test_menu")],
        [("🏠 منوی اصلی", "home")],
    ])


def plans_kb(category: str) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    for plan in PLANS.values():
        if plan.category == category:
            rows.append([(f"{plan.title} — {fmt_money(plan.price)}", f"plan:{plan.key}")])
    rows.append([("⬅️ بازگشت", "buy"), ("🏠 منوی اصلی", "home")])
    return inline(rows)


def free_service_type_kb() -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    for key, item in FREE_SERVICE_TYPES.items():
        rows.append([(item["title"], f"free_type:{key}")])
    rows.append([("⬅️ بازگشت", "buy"), ("🏠 منوی اصلی", "home")])
    return inline(rows)


def free_packages_kb(service_type: str) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    for plan in FREE_TEST_PLANS.values():
        if plan.category == f"free:{service_type}":
            rows.append([(f"🎁 {plan.title} — رایگان", f"free_plan:{plan.key}")])
    rows.append([("⬅️ بازگشت", "free_test_menu"), ("🏠 منوی اصلی", "home")])
    return inline(rows)


def addon_packages_kb(service_id: int) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    for pkg in DATA_ADDON_PACKAGES.values():
        rows.append([(f"📈 {pkg.title} — {fmt_money(pkg.price)}", f"addon_pkg:{service_id}:{pkg.key}")])
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
    for plan in PLANS.values():
        if plan.category == category:
            rows.append([(f"♻️ {plan.title} — {fmt_money(plan.price)}", f"renew_plan:{service_id}:{plan.key}")])
    rows.append([("⬅️ بازگشت", f"renew_menu:{service_id}"), ("🏠 منوی اصلی", "home")])
    return inline(rows)


def order_payment_kb(
    order_id: int,
    payable: int,
    wallet_balance: int,
    back_callback: str = "buy",
    back_text: str = "⬅️ انتخاب پلن دیگر",
) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    if wallet_balance >= payable and payable > 0:
        rows.append([("💰 پرداخت با کیف پول", f"pay_wallet:{order_id}")])
    rows.append([("🤖 ثبت پرداخت موفق دمو", f"pay_demo:{order_id}")])
    rows.append([(back_text, back_callback), ("🏠 منوی اصلی", "home")])
    return inline(rows)


def services_kb(services: list[sqlite3.Row]) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    active = [s for s in services if s["status"] != "deleted"]
    for service in active[:20]:
        rows.append([(f"📦 {service['name']}", f"service:{service['id']}")])
    rows.append([("🛒 خرید سرویس جدید", "buy")])
    rows.append([("🏠 منوی اصلی", "home")])
    return inline(rows)


def service_details_kb(service_id: int) -> InlineKeyboardMarkup:
    return inline([
        [("🔗 لینک اشتراک", f"sub_link:{service_id}"), ("🔄 تغییر لینک", f"revoke:{service_id}")],
        [("♻️ تمدید سرویس", f"renew_warn:{service_id}"), ("📈 افزایش حجم", f"addon_menu:{service_id}")],
        [("⚙️ تنظیمات اشتراک", f"svc_settings:{service_id}")],
        [("⬅️ سرویس‌های من", "my_services"), ("🏠 منوی اصلی", "home")],
    ])


def service_settings_kb(service_id: int) -> InlineKeyboardMarkup:
    return inline([
        [("✏️ تغییر نام اشتراک", f"rename:{service_id}")],
        [("❄️ فریز اشتراک", f"soon:freeze")],
        [("🔁 انتقال اشتراک", f"soon:transfer")],
        [("🗑 حذف سرویس", f"delete_ask:{service_id}")],
        [("⬅️ جزئیات سرویس", f"service:{service_id}"), ("🏠 منوی اصلی", "home")],
    ])


def delete_confirm_kb(service_id: int) -> InlineKeyboardMarkup:
    return inline([
        [("✅ بله، حذف شود", f"delete_yes:{service_id}")],
        [("❌ منصرف شدم", f"svc_settings:{service_id}")],
    ])


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


def wallet_kb() -> InlineKeyboardMarkup:
    return inline([
        [("➕ افزایش موجودی دمو ۱۰۰٬۰۰۰ تومان", "wallet_demo:100000")],
        [("➕ افزایش موجودی دمو ۵۰۰٬۰۰۰ تومان", "wallet_demo:500000")],
        [("🏠 منوی اصلی", "home")],
    ])


# -----------------------------
# Message builders
# -----------------------------
def welcome_text(first_name: str | None = None) -> str:
    name_part = f"، {h(first_name)}" if first_name else ""
    return (
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


def buy_text() -> str:
    return (
        header("🛒 خرید سرویس", "نوع سرویس را انتخاب کنید") +
        "پلن‌های آماده برای شروع سریع مناسب‌اند.\n"
        "پلن‌های سه‌ماهه برای مصرف پایدار و اقتصادی‌تر پیشنهاد می‌شوند."
    )


def free_service_text() -> str:
    return (
        header("🎁 سرویس رایگان", "اول نوع سرویس را انتخاب کنید") +
        "برای تست کیفیت، یک سرویس رایگان محدود می‌توانید فعال کنید.\n"
        "این سرویس فقط یک‌بار برای هر حساب قابل دریافت است."
    )


def free_package_text(service_type: str) -> str:
    item = FREE_SERVICE_TYPES.get(service_type, FREE_SERVICE_TYPES["standard"])
    return (
        header("🎁 انتخاب پکیج رایگان", item["title"]) +
        f"{h(item['subtitle'])}\n\n"
        "پکیج موردنظر را انتخاب کنید:"
    )


def addon_text(service: sqlite3.Row) -> str:
    return (
        header("📈 افزایش حجم سرویس", service["name"]) +
        "یکی از بسته‌های حجم اضافه را انتخاب کنید.\n\n"
        "✅ فقط حجم سرویس بیشتر می‌شود.\n"
        "⏳ زمان پایان سرویس هیچ تغییری نمی‌کند.\n"
        "💡 بسته‌های زیر نسبت به خرید سرویس جدید، به‌صرفه‌تر طراحی شده‌اند."
    )


def renew_text(service: sqlite3.Row) -> str:
    return (
        header("♻️ تمدید سرویس", service["name"]) +
        "نوع پلن تمدید را انتخاب کنید.\n\n"
        "⚠️ با تمدید، حجم و زمان سرویس شما مطابق پلن جدید ریست می‌شود.\n"
        "اگر فقط حجم بیشتری می‌خواهید، از گزینه «افزایش حجم» استفاده کنید."
    )


def renew_category_text(service: sqlite3.Row, category: str) -> str:
    base = plan_category_text(category)
    return base + "\n\n" + f"♻️ تمدید برای سرویس: <b>{h(service['name'])}</b>"


def plan_category_text(category: str) -> str:
    if category == "monthly":
        return header("🛍 پلن‌های آماده یک‌ماهه", "سرعت بالا، فعال‌سازی سریع") + "یکی از حجم‌های زیر را انتخاب کنید:"
    return header("💎 پلن‌های حرفه‌ای سه‌ماهه", "اقتصادی‌تر برای استفاده بلندمدت") + "یکی از پلن‌های سه‌ماهه زیر را انتخاب کنید:"


def plan_summary_text(plan: Plan, user: sqlite3.Row) -> tuple[str, int, int, int]:
    discount = 0
    if user["referred_by_telegram_id"] and not user["first_purchase_done"]:
        discount = int(plan.price * REFERRED_DISCOUNT_PERCENT / 100)
    payable = max(plan.price - discount, 0)
    text = (
        header("🧾 خلاصه سفارش", plan.title) +
        f"🏷 نوع پلن: <b>{h(plan.title)}</b>\n"
        f"📦 حجم: <b>{fmt_number(plan.data_gb)} گیگابایت</b>\n"
        f"⏳ اعتبار: <b>{fmt_number(plan.days)} روز</b>\n"
        f"💳 مبلغ اصلی: <b>{fmt_money(plan.price)}</b>\n"
    )
    if discount:
        text += f"🎁 تخفیف دعوت دوستان: <b>{fmt_money(discount)}</b>\n"
    text += f"✅ مبلغ قابل پرداخت: <b>{fmt_money(payable)}</b>\n\n"
    text += "یک نام دلخواه برای اشتراک وارد کنید یا دکمه زیر را بزنید تا خودکار ساخته شود.\n\n"
    text += "ربات به‌صورت خودکار ابتدای نام را <code>howtosee_</code> می‌گذارد.\n"
    text += "فقط حروف انگلیسی، عدد، خط تیره و آندرلاین مجاز است."
    return text, plan.price, discount, payable


def payment_text(plan: Plan, service_name: str, amount: int, discount: int, payable: int, wallet_balance: int) -> str:
    text = (
        header("💳 انتخاب روش پرداخت", service_name) +
        f"📦 پلن: <b>{h(plan.title)}</b>\n"
        f"💰 مبلغ اصلی: <b>{fmt_money(amount)}</b>\n"
    )
    if discount:
        text += f"🎁 تخفیف شما: <b>{fmt_money(discount)}</b>\n"
    text += (
        f"✅ قابل پرداخت: <b>{fmt_money(payable)}</b>\n"
        f"💼 موجودی کیف پول: <b>{fmt_money(int(wallet_balance))}</b>\n\n"
        "فعلاً درگاه واقعی وصل نشده؛ برای تست جریان ربات از گزینه پرداخت دمو استفاده کنید."
    )
    return text


def addon_payment_text(service: sqlite3.Row, pkg: DataAddon, wallet_balance: int) -> str:
    return (
        header("💳 پرداخت افزایش حجم", service["name"]) +
        f"📈 بسته انتخابی: <b>{h(pkg.title)}</b>\n"
        f"📊 حجم اضافه: <b>{fmt_number(pkg.data_gb)} گیگابایت</b>\n"
        f"⏳ افزایش زمان: <b>ندارد</b>\n"
        f"💰 مبلغ: <b>{fmt_money(pkg.price)}</b>\n"
        f"💼 موجودی کیف پول: <b>{fmt_money(wallet_balance)}</b>\n\n"
        "فعلاً درگاه واقعی وصل نشده؛ برای تست جریان ربات از گزینه پرداخت دمو استفاده کنید."
    )


def renew_payment_text(service: sqlite3.Row, plan: Plan, amount: int, discount: int, payable: int, wallet_balance: int) -> str:
    text = (
        header("💳 پرداخت تمدید سرویس", service["name"]) +
        f"♻️ پلن تمدید: <b>{h(plan.title)}</b>\n"
        f"📊 حجم جدید: <b>{fmt_number(plan.data_gb)} گیگابایت</b>\n"
        f"⏳ اعتبار جدید: <b>{fmt_number(plan.days)} روز</b>\n"
        f"💰 مبلغ اصلی: <b>{fmt_money(amount)}</b>\n"
    )
    if discount:
        text += f"🎁 تخفیف دعوت: <b>{fmt_money(discount)}</b>\n"
    text += (
        f"✅ قابل پرداخت: <b>{fmt_money(payable)}</b>\n"
        f"💼 موجودی کیف پول: <b>{fmt_money(wallet_balance)}</b>\n\n"
        "⚠️ با پرداخت، حجم مصرف‌شده صفر می‌شود و زمان سرویس مطابق پلن جدید ریست می‌شود."
    )
    return text


def service_text(service: sqlite3.Row) -> str:
    expires = datetime.fromisoformat(service["expires_at"])
    days_left = max((expires - datetime.now(TEHRAN_TZ)).days, 0)
    used_gb = service["data_used_mb"] / 1024
    left_gb = max(float(service["data_gb"]) - used_gb, 0)
    sub_link = f"{SUBSCRIPTION_BASE_URL}/{service['token']}"
    status_label = "فعال ✅" if service["status"] == "active" else "غیرفعال ⛔"
    test_label = "\n🎁 نوع: <b>سرویس رایگان</b>" if service["is_test"] else ""
    return (
        header("📦 جزئیات سرویس", service["name"]) +
        f"🟢 وضعیت: <b>{status_label}</b>{test_label}\n"
        f"🏷 پلن: <b>{h(service['plan_title'])}</b>\n"
        f"📊 حجم باقی‌مانده: <b>{fmt_number(round(left_gb, 2))} گیگابایت</b>\n"
        f"⏳ زمان باقی‌مانده: <b>{fmt_number(days_left)} روز</b>\n"
        f"💳 مبلغ پرداختی: <b>{fmt_money(int(service['paid_amount']))}</b>\n\n"
        f"🔗 لینک اشتراک:\n<code>{h(sub_link)}</code>\n\n"
        "برای مدیریت سرویس از دکمه‌های زیر استفاده کنید."
    )


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
        header("💎 معرفی به دوستان", "هر معرفی موفق = اعتبار کیف پول") +
        "دوستت با لینک اختصاصی تو وارد ربات میشه؛ وقتی اولین خریدش رو انجام بده، پورسانت به کیف پولت اضافه میشه.\n\n"
        f"💰 پورسانت شما: <b>{REFERRAL_COMMISSION_PERCENT}٪ از خرید اول دوست</b>\n"
        f"🎁 تخفیف دوست شما: <b>{REFERRED_DISCOUNT_PERCENT}٪ برای خرید اول</b>\n"
        "🔒 پرداخت پورسانت فقط بعد از خرید موفق انجام میشه؛ پس برای شما و ما منصفانه و به‌صرفه‌ست.\n\n"
        f"📊 دعوت‌ها: <b>{fmt_number(stats['total'])}</b>\n"
        f"✅ خرید موفق: <b>{fmt_number(stats['rewarded'])}</b>\n"
        f"💼 درآمد معرفی: <b>{fmt_money(stats['earned'])}</b>\n\n"
        f"🔗 لینک اختصاصی شما:\n<code>{h(invite_link)}</code>"
    )


def referral_how_text() -> str:
    return (
        header("💰 پورسانت معرفی چطوریه؟", "ساده، شفاف و به‌صرفه") +
        f"۱) لینک اختصاصی خودت رو برای دوستت می‌فرستی.\n"
        f"۲) دوستت از طریق لینک وارد ربات میشه و عضو میشه.\n"
        f"۳) برای خرید اول، <b>{REFERRED_DISCOUNT_PERCENT}٪ تخفیف</b> می‌گیره.\n"
        f"۴) بعد از پرداخت موفق دوستت، <b>{REFERRAL_COMMISSION_PERCENT}٪ مبلغ خرید اول</b> به کیف پول تو اضافه میشه.\n\n"
        "چرا این روش خوبه؟\n"
        "✅ دوستت با تخفیف شروع می‌کنه\n"
        "✅ تو اعتبار واقعی برای خرید یا تمدید می‌گیری\n"
        "✅ پورسانت فقط بعد از خرید موفق فعال میشه، پس سیستم سالم و قابل ادامه می‌مونه\n\n"
        "نمونه:\n"
        "اگر دوستت پلن ۴۰۰٬۰۰۰ تومانی بخره، ۴۰٬۰۰۰ تومان اعتبار به کیف پولت اضافه میشه."
    )


def account_text(user: sqlite3.Row) -> str:
    services = [s for s in db.list_services(int(user["telegram_id"])) if s["status"] != "deleted"]
    stats = db.referral_stats(int(user["telegram_id"]))
    username = f"@{user['username']}" if user["username"] else "ثبت نشده"
    return (
        header("📊 اطلاعات حساب شما") +
        f"🧾 شناسه کاربری: <code>{user['telegram_id']}</code>\n"
        f"👤 یوزرنیم: <b>{h(username)}</b>\n"
        f"💰 موجودی کیف پول: <b>{fmt_money(int(user['wallet_balance']))}</b>\n"
        f"📦 سرویس‌های فعال: <b>{fmt_number(len(services))}</b>\n"
        f"💎 دعوت‌های موفق: <b>{fmt_number(stats['rewarded'])}</b>\n"
        f"🎁 درآمد معرفی: <b>{fmt_money(stats['earned'])}</b>\n"
        f"📅 عضویت: <code>{h(user['created_at'][:10])}</code>"
    )


# -----------------------------
# User helpers
# -----------------------------
def parse_referrer_from_text(text: str | None) -> Optional[int]:
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


# -----------------------------
# Routes
# -----------------------------
@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    before = db.get_user(message.from_user.id) if message.from_user else None
    referrer_id = parse_referrer_from_text(message.text)
    user = ensure_from_message(message)

    if referrer_id and referrer_id != user["telegram_id"] and before is None:
        await message.answer(
            "🎁 شما با لینک دعوت وارد شدید.\n"
            f"برای خرید اول، <b>{REFERRED_DISCOUNT_PERCENT}٪ تخفیف</b> روی سفارش شما اعمال می‌شود.",
            reply_markup=main_menu_kb(),
        )

    await message.answer(welcome_text(message.from_user.first_name if message.from_user else None), reply_markup=main_menu_kb(), disable_web_page_preview=True)


@router.message(Command("menu"))
async def menu_cmd(message: Message, state: FSMContext) -> None:
    await state.clear()
    ensure_from_message(message)
    await message.answer(welcome_text(message.from_user.first_name if message.from_user else None), reply_markup=main_menu_kb(), disable_web_page_preview=True)


@router.callback_query(F.data == "home")
async def home_cb(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    ensure_from_callback(callback)
    if callback.message:
        await callback.message.answer(welcome_text(callback.from_user.first_name), reply_markup=main_menu_kb(), disable_web_page_preview=True)
    await callback.answer()


@router.message(F.text == "🛒 خرید سرویس")
async def buy_msg(message: Message, state: FSMContext) -> None:
    await state.clear()
    ensure_from_message(message)
    await message.answer(buy_text(), reply_markup=buy_type_kb())


@router.callback_query(F.data == "buy")
async def buy_cb(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    ensure_from_callback(callback)
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
    text, amount, discount, payable = plan_summary_text(plan, user)
    await state.set_state(BuyStates.waiting_name)
    await state.update_data(plan_key=plan.key, amount=amount, discount=discount, payable=payable)
    await edit_or_answer(callback, text, inline([[('✅ رد، نیازی ندارم', 'auto_name')], [('⬅️ بازگشت', f'buy_cat:{plan.category}')]]))


@router.callback_query(F.data == "auto_name")
async def auto_name(callback: CallbackQuery, state: FSMContext) -> None:
    user = ensure_from_callback(callback)
    data = await state.get_data()
    plan_key = data.get("plan_key")
    if not plan_key or plan_key not in PLANS:
        await callback.answer("سفارش پیدا نشد. دوباره پلن را انتخاب کنید.", show_alert=True)
        return
    service_name = make_service_name(int(user["telegram_id"]))
    await create_pending_order_and_show_payment(callback, state, service_name)


@router.message(BuyStates.waiting_name)
async def custom_name(message: Message, state: FSMContext) -> None:
    user = ensure_from_message(message)
    raw = (message.text or "").strip()
    cleaned = clean_username(raw)
    if not cleaned:
        service_name = make_service_name(int(user["telegram_id"]))
    else:
        if len(cleaned) < 3:
            await message.answer("نام اشتراک حداقل باید ۳ کاراکتر معتبر داشته باشد. فقط حروف انگلیسی، عدد، خط تیره و آندرلاین مجاز است.")
            return
        service_name = service_name_with_prefix(cleaned)
    await create_pending_order_and_show_payment(message, state, service_name)


async def create_pending_order_and_show_payment(target: Message | CallbackQuery, state: FSMContext, service_name: str) -> None:
    data = await state.get_data()
    plan = PLANS[data["plan_key"]]
    amount = int(data.get("amount", plan.price))
    discount = int(data.get("discount", 0))
    payable = int(data.get("payable", max(amount - discount, 0)))
    tg_id = target.from_user.id
    user = db.get_user(tg_id)
    if not user:
        user = db.ensure_user(tg_id, target.from_user.username, target.from_user.first_name)
    order_id = db.create_order(tg_id, plan.key, amount, discount, 0, "pending", "none")
    await state.clear()
    text = payment_text(plan, service_name, amount, discount, payable, int(user["wallet_balance"]))
    await state.update_data(service_name_by_order={str(order_id): service_name})
    # State is cleared intentionally; service name is stored in the order note via in-memory fallback below.
    pending_names[order_id] = service_name
    if isinstance(target, CallbackQuery):
        await edit_or_answer(target, text, order_payment_kb(order_id, payable, int(user["wallet_balance"])))
    else:
        await target.answer(text, reply_markup=order_payment_kb(order_id, payable, int(user["wallet_balance"])))


pending_names: dict[int, str] = {}


@router.callback_query(F.data.startswith("pay_demo:"))
async def pay_demo(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    order_id = int(callback.data.split(":", 1)[1])
    await complete_order(callback, int(user["telegram_id"]), order_id, "پرداخت دمو", use_wallet=False)


@router.callback_query(F.data.startswith("pay_wallet:"))
async def pay_wallet(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    order_id = int(callback.data.split(":", 1)[1])
    await complete_order(callback, int(user["telegram_id"]), order_id, "کیف پول", use_wallet=True)


async def complete_order(callback: CallbackQuery, telegram_id: int, order_id: int, method: str, use_wallet: bool) -> None:
    with closing(db.connect()) as conn:
        order = conn.execute("SELECT * FROM orders WHERE id = ? AND user_telegram_id = ?", (order_id, telegram_id)).fetchone()
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
    payable = int(order["amount"]) - int(order["discount_amount"])
    user = db.get_user(telegram_id)
    if not user:
        await callback.answer("حساب کاربری پیدا نشد.", show_alert=True)
        return

    if use_wallet:
        if int(user["wallet_balance"]) < payable:
            await callback.answer("موجودی کیف پول کافی نیست.", show_alert=True)
            return
        db.add_wallet(telegram_id, -payable, "wallet_payment", f"پرداخت سفارش #{order_id}")
        wallet_used = payable
    else:
        wallet_used = 0

    service_name = pending_names.get(order_id) or make_service_name(telegram_id)
    service_id = db.create_service(telegram_id, service_name, plan, payable, is_test=False)
    db.update_order_service(order_id, service_id)
    with closing(db.connect()) as conn:
        conn.execute(
            "UPDATE orders SET status = 'paid', payment_method = ?, wallet_used = ? WHERE id = ?",
            (method, wallet_used, order_id),
        )
        conn.commit()

    reward = db.reward_referrer_if_needed(telegram_id, order_id, payable)
    service = db.get_service(service_id, telegram_id)
    extra = ""
    if reward:
        extra = "\n\n💎 پورسانت معرفی با موفقیت برای معرف شما ثبت شد."

    await edit_or_answer(
        callback,
        header("✅ سفارش با موفقیت فعال شد", service_name)
        + f"سرویس شما آماده استفاده است.\n\n{service_text(service)}{extra}",
        service_details_kb(service_id),
    )


async def complete_addon_order(callback: CallbackQuery, telegram_id: int, order: sqlite3.Row, method: str, use_wallet: bool) -> None:
    parts = str(order["plan_key"]).split(":")
    if len(parts) != 3:
        await callback.answer("سفارش افزایش حجم نامعتبر است.", show_alert=True)
        return
    package_key = parts[1]
    service_id = int(parts[2])
    pkg = DATA_ADDON_PACKAGES.get(package_key)
    service = db.get_service(service_id, telegram_id)
    if not pkg or not service or service["status"] == "deleted":
        await callback.answer("سرویس یا بسته حجم پیدا نشد.", show_alert=True)
        return

    payable = int(order["amount"]) - int(order["discount_amount"])
    user = db.get_user(telegram_id)
    if not user:
        await callback.answer("حساب کاربری پیدا نشد.", show_alert=True)
        return

    if use_wallet:
        if int(user["wallet_balance"]) < payable:
            await callback.answer("موجودی کیف پول کافی نیست.", show_alert=True)
            return
        db.add_wallet(telegram_id, -payable, "wallet_payment", f"پرداخت افزایش حجم سفارش #{order['id']}")
        wallet_used = payable
    else:
        wallet_used = 0

    db.add_data_to_service(service_id, telegram_id, pkg.data_gb)
    if payable > 0:
        db.mark_first_purchase_done(telegram_id)
    db.update_order_service(int(order["id"]), service_id)
    with closing(db.connect()) as conn:
        conn.execute(
            "UPDATE orders SET status = 'paid', payment_method = ?, wallet_used = ? WHERE id = ?",
            (method, wallet_used, int(order["id"])),
        )
        conn.commit()

    reward = db.reward_referrer_if_needed(telegram_id, int(order["id"]), payable)
    service = db.get_service(service_id, telegram_id)
    extra = ""
    if reward:
        extra = "\n\n💎 پورسانت معرفی با موفقیت برای معرف شما ثبت شد."
    await edit_or_answer(
        callback,
        header("✅ حجم سرویس افزایش یافت", service["name"]) +
        f"بسته <b>{h(pkg.title)}</b> به سرویس شما اضافه شد.\n"
        "⏳ زمان پایان سرویس تغییری نکرد.\n\n" + service_text(service) + extra,
        service_details_kb(service_id),
    )


async def complete_renew_order(callback: CallbackQuery, telegram_id: int, order: sqlite3.Row, method: str, use_wallet: bool) -> None:
    parts = str(order["plan_key"]).split(":")
    if len(parts) != 3:
        await callback.answer("سفارش تمدید نامعتبر است.", show_alert=True)
        return
    plan_key = parts[1]
    service_id = int(parts[2])
    plan = PLANS.get(plan_key)
    service = db.get_service(service_id, telegram_id)
    if not plan or not service or service["status"] == "deleted":
        await callback.answer("سرویس یا پلن تمدید پیدا نشد.", show_alert=True)
        return

    payable = int(order["amount"]) - int(order["discount_amount"])
    user = db.get_user(telegram_id)
    if not user:
        await callback.answer("حساب کاربری پیدا نشد.", show_alert=True)
        return

    if use_wallet:
        if int(user["wallet_balance"]) < payable:
            await callback.answer("موجودی کیف پول کافی نیست.", show_alert=True)
            return
        db.add_wallet(telegram_id, -payable, "wallet_payment", f"پرداخت تمدید سفارش #{order['id']}")
        wallet_used = payable
    else:
        wallet_used = 0

    db.renew_service(service_id, telegram_id, plan, payable)
    db.update_order_service(int(order["id"]), service_id)
    with closing(db.connect()) as conn:
        conn.execute(
            "UPDATE orders SET status = 'paid', payment_method = ?, wallet_used = ? WHERE id = ?",
            (method, wallet_used, int(order["id"])),
        )
        conn.commit()

    reward = db.reward_referrer_if_needed(telegram_id, int(order["id"]), payable)
    service = db.get_service(service_id, telegram_id)
    extra = ""
    if reward:
        extra = "\n\n💎 پورسانت معرفی با موفقیت برای معرف شما ثبت شد."
    await edit_or_answer(
        callback,
        header("✅ سرویس با موفقیت تمدید شد", service["name"]) +
        "حجم مصرف‌شده صفر شد و زمان سرویس طبق پلن جدید تنظیم شد.\n\n" + service_text(service) + extra,
        service_details_kb(service_id),
    )


@router.message(F.text == "📦 سرویس‌های من")
async def my_services_msg(message: Message) -> None:
    user = ensure_from_message(message)
    await show_my_services(message, int(user["telegram_id"]))


@router.callback_query(F.data == "my_services")
async def my_services_cb(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    services = db.list_services(int(user["telegram_id"]))
    active = [s for s in services if s["status"] != "deleted"]
    text = header("📦 سرویس‌های من")
    if not active:
        text += "هنوز سرویس فعالی ندارید. از بخش خرید سرویس می‌توانید اولین سرویس خود را بسازید."
    else:
        text += "برای دیدن جزئیات، یکی از سرویس‌ها را انتخاب کنید:"
    await edit_or_answer(callback, text, services_kb(services))


async def show_my_services(message: Message, telegram_id: int) -> None:
    services = db.list_services(telegram_id)
    active = [s for s in services if s["status"] != "deleted"]
    text = header("📦 سرویس‌های من")
    if not active:
        text += "هنوز سرویس فعالی ندارید. از بخش خرید سرویس می‌توانید اولین سرویس خود را بسازید."
    else:
        text += "برای دیدن جزئیات، یکی از سرویس‌ها را انتخاب کنید:"
    await message.answer(text, reply_markup=services_kb(services))


@router.callback_query(F.data.startswith("service:"))
async def service_details(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    service_id = int(callback.data.split(":", 1)[1])
    service = db.get_service(service_id, int(user["telegram_id"]))
    if not service or service["status"] == "deleted":
        await callback.answer("سرویس پیدا نشد.", show_alert=True)
        return
    await edit_or_answer(callback, service_text(service), service_details_kb(service_id))


@router.callback_query(F.data.startswith("addon_menu:"))
async def addon_menu(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    service_id = int(callback.data.split(":", 1)[1])
    service = db.get_service(service_id, int(user["telegram_id"]))
    if not service or service["status"] == "deleted":
        await callback.answer("سرویس پیدا نشد.", show_alert=True)
        return
    await edit_or_answer(callback, addon_text(service), addon_packages_kb(service_id))


@router.callback_query(F.data.startswith("addon_pkg:"))
async def addon_package_selected(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("درخواست نامعتبر است.", show_alert=True)
        return
    service_id = int(parts[1])
    package_key = parts[2]
    service = db.get_service(service_id, int(user["telegram_id"]))
    pkg = DATA_ADDON_PACKAGES.get(package_key)
    if not service or service["status"] == "deleted":
        await callback.answer("سرویس پیدا نشد.", show_alert=True)
        return
    if not pkg:
        await callback.answer("بسته حجم پیدا نشد.", show_alert=True)
        return

    order_id = db.create_order(int(user["telegram_id"]), f"addon:{package_key}:{service_id}", pkg.price, 0, 0, "pending", "none")
    await edit_or_answer(
        callback,
        addon_payment_text(service, pkg, int(user["wallet_balance"])),
        order_payment_kb(order_id, pkg.price, int(user["wallet_balance"]), f"addon_menu:{service_id}", "⬅️ انتخاب بسته دیگر"),
    )


@router.callback_query(F.data.startswith("renew_warn:"))
async def renew_warn(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    service_id = int(callback.data.split(":", 1)[1])
    service = db.get_service(service_id, int(user["telegram_id"]))
    if not service or service["status"] == "deleted":
        await callback.answer("سرویس پیدا نشد.", show_alert=True)
        return

    await callback.answer(
        "⚠️ توجه!\nبا تمدید، حجم و زمان سرویس شما ریست می‌شود.\nاگر فقط حجم بیشتری می‌خواهید، از افزایش حجم استفاده کنید ✅",
        show_alert=True,
    )
    if callback.message:
        try:
            await callback.message.edit_text(renew_text(service), reply_markup=renew_type_kb(service_id), disable_web_page_preview=True)
        except Exception:
            await callback.message.answer(renew_text(service), reply_markup=renew_type_kb(service_id), disable_web_page_preview=True)


@router.callback_query(F.data.startswith("renew_menu:"))
async def renew_menu(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    service_id = int(callback.data.split(":", 1)[1])
    service = db.get_service(service_id, int(user["telegram_id"]))
    if not service or service["status"] == "deleted":
        await callback.answer("سرویس پیدا نشد.", show_alert=True)
        return
    await edit_or_answer(callback, renew_text(service), renew_type_kb(service_id))


@router.callback_query(F.data.startswith("renew_cat:"))
async def renew_category(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("درخواست نامعتبر است.", show_alert=True)
        return
    service_id = int(parts[1])
    category = parts[2]
    service = db.get_service(service_id, int(user["telegram_id"]))
    if not service or service["status"] == "deleted":
        await callback.answer("سرویس پیدا نشد.", show_alert=True)
        return
    await edit_or_answer(callback, renew_category_text(service, category), renew_plans_kb(service_id, category))


@router.callback_query(F.data.startswith("renew_plan:"))
async def renew_plan_selected(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("درخواست نامعتبر است.", show_alert=True)
        return
    service_id = int(parts[1])
    plan_key = parts[2]
    service = db.get_service(service_id, int(user["telegram_id"]))
    plan = PLANS.get(plan_key)
    if not service or service["status"] == "deleted":
        await callback.answer("سرویس پیدا نشد.", show_alert=True)
        return
    if not plan:
        await callback.answer("پلن تمدید پیدا نشد.", show_alert=True)
        return

    discount = 0
    if user["referred_by_telegram_id"] and not user["first_purchase_done"]:
        discount = int(plan.price * REFERRED_DISCOUNT_PERCENT / 100)
    payable = max(plan.price - discount, 0)
    order_id = db.create_order(int(user["telegram_id"]), f"renew:{plan_key}:{service_id}", plan.price, discount, 0, "pending", "none")
    await edit_or_answer(
        callback,
        renew_payment_text(service, plan, plan.price, discount, payable, int(user["wallet_balance"])),
        order_payment_kb(order_id, payable, int(user["wallet_balance"]), f"renew_menu:{service_id}", "⬅️ انتخاب پلن دیگر"),
    )


@router.callback_query(F.data.startswith("sub_link:"))
async def sub_link(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    service_id = int(callback.data.split(":", 1)[1])
    service = db.get_service(service_id, int(user["telegram_id"]))
    if not service:
        await callback.answer("سرویس پیدا نشد.", show_alert=True)
        return
    link = f"{SUBSCRIPTION_BASE_URL}/{service['token']}"
    await callback.answer("لینک اشتراک آماده شد.")
    if callback.message:
        await callback.message.answer(
            header("🔗 لینک اشتراک", service["name"]) + f"<code>{h(link)}</code>\n\nبرای کپی کردن، روی لینک بالا لمس کنید.",
            reply_markup=back_home_kb(),
            disable_web_page_preview=True,
        )


@router.callback_query(F.data.startswith("revoke:"))
async def revoke(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    service_id = int(callback.data.split(":", 1)[1])
    token = db.revoke_service_link(service_id, int(user["telegram_id"]))
    service = db.get_service(service_id, int(user["telegram_id"]))
    if not service:
        await callback.answer("سرویس پیدا نشد.", show_alert=True)
        return
    link = f"{SUBSCRIPTION_BASE_URL}/{token}"
    await edit_or_answer(
        callback,
        header("🔄 لینک اشتراک تغییر کرد", service["name"]) +
        "لینک قبلی دیگر قابل استفاده نیست. لینک جدید:\n\n" +
        f"<code>{h(link)}</code>",
        service_details_kb(service_id),
    )


@router.callback_query(F.data.startswith("svc_settings:"))
async def service_settings(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    service_id = int(callback.data.split(":", 1)[1])
    service = db.get_service(service_id, int(user["telegram_id"]))
    if not service:
        await callback.answer("سرویس پیدا نشد.", show_alert=True)
        return
    await edit_or_answer(
        callback,
        header("⚙️ تنظیمات اشتراک", service["name"]) + "گزینه موردنظر را انتخاب کنید:",
        service_settings_kb(service_id),
    )


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
    await state.set_state(RenameStates.waiting_new_name)
    await state.update_data(service_id=service_id)
    await edit_or_answer(
        callback,
        header("✏️ تغییر نام اشتراک", service["name"]) +
        "نام جدید را وارد کنید.\n\nربات به‌صورت خودکار ابتدای نام را <code>howtosee_</code> می‌گذارد.\n"
        "فقط حروف انگلیسی، عدد، خط تیره و آندرلاین مجاز است.",
        inline([[('❌ لغو', f'svc_settings:{service_id}')]]),
    )


@router.message(RenameStates.waiting_new_name)
async def rename_finish(message: Message, state: FSMContext) -> None:
    user = ensure_from_message(message)
    data = await state.get_data()
    service_id = int(data["service_id"])
    cleaned = clean_username(message.text or "")
    if len(cleaned) < 3:
        await message.answer("نام جدید حداقل باید ۳ کاراکتر معتبر داشته باشد.")
        return
    name = service_name_with_prefix(cleaned)
    db.rename_service(service_id, int(user["telegram_id"]), name)
    await state.clear()
    await message.answer(
        header("✅ نام اشتراک تغییر کرد", name) + "تغییر با موفقیت ذخیره شد.",
        reply_markup=service_details_kb(service_id),
    )


@router.callback_query(F.data.startswith("delete_ask:"))
async def delete_ask(callback: CallbackQuery) -> None:
    service_id = int(callback.data.split(":", 1)[1])
    ensure_from_callback(callback)
    await edit_or_answer(
        callback,
        header("🗑 حذف سرویس") +
        "آیا مطمئن هستید؟ این کار فقط سرویس را از لیست شما حذف می‌کند و در نسخه نمایشی برگشت‌پذیر نیست.",
        delete_confirm_kb(service_id),
    )


@router.callback_query(F.data.startswith("delete_yes:"))
async def delete_yes(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    service_id = int(callback.data.split(":", 1)[1])
    db.delete_service(service_id, int(user["telegram_id"]))
    await edit_or_answer(callback, header("✅ سرویس حذف شد") + "سرویس از لیست شما حذف شد.", back_home_kb())


@router.message(F.text == "🎁 سرویس رایگان")
async def free_test_msg(message: Message) -> None:
    user = ensure_from_message(message)
    await show_free_service_menu(message, int(user["telegram_id"]))


@router.callback_query(F.data == "free_test_menu")
async def free_test_menu_cb(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    await callback.answer()
    if callback.message:
        await show_free_service_menu(callback.message, int(user["telegram_id"]), edit_callback=callback)


async def show_free_service_menu(
    message: Message,
    telegram_id: int,
    edit_callback: Optional[CallbackQuery] = None,
) -> None:
    user = db.get_user(telegram_id)
    if user and user["free_test_used"]:
        text = (
            header("🎁 سرویس رایگان") +
            "شما قبلاً سرویس رایگان خود را دریافت کرده‌اید.\n\n"
            "برای ادامه استفاده، یکی از پلن‌های اصلی را انتخاب کنید."
        )
        markup = inline([[('🛒 خرید سرویس', 'buy')], [('🏠 منوی اصلی', 'home')]])
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
    if user and user["free_test_used"]:
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
    if user and user["free_test_used"]:
        await callback.answer("شما قبلاً سرویس رایگان خود را دریافت کرده‌اید.", show_alert=True)
        return

    plan_key = callback.data.split(":", 1)[1]
    test_plan = FREE_TEST_PLANS.get(plan_key)
    if not test_plan:
        await callback.answer("پکیج رایگان پیدا نشد.", show_alert=True)
        return

    suffix = str(telegram_id)[-5:]
    service_name = f"{SERVICE_NAME_PREFIX}free_{suffix}"[:32]
    service_id = db.create_service(telegram_id, service_name, test_plan, 0, is_test=True)
    service = db.get_service(service_id, telegram_id)
    await edit_or_answer(
        callback,
        header("🎁 سرویس رایگان فعال شد", service_name) +
        "این سرویس برای بررسی کیفیت اتصال ساخته شده است.\n\n" + service_text(service),
        service_details_kb(service_id),
    )


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
            payable = int(order["amount"]) - int(order["discount_amount"])
            text += (
                f"{i}. <b>{status}</b>\n"
                f"   🧾 شماره: <code>{order['id']}</code>\n"
                f"   💰 مبلغ: <b>{fmt_money(payable)}</b>\n"
                f"   📅 تاریخ: <code>{h(order['created_at'][:10])}</code>\n\n"
            )
    await message.answer(text, reply_markup=back_home_kb())


@router.message(F.text == "💰 کیف پول")
async def wallet(message: Message) -> None:
    user = ensure_from_message(message)
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
    await message.answer(text, reply_markup=wallet_kb())


@router.callback_query(F.data.startswith("wallet_demo:"))
async def wallet_demo(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    amount = int(callback.data.split(":", 1)[1])
    db.add_wallet(int(user["telegram_id"]), amount, "demo_topup", "افزایش موجودی دمو")
    user = db.get_user(int(user["telegram_id"]))
    await edit_or_answer(
        callback,
        header("✅ کیف پول شارژ شد") +
        f"مبلغ <b>{fmt_money(amount)}</b> به کیف پول نمایشی شما اضافه شد.\n\n"
        f"موجودی جدید: <b>{fmt_money(int(user['wallet_balance']))}</b>",
        wallet_kb(),
    )


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
    text = (
        header("🔗 لینک و متن دعوت آماده") +
        "متن زیر برای ارسال به دوستان آماده است:\n\n"
        f"<code>{h(invite_text)}</code>"
    )
    await edit_or_answer(callback, text, referral_back_kb(invite_link, invite_text))


@router.callback_query(F.data == "ref_stats")
async def referral_stats(callback: CallbackQuery) -> None:
    user = ensure_from_callback(callback)
    stats = db.referral_stats(int(user["telegram_id"]))
    invite_link = referral_invite_link(user)
    invite_text = referral_invite_text(invite_link)
    text = (
        header("📊 آمار دعوت‌های من") +
        f"👥 کل ثبت‌نام با لینک شما: <b>{fmt_number(stats['total'])}</b>\n"
        f"✅ خرید موفق دوستان: <b>{fmt_number(stats['rewarded'])}</b>\n"
        f"⏳ در انتظار خرید: <b>{fmt_number(stats['pending'])}</b>\n"
        f"💰 درآمد کل معرفی: <b>{fmt_money(stats['earned'])}</b>\n\n"
        "پورسانت‌ها بعد از اولین خرید موفق هر دوست، خودکار به کیف پول اضافه می‌شوند."
    )
    await edit_or_answer(callback, text, referral_back_kb(invite_link, invite_text))


@router.message()
async def unknown(message: Message, state: FSMContext) -> None:
    ensure_from_message(message)
    await message.answer(
        "گزینه موردنظر را از منوی پایین انتخاب کنید 👇",
        reply_markup=main_menu_kb(),
    )


async def main() -> None:
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    logger.info("Bot started: @%s", BOT_USERNAME)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
