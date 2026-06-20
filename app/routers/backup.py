"""Phase 3 backup and restore admin router."""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import settings
from app.services.admin_audit_service import audit_log
from app.services.backup_service import create_complete_backup, inspect_backup_file
from app.services.confirmation_service import create_confirmation, verify_confirmation
from app.services.restore_service import restore_complete_backup
from app.services.pasarguard_checkpoint_service import reconcile_backup_with_pasarguard, render_reconcile_report
from app.services.scheduled_backup_service import disable_auto_backup, enable_auto_backup, get_auto_backup_config, run_auto_backup_once
from app.services.ticket_service import is_admin

backup_router = Router(name="phase3_backup")


class BackupStates(StatesGroup):
    waiting_restore_file = State()
    waiting_restore_confirm = State()
    waiting_auto_backup_interval = State()


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


def backup_home_kb() -> InlineKeyboardMarkup:
    return inline([
        [("📦 ساخت بک‌آپ کامل", "adm_backup_create")],
        [("🕒 بک‌آپ خودکار", "adm_auto_backup")],
        [("♻️ ریستور از بک‌آپ", "adm_restore_start")],
        [("👑 منوی ادمین", "adm_home")],
    ])


def auto_backup_kb(enabled: bool) -> InlineKeyboardMarkup:
    rows = [
        [("✅ فعال/تغییر فاصله", "adm_auto_backup_enable")],
        [("🧪 اجرای تست الان", "adm_auto_backup_run_now")],
    ]
    if enabled:
        rows.append([("⛔ غیرفعال کردن", "adm_auto_backup_disable")])
    rows.append([("⬅️ بک‌آپ/ریستور", "adm_backup"), ("👑 منوی ادمین", "adm_home")])
    return inline(rows)


