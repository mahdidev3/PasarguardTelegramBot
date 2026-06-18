"""Phase 2 media/file/button broadcast router."""

from __future__ import annotations

import html
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup

from app.services.admin_audit_service import audit_log
from app.services.broadcast_service import (
    DraftPayload,
    TARGET_LABELS,
    buttons_markup,
    campaign_stats,
    create_campaign,
    parse_buttons,
    resolve_recipients,
    send_campaign,
    send_campaign_message,
)
from app.services.confirmation_service import create_confirmation, verify_confirmation
from app.services.ticket_service import is_admin

broadcast_router = Router(name="phase2_broadcast")


class BroadcastStates(StatesGroup):
    waiting_manual_users = State()
    waiting_content = State()
    waiting_buttons = State()
    waiting_confirm_code = State()


def h(value: Any) -> str:
    return html.escape(str(value or ""))


def header(title: str, subtitle: str = "") -> str:
    sub = f"\n<code>{h(subtitle)}</code>" if subtitle else ""
    return f"<b>{h(title)}</b>{sub}\n\n"


def inline(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])


def broadcast_home_kb() -> InlineKeyboardMarkup:
    return inline([
        [("➕ کمپین جدید", "adm_broadcast_new")],
        [("👑 منوی ادمین", "adm_home")],
    ])


def target_kb() -> InlineKeyboardMarkup:
    return inline([
        [("👥 همه", "adm_bc_target:all"), ("🟢 فعال", "adm_bc_target:active")],
        [("💳 خریدار", "adm_bc_target:buyers"), ("🎁 بدون خرید", "adm_bc_target:no_purchase")],
        [("📦 سرویس فعال", "adm_bc_target:active_services"), ("📝 لیست دستی", "adm_bc_target:manual")],
        [("⬅️ بازگشت", "adm_broadcast"), ("👑 منوی ادمین", "adm_home")],
    ])


def confirm_kb(campaign_id: int) -> InlineKeyboardMarkup:
    return inline([
        [("✅ دریافت کد تأیید", f"adm_bc_confirm:{campaign_id}"), ("❌ لغو", "adm_broadcast")],
        [("👑 منوی ادمین", "adm_home")],
    ])


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


def extract_message_payload(message: Message) -> dict[str, Any]:
    caption = message.caption or None
    if message.photo:
        file = message.photo[-1]
        return {"message_type": "photo", "caption": caption, "telegram_file_id": file.file_id, "telegram_file_unique_id": file.file_unique_id}
    if message.video:
        file = message.video
        return {"message_type": "video", "caption": caption, "telegram_file_id": file.file_id, "telegram_file_unique_id": file.file_unique_id, "file_name": file.file_name, "mime_type": file.mime_type}
    if message.document:
        file = message.document
        return {"message_type": "document", "caption": caption, "telegram_file_id": file.file_id, "telegram_file_unique_id": file.file_unique_id, "file_name": file.file_name, "mime_type": file.mime_type}
    if message.voice:
        file = message.voice
        return {"message_type": "voice", "caption": caption, "telegram_file_id": file.file_id, "telegram_file_unique_id": file.file_unique_id, "mime_type": file.mime_type}
    if message.audio:
        file = message.audio
        return {"message_type": "audio", "caption": caption, "telegram_file_id": file.file_id, "telegram_file_unique_id": file.file_unique_id, "file_name": file.file_name, "mime_type": file.mime_type}
    return {"message_type": "text", "text": message.html_text or message.text or ""}


