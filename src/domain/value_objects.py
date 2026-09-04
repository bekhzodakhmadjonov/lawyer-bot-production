"""
Value objects — o'zgarmas (immutable) qiymatlar va enum'lar.

Bu fayl domain/entities.py va application/ qatlamining har ikkalasi
tomonidan ishlatiladi, lekin o'zi hech narsaga (Postgres, Telegram, AI
provayderlariga) bog'liq emas.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import ClassVar


class MessageSender(str, Enum):
    USER = "user"
    AI = "ai"
    ADMIN = "admin"
    SYSTEM = "system"


class ConversationStatus(str, Enum):
    """Suhbat hozir kim tomonidan boshqarilyapti."""

    AI_HANDLED = "ai_handled"
    ESCALATED_LEAD = "escalated_lead"  # yurist (lead admin) kutmoqda/javob bermoqda
    ESCALATED_GENERAL = "escalated_general"  # umumiy admin kutmoqda
    CLOSED = "closed"


class EscalationTarget(str, Enum):
    """Eskalatsiya qaysi admin guruhga/topic'ga yo'naltirilishi kerak."""

    LEAD = "lead"


class LeadStatus(str, Enum):
    """Admin pipeline'dagi lead holati."""

    NEW = "new"
    CONTACTED = "contacted"
    BOOKED = "booked"
    PAID = "paid"
    LOST = "lost"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class LeadScore:
    """
    Foydalanuvchining "jiddiy mijoz nomzodi" ekanligini ko'rsatuvchi baho.

    value: 0.0 (umuman lead emas) dan 1.0 (aniq lead) gacha.
    reasons: nega shu baho qo'yilgani (guardrail/klassifikator izohlari) —
             admin ko'rib chiqqanda tushunarli bo'lishi uchun.
    """

    LEAD_THRESHOLD: ClassVar[float] = 0.6

    value: float
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not 0.0 <= self.value <= 1.0:
            raise ValueError(
                f"LeadScore.value 0.0-1.0 oralig'ida bo'lishi kerak: {self.value}"
            )

    @property
    def is_lead(self) -> bool:
        return self.value >= self.LEAD_THRESHOLD
