"""Async SQLAlchemy database setup for Phase 1.

Phase 1 moves new stateful features to PostgreSQL. Existing legacy handlers are
still being migrated gradually, but tickets and admin confirmations use this
module immediately.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models import Base

_engine: Optional[AsyncEngine] = None
_session_factory: Optional[async_sessionmaker[AsyncSession]] = None


def normalize_database_url(url: str) -> str:
    """Accept common PostgreSQL URLs and convert them to SQLAlchemy async form."""
    value = (url or "").strip()
    if value.startswith("postgresql://"):
        return value.replace("postgresql://", "postgresql+asyncpg://", 1)
    if value.startswith("postgres://"):
        return value.replace("postgres://", "postgresql+asyncpg://", 1)
    return value


def get_engine() -> AsyncEngine:
    global _engine, _session_factory
    if _engine is None:
        database_url = normalize_database_url(settings.database_url)
        if not database_url:
            raise RuntimeError(
                "DATABASE_URL is missing. Phase 1 requires PostgreSQL, for example: "
                "postgresql+asyncpg://pasarguard_bot:password@127.0.0.1:5432/pasarguard_bot"
            )
        _engine = create_async_engine(database_url, pool_pre_ping=True, echo=False)
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _session_factory is not None
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Provide a transaction-scoped async session."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_database() -> None:
    """Create tables for Phase 1.

    Alembic files are intentionally kept for the next hardening step. For this
    checkpoint we use create_all so the user can test quickly on a fresh DB.
    """
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


