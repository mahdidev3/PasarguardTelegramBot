"""Durable periodic jobs for runtime cleanup tasks.

Jobs are stored in legacy SQLite because the affected runtime state (orders,
receipts and package/order flows) is also still bridged through SQLite. The
scheduler is catch-up aware: after a restart, every due job is executed once and
then its next_run_at is recalculated from the actual finish time.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.config import settings, TEHRAN_TZ
from app.database import session_scope
from app.models import Ticket
from app.services.deadline_service import run_deadline_cleanup_once

logger = logging.getLogger("howtosee-jobs")
JOB_LOOP_SECONDS = 60


@dataclass(frozen=True)
class JobSpec:
    key: str
    title: str
    description: str
    default_interval_minutes: int


DEFAULT_JOBS: dict[str, JobSpec] = {
    "receipt_deadline_cleanup": JobSpec(
        key="receipt_deadline_cleanup",
        title="منقضی‌سازی رسیدها",
        description="کل دیتابیس را اسکن می‌کند و سفارش‌ها/رسیدهایی را که مهلت ارسال رسیدشان گذشته، بدون حذف‌کردن داده یا فایل، به وضعیت منقضی‌شده تبدیل می‌کند.",
        default_interval_minutes=1,
    ),
    "closed_ticket_cleanup": JobSpec(
        key="closed_ticket_cleanup",
        title="حذف تیکت‌های بسته‌شده",
        description="تیکت‌هایی را که بیش از ۲۴ ساعت از بسته‌شدنشان گذشته حذف می‌کند. فایل‌های تیکت از قبل هنگام بستن از دسترس ربات پاک می‌شوند.",
        default_interval_minutes=60,
    ),
}


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


def _connect() -> sqlite3.Connection:
    path = _sqlite_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_job_schema() -> None:
    with closing(_connect()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduled_jobs (
                job_key TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                interval_seconds INTEGER NOT NULL,
                last_run_at TEXT,
                next_run_at TEXT,
                last_status TEXT,
                last_summary TEXT,
                last_error TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        now = _now()
        for spec in DEFAULT_JOBS.values():
            interval = max(60, int(spec.default_interval_minutes) * 60)
            conn.execute(
                """
                INSERT INTO scheduled_jobs
                    (job_key, title, description, enabled, interval_seconds, next_run_at, updated_at)
                VALUES (?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(job_key) DO UPDATE SET
                    title = excluded.title,
                    description = excluded.description,
                    interval_seconds = CASE
                        WHEN scheduled_jobs.interval_seconds IS NULL OR scheduled_jobs.interval_seconds < 60
                        THEN excluded.interval_seconds ELSE scheduled_jobs.interval_seconds END,
                    next_run_at = COALESCE(scheduled_jobs.next_run_at, excluded.next_run_at),
                    updated_at = excluded.updated_at
                """,
                (spec.key, spec.title, spec.description, interval, (now + timedelta(seconds=interval)).isoformat(timespec="seconds"), now.isoformat(timespec="seconds")),
            )
        conn.commit()


def list_jobs() -> list[sqlite3.Row]:
    ensure_job_schema()
    with closing(_connect()) as conn:
        return list(conn.execute("SELECT * FROM scheduled_jobs ORDER BY job_key").fetchall())


def get_job(job_key: str) -> sqlite3.Row | None:
    ensure_job_schema()
    with closing(_connect()) as conn:
        return conn.execute("SELECT * FROM scheduled_jobs WHERE job_key = ?", (job_key,)).fetchone()


def update_job_interval(job_key: str, interval_minutes: int) -> bool:
    ensure_job_schema()
    interval_seconds = max(60, min(int(interval_minutes) * 60, 90 * 24 * 3600))
    now = _now()
    with closing(_connect()) as conn:
        cur = conn.execute(
            """
            UPDATE scheduled_jobs
            SET interval_seconds = ?, next_run_at = ?, updated_at = ?
            WHERE job_key = ?
            """,
            (interval_seconds, (now + timedelta(seconds=interval_seconds)).isoformat(timespec="seconds"), now.isoformat(timespec="seconds"), job_key),
        )
        conn.commit()
        return bool(cur.rowcount)


def set_job_enabled(job_key: str, enabled: bool) -> bool:
    ensure_job_schema()
    now = _now()
    job = get_job(job_key)
    if not job:
        return False
    next_run = (now + timedelta(seconds=max(60, int(job["interval_seconds"] or 60)))).isoformat(timespec="seconds")
    with closing(_connect()) as conn:
        cur = conn.execute(
            """
            UPDATE scheduled_jobs
            SET enabled = ?, next_run_at = CASE WHEN ? = 1 THEN ? ELSE next_run_at END, updated_at = ?
            WHERE job_key = ?
            """,
            (1 if enabled else 0, 1 if enabled else 0, next_run, now.isoformat(timespec="seconds"), job_key),
        )
        conn.commit()
        return bool(cur.rowcount)


def _job_summary_line(result: dict[str, int]) -> str:
    if not result:
        return "بدون تغییر"
    return "، ".join(f"{key}={value}" for key, value in sorted(result.items()))


async def cleanup_closed_tickets_older_than(hours: int = 24) -> dict[str, int]:
    cutoff = datetime.now(TEHRAN_TZ) - timedelta(hours=hours)
    async with session_scope() as session:
        result = await session.execute(
            select(Ticket).where(Ticket.status == "closed", Ticket.closed_at.is_not(None), Ticket.closed_at <= cutoff)
        )
        tickets = list(result.scalars().all())
        for ticket in tickets:
            await session.delete(ticket)
        return {"closed_tickets_deleted": len(tickets)}


async def run_job(job_key: str) -> dict[str, int]:
    if job_key == "receipt_deadline_cleanup":
        return await run_deadline_cleanup_once()
    if job_key == "closed_ticket_cleanup":
        return await cleanup_closed_tickets_older_than(24)
    raise ValueError(f"unknown job: {job_key}")


def _save_job_success(job_key: str, result: dict[str, int]) -> None:
    job = get_job(job_key)
    if not job:
        return
    finished_at = _now()
    interval = max(60, int(job["interval_seconds"] or 60))
    with closing(_connect()) as conn:
        conn.execute(
            """
            UPDATE scheduled_jobs
            SET last_run_at = ?, next_run_at = ?, last_status = 'ok', last_summary = ?, last_error = NULL, updated_at = ?
            WHERE job_key = ?
            """,
            (
                finished_at.isoformat(timespec="seconds"),
                (finished_at + timedelta(seconds=interval)).isoformat(timespec="seconds"),
                _job_summary_line(result),
                finished_at.isoformat(timespec="seconds"),
                job_key,
            ),
        )
        conn.commit()


def _save_job_error(job_key: str, error: BaseException) -> None:
    now = _now_iso()
    with closing(_connect()) as conn:
        conn.execute(
            """
            UPDATE scheduled_jobs
            SET last_run_at = ?, last_status = 'error', last_error = ?, updated_at = ?
            WHERE job_key = ?
            """,
            (now, str(error)[:1000], now, job_key),
        )
        conn.commit()


async def run_job_and_record(job_key: str) -> dict[str, int]:
    try:
        result = await run_job(job_key)
    except Exception as exc:
        _save_job_error(job_key, exc)
        raise
    _save_job_success(job_key, result)
    return result


async def run_due_jobs_once(*, include_disabled: bool = False) -> dict[str, dict[str, int]]:
    ensure_job_schema()
    now = _now()
    due: list[str] = []
    with closing(_connect()) as conn:
        rows = conn.execute("SELECT * FROM scheduled_jobs ORDER BY job_key").fetchall()
        for row in rows:
            if not include_disabled and int(row["enabled"] or 0) != 1:
                continue
            next_run = _parse_dt(row["next_run_at"])
            if next_run is None or next_run <= now:
                due.append(str(row["job_key"]))
    results: dict[str, dict[str, int]] = {}
    for job_key in due:
        try:
            results[job_key] = await run_job_and_record(job_key)
        except Exception:
            logger.exception("scheduled job failed: %s", job_key)
    return results


async def job_scheduler_loop() -> None:
    ensure_job_schema()
    while True:
        try:
            await run_due_jobs_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("job scheduler loop failed")
        await asyncio.sleep(JOB_LOOP_SECONDS)


def start_job_scheduler() -> asyncio.Task:
    return asyncio.create_task(job_scheduler_loop(), name="howtosee_job_scheduler_loop")


