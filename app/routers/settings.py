
"""Phase 2 editable text templates router."""

from __future__ import annotations

import html
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup

from app.services.admin_audit_service import audit_log
from app.services.text_template_service import get_template, list_templates, reset_template, update_template
from app.services.ticket_service import is_admin

settings_router = Router(name="phase2_settings")


class TextTemplateStates(StatesGroup):
    waiting_body = State()


def h(value: Any) -> str:
    return html.escape(str(value or ""))


def header(title: str, subtitle: str = "") -> str:
    sub = f"\n<code>{h(subtitle)}</code>" if subtitle else ""
    return f"<b>{h(title)}</b>{sub}\n\n"


def inline(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])


def text_home_kb() -> InlineKeyboardMarkup:
    return inline([
        [("📋 لیست متن‌ها", "adm_texts_list")],
        [("👑 منوی ادمین", "adm_home")],
    ])


def text_list_kb(rows_: list[Any]) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    for tpl in rows_[:40]:
        rows.append([(f"✏️ {tpl.key} | {tpl.title}", f"adm_text_view:{tpl.key}")])
    rows.append([("⬅️ بازگشت", "adm_texts"), ("👑 منوی ادمین", "adm_home")])
    return inline(rows)


def text_view_kb(key: str) -> InlineKeyboardMarkup:
    return inline([
        [("✏️ ویرایش متن", f"adm_text_edit:{key}"), ("↩️ متن پیش‌فرض", f"adm_text_reset:{key}")],
        [("⬅️ لیست متن‌ها", "adm_texts_list"), ("👑 منوی ادمین", "adm_home")],
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


@settings_router.callback_query(F.data == "adm_texts")
async def admin_texts(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    text = header("✏️ تغییر متن‌ها و پیام‌ها")
    text += "از این بخش می‌توانید جمله‌ها و پیام‌های اصلی ربات را بدون تغییر کد ویرایش کنید."
    await edit_or_answer(callback, text, text_home_kb())


@settings_router.callback_query(F.data == "adm_texts_list")
async def admin_texts_list(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    rows = await list_templates()
    text = header("📋 متن‌های قابل ویرایش")
    for tpl in rows:
        active = "✅" if tpl.is_active else "⛔"
        text += f"{active} <code>{h(tpl.key)}</code> — {h(tpl.title)} — <b>{h(tpl.group_name)}</b>\n"
    await edit_or_answer(callback, text, text_list_kb(rows))


@settings_router.callback_query(F.data.startswith("adm_text_view:"))
async def admin_text_view(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    key = callback.data.split(":", 1)[1]
    tpl = await get_template(key)
    if not tpl:
        await callback.answer("متن پیدا نشد.", show_alert=True)
        return
    body = tpl.body
    if len(body) > 2500:
        body = body[:2500] + "…"
    text = header("👁 پیش‌نمایش متن", tpl.key)
    text += f"عنوان: <b>{h(tpl.title)}</b>\nگروه: <b>{h(tpl.group_name)}</b>\nمتغیرهای مجاز: <code>{h(tpl.allowed_placeholders or '-')}</code>\n\n<b>متن فعلی:</b>\n{body}"
    await edit_or_answer(callback, text, text_view_kb(key))


@settings_router.callback_query(F.data.startswith("adm_text_edit:"))
async def admin_text_edit(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    key = callback.data.split(":", 1)[1]
    tpl = await get_template(key)
    if not tpl:
        await callback.answer("متن پیدا نشد.", show_alert=True)
        return
    await state.set_state(TextTemplateStates.waiting_body)
    await state.update_data(template_key=key)
    text = header("✏️ ویرایش متن", key)
    text += f"متن جدید را بفرستید.\n\nمتغیرهای مجاز: <code>{h(tpl.allowed_placeholders or '-')}</code>\n\nنکته: HTML تلگرام مثل <code>&lt;b&gt;</code> و <code>&lt;code&gt;</code> قابل استفاده است."
    await edit_or_answer(callback, text, inline([[('❌ لغو', f'adm_text_view:{key}'), ('👑 منوی ادمین', 'adm_home')]]))


@settings_router.message(TextTemplateStates.waiting_body)
async def admin_text_edit_finish(message: Message, state: FSMContext) -> None:
    if not message.from_user or not await is_admin(message.from_user.id):
        await message.answer("دسترسی ندارید.")
        return
    data = await state.get_data()
    key = str(data.get("template_key") or "")
    ok, result = await update_template(key, message.html_text or message.text or "", message.from_user.id)
    if not ok:
        await message.answer("❌ " + result)
        return
    await audit_log(message.from_user.id, "TEXT_TEMPLATE_UPDATE", "text_template", key, None)
    await state.clear()
    await message.answer(header("✅ ذخیره شد") + result, reply_markup=text_view_kb(key))


@settings_router.callback_query(F.data.startswith("adm_text_reset:"))
async def admin_text_reset(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    key = callback.data.split(":", 1)[1]
    ok, result = await reset_template(key, callback.from_user.id)
    if ok:
        await audit_log(callback.from_user.id, "TEXT_TEMPLATE_RESET", "text_template", key, None)
    await callback.answer(result, show_alert=True)
    tpl = await get_template(key)
    if tpl:
        body = tpl.body[:2500] + ("…" if len(tpl.body) > 2500 else "")
        text = header("👁 پیش‌نمایش متن", tpl.key)
        text += f"عنوان: <b>{h(tpl.title)}</b>\nگروه: <b>{h(tpl.group_name)}</b>\nمتغیرهای مجاز: <code>{h(tpl.allowed_placeholders or '-')}</code>\n\n<b>متن فعلی:</b>\n{body}"
        if callback.message:
            await callback.message.answer(text, reply_markup=text_view_kb(key), disable_web_page_preview=True)






