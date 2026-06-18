"""Timed automatic backup scheduler for Phase 3.1."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile
from sqlalchemy import select

from app.config import settings
from app.database import session_scope
from app.models import BotSetting
from app.services.admin_audit_service import audit_log
from app.services.backup_service import create_complete_backup

UTC = timezone.utc


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


async def get_setting(key: str, default: str = "") -> str:
    async with session_scope() as session:
        result = await session.execute(select(BotSetting).where(BotSetting.key == key))
        row = result.scalar_one_or_none()
        return row.value if row else default


async def set_setting(key: str, value: str) -> None:
    async with session_scope() as session:
        result = await session.execute(select(BotSetting).where(BotSetting.key == key))
        row = result.scalar_one_or_none()
        if row is None:
            session.add(BotSetting(key=key, value=value))
        else:
            row.value = value


async def get_auto_backup_config() -> dict[str, str]:
    return {
        "enabled": await get_setting("auto_backup_enabled", "0"),
        "interval_hours": await get_setting("auto_backup_interval_hours", "12"),
        "chat_ids": await get_setting("auto_backup_chat_ids", ""),
        "last_run_at": await get_setting("auto_backup_last_run_at", ""),
        "next_run_at": await get_setting("auto_backup_next_run_at", ""),
    }


async def enable_auto_backup(interval_hours: int, chat_ids: list[int]) -> dict[str, str]:
    interval_hours = max(1, min(int(interval_hours), 24 * 30))
    if not chat_ids:
        chat_ids = sorted(settings.bootstrap_super_admin_ids)
    next_run = _now() + timedelta(hours=interval_hours)
    await set_setting("auto_backup_enabled", "1")
    await set_setting("auto_backup_interval_hours", str(interval_hours))
    await set_setting("auto_backup_chat_ids", ",".join(str(x) for x in chat_ids))
    await set_setting("auto_backup_next_run_at", next_run.isoformat())
    return await get_auto_backup_config()


async def disable_auto_backup() -> None:
    await set_setting("auto_backup_enabled", "0")


def _chat_ids_from_config(config: dict[str, str]) -> list[int]:
    raw = config.get("chat_ids") or ""
    ids: list[int] = []
    for part in raw.replace("\n", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.append(int(part))
    if not ids:
        ids = sorted(settings.bootstrap_super_admin_ids)
    return ids


async def run_auto_backup_once(bot: Bot, reason: str = "manual") -> tuple[Path, dict]:
    config = await get_auto_backup_config()
    chat_ids = _chat_ids_from_config(config)
    path, manifest = await create_complete_backup(admin_id=None, bot=bot)
    usage = manifest.get("usage", {})
    files = manifest.get("ticket_files", {})
    caption = (
        "🕒 بک‌آپ خودکار ساخته شد.\n"
        f"دلیل: {reason}\n"
        f"کاربران: {usage.get('users_total', 0)}\n"
        f"سرویس‌های فعال: {usage.get('services_active', 0)}\n"
        f"فایل‌های تیکت داخل بک‌آپ: {files.get('active_files_backed_up', 0)}\n"
        f"فایل‌های ناموفق: {files.get('active_files_failed', 0)}"
    )
    for chat_id in chat_ids:
        try:
            await bot.send_document(chat_id, FSInputFile(path), caption=caption)
        except Exception:
            pass
    interval_hours = int(config.get("interval_hours") or "12")
    await set_setting("auto_backup_last_run_at", _now().isoformat())
    await set_setting("auto_backup_next_run_at", (_now() + timedelta(hours=interval_hours)).isoformat())
    try:
        await audit_log(chat_ids[0] if chat_ids else 0, "AUTO_BACKUP_RUN", "backup", path.name, f"reason={reason}")
    except Exception:
        pass
    return path, manifest


async def auto_backup_loop(bot: Bot) -> None:
    while True:
        try:
            config = await get_auto_backup_config()
            if config.get("enabled") == "1":
                interval_hours = int(config.get("interval_hours") or "12")
                next_run = _parse_dt(config.get("next_run_at"))
                if next_run is None:
                    next_run = _now() + timedelta(hours=interval_hours)
                    await set_setting("auto_backup_next_run_at", next_run.isoformat())
                if _now() >= next_run:
                    await run_auto_backup_once(bot, reason=f"every_{interval_hours}_hours")
        except asyncio.CancelledError:
            raise
        except Exception:
            # Do not kill polling because an automatic backup failed.
            pass
        await asyncio.sleep(60)


def start_auto_backup_scheduler(bot: Bot) -> asyncio.Task:
    return asyncio.create_task(auto_backup_loop(bot), name="howtoosee_auto_backup_loop")
