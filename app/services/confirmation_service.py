"""Numeric second-confirmation service for dangerous admin operations."""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update

from app.database import session_scope
from app.models import AdminConfirmation

TEHRAN_TZ = timezone(timedelta(hours=3, minutes=30))


@dataclass(frozen=True)
class PendingConfirmation:
    confirmation_id: int
    code: str
    expires_at: datetime



def _aware_tehran(value: datetime) -> datetime:
    """Return a timezone-aware datetime in Tehran time.

    SQLAlchemy/asyncpg may return naive datetimes for columns that were
    inserted with timezone info depending on DB column type. Confirmation
    expiry checks must handle both naive and aware values safely.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=TEHRAN_TZ)
    return value.astimezone(TEHRAN_TZ)


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


async def create_confirmation(
    admin_telegram_id: int,
    action: str,
    payload: dict[str, Any],
    ttl_minutes: int = 5,
) -> PendingConfirmation:
    """Create a short-lived numeric confirmation and return the plain code once."""
    code = f"{secrets.randbelow(900000) + 100000}"
    expires_at = datetime.now(TEHRAN_TZ) + timedelta(minutes=ttl_minutes)
    async with session_scope() as session:
        item = AdminConfirmation(
            admin_telegram_id=admin_telegram_id,
            action=action,
            payload_json=json.dumps(payload, ensure_ascii=False),
            code_hash=_hash_code(code),
            expires_at=expires_at,
        )
        session.add(item)
        await session.flush()
        return PendingConfirmation(int(item.id), code, expires_at)


async def verify_confirmation(
    confirmation_id: int,
    admin_telegram_id: int,
    code: str,
    action: str | None = None,
) -> dict[str, Any] | None:
    """Verify a numeric code and mark it used. Returns payload on success."""
    normalized = "".join(ch for ch in str(code or "") if ch.isdigit())
    if not normalized:
        return None
    async with session_scope() as session:
        result = await session.execute(
            select(AdminConfirmation).where(
                AdminConfirmation.id == confirmation_id,
                AdminConfirmation.admin_telegram_id == admin_telegram_id,
                AdminConfirmation.used_at.is_(None),
            )
        )
        item = result.scalar_one_or_none()
        if item is None:
            return None
        if action and item.action != action:
            return None
        if _aware_tehran(item.expires_at) <= datetime.now(TEHRAN_TZ):
            return None
        if item.code_hash != _hash_code(normalized):
            return None
        payload = json.loads(item.payload_json or "{}")
        await session.execute(
            update(AdminConfirmation)
            .where(AdminConfirmation.id == item.id)
            .values(used_at=datetime.now(TEHRAN_TZ))
        )
        return payload
