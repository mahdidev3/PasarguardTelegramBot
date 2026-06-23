"""Central configuration module.

Phase 0 introduces this module as the single future home for settings. The
current production logic still reads env values inside app.legacy_bot to avoid
behavior changes during the refactor checkpoint.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
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


def _parse_int_list(value: str) -> list[int]:
    values: list[int] = []
    for part in re.split(r"[,\s]+", value or ""):
        part = part.strip()
        if part.lstrip("-").isdigit():
            values.append(int(part))
    return values


def _parse_bool(value: str, default: bool = False) -> bool:
    raw = (value or "").strip().lower()
    if raw in {"1", "true", "yes", "on", "y"}:
        return True
    if raw in {"0", "false", "no", "off", "n"}:
        return False
    return default


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

    # Pasarguard integration (Phase 4)
    pasarguard_enabled: bool = False
    pasarguard_base_url: str = ""
    pasarguard_admin_username: str = ""
    pasarguard_admin_password: str = ""
    pasarguard_timeout_seconds: int = 20
    pasarguard_verify_ssl: bool = True
    pasarguard_dry_run: bool = True
    pasarguard_managed_prefix: str = "HTS_BOT"
    pasarguard_template_group_ids: list[int] = field(default_factory=list)
    pasarguard_username_prefix: str = ""
    pasarguard_username_suffix: str = ""


settings = Settings(
    bot_token=os.getenv("BOT_TOKEN", "").strip(),
    bot_username=os.getenv("BOT_USERNAME", "HowToSeeWorld_bot").strip().lstrip("@"),
    channel_username=os.getenv("CHANNEL_USERNAME", "HowToSeeWorld").strip().lstrip("@"),
    channel_link=os.getenv("CHANNEL_LINK", "").strip(),
    brand_name=os.getenv("BRAND_NAME", "HowToSee | Premium VPN").strip(),
    database_path=os.getenv("DATABASE_PATH", "bot.db").strip(),
    database_url=os.getenv("DATABASE_URL", "").strip(),
    subscription_base_url=os.getenv("SUBSCRIPTION_BASE_URL", "https://example.com/sub").rstrip("/"),
    referral_commission_percent=int(os.getenv("REFERRAL_COMMISSION_PERCENT", "10")),
    referred_discount_percent=int(os.getenv("REFERRED_DISCOUNT_PERCENT", "5")),
    free_test_mb=int(os.getenv("FREE_TEST_MB", "150")),
    wallet_min_topup=int(os.getenv("WALLET_MIN_TOPUP", "50000")),
    service_name_prefix=os.getenv("SERVICE_NAME_PREFIX", "howtosee_").strip(),
    bootstrap_super_admin_ids=_parse_id_set(os.getenv("ADMIN_CHAT_IDS", "")),
    pasarguard_enabled=_parse_bool(os.getenv("PASARGUARD_ENABLED", "false")),
    pasarguard_base_url=os.getenv("PASARGUARD_BASE_URL", "https://docdev.pangshanbebazar.ir:2087").strip().rstrip("/"),
    pasarguard_admin_username=os.getenv("PASARGUARD_ADMIN_USERNAME", "").strip(),
    pasarguard_admin_password=os.getenv("PASARGUARD_ADMIN_PASSWORD", "").strip(),
    pasarguard_timeout_seconds=int(os.getenv("PASARGUARD_TIMEOUT_SECONDS", "20")),
    pasarguard_verify_ssl=_parse_bool(os.getenv("PASARGUARD_VERIFY_SSL", "true"), True),
    pasarguard_dry_run=_parse_bool(os.getenv("PASARGUARD_DRY_RUN", "true"), True),
    pasarguard_managed_prefix=os.getenv("PASARGUARD_MANAGED_PREFIX", "HTS_BOT").strip() or "HTS_BOT",
    pasarguard_template_group_ids=_parse_int_list(os.getenv("PASARGUARD_TEMPLATE_GROUP_IDS", "")),
    pasarguard_username_prefix=os.getenv("PASARGUARD_USERNAME_PREFIX", "").strip()[:20],
    pasarguard_username_suffix=os.getenv("PASARGUARD_USERNAME_SUFFIX", "").strip()[:20],
)


