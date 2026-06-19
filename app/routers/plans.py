"""Phase 2 admin plan management router."""

from __future__ import annotations

import html
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup

from app.services.admin_audit_service import audit_log
from app.services.plan_service import (
    list_addons,
    list_categories,
    list_plans,
    set_category_active,
    set_plan_active,
    sync_legacy_catalog_from_db,
    upsert_addon_from_line,
    upsert_category_from_line,
    upsert_plan_from_line,
)
from app.services.ticket_service import is_admin
from app.utils.line_parser import pipe_escape_hint

plans_router = Router(name="phase2_plans")


class PlanStates(StatesGroup):
    waiting_plan_line = State()
    waiting_addon_line = State()
    waiting_category_line = State()


def h(value: Any) -> str:
    return html.escape(str(value or ""))


def header(title: str, subtitle: str = "") -> str:
    sub = f"\n<code>{h(subtitle)}</code>" if subtitle else ""
    return f"<b>{h(title)}</b>{sub}\n\n"


def inline(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])


def plans_home_kb() -> InlineKeyboardMarkup:
    return inline([
        [("📋 لیست پلن‌ها", "adm_plans_list"), ("➕ ساخت/ویرایش پلن", "adm_plans_add")],
        [("🗂 دسته‌بندی‌ها", "adm_categories_list"), ("➕ ساخت/ویرایش دسته", "adm_categories_add")],
        [("📈 بسته‌های حجم", "adm_addons_list"), ("➕ ساخت/ویرایش حجم", "adm_addons_add")],
        [("🔄 همگام‌سازی با خرید", "adm_plans_sync")],
        [("👑 منوی ادمین", "adm_home")],
    ])


def plan_list_kb(plans: list[Any]) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    for p in plans[:40]:
        icon = "✅" if p.is_active else "⛔"
        rows.append([(f"{icon} {p.key} | {p.title}", f"adm_plan_view:{p.key}")])
    rows.append([("⬅️ بازگشت", "adm_plans"), ("👑 منوی ادمین", "adm_home")])
    return inline(rows)


def plan_view_kb(key: str, active: bool) -> InlineKeyboardMarkup:
    toggle = ("⛔ غیرفعال کردن", f"adm_plan_active:{key}:0") if active else ("✅ فعال کردن", f"adm_plan_active:{key}:1")
    return inline([
        [toggle],
        [("✏️ ویرایش با خط جدید", "adm_plans_add")],
        [("⬅️ لیست پلن‌ها", "adm_plans_list"), ("👑 منوی ادمین", "adm_home")],
    ])


def category_list_kb(categories: list[Any]) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    for c in categories[:40]:
        icon = "✅" if c.is_active else "⛔"
        rows.append([(f"{icon} {c.key} | {c.title}", f"adm_category_view:{c.key}")])
    rows.append([("⬅️ بازگشت", "adm_plans"), ("👑 منوی ادمین", "adm_home")])
    return inline(rows)


