"""Broadcast campaign service for Phase 2."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select

from app.database import session_scope
from app.models import BroadcastButton, BroadcastCampaign, BroadcastEvent, BroadcastRecipient, Service, User

TEHRAN_TZ = timezone(timedelta(hours=3, minutes=30))

TARGET_LABELS = {
    "all": "همه کاربران",
    "active": "کاربران فعال",
    "buyers": "کاربران خریدار",
    "no_purchase": "کاربران بدون خرید",
    "active_services": "کاربران دارای سرویس فعال",
    "manual": "لیست دستی",
}


@dataclass(frozen=True)
class DraftPayload:
    created_by: int
    target_scope: str
    message_type: str
    text: str | None = None
    caption: str | None = None
    telegram_file_id: str | None = None
    telegram_file_unique_id: str | None = None
    file_name: str | None = None
    mime_type: str | None = None
    buttons: tuple[tuple[str, str], ...] = ()
    manual_user_ids: tuple[int, ...] = ()


def parse_buttons(raw: str) -> tuple[list[tuple[str, str]], str]:
    raw = (raw or "").strip()
    if not raw or raw == "-":
        return [], ""
    buttons: list[tuple[str, str]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|", 1)]
        if len(parts) != 2 or not parts[0] or not parts[1].startswith(("http://", "https://", "tg://")):
            return [], "فرمت دکمه اشتباه است. نمونه: خرید سرویس | https://t.me/example"
        buttons.append((parts[0][:64], parts[1]))
    return buttons, ""


def buttons_markup(buttons: list[tuple[str, str]] | tuple[tuple[str, str], ...]) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, url=url)] for text, url in buttons]
    )


async def resolve_recipients(scope: str, manual_user_ids: list[int] | tuple[int, ...] | None = None) -> list[int]:
    if scope == "manual":
        return sorted({int(x) for x in (manual_user_ids or []) if int(x) > 0})
    async with session_scope() as session:
        if scope == "active_services":
            result = await session.execute(select(Service.user_telegram_id).where(Service.status == "active"))
            return sorted({int(x) for x in result.scalars().all()})
        stmt = select(User.telegram_id).where(User.status != "deleted")
        if scope == "active":
            stmt = stmt.where(User.status == "active")
        elif scope == "buyers":
            stmt = stmt.where(User.first_purchase_done.is_(True))
        elif scope == "no_purchase":
            stmt = stmt.where(User.first_purchase_done.is_(False))
        result = await session.execute(stmt)
        return sorted({int(x) for x in result.scalars().all()})


async def create_campaign(payload: DraftPayload, recipient_ids: list[int]) -> int:
    async with session_scope() as session:
        campaign = BroadcastCampaign(
            created_by=payload.created_by,
            title=f"Broadcast {datetime.now(TEHRAN_TZ).strftime('%Y-%m-%d %H:%M')}",
            target_scope=payload.target_scope,
            message_type=payload.message_type,
            text=payload.text,
            caption=payload.caption,
            telegram_file_id=payload.telegram_file_id,
            telegram_file_unique_id=payload.telegram_file_unique_id,
            file_name=payload.file_name,
            mime_type=payload.mime_type,
            status="draft",
            recipient_count=len(recipient_ids),
        )
        session.add(campaign)
        await session.flush()
        for index, (text, url) in enumerate(payload.buttons):
            session.add(BroadcastButton(campaign_id=campaign.id, text=text, url=url, row_index=index, col_index=0))
        for user_id in recipient_ids:
            session.add(BroadcastRecipient(campaign_id=campaign.id, user_telegram_id=user_id, status="pending"))
        session.add(BroadcastEvent(campaign_id=campaign.id, event_type="created", details=f"target={payload.target_scope}, recipients={len(recipient_ids)}"))
        await session.flush()
        return int(campaign.id)


async def get_campaign(campaign_id: int) -> tuple[BroadcastCampaign | None, list[BroadcastButton]]:
    async with session_scope() as session:
        campaign = await session.get(BroadcastCampaign, campaign_id)
        buttons = []
        if campaign:
            buttons = list((await session.execute(select(BroadcastButton).where(BroadcastButton.campaign_id == campaign_id).order_by(BroadcastButton.row_index))).scalars().all())
        return campaign, buttons


async def campaign_stats(campaign_id: int) -> dict[str, int]:
    async with session_scope() as session:
        rows = (await session.execute(select(BroadcastRecipient.status, func.count()).where(BroadcastRecipient.campaign_id == campaign_id).group_by(BroadcastRecipient.status))).all()
    return {str(status): int(count) for status, count in rows}


async def send_campaign(bot: Bot, campaign_id: int, delay_seconds: float = 0.04) -> dict[str, int]:
    async with session_scope() as session:
        campaign = await session.get(BroadcastCampaign, campaign_id)
        if campaign is None:
            return {"sent": 0, "failed": 0}
        buttons = list((await session.execute(select(BroadcastButton).where(BroadcastButton.campaign_id == campaign_id).order_by(BroadcastButton.row_index))).scalars().all())
        recipients = list((await session.execute(select(BroadcastRecipient).where(BroadcastRecipient.campaign_id == campaign_id, BroadcastRecipient.status == "pending"))).scalars().all())
        campaign.status = "sending"
        campaign.started_at = datetime.now(TEHRAN_TZ)
        session.add(BroadcastEvent(campaign_id=campaign.id, event_type="sending_started", details=f"pending={len(recipients)}"))
    markup = buttons_markup([(b.text, b.url) for b in buttons])
    sent = 0
    failed = 0
    for rec in recipients:
        try:
            await send_campaign_message(bot, int(rec.user_telegram_id), campaign, markup)
            status = "sent"
            error = None
            sent += 1
        except Exception as exc:  # Telegram can fail for blocked users, bad chat, etc.
            status = "failed"
            error = str(exc)[:1000]
            failed += 1
        async with session_scope() as session:
            current = await session.get(BroadcastRecipient, rec.id)
            camp = await session.get(BroadcastCampaign, campaign_id)
            if current:
                current.status = status
                current.error = error
                if status == "sent":
                    current.sent_at = datetime.now(TEHRAN_TZ)
            if camp:
                camp.sent_count = (camp.sent_count or 0) + (1 if status == "sent" else 0)
                camp.failed_count = (camp.failed_count or 0) + (1 if status == "failed" else 0)
        if delay_seconds:
            await asyncio.sleep(delay_seconds)
    async with session_scope() as session:
        camp = await session.get(BroadcastCampaign, campaign_id)
        if camp:
            camp.status = "finished"
            camp.finished_at = datetime.now(TEHRAN_TZ)
            session.add(BroadcastEvent(campaign_id=campaign_id, event_type="finished", details=f"sent={sent}, failed={failed}"))
    return {"sent": sent, "failed": failed}


async def send_campaign_message(bot: Bot, chat_id: int, campaign: BroadcastCampaign, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    if campaign.message_type == "photo" and campaign.telegram_file_id:
        await bot.send_photo(chat_id, campaign.telegram_file_id, caption=campaign.caption, reply_markup=reply_markup)
    elif campaign.message_type == "video" and campaign.telegram_file_id:
        await bot.send_video(chat_id, campaign.telegram_file_id, caption=campaign.caption, reply_markup=reply_markup)
    elif campaign.message_type == "document" and campaign.telegram_file_id:
        await bot.send_document(chat_id, campaign.telegram_file_id, caption=campaign.caption, reply_markup=reply_markup)
    elif campaign.message_type == "voice" and campaign.telegram_file_id:
        await bot.send_voice(chat_id, campaign.telegram_file_id, caption=campaign.caption, reply_markup=reply_markup)
    elif campaign.message_type == "audio" and campaign.telegram_file_id:
        await bot.send_audio(chat_id, campaign.telegram_file_id, caption=campaign.caption, reply_markup=reply_markup)
    else:
        await bot.send_message(chat_id, campaign.text or campaign.caption or "", reply_markup=reply_markup, disable_web_page_preview=True)









