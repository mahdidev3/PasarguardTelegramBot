#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
03_reset_and_seed_everything.py

Reset/seed helper for HowTooSee bot.

کار این اسکریپت مرحله‌به‌مرحله است و قبل از هر مرحله از شما سؤال می‌پرسد.
می‌توانید هر مرحله را انجام دهید یا Skip کنید.

اجرا روی سرور:
    cd /home/PasarguardTelegramBot
    source .venv/bin/activate
    python scripts/03_reset_and_seed_everything.py

گزینه‌های مفید:
    --yes                         همه مراحل را بدون سؤال اجرا می‌کند
    --dry-run                     هیچ تغییر مخربی انجام نمی‌دهد؛ فقط گزارش می‌دهد
    --skip-pasarguard             مرحله‌های Pasarguard را رد می‌کند
    --first-buy-coupon-percent 5  درصد کد تخفیف خرید اول سرویس
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import os
import shutil
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import delete, select

TEHRAN_TZ = timezone(timedelta(hours=3, minutes=30))

DEFAULT_CARD = {
    "card_number": "6063731291146117",
    "owner_name": "امیر صادقی",
    "bank_name": "بانک قرض الحسنه مهر ایران",
    "note": "پرداخت سرویس",
    "is_active": 1,
}

DEFAULT_SALES_ADMIN = {
    "telegram_id": 483137754,
    "display_name": "امیر صادقی",
    "role": "sales",
    "is_active": 1,
}

DEFAULT_FIRST_BUY_COUPON_CODE = "FIRSTBUY"
DEFAULT_FIRST_BUY_COUPON_TITLE = "برای دوستانی که اولین بارشون هست"


def now_iso() -> str:
    return datetime.now(TEHRAN_TZ).isoformat(timespec="seconds")


