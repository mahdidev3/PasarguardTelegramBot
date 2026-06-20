"""Restore service for Phase 3 complete backup ZIPs."""

from __future__ import annotations

import json
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import DateTime as SADateTime, delete, text

from app.config import settings
from app.database import session_scope
from app.models import Base
from app.services.backup_service import create_complete_backup, inspect_backup_file


def _parse_jsonl(raw: bytes) -> list[dict[str, Any]]:
    text = raw.decode("utf-8")
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _sqlite_restore_table(conn: sqlite3.Connection, table: str, rows: list[dict[str, Any]]) -> int:
    if not rows:
        try:
            conn.execute(f"DELETE FROM {table}")
        except Exception:
            pass
        return 0
    cols = list(rows[0].keys())
    placeholders = ", ".join(["?"] * len(cols))
    col_sql = ", ".join(cols)
    conn.execute(f"DELETE FROM {table}")
    conn.executemany(
        f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders})",
        [[row.get(col) for col in cols] for row in rows],
    )
    return len(rows)


def _restore_sqlite(zf: zipfile.ZipFile) -> dict[str, int]:
    names = zf.namelist()
    table_files = [n for n in names if n.startswith("data/sqlite/") and n.endswith(".jsonl")]
    if not table_files:
        return {}
    db_path = Path(settings.database_path)
    existed_before = db_path.exists()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        restored: dict[str, int] = {}
        # Tables usually exist because legacy bootstrap initializes them. For a blank DB, replay the dump first.
        if "data/sqlite_dump.sql" in names and not existed_before:
            conn.executescript(zf.read("data/sqlite_dump.sql").decode("utf-8"))
        for file_name in sorted(table_files):
            table = Path(file_name).stem
            rows = _parse_jsonl(zf.read(file_name))
            try:
                restored[table] = _sqlite_restore_table(conn, table, rows)
            except Exception:
                # If table does not exist or schema changed, skip rather than corrupting the restore.
                restored[table] = -1
        conn.commit()
        return restored
    finally:
        conn.close()


def _coerce_pg_row(table_name: str, row: dict[str, Any]) -> dict[str, Any]:
    table = Base.metadata.tables[table_name]
    out: dict[str, Any] = {}
    for col in table.columns:
        value = row.get(col.name)
        if value is not None and isinstance(col.type, SADateTime) and isinstance(value, str):
            try:
                value = datetime.fromisoformat(value)
            except ValueError:
                pass
        out[col.name] = value
    return out


async def _restore_postgres(zf: zipfile.ZipFile) -> dict[str, int]:
    names = zf.namelist()
    table_files = {Path(n).stem: n for n in names if n.startswith("data/postgres/") and n.endswith(".jsonl")}
    if not table_files:
        return {}
    restored: dict[str, int] = {}
    async with session_scope() as session:
        # Delete child tables first, then insert parents first.
        for table in reversed(Base.metadata.sorted_tables):
            if table.name in table_files:
                await session.execute(delete(table))
        await session.flush()
        for table in Base.metadata.sorted_tables:
            file_name = table_files.get(table.name)
            if not file_name:
                continue
            rows = [_coerce_pg_row(table.name, row) for row in _parse_jsonl(zf.read(file_name))]
            if rows:
                await session.execute(table.insert(), rows)
                if "id" in table.c:
                    # Keep PostgreSQL sequences aligned after explicit primary-key restore.
                    await session.execute(text(f"SELECT setval(pg_get_serial_sequence('\"{table.name}\"', 'id'), COALESCE((SELECT MAX(id) FROM \"{table.name}\"), 1), true)"))
            restored[table.name] = len(rows)
    return restored


async def restore_complete_backup(zip_path: str | Path, admin_id: int | None = None, make_emergency: bool = True) -> dict[str, Any]:
    manifest = inspect_backup_file(zip_path)
    if not manifest.get("checksum_ok"):
        raise ValueError("Checksum فایل بک‌آپ معتبر نیست؛ ریستور متوقف شد.")
    emergency_path = None
    if make_emergency:
        emergency_path, _ = await create_complete_backup(admin_id=admin_id)
    with zipfile.ZipFile(zip_path, "r") as zf:
        sqlite_result = _restore_sqlite(zf)
        pg_result = await _restore_postgres(zf)
    return {
        "restored_at": datetime.utcnow().isoformat(),
        "emergency_backup": str(emergency_path) if emergency_path else None,
        "source_manifest": manifest,
        "sqlite": sqlite_result,
        "postgres": pg_result,
    }









