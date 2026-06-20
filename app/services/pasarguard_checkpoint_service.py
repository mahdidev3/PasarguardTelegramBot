
"""Pasarguard checkpoint backup/restore/reconcile service for Phase 4.8.

This service upgrades Phase 3 backups from "bot DB checkpoint" to
"bot + Pasarguard checkpoint":
- backup includes actual remote templates/users read from Pasarguard
- backup includes desired remote templates/users derived from bot plans/services
- restore/reconcile is always dry-run first and never deletes remote users
"""

from __future__ import annotations

import json
import sqlite3
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import settings
from app.database import session_scope
from app.models import CatalogPlan, PasarguardRemoteSnapshot
from app.services.pasarguard_client import PasarguardClient, connection_info
from app.services.pasarguard_template_service import managed_marker, template_name_for_plan, desired_payload_for_plan
from app.services.pasarguard_user_service import build_managed_note, sanitize_remote_username

BYTES_PER_GB = 1024 * 1024 * 1024
BYTES_PER_MB = 1024 * 1024


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=_json_default) + "\n")


def _read_jsonl_from_zip(zf: zipfile.ZipFile, name: str) -> list[dict[str, Any]]:
    if name not in zf.namelist():
        return []
    rows: list[dict[str, Any]] = []
    for line in zf.read(name).decode("utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _row_get(row: dict[str, Any], key: str, default: Any = None) -> Any:
    return row.get(key, default)


def _safe_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except Exception:
        return None


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _remote_id(row: dict[str, Any] | None) -> int | None:
    if not row:
        return None
    return _safe_int(row.get("id"))


def _extract_subscription_url(remote: dict[str, Any]) -> str | None:
    for key in ("subscription_url", "sub_url", "subscription_link", "link"):
        if remote.get(key):
            return str(remote[key])
    return None


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except Exception:
            return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return None
    return None


def _status_to_remote(local_status: str | None) -> str:
    if str(local_status or "").lower() in {"active", "enabled"}:
        return "active"
    return "disabled"


def _remote_status(remote: dict[str, Any]) -> str | None:
    status = str(remote.get("status") or "").lower()
    is_disabled = remote.get("is_disabled")
    if status in {"active", "enabled", "enable"} or is_disabled is False:
        return "active"
    if status in {"disabled", "disable", "suspended", "limited", "expired"} or is_disabled is True:
        return "disabled"
    return None


def _make_plan_like(row: dict[str, Any]):
    """Create a tiny object that has the attributes template helpers need."""
    class P:
        pass
    p = P()
    p.key = str(row.get("key") or "")
    p.title = str(row.get("title") or p.key)
    p.data_gb = float(row.get("data_gb") or 0)
    p.days = int(row.get("days") or 0)
    p.is_active = bool(row.get("is_active", True))
    return p


def desired_templates_from_pg_data(pg_data: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    rows = pg_data.get("plans", [])
    output: list[dict[str, Any]] = []
    for row in rows:
        if not row.get("key"):
            continue
        plan = _make_plan_like(row)
        payload = desired_payload_for_plan(plan)  # type: ignore[arg-type]
        output.append({
            "plan_key": plan.key,
            "template_name": payload["name"],
            "remote_template_id": row.get("pasarguard_template_id"),
            "data_limit": payload["data_limit"],
            "expire_duration": payload["expire_duration"],
            "username_prefix": payload["username_prefix"],
            "username_suffix": payload["username_suffix"],
            "group_ids": payload["group_ids"],
            "data_limit_reset_strategy": payload.get("data_limit_reset_strategy"),
            "reset_usages": payload.get("reset_usages"),
            "is_disabled": payload.get("is_disabled"),
            "managed_marker": managed_marker(),
        })
    return output


def desired_users_from_data(sqlite_data: dict[str, list[dict[str, Any]]], pg_data: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    # Current runtime still stores purchase services in legacy SQLite. If a later
    # checkpoint moves them fully to PostgreSQL, this fallback already supports it.
    services = sqlite_data.get("services") or pg_data.get("services") or []
    plans_by_key = {str(p.get("key")): p for p in pg_data.get("plans", [])}
    output: list[dict[str, Any]] = []
    for svc in services:
        plan_key = str(svc.get("plan_key") or "")
        data_gb = _safe_float(svc.get("data_gb")) or 0.0
        data_limit_bytes = int(data_gb * BYTES_PER_GB)
        used_mb = _safe_int(svc.get("data_used_mb")) or 0
        plan_row = plans_by_key.get(plan_key, {})
        template_name = svc.get("pasarguard_template_name") or plan_row.get("pasarguard_template_name")
        if not template_name and plan_key and plan_row:
            template_name = desired_payload_for_plan(_make_plan_like(plan_row))["name"]  # type: ignore[arg-type]
        username = str(svc.get("pasarguard_username") or "") or sanitize_remote_username(str(svc.get("name") or ""), int(svc.get("user_telegram_id") or 0), int(svc.get("id") or 0))
        output.append({
            "bot_service_id": svc.get("id"),
            "user_telegram_id": svc.get("user_telegram_id"),
            "service_name": svc.get("name"),
            "plan_key": plan_key,
            "template_name": template_name,
            "remote_template_id": svc.get("pasarguard_template_id") or plan_row.get("pasarguard_template_id"),
            "remote_user_id": svc.get("pasarguard_user_id"),
            "remote_username": username,
            "subscription_url": svc.get("pasarguard_subscription_url"),
            "data_total_bytes": data_limit_bytes,
            "data_total_mb": int(data_limit_bytes / BYTES_PER_MB),
            "data_used_mb": used_mb,
            "expires_at": svc.get("expires_at"),
            "status": svc.get("status"),
            "desired_remote_status": _status_to_remote(str(svc.get("status") or "active")),
            "note": build_managed_note(
                telegram_id=int(svc.get("user_telegram_id") or 0),
                service_id=int(svc.get("id") or 0),
                order_id=None,
                plan_key=plan_key,
                action="restore_checkpoint",
            ),
            "managed_marker": managed_marker(),
        })
    return output


async def fetch_actual_pasarguard_state(admin_id: int | None = None) -> dict[str, Any]:
    info = connection_info()
    result = {
        "enabled": info.enabled,
        "base_url": info.base_url,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "templates": [],
        "users": [],
        "error": None,
    }
    if not info.enabled:
        result["error"] = "PASARGUARD_ENABLED=false"
        return result
    try:
        async with PasarguardClient() as client:
            result["templates"] = await client.list_user_templates(limit=5000)
            result["users"] = await client.list_users(limit=10000)
        async with session_scope() as session:
            session.add(PasarguardRemoteSnapshot(snapshot_type="phase4_actual_templates", state_json=result["templates"], created_by=admin_id))
            session.add(PasarguardRemoteSnapshot(snapshot_type="phase4_actual_users", state_json=result["users"], created_by=admin_id))
    except Exception as exc:
        result["error"] = str(exc)
    return result


async def write_pasarguard_checkpoint_files(base: Path, sqlite_data: dict[str, list[dict[str, Any]]], pg_data: dict[str, list[dict[str, Any]]], admin_id: int | None = None) -> dict[str, Any]:
    desired_templates = desired_templates_from_pg_data(pg_data)
    desired_users = desired_users_from_data(sqlite_data, pg_data)
    actual = await fetch_actual_pasarguard_state(admin_id)

    write_jsonl(base / "external" / "pasarguard_desired_templates.jsonl", desired_templates)
    write_jsonl(base / "external" / "pasarguard_desired_state.jsonl", desired_users)
    write_jsonl(base / "external" / "pasarguard_actual_templates.jsonl", list(actual.get("templates") or []))
    write_jsonl(base / "external" / "pasarguard_actual_users.jsonl", list(actual.get("users") or []))

    summary = {
        "included": bool(actual.get("enabled")),
        "actual_state_included": bool(actual.get("enabled") and not actual.get("error")),
        "desired_state_included": True,
        "desired_templates": len(desired_templates),
        "desired_users": len(desired_users),
        "actual_templates": len(actual.get("templates") or []),
        "actual_users": len(actual.get("users") or []),
        "error": actual.get("error"),
        "files": {
            "desired_templates": "external/pasarguard_desired_templates.jsonl",
            "desired_users": "external/pasarguard_desired_state.jsonl",
            "actual_templates": "external/pasarguard_actual_templates.jsonl",
            "actual_users": "external/pasarguard_actual_users.jsonl",
        },
        "note": "Phase 4.8 checkpoint. Restore uses dry-run first and never remote-deletes users.",
    }
    (base / "external" / "pasarguard_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


@dataclass
class ReconcileAction:
    kind: str
    action: str
    key: str
    details: str = ""
    ok: bool = True
    applied: bool = False
    error: str | None = None


@dataclass
class PasarguardReconcileReport:
    enabled: bool
    dry_run: bool
    source: str
    templates_desired: int = 0
    users_desired: int = 0
    templates_actual: int = 0
    users_actual: int = 0
    actions: list[ReconcileAction] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def action_count(self) -> int:
        return len(self.actions)

    @property
    def failed_count(self) -> int:
        return len([a for a in self.actions if not a.ok]) + len(self.errors)

    @property
    def applied_count(self) -> int:
        return len([a for a in self.actions if a.applied])


def _diff_template(desired: dict[str, Any], actual: dict[str, Any] | None) -> list[str]:
    if not actual:
        return ["template در پنل وجود ندارد"]
    diffs: list[str] = []
    checks = [
        ("data_limit", "حجم"),
        ("expire_duration", "مدت"),
        ("username_prefix", "prefix"),
        ("username_suffix", "suffix"),
        ("is_disabled", "وضعیت"),
    ]
    for key, label in checks:
        if actual.get(key) != desired.get(key):
            diffs.append(f"{label}: پنل {actual.get(key)} → بک‌آپ {desired.get(key)}")
    if sorted([int(x) for x in (actual.get("group_ids") or [])]) != sorted([int(x) for x in (desired.get("group_ids") or [])]):
        diffs.append("گروه‌ها متفاوت است")
    return diffs


def _diff_user(desired: dict[str, Any], actual: dict[str, Any] | None) -> list[str]:
    if not actual:
        return ["user در پنل وجود ندارد"]
    diffs: list[str] = []
    if actual.get("data_limit") != desired.get("data_total_bytes"):
        diffs.append(f"حجم: پنل {actual.get('data_limit')} → بک‌آپ {desired.get('data_total_bytes')}")
    desired_status = desired.get("desired_remote_status") or _status_to_remote(desired.get("status"))
    current_status = _remote_status(actual)
    if current_status and desired_status and current_status != desired_status:
        diffs.append(f"وضعیت: پنل {current_status} → بک‌آپ {desired_status}")
    desired_expire = _parse_dt(desired.get("expires_at"))
    actual_expire = _parse_dt(actual.get("expire"))
    if desired_expire and actual_expire and abs((desired_expire - actual_expire).total_seconds()) > 60:
        diffs.append("تاریخ انقضا متفاوت است")
    desired_used = desired.get("data_used_mb")
    actual_used = actual.get("used_traffic")
    if desired_used not in (None, "") and actual_used not in (None, ""):
        try:
            actual_mb = int(float(actual_used) / BYTES_PER_MB)
            if int(desired_used) != actual_mb:
                if int(desired_used) == 0:
                    diffs.append("مصرف باید ریست شود")
                else:
                    diffs.append("مصرف متفاوت است؛ API فعلی ست‌کردن دقیق مصرف را تضمین نمی‌کند")
        except Exception:
            pass
    return diffs


def _payload_for_template(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": row.get("template_name"),
        "data_limit": row.get("data_limit"),
        "expire_duration": row.get("expire_duration"),
        "username_prefix": row.get("username_prefix"),
        "username_suffix": row.get("username_suffix"),
        "group_ids": row.get("group_ids") or [],
        "data_limit_reset_strategy": row.get("data_limit_reset_strategy") or "no_reset",
        "reset_usages": True,
        "is_disabled": bool(row.get("is_disabled")),
    }


def _payload_for_user_update(row: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "data_limit": row.get("data_total_bytes"),
        "status": row.get("desired_remote_status") or _status_to_remote(row.get("status")),
    }
    expire = row.get("expires_at")
    if expire:
        payload["expire"] = expire
    note = row.get("note")
    if note:
        payload["note"] = note
    return {k: v for k, v in payload.items() if v not in (None, "")}


async def reconcile_backup_with_pasarguard(zip_path: str | Path, *, admin_id: int | None = None, dry_run: bool = True) -> PasarguardReconcileReport:
    info = connection_info()
    report = PasarguardReconcileReport(enabled=info.enabled, dry_run=dry_run, source=str(zip_path))
    if not info.enabled:
        report.errors.append("PASARGUARD_ENABLED=false است.")
        return report

    with zipfile.ZipFile(zip_path, "r") as zf:
        desired_templates = _read_jsonl_from_zip(zf, "external/pasarguard_desired_templates.jsonl")
        desired_users = _read_jsonl_from_zip(zf, "external/pasarguard_desired_state.jsonl")

    report.templates_desired = len(desired_templates)
    report.users_desired = len(desired_users)
    try:
        async with PasarguardClient() as client:
            actual_templates = await client.list_user_templates(limit=5000)
            actual_users = await client.list_users(limit=10000)
            report.templates_actual = len(actual_templates)
            report.users_actual = len(actual_users)
            templates_by_name = {str(t.get("name") or ""): t for t in actual_templates}
            templates_by_id = {_remote_id(t): t for t in actual_templates if _remote_id(t) is not None}
            users_by_username = {str(u.get("username") or ""): u for u in actual_users}
            users_by_id = {_remote_id(u): u for u in actual_users if _remote_id(u) is not None}

            # Templates first, because user restore depends on them.
            for desired in desired_templates:
                name = str(desired.get("template_name") or "")
                actual = None
                rid = _safe_int(desired.get("remote_template_id"))
                if rid is not None:
                    actual = templates_by_id.get(rid)
                if actual is None:
                    actual = templates_by_name.get(name)
                diffs = _diff_template(desired, actual)
                if not diffs:
                    continue
                action = "create_template" if actual is None else "update_template"
                item = ReconcileAction("template", action, name, "؛ ".join(diffs))
                report.actions.append(item)
                if not dry_run:
                    try:
                        payload = _payload_for_template(desired)
                        if actual is None:
                            created = await client.create_user_template(payload)
                            item.applied = True
                            templates_by_name[str(created.get("name") or name)] = created
                            if _remote_id(created) is not None:
                                templates_by_id[_remote_id(created)] = created
                        else:
                            await client.update_user_template(int(actual.get("id")), payload)
                            item.applied = True
                    except Exception as exc:
                        item.ok = False
                        item.error = str(exc)

            # Refresh template maps after possible creation.
            if not dry_run and any(a.applied for a in report.actions if a.kind == "template"):
                actual_templates = await client.list_user_templates(limit=5000)
                templates_by_name = {str(t.get("name") or ""): t for t in actual_templates}
                templates_by_id = {_remote_id(t): t for t in actual_templates if _remote_id(t) is not None}

            for desired in desired_users:
                username = str(desired.get("remote_username") or "")
                if not username:
                    continue
                rid = _safe_int(desired.get("remote_user_id"))
                actual_user = users_by_id.get(rid) if rid is not None else None
                if actual_user is None:
                    actual_user = users_by_username.get(username)
                diffs = _diff_user(desired, actual_user)
                if not diffs:
                    continue
                desired_status = desired.get("desired_remote_status") or _status_to_remote(desired.get("status"))
                missing = actual_user is None
                if missing and desired_status != "active":
                    # Suspended/deleted users missing in remote are already safe.
                    report.actions.append(ReconcileAction("user", "missing_disabled_user_noop", username, "user در پنل نیست و وضعیت مطلوب فعال نیست؛ ساخته نمی‌شود."))
                    continue
                action = "create_user_from_template" if missing else "update_user"
                item = ReconcileAction("user", action, username, "؛ ".join(diffs))
                report.actions.append(item)
                if not dry_run:
                    try:
                        if missing:
                            template = None
                            tid = _safe_int(desired.get("remote_template_id"))
                            if tid is not None:
                                template = templates_by_id.get(tid)
                            if template is None and desired.get("template_name"):
                                template = templates_by_name.get(str(desired.get("template_name")))
                            if template is None or _remote_id(template) is None:
                                raise RuntimeError("template لازم برای ساخت user در پنل پیدا نشد.")
                            created = await client.create_user_from_template({
                                "user_template_id": _remote_id(template),
                                "username": username,
                                "note": desired.get("note") or "[HOWTOOSEE_BOT_RESTORE]",
                            })
                            item.applied = True
                            users_by_username[str(created.get("username") or username)] = created
                            if _remote_id(created) is not None:
                                users_by_id[_remote_id(created)] = created
                            actual_user = created
                        else:
                            await client.update_user_by_username(username, _payload_for_user_update(desired))
                            item.applied = True
                        # Usage exact-restore: only reset to zero is supported safely.
                        try:
                            if int(desired.get("data_used_mb") or 0) == 0:
                                await client.reset_user_usage(username)
                        except Exception:
                            # Non-fatal; report but do not mark entire user restore failed.
                            item.details += "؛ هشدار: ریست مصرف انجام نشد"
                    except Exception as exc:
                        item.ok = False
                        item.error = str(exc)
    except Exception as exc:
        report.errors.append(str(exc))
    return report


def render_reconcile_report(report: PasarguardReconcileReport) -> str:
    lines = [
        f"وضعیت اتصال: {'فعال' if report.enabled else 'غیرفعال'}",
        f"حالت: {'Dry-run / فقط گزارش' if report.dry_run else 'اعمال واقعی'}",
        f"template مطلوب در بک‌آپ: {report.templates_desired}",
        f"user/service مطلوب در بک‌آپ: {report.users_desired}",
        f"template فعلی پنل: {report.templates_actual}",
        f"user فعلی پنل: {report.users_actual}",
        f"تعداد عملیات/اختلاف: {report.action_count}",
        f"اعمال‌شده: {report.applied_count}",
        f"خطا: {report.failed_count}",
    ]
    if report.actions:
        lines.append("\nعملیات‌ها:")
        for action in report.actions[:40]:
            if report.dry_run:
                icon = "🧪"
                verb = "قرار است"
            else:
                icon = "✅" if action.ok else "❌"
                verb = "انجام شد" if action.applied else "انجام نشد"
            line = f"{icon} {action.kind} | {action.action} | {action.key} | {verb}"
            if action.details:
                line += f" | {action.details}"
            if action.error:
                line += f" | {action.error}"
            lines.append(line)
        if len(report.actions) > 40:
            lines.append(f"… و {len(report.actions) - 40} عملیات دیگر")
    if report.errors:
        lines.append("\nخطاهای کلی:")
        for err in report.errors[:20]:
            lines.append(f"❌ {err}")
    if not report.actions and not report.errors:
        lines.append("\n✅ اختلافی بین backup desired_state و پنل فعلی پیدا نشد.")
    return "\n".join(lines)






