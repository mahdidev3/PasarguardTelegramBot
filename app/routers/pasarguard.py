"""Phase 4 Pasarguard admin router.

This checkpoint adds the safe connection layer and template/plan sync controls.
Buying real Pasarguard users is intentionally left for the next subphase after
these controls are tested.
"""

from __future__ import annotations

import html
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import settings
from app.services.admin_audit_service import audit_log
from app.services.confirmation_service import create_confirmation, verify_confirmation
from app.services.pasarguard_client import connection_info
from app.services.pasarguard_template_service import health_check, render_sync_report, sync_plan_templates
from app.services.ticket_service import is_admin

pasarguard_router = Router(name="phase4_pasarguard")


class PasarguardStates(StatesGroup):
    waiting_template_sync_confirm = State()


def h(value: Any) -> str:
    return html.escape(str(value or ""))


def header(title: str, subtitle: str = "") -> str:
    sub = f"\n<code>{h(subtitle)}</code>" if subtitle else ""
    return f"<b>{h(title)}</b>{sub}\n\n"


def inline(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])


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


def pg_home_kb() -> InlineKeyboardMarkup:
    return inline([
        [("📡 تست اتصال", "adm_pg_health")],
        [("🧪 Dry-run سینک Templateها", "adm_pg_template_dryrun")],
        [("✅ اعمال Sync Templateها", "adm_pg_template_apply_start")],
        [("👑 منوی ادمین", "adm_home")],
    ])


@pasarguard_router.callback_query(F.data == "adm_pasarguard")
async def pg_home(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    info = connection_info()
    text = header("🔌 Pasarguard")
    text += f"وضعیت env: <b>{'فعال ✅' if info.enabled else 'غیرفعال ⛔'}</b>\n"
    text += f"Base URL: <code>{h(info.base_url)}</code>\n"
    text += f"Dry-run پیش‌فرض: <b>{'روشن ✅' if info.dry_run else 'خاموش ⚠️'}</b>\n"
    text += f"Marker بات: <code>{h(info.managed_prefix)}</code>\n"
    text += f"Group IDs برای template: <code>{h(','.join(map(str, info.group_ids)) or 'تنظیم نشده')}</code>\n\n"
    text += "در این checkpoint فقط client، health check و sync پلن‌های بات با User Templateهای Pasarguard فعال شده است. ساخت user واقعی بعد از تست این بخش وصل می‌شود."
    await edit_or_answer(callback, text, pg_home_kb())


@pasarguard_router.callback_query(F.data == "adm_pg_health")
async def pg_health(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    await callback.answer("در حال تست اتصال Pasarguard…", show_alert=False)
    ok, message, details = await health_check()
    await audit_log(callback.from_user.id, "PASARGUARD_HEALTH", "pasarguard", "health", f"ok={ok}; message={message}")
    text = header("📡 نتیجه تست اتصال Pasarguard")
    text += f"نتیجه: <b>{'موفق ✅' if ok else 'ناموفق ❌'}</b>\n"
    text += f"پیام: <code>{h(message)}</code>\n"
    if details and details.get("admin"):
        admin = details.get("admin")
        text += f"ادمین پنل: <code>{h(admin.get('username') if isinstance(admin, dict) else admin)}</code>\n"
    await edit_or_answer(callback, text, pg_home_kb())


@pasarguard_router.callback_query(F.data == "adm_pg_template_dryrun")
async def pg_template_dryrun(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    await callback.answer("در حال اجرای dry-run template sync…", show_alert=False)
    report = await sync_plan_templates(callback.from_user.id, dry_run=True)
    await audit_log(callback.from_user.id, "PASARGUARD_TEMPLATE_DRYRUN", "pasarguard", "templates", f"actions={report.action_count}; errors={report.errors}")
    text = header("🧪 Dry-run سینک Templateها") + f"<pre>{h(render_sync_report(report))}</pre>"
    await edit_or_answer(callback, text, pg_home_kb())


@pasarguard_router.callback_query(F.data == "adm_pg_template_apply_start")
async def pg_template_apply_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    pending = await create_confirmation(
        callback.from_user.id,
        "PASARGUARD_TEMPLATE_SYNC_APPLY",
        {"dry_run": False},
        ttl_minutes=5,
    )
    await state.set_state(PasarguardStates.waiting_template_sync_confirm)
    await state.update_data(confirmation_id=pending.confirmation_id)
    text = header("⚠️ تأیید اعمال Sync Templateها")
    text += (
        "این عملیات می‌تواند در پنل Pasarguard، User Template بسازد یا templateهای مدیریت‌شده توسط بات را ویرایش کند.\n\n"
        "قبل از این کار بهتر است dry-run را اجرا کرده باشید.\n\n"
        f"برای تأیید، کد زیر را وارد کنید:\n<code>{pending.code}</code>\n\n"
        "اگر منصرف شدید، روی لغو بزنید."
    )
    await edit_or_answer(callback, text, inline([[('❌ لغو', 'adm_pasarguard'), ('👑 منوی ادمین', 'adm_home')]]))


@pasarguard_router.message(PasarguardStates.waiting_template_sync_confirm)
async def pg_template_apply_finish(message: Message, state: FSMContext) -> None:
    if not message.from_user or not await is_admin(message.from_user.id):
        await message.answer("دسترسی ندارید.")
        return
    data = await state.get_data()
    confirmation_id = int(data.get("confirmation_id") or 0)
    payload = await verify_confirmation(
        confirmation_id,
        message.from_user.id,
        message.text or "",
        action="PASARGUARD_TEMPLATE_SYNC_APPLY",
    )
    if payload is None:
        await message.answer("❌ کد تأیید معتبر نیست یا منقضی شده است.")
        return
    await state.clear()
    await message.answer("⏳ در حال اعمال sync templateها روی Pasarguard…")
    report = await sync_plan_templates(message.from_user.id, dry_run=False)
    await audit_log(message.from_user.id, "PASARGUARD_TEMPLATE_APPLY", "pasarguard", "templates", f"actions={report.action_count}; failed={report.failed_count}")
    await message.answer(header("✅ نتیجه Sync Templateها") + f"<pre>{h(render_sync_report(report))}</pre>", reply_markup=pg_home_kb())
