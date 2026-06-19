"""SQLAlchemy models for Phase 1 PostgreSQL storage.

The schema mirrors the current bot entities and adds the complete ticket and
numeric-confirmation foundations requested for Phase 1.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    JSON,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), onupdate=func.now())


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(255))
    first_name: Mapped[str | None] = mapped_column(String(255))
    referral_code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    referred_by_telegram_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    wallet_balance: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_referral_earned: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    free_test_used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    first_purchase_done: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    locked_reason: Mapped[str | None] = mapped_column(Text)
    locked_notice: Mapped[str | None] = mapped_column(Text)
    admin_note: Mapped[str | None] = mapped_column(Text)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Service(Base, TimestampMixin):
    __tablename__ = "services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    plan_key: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    plan_title: Mapped[str] = mapped_column(String(255), nullable=False)
    data_gb: Mapped[float] = mapped_column(Float, nullable=False)
    days: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[int] = mapped_column(Integer, nullable=False)
    paid_amount: Mapped[int] = mapped_column(Integer, nullable=False)
    token: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    data_used_mb: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_test: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    admin_note: Mapped[str | None] = mapped_column(Text)
    locked_reason: Mapped[str | None] = mapped_column(Text)

    # Pasarguard remote binding (Phase 4)
    pasarguard_user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    pasarguard_username: Mapped[str | None] = mapped_column(String(255), index=True)
    pasarguard_template_id: Mapped[int | None] = mapped_column(Integer, index=True)
    pasarguard_subscription_url: Mapped[str | None] = mapped_column(Text)
    pasarguard_last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pasarguard_last_state_json: Mapped[dict | None] = mapped_column(JSON)
    pasarguard_sync_status: Mapped[str | None] = mapped_column(String(40))
    pasarguard_sync_error: Mapped[str | None] = mapped_column(Text)


class Order(Base, TimestampMixin):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    service_id: Mapped[int | None] = mapped_column(Integer, index=True)
    plan_key: Mapped[str] = mapped_column(String(120), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    discount_amount: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    wallet_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    payment_method: Mapped[str] = mapped_column(String(120), nullable=False)
    coupon_code: Mapped[str | None] = mapped_column(String(80))
    coupon_discount: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    admin_note: Mapped[str | None] = mapped_column(Text)


class WalletTransaction(Base, TimestampMixin):
    __tablename__ = "wallet_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    related_user_id: Mapped[int | None] = mapped_column(BigInteger)


class Referral(Base, TimestampMixin):
    __tablename__ = "referrals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    referrer_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    referred_telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    rewarded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    first_order_id: Mapped[int | None] = mapped_column(Integer)
    commission_amount: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rewarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Admin(Base, TimestampMixin):
    __tablename__ = "admins"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    role: Mapped[str] = mapped_column(String(32), default="support", nullable=False)
    added_by: Mapped[int | None] = mapped_column(BigInteger)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class AdminLog(Base):
    __tablename__ = "admin_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    admin_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(80))
    target_id: Mapped[str | None] = mapped_column(String(120))
    details: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class BotSetting(Base):
    __tablename__ = "bot_settings"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class Coupon(Base, TimestampMixin):
    __tablename__ = "coupons"

    code: Mapped[str] = mapped_column(String(80), primary_key=True)
    percent: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    scope: Mapped[str] = mapped_column(String(50), default="all", nullable=False)
    target_user_ids: Mapped[str | None] = mapped_column(Text)
    usage_limit: Mapped[int | None] = mapped_column(Integer)
    per_user_limit: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    used_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    stack_with_referral: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    max_discount_percent: Mapped[int] = mapped_column(Integer, default=40, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[int | None] = mapped_column(BigInteger)


class CouponUsage(Base):
    __tablename__ = "coupon_usages"
    __table_args__ = (UniqueConstraint("code", "order_id", name="uq_coupon_usage_order"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    order_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    discount_amount: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PlanCategory(Base, TimestampMixin):
    __tablename__ = "plan_categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class CatalogPlan(Base, TimestampMixin):
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    data_gb: Mapped[float] = mapped_column(Float, nullable=False)
    days: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[int] = mapped_column(Integer, nullable=False)
    category: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    badge: Mapped[str | None] = mapped_column(String(120))
    sort_order: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_featured: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    max_per_user: Mapped[int | None] = mapped_column(Integer)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    updated_by: Mapped[int | None] = mapped_column(BigInteger)

    # Pasarguard template binding (Phase 4)
    pasarguard_template_id: Mapped[int | None] = mapped_column(Integer, index=True)
    pasarguard_template_name: Mapped[str | None] = mapped_column(String(255), index=True)
    pasarguard_sync_status: Mapped[str | None] = mapped_column(String(40))
    pasarguard_sync_error: Mapped[str | None] = mapped_column(Text)
    pasarguard_last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pasarguard_last_state_json: Mapped[dict | None] = mapped_column(JSON)


class CatalogPlanVersion(Base):
    __tablename__ = "plan_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_key: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    snapshot_json: Mapped[dict | None] = mapped_column(JSON)
    changed_by: Mapped[int | None] = mapped_column(BigInteger)
    change_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class DataAddonPackageDB(Base, TimestampMixin):
    __tablename__ = "data_addon_packages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    data_gb: Mapped[float] = mapped_column(Float, nullable=False)
    price: Mapped[int] = mapped_column(Integer, nullable=False)
    badge: Mapped[str | None] = mapped_column(String(120))
    sort_order: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    updated_by: Mapped[int | None] = mapped_column(BigInteger)


class FreeTestPlanDB(Base, TimestampMixin):
    __tablename__ = "free_test_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    data_gb: Mapped[float] = mapped_column(Float, nullable=False)
    days: Mapped[int] = mapped_column(Integer, nullable=False)
    category: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    badge: Mapped[str | None] = mapped_column(String(120))
    sort_order: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    updated_by: Mapped[int | None] = mapped_column(BigInteger)


class TextTemplate(Base):
    __tablename__ = "text_templates"

    key: Mapped[str] = mapped_column(String(160), primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    group_name: Mapped[str] = mapped_column(String(80), index=True, default="general", nullable=False)
    allowed_placeholders: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    updated_by: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class TextTemplateVersion(Base):
    __tablename__ = "text_template_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    template_key: Mapped[str] = mapped_column(String(160), index=True, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    changed_by: Mapped[int | None] = mapped_column(BigInteger)
    change_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class BroadcastCampaign(Base, TimestampMixin):
    __tablename__ = "broadcast_campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_by: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    target_scope: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    message_type: Mapped[str] = mapped_column(String(40), default="text", nullable=False)
    text: Mapped[str | None] = mapped_column(Text)
    caption: Mapped[str | None] = mapped_column(Text)
    telegram_file_id: Mapped[str | None] = mapped_column(Text)
    telegram_file_unique_id: Mapped[str | None] = mapped_column(Text)
    file_name: Mapped[str | None] = mapped_column(String(255))
    mime_type: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(40), default="draft", index=True, nullable=False)
    recipient_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sent_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BroadcastButton(Base):
    __tablename__ = "broadcast_buttons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("broadcast_campaigns.id", ondelete="CASCADE"), index=True, nullable=False)
    text: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    row_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    col_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class BroadcastRecipient(Base):
    __tablename__ = "broadcast_recipients"
    __table_args__ = (UniqueConstraint("campaign_id", "user_telegram_id", name="uq_broadcast_recipient"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("broadcast_campaigns.id", ondelete="CASCADE"), index=True, nullable=False)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="pending", index=True, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class BroadcastEvent(Base):
    __tablename__ = "broadcast_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("broadcast_campaigns.id", ondelete="CASCADE"), index=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    details: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Ticket(Base, TimestampMixin):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    category: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    related_type: Mapped[str] = mapped_column(String(80), default="general", nullable=False)
    related_id: Mapped[str | None] = mapped_column(String(120), index=True)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="open", index=True, nullable=False)
    priority: Mapped[str] = mapped_column(String(40), default="normal", index=True, nullable=False)
    assigned_admin_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    messages: Mapped[list["TicketMessage"]] = relationship(back_populates="ticket", cascade="all, delete-orphan")
    notes: Mapped[list["TicketAdminNote"]] = relationship(back_populates="ticket", cascade="all, delete-orphan")
    events: Mapped[list["TicketEvent"]] = relationship(back_populates="ticket", cascade="all, delete-orphan")


class TicketMessage(Base):
    __tablename__ = "ticket_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), index=True, nullable=False)
    sender_type: Mapped[str] = mapped_column(String(30), nullable=False)  # user/admin/system
    sender_telegram_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    body: Mapped[str | None] = mapped_column(Text)
    message_type: Mapped[str] = mapped_column(String(40), default="text", nullable=False)
    telegram_file_id: Mapped[str | None] = mapped_column(Text)
    telegram_file_unique_id: Mapped[str | None] = mapped_column(Text)
    file_name: Mapped[str | None] = mapped_column(String(255))
    mime_type: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    ticket: Mapped[Ticket] = relationship(back_populates="messages")
    attachments: Mapped[list["TicketAttachment"]] = relationship(back_populates="message", cascade="all, delete-orphan")


class TicketAttachment(Base):
    __tablename__ = "ticket_attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), index=True, nullable=False)
    message_id: Mapped[int | None] = mapped_column(ForeignKey("ticket_messages.id", ondelete="SET NULL"), index=True)
    telegram_file_id: Mapped[str | None] = mapped_column(Text)
    telegram_file_unique_id: Mapped[str | None] = mapped_column(Text)
    file_name: Mapped[str | None] = mapped_column(String(255))
    mime_type: Mapped[str | None] = mapped_column(String(255))
    file_size: Mapped[int | None] = mapped_column(Integer)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delete_reason: Mapped[str | None] = mapped_column(Text)
    backed_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    backup_path: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    message: Mapped[TicketMessage | None] = relationship(back_populates="attachments")


class TicketAdminNote(Base):
    __tablename__ = "ticket_admin_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), index=True, nullable=False)
    admin_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    ticket: Mapped[Ticket] = relationship(back_populates="notes")


class TicketEvent(Base):
    __tablename__ = "ticket_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), index=True, nullable=False)
    actor_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    details: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    ticket: Mapped[Ticket] = relationship(back_populates="events")


class AdminConfirmation(Base):
    __tablename__ = "admin_confirmations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    admin_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    action: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    code_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PasarguardTemplate(Base, TimestampMixin):
    __tablename__ = "pasarguard_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_key: Mapped[str | None] = mapped_column(String(80), index=True)
    remote_template_id: Mapped[int | None] = mapped_column(Integer, unique=True, index=True)
    remote_name: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    managed_marker: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    data_limit_bytes: Mapped[int | None] = mapped_column(BigInteger)
    expire_duration_seconds: Mapped[int | None] = mapped_column(Integer)
    username_prefix: Mapped[str | None] = mapped_column(String(20))
    username_suffix: Mapped[str | None] = mapped_column(String(20))
    group_ids_json: Mapped[list | None] = mapped_column(JSON)
    is_disabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sync_status: Mapped[str] = mapped_column(String(40), default="local", index=True, nullable=False)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_remote_state_json: Mapped[dict | None] = mapped_column(JSON)
    last_error: Mapped[str | None] = mapped_column(Text)


class PasarguardUser(Base, TimestampMixin):
    __tablename__ = "pasarguard_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    service_id: Mapped[int | None] = mapped_column(Integer, index=True)
    user_telegram_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    plan_key: Mapped[str | None] = mapped_column(String(80), index=True)
    remote_user_id: Mapped[int | None] = mapped_column(Integer, unique=True, index=True)
    remote_username: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    remote_template_id: Mapped[int | None] = mapped_column(Integer, index=True)
    subscription_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(String(40), index=True)
    data_limit_bytes: Mapped[int | None] = mapped_column(BigInteger)
    used_traffic_bytes: Mapped[int | None] = mapped_column(BigInteger)
    expire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    managed_marker: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_remote_state_json: Mapped[dict | None] = mapped_column(JSON)
    sync_status: Mapped[str] = mapped_column(String(40), default="local", index=True, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)


class PasarguardSyncJob(Base):
    __tablename__ = "pasarguard_sync_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    admin_telegram_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    mode: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="running", index=True, nullable=False)
    total_items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    success_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    report_json: Mapped[dict | None] = mapped_column(JSON)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PasarguardSyncEvent(Base):
    __tablename__ = "pasarguard_sync_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int | None] = mapped_column(Integer, index=True)
    service_id: Mapped[int | None] = mapped_column(Integer, index=True)
    plan_key: Mapped[str | None] = mapped_column(String(80), index=True)
    remote_id: Mapped[str | None] = mapped_column(String(120), index=True)
    action: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    old_state_json: Mapped[dict | None] = mapped_column(JSON)
    new_state_json: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PasarguardRemoteSnapshot(Base):
    __tablename__ = "pasarguard_remote_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_type: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    source: Mapped[str] = mapped_column(String(80), default="pasarguard", nullable=False)
    state_json: Mapped[dict | list | None] = mapped_column(JSON)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)



