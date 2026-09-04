"""
EscalateConversation Use Case — Suhbatni admin yoki yurist guruhiga yo'naltirish.
Bu use case:
1. Suhbat holatini ESCALATED ga o'tkazadi.
2. NotifierPort orqali admin Telegram guruhiga bildirishnoma yuboradi.
3. Agar foydalanuvchi jiddiy mijoz (Lead) bo'lsa, Lead entity yaratib saqlaydi va alohida xabar beradi.
"""

from __future__ import annotations

from uuid import UUID

from domain.entities import Conversation, Lead, Message, User
from domain.exceptions import ConversationNotFoundError
from domain.value_objects import EscalationTarget, LeadScore, MessageSender
from infrastructure.notifications.telegram_admin_notifier import TelegramAdminNotifier
from infrastructure.persistence.sqlite_conversation_repo import SQLiteConversationRepo
from infrastructure.persistence.sqlite_lead_repo import SQLiteLeadRepo

# Bazadan o'qiladigan xabarlar: tozalangandan keyin 4 ta qolishi uchun zaxira bilan
_TRANSCRIPT_FETCH = 8
_TRANSCRIPT_SHOW = 4  # Admin ko'radigan maksimal xabarlar soni


def _build_transcript(messages: tuple[Message, ...]) -> str:
    """So'nggi xabarlardan o'qish qulay dialog transkripsiyasi yasaydi.

    Boshida bot/tizim xabarlari bo'lsa ularni olib tashlaymiz — transkripsiya
    har doim foydalanuvchi xabaridan boshlanishi kerak.
    """
    if not messages:
        return "(xabarlar mavjud emas)"

    # Boshida bot yoki tizim xabarlari bo'lsa olib tashlaymiz
    msgs = list(messages)
    while msgs and msgs[0].sender != MessageSender.USER:
        msgs.pop(0)

    # Ko'rsatish uchun so'nggi _TRANSCRIPT_SHOW ta xabar
    msgs = msgs[-_TRANSCRIPT_SHOW:]

    if not msgs:
        return "(xabarlar mavjud emas)"

    lines: list[str] = []
    for msg in msgs:
        if msg.sender == MessageSender.USER:
            prefix = "👤 Mijoz"
        elif msg.sender == MessageSender.AI:
            prefix = "🤖 Bot"
        else:
            prefix = "ℹ️ Tizim"
        # Uzun xabarlarni qisqartirish (400 belgi)
        body = msg.text if len(msg.text) <= 400 else msg.text[:397] + "..."
        lines.append(f"<b>{prefix}:</b> {body}")

    return "\n".join(lines)


class EscalateConversationUseCase:
    """Suhbatni admin/yuristlarga yo'naltirish (escalation) logikasi."""

    def __init__(
        self,
        conversation_repo: SQLiteConversationRepo,
        notifier: TelegramAdminNotifier,
        lead_repo: SQLiteLeadRepo,
    ) -> None:
        self._conversation_repo = conversation_repo
        self._notifier = notifier
        self._lead_repo = lead_repo

    async def execute(
        self,
        conversation_id: UUID,
        user: User,
        target: EscalationTarget,
        reason: str,
        user_message: str,
        lead_score: LeadScore | None = None,
    ) -> Conversation:
        """
        Suhbatni eskalatsiya qiladi va bildirishnomalarni yuboradi.
        Args:
            conversation_id: Eskalatsiya qilinishi kerak bo'lgan suhbat IDsi.
            user: Suhbat egasi bo'lgan foydalanuvchi.
            target: Yo'naltirilayotgan manzil (GENERAL_SUPPORT yoki LEAD).
            reason: Eskalatsiya sababi (izoh).
            lead_score: Lead bahosi (mavjud bo'lsa).
        Returns:
            Yangilangan Conversation obyekti.
        """
        # 1. Bazadan suhbatni olish
        conversation = await self._conversation_repo.get(conversation_id)
        if conversation is None:
            raise ConversationNotFoundError(conversation_id)

        # 2. Domain qoidasiga binoan suhbat statusini o'zgartirish
        conversation.escalate(target)

        # 3. Suhbatning yangilanagan holatini saqlash
        await self._conversation_repo.save(conversation)

        # 4. Admin guruhiga notification yuborish — so'nggi xabarlarni ko'rsatish
        recent = await self._conversation_repo.get_recent_messages(
            conversation.id, limit=_TRANSCRIPT_FETCH
        )
        transcript = _build_transcript(recent)

        # 5. Lead shakllantirish sharti tekshiruvi
        is_lead_candidate = target == EscalationTarget.LEAD or (
            lead_score is not None and lead_score.is_lead
        )

        if is_lead_candidate:
            score = lead_score or LeadScore(
                value=0.85,
                reasons=("User qualified for advocate handoff.",),
            )
            lead = await self._lead_repo.get_by_user(user.id)
            if lead is None:
                # Yangi Lead entity yaratish
                lead = Lead.new(
                    conversation_id=conversation.id,
                    user_id=user.id,
                    score=score,
                    topic_summary=user_message,
                    contact_info=f"@{user.username}" if user.username else None,
                )
            else:
                # Mavjud leadni yangi ma'lumotlar bilan yangilash
                lead.update_profile(
                    conversation_id=conversation.id,
                    topic_summary=user_message,
                    contact_info=f"@{user.username}" if user.username else None,
                )
            # Leadni bazaga saqlash
            await self._lead_repo.save(lead)

            # Yuristlar uchun alohida Lead notification yuborish
            await self._notifier.notify_new_lead(
                lead=lead,
                user=user,
                conversation=conversation,
            )

        return conversation
