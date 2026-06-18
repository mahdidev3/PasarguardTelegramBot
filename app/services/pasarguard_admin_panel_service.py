"""Pasarguard admin panel utilities for Phase 4.9.

This module powers the Telegram admin UI around the Pasarguard integration:
health overview, current reconcile, orphan detection, logs and snapshots.  It is
read-only by default; write operations are only delegated to the existing
reconcile service after the confirmation flow.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from app.config import settings
from app.database import session_scope
from app.models import (
    CatalogPlan,
    PasarguardRemoteSnapshot,
    PasarguardSyncEvent,
    PasarguardSyncJob,
    PasarguardTemplate,
    PasarguardUser,
)
from app.services.backup_service import _pg_tables, _sqlite_tables  # internal exporter reused for current desired-state
from app.services.pasarguard_checkpoint_service import (
    PasarguardReconcileReport,
    desired_templates_from_pg_data,
    desired_users_from_data,
    reconcile_backup_with_pasarguard,
    render_reconcile_report,
    write_jsonl,
)
from app.services.pasarguard_client import PasarguardClient, connection_info
from app.services.pasarguard_template_service import is_managed_template, managed_marker, render_sync_report, sync_plan_templates


BYTES_PER_GB = 1024 * 1024 * 1024


@dataclass
class PasarguardAdminOverview:
    enabled: bool
    dry_run: bool
    base_url: str
    marker: str
    local_plans: int = 0
    local_active_plans: int = 0
    local_services_with_remote: int = 0
    local_template_mappings: int = 0
    local_user_mappings: int = 0
    remote_templates: int = 0
    remote_users: int = 0
    managed_remote_templates: int = 0
    managed_remote_users: int = 0
    orphan_remote_users: int = 0
    recent_failed_jobs: int = 0
    last_job_line: str = "ندارد"
    error: str | None = None


@dataclass
class OrphanUserReport:
    enabled: bool
    remote_users: int = 0
    managed_remote_users: int = 0
    local_usernames: int = 0
    orphan_users: list[dict[str, Any]] = field(default_factory=list)
    possible_external_users: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


@dataclass
class SyncLogsReport:
    jobs: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class SnapshotReport:
    snapshots: list[dict[str, Any]] = field(default_factory=list)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _safe_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except Exception:
        return None


def _sqlite_remote_usernames() -> set[str]:
    db_path = Path(settings.database_path)
    if not db_path.exists():
        return set()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(services)").fetchall()]
        if "pasarguard_username" not in cols:
            return set()
        return {
            str(row[0]).strip()
            for row in conn.execute("SELECT pasarguard_username FROM services WHERE pasarguard_username IS NOT NULL AND TRIM(pasarguard_username) != ''").fetchall()
            if str(row[0]).strip()
        }
    finally:
        conn.close()


def _sqlite_remote_service_count() -> int:
    db_path = Path(settings.database_path)
    if not db_path.exists():
        return 0
    conn = sqlite3.connect(db_path)
    try:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(services)").fetchall()]
        if "pasarguard_username" not in cols:
            return 0
        row = conn.execute("SELECT COUNT(*) FROM services WHERE pasarguard_username IS NOT NULL AND TRIM(pasarguard_username) != ''").fetchone()
        return int(row[0] or 0) if row else 0
    finally:
        conn.close()


def _remote_username(remote: dict[str, Any]) -> str:
    return str(remote.get("username") or remote.get("name") or "").strip()


def _remote_id(remote: dict[str, Any]) -> int | None:
    return _safe_int(remote.get("id"))


def _remote_note(remote: dict[str, Any]) -> str:
    for key in ("note", "description", "comment"):
        if remote.get(key):
            return str(remote[key])
    return ""


def is_managed_remote_user(remote: dict[str, Any]) -> bool:
    username = _remote_username(remote).lower()
    note = _remote_note(remote)
    marker = managed_marker()
    configured_prefix = (settings.pasarguard_username_prefix or "hts_").lower()
    return (
        marker in note
        or "[HOWTOOSEE_BOT_START]" in note
        or username.startswith(configured_prefix)
        or username.startswith("hts_")
        or username.startswith("howtosee_")
    )


async def _local_pg_usernames() -> set[str]:
    async with session_scope() as session:
        rows = (await session.execute(select(PasarguardUser.remote_username))).all()
    return {str(row[0]).strip() for row in rows if row[0] and str(row[0]).strip()}


async def current_desired_state_zip() -> Path:
    """Build a temporary ZIP with current desired templates/users.

    This avoids downloading ticket files or creating a full backup while letting
    the existing reconcile engine compare current bot state with live Pasarguard.
    """
    sqlite_data = _sqlite_tables()
    pg_data = await _pg_tables()
    desired_templates = desired_templates_from_pg_data(pg_data)
    desired_users = desired_users_from_data(sqlite_data, pg_data)
    tmp_dir = Path(tempfile.mkdtemp(prefix="howtoosee-pg-current-"))
    write_jsonl(tmp_dir / "external" / "pasarguard_desired_templates.jsonl", desired_templates)
    write_jsonl(tmp_dir / "external" / "pasarguard_desired_state.jsonl", desired_users)
    manifest = {
        "backup_version": "current-desired-state",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": "phase4.9 current bot state",
        "desired_templates": len(desired_templates),
        "desired_users": len(desired_users),
    }
    (tmp_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    zip_path = tmp_dir / "current-pasarguard-desired-state.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(p for p in tmp_dir.rglob("*") if p.is_file() and p != zip_path):
            zf.write(path, path.relative_to(tmp_dir).as_posix())
    return zip_path


async def reconcile_current_state(*, admin_id: int | None, dry_run: bool = True) -> PasarguardReconcileReport:
    zip_path = await current_desired_state_zip()
    return await reconcile_backup_with_pasarguard(zip_path, admin_id=admin_id, dry_run=dry_run)


async def get_pasarguard_overview() -> PasarguardAdminOverview:
    info = connection_info()
    overview = PasarguardAdminOverview(info.enabled, info.dry_run, info.base_url, info.managed_prefix)
    try:
        async with session_scope() as session:
            overview.local_plans = int((await session.execute(select(func.count()).select_from(CatalogPlan))).scalar() or 0)
            overview.local_active_plans = int((await session.execute(select(func.count()).select_from(CatalogPlan).where(CatalogPlan.is_active.is_(True)))).scalar() or 0)
            overview.local_template_mappings = int((await session.execute(select(func.count()).select_from(PasarguardTemplate))).scalar() or 0)
            overview.local_user_mappings = int((await session.execute(select(func.count()).select_from(PasarguardUser))).scalar() or 0)
            overview.recent_failed_jobs = int((await session.execute(select(func.count()).select_from(PasarguardSyncJob).where(PasarguardSyncJob.status == "failed"))).scalar() or 0)
            last_job = (await session.execute(select(PasarguardSyncJob).order_by(PasarguardSyncJob.id.desc()).limit(1))).scalar_one_or_none()
            if last_job:
                overview.last_job_line = f"#{last_job.id} | {last_job.mode} | {last_job.status} | failed={last_job.failed_count}"
        overview.local_services_with_remote = _sqlite_remote_service_count()
        if info.enabled:
            async with PasarguardClient() as client:
                templates = await client.list_user_templates(limit=5000)
                users = await client.list_users(limit=10000)
            overview.remote_templates = len(templates)
            overview.remote_users = len(users)
            overview.managed_remote_templates = len([t for t in templates if is_managed_template(t)])
            managed_users = [u for u in users if is_managed_remote_user(u)]
            overview.managed_remote_users = len(managed_users)
            local_usernames = _sqlite_remote_usernames() | await _local_pg_usernames()
            overview.orphan_remote_users = len([u for u in managed_users if _remote_username(u) and _remote_username(u) not in local_usernames])
    except Exception as exc:
        overview.error = str(exc)
    return overview


async def detect_orphan_users(limit: int = 30) -> OrphanUserReport:
    info = connection_info()
    report = OrphanUserReport(enabled=info.enabled)
    if not info.enabled:
        report.error = "PASARGUARD_ENABLED=false است."
        return report
    try:
        local_usernames = _sqlite_remote_usernames() | await _local_pg_usernames()
        report.local_usernames = len(local_usernames)
        async with PasarguardClient() as client:
            users = await client.list_users(limit=10000)
        report.remote_users = len(users)
        managed = [u for u in users if is_managed_remote_user(u)]
        report.managed_remote_users = len(managed)
        for user in managed:
            username = _remote_username(user)
            if username and username not in local_usernames:
                report.orphan_users.append(user)
        for user in users:
            username = _remote_username(user)
            if username and username not in local_usernames and user not in report.orphan_users:
                report.possible_external_users.append(user)
        report.orphan_users = report.orphan_users[:limit]
        report.possible_external_users = report.possible_external_users[:limit]
    except Exception as exc:
        report.error = str(exc)
    return report


async def get_sync_logs(limit: int = 10) -> SyncLogsReport:
    async with session_scope() as session:
        jobs = list((await session.execute(select(PasarguardSyncJob).order_by(PasarguardSyncJob.id.desc()).limit(limit))).scalars().all())
        job_ids = [job.id for job in jobs]
        events = []
        if job_ids:
            events = list((await session.execute(select(PasarguardSyncEvent).where(PasarguardSyncEvent.job_id.in_(job_ids)).order_by(PasarguardSyncEvent.id.desc()).limit(limit * 3))).scalars().all())
    return SyncLogsReport(
        jobs=[{
            "id": job.id,
            "mode": job.mode,
            "dry_run": job.dry_run,
            "status": job.status,
            "total": job.total_items,
            "success": job.success_count,
            "failed": job.failed_count,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
        } for job in jobs],
        events=[{
            "id": event.id,
            "job_id": event.job_id,
            "service_id": event.service_id,
            "plan_key": event.plan_key,
            "remote_id": event.remote_id,
            "action": event.action,
            "error": event.error,
            "created_at": event.created_at,
        } for event in events],
    )


async def get_snapshots(limit: int = 12) -> SnapshotReport:
    async with session_scope() as session:
        snapshots = list((await session.execute(select(PasarguardRemoteSnapshot).order_by(PasarguardRemoteSnapshot.id.desc()).limit(limit))).scalars().all())
    return SnapshotReport(snapshots=[{
        "id": item.id,
        "type": item.snapshot_type,
        "source": item.source,
        "created_by": item.created_by,
        "created_at": item.created_at,
        "count": len(item.state_json or []) if isinstance(item.state_json, list) else (len(item.state_json or {}) if isinstance(item.state_json, dict) else 0),
    } for item in snapshots])


def render_overview(overview: PasarguardAdminOverview) -> str:
    lines = [
        f"وضعیت اتصال: {'فعال ✅' if overview.enabled else 'غیرفعال ⛔'}",
        f"حالت پیش‌فرض: {'Dry-run / امن' if overview.dry_run else 'اعمال واقعی ⚠️'}",
        f"Base URL: {overview.base_url}",
        f"Marker: {overview.marker}",
        "",
        f"پلن‌های بات: {overview.local_plans} | فعال: {overview.local_active_plans}",
        f"سرویس‌های local دارای remote username: {overview.local_services_with_remote}",
        f"mapping template در بات: {overview.local_template_mappings}",
        f"mapping user در بات: {overview.local_user_mappings}",
        "",
        f"templateهای پنل: {overview.remote_templates} | مدیریت‌شده توسط بات: {overview.managed_remote_templates}",
        f"userهای پنل: {overview.remote_users} | مدیریت‌شده/مشکوک به بات: {overview.managed_remote_users}",
        f"orphan remote users: {overview.orphan_remote_users}",
        "",
        f"آخرین job: {overview.last_job_line}",
        f"jobهای failed: {overview.recent_failed_jobs}",
    ]
    if overview.error:
        lines.append(f"\nخطا: {overview.error}")
    return "\n".join(lines)


def _short_remote_user(user: dict[str, Any]) -> str:
    username = _remote_username(user) or "بدون username"
    uid = _remote_id(user)
    status = user.get("status") or ("disabled" if user.get("is_disabled") else "")
    data_gb = "?"
    try:
        if user.get("data_limit") is not None:
            data_gb = f"{float(user.get('data_limit')) / BYTES_PER_GB:g}GB"
    except Exception:
        pass
    return f"#{uid or '-'} | {username} | {status or '-'} | {data_gb}"


def render_orphans(report: OrphanUserReport) -> str:
    lines = [
        f"وضعیت اتصال: {'فعال' if report.enabled else 'غیرفعال'}",
        f"userهای پنل: {report.remote_users}",
        f"userهای مدیریت‌شده/مشکوک به بات: {report.managed_remote_users}",
        f"usernameهای local: {report.local_usernames}",
        f"orphanهای مدیریت‌شده: {len(report.orphan_users)}",
    ]
    if report.error:
        lines.append(f"\nخطا: {report.error}")
        return "\n".join(lines)
    if report.orphan_users:
        lines.append("\nOrphanهای مدیریت‌شده توسط بات/marker:")
        for user in report.orphan_users[:20]:
            lines.append("• " + _short_remote_user(user))
    if report.possible_external_users:
        lines.append("\nUserهای پنل که در local mapping نیستند، اما marker بات ندارند:")
        for user in report.possible_external_users[:10]:
            lines.append("• " + _short_remote_user(user))
    if not report.orphan_users and not report.error:
        lines.append("\n✅ orphan مدیریت‌شده‌ای پیدا نشد.")
    return "\n".join(lines)


def render_sync_logs(report: SyncLogsReport) -> str:
    lines = ["آخرین jobها:"]
    if not report.jobs:
        lines.append("ندارد")
    for job in report.jobs:
        lines.append(
            f"• #{job['id']} | {job['mode']} | {'dry' if job['dry_run'] else 'real'} | {job['status']} | total={job['total']} ok={job['success']} fail={job['failed']}"
        )
    lines.append("\nآخرین eventها:")
    if not report.events:
        lines.append("ندارد")
    for event in report.events:
        err = f" | خطا: {event['error']}" if event.get("error") else ""
        lines.append(f"• #{event['id']} | job={event['job_id']} | {event['action']} | service={event['service_id'] or '-'} | plan={event['plan_key'] or '-'}{err}")
    return "\n".join(lines)


def render_snapshots(report: SnapshotReport) -> str:
    lines = ["آخرین snapshotهای remote:"]
    if not report.snapshots:
        lines.append("ندارد")
    for item in report.snapshots:
        lines.append(f"• #{item['id']} | {item['type']} | count={item['count']} | by={item['created_by'] or '-'} | {item['created_at']}")
    return "\n".join(lines)


__all__ = [
    "detect_orphan_users",
    "get_pasarguard_overview",
    "get_snapshots",
    "get_sync_logs",
    "reconcile_current_state",
    "render_orphans",
    "render_overview",
    "render_reconcile_report",
    "render_snapshots",
    "render_sync_logs",
]
