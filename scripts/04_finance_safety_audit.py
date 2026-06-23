#!/usr/bin/env python3
"""Read-only finance/coupon safety audit for HowTooSee bot.

Checks the legacy SQLite finance state for issues that can lose money or allow
coupon abuse. Use --fix-duplicates to cancel older editable duplicate coupon
reservations and keep only the newest order for each user+coupon pair.
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
from pathlib import Path

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None

ROOT = Path(__file__).resolve().parents[1]
if load_dotenv:
    load_dotenv(ROOT / ".env")

DB_PATH = Path(os.getenv("DATABASE_PATH", str(ROOT / "bot.db")))
if not DB_PATH.is_absolute():
    DB_PATH = ROOT / DB_PATH

ACTIVE_COUPON_STATUSES = ("draft", "pending", "payment_rejected", "waiting_receipt", "receipt_pending", "processing")
EDITABLE_COUPON_STATUSES = ("draft", "pending", "payment_rejected")
TERMINAL_STATUSES = ("paid", "cancelled", "payment_expired", "expired", "provisioning_failed", "rejected")


def fmt_money(v: int) -> str:
    return f"{int(v):,}".replace(",", "٬") + " تومان"


def placeholders(items: tuple[str, ...]) -> str:
    return ",".join("?" for _ in items)


def connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise SystemExit(f"SQLite DB not found: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return bool(row)


def audit_duplicate_coupon_reservations(conn: sqlite3.Connection, fix: bool) -> int:
    if not table_exists(conn, "orders"):
        return 0
    rows = conn.execute(
        f"""
        SELECT user_telegram_id, UPPER(coupon_code) AS code, COUNT(*) AS c, MAX(id) AS keep_id,
               GROUP_CONCAT(id) AS order_ids
        FROM orders
        WHERE coupon_code IS NOT NULL AND TRIM(coupon_code) != ''
          AND status IN ({placeholders(ACTIVE_COUPON_STATUSES)})
        GROUP BY user_telegram_id, UPPER(coupon_code)
        HAVING COUNT(*) > 1
        ORDER BY c DESC
        """,
        ACTIVE_COUPON_STATUSES,
    ).fetchall()
    if not rows:
        print("✅ رزرو تکراری کد تخفیف فعال پیدا نشد.")
        return 0
    print("⚠️ رزرو تکراری کد تخفیف پیدا شد:")
    fixed = 0
    for r in rows:
        print(f"  - user={r['user_telegram_id']} code={r['code']} count={r['c']} orders={r['order_ids']} keep=#{r['keep_id']}")
        if fix:
            cur = conn.execute(
                f"""
                UPDATE orders
                SET status='cancelled', payment_method='cancelled', service_name=NULL,
                    admin_note=COALESCE(?, admin_note)
                WHERE user_telegram_id=? AND UPPER(coupon_code)=? AND id!=?
                  AND status IN ({placeholders(EDITABLE_COUPON_STATUSES)})
                """,
                (
                    f"لغو توسط finance audit برای جلوگیری از رزرو چندباره کد {r['code']}; سفارش #{r['keep_id']} باقی ماند.",
                    int(r["user_telegram_id"]),
                    str(r["code"]),
                    int(r["keep_id"]),
                    *EDITABLE_COUPON_STATUSES,
                ),
            )
            fixed += int(cur.rowcount or 0)
    if fix:
        conn.commit()
        print(f"✅ {fixed} سفارش نیمه‌کاره تکراری لغو شد.")
    else:
        print("برای اصلاح خودکار سفارش‌های editable قدیمی اجرا کن: python scripts/04_finance_safety_audit.py --fix-duplicates")
    return len(rows)


def audit_coupon_usage_integrity(conn: sqlite3.Connection) -> int:
    if not (table_exists(conn, "orders") and table_exists(conn, "coupon_usages")):
        return 0
    rows = conn.execute(
        """
        SELECT cu.id, cu.code, cu.user_telegram_id, cu.order_id, o.status
        FROM coupon_usages cu
        LEFT JOIN orders o ON o.id = cu.order_id
        WHERE o.id IS NULL OR o.status != 'paid'
        ORDER BY cu.id DESC
        """
    ).fetchall()
    if not rows:
        print("✅ مصرف نهایی کد تخفیف فقط روی سفارش‌های paid ثبت شده است.")
        return 0
    print("⚠️ coupon_usages مشکوک پیدا شد؛ این‌ها روی سفارش paid نیستند:")
    for r in rows[:50]:
        print(f"  - usage=#{r['id']} code={r['code']} user={r['user_telegram_id']} order={r['order_id']} order_status={r['status']}")
    return len(rows)


def audit_wallet_payments(conn: sqlite3.Connection) -> int:
    if not (table_exists(conn, "orders") and table_exists(conn, "wallet_transactions")):
        return 0
    issues = 0
    paid_wallet_orders = conn.execute(
        """
        SELECT * FROM orders
        WHERE status='paid' AND wallet_used > 0
        ORDER BY id DESC
        """
    ).fetchall()
    for o in paid_wallet_orders:
        expected = max(int(o["amount"] or 0) - int(o["discount_amount"] or 0), 0)
        if int(o["wallet_used"] or 0) != expected:
            issues += 1
            print(f"⚠️ wallet_used mismatch order=#{o['id']} wallet_used={fmt_money(o['wallet_used'])} expected={fmt_money(expected)}")
        like = f"%#{o['id']}%"
        tx = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS s, COUNT(*) AS c FROM wallet_transactions WHERE user_telegram_id=? AND amount<0 AND description LIKE ?",
            (int(o["user_telegram_id"]), like),
        ).fetchone()
        if tx and int(tx["s"] or 0) != -int(o["wallet_used"] or 0):
            issues += 1
            print(f"⚠️ wallet transaction mismatch order=#{o['id']} tx_sum={fmt_money(tx['s'])} wallet_used={fmt_money(o['wallet_used'])}")
    if issues == 0:
        print("✅ پرداخت‌های کیف پول با wallet_used و تراکنش منفی هم‌خوان هستند.")
    return issues


def audit_negative_wallets(conn: sqlite3.Connection) -> int:
    if not table_exists(conn, "users"):
        return 0
    rows = conn.execute("SELECT telegram_id, wallet_balance FROM users WHERE wallet_balance < 0 ORDER BY wallet_balance ASC").fetchall()
    if not rows:
        print("✅ کیف پول منفی پیدا نشد.")
        return 0
    print("⚠️ کیف پول منفی:")
    for r in rows[:50]:
        print(f"  - user={r['telegram_id']} balance={fmt_money(r['wallet_balance'])}")
    return len(rows)


def audit_processing_orders(conn: sqlite3.Connection) -> int:
    if not table_exists(conn, "orders"):
        return 0
    rows = conn.execute("SELECT id, user_telegram_id, plan_key, created_at FROM orders WHERE status='processing' ORDER BY id DESC").fetchall()
    if not rows:
        print("✅ سفارش گیرکرده در processing پیدا نشد.")
        return 0
    print("⚠️ سفارش‌های processing؛ اگر قدیمی هستند باید دستی بررسی شوند:")
    for r in rows[:50]:
        print(f"  - order=#{r['id']} user={r['user_telegram_id']} plan={r['plan_key']} created={r['created_at']}")
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fix-duplicates", action="store_true", help="Cancel older editable duplicate coupon reservations")
    args = parser.parse_args()
    print(f"DB: {DB_PATH}")
    with connect() as conn:
        total = 0
        total += audit_duplicate_coupon_reservations(conn, args.fix_duplicates)
        total += audit_coupon_usage_integrity(conn)
        total += audit_wallet_payments(conn)
        total += audit_negative_wallets(conn)
        total += audit_processing_orders(conn)
    print("\nنتیجه:", "✅ مشکل مهمی پیدا نشد" if total == 0 else f"⚠️ {total} مورد نیازمند بررسی/اصلاح پیدا شد")


if __name__ == "__main__":
    main()
