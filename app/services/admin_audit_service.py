"""Admin audit logging for PostgreSQL-backed Phase 1 features."""

from __future__ import annotations

from app.database import session_scope
from app.models import AdminLog


async def audit_log(
    admin_telegram_id: int,
    action: str,
    target_type: str | None = None,
    target_id: str | int | None = None,
    details: str | None = None,
) -> None:
    async with session_scope() as session:
        session.add(
            AdminLog(
                admin_telegram_id=admin_telegram_id,
                action=action,
                target_type=target_type,
                target_id=str(target_id) if target_id is not None else None,
                details=details,
            )
        )


