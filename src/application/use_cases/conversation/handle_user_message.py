"""
HandleUserMessage Use Case — Foydalanuvchining har bir kelgan xabarini qayta ishlash.

Bu loyihaning markaziy biznes logikasidir (AI Pipeline):
1. Rate limit tekshiruvi.
2. Suhbat holatini aniqlash (AI javob beradimi yoki Admin).
3. Gemini orqali javob tayyorlash (Google Search bilan).
4. Lead qualification va eskalatsiya.
"""

from __future__ import annotations

from datetime import UTC, datetime

import re

import structlog

from application.use_cases.conversation.escalate_conversation import (
    EscalateConversationUseCase,
)
from application.scoring import DynamicLeadScorer
from application.context import ContextAwareResponseGenerator
from domain.entities import Conversation, Message, User
from domain.exceptions import ChannelMembershipRequiredError, RateLimitExceededError
from domain.value_objects import (
    ConversationStatus,
    EscalationTarget,
    MessageSender,
)
from infrastructure.ai.gemini_chat_adapter import (
    ConversationTurn,
    GeminiChatAdapter,
)
from infrastructure.notifications.telegram_admin_notifier import TelegramAdminNotifier
from infrastructure.persistence.postgres_conversation_repo import PostgresConversationRepo
from infrastructure.persistence.postgres_lead_repo import PostgresLeadRepo
from infrastructure.persistence.redis_rate_limiter import RedisRateLimiter
from infrastructure.security.logging_utils import mask_message, mask_telegram_id

logger = structlog.get_logger()


