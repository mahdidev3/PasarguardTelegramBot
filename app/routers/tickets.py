"""Phase 1 ticket router.

This router is included before the legacy catch-all router, so ticket buttons and
messages are handled here while the rest of the bot keeps working unchanged.
"""

from __future__ import annotations

import html
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.config import settings
from app.keyboards.tickets import (
    admin_ticket_files_kb,
    admin_ticket_home_kb,
    admin_ticket_list_kb,
    admin_ticket_view_kb,
    confirm_cancel_kb,
    ticket_body_kb,
    ticket_category_kb,
    ticket_home_kb,
    ticket_subject_kb,
    user_ticket_list_kb,
    user_ticket_view_kb,
)
from app.services.admin_audit_service import audit_log
from app.services.confirmation_service import create_confirmation, verify_confirmation
from app.services.ticket_service import (
    TICKET_CATEGORY_LABELS,
    TICKET_PRIORITY_LABELS,
    TICKET_STATUS_LABELS,
    active_admin_ids,
    add_admin_note,
    add_ticket_message,
    admin_assignee_label,
    assign_ticket,
    create_ticket,
    ensure_pg_user,
    get_ticket,
    is_admin,
    list_admin_tickets,
    list_user_tickets,
    purge_ticket_attachments,
    count_active_ticket_attachments,
    set_ticket_priority,
    set_ticket_status,
    ticket_stats,
)

TICKET_MENU_TEXT = "🎫 پشتیبانی / تیکت‌ها"

ticket_router = Router(name="phase1_tickets")


class TicketStates(StatesGroup):
    waiting_subject = State()
    waiting_body = State()
    waiting_user_reply = State()
    waiting_admin_reply = State()
    waiting_admin_note = State()
    waiting_confirm_code = State()


def h(value: Any) -> str:
    return html.escape(str(value or ""))


def header(title: str, subtitle: str = "") -> str:
    sub = f"\n<code>{h(subtitle)}</code>" if subtitle else ""
    return f"<b>{h(title)}</b>{sub}\n\n"


def user_identity(message: Message) -> tuple[int, str | None, str | None]:
    user = message.from_user
    if not user:
        raise RuntimeError("No Telegram user in message")
    return user.id, user.username, user.first_name


def callback_user_identity(callback: CallbackQuery) -> tuple[int, str | None, str | None]:
    user = callback.from_user
    return user.id, user.username, user.first_name


async def edit_or_answer(callback: CallbackQuery, text: str, reply_markup=None) -> None:
    try:
        if callback.message:
            await callback.message.edit_text(text, reply_markup=reply_markup, disable_web_page_preview=True)
        else:
            await callback.answer(text)
    except Exception:
        if callback.message:
            await callback.message.answer(text, reply_markup=reply_markup, disable_web_page_preview=True)
    await callback.answer()


async def render_ticket(ticket: Any, admin_view: bool = False) -> str:
    status = TICKET_STATUS_LABELS.get(ticket.status, ticket.status)
    priority = TICKET_PRIORITY_LABELS.get(ticket.priority, ticket.priority)
    assignee = await admin_assignee_label(getattr(ticket, "assigned_admin_id", None), admin_view=admin_view)
    text = header("🎫 تیکت", f"#{ticket.id}")
    text += f"👤 کاربر: <code>{ticket.user_telegram_id}</code>\n"
    text += f"🚦 وضعیت: <b>{h(status)}</b>\n"
    text += f"⚡ اولویت: <b>{h(priority)}</b>\n"
    text += f"👮 مسئول: <b>{h(assignee)}</b>\n"
    text += f"🧾 عنوان: <b>{h(ticket.subject)}</b>\n\n"
    if getattr(ticket, "messages", None):
        text += "<b>آخرین پیام‌ها:</b>\n"
        for msg in sorted(ticket.messages, key=lambda item: item.id)[-6:]:
            sender = "کاربر" if msg.sender_type == "user" else "ادمین" if msg.sender_type == "admin" else "سیستم"
            body = msg.body or f"[{msg.message_type}]"
            if len(body) > 500:
                body = body[:500] + "…"
            text += f"\n<b>{h(sender)}</b>: {h(body)}"
    if admin_view and getattr(ticket, "notes", None):
        text += "\n\n<b>یادداشت‌های داخلی:</b>\n"
        for note in sorted(ticket.notes, key=lambda item: item.id)[-4:]:
            text += f"• <code>{note.admin_telegram_id}</code>: {h(note.note[:250])}\n"
    return text


