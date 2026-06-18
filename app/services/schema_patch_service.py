"""Small PostgreSQL schema patches for staged checkpoints.

The project still uses create_all for quick testing. create_all does not alter
existing tables, so Phase 3.1 applies the few additive/relaxing changes needed
for already-created Phase 3 databases.
"""

from __future__ import annotations

from sqlalchemy import text

from app.database import get_engine


async def apply_runtime_schema_patches() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE IF EXISTS ticket_attachments ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN NOT NULL DEFAULT FALSE"))
        await conn.execute(text("ALTER TABLE IF EXISTS ticket_attachments ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP WITH TIME ZONE"))
        await conn.execute(text("ALTER TABLE IF EXISTS ticket_attachments ADD COLUMN IF NOT EXISTS delete_reason TEXT"))
        await conn.execute(text("ALTER TABLE IF EXISTS ticket_attachments ADD COLUMN IF NOT EXISTS backed_up_at TIMESTAMP WITH TIME ZONE"))
        await conn.execute(text("ALTER TABLE IF EXISTS ticket_attachments ADD COLUMN IF NOT EXISTS backup_path TEXT"))
        await conn.execute(text("ALTER TABLE IF EXISTS ticket_attachments ALTER COLUMN telegram_file_id DROP NOT NULL"))
