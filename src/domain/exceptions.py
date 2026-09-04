"""
Domain darajasidagi xatoliklar.

Bular HTTP xatoliklari emas ("404", "500") — bu "biznes qoidasi buzildi"
degan ma'noni bildiradi. interface/ qatlami (masalan webhook_app.py)
bularni tegishli HTTP javobiga aylantiradi, lekin domain/ va
application/ qatlami HTTP haqida hech narsa bilmaydi.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID


class DomainError(Exception):
    """Barcha domain xatoliklari uchun asosiy klass."""


class ConversationClosedError(DomainError):
    """Yopilgan suhbatga yangi xabar/eskalatsiya qo'shishga urinilganda."""

    def __init__(self, conversation_id: UUID) -> None:
        self.conversation_id = conversation_id
        super().__init__(f"Suhbat allaqachon yopilgan: {conversation_id}")


class ConversationNotFoundError(DomainError):
    def __init__(self, conversation_id: UUID) -> None:
        self.conversation_id = conversation_id
        super().__init__(f"Suhbat topilmadi: {conversation_id}")


class LeadAlreadyClaimedError(DomainError):
    """Suhbatni ikkinchi admin ham "claim" qilmoqchi bo'lganda."""

    def __init__(self, conversation_id: UUID) -> None:
        self.conversation_id = conversation_id
        super().__init__(
            f"Suhbat allaqachon boshqa admin tomonidan olingan: {conversation_id}"
        )


class ChannelMembershipRequiredError(DomainError):
    """Foydalanuvchi kanalga a'zo bo'lmasdan botdan foydalanmoqchi bo'lganda."""

    def __init__(self, telegram_id: int) -> None:
        self.telegram_id = telegram_id
        super().__init__(f"Foydalanuvchi kanalga a'zo emas: {telegram_id}")


class GuardrailBlockedError(DomainError):
    """
    AI tayyorlagan javob guardrail tekshiruvidan o'tmaganda.

    Bu xatolik chiqqanda, use case javobni foydalanuvchiga yubormaydi —
    o'rniga xavfsiz fallback javob + eskalatsiya taklif qilinadi.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Guardrail javobni bloklashdi: {reason}")


class RateLimitExceededError(DomainError):
    """Foydalanuvchi yoki kunlik global AI-byudjet limiti oshib ketganda."""

    def __init__(
        self,
        scope: str,
        *,
        reset_at: datetime | None = None,
        message: str | None = None,
    ) -> None:
        self.scope = scope
        self.reset_at = reset_at  # UTC datetime, limit tiklanadigan vaqt
        self.message = message  # Custom user-friendly error message
        super().__init__(f"Limit oshib ketdi: {scope}")
