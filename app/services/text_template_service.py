"""Editable text templates for Phase 2."""

from __future__ import annotations

import html
import re
from typing import Any

from sqlalchemy import select

from app.config import settings
from app.database import session_scope
from app.models import TextTemplate, TextTemplateVersion

_TEMPLATE_CACHE: dict[str, str] = {}

DEFAULT_TEMPLATES: dict[str, dict[str, str]] = {
    "welcome.body": {
        "title": "پیام خوشامدگویی",
        "group": "user",
        "placeholders": "first_name,first_name_part,brand_name,channel_link,channel_username",
        "body": "🌍 <b>{brand_name}</b>\n<code>See beyond limits</code>\n\nسلام{first_name_part} 👋\nبه ربات رسمی <b>{brand_name}</b> خوش آمدید.\n\nاینجا می‌تونید سرویس VPN بخرید، سرویس رایگان بگیرید، سرویس‌هاتون رو مدیریت کنید و با معرفی دوستان اعتبار کیف پول بگیرید.\n\n🔹 سرورهای پرسرعت و پایدار\n🔹 ترافیک امن و اختصاصی\n🔹 مناسب ایرانسل، همراه اول و مخابرات\n\n📣 کانال رسمی: <a href=\"{channel_link}\">@{channel_username}</a>",
    },
    "menu.main": {
        "title": "متن منوی اصلی",
        "group": "user",
        "placeholders": "",
        "body": "<b>🏠 منوی اصلی</b>\n\nیکی از گزینه‌های پایین را انتخاب کنید.",
    },
    "buy.intro": {
        "title": "متن شروع خرید",
        "group": "purchase",
        "placeholders": "",
        "body": "<b>🛒 خرید سرویس</b>\n<code>نوع سرویس را انتخاب کنید</code>\n\nپلن‌های آماده برای شروع سریع مناسب‌اند.\nپلن‌های سه‌ماهه برای استفاده پایدار و اقتصادی‌تر پیشنهاد می‌شوند.",
    },
    "free.intro": {
        "title": "متن سرویس رایگان",
        "group": "purchase",
        "placeholders": "",
        "body": "<b>🎁 سرویس رایگان</b>\n<code>اول نوع سرویس را انتخاب کنید</code>\n\nبرای تست کیفیت، یک سرویس رایگان محدود می‌توانید فعال کنید.\nاین سرویس فقط یک‌بار برای هر حساب قابل دریافت است.",
    },
    "ticket.new_help": {
        "title": "راهنمای ثبت تیکت",
        "group": "ticket",
        "placeholders": "",
        "body": "عنوان تیکت را کوتاه وارد کنید.\n\nدر مرحله بعد متن کامل مشکل را بفرستید. اگر می‌خواهید عکس، ویدیو، فایل، ویس یا توضیح بفرستید، تا حد امکان همه را در همان یک پیام و همراه کپشن ارسال کنید تا تیکت کامل ثبت شود.",
    },
    "bot.locked": {
        "title": "پیام قفل بودن بات",
        "group": "system",
        "placeholders": "",
        "body": "🛠 ربات موقتاً در حال بروزرسانی است. لطفاً کمی بعد دوباره تلاش کنید.",
    },
}


def _safe_format(template: str, **values: Any) -> str:
    class SafeDict(dict):
        def __missing__(self, key: str) -> str:
            return "{" + key + "}"
    return template.format_map(SafeDict({k: str(v) for k, v in values.items()}))


async def seed_text_templates() -> None:
    async with session_scope() as session:
        for key, item in DEFAULT_TEMPLATES.items():
            existing = (await session.execute(select(TextTemplate).where(TextTemplate.key == key))).scalar_one_or_none()
            if existing is None:
                session.add(
                    TextTemplate(
                        key=key,
                        title=item["title"],
                        body=item["body"],
                        group_name=item["group"],
                        allowed_placeholders=item.get("placeholders") or "",
                        is_active=True,
                    )
                )
    await refresh_template_cache()


async def refresh_template_cache() -> None:
    global _TEMPLATE_CACHE
    async with session_scope() as session:
        rows = list((await session.execute(select(TextTemplate).where(TextTemplate.is_active.is_(True)))).scalars().all())
    _TEMPLATE_CACHE = {row.key: row.body for row in rows}


def render_template_sync(key: str, fallback: str, **values: Any) -> str:
    template = _TEMPLATE_CACHE.get(key, fallback)
    return _safe_format(template, **values)


async def list_templates(group: str | None = None) -> list[TextTemplate]:
    async with session_scope() as session:
        stmt = select(TextTemplate).order_by(TextTemplate.group_name, TextTemplate.key)
        if group:
            stmt = stmt.where(TextTemplate.group_name == group)
        return list((await session.execute(stmt)).scalars().all())


async def get_template(key: str) -> TextTemplate | None:
    async with session_scope() as session:
        return (await session.execute(select(TextTemplate).where(TextTemplate.key == key))).scalar_one_or_none()


def validate_template_body(body: str, allowed_placeholders: str | None) -> tuple[bool, str]:
    allowed = {x.strip() for x in (allowed_placeholders or "").split(",") if x.strip()}
    if not allowed:
        return True, ""
    found = set(re.findall(r"{([a-zA-Z0-9_]+)}", body or ""))
    bad = sorted(found - allowed)
    if bad:
        return False, "placeholder نامعتبر: " + ", ".join(bad)
    return True, ""


async def update_template(key: str, body: str, admin_id: int) -> tuple[bool, str]:
    async with session_scope() as session:
        tpl = (await session.execute(select(TextTemplate).where(TextTemplate.key == key))).scalar_one_or_none()
        if tpl is None:
            return False, "این متن پیدا نشد."
        ok, error = validate_template_body(body, tpl.allowed_placeholders)
        if not ok:
            return False, error
        session.add(TextTemplateVersion(template_key=key, body=tpl.body, changed_by=admin_id, change_note="admin edit"))
        tpl.body = body
        tpl.updated_by = admin_id
    await refresh_template_cache()
    return True, "متن با موفقیت ذخیره شد."


async def reset_template(key: str, admin_id: int) -> tuple[bool, str]:
    default = DEFAULT_TEMPLATES.get(key)
    if not default:
        return False, "برای این متن، پیش‌فرض داخلی تعریف نشده است."
    return await update_template(key, default["body"], admin_id)


