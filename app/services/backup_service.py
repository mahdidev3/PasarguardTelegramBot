"""Complete backup service for Phase 3/3.1.

Creates a ZIP checkpoint that includes PostgreSQL tables, legacy SQLite tables,
usage summary, checksums, ticket media files downloaded from Telegram, and a
future Pasarguard desired-state snapshot.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiogram import Bot
from sqlalchemy import select, update

from app.config import settings
from app.database import session_scope
from app.models import Base, TicketAttachment
from app.services.export_service import usage_summary_rows
from app.services.pasarguard_checkpoint_service import write_pasarguard_checkpoint_files


BACKUP_VERSION = 3


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=_json_default) + "\n")


def _safe_filename(value: str | None, fallback: str) -> str:
    raw = value or fallback
    raw = re.sub(r"[^A-Za-z0-9._\-\u0600-\u06FF]+", "_", raw).strip("._")
    return raw[:180] or fallback


def _extension_for_attachment(item: TicketAttachment, fallback: str = ".bin") -> str:
    if item.file_name and "." in item.file_name:
        suffix = Path(item.file_name).suffix
        if suffix and len(suffix) <= 12:
            return suffix
    if item.mime_type:
        guessed = mimetypes.guess_extension(item.mime_type.split(";", 1)[0].strip())
        if guessed:
            return guessed
    return fallback


def _sqlite_tables() -> dict[str, list[dict[str, Any]]]:
    db_path = Path(settings.database_path)
    if not db_path.exists():
        return {}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()]
        result: dict[str, list[dict[str, Any]]] = {}
        for table in tables:
            try:
                result[table] = [dict(row) for row in conn.execute(f"SELECT * FROM {table}").fetchall()]
            except Exception:
                result[table] = []
        return result
    finally:
        conn.close()


def _sqlite_dump() -> str:
    db_path = Path(settings.database_path)
    if not db_path.exists():
        return ""
    conn = sqlite3.connect(db_path)
    try:
        return "\n".join(conn.iterdump())
    finally:
        conn.close()


async def _pg_tables() -> dict[str, list[dict[str, Any]]]:
    tables: dict[str, list[dict[str, Any]]] = {}
    async with session_scope() as session:
        for table in Base.metadata.sorted_tables:
            result = await session.execute(table.select())
            rows: list[dict[str, Any]] = []
            for row in result.mappings().all():
                rows.append({key: _json_default(value) if isinstance(value, datetime) else value for key, value in dict(row).items()})
            tables[table.name] = rows
    return tables


async def _download_ticket_files(base: Path, bot: Bot | None) -> list[dict[str, Any]]:
    """Download active ticket attachments into the backup archive workspace.

    Telegram media are not stored on our server by default; only file_id is in DB.
    For a complete backup we must ask Telegram for each active file and store the
    bytes in the ZIP. Closed-ticket attachments are intentionally skipped because
    Phase 3.1 deletes their bot-side access data when the ticket is closed.
    """
    manifest: list[dict[str, Any]] = []
    async with session_scope() as session:
        result = await session.execute(
            select(TicketAttachment).where(
                TicketAttachment.is_deleted.is_(False),
                TicketAttachment.telegram_file_id.is_not(None),
            ).order_by(TicketAttachment.ticket_id, TicketAttachment.id)
        )
        attachments = list(result.scalars().all())
        if bot is None:
            for item in attachments:
                manifest.append({
                    "attachment_id": item.id,
                    "ticket_id": item.ticket_id,
                    "message_id": item.message_id,
                    "backed_up": False,
                    "error": "bot_not_provided",
                    "file_name": item.file_name,
                    "mime_type": item.mime_type,
                    "file_size": item.file_size,
                })
            return manifest

        for item in attachments:
            row: dict[str, Any] = {
                "attachment_id": item.id,
                "ticket_id": item.ticket_id,
                "message_id": item.message_id,
                "file_name": item.file_name,
                "mime_type": item.mime_type,
                "file_size": item.file_size,
                "telegram_file_unique_id": item.telegram_file_unique_id,
                "backed_up": False,
                "path": None,
                "sha256": None,
                "error": None,
            }
            try:
                tg_file = await bot.get_file(item.telegram_file_id)  # type: ignore[arg-type]
                ext = _extension_for_attachment(item)
                filename = _safe_filename(item.file_name, f"attachment_{item.id}{ext}")
                if not Path(filename).suffix:
                    filename += ext
                rel = Path("files") / "tickets" / str(item.ticket_id) / f"{item.id}_{filename}"
                dest = base / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                await bot.download_file(tg_file.file_path, destination=dest)
                row["backed_up"] = True
                row["path"] = rel.as_posix()
                row["sha256"] = _sha256(dest)
                item.backed_up_at = datetime.now(timezone.utc)
                item.backup_path = rel.as_posix()
            except Exception as exc:  # keep the rest of backup healthy
                row["error"] = str(exc)
            manifest.append(row)
        return manifest


def _desired_pasarguard_state(sqlite_data: dict[str, list[dict[str, Any]]], pg_data: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Future adapter input: what Pasarguard should look like after restore.

    No API call is made in Phase 3. This is only a checkpoint of the desired
    remote service state based on the bot's service records.
    """
    rows = sqlite_data.get("services") or pg_data.get("services") or []
    output: list[dict[str, Any]] = []
    for service in rows:
        data_gb = float(service.get("data_gb") or 0)
        output.append({
            "bot_service_id": service.get("id"),
            "user_telegram_id": service.get("user_telegram_id"),
            "service_name": service.get("name"),
            "token": service.get("token"),
            "data_total_mb": int(data_gb * 1024),
            "data_used_mb": int(service.get("data_used_mb") or 0),
            "expires_at": service.get("expires_at"),
            "status": service.get("status"),
            "pasarguard_remote_id": service.get("pasarguard_remote_id"),
            "phase3_note": "Pasarguard API is not connected yet; this is desired state only.",
        })
    return output


