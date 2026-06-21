"""Durable deadline cleanup for receipts, admin confirmation codes, and timed jobs.

The important rule is that a deadline is stored in the database and is checked
again after every restart. FSM state alone is not trusted because in-memory FSM
state disappears when the bot stops.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import delete

from app.config import settings, TEHRAN_TZ
from app.database import session_scope
from app.models import AdminConfirmation

logger = logging.getLogger("howtosee-deadlines")

DEADLINE_CLEANUP_INTERVAL_SECONDS = int(os.getenv("DEADLINE_CLEANUP_INTERVAL_SECONDS", "60"))
RECEIPT_UPLOAD_WINDOW_MINUTES = int(os.getenv("RECEIPT_UPLOAD_WINDOW_MINUTES", "30"))
CONFIRMATION_RETENTION_HOURS = int(os.getenv("CONFIRMATION_RETENTION_HOURS", "24"))


def _now() -> datetime:
    return datetime.now(TEHRAN_TZ)


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TEHRAN_TZ)
        return dt.astimezone(TEHRAN_TZ)
    except Exception:
        return None


def _sqlite_path() -> Path:
    return Path(settings.database_path)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (table,)).fetchone()
    return bool(row)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def _ensure_deadline_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS deadline_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            details TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(entity_type, entity_id, event_type)
        )
        """
    )


def _record_event(conn: sqlite3.Connection, entity_type: str, entity_id: Any, event_type: str, details: str = "") -> None:
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO deadline_events (entity_type, entity_id, event_type, details, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (entity_type, str(entity_id), event_type, details[:1000], _now_iso()),
        )
    except Exception:
        # Expiration must never fail just because audit logging failed.
        pass


def _expire_receipt(conn: sqlite3.Connection, receipt: sqlite3.Row, *, now_iso: str) -> bool:
    receipt_id = int(receipt["id"])
    order_id = int(receipt["order_id"])
    status = str(receipt["status"] or "")
    if status != "waiting_receipt":
        return False

    conn.execute(
        """
        UPDATE payment_receipts
        SET status = 'expired',
            receipt_file_id = NULL,
            receipt_file_unique_id = NULL,
            receipt_file_type = NULL,
            receipt_message_id = NULL,
            receipt_chat_id = NULL,
            updated_at = ?
        WHERE id = ? AND status = 'waiting_receipt'
        """,
        (now_iso, receipt_id),
    )
    # Files that were uploaded but not submitted before the deadline must not be
    # treated as a real transaction. Removing the DB references also keeps future
    # backups from downloading stale receipt media.
    if _table_exists(conn, "payment_receipt_files"):
        conn.execute("DELETE FROM payment_receipt_files WHERE receipt_id = ?", (receipt_id,))
    conn.execute(
        """
        UPDATE orders
        SET status = 'expired', admin_note = COALESCE(admin_note, 'مهلت ارسال رسید تمام شد.')
        WHERE id = ? AND status IN ('pending', 'payment_rejected')
        """,
        (order_id,),
    )
    _record_event(conn, "payment_receipt", receipt_id, "expired", f"order_id={order_id}")
    _record_event(conn, "order", order_id, "expired", f"receipt_id={receipt_id}")
    return True