def print_step(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def ask(title: str, *, default: bool = False, assume_yes: bool = False) -> bool:
    if assume_yes:
        print(f"✅ {title}  [auto yes]")
        return True
    suffix = "[y/N]" if not default else "[Y/n]"
    while True:
        answer = input(f"{title} {suffix}: ").strip().lower()
        if not answer:
            return default
        if answer in {"y", "yes", "1", "بله", "آره", "اره"}:
            return True
        if answer in {"n", "no", "0", "نه", "خیر", "skip", "s"}:
            return False
        print("لطفاً y یا n وارد کنید.")


def read_dotenv(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        env[key.strip()] = value
    return env


def parse_first_admin_id(raw: str | None) -> int:
    if raw:
        for part in raw.replace(",", " ").split():
            if part.lstrip("-").isdigit():
                return int(part)
    return 0


def resolve_sqlite_path(project_dir: Path, dotenv: dict[str, str]) -> Path:
    raw = dotenv.get("DATABASE_PATH") or os.getenv("DATABASE_PATH") or "bot.db"
    db_path = Path(raw)
    if not db_path.is_absolute():
        db_path = project_dir / db_path
    return db_path


def backup_file(path: Path, label: str) -> Path | None:
    if not path.exists():
        return None
    suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = path.with_name(f"{path.name}.{label}_backup_{suffix}")
    shutil.copy2(path, backup_path)
    return backup_path


def sqlite_column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def load_legacy_module(project_dir: Path):
    if str(project_dir) not in sys.path:
        sys.path.insert(0, str(project_dir))
    # Importing app.legacy_bot creates/recreates the SQLite schema and admin tables.
    import app.legacy_bot as legacy_bot  # type: ignore
    return legacy_bot


def sqlite_service_usernames(db_path: Path) -> list[str]:
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        with closing(conn):
            cols = sqlite_column_names(conn, "services")
            if "pasarguard_username" not in cols:
                return []
            rows = conn.execute(
                "SELECT DISTINCT pasarguard_username FROM services WHERE pasarguard_username IS NOT NULL AND TRIM(pasarguard_username) != ''"
            ).fetchall()
            return sorted({str(row["pasarguard_username"]).strip() for row in rows if row["pasarguard_username"]})
    except Exception as exc:
        print(f"⚠️ خواندن usernameهای سرویس از SQLite ناموفق بود: {exc}")
        return []


async def disable_pasarguard_users(project_dir: Path, db_path: Path, *, dry_run: bool) -> None:
    from app.config import settings
    from app.services.pasarguard_client import PasarguardClient
    from app.services.pasarguard_template_service import managed_marker

    marker = managed_marker()
    local_usernames = set(sqlite_service_usernames(db_path))
    marked_usernames: set[str] = set()

    if not settings.pasarguard_enabled:
        print("⏭ PASARGUARD_ENABLED=false است؛ مرحله کاربران remote رد شد.")
        return

    async with PasarguardClient() as client:
        try:
            remote_users = await client.list_users(limit=2000)
        except Exception as exc:
            print(f"❌ دریافت لیست کاربران Pasarguard ناموفق بود: {exc}")
            return

        for user in remote_users:
            note = str(user.get("note") or user.get("description") or "")
            username = str(user.get("username") or "").strip()
            if username and ("[HOWTOSEE_BOT_START]" in note or f"marker={marker}" in note):
                marked_usernames.add(username)

        usernames = sorted(local_usernames | marked_usernames)
        print(f"کاربران remote شناسایی‌شده برای غیرفعال‌سازی: {len(usernames)}")
        if not usernames:
            return

        for username in usernames:
            if dry_run:
                print(f"🧪 dry-run: disable/revoke user {username}")
                continue
            try:
                try:
                    await client.revoke_user_subscription(username)
                except Exception:
                    pass
                await client.update_user_by_username(username, {"status": "disabled"})
                print(f"✅ remote user disabled: {username}")
            except Exception as exc:
                print(f"⚠️ خطا در غیرفعال‌سازی {username}: {exc}")


async def disable_pasarguard_templates(*, dry_run: bool) -> None:
    from app.config import settings
    from app.services.pasarguard_client import PasarguardClient
    from app.services.pasarguard_template_service import is_managed_template

    if not settings.pasarguard_enabled:
        print("⏭ PASARGUARD_ENABLED=false است؛ مرحله templateهای remote رد شد.")
        return

    async with PasarguardClient() as client:
        try:
            templates = await client.list_user_templates(limit=2000)
        except Exception as exc:
            print(f"❌ دریافت templateها از Pasarguard ناموفق بود: {exc}")
            return

        ids: list[int] = []
        for template in templates:
            if is_managed_template(template) and template.get("id") is not None:
                try:
                    ids.append(int(template["id"]))
                except Exception:
                    pass
        print(f"Templateهای مدیریت‌شده توسط ربات برای غیرفعال‌سازی: {len(ids)}")
        if not ids:
            return
        if dry_run:
            print(f"🧪 dry-run: bulk disable templates {ids}")
            return
        try:
            await client.bulk_disable_user_templates(ids)
            print("✅ templateهای مدیریت‌شده ربات در Pasarguard غیرفعال شدند.")
        except Exception as exc:
            print(f"⚠️ bulk disable ناموفق بود؛ تلاش تک‌به‌تک انجام می‌شود: {exc}")
            for tid in ids:
                try:
                    current = await client.get_user_template(tid)
                    current["is_disabled"] = True
                    await client.update_user_template(tid, current)
                    print(f"✅ template disabled: {tid}")
                except Exception as item_exc:
                    print(f"⚠️ خطا برای template {tid}: {item_exc}")


async def reset_postgres(*, dry_run: bool) -> None:
    from app.database import get_engine, init_database
    from app.models import Base
    from app.services.schema_patch_service import apply_runtime_schema_patches

    engine = get_engine()
    if dry_run:
        print("🧪 dry-run: PostgreSQL drop_all/create_all انجام نمی‌شود.")
        return
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await apply_runtime_schema_patches()
    print("✅ PostgreSQL پاک و دوباره ساخته شد.")


async def seed_default_catalog(admin_id: int, free_test_mb: int) -> None:
    from app.database import session_scope
    from app.models import CatalogPlan, DataAddonPackageDB, FreeTestPlanDB, PlanCategory
    from app.services.plan_service import DEFAULT_ADDONS, DEFAULT_CATEGORIES, DEFAULT_PLANS

    async with session_scope() as session:
        for key, title, description, sort_order in DEFAULT_CATEGORIES:
            item = (await session.execute(select(PlanCategory).where(PlanCategory.key == key))).scalar_one_or_none()
            if item is None:
                item = PlanCategory(key=key)
                session.add(item)
            item.title = title
            item.description = description
            item.sort_order = sort_order
            item.is_active = True

        for idx, seed in enumerate(DEFAULT_PLANS, start=1):
            plan = (await session.execute(select(CatalogPlan).where(CatalogPlan.key == seed.key))).scalar_one_or_none()
            if plan is None:
                plan = CatalogPlan(key=seed.key, created_by=admin_id)
                session.add(plan)
            plan.title = seed.title
            plan.data_gb = seed.data_gb
            plan.days = seed.days
            plan.price = seed.price
            plan.category = seed.category
            plan.badge = seed.badge
            plan.sort_order = idx * 10
            plan.is_active = True
            plan.updated_by = admin_id

        for idx, seed in enumerate(DEFAULT_ADDONS, start=1):
            addon = (await session.execute(select(DataAddonPackageDB).where(DataAddonPackageDB.key == seed.key))).scalar_one_or_none()
            if addon is None:
                addon = DataAddonPackageDB(key=seed.key, created_by=admin_id)
                session.add(addon)
            addon.title = seed.title
            addon.data_gb = seed.data_gb
            addon.price = seed.price
            addon.badge = seed.badge
            addon.sort_order = idx * 10
            addon.is_active = True
            addon.updated_by = admin_id

        for service_type, title, sort_order in [("standard", "رایگان استاندارد", 500), ("speed", "رایگان پرسرعت", 510)]:
            key = f"free_{service_type}_{free_test_mb}"
            free_title = f"{free_test_mb} مگابایت | {title}"
            free_plan = (await session.execute(select(FreeTestPlanDB).where(FreeTestPlanDB.key == key))).scalar_one_or_none()
            if free_plan is None:
                free_plan = FreeTestPlanDB(key=key)
                session.add(free_plan)
            free_plan.title = free_title
            free_plan.data_gb = free_test_mb / 1024
            free_plan.days = 3
            free_plan.category = f"free:{service_type}"
            free_plan.badge = "رایگان"
            free_plan.sort_order = 10
            free_plan.is_active = True

            catalog_free = (await session.execute(select(CatalogPlan).where(CatalogPlan.key == key))).scalar_one_or_none()
            if catalog_free is None:
                catalog_free = CatalogPlan(key=key, created_by=admin_id)
                session.add(catalog_free)
            catalog_free.title = free_title
            catalog_free.data_gb = free_test_mb / 1024
            catalog_free.days = 3
            catalog_free.price = 0
            catalog_free.category = f"free:{service_type}"
            catalog_free.badge = "رایگان"
            catalog_free.sort_order = sort_order
            catalog_free.is_active = True
            catalog_free.updated_by = admin_id

    print("✅ پلن‌ها، دسته‌ها، بسته‌های افزایش حجم و پلن‌های رایگان seed/upsert شدند.")


def seed_sqlite_defaults(legacy_bot: Any, *, coupon_percent: int, admin_id: int, dry_run: bool) -> None:
    db = legacy_bot.db
    if dry_run:
        print("🧪 dry-run: seed کارت/ادمین فروش/کد تخفیف در SQLite انجام نمی‌شود.")
        return

    with closing(db.connect()) as conn:
        # Default card: make this card the active/default one.
        conn.execute("UPDATE payment_cards SET is_active = 0, updated_at = ?", (now_iso(),))
        existing = conn.execute("SELECT id FROM payment_cards WHERE card_number = ?", (DEFAULT_CARD["card_number"],)).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE payment_cards
                SET owner_name = ?, bank_name = ?, note = ?, is_active = 1, updated_at = ?
                WHERE card_number = ?
                """,
                (DEFAULT_CARD["owner_name"], DEFAULT_CARD["bank_name"], DEFAULT_CARD["note"], now_iso(), DEFAULT_CARD["card_number"]),
            )
        else:
            conn.execute(
                """
                INSERT INTO payment_cards (card_number, owner_name, bank_name, note, is_active, created_at, updated_at)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                (DEFAULT_CARD["card_number"], DEFAULT_CARD["owner_name"], DEFAULT_CARD["bank_name"], DEFAULT_CARD["note"], now_iso(), now_iso()),
            )

        # Sales admin.
        conn.execute(
            """
            INSERT INTO admins (telegram_id, role, display_name, added_by, is_active, created_at)
            VALUES (?, 'sales', ?, ?, 1, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                role = CASE WHEN admins.role = 'super' THEN admins.role ELSE 'sales' END,
                display_name = excluded.display_name,
                is_active = 1
            """,
            (DEFAULT_SALES_ADMIN["telegram_id"], DEFAULT_SALES_ADMIN["display_name"], admin_id or None, now_iso()),
        )
        conn.commit()

    condition = {
        "version": 1,
        "groups": [[
            {"type": "first_purchase"},
            {"type": "order_operations", "operations": ["service_purchase"]},
        ]],
    }
    legacy_bot.create_coupon_admin(
        DEFAULT_FIRST_BUY_COUPON_CODE,
        int(coupon_percent),
        DEFAULT_FIRST_BUY_COUPON_TITLE,
        "custom",
        "",
        None,
        None,
        admin_id,
        per_user_limit=1,
        max_discount_percent=100,
        max_discount_amount=None,
        min_order_amount=0,
        stack_with_referral=1,
        condition_json=__import__("json").dumps(condition, ensure_ascii=False),
        condition_label=legacy_bot.render_coupon_condition_label(condition),
    )
    print("✅ کارت پیش‌فرض، ادمین sales و کد تخفیف FIRSTBUY seed شدند.")


def run_tutorial_reseed(project_dir: Path, db_path: Path, *, admin_id: int, dry_run: bool) -> None:
    script_path = project_dir / "scripts" / "02_reseed_tutorials.py"
    if not script_path.exists():
        print(f"⚠️ اسکریپت آموزش‌ها پیدا نشد: {script_path}")
        return
    spec = importlib.util.spec_from_file_location("reseed_tutorials", script_path)
    if spec is None or spec.loader is None:
        print("⚠️ امکان import اسکریپت آموزش‌ها نبود.")
        return
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    if dry_run:
        print("🧪 dry-run: reseed آموزش‌ها انجام نمی‌شود.")
        return
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    with closing(conn):
        module.ensure_schema(conn)
        module.reset_tutorials(conn)
        category_ids: dict[str, int] = {}
        for cat in module.CATEGORIES:
            category_ids[cat["key"]] = module.insert_category(conn, cat, admin_id or None)
        for tut in module.TUTORIALS:
            module.insert_tutorial(conn, category_ids[tut["category"]], tut, admin_id or None)
        conn.commit()
    print(f"✅ آموزش‌ها seed شدند: {len(module.CATEGORIES)} دسته، {len(module.TUTORIALS)} آموزش")


async def main_async() -> int:
    parser = argparse.ArgumentParser(description="Reset Pasarguard + DBs and seed HowTooSee defaults.")
    parser.add_argument("--project-dir", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--yes", action="store_true", help="run all steps without prompts")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-pasarguard", action="store_true")
    parser.add_argument("--first-buy-coupon-percent", type=int, default=5)
    args = parser.parse_args()

    project_dir = Path(args.project_dir).resolve()
    if str(project_dir) not in sys.path:
        sys.path.insert(0, str(project_dir))
    load_dotenv(project_dir / ".env", override=True)
    dotenv = read_dotenv(project_dir / ".env")
    admin_id = parse_first_admin_id(dotenv.get("ADMIN_CHAT_IDS") or os.getenv("ADMIN_CHAT_IDS"))
    db_path = resolve_sqlite_path(project_dir, dotenv)

    print_step("HowTooSee Reset & Seed")
    print(f"project_dir: {project_dir}")
    print(f"sqlite_db  : {db_path}")
    print(f"admin_id   : {admin_id or '-'}")
    print(f"dry_run    : {args.dry_run}")

    if not args.yes:
        print("\n⚠️ این اسکریپت می‌تواند دیتابیس‌ها را پاک کند و کاربران/templateهای مدیریت‌شده در Pasarguard را غیرفعال کند.")
        if not ask("ادامه می‌دهید؟", default=False):
            return 0

    # Import config after dotenv is loaded.
    import app.config as app_config  # noqa: F401

    if not args.skip_pasarguard:
        print_step("مرحله Pasarguard / Users")
        if ask("کاربران remote مربوط به ربات را در Pasarguard revoke/disable کنم؟", assume_yes=args.yes):
            await disable_pasarguard_users(project_dir, db_path, dry_run=args.dry_run)
        else:
            print("⏭ Skip")

        print_step("مرحله Pasarguard / Templates")
        if ask("templateهای مدیریت‌شده توسط ربات را در Pasarguard غیرفعال کنم؟", assume_yes=args.yes):
            await disable_pasarguard_templates(dry_run=args.dry_run)
        else:
            print("⏭ Skip")

    print_step("مرحله SQLite")
    if ask("SQLite bot.db را پاک و از نو بسازم؟", assume_yes=args.yes):
        if db_path.exists() and not args.dry_run:
            backup = backup_file(db_path, "sqlite_reset")
            if backup:
                print(f"✅ بک‌آپ SQLite ساخته شد: {backup}")
            db_path.unlink()
            for suffix in ("-wal", "-shm"):
                side = Path(str(db_path) + suffix)
                if side.exists():
                    side.unlink()
        elif args.dry_run:
            print("🧪 dry-run: SQLite حذف نمی‌شود.")
    else:
        print("⏭ Skip")

    print_step("مرحله PostgreSQL")
    if ask("PostgreSQL را drop/create کنم؟", assume_yes=args.yes):
        await reset_postgres(dry_run=args.dry_run)
    else:
        print("⏭ Skip")
        # Make sure tables exist for seed steps.
        if not args.dry_run:
            from app.database import init_database
            from app.services.schema_patch_service import apply_runtime_schema_patches
            await init_database()
            await apply_runtime_schema_patches()

    print_step("Seed پلن‌ها و تنظیمات پایه")
    if ask("پلن‌های پیش‌فرض، پلن رایگان و بسته‌های افزایش حجم را seed/upsert کنم؟", assume_yes=args.yes, default=True):
        if not args.dry_run:
            from app.services.ticket_service import seed_bootstrap_admins
            from app.services.text_template_service import seed_text_templates
            await seed_bootstrap_admins(set([admin_id]) if admin_id else set())
            await seed_text_templates()
        await seed_default_catalog(admin_id, int(os.getenv("FREE_TEST_MB", "150")))
    else:
        print("⏭ Skip")

    print_step("Sync پلن‌ها با Pasarguard")
    if not args.skip_pasarguard and ask("پلن‌ها را با templateهای Pasarguard sync کنم؟", assume_yes=args.yes, default=True):
        if args.dry_run:
            print("🧪 dry-run: sync واقعی انجام نمی‌شود.")
        else:
            from app.services.pasarguard_template_service import render_sync_report, sync_plan_templates
            report = await sync_plan_templates(admin_id=admin_id or None, dry_run=False)
            print(render_sync_report(report))
    else:
        print("⏭ Skip")

    print_step("Seed SQLite defaults")
    legacy_bot = load_legacy_module(project_dir)
    if ask("کارت پیش‌فرض، sales admin و کد FIRSTBUY را seed کنم؟", assume_yes=args.yes, default=True):
        seed_sqlite_defaults(
            legacy_bot,
            coupon_percent=max(1, min(int(args.first_buy_coupon_percent), 100)),
            admin_id=admin_id,
            dry_run=args.dry_run,
        )
    else:
        print("⏭ Skip")

    print_step("Seed آموزش‌ها")
    if ask("آموزش‌های آماده را از اسکریپت موجود seed کنم؟", assume_yes=args.yes, default=True):
        run_tutorial_reseed(project_dir, db_path, admin_id=admin_id, dry_run=args.dry_run)
    else:
        print("⏭ Skip")

    print_step("پایان")
    print("✅ عملیات تمام شد. بعد از بررسی خروجی، سرویس ربات را ری‌استارت کنید:")
    print("sudo systemctl restart pasarguardtelegrambot")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
