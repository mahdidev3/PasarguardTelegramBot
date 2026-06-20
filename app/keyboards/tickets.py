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


def ticket_subject_kb() -> InlineKeyboardMarkup:
    return inline([[('⬅️ تغییر عنوان', 'ticket_new'), ('🏠 منوی اصلی', 'home')]])


def ticket_body_kb() -> InlineKeyboardMarkup:
    return inline([[('⬅️ تغییر عنوان', 'ticket_new'), ('🏠 منوی اصلی', 'home')]])


def user_ticket_list_kb(ticket_rows: list[object], back: str = "ticket_home") -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    for ticket in ticket_rows[:20]:
        rows.append([(f"🎫 #{ticket.id} | {ticket.subject[:30]}", f"ticket_view:{ticket.id}")])
    rows.append([("⬅️ بازگشت", back), ("🏠 منوی اصلی", "home")])
    return inline(rows)


def user_ticket_view_kb(ticket_id: int, status: str | None = None) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    if status == "closed":
        # A closed ticket must not show the "close ticket" button again.
        rows.append([("✉️ ارسال پاسخ/بازگشایی", f"ticket_reply:{ticket_id}")])
    else:
        rows.append([("✉️ ارسال پاسخ/فایل", f"ticket_reply:{ticket_id}"), ("✅ بستن تیکت", f"ticket_user_close:{ticket_id}")])
    rows.append([("⬅️ تیکت‌های من", "ticket_my_open"), ("🏠 منوی اصلی", "home")])
    return inline(rows)




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
    assign_text = "✅ مسئولیت با شماست" if assigned_to_me else "👮 گرفتن مسئولیت"
    return inline(
        [
            [("✉️ پاسخ دادن", f"adm_ticket_reply:{ticket_id}"), (assign_text, f"adm_ticket_assign:{ticket_id}")],
            [("📝 یادداشت داخلی", f"adm_ticket_note:{ticket_id}"), ("📎 فایل‌ها", f"adm_ticket_files:{ticket_id}")],
            [("⚡ اولویت زیاد", f"adm_ticket_prio:{ticket_id}:high"), ("🔥 فوری", f"adm_ticket_prio:{ticket_id}:urgent")],
            [("✅ بستن با کد", f"adm_ticket_close_ask:{ticket_id}"), ("🔁 باز کردن", f"adm_ticket_reopen:{ticket_id}")],
            [("⬅️ لیست تیکت‌ها", "adm_tickets_open"), ("👑 منوی ادمین", "adm_home")],
        ]
    )


def admin_ticket_files_kb(ticket_id: int, file_messages: list[object]) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    icons = {
        "photo": "🖼",
        "video": "🎬",
        "voice": "🎤",
        "audio": "🎧",
        "document": "📄",
    }
    for msg in file_messages[:25]:
        icon = icons.get(getattr(msg, "message_type", ""), "📎")
        name = getattr(msg, "file_name", None) or getattr(msg, "message_type", "file")
        rows.append([(f"{icon} پیام #{getattr(msg, 'id', 0)} | {str(name)[:28]}", f"adm_ticket_file:{ticket_id}:{getattr(msg, 'id', 0)}")])
    rows.append([("⬅️ برگشت به تیکت", f"adm_ticket:{ticket_id}"), ("👑 منوی ادمین", "adm_home")])
    return inline(rows)


def confirm_cancel_kb(back_callback: str) -> InlineKeyboardMarkup:
    return inline([[('❌ لغو', back_callback), ('👑 منوی ادمین', 'adm_home')]])











