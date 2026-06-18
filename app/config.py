"""Central configuration module.

Phase 0 introduces this module as the single future home for settings. The
current production logic still reads env values inside app.legacy_bot to avoid
behavior changes during the refactor checkpoint.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

TEHRAN_TZ = timezone(timedelta(hours=3, minutes=30))


def _parse_id_set(value: str) -> set[int]:
    ids: set[int] = set()
    for part in re.split(r"[,\s]+", value or ""):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return ids


@dataclass(frozen=True)
class Settings:
    bot_token: str
    bot_username: str
    channel_username: str
    channel_link: str
    brand_name: str
    database_path: str
    database_url: str
    subscription_base_url: str
    referral_commission_percent: int
    referred_discount_percent: int
    free_test_mb: int
    wallet_min_topup: int
    service_name_prefix: str
    bootstrap_super_admin_ids: set[int]


settings = Settings(
    bot_token=os.getenv("BOT_TOKEN", "").strip(),
    bot_username=os.getenv("BOT_USERNAME", "HowToSeeWorld_bot").strip().lstrip("@"),
    channel_username=os.getenv("CHANNEL_USERNAME", "HowTooSeeWorld").strip().lstrip("@"),
    channel_link=os.getenv("CHANNEL_LINK", "").strip(),
    brand_name=os.getenv("BRAND_NAME", "HowTooSee | Premium VPN").strip(),
    database_path=os.getenv("DATABASE_PATH", "bot.db").strip(),
    database_url=os.getenv("DATABASE_URL", "").strip(),
    subscription_base_url=os.getenv("SUBSCRIPTION_BASE_URL", "https://example.com/sub").rstrip("/"),
    referral_commission_percent=int(os.getenv("REFERRAL_COMMISSION_PERCENT", "10")),
    referred_discount_percent=int(os.getenv("REFERRED_DISCOUNT_PERCENT", "5")),
    free_test_mb=int(os.getenv("FREE_TEST_MB", "150")),
    wallet_min_topup=int(os.getenv("WALLET_MIN_TOPUP", "50000")),
    service_name_prefix=os.getenv("SERVICE_NAME_PREFIX", "howtosee_").strip(),
    bootstrap_super_admin_ids=_parse_id_set(os.getenv("ADMIN_CHAT_IDS", "")),
)
