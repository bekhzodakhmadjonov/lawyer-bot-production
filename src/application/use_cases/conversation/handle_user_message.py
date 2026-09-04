"""
HandleUserMessage Use Case — Foydalanuvchining har bir kelgan xabarini qayta ishlash.

Bu loyihaning markaziy biznes logikasidir (AI Pipeline):
1. Rate limit tekshiruvi.
2. Suhbat holatini aniqlash (AI javob beradimi yoki Admin).
3. GPT-4o-mini orqali javob tayyorlash (GPT o'zi qidiruvni boshqaradi).
4. Eskalatsiya va Lead boshqaruvi.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog

from application.use_cases.conversation.escalate_conversation import (
    EscalateConversationUseCase,
)
from domain.entities import Conversation, Message, User
from domain.exceptions import ChannelMembershipRequiredError, RateLimitExceededError
from domain.value_objects import (
    ConversationStatus,
    EscalationTarget,
    MessageSender,
)
from infrastructure.ai.openai_chat_adapter import (
    ConversationTurn,
    OpenAIChatAdapter,
)
from infrastructure.notifications.telegram_admin_notifier import TelegramAdminNotifier
from infrastructure.persistence.sqlite_conversation_repo import SQLiteConversationRepo
from infrastructure.persistence.sqlite_lead_repo import SQLiteLeadRepo
from infrastructure.persistence.sqlite_rate_limiter import SQLiteRateLimiter
from infrastructure.security.logging_utils import mask_message, mask_telegram_id

logger = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class IntakeSnapshot:
    """Lightweight intake state derived from the recent conversation."""

    has_problem: bool
    has_location: bool
    has_documents: bool
    has_urgency: bool
    lawyer_request_count: int
    user_turn_count: int

    @property
    def collected_field_count(self) -> int:
        return sum(
            (
                self.has_problem,
                self.has_location,
                self.has_documents,
                self.has_urgency,
            )
        )

    @property
    def is_ready_for_handoff(self) -> bool:
        if self.has_urgency and self.has_problem:
            return True
        if self.lawyer_request_count >= 2 and self.has_problem:
            return True
        return self.has_problem and self.collected_field_count >= 3


class HandledUserMessageUseCase:
    """Foydalanuvchi xabarlarini qayta ishlaydigan asosiy Use Case."""

    _AI_FAILURE_FALLBACK = (
        "Kechirasiz, hozir texnik sababga ko'ra javob tayyorlay olmadim. "
        "So'rovingiz mutaxassisimizga yuborildi, tez orada aloqaga chiqishadi."
    )
    _AI_FAILURE_ESCALATION_REASON = "AI provider unavailable."

    _RETURN_TO_AI_KEYWORDS = frozenset(
        {"ai javob bersin", "ai", "bot", "botga qaytish", "/start"}
    )

    _LAWYER_REQUEST_PHRASES = frozenset(
        {
            # O'zbekcha — to'g'ridan-to'g'ri so'rovlar
            "advokat kerak",
            "yurist kerak",
            "huquqshunos kerak",
            "advokat bilan gaplash",
            "yurist bilan gaplash",
            "advokatga ul",
            "yuristga ul",
            "bog'lang",
            "boglang",
            "ulab qo'ying",
            "ulab qoying",
            "konsultatsiya kerak",
            "maslahat kerak",
            "telefon qiling",
            "aloqaga chiqing",
            # Qo'shimcha o'zbekcha iboralar
            "advokat top",
            "yurist top",
            "aloqa qil",
            "bog'lanib bering",
            "ulab bering",
            "murojaat qilay",
            "uchrashuv",
            "uchrashmoqchi",
            "men advokat",
            "sizga kelamiz",
            "ofisga kelaman",
            "kelishib olaylik",
            # Ruscha
            "адвокат нужен",
            "юрист нужен",
            "свяжите",
            "консультация нужна",
            "запишитесь",
            "позвоните",
            "назначьте встречу",
        }
    )
    _LEGAL_PROBLEM_HINTS = frozenset(
        {
            "sud",
            "ajrash",
            "aliment",
            "qarz",
            "shartnoma",
            "meros",
            "mulk",
            "uy",
            "yer",
            "jinoyat",
            "qam",
            "jarima",
            "soliq",
            "ishdan",
            "oylik",
            "kompensatsiya",
            "hujjat",
            "dalil",
            "арест",
            "суд",
            "развод",
            "алименты",
            "долг",
            "договор",
            "наследство",
            "штраф",
            "налог",
            "увол",
            "зарплат",
        }
    )
    _LOCATION_HINTS = frozenset(
        {
            "toshkent",
            "chilonzor",
            "samarqand",
            "buxoro",
            "andijon",
            "farg'ona",
            "fargona",
            "namangan",
            "navoiy",
            "jizzax",
            "sirdaryo",
            "qashqadaryo",
            "surxondaryo",
            "xorazm",
            "nukus",
            "qoraqalpog",
            "ташкент",
            "самарканд",
            "бухара",
            "андижан",
            "фергана",
            "наманган",
            "город",
            "вилоят",
            "viloyat",
        }
    )
    _DOCUMENT_HINTS = frozenset(
        {
            "hujjat",
            "shartnoma",
            "qaror",
            "ariza",
            "dalil",
            "rasm",
            "video",
            "audio",
            "chek",
            "tilxat",
            "pasport",
            "документ",
            "договор",
            "решение",
            "заявление",
            "доказ",
            "расписка",
        }
    )
    _URGENCY_HINTS = frozenset(
        {
            "bugun",
            "ertaga",
            "tez",
            "shoshilinch",
            "muddat",
            "sud ertaga",
            "bu hafta",
            "hozir",
            "срочно",
            "сегодня",
            "завтра",
            "срок",
            "на этой неделе",
        }
    )
    _INFORMATIONAL_LAWYER_QUESTION_PATTERNS = tuple(
        re.compile(pattern)
        for pattern in (
            r"\bnega\b",
            r"\bnima uchun\b",
            r"\bkim\b",
            r"\bqanday\b",
            r"\bkerakmi\b",
            r"\bkerak\?",
            r"\bnima\b.*\bkerak\b",
            r"\bпочему\b",
            r"\bзачем\b",
            r"\bкто\b",
            r"\bнужен\s+ли\b",
        )
    )

    # AI javobida shu so'zlar bo'lsa — AI o'zi eskalatsiya qilmoqchi
    _AI_ESCALATION_SIGNALS = frozenset(
        {
            "ulayapman",
            "sizni bog'layman",
            "sizni yo'naltiraman",
            "bog'lanishadi",
            "tez orada aloqaga",
            "jamoamiz a'zosiga",
            "so'rovingiz advokatimizga yuborildi",
        }
    )

    def __init__(
        self,
        conversation_repo: SQLiteConversationRepo,
        rate_limiter: SQLiteRateLimiter,
        chat_llm: OpenAIChatAdapter,
        escalate_conversation: EscalateConversationUseCase,
        notifier: TelegramAdminNotifier,
        lead_repo: SQLiteLeadRepo,
    ) -> None:
        self._conversation_repo = conversation_repo
        self._rate_limiter = rate_limiter
        self._chat_llm = chat_llm
        self._escalate_conversation = escalate_conversation
        self._notifier = notifier
        self._lead_repo = lead_repo

    async def execute(self, user: User, message_text: str) -> str:
        """Xabarni qayta ishlaydi va foydalanuvchiga yuboriladigan matnni qaytaradi."""
        # 1. Rate-limit tekshiruvi
        (
            is_allowed,
            reset_at,
            reason,
        ) = await self._rate_limiter.check_and_increment_user(user.id)
        if not is_allowed:
            if reason == "burst":
                # Burst limit exceeded - progressive penalty
                if reset_at:
                    cooldown_minutes = int(
                        (reset_at - datetime.now(UTC)).total_seconds() / 60
                    )
                    if cooldown_minutes == 0:
                        raise RateLimitExceededError(
                            "user",
                            reset_at=reset_at,
                            message="⚠️ Juda tez xabar yuboryapsiz. Iltimos, sekinroq yuboring.",
                        )
                    else:
                        raise RateLimitExceededError(
                            "user",
                            reset_at=reset_at,
                            message=f"⚠️ Juda tez xabar yuboryapsiz. {cooldown_minutes} daqiqadan keyin yana urinib ko'ring.",
                        )
            elif reason == "hourly":
                # Hourly limit exceeded
                raise RateLimitExceededError("user", reset_at=reset_at)
            else:
                # Fallback
                raise RateLimitExceededError("user", reset_at=reset_at)

        # 2. Majburiy kanal a'zolari tekshiruvi
        if not user.has_joined_channel:
            raise ChannelMembershipRequiredError(user.telegram_id)

        # 3. Foydalanuvchining ochiq suhbatini olish yoki yangi boshlash
        conversation = await self._conversation_repo.get_active_for_user(user.id)
        if conversation is None:
            conversation = Conversation.start(
                user.id, user_telegram_id=user.telegram_id
            )
            await self._conversation_repo.save(conversation)

        # 4. Foydalanuvchi xabarini bazaga yozish
        user_msg = Message.new(
            conversation_id=conversation.id,
            sender=MessageSender.USER,
            text=message_text,
        )
        await self._conversation_repo.add_message(user_msg)

        # 5. Suhbat tarixini yuklash va intake holatini baholash
        recent_messages = await self._conversation_repo.get_recent_messages(
            conversation.id, limit=16
        )
        history = self._build_history(recent_messages[:-1])
        intake = self._build_intake_snapshot(recent_messages)

        # 6. Eskalatsiya holatidagi suhbatni boshqarish
        if conversation.status == ConversationStatus.ESCALATED_LEAD:
            text_normalized = message_text.lower().strip()
            if text_normalized in self._RETURN_TO_AI_KEYWORDS:
                conversation.return_to_ai()
                await self._conversation_repo.save(conversation)
                await self._notifier.notify_returned_to_ai(conversation)
                return "🤖 Suhbatingiz AI yordamchiga qaytarildi."
            else:
                await self._notifier.notify_user_followup(
                    conversation=conversation,
                    user=user,
                    message_text=message_text,
                )
                return (
                    "✉️ <b>Xabaringiz mutaxassisga yuborildi.</b>\n\n"
                    "Jasurbek advokat jamoasi ko'rib chiqadi — "
                    "javob tez orada keladi."
                )

        # 7. Advokat so'rovi tekshiruvi
        if self._should_escalate_lawyer_request(message_text, intake):
            logger.info(
                "Lawyer request detected",
                user_id=mask_telegram_id(user.telegram_id),
                message=mask_message(message_text),
                intake=intake,
            )
            await self._escalate_with_profile(
                conversation=conversation,
                user=user,
                message_text=message_text,
                reason="Foydalanuvchi advokat so'radi",
            )
            return (
                "✅ So'rovingiz advokatimizga yuborildi.\n\n"
                "Jasurbek advokat jamoasi vaziyatingizni ko'rib chiqadi va "
                "tez orada siz bilan bog'lanadi. "
                "Agar AI yordamchiga qaytmoqchi bo'lsangiz, "
                "quyidagi tugmani bosing."
            )

        # 8. GPT dan javob olish
        # GPT o'zi qaror qiladi: qidiruv kerakmi yoki yo'qmi.
        # Qidiruv kerak bo'lsa — GPT tavily_search tool ni chaqiradi,
        # natijani o'qiydi va foydalanuvchiga aniq javob beradi.
        try:
            llm_response = await self._chat_llm.answer(
                user_message=message_text,
                history=history,
            )
        except Exception:
            logger.exception("LLM generation failed")
            return await self._handle_ai_failure(conversation, user, message_text)

        # 9. AI javobini saqlash
        ai_msg = Message.new(
            conversation_id=conversation.id,
            sender=MessageSender.AI,
            text=llm_response.text,
        )
        await self._conversation_repo.add_message(ai_msg)

        # 10. AI javobida eskalatsiya signali bormi tekshirish
        ai_text_lower = llm_response.text.lower()
        if any(signal in ai_text_lower for signal in self._AI_ESCALATION_SIGNALS):
            logger.info(
                "AI response contains escalation signal",
                user_id=mask_telegram_id(user.telegram_id),
            )
            await self._escalate_with_profile(
                conversation=conversation,
                user=user,
                message_text=message_text,
                reason="AI javobida eskalatsiya signali aniqlandi",
            )

        return llm_response.text

    # ──────────── Lawyer request detection ────────────

    def _should_escalate_lawyer_request(
        self,
        message_text: str,
        intake: IntakeSnapshot,
    ) -> bool:
        normalized = self._normalize_text(message_text)
        if not any(phrase in normalized for phrase in self._LAWYER_REQUEST_PHRASES):
            return False

        # Check if this is an informational question about lawyers
        if any(
            pattern.search(normalized)
            for pattern in self._INFORMATIONAL_LAWYER_QUESTION_PATTERNS
        ):
            return False

        if not intake.is_ready_for_handoff:
            logger.info(
                "Lawyer request but intake is not ready",
                intake=intake,
            )
            return False

        return True

    # ──────────── Helpers ────────────

    @staticmethod
    def _normalize_text(text: str) -> str:
        normalized = text.lower().strip()
        normalized = (
            normalized.replace("\u2018", "'").replace("`", "'").replace("\u02bc", "'")
        )
        return re.sub(r"\s+", " ", normalized)

    @classmethod
    def _build_history(
        cls, messages: tuple[Message, ...]
    ) -> tuple[ConversationTurn, ...]:
        return tuple(
            ConversationTurn(
                role="user" if m.sender == MessageSender.USER else "assistant",
                text=m.text,
            )
            for m in messages
            if m.sender in (MessageSender.USER, MessageSender.AI)
        )

    @classmethod
    def _build_intake_snapshot(cls, messages: tuple[Message, ...]) -> IntakeSnapshot:
        user_texts = [
            cls._normalize_text(m.text)
            for m in messages
            if m.sender == MessageSender.USER
        ]
        combined = " ".join(user_texts)
        lawyer_request_count = sum(
            1
            for text in user_texts
            if any(phrase in text for phrase in cls._LAWYER_REQUEST_PHRASES)
            and not any(
                pattern.search(text)
                for pattern in cls._INFORMATIONAL_LAWYER_QUESTION_PATTERNS
            )
        )

        has_problem = any(hint in combined for hint in cls._LEGAL_PROBLEM_HINTS) or any(
            len(text.split()) >= 7 for text in user_texts
        )

        return IntakeSnapshot(
            has_problem=has_problem,
            has_location=any(hint in combined for hint in cls._LOCATION_HINTS),
            has_documents=any(hint in combined for hint in cls._DOCUMENT_HINTS),
            has_urgency=any(hint in combined for hint in cls._URGENCY_HINTS),
            lawyer_request_count=lawyer_request_count,
            user_turn_count=len(user_texts),
        )

    async def _handle_ai_failure(
        self,
        conversation: Conversation,
        user: User,
        message_text: str,
    ) -> str:
        """AI ishlamay qolganda eskalatsiyaga o'tkazish."""
        await self._escalate_conversation.execute(
            conversation_id=conversation.id,
            user=user,
            target=EscalationTarget.LEAD,
            reason=self._AI_FAILURE_ESCALATION_REASON,
            user_message=message_text,
        )
        await self._conversation_repo.add_message(
            Message.new(
                conversation_id=conversation.id,
                sender=MessageSender.SYSTEM,
                text=self._AI_FAILURE_FALLBACK,
            )
        )
        return self._AI_FAILURE_FALLBACK

    async def _escalate_with_profile(
        self,
        conversation: Conversation,
        user: User,
        message_text: str,
        reason: str,
    ) -> None:
        """Suhbat tarixidan mijoz anketasini yasab, eskalatsiya qiladi."""
        recent_messages = await self._conversation_repo.get_recent_messages(
            conversation.id, limit=15
        )
        history = tuple(
            ConversationTurn(
                role="user" if m.sender == MessageSender.USER else "assistant",
                text=m.text,
            )
            for m in recent_messages
            if m.sender in (MessageSender.USER, MessageSender.AI)
        )

        # AI orqali mijoz anketasini shakllantiramiz
        profile = await self._chat_llm.extract_lead_profile(
            history=history, username=user.username
        )

        profile_text = (
            f"👤 <b>Ism:</b> {profile.name}\n"
            f"📍 <b>Hudud:</b> {profile.location}\n"
            f"📞 <b>Telefon:</b> {profile.phone_number}\n"
            f"⚖️ <b>Sohasi:</b> {profile.category}\n"
            f"🔥 <b>Muhimlik:</b> {profile.urgency}\n"
            f"📄 <b>Hujjatlar:</b> {profile.documents_mentioned}\n\n"
            f"📝 <b>Muammo:</b> {profile.problem_summary}"
        )

        await self._escalate_conversation.execute(
            conversation_id=conversation.id,
            user=user,
            target=EscalationTarget.LEAD,
            user_message=profile_text,
            reason=reason,
        )
