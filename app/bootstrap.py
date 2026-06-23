"""Application bootstrap for the staged refactor."""

from __future__ import annotations

from app.config import settings
from app.database import init_database
from app.services.plan_service import seed_catalog_defaults, sync_legacy_catalog_from_db
from app.services.text_template_service import seed_text_templates
from app.services.ticket_service import seed_bootstrap_admins
from app.services.schema_patch_service import apply_runtime_schema_patches


async def bootstrap_phase1() -> None:
    """Initialize PostgreSQL schema and seed bootstrap super admins."""
    await init_database()
    await apply_runtime_schema_patches()
    await seed_bootstrap_admins(settings.bootstrap_super_admin_ids)
    await seed_catalog_defaults(settings.free_test_mb)
    await seed_text_templates()
    try:
        import app.legacy_bot as legacy_bot
        await sync_legacy_catalog_from_db(legacy_bot)
    except Exception:
        # Legacy catalog sync is a bridge only; failing here should not hide DB init errors.
        pass