@broadcast_router.callback_query(F.data == "adm_broadcast")
async def admin_broadcast(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    text = header("📢 پیام همگانی حرفه‌ای")
    text += "می‌توانید متن، عکس، ویدیو، فایل، ویس و دکمه URL سفارشی ارسال کنید. قبل از ارسال نهایی، پیام پیش‌نمایش داده می‌شود و کد تأیید عددی لازم است."
    await edit_or_answer(callback, text, broadcast_home_kb())


@broadcast_router.callback_query(F.data == "adm_broadcast_new")
async def admin_broadcast_new(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    await edit_or_answer(callback, header("🎯 انتخاب گیرنده‌ها") + "پیام برای چه گروهی ارسال شود؟", target_kb())


@broadcast_router.callback_query(F.data.startswith("adm_bc_target:"))
async def admin_broadcast_target(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    scope = callback.data.split(":", 1)[1]
    await state.update_data(target_scope=scope)
    if scope == "manual":
        await state.set_state(BroadcastStates.waiting_manual_users)
        await edit_or_answer(callback, header("📝 لیست دستی") + "چت‌آیدی‌ها را با فاصله، ویرگول یا خط جدید بفرستید.", inline([[('❌ لغو', 'adm_broadcast'), ('👑 منوی ادمین', 'adm_home')]]))
        return
    await state.set_state(BroadcastStates.waiting_content)
    text = header("✉️ محتوای پیام")
    text += "حالا پیام همگانی را دقیقاً همان‌طور که باید ارسال شود بفرستید.\n\nمی‌تواند متن، عکس + کپشن، ویدیو + کپشن، فایل + کپشن، ویس یا صوت باشد."
    await edit_or_answer(callback, text, inline([[('❌ لغو', 'adm_broadcast'), ('👑 منوی ادمین', 'adm_home')]]))


@broadcast_router.message(BroadcastStates.waiting_manual_users)
async def admin_broadcast_manual_users(message: Message, state: FSMContext) -> None:
    if not message.from_user or not await is_admin(message.from_user.id):
        await message.answer("دسترسی ندارید.")
        return
    import re
    ids = []
    for part in re.split(r"[,\s]+", message.text or ""):
        if part.strip().isdigit():
            ids.append(int(part.strip()))
    if not ids:
        await message.answer("❌ هیچ چت‌آیدی معتبری پیدا نشد.")
        return
    await state.update_data(manual_user_ids=ids)
    await state.set_state(BroadcastStates.waiting_content)
    await message.answer(header("✉️ محتوای پیام") + f"تعداد گیرنده دستی: <b>{len(set(ids))}</b>\n\nحالا متن/عکس/فایل/ویدیو/ویس پیام را بفرستید.")


@broadcast_router.message(BroadcastStates.waiting_content)
async def admin_broadcast_content(message: Message, state: FSMContext) -> None:
    if not message.from_user or not await is_admin(message.from_user.id):
        await message.answer("دسترسی ندارید.")
        return
    payload = extract_message_payload(message)
    if payload.get("message_type") == "text" and not payload.get("text"):
        await message.answer("❌ پیام خالی است. متن یا فایل معتبر بفرستید.")
        return
    await state.update_data(content_payload=payload)
    await state.set_state(BroadcastStates.waiting_buttons)
    text = header("🔘 دکمه سفارشی")
    text += "اگر دکمه URL می‌خواهید، هر خط را با این فرمت بفرستید:\n\n<code>متن دکمه | https://example.com</code>\n\nبرای چند دکمه، هر دکمه در یک خط. اگر دکمه نمی‌خواهید فقط <code>-</code> بفرستید."
    await message.answer(text)


@broadcast_router.message(BroadcastStates.waiting_buttons)
async def admin_broadcast_buttons(message: Message, state: FSMContext) -> None:
    if not message.from_user or not await is_admin(message.from_user.id):
        await message.answer("دسترسی ندارید.")
        return
    buttons, error = parse_buttons(message.text or "")
    if error:
        await message.answer("❌ " + error)
        return
    data = await state.get_data()
    scope = str(data.get("target_scope") or "all")
    manual_user_ids = list(data.get("manual_user_ids") or [])
    content = dict(data.get("content_payload") or {})
    recipients = await resolve_recipients(scope, manual_user_ids)
    if not recipients:
        await message.answer("❌ هیچ گیرنده‌ای برای این کمپین پیدا نشد.")
        return
    draft = DraftPayload(
        created_by=message.from_user.id,
        target_scope=scope,
        buttons=tuple(buttons),
        manual_user_ids=tuple(manual_user_ids),
        **content,
    )
    campaign_id = await create_campaign(draft, recipients)
    await state.clear()
    text = header("📢 پیش‌نمایش کمپین", f"#{campaign_id}")
    text += f"گیرنده‌ها: <b>{len(recipients)}</b>\nگروه: <b>{h(TARGET_LABELS.get(scope, scope))}</b>\nنوع پیام: <b>{h(content.get('message_type'))}</b>\nدکمه‌ها: <b>{len(buttons)}</b>\n\nپیش‌نمایش پیام پایین ارسال می‌شود. اگر درست بود، کد تأیید عددی بگیر و ارسال را نهایی کن."
    await message.answer(text, reply_markup=confirm_kb(campaign_id))
    class Obj:  # lightweight object for preview
        pass
    preview = Obj()
    for k, v in content.items():
        setattr(preview, k, v)
    setattr(preview, "message_type", content.get("message_type", "text"))
    await send_campaign_message(message.bot, message.chat.id, preview, buttons_markup(buttons))


@broadcast_router.callback_query(F.data.startswith("adm_bc_confirm:"))
async def admin_broadcast_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    campaign_id = int(callback.data.split(":", 1)[1])
    confirmation = await create_confirmation(callback.from_user.id, "broadcast_send", {"campaign_id": campaign_id})
    await state.set_state(BroadcastStates.waiting_confirm_code)
    await state.update_data(campaign_id=campaign_id, confirmation_id=confirmation.confirmation_id)
    text = header("⚠️ تأیید عددی ارسال همگانی")
    text += f"برای ارسال نهایی کمپین <code>#{campaign_id}</code>، کد زیر را وارد کنید:\n\n<code>{confirmation.code}</code>\n\nاین کد ۵ دقیقه اعتبار دارد."
    await edit_or_answer(callback, text, inline([[('❌ لغو', 'adm_broadcast'), ('👑 منوی ادمین', 'adm_home')]]))


@broadcast_router.message(BroadcastStates.waiting_confirm_code)
async def admin_broadcast_send_confirmed(message: Message, state: FSMContext) -> None:
    if not message.from_user or not await is_admin(message.from_user.id):
        await message.answer("دسترسی ندارید.")
        return
    data = await state.get_data()
    campaign_id = int(data.get("campaign_id") or 0)
    confirmation_id = int(data.get("confirmation_id") or 0)
    verified = await verify_confirmation(confirmation_id, message.from_user.id, message.text or "", "broadcast_send")
    if not verified:
        await message.answer("❌ کد تأیید اشتباه یا منقضی شده است.")
        return
    await state.clear()
    await message.answer(header("⏳ ارسال شروع شد") + f"کمپین <code>#{campaign_id}</code> در حال ارسال است. لطفاً همین پیام را ملاک وضعیت نهایی ندانید؛ گزارش نهایی بعد از ارسال می‌آید.")
    result = await send_campaign(message.bot, campaign_id)
    await audit_log(message.from_user.id, "BROADCAST_SEND", "broadcast", campaign_id, str(result))
    stats = await campaign_stats(campaign_id)
    await message.answer(header("✅ ارسال همگانی تمام شد", f"#{campaign_id}") + f"موفق: <b>{result.get('sent', 0)}</b>\nناموفق: <b>{result.get('failed', 0)}</b>\nجزئیات وضعیت: <code>{h(stats)}</code>", reply_markup=broadcast_home_kb())
