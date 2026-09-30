"""Persistence models. Dates are UTC-aware on PostgreSQL and in SQLite tests."""
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(128))
    first_name: Mapped[str | None] = mapped_column(String(256))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    manual_quota_day: Mapped[datetime | None] = mapped_column(UTCDateTime)
    manual_checks_today: Mapped[int] = mapped_column(Integer, default=0)
    last_manual_check_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Story(Base):
    __tablename__ = "stories"
    __table_args__ = (
        CheckConstraint("status IN ('draft','active','paused','deleted')", name="ck_story_status"),
        CheckConstraint("check_frequency_hours > 0", name="ck_story_frequency"),
        CheckConstraint("monitoring_mode IN ('daily','intensive')", name="ck_story_monitoring_mode"),
        CheckConstraint("monitoring_mode != 'intensive' OR (intensive_started_at IS NOT NULL AND intensive_until IS NOT NULL)", name="ck_story_intensive_dates"),
        Index("uq_stories_user_intensive", "user_id", unique=True,
              postgresql_where=text("monitoring_mode = 'intensive' AND status IN ('active','paused')"),
              sqlite_where=text("monitoring_mode = 'intensive' AND status IN ('active','paused')")),
        Index("ix_stories_due", "status", "next_check_at"),
        Index("ix_stories_user_status", "user_id", "status"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"), nullable=False)
    title: Mapped[str] = mapped_column(String(160))
    original_input: Mapped[str] = mapped_column(Text)
    original_url: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    current_state: Mapped[str] = mapped_column(Text)
    entities: Mapped[list] = mapped_column(JSON, default=list)
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    search_queries: Mapped[list] = mapped_column(JSON, default=list)
    watch_goals: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="draft")
    check_frequency_hours: Mapped[int] = mapped_column(Integer, default=24)
    monitoring_mode: Mapped[str] = mapped_column(String(16), default="daily", server_default="daily")
    intensive_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    intensive_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_meaningful_update_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    next_check_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    lock_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    lock_token: Mapped[str | None] = mapped_column(String(64))
    last_manual_check_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Source(Base):
    __tablename__ = "sources"
    __table_args__ = (UniqueConstraint("story_id", "normalized_url", name="uq_source_story_url"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    story_id: Mapped[int] = mapped_column(ForeignKey("stories.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(Text)
    normalized_url: Mapped[str] = mapped_column(Text)
    domain: Mapped[str] = mapped_column(String(253))
    title: Mapped[str] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    content_hash: Mapped[str] = mapped_column(String(64))
    content_excerpt: Mapped[str] = mapped_column(Text)
    search_query: Mapped[str] = mapped_column(Text, default="")
    relevance_score: Mapped[float] = mapped_column(Float, default=0)
    is_duplicate: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class UserInterest(Base):
    """An explicit topic preference, independent of monitoring and usefulness votes."""
    __tablename__ = "user_interests"
    __table_args__ = (
        UniqueConstraint("user_id", "source_story_id", name="uq_interest_user_story"),
        Index("ix_interests_user_id", "user_id", "id"),
        {"sqlite_autoincrement": True},
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False)
    source_story_id: Mapped[int | None] = mapped_column(ForeignKey("stories.id", ondelete="SET NULL"))
    # Snapshot survives deleting the monitored story. Only an explicit removal
    # from the interests list deletes this preference and its topic description.
    title: Mapped[str] = mapped_column(String(160))
    summary: Mapped[str] = mapped_column(Text)
    entities: Mapped[list] = mapped_column(JSON, default=list)
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    source_url: Mapped[str | None] = mapped_column(Text)
    input_fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class StoryUpdate(Base):
    __tablename__ = "story_updates"
    __table_args__ = (Index("ix_updates_outbox", "notified_at", "delivery_locked_until"),
                     CheckConstraint("update_kind IN ('development','context')", name="ck_update_kind"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    story_id: Mapped[int] = mapped_column(ForeignKey("stories.id", ondelete="CASCADE"), index=True)
    summary: Mapped[str] = mapped_column(Text)
    new_facts: Mapped[list] = mapped_column(JSON, default=list)
    previous_state: Mapped[str] = mapped_column(Text)
    new_state: Mapped[str] = mapped_column(Text)
    importance_score: Mapped[float] = mapped_column(Float)
    confidence_score: Mapped[float] = mapped_column(Float)
    source_urls: Mapped[list] = mapped_column(JSON, default=list)
    reason: Mapped[str] = mapped_column(Text, default="")
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    update_kind: Mapped[str] = mapped_column(String(16), default="development", server_default="development")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    delivery_attempts: Mapped[int] = mapped_column(Integer, default=0)
    delivery_locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    delivery_lock_token: Mapped[str | None] = mapped_column(String(64))


class Feedback(Base):
    __tablename__ = "feedback"
    __table_args__ = (CheckConstraint("feedback_type IN ('useful','not_useful')", name="ck_feedback_kind"),)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"), primary_key=True)
    update_id: Mapped[int] = mapped_column(ForeignKey("story_updates.id", ondelete="CASCADE"), primary_key=True)
    story_id: Mapped[int] = mapped_column(ForeignKey("stories.id", ondelete="CASCADE"), index=True)
    feedback_type: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class UsageEvent(Base):
    __tablename__ = "usage_events"
    __table_args__ = (Index("ix_usage_operation_created", "operation", "created_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.telegram_id", ondelete="SET NULL"), index=True)
    story_id: Mapped[int | None] = mapped_column(ForeignKey("stories.id", ondelete="SET NULL"), index=True)
    operation: Mapped[str] = mapped_column(String(80))
    provider: Mapped[str] = mapped_column(String(80), default="")
    model: Mapped[str | None] = mapped_column(String(160))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost: Mapped[float] = mapped_column(Float, default=0)
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
