"""
database.py — SQLite bilan aloqaning texnik asosi: engine,
session factory, va ORM modellari (jadval ta'riflari).

MUHIM QOIDA: bu fayldagi klasslarda (ConversationModel va h.k.)
HECH QANDAY METOD YO'Q — faqat ustunlar. Biznes qoidasi (escalate,
assign_admin va h.k.) faqat domain/entities.py'dagi Conversation'da.
Bu ikkalasini repo fayllari (sqlite_conversation_repo.py) bir-biriga
tarjima qiladi.
"""

from __future__ import annotations

import os
import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, String, Text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from config.settings import Settings


class Base(DeclarativeBase):
    pass


class UserModel(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    has_joined_channel: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ConversationModel(Base):
    __tablename__ = "conversations"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(index=True)
    user_telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(32))
    assigned_admin_telegram_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    message_count: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MessageModel(Base):
    __tablename__ = "messages"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id"), index=True
    )
    sender: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class LeadModel(Base):
    __tablename__ = "leads"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id"), index=True
    )
    user_id: Mapped[UUID] = mapped_column(unique=True, index=True)
    score_value: Mapped[float]
    score_reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    topic_summary: Mapped[str] = mapped_column(Text)
    contact_info: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="new")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RateLimitModel(Base):
    __tablename__ = "rate_limits"

    user_id: Mapped[UUID] = mapped_column(primary_key=True)
    message_count: Mapped[int] = mapped_column(default=0)
    last_reset: Mapped[datetime] = mapped_column(DateTime())
    # Burst detection: track recent message timestamps (JSON array)
    recent_timestamps: Mapped[str] = mapped_column(Text, default="[]")
    # Violation tracking for progressive penalties
    violation_count: Mapped[int] = mapped_column(default=0)
    last_violation_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AdminNotificationModel(Base):
    """Admin guruhiga yuborilgan bot xabari → foydalanuvchi IDsi xaritalash.

    Bu jadval TelegramAdminNotifier'ning in-memory registrini
    persistent qiladi — konteyner qayta ishga tushganda ham
    adminlar eski xabarlarga reply qila oladi.
    """

    __tablename__ = "admin_notifications"

    # Telegram message_id (admin guruhida bot yuborgan xabar)
    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    # "@username" yoki "Ism Familiya" — tasdiq xabarida ko'rsatiladi
    display_name: Mapped[str] = mapped_column(String(128))
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # Conversation ID for history toggle callbacks
    conversation_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    # Notification type for re-rendering (new_lead, user_followup, escalation)
    notification_type: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )


def create_engine(settings: Settings) -> AsyncEngine:
    # SQLite fayl bazasi yaratish
    db_path = Path("data/lawyer_bot.db")
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Set restrictive permissions on existing database file
    if db_path.exists():
        try:
            os.chmod(
                db_path, stat.S_IRUSR | stat.S_IWUSR
            )  # 600 - read/write for owner only
        except OSError:
            # Log warning but don't fail if permissions can't be set
            pass

    db_url = f"sqlite+aiosqlite:///{db_path}"
    return create_async_engine(db_url)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """
    Har bir use case chaqiruvi uchun bitta transaction: muvaffaqiyatli
    bo'lsa commit, xatolik chiqsa avtomatik rollback — yarim yozilgan
    ma'lumot bazada qolib ketmaydi.
    """
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