@backup_router.callback_query(F.data == "adm_backup")
async def backup_home(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    text = header("🗄 بک‌آپ و ریستور کامل")
    text += (
        "بک‌آپ شامل کاربران، سرویس‌ها، سفارش‌ها، کیف پول، رفرال، تیکت‌ها، ادمین‌ها، تنظیمات، پلن‌ها، "
        "متن‌ها، کدهای تخفیف و گزارش استفاده است.\n\n"
        "اگر Pasarguard فعال باشد، actual_state و desired_state پنل هم داخل بک‌آپ ذخیره می‌شود و هنگام ریستور dry-run/reconcile قابل اجراست."
    )
    await edit_or_answer(callback, text, backup_home_kb())


@backup_router.callback_query(F.data == "adm_backup_create")
async def create_backup(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    await callback.answer("در حال ساخت بک‌آپ کامل…", show_alert=False)
    path, manifest = await create_complete_backup(admin_id=callback.from_user.id, bot=callback.bot)
    await audit_log(callback.from_user.id, "BACKUP_CREATE", "backup", Path(path).name, f"counts={manifest.get('counts')}")
    if callback.message:
        usage = manifest.get("usage", {})
        caption = (
            "📦 بک‌آپ کامل ساخته شد.\n"
            f"کاربران: {usage.get('users_total', 0)}\n"
            f"سرویس‌های فعال: {usage.get('services_active', 0)}\n"
            f"مصرف کل GB: {usage.get('data_used_gb', 0)} / {usage.get('data_total_gb', 0)}\n"
            f"فایل‌های تیکت: {manifest.get('ticket_files', {}).get('active_files_backed_up', 0)} موفق / "
            f"{manifest.get('ticket_files', {}).get('active_files_failed', 0)} ناموفق\n"
            f"Pasarguard: desired {manifest.get('pasarguard', {}).get('desired_users', 0)} user / "
            f"actual {manifest.get('pasarguard', {}).get('actual_users', 0)} user"
        )
        await callback.message.answer_document(FSInputFile(path), caption=caption)


@backup_router.callback_query(F.data == "adm_auto_backup")
async def auto_backup_home(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    config = await get_auto_backup_config()
    enabled = config.get("enabled") == "1"
    text = header("🕒 بک‌آپ خودکار")
    text += f"وضعیت: <b>{'فعال ✅' if enabled else 'غیرفعال ⛔'}</b>\n"
    text += f"فاصله: <b>هر {h(config.get('interval_hours') or '12')} ساعت</b>\n"
    text += f"ارسال به: <code>{h(config.get('chat_ids') or 'ADMIN_CHAT_IDS')}</code>\n"
    text += f"آخرین اجرا: <code>{h(config.get('last_run_at') or 'ندارد')}</code>\n"
    text += f"اجرای بعدی: <code>{h(config.get('next_run_at') or 'بعد از فعال‌سازی')}</code>\n\n"
    text += "در هر اجرای خودکار، بک‌آپ کامل ساخته و در تلگرام برای ادمین‌ها ارسال می‌شود. فایل‌های فعال تیکت هم از تلگرام دانلود و داخل ZIP ذخیره می‌شوند."
    await edit_or_answer(callback, text, auto_backup_kb(enabled))


@backup_router.callback_query(F.data == "adm_auto_backup_enable")
async def auto_backup_enable_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    await state.set_state(BackupStates.waiting_auto_backup_interval)
    text = header("✅ فعال‌سازی بک‌آپ خودکار")
    text += (
        "فاصله را به ساعت وارد کنید. مثال: <code>12</code> یعنی هر ۱۲ ساعت یک‌بار.\n\n"
        "اگر می‌خواهید علاوه بر خودتان برای چند چت‌آیدی دیگر هم ارسال شود، این فرمت را بفرستید:\n"
        "<code>12 | 123456789,987654321</code>\n\n"
        "اگر فقط عدد وارد کنید، بک‌آپ برای شما و ADMIN_CHAT_IDS ارسال می‌شود."
    )
    await edit_or_answer(callback, text, inline([[('❌ لغو', 'adm_auto_backup'), ('👑 منوی ادمین', 'adm_home')]]))


@backup_router.message(BackupStates.waiting_auto_backup_interval)
async def auto_backup_enable_finish(message: Message, state: FSMContext) -> None:
    if not message.from_user or not await is_admin(message.from_user.id):
        await message.answer("دسترسی ندارید.")
        return
    raw = (message.text or "").strip()
    parts = [p.strip() for p in raw.split("|", 1)]
    if not parts or not parts[0].isdigit():
        await message.answer("❌ فاصله باید عدد ساعت باشد. مثال: <code>12</code>")
        return
    interval = int(parts[0])
    chat_ids = sorted({message.from_user.id, *settings.bootstrap_super_admin_ids})
    if len(parts) > 1:
        parsed = []
        for item in parts[1].replace("\n", ",").split(","):
            item = item.strip()
            if item.lstrip("-").isdigit():
                parsed.append(int(item))
        if parsed:
            chat_ids = parsed
    config = await enable_auto_backup(interval, chat_ids)
    await audit_log(message.from_user.id, "AUTO_BACKUP_ENABLE", "backup", "auto", f"interval={interval}, chat_ids={chat_ids}")
    await state.clear()
    await message.answer(
        header("✅ بک‌آپ خودکار فعال شد")
        + f"از این به بعد هر <b>{h(config.get('interval_hours'))} ساعت</b> یک بک‌آپ کامل ساخته و ارسال می‌شود.\n"
        + f"اجرای بعدی: <code>{h(config.get('next_run_at'))}</code>",
        reply_markup=backup_home_kb(),
    )


@backup_router.callback_query(F.data == "adm_auto_backup_disable")
async def auto_backup_disable(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    await disable_auto_backup()
    await audit_log(callback.from_user.id, "AUTO_BACKUP_DISABLE", "backup", "auto", "")
    await edit_or_answer(callback, header("⛔ بک‌آپ خودکار غیرفعال شد"), backup_home_kb())


@backup_router.callback_query(F.data == "adm_auto_backup_run_now")
async def auto_backup_run_now(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    await callback.answer("در حال اجرای بک‌آپ خودکار تستی…", show_alert=False)
    path, manifest = await run_auto_backup_once(callback.bot, reason="manual_test")
    await audit_log(callback.from_user.id, "AUTO_BACKUP_RUN_NOW", "backup", Path(path).name, "manual test")
    await edit_or_answer(
        callback,
        header("✅ بک‌آپ تستی اجرا شد")
        + f"فایل: <code>{h(Path(path).name)}</code>\n"
        + f"فایل‌های تیکت داخل بک‌آپ: <b>{h(manifest.get('ticket_files', {}).get('active_files_backed_up', 0))}</b>",
        backup_home_kb(),
    )


@backup_router.callback_query(F.data == "adm_restore_start")
async def restore_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    await state.set_state(BackupStates.waiting_restore_file)
    text = header("♻️ ریستور از بک‌آپ")
    text += "فایل ZIP بک‌آپ را همینجا ارسال کنید. ابتدا بررسی و dry-run انجام می‌شود، بعد برای ریستور کامل کد تأیید عددی لازم است."
    await edit_or_answer(callback, text, inline([[('❌ لغو', 'adm_backup'), ('👑 منوی ادمین', 'adm_home')]]))


@backup_router.message(BackupStates.waiting_restore_file)
async def restore_file_received(message: Message, state: FSMContext) -> None:
    if not message.from_user or not await is_admin(message.from_user.id):
        await message.answer("دسترسی ندارید.")
        return
    if not message.document or not (message.document.file_name or "").endswith(".zip"):
        await message.answer("❌ لطفاً فایل ZIP بک‌آپ را ارسال کنید.")
        return
    restore_dir = Path("/tmp/howtosee_restore_uploads")
    restore_dir.mkdir(parents=True, exist_ok=True)
    dest = restore_dir / f"restore-{message.from_user.id}-{message.document.file_unique_id}.zip"
    file_info = await message.bot.get_file(message.document.file_id)
    await message.bot.download_file(file_info.file_path, destination=dest)
    try:
        manifest = inspect_backup_file(dest)
    except Exception as exc:
        await state.clear()
        await message.answer(f"❌ فایل بک‌آپ معتبر نیست:\n<code>{h(exc)}</code>")
        return
    if not manifest.get("checksum_ok"):
        await state.clear()
        await message.answer("❌ checksum فایل بک‌آپ معتبر نیست. ریستور متوقف شد.")
        return
    pg_report_text = ""
    try:
        pg_report = await reconcile_backup_with_pasarguard(dest, admin_id=message.from_user.id, dry_run=True)
        pg_report_text = "\n\n🔌 Dry-run ریستور Pasarguard:\n<pre>" + h(render_reconcile_report(pg_report)) + "</pre>"
    except Exception as exc:
        pg_report_text = "\n\n⚠️ Dry-run Pasarguard انجام نشد:\n<code>" + h(exc) + "</code>"
    confirmation = await create_confirmation(
        message.from_user.id,
        "RESTORE_FULL",
        {"path": str(dest), "file_name": manifest.get("file_name")},
        ttl_minutes=5,
    )
    await state.set_state(BackupStates.waiting_restore_confirm)
    await state.update_data(confirmation_id=confirmation.confirmation_id, restore_path=str(dest))
    counts = manifest.get("counts", {})
    usage = manifest.get("usage", {})
    text = header("⚠️ تأیید ریستور کامل")
    text += (
        f"فایل: <code>{h(manifest.get('file_name'))}</code>\n"
        f"نسخه بک‌آپ: <b>{h(manifest.get('backup_version'))}</b>\n"
        f"Checksum: <b>OK</b>\n"
        f"کاربران: <b>{h(usage.get('users_total', 0))}</b>\n"
        f"سرویس‌های فعال: <b>{h(usage.get('services_active', 0))}</b>\n"
        f"مصرف: <b>{h(usage.get('data_used_gb', 0))} / {h(usage.get('data_total_gb', 0))} GB</b>\n\n"
        "قبل از ریستور، یک بک‌آپ اضطراری از وضعیت فعلی ساخته می‌شود.\n"
        "اگر PASARGUARD_ENABLED=true باشد، reconcile پنل هم بعد از ریستور اجرا می‌شود؛ "
        "با PASARGUARD_DRY_RUN=true فقط گزارش می‌دهد و با false اعمال واقعی انجام می‌دهد.\n"
        f"برای تأیید نهایی این کد را وارد کنید:\n<code>{confirmation.code}</code>"
    )
    text += pg_report_text
    await message.answer(text)



@backup_router.message(BackupStates.waiting_restore_confirm)
async def restore_confirm(message: Message, state: FSMContext) -> None:
    if not message.from_user or not await is_admin(message.from_user.id):
        await message.answer("دسترسی ندارید.")
        return
    data = await state.get_data()
    confirmation_id = int(data.get("confirmation_id") or 0)
    payload = await verify_confirmation(confirmation_id, message.from_user.id, message.text or "", action="RESTORE_FULL")
    if not payload:
        await message.answer("❌ کد تأیید اشتباه یا منقضی شده است.")
        return
    restore_path = payload.get("path") or data.get("restore_path")
    try:
        result = await restore_complete_backup(restore_path, admin_id=message.from_user.id, make_emergency=True)
    except Exception as exc:
        await state.clear()
        await message.answer(f"❌ ریستور انجام نشد:\n<code>{h(exc)}</code>")
        return
    pg_restore_text = ""
    try:
        from app.config import settings as _settings
        pg_report = await reconcile_backup_with_pasarguard(restore_path, admin_id=message.from_user.id, dry_run=_settings.pasarguard_dry_run)
        pg_restore_text = "\n\n🔌 نتیجه Reconcile Pasarguard:\n<pre>" + h(render_reconcile_report(pg_report)) + "</pre>"
    except Exception as exc:
        pg_restore_text = "\n\n⚠️ Reconcile Pasarguard انجام نشد:\n<code>" + h(exc) + "</code>"
    await audit_log(message.from_user.id, "RESTORE_FULL", "backup", payload.get("file_name"), "restore completed")
    await state.clear()
    text = header("✅ ریستور کامل انجام شد")
    text += f"بک‌آپ اضطراری قبل از ریستور:\n<code>{h(result.get('emergency_backup'))}</code>\n\n"
    text += f"SQLite tables: <b>{len(result.get('sqlite', {}))}</b>\nPostgreSQL tables: <b>{len(result.get('postgres', {}))}</b>"
    text += pg_restore_text
    await message.answer(text, reply_markup=backup_home_kb())
    emergency = result.get("emergency_backup")
    if emergency and Path(str(emergency)).exists():
        await message.answer_document(FSInputFile(Path(str(emergency))), caption="🛟 بک‌آپ اضطراری قبل از ریستور")









