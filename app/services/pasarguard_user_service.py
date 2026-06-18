"""Pasarguard user/service operations for Phase 4.5 and 4.6.

This module bridges the legacy SQLite service records with Pasarguard users.
It keeps Telegram handlers small and makes every remote operation safe:
- PASARGUARD_ENABLED=false => no-op
- PASARGUARD_DRY_RUN=true => report-only no-op
- delete operations are converted to disable, never remote delete
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import select, update

from app.config import settings
from app.database import session_scope
from app.models import CatalogPlan, PasarguardSyncEvent, PasarguardSyncJob, PasarguardUser
from app.services.pasarguard_client import PasarguardAPIError, PasarguardClient, connection_info
from app.services.pasarguard_template_service import managed_marker, sync_plan_templates, template_name_for_plan

BYTES_PER_GB = 1024 * 1024 * 1024
BYTES_PER_MB = 1024 * 1024


@dataclass
class RemoteServiceResult:
    ok: bool
    action: str
    applied: bool = False
    skipped: bool = False
    message: str = ""
    remote_username: str | None = None
    remote_user_id: int | None = None
    subscription_url: str | None = None
    remote_state: dict[str, Any] | None = None
    error: str | None = None

    def notice(self) -> str:
        if self.ok and self.applied:
            return f"\n\n🔌 Pasarguard: {self.message or 'عملیات remote با موفقیت انجام شد.'}"
        if self.skipped:
            return f"\n\n🔌 Pasarguard: {self.message}"
        if not self.ok:
            return f"\n\n⚠️ Pasarguard: {self.error or self.message or 'عملیات remote ناموفق بود.'}"
        return ""


def integration_ready() -> tuple[bool, str]:
    info = connection_info()
    if not info.enabled:
        return False, "غیرفعال است؛ فقط دیتابیس داخلی بروزرسانی شد."
    if settings.pasarguard_dry_run:
        return False, "Dry-run روشن است؛ عملیات واقعی روی پنل انجام نشد."
    return True, ""


def _row_get(row: Mapping[str, Any] | Any, key: str, default: Any = None) -> Any:
    try:
        if hasattr(row, "keys") and key in row.keys():
            return row[key]
        return row[key]
    except Exception:
        return default


def _remote_id(remote: dict[str, Any] | None) -> int | None:
    if not remote:
        return None
    raw = remote.get("id")
    try:
        return int(raw) if raw is not None else None
    except Exception:
        return None


def _parse_remote_expire(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, int):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return None
    return None


def sanitize_remote_username(value: str, telegram_id: int, service_id: int) -> str:
    raw = (value or "").strip().lower()
    raw = re.sub(r"[^a-z0-9_\-]+", "_", raw).strip("_")
    if not raw:
        raw = f"hts_{telegram_id}_{service_id}"
    if not raw.startswith("hts_") and not raw.startswith("howtosee_"):
        raw = f"hts_{raw}"
    # keep enough uniqueness at the end
    if len(raw) > 48:
        raw = f"{raw[:32]}_{telegram_id % 100000}_{service_id}"
    return raw[:64]


def build_managed_note(*, telegram_id: int, service_id: int, order_id: int | None, plan_key: str, action: str) -> str:
    return (
        "[HOWTOOSEE_BOT_START]\n"
        f"managed_by=howtoosee_bot\n"
        f"marker={managed_marker()}\n"
        f"action={action}\n"
        f"plan_key={plan_key}\n"
        f"telegram_id={telegram_id}\n"
        f"service_id={service_id}\n"
        f"order_id={order_id or ''}\n"
        "[HOWTOOSEE_BOT_END]"
    )[:500]


async def _create_job(action: str, service_id: int | None = None) -> int:
    async with session_scope() as session:
        job = PasarguardSyncJob(mode=action, dry_run=settings.pasarguard_dry_run, status="running", total_items=1)
        session.add(job)
        await session.flush()
        return int(job.id)


async def _finish_job(job_id: int, result: RemoteServiceResult) -> None:
    async with session_scope() as session:
        await session.execute(
            update(PasarguardSyncJob)
            .where(PasarguardSyncJob.id == job_id)
            .values(
                status="done" if result.ok else "failed",
                success_count=1 if result.ok else 0,
                failed_count=0 if result.ok else 1,
                report_json={
                    "action": result.action,
                    "applied": result.applied,
                    "skipped": result.skipped,
                    "message": result.message,
                    "error": result.error,
                    "remote_username": result.remote_username,
                    "remote_user_id": result.remote_user_id,
                    "subscription_url": result.subscription_url,
                },
                finished_at=datetime.now(timezone.utc),
            )
        )


async def _log_event(job_id: int | None, service_id: int | None, action: str, remote_id: str | None, old: dict[str, Any] | None, new: dict[str, Any] | None, error: str | None = None, plan_key: str | None = None) -> None:
    async with session_scope() as session:
        session.add(
            PasarguardSyncEvent(
                job_id=job_id,
                service_id=service_id,
                plan_key=plan_key,
                remote_id=remote_id,
                action=action,
                old_state_json=old,
                new_state_json=new,
                error=error,
            )
        )


async def _upsert_remote_user_mapping(
    *,
    service_id: int,
    telegram_id: int,
    plan_key: str,
    username: str,
    template_id: int | None,
    remote: dict[str, Any] | None,
    status: str,
    error: str | None = None,
) -> None:
    rid = _remote_id(remote)
    subscription_url = str((remote or {}).get("subscription_url") or "") or None
    async with session_scope() as session:
        item = (await session.execute(select(PasarguardUser).where(PasarguardUser.service_id == service_id))).scalar_one_or_none()
        if item is None:
            item = (await session.execute(select(PasarguardUser).where(PasarguardUser.remote_username == username))).scalar_one_or_none()
        if item is None:
            item = PasarguardUser(service_id=service_id, remote_username=username, managed_marker=managed_marker())
            session.add(item)
        item.service_id = service_id
        item.user_telegram_id = telegram_id
        item.plan_key = plan_key
        item.remote_user_id = rid
        item.remote_username = username
        item.remote_template_id = template_id
        item.subscription_url = subscription_url
        item.status = str((remote or {}).get("status") or status)
        item.data_limit_bytes = (remote or {}).get("data_limit")
        item.used_traffic_bytes = (remote or {}).get("used_traffic")
        item.expire_at = _parse_remote_expire((remote or {}).get("expire"))
        item.managed_marker = managed_marker()
        item.last_sync_at = datetime.now(timezone.utc)
        item.last_remote_state_json = remote
        item.sync_status = status
        item.last_error = error


async def ensure_template_for_plan(plan_key: str) -> tuple[int | None, CatalogPlan | None, str | None]:
    """Return remote template id for a plan, syncing local mapping when needed."""
    async with session_scope() as session:
        plan = (await session.execute(select(CatalogPlan).where(CatalogPlan.key == plan_key))).scalar_one_or_none()
    if not plan:
        return None, None, f"پلن {plan_key} در دیتابیس پلن‌ها پیدا نشد."
    if getattr(plan, "pasarguard_template_id", None):
        return int(plan.pasarguard_template_id), plan, None
    if settings.pasarguard_dry_run:
        return None, plan, "برای ساخت user واقعی، اول باید sync templateها را با dry-run خاموش انجام بدهی."
    report = await sync_plan_templates(None, dry_run=False)
    if report.failed_count:
        return None, plan, "sync templateها خطا داشت؛ اول بخش Pasarguard را بررسی کن."
    async with session_scope() as session:
        refreshed = (await session.execute(select(CatalogPlan).where(CatalogPlan.key == plan_key))).scalar_one_or_none()
    if refreshed and getattr(refreshed, "pasarguard_template_id", None):
        return int(refreshed.pasarguard_template_id), refreshed, None
    return None, plan, f"template مرتبط با پلن {plan_key} پیدا/ساخته نشد."


def _sqlite_update_remote(sqlite_db: Any, service_id: int, *, remote: dict[str, Any] | None = None, username: str | None = None, template_id: int | None = None, status: str = "synced", error: str | None = None) -> None:
    remote = remote or {}
    remote_user_id = _remote_id(remote)
    subscription_url = str(remote.get("subscription_url") or "") or None
    if username is None:
        username = str(remote.get("username") or "") or None
    # Keep this SQL local to legacy sqlite; PostgreSQL mappings are handled above.
    with sqlite_db.connect() as conn:
        conn.execute(
            """
            UPDATE services
            SET pasarguard_user_id = COALESCE(?, pasarguard_user_id),
                pasarguard_username = COALESCE(?, pasarguard_username),
                pasarguard_template_id = COALESCE(?, pasarguard_template_id),
                pasarguard_subscription_url = COALESCE(?, pasarguard_subscription_url),
                pasarguard_last_sync_at = ?,
                pasarguard_last_state_json = ?,
                pasarguard_sync_status = ?,
                pasarguard_sync_error = ?
            WHERE id = ?
            """,
            (
                remote_user_id,
                username,
                template_id,
                subscription_url,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                __import__("json").dumps(remote, ensure_ascii=False) if remote else None,
                status,
                error,
                service_id,
            ),
        )
        conn.commit()


async def create_remote_user_for_service(sqlite_db: Any, service: Mapping[str, Any], *, order_id: int | None = None) -> RemoteServiceResult:
    action = "create_user_from_template"
    service_id = int(_row_get(service, "id"))
    telegram_id = int(_row_get(service, "user_telegram_id"))
    plan_key = str(_row_get(service, "plan_key"))
    username = sanitize_remote_username(str(_row_get(service, "name", "")), telegram_id, service_id)

    ready, reason = integration_ready()
    if not ready:
        _sqlite_update_remote(sqlite_db, service_id, username=username, status="skipped", error=reason)
        return RemoteServiceResult(True, action, skipped=True, message=reason, remote_username=username)

    template_id, _plan, error = await ensure_template_for_plan(plan_key)
    if error or not template_id:
        _sqlite_update_remote(sqlite_db, service_id, username=username, status="error", error=error)
        return RemoteServiceResult(False, action, error=error or "template id خالی است.", remote_username=username)

    job_id = await _create_job(action, service_id)
    try:
        payload = {
            "user_template_id": template_id,
            "username": username,
            "note": build_managed_note(telegram_id=telegram_id, service_id=service_id, order_id=order_id, plan_key=plan_key, action="create"),
        }
        async with PasarguardClient() as client:
            remote = await client.create_user_from_template(payload)
        actual_username = str(remote.get("username") or username)
        _sqlite_update_remote(sqlite_db, service_id, remote=remote, username=actual_username, template_id=template_id, status="synced")
        await _upsert_remote_user_mapping(service_id=service_id, telegram_id=telegram_id, plan_key=plan_key, username=actual_username, template_id=template_id, remote=remote, status="synced")
        result = RemoteServiceResult(True, action, applied=True, message="user واقعی از template ساخته شد.", remote_username=actual_username, remote_user_id=_remote_id(remote), subscription_url=remote.get("subscription_url"), remote_state=remote)
        await _log_event(job_id, service_id, action, str(_remote_id(remote) or ""), None, remote, plan_key=plan_key)
    except Exception as exc:
        msg = str(exc)
        _sqlite_update_remote(sqlite_db, service_id, username=username, template_id=template_id, status="error", error=msg)
        result = RemoteServiceResult(False, action, error=msg, remote_username=username)
        await _log_event(job_id, service_id, action, None, None, None, msg, plan_key=plan_key)
    await _finish_job(job_id, result)
    return result


async def apply_template_to_remote_user(sqlite_db: Any, service: Mapping[str, Any], *, order_id: int | None = None) -> RemoteServiceResult:
    action = "modify_user_with_template"
    service_id = int(_row_get(service, "id"))
    telegram_id = int(_row_get(service, "user_telegram_id"))
    plan_key = str(_row_get(service, "plan_key"))
    username = str(_row_get(service, "pasarguard_username") or "") or sanitize_remote_username(str(_row_get(service, "name", "")), telegram_id, service_id)
    ready, reason = integration_ready()
    if not ready:
        _sqlite_update_remote(sqlite_db, service_id, username=username, status="skipped", error=reason)
        return RemoteServiceResult(True, action, skipped=True, message=reason, remote_username=username)
    template_id, _plan, error = await ensure_template_for_plan(plan_key)
    if error or not template_id:
        return RemoteServiceResult(False, action, error=error or "template id خالی است.", remote_username=username)
    job_id = await _create_job(action, service_id)
    try:
        payload = {"user_template_id": template_id, "note": build_managed_note(telegram_id=telegram_id, service_id=service_id, order_id=order_id, plan_key=plan_key, action="modify_template")}
        async with PasarguardClient() as client:
            remote = await client.modify_user_with_template(username, payload)
        actual_username = str(remote.get("username") or username)
        _sqlite_update_remote(sqlite_db, service_id, remote=remote, username=actual_username, template_id=template_id, status="synced")
        await _upsert_remote_user_mapping(service_id=service_id, telegram_id=telegram_id, plan_key=plan_key, username=actual_username, template_id=template_id, remote=remote, status="synced")
        result = RemoteServiceResult(True, action, applied=True, message="remote user با template جدید بروزرسانی شد.", remote_username=actual_username, remote_user_id=_remote_id(remote), subscription_url=remote.get("subscription_url"), remote_state=remote)
        await _log_event(job_id, service_id, action, str(_remote_id(remote) or ""), None, remote, plan_key=plan_key)
    except Exception as exc:
        result = RemoteServiceResult(False, action, error=str(exc), remote_username=username)
        _sqlite_update_remote(sqlite_db, service_id, username=username, template_id=template_id, status="error", error=str(exc))
        await _log_event(job_id, service_id, action, None, None, None, str(exc), plan_key=plan_key)
    await _finish_job(job_id, result)
    return result


async def update_remote_user_limit(sqlite_db: Any, service: Mapping[str, Any]) -> RemoteServiceResult:
    action = "update_user_data_limit"
    service_id = int(_row_get(service, "id"))
    username = str(_row_get(service, "pasarguard_username") or "")
    if not username:
        return RemoteServiceResult(True, action, skipped=True, message="remote username ذخیره نشده؛ فقط دیتابیس داخلی آپدیت شد.")
    ready, reason = integration_ready()
    if not ready:
        _sqlite_update_remote(sqlite_db, service_id, username=username, status="skipped", error=reason)
        return RemoteServiceResult(True, action, skipped=True, message=reason, remote_username=username)
    total_bytes = int(float(_row_get(service, "data_gb", 0)) * BYTES_PER_GB)
    job_id = await _create_job(action, service_id)
    try:
        async with PasarguardClient() as client:
            remote = await client.update_user_by_username(username, {"data_limit": total_bytes})
        _sqlite_update_remote(sqlite_db, service_id, remote=remote, username=username, status="synced")
        result = RemoteServiceResult(True, action, applied=True, message="حجم remote user بروزرسانی شد.", remote_username=username, remote_user_id=_remote_id(remote), subscription_url=remote.get("subscription_url"), remote_state=remote)
        await _log_event(job_id, service_id, action, str(_remote_id(remote) or ""), None, remote, plan_key=str(_row_get(service, "plan_key", "")))
    except Exception as exc:
        result = RemoteServiceResult(False, action, error=str(exc), remote_username=username)
        _sqlite_update_remote(sqlite_db, service_id, username=username, status="error", error=str(exc))
        await _log_event(job_id, service_id, action, None, None, None, str(exc), plan_key=str(_row_get(service, "plan_key", "")))
    await _finish_job(job_id, result)
    return result


async def set_remote_user_status(sqlite_db: Any, service: Mapping[str, Any], status: str) -> RemoteServiceResult:
    action = f"set_user_status_{status}"
    service_id = int(_row_get(service, "id"))
    username = str(_row_get(service, "pasarguard_username") or "")
    if not username:
        return RemoteServiceResult(True, action, skipped=True, message="remote username ذخیره نشده؛ فقط دیتابیس داخلی آپدیت شد.")
    ready, reason = integration_ready()
    if not ready:
        _sqlite_update_remote(sqlite_db, service_id, username=username, status="skipped", error=reason)
        return RemoteServiceResult(True, action, skipped=True, message=reason, remote_username=username)
    remote_status = "active" if status == "active" else "disabled"
    job_id = await _create_job(action, service_id)
    try:
        async with PasarguardClient() as client:
            remote = await client.update_user_by_username(username, {"status": remote_status})
        _sqlite_update_remote(sqlite_db, service_id, remote=remote, username=username, status="synced")
        result = RemoteServiceResult(True, action, applied=True, message=f"وضعیت remote user روی {remote_status} تنظیم شد.", remote_username=username, remote_user_id=_remote_id(remote), subscription_url=remote.get("subscription_url"), remote_state=remote)
        await _log_event(job_id, service_id, action, str(_remote_id(remote) or ""), None, remote, plan_key=str(_row_get(service, "plan_key", "")))
    except Exception as exc:
        result = RemoteServiceResult(False, action, error=str(exc), remote_username=username)
        _sqlite_update_remote(sqlite_db, service_id, username=username, status="error", error=str(exc))
        await _log_event(job_id, service_id, action, None, None, None, str(exc), plan_key=str(_row_get(service, "plan_key", "")))
    await _finish_job(job_id, result)
    return result


async def reset_remote_user_usage(sqlite_db: Any, service: Mapping[str, Any]) -> RemoteServiceResult:
    action = "reset_user_usage"
    service_id = int(_row_get(service, "id"))
    username = str(_row_get(service, "pasarguard_username") or "")
    if not username:
        return RemoteServiceResult(True, action, skipped=True, message="remote username ذخیره نشده؛ فقط دیتابیس داخلی ریست شد.")
    ready, reason = integration_ready()
    if not ready:
        _sqlite_update_remote(sqlite_db, service_id, username=username, status="skipped", error=reason)
        return RemoteServiceResult(True, action, skipped=True, message=reason, remote_username=username)
    job_id = await _create_job(action, service_id)
    try:
        async with PasarguardClient() as client:
            remote = await client.reset_user_usage(username)
        _sqlite_update_remote(sqlite_db, service_id, remote=remote if isinstance(remote, dict) else {}, username=username, status="synced")
        result = RemoteServiceResult(True, action, applied=True, message="مصرف remote user ریست شد.", remote_username=username, remote_user_id=_remote_id(remote if isinstance(remote, dict) else None), remote_state=remote if isinstance(remote, dict) else None)
        await _log_event(job_id, service_id, action, None, None, remote if isinstance(remote, dict) else {"response": remote}, plan_key=str(_row_get(service, "plan_key", "")))
    except Exception as exc:
        result = RemoteServiceResult(False, action, error=str(exc), remote_username=username)
        _sqlite_update_remote(sqlite_db, service_id, username=username, status="error", error=str(exc))
        await _log_event(job_id, service_id, action, None, None, None, str(exc), plan_key=str(_row_get(service, "plan_key", "")))
    await _finish_job(job_id, result)
    return result


async def revoke_remote_subscription(sqlite_db: Any, service: Mapping[str, Any]) -> RemoteServiceResult:
    action = "revoke_user_subscription"
    service_id = int(_row_get(service, "id"))
    username = str(_row_get(service, "pasarguard_username") or "")
    if not username:
        return RemoteServiceResult(True, action, skipped=True, message="remote username ذخیره نشده؛ فقط لینک داخلی تغییر کرد.")
    ready, reason = integration_ready()
    if not ready:
        _sqlite_update_remote(sqlite_db, service_id, username=username, status="skipped", error=reason)
        return RemoteServiceResult(True, action, skipped=True, message=reason, remote_username=username)
    job_id = await _create_job(action, service_id)
    try:
        async with PasarguardClient() as client:
            remote = await client.revoke_user_subscription(username)
            # Some API versions return a compact response; fetch user again to get subscription_url.
            try:
                user = await client.get_user_by_username(username)
            except Exception:
                user = remote if isinstance(remote, dict) else {}
        if not isinstance(user, dict):
            user = {}
        _sqlite_update_remote(sqlite_db, service_id, remote=user, username=username, status="synced")
        result = RemoteServiceResult(True, action, applied=True, message="لینک remote user تغییر کرد.", remote_username=username, remote_user_id=_remote_id(user), subscription_url=user.get("subscription_url"), remote_state=user)
        await _log_event(job_id, service_id, action, str(_remote_id(user) or ""), None, user, plan_key=str(_row_get(service, "plan_key", "")))
    except Exception as exc:
        result = RemoteServiceResult(False, action, error=str(exc), remote_username=username)
        _sqlite_update_remote(sqlite_db, service_id, username=username, status="error", error=str(exc))
        await _log_event(job_id, service_id, action, None, None, None, str(exc), plan_key=str(_row_get(service, "plan_key", "")))
    await _finish_job(job_id, result)
    return result

async def sync_remote_user_from_local(sqlite_db: Any, service: Mapping[str, Any]) -> RemoteServiceResult:
    """Push current local service limit/expire/status to Pasarguard user."""
    action = "sync_user_from_local"
    service_id = int(_row_get(service, "id"))
    username = str(_row_get(service, "pasarguard_username") or "")
    if not username:
        return RemoteServiceResult(True, action, skipped=True, message="remote username ذخیره نشده؛ فقط دیتابیس داخلی آپدیت شد.")
    ready, reason = integration_ready()
    if not ready:
        _sqlite_update_remote(sqlite_db, service_id, username=username, status="skipped", error=reason)
        return RemoteServiceResult(True, action, skipped=True, message=reason, remote_username=username)
    remote_status = "active" if str(_row_get(service, "status", "active")) == "active" else "disabled"
    payload = {
        "data_limit": int(float(_row_get(service, "data_gb", 0)) * BYTES_PER_GB),
        "expire": str(_row_get(service, "expires_at", "")),
        "status": remote_status,
    }
    job_id = await _create_job(action, service_id)
    try:
        async with PasarguardClient() as client:
            remote = await client.update_user_by_username(username, payload)
        _sqlite_update_remote(sqlite_db, service_id, remote=remote, username=username, status="synced")
        result = RemoteServiceResult(True, action, applied=True, message="حجم/زمان/وضعیت remote user با سرویس داخلی sync شد.", remote_username=username, remote_user_id=_remote_id(remote), subscription_url=remote.get("subscription_url"), remote_state=remote)
        await _log_event(job_id, service_id, action, str(_remote_id(remote) or ""), None, remote, plan_key=str(_row_get(service, "plan_key", "")))
    except Exception as exc:
        result = RemoteServiceResult(False, action, error=str(exc), remote_username=username)
        _sqlite_update_remote(sqlite_db, service_id, username=username, status="error", error=str(exc))
        await _log_event(job_id, service_id, action, None, None, None, str(exc), plan_key=str(_row_get(service, "plan_key", "")))
    await _finish_job(job_id, result)
    return result
