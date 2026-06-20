"""Small PostgreSQL schema patches for staged checkpoints.

The project still uses create_all for quick testing. create_all does not alter
existing tables, so staged checkpoints apply additive/relaxing changes needed
for already-created databases.
"""

from __future__ import annotations

from sqlalchemy import text

from app.database import get_engine


async def apply_runtime_schema_patches() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        # Phase 3.1 ticket-media lifecycle fields.
        await conn.execute(text("ALTER TABLE IF EXISTS ticket_attachments ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN NOT NULL DEFAULT FALSE"))
        await conn.execute(text("ALTER TABLE IF EXISTS ticket_attachments ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP WITH TIME ZONE"))
        await conn.execute(text("ALTER TABLE IF EXISTS ticket_attachments ADD COLUMN IF NOT EXISTS delete_reason TEXT"))
        await conn.execute(text("ALTER TABLE IF EXISTS ticket_attachments ADD COLUMN IF NOT EXISTS backed_up_at TIMESTAMP WITH TIME ZONE"))
        await conn.execute(text("ALTER TABLE IF EXISTS ticket_attachments ADD COLUMN IF NOT EXISTS backup_path TEXT"))
        await conn.execute(text("ALTER TABLE IF EXISTS ticket_attachments ALTER COLUMN telegram_file_id DROP NOT NULL"))

        # Phase 4 Pasarguard bindings for existing Phase 2/3 tables.
        await conn.execute(text("ALTER TABLE IF EXISTS plans ADD COLUMN IF NOT EXISTS pasarguard_template_id INTEGER"))
        await conn.execute(text("ALTER TABLE IF EXISTS plans ADD COLUMN IF NOT EXISTS pasarguard_template_name VARCHAR(255)"))
        await conn.execute(text("ALTER TABLE IF EXISTS plans ADD COLUMN IF NOT EXISTS pasarguard_sync_status VARCHAR(40)"))
        await conn.execute(text("ALTER TABLE IF EXISTS plans ADD COLUMN IF NOT EXISTS pasarguard_sync_error TEXT"))
        await conn.execute(text("ALTER TABLE IF EXISTS plans ADD COLUMN IF NOT EXISTS pasarguard_last_sync_at TIMESTAMP WITH TIME ZONE"))
        await conn.execute(text("ALTER TABLE IF EXISTS plans ADD COLUMN IF NOT EXISTS pasarguard_last_state_json JSON"))

        await conn.execute(text("ALTER TABLE IF EXISTS services ADD COLUMN IF NOT EXISTS pasarguard_user_id INTEGER"))
        await conn.execute(text("ALTER TABLE IF EXISTS services ADD COLUMN IF NOT EXISTS pasarguard_username VARCHAR(255)"))
        await conn.execute(text("ALTER TABLE IF EXISTS services ADD COLUMN IF NOT EXISTS pasarguard_template_id INTEGER"))
        await conn.execute(text("ALTER TABLE IF EXISTS services ADD COLUMN IF NOT EXISTS pasarguard_subscription_url TEXT"))
        await conn.execute(text("ALTER TABLE IF EXISTS services ADD COLUMN IF NOT EXISTS pasarguard_last_sync_at TIMESTAMP WITH TIME ZONE"))
        await conn.execute(text("ALTER TABLE IF EXISTS services ADD COLUMN IF NOT EXISTS pasarguard_last_state_json JSON"))
        await conn.execute(text("ALTER TABLE IF EXISTS services ADD COLUMN IF NOT EXISTS pasarguard_sync_status VARCHAR(40)"))
        await conn.execute(text("ALTER TABLE IF EXISTS services ADD COLUMN IF NOT EXISTS pasarguard_sync_error TEXT"))