def extract_message_payload(message: Message) -> dict[str, Any]:
    """Extract text/file metadata from Telegram message for ticket storage."""
    if message.photo:
        photo = message.photo[-1]
        return {
            "body": message.caption,
            "message_type": "photo",
            "telegram_file_id": photo.file_id,
            "telegram_file_unique_id": photo.file_unique_id,
            "file_name": None,
            "mime_type": "image/jpeg",
            "file_size": photo.file_size,
        }
    if message.document:
        doc = message.document
        return {
            "body": message.caption,
            "message_type": "document",
            "telegram_file_id": doc.file_id,
            "telegram_file_unique_id": doc.file_unique_id,
            "file_name": doc.file_name,
            "mime_type": doc.mime_type,
            "file_size": doc.file_size,
        }
    if message.video:
        video = message.video
        return {
            "body": message.caption,
            "message_type": "video",
            "telegram_file_id": video.file_id,
            "telegram_file_unique_id": video.file_unique_id,
            "file_name": video.file_name,
            "mime_type": video.mime_type,
            "file_size": video.file_size,
        }
    if message.voice:
        voice = message.voice
        return {
            "body": message.caption,
            "message_type": "voice",
            "telegram_file_id": voice.file_id,
            "telegram_file_unique_id": voice.file_unique_id,
            "file_name": None,
            "mime_type": voice.mime_type,
            "file_size": voice.file_size,
        }
    if message.audio:
        audio = message.audio
        return {
            "body": message.caption,
            "message_type": "audio",
            "telegram_file_id": audio.file_id,
            "telegram_file_unique_id": audio.file_unique_id,
            "file_name": audio.file_name,
            "mime_type": audio.mime_type,
            "file_size": audio.file_size,
        }
    return {
        "body": message.text or message.caption or "",
        "message_type": "text",
        "telegram_file_id": None,
        "telegram_file_unique_id": None,
        "file_name": None,
        "mime_type": None,
        "file_size": None,
    }


async def notify_admins(bot, text: str) -> None:
    ids = set(settings.bootstrap_super_admin_ids)
    ids.update(await active_admin_ids())
    for admin_id in ids:
        try:
            await bot.send_message(admin_id, text, disable_web_page_preview=True)
        except Exception:
            pass


@ticket_router.message(F.text == TICKET_MENU_TEXT)
async def ticket_home_message(message: Message, state: FSMContext) -> None:
    await state.clear()
    uid, username, first_name = user_identity(message)
    await ensure_pg_user(uid, username, first_name)
    await message.answer(
        header("🎫 پشتیبانی و تیکت‌ها")
        + "از این بخش می‌توانید برای سرویس، سفارش، کیف پول یا سوال عمومی تیکت ثبت کنید.",
        reply_markup=ticket_home_kb(),
    )


@ticket_router.callback_query(F.data == "ticket_home")
async def ticket_home_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    uid, username, first_name = callback_user_identity(callback)
    await ensure_pg_user(uid, username, first_name)
    await edit_or_answer(
        callback,
        header("🎫 پشتیبانی و تیکت‌ها") + "یکی از گزینه‌ها را انتخاب کنید.",
        ticket_home_kb(),
    )


