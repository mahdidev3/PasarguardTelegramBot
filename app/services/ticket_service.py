"""Ticket service layer for users and admins."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import desc, func, select, update
from sqlalchemy.orm import selectinload

from app.database import session_scope
from app.models import (
    Admin,
    Ticket,
    TicketAdminNote,
    TicketAttachment,
    TicketEvent,
    TicketMessage,
    User,
)

TEHRAN_TZ = timezone(timedelta(hours=3, minutes=30))

TICKET_CATEGORY_LABELS: dict[str, str] = {
    # These are user-facing ticket title presets. Internally we still store
    # category/related_type for backward compatibility, but the user flow no
    # longer asks for a separate "related to" category.
    "service": "📦 مشکل اتصال یا سرویس",
    "order": "🧾 مشکل پرداخت یا سفارش",
    "wallet": "💰 مشکل کیف پول",
    "technical": "🛠 مشکل فنی",
    "account": "👤 مشکل حساب کاربری",
    "general": "❓ سوال عمومی",
    "other": "✍️ موارد دیگر",
}

TICKET_STATUS_LABELS: dict[str, str] = {
    "open": "باز",
    "waiting_admin": "در انتظار پاسخ ادمین",
    "answered": "پاسخ داده‌شده",
    "waiting_user": "در انتظار پاسخ کاربر",
    "closed": "بسته‌شده",
    "reopened": "بازگشایی‌شده",
}

TICKET_PRIORITY_LABELS: dict[str, str] = {
    "low": "کم",
    "normal": "معمولی",
    "high": "زیاد",
    "urgent": "فوری",
}

ADMIN_ROLE_LABELS: dict[str, str] = {
    "super": "مدیریت کل",
    "sales": "مدیریت فروش",
    "support": "پشتیبانی",
    "marketing": "مدیریت مارکتینگ",
    "appearance": "مدیریت ظاهر",
}


@dataclass(frozen=True)
class TicketStats:
    open_count: int
    waiting_admin_count: int
    waiting_user_count: int
    closed_count: int


async def ensure_pg_user(telegram_id: int, username: str | None, first_name: str | None) -> User:
    async with session_scope() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if user is None:
            user = User(
                telegram_id=telegram_id,
                username=username,
                first_name=first_name,
                referral_code=str(telegram_id),
            )
            session.add(user)
            await session.flush()
        else:
            user.username = username
            user.first_name = first_name
        return user


async def seed_bootstrap_admins(admin_ids: set[int]) -> None:
    if not admin_ids:
        return
    async with session_scope() as session:
        for admin_id in admin_ids:
            result = await session.execute(select(Admin).where(Admin.telegram_id == admin_id))
            admin = result.scalar_one_or_none()
            if admin is None:
                session.add(Admin(telegram_id=admin_id, role="super", display_name=str(admin_id), is_active=True))
            else:
                admin.role = "super"
                if not getattr(admin, "display_name", None):
                    admin.display_name = str(admin_id)
                admin.is_active = True


async def upsert_admin_role(telegram_id: int, role: str, added_by: int | None = None, display_name: str | None = None) -> None:
    """Keep the PostgreSQL admin table in sync with the legacy admin panel."""
    async with session_scope() as session:
        result = await session.execute(select(Admin).where(Admin.telegram_id == telegram_id))
        admin = result.scalar_one_or_none()
        if admin is None:
            session.add(Admin(telegram_id=telegram_id, role=role, added_by=added_by, display_name=(display_name or str(telegram_id)), is_active=True))
        else:
            admin.role = role
            admin.added_by = added_by
            if display_name is not None:
                admin.display_name = display_name or str(telegram_id)
            elif not getattr(admin, "display_name", None):
                admin.display_name = str(telegram_id)
            admin.is_active = True


async def deactivate_admin_role(telegram_id: int) -> None:
    """Disable an admin in PostgreSQL while keeping its audit/history row."""
    async with session_scope() as session:
        result = await session.execute(select(Admin).where(Admin.telegram_id == telegram_id))
        admin = result.scalar_one_or_none()
        if admin is not None:
            admin.is_active = False


async def is_admin(telegram_id: int) -> bool:
    async with session_scope() as session:
        result = await session.execute(
            select(Admin).where(Admin.telegram_id == telegram_id, Admin.is_active.is_(True))
        )
        return result.scalar_one_or_none() is not None


async def has_appearance_access(telegram_id: int) -> bool:
    """Allow only super admins and the dedicated appearance role to edit visual/text content."""
    async with session_scope() as session:
        result = await session.execute(
            select(Admin.role).where(Admin.telegram_id == telegram_id, Admin.is_active.is_(True))
        )
        role = str(result.scalar_one_or_none() or "")
    return role in {"super", "appearance"}


async def active_admin_ids() -> list[int]:
    async with session_scope() as session:
        result = await session.execute(select(Admin.telegram_id).where(Admin.is_active.is_(True)))
        return [int(x) for x in result.scalars().all()]


async def admin_role_label(telegram_id: int | None) -> str:
    if not telegram_id:
        return "ندارد"
    async with session_scope() as session:
        result = await session.execute(
            select(Admin.role).where(Admin.telegram_id == telegram_id, Admin.is_active.is_(True))
        )
        role = result.scalar_one_or_none()
    return ADMIN_ROLE_LABELS.get(str(role or ""), "ادمین")


async def admin_assignee_label(telegram_id: int | None, admin_view: bool = False) -> str:
    if not telegram_id:
        return "ندارد"
    label = await admin_role_label(telegram_id)
    if admin_view:
        return f"{label} | {telegram_id}"
    return label


async def create_ticket(
    user_telegram_id: int,
    category: str,
    related_type: str,
    related_id: str | None,
    subject: str,
    body: str | None = None,
) -> Ticket:
    """Create the ticket shell.

    The first user message is stored by add_ticket_message so media tickets do
    not appear twice in the conversation history. The body argument is kept for
    backward compatibility with earlier Phase 1 code.
    """
    async with session_scope() as session:
        ticket = Ticket(
            user_telegram_id=user_telegram_id,
            category=category,
            related_type=related_type or "general",
            related_id=related_id,
            subject=subject[:255],
            status="waiting_admin",
            priority="normal",
        )
        session.add(ticket)
        await session.flush()
        session.add(
            TicketEvent(
                ticket_id=ticket.id,
                actor_telegram_id=user_telegram_id,
                event_type="created",
                details=f"category={category}, related_type={related_type}, related_id={related_id}",
            )
        )
        await session.refresh(ticket)
        return ticket


async def add_ticket_message(
    ticket_id: int,
    sender_type: str,
    sender_telegram_id: int | None,
    body: str | None,
    message_type: str = "text",
    telegram_file_id: str | None = None,
    telegram_file_unique_id: str | None = None,
    file_name: str | None = None,
    mime_type: str | None = None,
    file_size: int | None = None,
) -> TicketMessage | None:
    async with session_scope() as session:
        ticket = await session.get(Ticket, ticket_id)
        if ticket is None:
            return None
        message = TicketMessage(
            ticket_id=ticket_id,
            sender_type=sender_type,
            sender_telegram_id=sender_telegram_id,
            body=body,
            message_type=message_type,
            telegram_file_id=telegram_file_id,
            telegram_file_unique_id=telegram_file_unique_id,
            file_name=file_name,
            mime_type=mime_type,
        )
        session.add(message)
        await session.flush()
        if telegram_file_id:
            session.add(
                TicketAttachment(
                    ticket_id=ticket_id,
                    message_id=message.id,
                    telegram_file_id=telegram_file_id,
                    telegram_file_unique_id=telegram_file_unique_id,
                    file_name=file_name,
                    mime_type=mime_type,
                    file_size=file_size,
                )
            )
        if sender_type == "user":
            ticket.status = "waiting_admin"
        elif sender_type == "admin":
            ticket.status = "waiting_user"
        ticket.updated_at = datetime.now(TEHRAN_TZ)
        session.add(
            TicketEvent(
                ticket_id=ticket_id,
                actor_telegram_id=sender_telegram_id,
                event_type="message_added",
                details=f"sender={sender_type}, message_type={message_type}",
            )
        )
        return message


async def add_admin_note(ticket_id: int, admin_telegram_id: int, note: str) -> bool:
    async with session_scope() as session:
        ticket = await session.get(Ticket, ticket_id)
        if ticket is None:
            return False
        session.add(TicketAdminNote(ticket_id=ticket_id, admin_telegram_id=admin_telegram_id, note=note))
        session.add(TicketEvent(ticket_id=ticket_id, actor_telegram_id=admin_telegram_id, event_type="admin_note_added", details=None))
        return True


async def get_ticket(ticket_id: int) -> Ticket | None:
    async with session_scope() as session:
        result = await session.execute(
            select(Ticket)
            .where(Ticket.id == ticket_id)
            .options(selectinload(Ticket.messages), selectinload(Ticket.notes), selectinload(Ticket.events))
        )
        return result.scalar_one_or_none()


async def list_user_tickets(user_telegram_id: int, status: str | None = None, limit: int = 20) -> list[Ticket]:
    async with session_scope() as session:
        stmt = select(Ticket).where(Ticket.user_telegram_id == user_telegram_id)
        if status:
            stmt = stmt.where(Ticket.status == status)
        result = await session.execute(stmt.order_by(desc(Ticket.id)).limit(limit))
        return list(result.scalars().all())


async def list_admin_tickets(filter_name: str = "open", admin_telegram_id: int | None = None, limit: int = 30) -> list[Ticket]:
    async with session_scope() as session:
        stmt = select(Ticket)
        if filter_name == "new":
            stmt = stmt.where(Ticket.status == "waiting_admin", Ticket.assigned_admin_id.is_(None))
        elif filter_name == "mine" and admin_telegram_id:
            stmt = stmt.where(Ticket.assigned_admin_id == admin_telegram_id, Ticket.status != "closed")
        elif filter_name == "closed":
            stmt = stmt.where(Ticket.status == "closed")
        elif filter_name == "waiting_user":
            stmt = stmt.where(Ticket.status == "waiting_user")
        else:
            stmt = stmt.where(Ticket.status != "closed")
        result = await session.execute(stmt.order_by(desc(Ticket.id)).limit(limit))
        return list(result.scalars().all())


async def assign_ticket(ticket_id: int, admin_telegram_id: int) -> bool:
    async with session_scope() as session:
        ticket = await session.get(Ticket, ticket_id)
        if ticket is None:
            return False
        ticket.assigned_admin_id = admin_telegram_id
        ticket.updated_at = datetime.now(TEHRAN_TZ)
        session.add(TicketEvent(ticket_id=ticket_id, actor_telegram_id=admin_telegram_id, event_type="assigned", details=str(admin_telegram_id)))
        return True


async def set_ticket_status(ticket_id: int, status: str, actor_telegram_id: int | None = None) -> bool:
    async with session_scope() as session:
        ticket = await session.get(Ticket, ticket_id)
        if ticket is None:
            return False
        ticket.status = status
        ticket.updated_at = datetime.now(TEHRAN_TZ)
        if status == "closed":
            ticket.closed_at = datetime.now(TEHRAN_TZ)
        elif status in {"open", "reopened", "waiting_admin", "waiting_user"}:
            ticket.closed_at = None
        session.add(TicketEvent(ticket_id=ticket_id, actor_telegram_id=actor_telegram_id, event_type="status_changed", details=status))
        return True


async def set_ticket_priority(ticket_id: int, priority: str, actor_telegram_id: int | None = None) -> bool:
    async with session_scope() as session:
        ticket = await session.get(Ticket, ticket_id)
        if ticket is None:
            return False
        ticket.priority = priority
        ticket.updated_at = datetime.now(TEHRAN_TZ)
        session.add(TicketEvent(ticket_id=ticket_id, actor_telegram_id=actor_telegram_id, event_type="priority_changed", details=priority))
        return True


async def count_active_ticket_attachments(ticket_id: int) -> int:
    async with session_scope() as session:
        result = await session.execute(
            select(func.count(TicketAttachment.id)).where(
                TicketAttachment.ticket_id == ticket_id,
                TicketAttachment.is_deleted.is_(False),
                TicketAttachment.telegram_file_id.is_not(None),
            )
        )
        return int(result.scalar_one() or 0)


async def purge_ticket_attachments(ticket_id: int, actor_telegram_id: int | None = None, reason: str = "ticket_closed") -> int:
    """Remove access to ticket files after close.

    Telegram does not provide a Bot API method to delete a file from Telegram's
    storage. What we can and should delete is the bot-side access token/file_id
    and mark the attachment unavailable, so admins cannot re-open it from the
    ticket after closure. Backups made before closure may still contain the file.
    """
    async with session_scope() as session:
        result = await session.execute(
            select(TicketAttachment).where(
                TicketAttachment.ticket_id == ticket_id,
                TicketAttachment.is_deleted.is_(False),
            )
        )
        attachments = list(result.scalars().all())
        for item in attachments:
            item.is_deleted = True
            item.deleted_at = datetime.now(TEHRAN_TZ)
            item.delete_reason = reason
            item.telegram_file_id = None
        await session.execute(
            update(TicketMessage)
            .where(TicketMessage.ticket_id == ticket_id, TicketMessage.telegram_file_id.is_not(None))
            .values(telegram_file_id=None)
        )
        if attachments:
            session.add(TicketEvent(ticket_id=ticket_id, actor_telegram_id=actor_telegram_id, event_type="attachments_deleted", details=f"count={len(attachments)}, reason={reason}"))
        return len(attachments)


async def ticket_stats() -> TicketStats:
    async with session_scope() as session:
        async def count_where(status: str) -> int:
            result = await session.execute(select(func.count(Ticket.id)).where(Ticket.status == status))
            return int(result.scalar_one() or 0)

        open_count = int((await session.execute(select(func.count(Ticket.id)).where(Ticket.status != "closed"))).scalar_one() or 0)
        waiting_admin_count = await count_where("waiting_admin")
        waiting_user_count = await count_where("waiting_user")
        closed_count = await count_where("closed")
        return TicketStats(open_count, waiting_admin_count, waiting_user_count, closed_count)
