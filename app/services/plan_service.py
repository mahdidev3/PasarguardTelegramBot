"""Database-backed plan catalog service for Phase 2."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import desc, select

from app.database import session_scope
from app.models import CatalogPlan, CatalogPlanVersion, DataAddonPackageDB, FreeTestPlanDB, PlanCategory


@dataclass(frozen=True)
class SeedPlan:
    key: str
    title: str
    data_gb: float
    days: int
    price: int
    category: str
    badge: str


@dataclass(frozen=True)
class SeedAddon:
    key: str
    title: str
    data_gb: float
    price: int
    badge: str


DEFAULT_CATEGORIES: list[tuple[str, str, str, int]] = [
    ("monthly", "🛍 پلن‌های آماده یک‌ماهه", "فعال‌سازی سریع و ساده", 10),
    ("quarterly", "💎 پلن‌های حرفه‌ای سه‌ماهه", "اقتصادی‌تر برای استفاده بلندمدت", 20),
]

DEFAULT_PLANS: list[SeedPlan] = [
    SeedPlan("m_10", "۱۰ گیگابایت | یک‌ماهه", 10, 31, 100_000, "monthly", "اقتصادی"),
    SeedPlan("m_20", "۲۰ گیگابایت | یک‌ماهه", 20, 31, 190_000, "monthly", "محبوب"),
    SeedPlan("m_30", "۳۰ گیگابایت | یک‌ماهه", 30, 31, 270_000, "monthly", "متعادل"),
    SeedPlan("m_40", "۴۰ گیگابایت | یک‌ماهه", 40, 31, 340_000, "monthly", "حرفه‌ای"),
    SeedPlan("m_50", "۵۰ گیگابایت | یک‌ماهه", 50, 31, 400_000, "monthly", "پرفروش"),
    SeedPlan("q_60", "۶۰ گیگابایت | سه‌ماهه", 60, 91, 540_000, "quarterly", "سه‌ماهه"),
    SeedPlan("q_100", "۱۰۰ گیگابایت | سه‌ماهه", 100, 91, 850_000, "quarterly", "پیشنهادی"),
    SeedPlan("q_150", "۱۵۰ گیگابایت | سه‌ماهه", 150, 91, 1_180_000, "quarterly", "حجیم"),
]

DEFAULT_ADDONS: list[SeedAddon] = [
    SeedAddon("add_5", "۵ گیگابایت حجم اضافه", 5, 39_000, "شروع اقتصادی"),
    SeedAddon("add_10", "۱۰ گیگابایت حجم اضافه", 10, 69_000, "انتخاب هوشمند"),
    SeedAddon("add_20", "۲۰ گیگابایت حجم اضافه", 20, 129_000, "پیشنهادی"),
    SeedAddon("add_50", "۵۰ گیگابایت حجم اضافه", 50, 299_000, "به‌صرفه‌ترین"),
]


async def seed_catalog_defaults(free_test_mb: int) -> None:
    """Seed the old hard-coded catalog into PostgreSQL on fresh DBs."""
    async with session_scope() as session:
        for key, title, description, sort_order in DEFAULT_CATEGORIES:
            existing = (await session.execute(select(PlanCategory).where(PlanCategory.key == key))).scalar_one_or_none()
            if existing is None:
                session.add(PlanCategory(key=key, title=title, description=description, sort_order=sort_order, is_active=True))
        for idx, seed in enumerate(DEFAULT_PLANS, start=1):
            existing = (await session.execute(select(CatalogPlan).where(CatalogPlan.key == seed.key))).scalar_one_or_none()
            if existing is None:
                session.add(CatalogPlan(**asdict(seed), sort_order=idx * 10, is_active=True))
        for idx, seed in enumerate(DEFAULT_ADDONS, start=1):
            existing = (await session.execute(select(DataAddonPackageDB).where(DataAddonPackageDB.key == seed.key))).scalar_one_or_none()
            if existing is None:
                session.add(DataAddonPackageDB(**asdict(seed), sort_order=idx * 10, is_active=True))
        for service_type, title in [("standard", "رایگان استاندارد"), ("speed", "رایگان پرسرعت")]:
            key = f"free_{service_type}_{free_test_mb}"
            existing = (await session.execute(select(FreeTestPlanDB).where(FreeTestPlanDB.key == key))).scalar_one_or_none()
            if existing is None:
                session.add(
                    FreeTestPlanDB(
                        key=key,
                        title=f"{free_test_mb} مگابایت | {title}",
                        data_gb=free_test_mb / 1024,
                        days=3,
                        category=f"free:{service_type}",
                        badge="رایگان",
                        sort_order=10,
                        is_active=True,
                    )
                )


async def list_categories(active_only: bool = True) -> list[PlanCategory]:
    async with session_scope() as session:
        stmt = select(PlanCategory).order_by(PlanCategory.sort_order, PlanCategory.id)
        if active_only:
            stmt = stmt.where(PlanCategory.is_active.is_(True))
        return list((await session.execute(stmt)).scalars().all())


async def list_plans(active_only: bool = False) -> list[CatalogPlan]:
    async with session_scope() as session:
        stmt = select(CatalogPlan).order_by(CatalogPlan.category, CatalogPlan.sort_order, CatalogPlan.id)
        if active_only:
            stmt = stmt.where(CatalogPlan.is_active.is_(True))
        return list((await session.execute(stmt)).scalars().all())


async def get_plan(plan_key: str) -> CatalogPlan | None:
    async with session_scope() as session:
        return (await session.execute(select(CatalogPlan).where(CatalogPlan.key == plan_key))).scalar_one_or_none()


async def upsert_plan_from_line(line: str, admin_id: int) -> tuple[bool, str]:
    """Parse and save a plan line: key | title | data_gb | days | price | category | badge."""
    parts = [p.strip() for p in (line or "").split("|")]
    if len(parts) < 7:
        return False, "فرمت اشتباه است. نمونه: key | title | data_gb | days | price | category | badge"
    key, title, data_s, days_s, price_s, category, badge = parts[:7]
    try:
        data_gb = float(data_s)
        days = int(days_s)
        price = int(price_s.replace(",", "").replace("٬", ""))
    except ValueError:
        return False, "حجم، روز یا قیمت معتبر نیست."
    if not key or not title or days <= 0 or data_gb <= 0 or price < 0:
        return False, "مقادیر واردشده معتبر نیستند."
    async with session_scope() as session:
        plan = (await session.execute(select(CatalogPlan).where(CatalogPlan.key == key))).scalar_one_or_none()
        snapshot: dict[str, Any] | None = None
        if plan is None:
            plan = CatalogPlan(key=key, created_by=admin_id, sort_order=100)
            session.add(plan)
        else:
            snapshot = {
                "key": plan.key,
                "title": plan.title,
                "data_gb": plan.data_gb,
                "days": plan.days,
                "price": plan.price,
                "category": plan.category,
                "badge": plan.badge,
                "is_active": plan.is_active,
            }
        plan.title = title
        plan.data_gb = data_gb
        plan.days = days
        plan.price = price
        plan.category = category
        plan.badge = badge
        plan.is_active = True
        plan.updated_by = admin_id
        if snapshot:
            session.add(CatalogPlanVersion(plan_key=key, snapshot_json=snapshot, changed_by=admin_id, change_note="admin upsert"))
    return True, f"پلن <code>{key}</code> ذخیره شد."


async def set_plan_active(plan_key: str, active: bool, admin_id: int) -> bool:
    async with session_scope() as session:
        plan = (await session.execute(select(CatalogPlan).where(CatalogPlan.key == plan_key))).scalar_one_or_none()
        if plan is None:
            return False
        snapshot = {
            "key": plan.key,
            "title": plan.title,
            "data_gb": plan.data_gb,
            "days": plan.days,
            "price": plan.price,
            "category": plan.category,
            "badge": plan.badge,
            "is_active": plan.is_active,
        }
        plan.is_active = active
        plan.updated_by = admin_id
        session.add(CatalogPlanVersion(plan_key=plan_key, snapshot_json=snapshot, changed_by=admin_id, change_note=f"active={active}"))
        return True


async def list_addons(active_only: bool = False) -> list[DataAddonPackageDB]:
    async with session_scope() as session:
        stmt = select(DataAddonPackageDB).order_by(DataAddonPackageDB.sort_order, DataAddonPackageDB.id)
        if active_only:
            stmt = stmt.where(DataAddonPackageDB.is_active.is_(True))
        return list((await session.execute(stmt)).scalars().all())


async def upsert_addon_from_line(line: str, admin_id: int) -> tuple[bool, str]:
    parts = [p.strip() for p in (line or "").split("|")]
    if len(parts) < 5:
        return False, "فرمت اشتباه است. نمونه: key | title | data_gb | price | badge"
    key, title, data_s, price_s, badge = parts[:5]
    try:
        data_gb = float(data_s)
        price = int(price_s.replace(",", "").replace("٬", ""))
    except ValueError:
        return False, "حجم یا قیمت معتبر نیست."
    async with session_scope() as session:
        addon = (await session.execute(select(DataAddonPackageDB).where(DataAddonPackageDB.key == key))).scalar_one_or_none()
        if addon is None:
            addon = DataAddonPackageDB(key=key, created_by=admin_id, sort_order=100)
            session.add(addon)
        addon.title = title
        addon.data_gb = data_gb
        addon.price = price
        addon.badge = badge
        addon.is_active = True
        addon.updated_by = admin_id
    return True, f"بسته حجم <code>{key}</code> ذخیره شد."


async def sync_legacy_catalog_from_db(legacy_module: Any) -> None:
    """Push DB catalog into the old legacy dictionaries until all buy flow is refactored."""
    plans = await list_plans(active_only=True)
    addons = await list_addons(active_only=True)
    async with session_scope() as session:
        free_plans = list((await session.execute(select(FreeTestPlanDB).where(FreeTestPlanDB.is_active.is_(True)).order_by(FreeTestPlanDB.sort_order))).scalars().all())
    if hasattr(legacy_module, "Plan"):
        legacy_module.PLANS.clear()
        for p in plans:
            legacy_module.PLANS[p.key] = legacy_module.Plan(p.key, p.title, p.data_gb, p.days, p.price, p.category, p.badge or "")
        legacy_module.FREE_TEST_PLANS.clear()
        for p in free_plans:
            legacy_module.FREE_TEST_PLANS[p.key] = legacy_module.Plan(p.key, p.title, p.data_gb, p.days, 0, p.category, p.badge or "رایگان")
    if hasattr(legacy_module, "DataAddon"):
        legacy_module.DATA_ADDON_PACKAGES.clear()
        for a in addons:
            legacy_module.DATA_ADDON_PACKAGES[a.key] = legacy_module.DataAddon(a.key, a.title, a.data_gb, a.price, a.badge or "")
