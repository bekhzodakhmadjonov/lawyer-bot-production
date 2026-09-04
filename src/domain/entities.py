"""
Entity'lar — loyihaning asosiy biznes obyektlari.

Bu yerda Telegram, Postgres yoki AI provayderiga oid hech qanday narsa
yo'q — faqat "suhbat nima", "lead nima" degan sof tushunchalar.
Har bir entity o'zining minimal ichki qoidalarini o'zi qo'riqlaydi
(masalan yopiq suhbatga xabar qo'shib bo'lmaydi).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from domain.exceptions import ConversationClosedError, LeadAlreadyClaimedError
from domain.value_objects import (
    ConversationStatus,
    EscalationTarget,
    LeadScore,
    LeadStatus,
    MessageSender,
)


@dataclass(slots=True)
class User:
    """Telegram foydalanuvchisi."""

    id: UUID
    telegram_id: int
    username: str | None
    first_name: str | None = None
    last_name: str | None = None
    has_joined_channel: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def new(
        cls,
        telegram_id: int,
        username: str | None,
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> User:
        return cls(
            id=uuid4(),
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
        )


@dataclass(slots=True)
class Message:
    """Suhbatdagi bitta xabar (foydalanuvchi, AI yoki admin tomonidan)."""

    id: UUID
    conversation_id: UUID
    sender: MessageSender
    text: str
    sent_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def new(
        cls,
        conversation_id: UUID,
        sender: MessageSender,
        text: str,
    ) -> Message:
        return cls(
            id=uuid4(),
            conversation_id=conversation_id,
            sender=sender,
            text=text,
        )


@dataclass(slots=True)
class Conversation:
    """Bitta foydalanuvchi bilan bo'lgan butun suhbat oqimi."""

    id: UUID
    user_id: UUID
    user_telegram_id: int | None = None
    status: ConversationStatus = ConversationStatus.AI_HANDLED
    assigned_admin_telegram_id: int | None = None
    message_count: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def start(cls, user_id: UUID, *, user_telegram_id: int) -> Conversation:
        return cls(id=uuid4(), user_id=user_id, user_telegram_id=user_telegram_id)

    def escalate(self, target: EscalationTarget) -> None:
        if self.status == ConversationStatus.CLOSED:
            raise ConversationClosedError(self.id)
        self.status = (
            ConversationStatus.ESCALATED_LEAD
            if target == EscalationTarget.LEAD
            else ConversationStatus.ESCALATED_GENERAL
        )
        self.updated_at = datetime.now(UTC)

    def assign_admin(self, admin_telegram_id: int) -> None:
        if self.assigned_admin_telegram_id is not None:
            raise LeadAlreadyClaimedError(self.id)
        self.assigned_admin_telegram_id = admin_telegram_id
        self.updated_at = datetime.now(UTC)

    def return_to_ai(self) -> None:
        self.status = ConversationStatus.AI_HANDLED
        self.assigned_admin_telegram_id = None
        self.updated_at = datetime.now(UTC)

    def close(self) -> None:
        self.status = ConversationStatus.CLOSED
        self.updated_at = datetime.now(UTC)


@dataclass(slots=True)
class Lead:
    """Real mijoz nomzodi sifatida qayd etilgan suhbat."""

    id: UUID
    conversation_id: UUID
    user_id: UUID
    score: LeadScore
    topic_summary: str
    contact_info: str | None = None
    status: LeadStatus = LeadStatus.NEW
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def new(
        cls,
        conversation_id: UUID,
        user_id: UUID,
        score: LeadScore,
        topic_summary: str,
        contact_info: str | None = None,
    ) -> Lead:
        return cls(
            id=uuid4(),
            conversation_id=conversation_id,
            user_id=user_id,
            score=score,
            topic_summary=topic_summary,
            contact_info=contact_info,
        )

    def mark_status(self, status: LeadStatus) -> None:
        self.status = status

    def update_profile(
        self,
        conversation_id: UUID,
        topic_summary: str,
        contact_info: str | None = None,
    ) -> None:
        """Update lead with new conversation data."""
        self.conversation_id = conversation_id
        self.topic_summary = topic_summary
        if contact_info:
            self.contact_info = contact_info
        self.last_updated_at = datetime.now(UTC)
