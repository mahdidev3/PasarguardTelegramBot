"""Phase 3 admin reports router: CSV/XLSX exports and usage report."""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from aiogram import F, Router
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from app.services.admin_audit_service import audit_log
from app.services.export_service import REPORT_SPECS, build_all_reports_zip, build_report_file
from app.services.ticket_service import is_admin

reports_router = Router(name="phase3_reports")


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


def reports_home_kb() -> InlineKeyboardMarkup:
    return inline([
        [("📊 گزارش استفاده", "adm_report:usage"), ("👥 کاربران", "adm_report:users")],
        [("📦 سرویس‌ها", "adm_report:services"), ("🧾 سفارش‌ها", "adm_report:orders")],
        [("💰 کیف پول", "adm_report:wallet"), ("💳 رسیدها", "adm_report:payment_receipts")],
        [("📈 خلاصه مالی", "adm_report:finance_summary"), ("📒 گردش مالی", "adm_report:financial_ledger")],
        [("💳 کارت‌ها", "adm_report:payment_cards"), ("💎 رفرال", "adm_report:referrals")],
        [("🎫 تیکت‌ها", "adm_report:tickets"), ("💬 پیام‌های تیکت", "adm_report:ticket_messages")],
        [("🎟 کدهای تخفیف", "adm_report:coupons"), ("📦 پلن‌ها", "adm_report:plans")],
        [("📢 پیام همگانی", "adm_report:broadcasts"), ("📜 لاگ ادمین", "adm_report:admin_logs")],
        [("🗜 همه گزارش‌ها CSV", "adm_report_all_csv")],
        [("👑 منوی ادمین", "adm_home")],
    ])


def report_format_kb(report_key: str) -> InlineKeyboardMarkup:
    return inline([
        [("📄 CSV", f"adm_report_file:{report_key}:csv"), ("📊 Excel", f"adm_report_file:{report_key}:xlsx")],
        [("⬅️ گزارش‌ها", "adm_reports"), ("👑 منوی ادمین", "adm_home")],
    ])


@reports_router.callback_query(F.data == "adm_reports")
async def admin_reports(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    text = header("📊 گزارش‌ها و خروجی فایل")
    text += "از این بخش می‌توانید خروجی CSV یا Excel بگیرید. گزارش‌ها در مرحله مهاجرت، هم SQLite legacy و هم PostgreSQL جدید را پوشش می‌دهند."
    await edit_or_answer(callback, text, reports_home_kb())


@reports_router.callback_query(F.data.startswith("adm_report:"))
async def choose_report(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    key = callback.data.split(":", 1)[1]
    spec = REPORT_SPECS.get(key)
    if not spec:
        await callback.answer("گزارش پیدا نشد.", show_alert=True)
        return
    text = header("📤 انتخاب فرمت خروجی", spec.title)
    text += "فرمت موردنظر را انتخاب کنید. فایل ساخته‌شده همینجا در تلگرام ارسال می‌شود."
    await edit_or_answer(callback, text, report_format_kb(key))


@reports_router.callback_query(F.data.startswith("adm_report_file:"))
async def build_report(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    _, key, fmt = callback.data.split(":")
    if key not in REPORT_SPECS or fmt not in {"csv", "xlsx"}:
        await callback.answer("درخواست نامعتبر است.", show_alert=True)
        return
    await callback.answer("در حال ساخت گزارش…", show_alert=False)
    path, count, title = await build_report_file(key, fmt)
    await audit_log(callback.from_user.id, "REPORT_EXPORT", "report", key, f"format={fmt}, rows={count}")
    if callback.message:
        caption = f"📊 گزارش: {h(title)}\nتعداد رکورد: {count}\nفرمت: {fmt.upper()}"
        await callback.message.answer_document(FSInputFile(path), caption=caption)


@reports_router.callback_query(F.data == "adm_report_all_csv")
async def build_all_reports(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    await callback.answer("در حال ساخت بسته گزارش‌ها…", show_alert=False)
    path = await build_all_reports_zip()
    await audit_log(callback.from_user.id, "REPORT_EXPORT_ALL", "report", "all", "format=zip/csv")
    if callback.message:
        await callback.message.answer_document(FSInputFile(Path(path)), caption="🗜 همه گزارش‌ها به صورت CSV داخل فایل ZIP")