class HandledUserMessageUseCase:
    """Foydalanuvchi xabarlarini qayta ishlaydigan asosiy Use Case."""

    _AI_FAILURE_FALLBACK = (
        "Kechirasiz, hozir texnik sababga ko'ra javob tayyorlay olmadim. "
        "So'rovingiz mutaxassisga yuborildi, tez orada aloqaga chiqishadi."
    )
    _AI_FAILURE_ESCALATION_REASON = "AI provider unavailable."

    _RETURN_TO_AI_KEYWORDS = frozenset(
        {"ai javob bersin", "ai", "bot", "botga qaytish", "/start"}
    )

    # Legal response cache settings
    _CACHE_TTL_SECONDS = 86400  # 24 hours
    _CACHE_PREFIX = "legal_cache"

    # Pattern to extract "kodeks_name + modda_number" from user queries
    _LEGAL_ARTICLE_PATTERN = re.compile(
        r"(oila|mehnat|jinoyat|fuqarolik|ma'muriy|soliq|yer|bojxona|"
        r"konstitutsiya|turar-joy|budjet)\s*"
        r"(?:kodeksi(?:ning)?|kodeks)?\s*"
        r"(\d{1,4})\s*-?\s*modd",
        re.IGNORECASE,
    )

    @staticmethod
    def _normalize_legal_query(message: str) -> str | None:
        """Yuridik modda so'rovini normalized cache kalitiga aylantiradi.

        Examples:
            'Oila kodeksi 40 modda' -> 'oila_kodeksi:40'
            'mehnat kodeksining 172-moddasi nima?' -> 'mehnat_kodeksi:172'
            'salom' -> None (not a legal article query)
        """
        match = HandledUserMessageUseCase._LEGAL_ARTICLE_PATTERN.search(
            message.lower()
        )
        if not match:
            return None
        kodeks = match.group(1).replace("'", "").replace(" ", "_")
        modda = match.group(2)
        return f"{kodeks}_kodeksi:{modda}"

    def __init__(
        self,
        conversation_repo: PostgresConversationRepo,
        rate_limiter: RedisRateLimiter,
        chat_llm: GeminiChatAdapter,
        legal_llm: GeminiChatAdapter,
        escalate_conversation: EscalateConversationUseCase,
        notifier: TelegramAdminNotifier,
        lead_repo: PostgresLeadRepo,
        lead_scorer: DynamicLeadScorer,
        context_generator: ContextAwareResponseGenerator,
    ) -> None:
        self._conversation_repo = conversation_repo
        self._rate_limiter = rate_limiter
        self._chat_llm = chat_llm
        self._legal_llm = legal_llm
        self._escalate_conversation = escalate_conversation
        self._notifier = notifier
        self._lead_repo = lead_repo
        self._lead_scorer = lead_scorer
        self._context_generator = context_generator

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
                if reset_at:
                    cooldown_minutes = int(
                        (reset_at - datetime.now(UTC)).total_seconds() / 60
                    )
                    if cooldown_minutes <= 0:
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

        # 5. Suhbat tarixini yuklash
        recent_messages = await self._conversation_repo.get_recent_messages(
            conversation.id, limit=8
        )
        history = self._build_history(recent_messages[:-1])

        # 6. Eskalatsiya holatidagi suhbatni boshqarish
        if conversation.status == ConversationStatus.ESCALATED_LEAD:
            text_normalized = message_text.lower().strip()
            if text_normalized in self._RETURN_TO_AI_KEYWORDS:
                conversation.return_to_ai()
                await self._conversation_repo.save(conversation)
                await self._notifier.notify_returned_to_ai(conversation, user=user)
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

        # 6b. COLLECTING_INFO holatidagi suhbatni boshqarish
        if conversation.status == ConversationStatus.COLLECTING_INFO:
            # User is providing information, continue with normal AI processing
            # The state will be updated based on escalation logic below
            pass

        # 7. AI response generation
        conversation_texts = [m.text for m in recent_messages if m.sender == MessageSender.USER]
        context = self._context_generator.build_context(
            enhanced_response=None,
            conversation_texts=conversation_texts,
        )

        # Try rule-based response for greetings and service requests first
        rule_based_response = self._context_generator.generate_rule_based_response(message_text, context)

        if rule_based_response:
            logger.info("Using rule-based response")
            ai_response_text = rule_based_response
            # Skip lead scoring for rule-based responses
            conversation.enhanced_data = None
            conversation.dynamic_score = None
        else:
            # Use Gemini for AI response with lead qualification
            logger.info("Using Gemini for AI response with lead qualification")
            
            # Determine if web search is needed for this message
            needs_search = self._context_generator.needs_web_search(message_text)
            logger.info("Web search decision", needs_search=needs_search)

            # Check Redis cache for legal article queries (saves tokens + search quota)
            cache_key = self._normalize_legal_query(message_text)
            if cache_key and needs_search:
                try:
                    cached_response = await self._rate_limiter._redis.get(
                        f"{self._CACHE_PREFIX}:{cache_key}"
                    )
                    if cached_response:
                        logger.info("Legal cache HIT", cache_key=cache_key)
                        ai_response_text = cached_response
                        conversation.enhanced_data = None
                        conversation.dynamic_score = None
                        # Save AI message and return cached response
                        conversation.add_message(
                            Message.new(sender=MessageSender.AI, text=ai_response_text)
                        )
                        await self._conversation_repo.save(conversation)
                        return ai_response_text
                    else:
                        logger.info("Legal cache MISS", cache_key=cache_key)
                except Exception:
                    logger.warning("Redis cache lookup failed, proceeding without cache")
            
            try:
                # Inject user context invisibly
                user_display_name = user.first_name
                if user.last_name:
                    user_display_name += f" {user.last_name}"
                elif user.username:
                    user_display_name = user.username
                else:
                    user_display_name = "Foydalanuvchi"
                    
                injected_message = (
                    f"[System Note: Mijozning Telegramdagi ismi: '{user_display_name}'. "
                    f"Suhbat davomida shu ismdan foydalaning va ismini so'ramang.]\n\n"
                    f"User Message: {message_text}"
                )

                enhanced_response = await self._chat_llm.answer_enhanced(
                    user_message=injected_message,
                    history=history,
                    enable_search=needs_search,
                )
                ai_response_text = enhanced_response.ai_response

                # Calculate dynamic lead score
                dynamic_score, score_factors = self._lead_scorer.calculate_from_enhanced_response(
                    enhanced_response, conversation_texts
                )

                # Store enhanced data for later use
                conversation.enhanced_data = enhanced_response
                conversation.dynamic_score = dynamic_score
                conversation.score_factors = score_factors

                logger.info(
                    "Dynamic lead score calculated",
                    user_id=mask_telegram_id(user.telegram_id),
                    score=dynamic_score,
                )

                # Cache search-grounded legal article responses for future users
                if cache_key and needs_search and ai_response_text:
                    try:
                        await self._rate_limiter._redis.set(
                            f"{self._CACHE_PREFIX}:{cache_key}",
                            ai_response_text,
                            ex=self._CACHE_TTL_SECONDS,
                        )
                        logger.info("Legal response cached", cache_key=cache_key)
                    except Exception:
                        logger.warning("Redis cache write failed")
            except Exception:
                logger.exception("LLM generation failed")
                return await self._handle_ai_failure(conversation, user, message_text)

        # 8. Escalation logic — collect info FIRST, escalate AFTER
        should_escalate = False
        escalation_reason = ""

        if conversation.enhanced_data:
            enhanced = conversation.enhanced_data

            # Check what info we have
            has_problem = bool(
                enhanced.problem_description
                and enhanced.problem_description.lower() not in (
                    "noma'lum", "unknown", "",
                    "advokat xizmati so'ralmoqda",
                    "advokat kerak",
                )
            )
            # Require actual phone number from conversation, not just Telegram name
            # Phone number must contain digits and be at least 9 characters
            phone_value = enhanced.phone_number or ""
            has_contact = bool(
                phone_value
                and phone_value.lower() not in ("noma'lum", "unknown", "")
                and len(phone_value) >= 9
                and any(c.isdigit() for c in phone_value)  # Must contain at least one digit
            )
            has_name = bool(
                enhanced.full_name
                and enhanced.full_name.lower() not in ("noma'lum", "unknown", "")
            )

            logger.info(
                "Escalation decision data",
                user_id=mask_telegram_id(user.telegram_id),
                needs_lawyer=enhanced.needs_lawyer,
                intent=enhanced.intent,
                urgency=enhanced.urgency,
                has_problem=has_problem,
                has_contact=has_contact,
                has_name=has_name,
                lead_score=conversation.dynamic_score,
            )

            # Trigger: does user want lawyer services?
            trigger_met = False
            MINIMUM_LEAD_SCORE = 0.5  # Raised to 0.5 for stricter quality control
            
            if enhanced.needs_lawyer:
                # Even if needs_lawyer, require minimum score
                if conversation.dynamic_score and conversation.dynamic_score >= MINIMUM_LEAD_SCORE:
                    trigger_met = True
                    escalation_reason = f"User needs lawyer (intent: {enhanced.intent}), score: {conversation.dynamic_score:.2f}"
                else:
                    logger.info(
                        "User needs lawyer but score below threshold",
                        score=conversation.dynamic_score,
                        threshold=MINIMUM_LEAD_SCORE,
                    )
            elif enhanced.intent in ("consultation", "service_request"):
                # For service requests, require minimum lead score
                if conversation.dynamic_score and conversation.dynamic_score >= MINIMUM_LEAD_SCORE:
                    trigger_met = True
                    escalation_reason = f"Service request intent: {enhanced.intent}, score: {conversation.dynamic_score:.2f}"
                else:
                    logger.info(
                        "Service request but score below threshold",
                        score=conversation.dynamic_score,
                        threshold=MINIMUM_LEAD_SCORE,
                    )
            elif conversation.dynamic_score and conversation.dynamic_score > 0.7:
                trigger_met = True
                escalation_reason = f"High lead score: {conversation.dynamic_score:.2f}"

            # Gating: escalate ONLY when we have enough info to be useful
            # to the lawyer. Otherwise let AI continue collecting info.
            # Require phone number (not just name) for escalation
            if trigger_met:
                has_enough_info = has_problem and has_contact
                logger.info(
                    "Escalation gating check",
                    has_problem=has_problem,
                    has_contact=has_contact,
                    has_enough_info=has_enough_info,
                    phone_value=enhanced.phone_number if enhanced else None,
                )
                if has_enough_info:
                    should_escalate = True
                    logger.info(
                        "Escalation approved — sufficient info collected",
                        reason=escalation_reason,
                    )
                else:
                    # Transition to COLLECTING_INFO state to track that we're gathering info
                    if conversation.status != ConversationStatus.COLLECTING_INFO:
                        conversation.start_collecting_info()
                        await self._conversation_repo.save(conversation)
                    
                    # AI will naturally ask for missing info via system prompt
                    logger.info(
                        "Escalation deferred — AI collecting more info",
                        reason=escalation_reason,
                        missing="problem" if not has_problem else "phone",
                    )

        # 9. AI javobini saqlash
        ai_msg = Message.new(
            conversation_id=conversation.id,
            sender=MessageSender.AI,
            text=ai_response_text,
        )
        await self._conversation_repo.add_message(ai_msg)

        # 10. Execute escalation if needed
        if should_escalate:
            logger.info(
                "Escalating conversation",
                user_id=mask_telegram_id(user.telegram_id),
                reason=escalation_reason,
                lead_score=conversation.dynamic_score,
            )
            await self._escalate_with_profile(
                conversation=conversation,
                user=user,
                message_text=message_text,
                reason=escalation_reason,
            )
            # Return escalation confirmation to user (triggers "AI ga qaytish" button)
            escalation_msg = (
                "✉️ <b>So'rovingiz mutaxassisga yuborildi!</b>\n\n"
                "Advokat Jasurbek Tojiboyev jamoasi tez orada siz bilan bog'lanadi.\n"
                "Qo'shimcha savol yoki ma'lumot bo'lsa — shu yerda yozishingiz mumkin."
            )
            # Save escalation message too
            await self._conversation_repo.add_message(
                Message.new(
                    conversation_id=conversation.id,
                    sender=MessageSender.SYSTEM,
                    text=escalation_msg,
                )
            )
            return escalation_msg

        return ai_response_text

    # ──────────── Helpers ────────────

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
            if m.sender in (MessageSender.USER, MessageSender.AI, MessageSender.ADMIN)
        )

    async def _handle_ai_failure(
        self,
        conversation: Conversation,
        user: User,
        message_text: str,
    ) -> str:
        """AI ishlamay qolganda eskalatsiyaga o'tkazish."""
        username_display = f"@{user.username}" if user.username else "noma'lum"
        user_display_name = user.first_name or "noma'lum"
        if user.last_name:
            user_display_name += f" {user.last_name}"
            
        profile_text = (
            f"👤 <b>Foydalanuvchi:</b> {username_display}\n"
            f"📛 <b>Ism:</b> {user_display_name}\n"
            f"📝 <b>Xabar:</b> {message_text}\n"
            f"⚠️ <b>Status:</b> AI xizmati vaqtincha ishlamayapti"
        )
        
        await self._escalate_conversation.execute(
            conversation_id=conversation.id,
            user=user,
            target=EscalationTarget.LEAD,
            reason=self._AI_FAILURE_ESCALATION_REASON,
            user_message=profile_text,
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
        # Build profile from enhanced response data
        if conversation.enhanced_data:
            enhanced = conversation.enhanced_data
            username_display = f"@{user.username}" if user.username else "noma'lum"

            # Build profile lines, only include fields that have values
            profile_lines = [
                f"👤 <b>Foydalanuvchi:</b> {username_display}",
            ]

            if enhanced.full_name:
                profile_lines.append(f"📛 <b>Ism:</b> {enhanced.full_name}")

            if enhanced.phone_number and enhanced.phone_number.lower() not in ("noma'lum", "unknown", ""):
                profile_lines.append(f"📞 <b>Telefon:</b> {enhanced.phone_number}")

            if enhanced.location and enhanced.location.lower() not in ("noma'lum", "unknown", ""):
                profile_lines.append(f"📍 <b>Hudud:</b> {enhanced.location}")

            urgency_display = {
                "high": "Yuqori 🔴",
                "medium": "O'rtacha 🟡",
                "low": "Oddiy 🟢",
            }.get((enhanced.urgency or "").lower(), enhanced.urgency)
            if urgency_display:
                profile_lines.append(f"🔥 <b>Muhimlik:</b> {urgency_display}")

            if enhanced.has_documents:
                profile_lines.append("📄 <b>Hujjatlar:</b> bor")

            if enhanced.preferred_contact_time:
                profile_lines.append(f"🕐 <b>Bog'lanish vaqti:</b> {enhanced.preferred_contact_time}")

            if enhanced.problem_description and enhanced.problem_description.lower() not in ("noma'lum", "unknown", ""):
                profile_lines.append(f"\n📝 <b>Muammo:</b> {enhanced.problem_description}")

            profile_text = "\n".join(profile_lines)
        else:
            # Fallback to basic profile
            username_display = f"@{user.username}" if user.username else "noma'lum"
            profile_text = (
                f"👤 <b>Foydalanuvchi:</b> {username_display}\n"
                f"📝 <b>Xabar:</b> {message_text}"
            )

        await self._escalate_conversation.execute(
            conversation_id=conversation.id,
            user=user,
            target=EscalationTarget.LEAD,
            user_message=profile_text,
            reason=reason,
        )