async def create_complete_backup(admin_id: int | None = None, output_dir: str | Path = "/tmp/howtoosee_backups", bot: Bot | None = None) -> tuple[Path, dict[str, Any]]:
    base = Path(output_dir) / f"backup-work-{_stamp()}"
    base.mkdir(parents=True, exist_ok=True)
    sqlite_data = _sqlite_tables()
    pg_data = await _pg_tables()
    usage_rows = await usage_summary_rows()
    ticket_file_manifest = await _download_ticket_files(base, bot)

    for table, rows in sqlite_data.items():
        _write_jsonl(base / "data" / "sqlite" / f"{table}.jsonl", rows)
    for table, rows in pg_data.items():
        _write_jsonl(base / "data" / "postgres" / f"{table}.jsonl", rows)
    _write_jsonl(base / "data" / "ticket_files_manifest.jsonl", ticket_file_manifest)
    pasarguard_summary = await write_pasarguard_checkpoint_files(base, sqlite_data, pg_data, admin_id)
    (base / "data" / "sqlite_dump.sql").write_text(_sqlite_dump(), encoding="utf-8")

    usage = {row["key"]: row["value"] for row in usage_rows}
    file_success = sum(1 for row in ticket_file_manifest if row.get("backed_up"))
    file_failed = sum(1 for row in ticket_file_manifest if not row.get("backed_up"))
    manifest = {
        "backup_version": BACKUP_VERSION,
        "bot_name": settings.brand_name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_by": admin_id,
        "database_engine": "postgresql+legacy_sqlite_bridge",
        "schema_version": "phase4.8-pasarguard-checkpoint",
        "counts": {
            "sqlite": {table: len(rows) for table, rows in sqlite_data.items()},
            "postgres": {table: len(rows) for table, rows in pg_data.items()},
            "ticket_files": {"total": len(ticket_file_manifest), "backed_up": file_success, "failed": file_failed},
        },
        "usage": usage,
        "ticket_files": {
            "included": bot is not None,
            "active_files_backed_up": file_success,
            "active_files_failed": file_failed,
            "manifest": "data/ticket_files_manifest.jsonl",
            "note": "Closed ticket files are deleted from bot-side access storage and are not included after closure.",
        },
        "pasarguard": pasarguard_summary,
    }
    (base / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    checksums: list[str] = []
    for path in sorted(p for p in base.rglob("*") if p.is_file() and "checksums" not in p.parts):
        checksums.append(f"{_sha256(path)}  {path.relative_to(base).as_posix()}")
    checksums_dir = base / "checksums"
    checksums_dir.mkdir(parents=True, exist_ok=True)
    (checksums_dir / "sha256.txt").write_text("\n".join(checksums) + "\n", encoding="utf-8")

    zip_path = Path(output_dir) / f"backup-howtoosee-{_stamp()}.zip"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(p for p in base.rglob("*") if p.is_file()):
            zf.write(path, arcname=path.relative_to(base).as_posix())
    return zip_path, manifest


def inspect_backup_file(zip_path: str | Path) -> dict[str, Any]:
    path = Path(zip_path)
    with zipfile.ZipFile(path, "r") as zf:
        names = set(zf.namelist())
        if "manifest.json" not in names:
            raise ValueError("manifest.json داخل فایل بک‌آپ پیدا نشد.")
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        checksum_ok = True
        if "checksums/sha256.txt" in names:
            for line in zf.read("checksums/sha256.txt").decode("utf-8").splitlines():
                if not line.strip():
                    continue
                expected, rel = line.split(None, 1)
                rel = rel.strip()
                if rel not in names:
                    checksum_ok = False
                    break
                actual = hashlib.sha256(zf.read(rel)).hexdigest()
                if actual != expected:
                    checksum_ok = False
                    break
        manifest["checksum_ok"] = checksum_ok
        manifest["file_name"] = path.name
        manifest["file_size"] = path.stat().st_size
        return manifest
