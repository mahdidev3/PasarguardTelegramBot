"""Inline keyboards for Phase 1 ticket flows."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.services.ticket_service import TICKET_CATEGORY_LABELS


def inline(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=data) for text, data in row] for row in rows]
    )


def ticket_home_kb() -> InlineKeyboardMarkup:
    return inline(
        [
            [("🎫 تیکت جدید", "ticket_new")],
            [("📂 تیکت‌های باز من", "ticket_my_open"), ("✅ بسته‌شده‌ها", "ticket_my_closed")],
            [("🏠 منوی اصلی", "home")],
        ]
    )


def ticket_category_kb() -> InlineKeyboardMarkup:
    rows = [[(label, f"ticket_cat:{key}")] for key, label in TICKET_CATEGORY_LABELS.items()]
    rows.append([("⬅️ بازگشت", "ticket_home"), ("🏠 منوی اصلی", "home")])
    return inline(rows)


def user_ticket_list_kb(ticket_rows: list[object], back: str = "ticket_home") -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    for ticket in ticket_rows[:20]:
        rows.append([(f"🎫 #{ticket.id} | {ticket.subject[:30]}", f"ticket_view:{ticket.id}")])
    rows.append([("⬅️ بازگشت", back), ("🏠 منوی اصلی", "home")])
    return inline(rows)


def user_ticket_view_kb(ticket_id: int) -> InlineKeyboardMarkup:
    return inline(
        [
            [("✉️ ارسال پاسخ/فایل", f"ticket_reply:{ticket_id}"), ("✅ بستن تیکت", f"ticket_user_close:{ticket_id}")],
            [("⬅️ تیکت‌های من", "ticket_my_open"), ("🏠 منوی اصلی", "home")],
        ]
    )


def admin_ticket_home_kb() -> InlineKeyboardMarkup:
    return inline(
        [
            [("🆕 جدید", "adm_tickets_new"), ("📂 همه باز", "adm_tickets_open")],
            [("👤 تیکت‌های من", "adm_tickets_mine"), ("⏳ منتظر کاربر", "adm_tickets_waiting_user")],
            [("✅ بسته‌شده‌ها", "adm_tickets_closed")],
            [("👑 منوی ادمین", "adm_home")],
        ]
    )


def admin_ticket_list_kb(ticket_rows: list[object], back: str = "adm_tickets") -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    for ticket in ticket_rows[:30]:
        rows.append([(f"🎫 #{ticket.id} | {ticket.priority} | {ticket.subject[:28]}", f"adm_ticket:{ticket.id}")])
    rows.append([("⬅️ بازگشت", back), ("👑 منوی ادمین", "adm_home")])
    return inline(rows)


def admin_ticket_view_kb(ticket_id: int, assigned_to_me: bool = False) -> InlineKeyboardMarkup:
    return inline(
        [
            [("✉️ پاسخ دادن", f"adm_ticket_reply:{ticket_id}"), ("👮 گرفتن مسئولیت", f"adm_ticket_assign:{ticket_id}")],
            [("📝 یادداشت داخلی", f"adm_ticket_note:{ticket_id}"), ("📎 فایل‌ها", f"adm_ticket_files:{ticket_id}")],
            [("⚡ اولویت زیاد", f"adm_ticket_prio:{ticket_id}:high"), ("🔥 فوری", f"adm_ticket_prio:{ticket_id}:urgent")],
            [("✅ بستن با کد", f"adm_ticket_close_ask:{ticket_id}"), ("🔁 باز کردن", f"adm_ticket_reopen:{ticket_id}")],
            [("⬅️ لیست تیکت‌ها", "adm_tickets_open"), ("👑 منوی ادمین", "adm_home")],
        ]
    )


def confirm_cancel_kb(back_callback: str) -> InlineKeyboardMarkup:
    return inline([[('❌ لغو', back_callback), ('👑 منوی ادمین', 'adm_home')]])
