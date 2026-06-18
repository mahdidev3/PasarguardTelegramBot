"""Application bootstrap for Phase 1."""

from __future__ import annotations

from app.config import settings
from app.database import init_database
from app.services.ticket_service import seed_bootstrap_admins


async def bootstrap_phase1() -> None:
    """Initialize PostgreSQL schema and seed bootstrap super admins."""
    await init_database()
    await seed_bootstrap_admins(settings.bootstrap_super_admin_ids)