def category_view_kb(key: str, active: bool) -> InlineKeyboardMarkup:
    toggle = ("⛔ غیرفعال کردن", f"adm_category_active:{key}:0") if active else ("✅ فعال کردن", f"adm_category_active:{key}:1")
    return inline([
        [toggle],
        [("✏️ ویرایش با خط جدید", "adm_categories_add")],
        [("⬅️ لیست دسته‌ها", "adm_categories_list"), ("👑 منوی ادمین", "adm_home")],
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


async def sync_legacy() -> None:
    try:
        import app.legacy_bot as legacy_bot
        await sync_legacy_catalog_from_db(legacy_bot)
    except Exception:
        pass


@plans_router.callback_query(F.data == "adm_plans")
async def admin_plans(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    text = header("📦 مدیریت کامل پلن‌ها") + "پلن‌ها از دیتابیس خوانده می‌شوند و بعد از همگام‌سازی، روی بخش خرید ربات هم اثر می‌گذارند."
    await edit_or_answer(callback, text, plans_home_kb())


@plans_router.callback_query(F.data == "adm_plans_list")
async def admin_plans_list(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    plans = await list_plans(active_only=False)
    text = header("📋 لیست پلن‌ها")
    if not plans:
        text += "هنوز پلنی در دیتابیس نیست."
    else:
        for p in plans[:30]:
            icon = "✅" if p.is_active else "⛔"
            text += f"{icon} <code>{h(p.key)}</code> | <b>{h(p.title)}</b> | {p.data_gb:g}GB | {p.days} روز | {p.price:,} تومان | {h(p.category)}\n"
    await edit_or_answer(callback, text, plan_list_kb(plans))


@plans_router.callback_query(F.data.startswith("adm_plan_view:"))
async def admin_plan_view(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    key = callback.data.split(":", 1)[1]
    plans = [p for p in await list_plans(active_only=False) if p.key == key]
    if not plans:
        await callback.answer("پلن پیدا نشد.", show_alert=True)
        return
    p = plans[0]
    text = header("📦 جزئیات پلن", p.key)
    text += f"عنوان: <b>{h(p.title)}</b>\nحجم: <b>{p.data_gb:g} GB</b>\nزمان: <b>{p.days} روز</b>\nقیمت: <b>{p.price:,} تومان</b>\nدسته: <code>{h(p.category)}</code>\nبرچسب: <b>{h(p.badge)}</b>\nوضعیت: <b>{'فعال' if p.is_active else 'غیرفعال'}</b>"
    await edit_or_answer(callback, text, plan_view_kb(p.key, bool(p.is_active)))


@plans_router.callback_query(F.data.startswith("adm_plan_active:"))
async def admin_plan_active(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    _, key, active_s = callback.data.split(":")
    ok = await set_plan_active(key, active_s == "1", callback.from_user.id)
    if ok:
        await audit_log(callback.from_user.id, "PLAN_SET_ACTIVE", "plan", key, f"active={active_s}")
        await sync_legacy()
        await callback.answer("وضعیت پلن تغییر کرد.", show_alert=True)
    else:
        await callback.answer("پلن پیدا نشد.", show_alert=True)
    await admin_plans_list(callback)


@plans_router.callback_query(F.data == "adm_plans_add")
async def admin_plans_add(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    await state.set_state(PlanStates.waiting_plan_line)
    text = header("➕ ساخت/ویرایش پلن")
    text += "فرمت را دقیقاً این‌طور بفرستید:\n\n<code>key | title | data_gb | days | price | category | badge</code>\n\nنمونه:\n<code>m_80 | ۸۰ گیگابایت یک‌ماهه | 80 | 31 | 590000 | monthly | VIP</code>"
    await edit_or_answer(callback, text, inline([[('❌ لغو', 'adm_plans'), ('👑 منوی ادمین', 'adm_home')]]))


@plans_router.message(PlanStates.waiting_plan_line)
async def admin_plans_add_finish(message: Message, state: FSMContext) -> None:
    if not message.from_user or not await is_admin(message.from_user.id):
        await message.answer("دسترسی ندارید.")
        return
    ok, result = await upsert_plan_from_line(message.text or "", message.from_user.id)
    if not ok:
        await message.answer("❌ " + result)
        return
    await state.clear()
    await audit_log(message.from_user.id, "PLAN_UPSERT", "plan", None, message.text)
    await sync_legacy()
    await message.answer(header("✅ ذخیره شد") + result, reply_markup=plans_home_kb())


@plans_router.callback_query(F.data == "adm_categories_list")
async def admin_categories_list(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    categories = await list_categories(active_only=False)
    plans = await list_plans(active_only=False)
    counts: dict[str, int] = {}
    for p in plans:
        counts[p.category] = counts.get(p.category, 0) + 1
    text = header("🗂 دسته‌بندی‌های پلن")
    if not categories:
        text += "هنوز دسته‌ای در دیتابیس نیست."
    else:
        for c in categories:
            icon = "✅" if c.is_active else "⛔"
            text += f"{icon} <code>{h(c.key)}</code> | <b>{h(c.title)}</b> | sort={c.sort_order} | پلن‌ها: {counts.get(c.key, 0)}\n"
            if c.description:
                text += f"   <i>{h(c.description)}</i>\n"
    await edit_or_answer(callback, text, category_list_kb(categories))


@plans_router.callback_query(F.data.startswith("adm_category_view:"))
async def admin_category_view(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    key = callback.data.split(":", 1)[1]
    cats = [c for c in await list_categories(active_only=False) if c.key == key]
    if not cats:
        await callback.answer("دسته پیدا نشد.", show_alert=True)
        return
    c = cats[0]
    text = header("🗂 جزئیات دسته", c.key)
    text += f"عنوان: <b>{h(c.title)}</b>\nتوضیح: <b>{h(c.description)}</b>\nترتیب: <code>{c.sort_order}</code>\nوضعیت: <b>{'فعال' if c.is_active else 'غیرفعال'}</b>"
    await edit_or_answer(callback, text, category_view_kb(c.key, bool(c.is_active)))


@plans_router.callback_query(F.data.startswith("adm_category_active:"))
async def admin_category_active(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    _, key, active_s = callback.data.split(":")
    ok = await set_category_active(key, active_s == "1", callback.from_user.id)
    if ok:
        await audit_log(callback.from_user.id, "CATEGORY_SET_ACTIVE", "plan_category", key, f"active={active_s}")
        await sync_legacy()
        await callback.answer("وضعیت دسته تغییر کرد.", show_alert=True)
    else:
        await callback.answer("دسته پیدا نشد.", show_alert=True)
    await admin_categories_list(callback)


@plans_router.callback_query(F.data == "adm_categories_add")
async def admin_categories_add(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    await state.set_state(PlanStates.waiting_category_line)
    text = header("➕ ساخت/ویرایش دسته‌بندی پلن")
    text += "فرمت را دقیقاً این‌طور بفرستید:\n\n<code>key | title | description | sort_order | active</code>\n\nنمونه:\n<code>semiannual | 🏆 پلن‌های شش‌ماهه | اقتصادی برای استفاده طولانی | 30 | 1</code>"
    await edit_or_answer(callback, text, inline([[("❌ لغو", "adm_plans"), ("👑 منوی ادمین", "adm_home")]]))


@plans_router.message(PlanStates.waiting_category_line)
async def admin_categories_add_finish(message: Message, state: FSMContext) -> None:
    if not message.from_user or not await is_admin(message.from_user.id):
        await message.answer("دسترسی ندارید.")
        return
    ok, result = await upsert_category_from_line(message.text or "", message.from_user.id)
    if not ok:
        await message.answer("❌ " + result)
        return
    await state.clear()
    await audit_log(message.from_user.id, "CATEGORY_UPSERT", "plan_category", None, message.text)
    await sync_legacy()
    await message.answer(header("✅ ذخیره شد") + result, reply_markup=plans_home_kb())


@plans_router.callback_query(F.data == "adm_addons_list")
async def admin_addons_list(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    addons = await list_addons(active_only=False)
    text = header("📈 بسته‌های افزایش حجم")
    if not addons:
        text += "بسته‌ای ثبت نشده است."
    else:
        for a in addons:
            icon = "✅" if a.is_active else "⛔"
            text += f"{icon} <code>{h(a.key)}</code> | <b>{h(a.title)}</b> | {a.data_gb:g}GB | {a.price:,} تومان | {h(a.badge)}\n"
    await edit_or_answer(callback, text, plans_home_kb())


@plans_router.callback_query(F.data == "adm_addons_add")
async def admin_addons_add(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    await state.set_state(PlanStates.waiting_addon_line)
    text = header("➕ ساخت/ویرایش بسته حجم")
    text += "فرمت:\n\n<code>key | title | data_gb | price | badge</code>\n\nنمونه:\n<code>add_100 | ۱۰۰ گیگابایت حجم اضافه | 100 | 499000 | اقتصادی</code>"
    await edit_or_answer(callback, text, inline([[('❌ لغو', 'adm_plans'), ('👑 منوی ادمین', 'adm_home')]]))


@plans_router.message(PlanStates.waiting_addon_line)
async def admin_addons_add_finish(message: Message, state: FSMContext) -> None:
    if not message.from_user or not await is_admin(message.from_user.id):
        await message.answer("دسترسی ندارید.")
        return
    ok, result = await upsert_addon_from_line(message.text or "", message.from_user.id)
    if not ok:
        await message.answer("❌ " + result)
        return
    await state.clear()
    await audit_log(message.from_user.id, "ADDON_UPSERT", "addon", None, message.text)
    await sync_legacy()
    await message.answer(header("✅ ذخیره شد") + result, reply_markup=plans_home_kb())


@plans_router.callback_query(F.data == "adm_plans_sync")
async def admin_plans_sync(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    await sync_legacy()
    await callback.answer("کاتالوگ خرید با دیتابیس همگام شد.", show_alert=True)
    await admin_plans(callback)



