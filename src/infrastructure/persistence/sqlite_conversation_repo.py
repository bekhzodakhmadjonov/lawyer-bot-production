"""
SQLiteConversationRepo — ConversationRepoPort'ning SQLite orqali
amalga oshirilishi.

Bu fayl ikkita dunyoni bog'laydi: domain (Conversation, Message —
metodli, sof Python) va SQLite (ConversationModel, MessageModel —
faqat ustunlar). Tarjima har doim shu yerda, boshqa hech qayerda.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.entities import Conversation, Message
from domain.value_objects import ConversationStatus, MessageSender
from infrastructure.persistence.database import ConversationModel, MessageModel


@dataclass(frozen=True, slots=True)
class ConversationStats:
    total_conversations: int
    active_ai_conversations: int
    escalated_conversations: int
    closed_conversations: int
    total_messages: int
    conversations_since: int


class SQLiteConversationRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, conversation_id: UUID) -> Conversation | None:
        model = await self._session.get(ConversationModel, conversation_id)
        return self._to_entity(model) if model else None

    async def get_active_for_user(self, user_id: UUID) -> Conversation | None:
        stmt = (
            select(ConversationModel)
            .where(
                ConversationModel.user_id == user_id,
                ConversationModel.status != ConversationStatus.CLOSED.value,
            )
            .order_by(ConversationModel.created_at.desc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return self._to_entity(model) if model else None

    async def save(self, conversation: Conversation) -> None:
        # merge — mavjud bo'lsa yangilaydi, bo'lmasa yaratadi (upsert).
        await self._session.merge(self._to_model(conversation))
        await self._session.flush()

    async def add_message(self, message: Message) -> None:
        self._session.add(self._message_to_model(message))
        await self._session.flush()

        # Increment message count in conversation
        conversation_model = await self._session.get(
            ConversationModel, message.conversation_id
        )
        if conversation_model:
            conversation_model.message_count = (
                conversation_model.message_count or 0
            ) + 1
            await self._session.flush()

    async def get_recent_messages(
        self, conversation_id: UUID, limit: int = 20
    ) -> tuple[Message, ...]:
        stmt = (
            select(MessageModel)
            .where(MessageModel.conversation_id == conversation_id)
            .order_by(MessageModel.sent_at.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        models = result.scalars().all()
        # Postgres'dan eng yangidan keldi, lekin LLM'ga eng eskidan
        # berish kerak — shuning uchun teskari qaytaramiz.
        return tuple(self._message_to_entity(m) for m in reversed(models))

    async def get_stats(self, *, since: datetime) -> ConversationStats:
        """Admin dashboard uchun yengil aggregate statistikalar."""
        total_conversations = await self._count_conversations()
        active_ai_conversations = await self._count_conversations(
            status=ConversationStatus.AI_HANDLED
        )
        escalated_leads = await self._count_conversations(
            status=ConversationStatus.ESCALATED_LEAD
        )
        escalated_general = await self._count_conversations(
            status=ConversationStatus.ESCALATED_GENERAL
        )
        closed_conversations = await self._count_conversations(
            status=ConversationStatus.CLOSED
        )
        total_messages = await self._count_messages()
        conversations_since = await self._count_conversations(created_since=since)

        return ConversationStats(
            total_conversations=total_conversations,
            active_ai_conversations=active_ai_conversations,
            escalated_conversations=escalated_leads + escalated_general,
            closed_conversations=closed_conversations,
            total_messages=total_messages,
            conversations_since=conversations_since,
        )

    async def _count_conversations(
        self,
        *,
        status: ConversationStatus | None = None,
        created_since: datetime | None = None,
    ) -> int:
        stmt = select(func.count()).select_from(ConversationModel)
        if status is not None:
            stmt = stmt.where(ConversationModel.status == status.value)
        if created_since is not None:
            stmt = stmt.where(ConversationModel.created_at >= created_since)
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def _count_messages(self) -> int:
        result = await self._session.execute(
            select(func.count()).select_from(MessageModel)
        )
        return int(result.scalar_one())

    # --- Tarjima metodlari (bu klass ichidagi maxsus yordamchilar) ---

    @staticmethod
    def _to_entity(model: ConversationModel) -> Conversation:
        return Conversation(
            id=model.id,
            user_id=model.user_id,
            user_telegram_id=model.user_telegram_id,
            status=ConversationStatus(model.status),
            assigned_admin_telegram_id=model.assigned_admin_telegram_id,
            message_count=model.message_count or 0,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    @staticmethod
    def _to_model(conversation: Conversation) -> ConversationModel:
        return ConversationModel(
            id=conversation.id,
            user_id=conversation.user_id,
            user_telegram_id=conversation.user_telegram_id,
            status=conversation.status.value,
            assigned_admin_telegram_id=conversation.assigned_admin_telegram_id,
            message_count=conversation.message_count,
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
        )

    @staticmethod
    def _message_to_model(message: Message) -> MessageModel:
        return MessageModel(
            id=message.id,
            conversation_id=message.conversation_id,
            sender=message.sender.value,
            text=message.text,
            sent_at=message.sent_at,
        )

    @staticmethod
    def _message_to_entity(model: MessageModel) -> Message:
        return Message(
            id=model.id,
            conversation_id=model.conversation_id,
            sender=MessageSender(model.sender),
            text=model.text,
            sent_at=model.sent_at,
        )
