"""Template governance and plan-template sync for Pasarguard Phase 4."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update

from app.config import settings
from app.database import session_scope
from app.models import CatalogPlan, PasarguardRemoteSnapshot, PasarguardSyncEvent, PasarguardSyncJob, PasarguardTemplate
from app.services.pasarguard_client import PasarguardAPIError, PasarguardClient, PasarguardConfigurationError, connection_info


BYTES_PER_GB = 1024 * 1024 * 1024


@dataclass
class TemplateSyncAction:
    plan_key: str
    action: str
    template_name: str
    remote_template_id: int | None = None
    details: dict[str, Any] = field(default_factory=dict)
    applied: bool = False
    ok: bool = True
    error: str | None = None


@dataclass
class TemplateSyncReport:
    enabled: bool
    dry_run: bool
    total_plans: int = 0
    actions: list[TemplateSyncAction] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    remote_count: int = 0
    managed_remote_count: int = 0
    job_id: int | None = None

    @property
    def action_count(self) -> int:
        return len(self.actions)

    @property
    def failed_count(self) -> int:
        return len([a for a in self.actions if not a.ok]) + len(self.errors)

    @property
    def success_count(self) -> int:
        return len([a for a in self.actions if a.ok])


def managed_marker() -> str:
    return (settings.pasarguard_managed_prefix or "HTS_BOT").strip()


def normalize_key(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_\-]+", "_", value or "").strip("_")
    return safe[:50] or "plan"


def template_name_for_plan(plan: CatalogPlan) -> str:
    key = normalize_key(plan.key)
    data = f"{float(plan.data_gb):g}GB".replace(".", "_")
    return f"{managed_marker()}__PLAN__{key}__{data}__{int(plan.days)}D"[:255]


def username_suffix_for_plan(plan: CatalogPlan) -> str:
    base = f"_{normalize_key(plan.key)}"
    custom = settings.pasarguard_username_suffix or ""
    suffix = f"{base}{custom}"
    return suffix[:20]


def desired_payload_for_plan(plan: CatalogPlan) -> dict[str, Any]:
    # UserTemplateCreate requires group_ids. If env is empty, we still build a
    # payload for dry-run but refuse real creation/apply with a clear error.
    return {
        "name": template_name_for_plan(plan),
        "data_limit": int(float(plan.data_gb) * BYTES_PER_GB),
        "expire_duration": int(plan.days) * 86400,
        "username_prefix": (settings.pasarguard_username_prefix or "hts_")[:20],
        "username_suffix": username_suffix_for_plan(plan),
        "group_ids": list(settings.pasarguard_template_group_ids),
        "data_limit_reset_strategy": "no_reset",
        "reset_usages": True,
        "is_disabled": not bool(plan.is_active),
    }


def is_managed_template(remote: dict[str, Any]) -> bool:
    name = str(remote.get("name") or "")
    return name.startswith(f"{managed_marker()}__PLAN__")


def _remote_id(remote: dict[str, Any] | None) -> int | None:
    if not remote:
        return None
    raw = remote.get("id")
    try:
        return int(raw) if raw is not None else None
    except Exception:
        return None


def diff_template(remote: dict[str, Any], desired: dict[str, Any]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    for key in ["name", "data_limit", "expire_duration", "username_prefix", "username_suffix", "group_ids", "data_limit_reset_strategy", "is_disabled"]:
        rv = remote.get(key)
        dv = desired.get(key)
        if key == "group_ids":
            rv = sorted([int(x) for x in (rv or [])])
            dv = sorted([int(x) for x in (dv or [])])
        if rv != dv:
            changes[key] = {"remote": rv, "desired": dv}
    return changes


async def create_sync_job(admin_id: int | None, mode: str, dry_run: bool) -> int:
    async with session_scope() as session:
        job = PasarguardSyncJob(admin_telegram_id=admin_id, mode=mode, dry_run=dry_run, status="running")
        session.add(job)
        await session.flush()
        return int(job.id)


async def finish_sync_job(job_id: int | None, report: TemplateSyncReport) -> None:
    if not job_id:
        return
    async with session_scope() as session:
        await session.execute(
            update(PasarguardSyncJob)
            .where(PasarguardSyncJob.id == job_id)
            .values(
                status="failed" if report.failed_count else "done",
                total_items=report.total_plans,
                success_count=report.success_count,
                failed_count=report.failed_count,
                report_json={
                    "actions": [a.__dict__ for a in report.actions],
                    "errors": report.errors,
                    "remote_count": report.remote_count,
                    "managed_remote_count": report.managed_remote_count,
                },
                finished_at=datetime.now(timezone.utc),
            )
        )


async def log_sync_event(job_id: int | None, action: TemplateSyncAction, old_state: dict[str, Any] | None = None, new_state: dict[str, Any] | None = None) -> None:
    async with session_scope() as session:
        session.add(
            PasarguardSyncEvent(
                job_id=job_id,
                plan_key=action.plan_key,
                remote_id=str(action.remote_template_id or ""),
                action=action.action,
                old_state_json=old_state,
                new_state_json=new_state or action.details,
                error=action.error,
            )
        )


async def upsert_local_template_mapping(plan: CatalogPlan, remote: dict[str, Any] | None, status: str, error: str | None = None) -> None:
    rid = _remote_id(remote)
    payload = desired_payload_for_plan(plan)
    async with session_scope() as session:
        item = None
        if rid is not None:
            item = (await session.execute(select(PasarguardTemplate).where(PasarguardTemplate.remote_template_id == rid))).scalar_one_or_none()
        if item is None:
            item = (await session.execute(select(PasarguardTemplate).where(PasarguardTemplate.plan_key == plan.key))).scalar_one_or_none()
        if item is None:
            item = PasarguardTemplate(plan_key=plan.key, remote_name=payload["name"], managed_marker=managed_marker())
            session.add(item)
        item.plan_key = plan.key
        item.remote_template_id = rid
        item.remote_name = str((remote or {}).get("name") or payload["name"])
        item.managed_marker = managed_marker()
        item.data_limit_bytes = payload["data_limit"]
        item.expire_duration_seconds = payload["expire_duration"]
        item.username_prefix = payload["username_prefix"]
        item.username_suffix = payload["username_suffix"]
        item.group_ids_json = payload["group_ids"]
        item.is_disabled = bool((remote or payload).get("is_disabled") or False)
        item.sync_status = status
        item.last_sync_at = datetime.now(timezone.utc)
        item.last_remote_state_json = remote
        item.last_error = error
        await session.execute(
            update(CatalogPlan)
            .where(CatalogPlan.key == plan.key)
            .values(
                pasarguard_template_id=rid,
                pasarguard_template_name=item.remote_name,
                pasarguard_sync_status=status,
                pasarguard_sync_error=error,
                pasarguard_last_sync_at=datetime.now(timezone.utc),
                pasarguard_last_state_json=remote,
            )
        )


async def health_check() -> tuple[bool, str, dict[str, Any] | None]:
    info = connection_info()
    if not info.enabled:
        return False, "اتصال Pasarguard در env غیرفعال است.", {"connection": info.__dict__}
    try:
        async with PasarguardClient() as client:
            health = await client.health()
            await client.login()
            admin = await client.current_admin()
            return True, "اتصال و احراز هویت موفق بود.", {"health": health, "admin": admin, "connection": info.__dict__}
    except (PasarguardConfigurationError, PasarguardAPIError, Exception) as exc:
        return False, str(exc), {"connection": info.__dict__}


async def fetch_remote_templates_snapshot(admin_id: int | None = None) -> list[dict[str, Any]]:
    async with PasarguardClient() as client:
        templates = await client.list_user_templates()
    async with session_scope() as session:
        session.add(PasarguardRemoteSnapshot(snapshot_type="user_templates", state_json=templates, created_by=admin_id))
    return templates


async def sync_plan_templates(admin_id: int | None = None, *, dry_run: bool | None = None) -> TemplateSyncReport:
    if dry_run is None:
        dry_run = settings.pasarguard_dry_run
    info = connection_info()
    report = TemplateSyncReport(enabled=info.enabled, dry_run=bool(dry_run))
    if not info.enabled:
        report.errors.append("PASARGUARD_ENABLED=false است.")
        return report

    job_id = await create_sync_job(admin_id, "template_sync", bool(dry_run))
    report.job_id = job_id
    try:
        async with session_scope() as session:
            plans = list((await session.execute(select(CatalogPlan).order_by(CatalogPlan.category, CatalogPlan.sort_order, CatalogPlan.id))).scalars().all())
        report.total_plans = len(plans)

        async with PasarguardClient() as client:
            remote_templates = await client.list_user_templates()
            report.remote_count = len(remote_templates)
            managed_remotes = [t for t in remote_templates if is_managed_template(t)]
            report.managed_remote_count = len(managed_remotes)
            by_name = {str(t.get("name") or ""): t for t in remote_templates}
            by_id = {_remote_id(t): t for t in remote_templates if _remote_id(t) is not None}

            async with session_scope() as session:
                session.add(PasarguardRemoteSnapshot(snapshot_type="user_templates", state_json=remote_templates, created_by=admin_id))

            for plan in plans:
                desired = desired_payload_for_plan(plan)
                remote = by_id.get(plan.pasarguard_template_id) if getattr(plan, "pasarguard_template_id", None) else None
                if remote is None:
                    remote = by_name.get(desired["name"])
                rid = _remote_id(remote)

                if remote is None:
                    action = TemplateSyncAction(plan.key, "create", desired["name"], details={"desired": desired})
                    if not desired.get("group_ids"):
                        action.ok = False
                        action.error = "PASARGUARD_TEMPLATE_GROUP_IDS خالی است؛ ساخت template واقعی ممکن نیست."
                    elif not dry_run:
                        try:
                            remote = await client.create_user_template(desired)
                            action.remote_template_id = _remote_id(remote)
                            action.applied = True
                            await upsert_local_template_mapping(plan, remote, "synced")
                        except Exception as exc:
                            action.ok = False
                            action.error = str(exc)
                            await upsert_local_template_mapping(plan, None, "error", str(exc))
                    else:
                        await upsert_local_template_mapping(plan, None, "dry_run_create")
                    report.actions.append(action)
                    await log_sync_event(job_id, action, None, remote or desired)
                    continue

                changes = diff_template(remote, desired)
                if changes:
                    action = TemplateSyncAction(plan.key, "update", desired["name"], remote_template_id=rid, details={"changes": changes, "desired": desired})
                    if not dry_run and rid is not None:
                        try:
                            remote = await client.update_user_template(rid, desired)
                            action.applied = True
                            await upsert_local_template_mapping(plan, remote, "synced")
                        except Exception as exc:
                            action.ok = False
                            action.error = str(exc)
                            await upsert_local_template_mapping(plan, remote, "error", str(exc))
                    else:
                        await upsert_local_template_mapping(plan, remote, "dry_run_update")
                    report.actions.append(action)
                    await log_sync_event(job_id, action, remote, desired)
                else:
                    await upsert_local_template_mapping(plan, remote, "synced")

            # Orphan managed templates in the panel that no local plan wants.
            desired_names = {template_name_for_plan(p) for p in plans}
            for remote in managed_remotes:
                name = str(remote.get("name") or "")
                if name and name not in desired_names:
                    action = TemplateSyncAction("", "orphan_remote", name, remote_template_id=_remote_id(remote), details={"remote": remote})
                    report.actions.append(action)
                    await log_sync_event(job_id, action, remote, None)
    except Exception as exc:
        report.errors.append(str(exc))
    finally:
        await finish_sync_job(job_id, report)
    return report


def _action_label(action: TemplateSyncAction, *, dry_run: bool) -> str:
    labels = {
        "create": "ساخت template",
        "update": "ویرایش template",
        "orphan_remote": "template اضافه در پنل",
    }
    base = labels.get(action.action, action.action)
    if dry_run and action.action in {"create", "update"}:
        return f"قرار است {base} انجام شود"
    if dry_run and action.action == "orphan_remote":
        return "فقط گزارش: template اضافه در پنل"
    return base


def render_sync_report(report: TemplateSyncReport) -> str:
    lines = []
    lines.append(f"وضعیت اتصال: {'فعال' if report.enabled else 'غیرفعال'}")
    lines.append(f"حالت: {'Dry-run / فقط گزارش؛ هیچ تغییری در Pasarguard انجام نشده' if report.dry_run else 'اعمال واقعی'}")
    lines.append(f"تعداد پلن‌ها: {report.total_plans}")
    lines.append(f"templateهای پنل: {report.remote_count}")
    lines.append(f"templateهای تشخیص‌داده‌شده بات: {report.managed_remote_count}")
    lines.append(f"تعداد عملیات/اختلاف: {report.action_count}")
    if report.errors:
        lines.append("\nخطاها:")
        for err in report.errors[:10]:
            lines.append(f"• {err}")
    if report.actions:
        lines.append("\nعملیات‌ها:")
        for action in report.actions[:25]:
            if report.dry_run:
                status = "🧪" if action.ok else "❌"
                applied = " | فقط گزارش"
            else:
                status = "✅" if action.ok else "❌"
                applied = " | اعمال شد" if action.applied else " | اعمال نشد"
            detail = f" | {action.error}" if action.error else ""
            key = action.plan_key or "orphan"
            lines.append(f"{status} {_action_label(action, dry_run=report.dry_run)} | {key} | {action.template_name}{applied}{detail}")
        if len(report.actions) > 25:
            lines.append(f"… و {len(report.actions) - 25} مورد دیگر")
    if report.dry_run and report.actions:
        lines.append("\nیادآوری: این خروجی فقط برنامه تغییرات است؛ برای اعمال واقعی باید گزینه «اعمال Sync Templateها» را اجرا کنید.")
    return "\n".join(lines)