def expire_sqlite_payment_deadlines(now: datetime | None = None) -> dict[str, int]:
    """Finalize expired payment windows in legacy SQLite.

    This function is safe to call often. It is idempotent and uses persistent DB
    statuses rather than in-memory FSM state.
    """
    path = _sqlite_path()
    if not path.exists():
        return {"receipts_expired": 0, "orders_expired": 0, "files_removed": 0}

    now_dt = (now or _now()).astimezone(TEHRAN_TZ)
    now_iso = now_dt.isoformat(timespec="seconds")
    receipts_expired = 0
    orders_expired = 0
    files_removed = 0

    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        if not _table_exists(conn, "orders"):
            return {"receipts_expired": 0, "orders_expired": 0, "files_removed": 0}
        _ensure_deadline_schema(conn)

        if _table_exists(conn, "payment_receipts"):
            receipt_cols = _columns(conn, "payment_receipts")
            if {"id", "order_id", "status", "expires_at"}.issubset(receipt_cols):
                rows = conn.execute(
                    "SELECT * FROM payment_receipts WHERE status = 'waiting_receipt' AND expires_at IS NOT NULL"
                ).fetchall()
                for receipt in rows:
                    deadline = _parse_dt(receipt["expires_at"])
                    if deadline and deadline <= now_dt:
                        before = 0
                        if _table_exists(conn, "payment_receipt_files"):
                            count_row = conn.execute("SELECT COUNT(*) AS c FROM payment_receipt_files WHERE receipt_id = ?", (int(receipt["id"]),)).fetchone()
                            before = int(count_row["c"] if count_row else 0)
                        if _expire_receipt(conn, receipt, now_iso=now_iso):
                            receipts_expired += 1
                            orders_expired += 1
                            files_removed += before

        # Wallet top-up orders that never reached the receipt page should not live
        # forever in the user's transaction list. They are checkout attempts, not
        # completed wallet transactions.
        order_cols = _columns(conn, "orders")
        if {"id", "status", "plan_key", "created_at"}.issubset(order_cols):
            rows = conn.execute(
                """
                SELECT * FROM orders
                WHERE status IN ('pending', 'payment_rejected')
                  AND plan_key LIKE 'wallet_topup:%'
                """
            ).fetchall()
            for order in rows:
                # Skip orders that already have an active or submitted receipt.
                active_receipt = None
                if _table_exists(conn, "payment_receipts"):
                    active_receipt = conn.execute(
                        """
                        SELECT id FROM payment_receipts
                        WHERE order_id = ? AND status IN ('waiting_receipt', 'receipt_pending', 'approved')
                        ORDER BY id DESC LIMIT 1
                        """,
                        (int(order["id"]),),
                    ).fetchone()
                if active_receipt:
                    continue
                created = _parse_dt(order["created_at"])
                if created and created + timedelta(minutes=RECEIPT_UPLOAD_WINDOW_MINUTES) <= now_dt:
                    cur = conn.execute(
                        """
                        UPDATE orders
                        SET status = 'expired', admin_note = COALESCE(admin_note, 'مهلت تکمیل پرداخت تمام شد.')
                        WHERE id = ? AND status IN ('pending', 'payment_rejected')
                        """,
                        (int(order["id"]),),
                    )
                    if cur.rowcount:
                        orders_expired += int(cur.rowcount)
                        _record_event(conn, "order", int(order["id"]), "expired", "wallet_topup_without_active_receipt")

        conn.commit()

    return {"receipts_expired": receipts_expired, "orders_expired": orders_expired, "files_removed": files_removed}


async def cleanup_expired_admin_confirmations(now: datetime | None = None) -> dict[str, int]:
    """Remove old unusable numeric confirmation codes after a short retention.

    Verification already rejects expired codes immediately. Cleanup is only for
    DB hygiene and protects the table from growing forever.
    """
    now_dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = now_dt - timedelta(hours=max(1, CONFIRMATION_RETENTION_HOURS))
    async with session_scope() as session:
        result = await session.execute(
            delete(AdminConfirmation).where(
                AdminConfirmation.expires_at < cutoff,
                AdminConfirmation.used_at.is_(None),
            )
        )
        return {"admin_confirmations_deleted": int(result.rowcount or 0)}


async def run_deadline_cleanup_once() -> dict[str, int]:
    payment = expire_sqlite_payment_deadlines()
    confirmations = await cleanup_expired_admin_confirmations()
    return {**payment, **confirmations}


async def deadline_cleanup_loop() -> None:
    while True:
        try:
            await run_deadline_cleanup_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("deadline cleanup failed")
        await asyncio.sleep(max(10, DEADLINE_CLEANUP_INTERVAL_SECONDS))


def start_deadline_cleanup_scheduler() -> asyncio.Task:
    return asyncio.create_task(deadline_cleanup_loop(), name="howtosee_deadline_cleanup_loop")
