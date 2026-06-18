"""Database access boundary.

Phase 0 creates this boundary so future code does not talk directly to sqlite3 or
SQLAlchemy from handlers. The current bot still uses app.legacy_bot.DB until the
PostgreSQL migration phase.
"""

from __future__ import annotations

from app.config import settings


def current_database_description() -> str:
    if settings.database_url:
        return "DATABASE_URL is configured; future phases can use PostgreSQL."
    return f"Using legacy SQLite path: {settings.database_path}"