@ticket_router.callback_query(F.data == "ticket_new")
async def ticket_new(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    uid, username, first_name = callback_user_identity(callback)
    await ensure_pg_user(uid, username, first_name)
    await state.update_data(ticket_flow={})
    await edit_or_answer(
        callback,
        header("🎫 تیکت جدید")
        + "عنوان تیکت را انتخاب کنید. اگر هیچ‌کدام مناسب نبود، «موارد دیگر» را بزنید و عنوان دلخواه خودتان را وارد کنید.",
        ticket_category_kb(),
    )


@ticket_router.callback_query(F.data.startswith("ticket_cat:"))
async def ticket_category(callback: CallbackQuery, state: FSMContext) -> None:
    selected_key = callback.data.split(":", 1)[1]
    if selected_key not in TICKET_CATEGORY_LABELS:
        await callback.answer("عنوان تیکت نامعتبر است.", show_alert=True)
        return

    if selected_key == "other":
        await state.update_data(
            ticket_flow={
                "category": "general",
                "related_type": "general",
                "related_id": None,
            }
        )
        await state.set_state(TicketStates.waiting_subject)
        await edit_or_answer(
            callback,
            header("🧾 عنوان تیکت", "موارد دیگر")
            + "یک عنوان کوتاه و واضح برای تیکت وارد کنید.\n\nمثلاً: <code>درخواست بررسی حساب</code>",
            ticket_subject_kb(),
        )
        return

    subject = TICKET_CATEGORY_LABELS.get(selected_key, selected_key)
    await state.update_data(
        ticket_flow={
            "category": "general",
            "related_type": "general",
            "related_id": None,
            "subject": subject[:255],
        }
    )
    await state.set_state(TicketStates.waiting_body)
    await edit_or_answer(
        callback,
        header("✉️ متن تیکت", subject)
        + "حالا توضیح کامل تیکت را فقط در یک پیام بفرستید.\n\n"
        + "اگر عکس، ویدیو یا فایل دارید، توضیح را در کپشن همان پیام بنویسید. اگر ویس می‌فرستید، همان ویس به عنوان پیام تیکت ثبت می‌شود.",
        ticket_body_kb(),
    )


@ticket_router.message(TicketStates.waiting_subject)
async def ticket_subject(message: Message, state: FSMContext) -> None:
    subject = (message.text or "").strip()
    if len(subject) < 3:
        await message.answer("❌ عنوان خیلی کوتاه است. لطفاً کمی واضح‌تر بنویسید.")
        return
    data = await state.get_data()
    flow = data.get("ticket_flow", {})
    flow["subject"] = subject[:255]
    await state.update_data(ticket_flow=flow)
    await state.set_state(TicketStates.waiting_body)
    await message.answer(
        header("✉️ متن تیکت")
        + "حالا کل توضیح تیکت را فقط در یک پیام بفرستید.\n\n"
        + "اگر عکس، ویدیو یا فایل دارید، توضیح را در کپشن همان پیام بنویسید. اگر ویس می‌فرستید، همان ویس به عنوان پیام تیکت ثبت می‌شود. لطفاً تا حد ممکن همه‌چیز، یعنی توضیح، عکس/ویدیو/فایل/ویس، را در همان یک پیام ارسال کنید تا تیکت درست ثبت شود.",
        reply_markup=ticket_body_kb(),
    )


@ticket_router.message(TicketStates.waiting_body)
async def ticket_body(message: Message, state: FSMContext) -> None:
    uid, username, first_name = user_identity(message)
    await ensure_pg_user(uid, username, first_name)
    payload = extract_message_payload(message)
    body = payload["body"] or "[فایل بدون توضیح]"
    data = await state.get_data()
    flow = data.get("ticket_flow", {})
    if not flow.get("subject") or not flow.get("category"):
        await state.clear()
        await message.answer("جریان ساخت تیکت ناقص شد. لطفاً دوباره تیکت جدید بسازید.", reply_markup=ticket_home_kb())
        return
    ticket = await create_ticket(
        user_telegram_id=uid,
        category=flow["category"],
        related_type=flow.get("related_type") or "general",
        related_id=flow.get("related_id"),
        subject=flow["subject"],
        body=body,
    )
    await add_ticket_message(
        ticket_id=ticket.id,
        sender_type="user",
        sender_telegram_id=uid,
        body=body,
        message_type=payload["message_type"],
        telegram_file_id=payload["telegram_file_id"],
        telegram_file_unique_id=payload["telegram_file_unique_id"],
        file_name=payload["file_name"],
        mime_type=payload["mime_type"],
        file_size=payload["file_size"],
    )
    await state.clear()
    await message.answer(
        header("✅ تیکت ساخته شد", f"#{ticket.id}")
        + "پیام شما ثبت شد و ادمین‌ها می‌توانند پاسخ بدهند.",
        reply_markup=user_ticket_view_kb(ticket.id, ticket.status),
    )
    await notify_admins(
        message.bot,
        header("🆕 تیکت جدید", f"#{ticket.id}")
        + f"👤 کاربر: <code>{uid}</code>\n"
        + f"🧾 عنوان: <b>{h(flow['subject'])}</b>",
    )


@ticket_router.callback_query(F.data.in_({"ticket_my_open", "ticket_my_closed"}))
async def ticket_my_list(callback: CallbackQuery) -> None:
    uid, username, first_name = callback_user_identity(callback)
    await ensure_pg_user(uid, username, first_name)
    if callback.data == "ticket_my_closed":
        rows = await list_user_tickets(uid, status="closed")
        title = "✅ تیکت‌های بسته‌شده"
    else:
        all_rows = await list_user_tickets(uid)
        rows = [item for item in all_rows if item.status != "closed"]
        title = "📂 تیکت‌های باز من"
    text = header(title) + ("تیکتی پیدا نشد." if not rows else "برای مشاهده جزئیات، یکی را انتخاب کنید:")
    await edit_or_answer(callback, text, user_ticket_list_kb(rows))


@ticket_router.callback_query(F.data.startswith("ticket_view:"))
async def ticket_view(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    ticket_id = int(callback.data.split(":", 1)[1])
    ticket = await get_ticket(ticket_id)
    if not ticket or ticket.user_telegram_id != uid:
        await callback.answer("تیکت پیدا نشد.", show_alert=True)
        return
    await edit_or_answer(callback, await render_ticket(ticket), user_ticket_view_kb(ticket_id, ticket.status))


@ticket_router.callback_query(F.data.startswith("ticket_reply:"))
async def ticket_reply_start(callback: CallbackQuery, state: FSMContext) -> None:
    ticket_id = int(callback.data.split(":", 1)[1])
    ticket = await get_ticket(ticket_id)
    if not ticket or ticket.user_telegram_id != callback.from_user.id:
        await callback.answer("تیکت پیدا نشد.", show_alert=True)
        return
    if ticket.status == "closed":
        await set_ticket_status(ticket_id, "reopened", callback.from_user.id)
    await state.set_state(TicketStates.waiting_user_reply)
    await state.update_data(ticket_id=ticket_id)
    await edit_or_answer(
        callback,
        header("✉️ پاسخ تیکت")
        + "پاسخ، فایل، عکس، ویدیو یا ویس را تا حد ممکن در یک پیام بفرستید. اگر فایل/عکس/ویدیو دارید، توضیح را در کپشن همان پیام بنویسید.",
        user_ticket_view_kb(ticket_id, ticket.status),
    )


@ticket_router.message(TicketStates.waiting_user_reply)
async def ticket_user_reply_finish(message: Message, state: FSMContext) -> None:
    uid = message.from_user.id if message.from_user else 0
    data = await state.get_data()
    ticket_id = int(data.get("ticket_id", 0))
    ticket = await get_ticket(ticket_id)
    if not ticket or ticket.user_telegram_id != uid:
        await state.clear()
        await message.answer("تیکت پیدا نشد.", reply_markup=ticket_home_kb())
        return
    payload = extract_message_payload(message)
    await add_ticket_message(ticket_id, "user", uid, **payload)
    await state.clear()
    await message.answer("✅ پاسخ شما ثبت شد.", reply_markup=user_ticket_view_kb(ticket_id, ticket.status))
    await notify_admins(message.bot, header("📨 پاسخ جدید کاربر", f"تیکت #{ticket_id}") + f"👤 کاربر: <code>{uid}</code>")


@ticket_router.callback_query(F.data.startswith("ticket_user_close:"))
async def ticket_user_close(callback: CallbackQuery) -> None:
    ticket_id = int(callback.data.split(":", 1)[1])
    ticket = await get_ticket(ticket_id)
    if not ticket or ticket.user_telegram_id != callback.from_user.id:
        await callback.answer("تیکت پیدا نشد.", show_alert=True)
        return
    deleted_count = await purge_ticket_attachments(ticket_id, callback.from_user.id, reason="closed_by_user")
    await set_ticket_status(ticket_id, "closed", callback.from_user.id)
    extra = f"\n\n🧹 فایل‌های مربوط به این تیکت از دسترس ربات پاک شد: <b>{deleted_count}</b> مورد." if deleted_count else ""
    await edit_or_answer(callback, header("✅ تیکت بسته شد") + f"تیکت <code>#{ticket_id}</code> بسته شد." + extra, ticket_home_kb())


# -----------------------------
# Admin ticket panel
# -----------------------------
@ticket_router.callback_query(F.data == "adm_tickets")
async def admin_ticket_home(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    stats = await ticket_stats()
    text = (
        header("🎫 مدیریت تیکت‌ها")
        + f"📂 باز: <b>{stats.open_count}</b>\n"
        + f"🆕 منتظر ادمین: <b>{stats.waiting_admin_count}</b>\n"
        + f"⏳ منتظر کاربر: <b>{stats.waiting_user_count}</b>\n"
        + f"✅ بسته‌شده: <b>{stats.closed_count}</b>"
    )
    await edit_or_answer(callback, text, admin_ticket_home_kb())


@ticket_router.callback_query(F.data.in_({"adm_tickets_new", "adm_tickets_open", "adm_tickets_mine", "adm_tickets_waiting_user", "adm_tickets_closed"}))
async def admin_ticket_list(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    mapping = {
        "adm_tickets_new": ("new", "🆕 تیکت‌های جدید"),
        "adm_tickets_open": ("open", "📂 همه تیکت‌های باز"),
        "adm_tickets_mine": ("mine", "👤 تیکت‌های من"),
        "adm_tickets_waiting_user": ("waiting_user", "⏳ منتظر کاربر"),
        "adm_tickets_closed": ("closed", "✅ بسته‌شده‌ها"),
    }
    filter_name, title = mapping[callback.data]
    rows = await list_admin_tickets(filter_name, callback.from_user.id)
    text = header(title) + ("موردی پیدا نشد." if not rows else "یک تیکت را انتخاب کنید:")
    await edit_or_answer(callback, text, admin_ticket_list_kb(rows))


@ticket_router.callback_query(F.data.startswith("adm_ticket:"))
async def admin_ticket_view(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    ticket_id = int(callback.data.split(":", 1)[1])
    ticket = await get_ticket(ticket_id)
    if not ticket:
        await callback.answer("تیکت پیدا نشد.", show_alert=True)
        return
    await edit_or_answer(callback, await render_ticket(ticket, admin_view=True), admin_ticket_view_kb(ticket_id, ticket.assigned_admin_id == callback.from_user.id))


@ticket_router.callback_query(F.data.startswith("adm_ticket_assign:"))
async def admin_ticket_assign(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    ticket_id = int(callback.data.split(":", 1)[1])
    ok = await assign_ticket(ticket_id, callback.from_user.id)
    if not ok:
        await callback.answer("تیکت پیدا نشد.", show_alert=True)
        return
    await audit_log(callback.from_user.id, "TICKET_ASSIGN", "ticket", ticket_id)
    ticket = await get_ticket(ticket_id)
    await edit_or_answer(callback, header("✅ تیکت به شما واگذار شد") + await render_ticket(ticket, admin_view=True), admin_ticket_view_kb(ticket_id, True))


@ticket_router.callback_query(F.data.startswith("adm_ticket_reply:"))
async def admin_ticket_reply_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    ticket_id = int(callback.data.split(":", 1)[1])
    if not await get_ticket(ticket_id):
        await callback.answer("تیکت پیدا نشد.", show_alert=True)
        return
    await state.set_state(TicketStates.waiting_admin_reply)
    await state.update_data(ticket_id=ticket_id)
    await edit_or_answer(
        callback,
        header("✉️ پاسخ ادمین")
        + "پاسخ، فایل، عکس، ویدیو یا ویس را تا حد ممکن در یک پیام ارسال کنید. اگر فایل/عکس/ویدیو دارید، توضیح را در کپشن همان پیام بنویسید.",
        admin_ticket_view_kb(ticket_id),
    )


@ticket_router.message(TicketStates.waiting_admin_reply)
async def admin_ticket_reply_finish(message: Message, state: FSMContext) -> None:
    if not message.from_user or not await is_admin(message.from_user.id):
        await message.answer("دسترسی ندارید.")
        return
    data = await state.get_data()
    ticket_id = int(data.get("ticket_id", 0))
    ticket = await get_ticket(ticket_id)
    if not ticket:
        await state.clear()
        await message.answer("تیکت پیدا نشد.", reply_markup=admin_ticket_home_kb())
        return
    payload = extract_message_payload(message)
    await add_ticket_message(ticket_id, "admin", message.from_user.id, **payload)
    await audit_log(message.from_user.id, "TICKET_REPLY", "ticket", ticket_id, payload.get("message_type"))
    await state.clear()
    await message.answer("✅ پاسخ ادمین ثبت شد و برای کاربر ارسال می‌شود.", reply_markup=admin_ticket_view_kb(ticket_id))
    try:
        if payload["telegram_file_id"] and payload["message_type"] == "photo":
            await message.bot.send_photo(ticket.user_telegram_id, payload["telegram_file_id"], caption=payload["body"] or f"پاسخ تیکت #{ticket_id}")
        elif payload["telegram_file_id"] and payload["message_type"] == "document":
            await message.bot.send_document(ticket.user_telegram_id, payload["telegram_file_id"], caption=payload["body"] or f"پاسخ تیکت #{ticket_id}")
        elif payload["telegram_file_id"] and payload["message_type"] == "video":
            await message.bot.send_video(ticket.user_telegram_id, payload["telegram_file_id"], caption=payload["body"] or f"پاسخ تیکت #{ticket_id}")
        elif payload["telegram_file_id"] and payload["message_type"] == "voice":
            await message.bot.send_voice(ticket.user_telegram_id, payload["telegram_file_id"], caption=payload["body"] or f"پاسخ تیکت #{ticket_id}")
        elif payload["telegram_file_id"] and payload["message_type"] == "audio":
            await message.bot.send_audio(ticket.user_telegram_id, payload["telegram_file_id"], caption=payload["body"] or f"پاسخ تیکت #{ticket_id}")
        else:
            await message.bot.send_message(ticket.user_telegram_id, header("📨 پاسخ پشتیبانی", f"تیکت #{ticket_id}") + h(payload["body"] or "[فایل]"))
    except Exception:
        await message.answer("⚠️ پاسخ ذخیره شد، اما ارسال مستقیم به کاربر ناموفق بود.")


@ticket_router.callback_query(F.data.startswith("adm_ticket_note:"))
async def admin_ticket_note_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    ticket_id = int(callback.data.split(":", 1)[1])
    await state.set_state(TicketStates.waiting_admin_note)
    await state.update_data(ticket_id=ticket_id)
    await edit_or_answer(callback, header("📝 یادداشت داخلی") + "یادداشت داخلی را وارد کنید. کاربر این متن را نمی‌بیند.", admin_ticket_view_kb(ticket_id))


@ticket_router.message(TicketStates.waiting_admin_note)
async def admin_ticket_note_finish(message: Message, state: FSMContext) -> None:
    if not message.from_user or not await is_admin(message.from_user.id):
        await message.answer("دسترسی ندارید.")
        return
    data = await state.get_data()
    ticket_id = int(data.get("ticket_id", 0))
    note = (message.text or "").strip()
    if len(note) < 2:
        await message.answer("یادداشت خیلی کوتاه است.")
        return
    await add_admin_note(ticket_id, message.from_user.id, note)
    await audit_log(message.from_user.id, "TICKET_INTERNAL_NOTE", "ticket", ticket_id)
    await state.clear()
    await message.answer("✅ یادداشت داخلی ثبت شد.", reply_markup=admin_ticket_view_kb(ticket_id))


@ticket_router.callback_query(F.data.startswith("adm_ticket_prio:"))
async def admin_ticket_priority(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    _, ticket_id_s, priority = callback.data.split(":")
    ticket_id = int(ticket_id_s)
    await set_ticket_priority(ticket_id, priority, callback.from_user.id)
    await audit_log(callback.from_user.id, "TICKET_PRIORITY", "ticket", ticket_id, priority)
    ticket = await get_ticket(ticket_id)
    await edit_or_answer(callback, await render_ticket(ticket, admin_view=True), admin_ticket_view_kb(ticket_id))


@ticket_router.callback_query(F.data.startswith("adm_ticket_reopen:"))
async def admin_ticket_reopen(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    ticket_id = int(callback.data.split(":", 1)[1])
    await set_ticket_status(ticket_id, "reopened", callback.from_user.id)
    await audit_log(callback.from_user.id, "TICKET_REOPEN", "ticket", ticket_id)
    ticket = await get_ticket(ticket_id)
    await edit_or_answer(callback, await render_ticket(ticket, admin_view=True), admin_ticket_view_kb(ticket_id))


@ticket_router.callback_query(F.data.startswith("adm_ticket_close_ask:"))
async def admin_ticket_close_ask(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    ticket_id = int(callback.data.split(":", 1)[1])
    file_count = await count_active_ticket_attachments(ticket_id)
    confirmation = await create_confirmation(
        admin_telegram_id=callback.from_user.id,
        action="ticket_close",
        payload={"ticket_id": ticket_id, "file_count": file_count},
        ttl_minutes=5,
    )
    await state.set_state(TicketStates.waiting_confirm_code)
    await state.update_data(confirmation_id=confirmation.confirmation_id, action="ticket_close", ticket_id=ticket_id)
    await edit_or_answer(
        callback,
        header("⚠️ تأیید دوم عددی")
        + f"برای بستن تیکت <code>#{ticket_id}</code> کد زیر را وارد کنید:\n\n"
        + f"⚠️ با بستن تیکت، فایل‌ها/عکس‌ها/ویدیوها/ویس‌های مربوط به این تیکت از دسترس ربات پاک می‌شوند. تعداد فایل فعال: <b>{file_count}</b>\n\n"
        + f"<code>{confirmation.code}</code>\n\n"
        + "این کد تا ۵ دقیقه معتبر است.",
        confirm_cancel_kb(f"adm_ticket:{ticket_id}"),
    )


@ticket_router.message(TicketStates.waiting_confirm_code)
async def admin_confirmation_finish(message: Message, state: FSMContext) -> None:
    if not message.from_user or not await is_admin(message.from_user.id):
        await message.answer("دسترسی ندارید.")
        return
    data = await state.get_data()
    confirmation_id = int(data.get("confirmation_id", 0))
    action = str(data.get("action", ""))
    payload = await verify_confirmation(confirmation_id, message.from_user.id, message.text or "", action)
    if not payload:
        await message.answer("❌ کد نامعتبر یا منقضی شده است. دوباره تلاش کنید.")
        return
    if action == "ticket_close":
        ticket_id = int(payload["ticket_id"])
        deleted_count = await purge_ticket_attachments(ticket_id, message.from_user.id, reason="closed_by_admin")
        await set_ticket_status(ticket_id, "closed", message.from_user.id)
        await audit_log(message.from_user.id, "TICKET_CLOSE_CONFIRMED", "ticket", ticket_id, f"deleted_files={deleted_count}")
        await state.clear()
        await message.answer(header("✅ تیکت بسته شد") + f"تیکت <code>#{ticket_id}</code> با تأیید عددی بسته شد.\n\n🧹 فایل‌های پاک‌شده از دسترس ربات: <b>{deleted_count}</b>", reply_markup=admin_ticket_home_kb())
        return
    await state.clear()
    await message.answer("کد تأیید ثبت شد، اما عملیات شناخته نشد.", reply_markup=admin_ticket_home_kb())


@ticket_router.callback_query(F.data.startswith("adm_ticket_files:"))
async def admin_ticket_files(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    ticket_id = int(callback.data.split(":", 1)[1])
    ticket = await get_ticket(ticket_id)
    if not ticket:
        await callback.answer("تیکت پیدا نشد.", show_alert=True)
        return
    file_messages = [m for m in sorted(ticket.messages, key=lambda item: item.id) if m.message_type in {"photo", "document", "video", "voice", "audio"}]
    active_file_messages = [m for m in file_messages if m.telegram_file_id]
    text = header("📎 فایل‌های تیکت", f"#{ticket_id}")
    if not file_messages:
        text += "فایلی برای این تیکت ثبت نشده است."
        await edit_or_answer(callback, text, admin_ticket_view_kb(ticket_id))
        return
    if not active_file_messages:
        text += "فایل‌های این تیکت قبلاً هنگام بستن تیکت پاک شده‌اند و دیگر در دسترس نیستند. اگر روی هر مورد بزنید، پیام عدم دسترسی نمایش داده می‌شود.\n\n"
    else:
        text += "برای دیدن فایل، روی مورد موردنظر بزنید. ربات همان فایل/عکس/ویدیو/ویس را برای شما ارسال می‌کند. موارد پاک‌شده دیگر قابل مشاهده نیستند.\n\n"
    for msg in file_messages[:25]:
        name = msg.file_name or msg.message_type
        body = (msg.body or "").strip()
        if len(body) > 80:
            body = body[:80] + "…"
        availability = "✅ موجود" if msg.telegram_file_id else "🗑 پاک‌شده"
        text += f"• پیام <code>#{msg.id}</code> | <b>{h(msg.message_type)}</b> | {h(name)} | {availability}"
        if body:
            text += f" — {h(body)}"
        text += "\n"
    await edit_or_answer(callback, text, admin_ticket_files_kb(ticket_id, file_messages))


@ticket_router.callback_query(F.data.startswith("adm_ticket_file:"))
async def admin_ticket_file_send(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    try:
        _, ticket_id_s, message_id_s = callback.data.split(":")
        ticket_id = int(ticket_id_s)
        message_id = int(message_id_s)
    except Exception:
        await callback.answer("درخواست فایل نامعتبر است.", show_alert=True)
        return
    ticket = await get_ticket(ticket_id)
    if not ticket:
        await callback.answer("تیکت پیدا نشد.", show_alert=True)
        return
    msg = next((m for m in ticket.messages if m.id == message_id), None)
    if not msg:
        await callback.answer("فایل پیدا نشد.", show_alert=True)
        return
    if not msg.telegram_file_id:
        await callback.answer("این فایل دیگر در دسترس نیست؛ احتمالاً بعد از بسته‌شدن تیکت پاک شده است.", show_alert=True)
        return
    caption = msg.body or f"فایل تیکت #{ticket_id} | پیام #{message_id}"
    if len(caption) > 1024:
        caption = caption[:1020] + "…"
    try:
        if msg.message_type == "photo":
            await callback.message.answer_photo(msg.telegram_file_id, caption=caption)
        elif msg.message_type == "video":
            await callback.message.answer_video(msg.telegram_file_id, caption=caption)
        elif msg.message_type == "voice":
            await callback.message.answer_voice(msg.telegram_file_id, caption=caption)
        elif msg.message_type == "audio":
            await callback.message.answer_audio(msg.telegram_file_id, caption=caption)
        else:
            await callback.message.answer_document(msg.telegram_file_id, caption=caption)
        await callback.answer("فایل ارسال شد.")
    except Exception:
        await callback.answer("ارسال فایل ناموفق بود. شاید فایل از سمت تلگرام در دسترس نباشد.", show_alert=True)











