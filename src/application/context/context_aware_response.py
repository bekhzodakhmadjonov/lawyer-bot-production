"""Context-aware response generation for adaptive follow-up questions."""

from __future__ import annotations

import re
from dataclasses import dataclass

import structlog

from infrastructure.ai.gemini_chat_adapter import EnhancedResponse

logger = structlog.get_logger()


@dataclass
class ConversationContext:
    """Context about the conversation state."""

    has_problem: bool
    has_location: bool
    has_documents: bool
    has_urgency: bool
    has_phone: bool
    message_count: int
    intent: str
    current_urgency: str


class ContextAwareResponseGenerator:
    """Generate context-aware responses to reduce API calls."""

    # Common patterns that can be handled with rule-based responses
    GREETING_PATTERNS = {
        r"\b(salom|assalomu\s*alaykum|assalom)\b",
        r"^(yordam)$",
        r"^(maslahat)$",
    }

    # Service request patterns
    SERVICE_REQUEST_PATTERNS = {
        r"^(advokat|yurist)\s*(kerak|kerakmi|lazim)?$",
        r"^(maslahat|konsultatsiya)\s*(kerak|kerakmi|lazim)?$",
        r"^(konsultatsiya)\s*(kerak|kerakmi|lazim)?$",
        r"^(yuridik)\s*(yordam)\s*(kerak|kerakmi|lazim)?$",
    }

    # Gratitude patterns
    GRATITUDE_PATTERNS = {
        r"^(rahmat|rakhmat|tashakkur)$",
        r"^(rahmat|rakhmat|tashakkur)\s*(sizga)$",
    }

    # Legal terms that indicate the message is a legal question, not a greeting
    LEGAL_TERMS = {
        "aliment", "deport", "sud", "shartnoma", "qaror",
        "ariza", "dalil", "javobgarlik", "majburiyat", "modda",
        "kodeks", "tartib", "protsedura", "biznes", "fuqarolik", "jinoiy",
        "oila", "nikoh", "taloq", "meros", "ijara", "qarz", "bankrot",
        "solik", "bojxona", "viz", "pasport", "fuqarolik", "passport",
    }

    # Search-worthy patterns - messages that benefit from web search
    SEARCH_WORTHY_PATTERNS = {
        r"\b(qancha|narx|haq|to'lov|miqdor|foiz|jarima)\b",
        r"\b(qonun|kodeks|modda|tartib)\b",
        r"\b(oxirgi|yangi|hozirgi|2024|2025|2026)\b",
    }

    # Rule-based responses for common patterns
    GREETING_RESPONSE = (
        "👋 <b>Assalomu alaykum!</b>\n\n"
        "Men Advokat Jasurbek jamoasining AI yordamchisiman. "
        "Huquqiy muammolaringizni tushunib, to'g'ri yo'nalishga yo'naltirishga yordam beraman.\n\n"
        "💬 <b>Vaziyatingizni 2-3 gapda yozing:</b>\n"
        "• Nima bo'ldi?\n"
        "• Qachon bo'ldi?\n"
        "• Qo'lingizda qanday hujjatlar bor?\n\n"
        "👇 <b>Savolingizni yozing:</b>"
    )

    SERVICE_REQUEST_RESPONSE = (
        "💼 <b>Advokat kerakligini aytdingiz.</b>\n\n"
        "Qanday huquqiy muammoingiz bor? Qisqacha bayon qiling.\n\n"
        "📍 <b>Qaysi shaharda yashaysiz?</b>\n\n"
        "📄 <b>Qo'lingizda hujjatlar bormi?</b>"
    )

    GRATITUDE_RESPONSE = (
        "😊 <b>Marhamat!</b>\n\n"
        "Yana savollaringiz bo'lsa, bemalol yozing. "
        "Sizga yordam berishdan xursandman."
    )

    def generate_rule_based_response(
        self,
        message: str,
        context: ConversationContext,
    ) -> str | None:
        """Generate rule-based response if message matches common patterns."""
        message_lower = message.lower().strip()

        # Check for gratitude patterns (highest priority)
        if any(re.match(pattern, message_lower) for pattern in self.GRATITUDE_PATTERNS):
            logger.debug("Gratitude pattern detected, using rule-based response")
            return self.GRATITUDE_RESPONSE

        # Check for service request patterns
        if any(re.match(pattern, message_lower) for pattern in self.SERVICE_REQUEST_PATTERNS):
            logger.debug("Service request pattern detected, using rule-based response")
            # Make contextual based on what information we already have
            if context.has_problem:
                return (
                    "💼 <b>Advokat kerakligini aytdingiz.</b>\n\n"
                    f"Vaziyatingizni tushundim. "
                    f"{'Qaysi shaharda yashaysiz?' if not context.has_location else ''} "
                    f"{'Qo\'lingizda hujjatlar bormi?' if not context.has_documents else ''}"
                ).strip()
            return self.SERVICE_REQUEST_RESPONSE

        # Don't use rule-based response if message contains question marks
        if "?" in message:
            return None

        # Don't use rule-based response if message contains legal terms
        if any(term in message_lower for term in self.LEGAL_TERMS):
            return None

        # Check for greeting patterns (standalone only)
        if any(re.match(pattern, message_lower) for pattern in self.GREETING_PATTERNS):
            logger.debug("Greeting pattern detected, using rule-based response")
            # Make contextual - if user has already provided info, acknowledge it
            if context.has_problem:
                return (
                    "👋 <b>Assalomu alaykum!</b>\n\n"
                    "Men Advokat Jasurbek jamoasining AI yordamchisiman. "
                    "Vaziyatingizni tushundim. Sizga yordam berishdan xursandman.\n\n"
                    "👇 <b>Yana savolingiz bormi?</b>"
                )
            return self.GREETING_RESPONSE

        return None

    def generate_context_aware_followup(
        self,
        enhanced_response: EnhancedResponse | None,
        context: ConversationContext,
    ) -> str | None:
        """Generate context-aware follow-up question based on missing information."""
        # If AI already provided a good response, don't override
        if enhanced_response and enhanced_response.conversation_score > 0.7:
            return None

        # Generate follow-up based on missing information
        followup_questions = []

        if not context.has_problem:
            followup_questions.append("Muammoningiz nima?")
        if not context.has_location:
            followup_questions.append("Qaysi shaharda yashaysiz?")
        if not context.has_phone:
            followup_questions.append("Telefon raqamingizni yozing")
        if not context.has_documents:
            followup_questions.append("Qo'lingizda hujjatlar bormi?")
        if not context.has_urgency and context.current_urgency.lower() == "low":
            followup_questions.append("Muddat bormi yoki shoshilinchmi?")

        if followup_questions:
            # Select the most important missing info
            primary_question = followup_questions[0]
            return (
                f"📌 <b>Tushunarli.</b> {primary_question}\n\n"
                f"Bu ma'lumot sizga mos advokatni tanlashimizga yordam beradi."
            )

        return None

    def build_context(
        self,
        enhanced_response: EnhancedResponse | None,
        conversation_texts: list[str],
    ) -> ConversationContext:
        """Build conversation context from enhanced response and history."""
        combined_text = " ".join(conversation_texts).lower()

        # Handle case when enhanced_response is None (before AI call)
        if enhanced_response is None:
            return ConversationContext(
                has_problem=self._has_problem(None, combined_text),
                has_location=self._has_location(None),
                has_documents=self._has_documents(None, combined_text),
                has_urgency=self._has_urgency("Medium", combined_text),
                has_phone=self._has_phone(None),
                message_count=len(conversation_texts),
                intent="general_inquiry",
                current_urgency="Medium",
            )

        return ConversationContext(
            has_problem=self._has_problem(enhanced_response.problem_description, combined_text),
            has_location=self._has_location(enhanced_response.location),
            has_documents=self._has_documents(enhanced_response.has_documents, combined_text),
            has_urgency=self._has_urgency(enhanced_response.urgency, combined_text),
            has_phone=self._has_phone(enhanced_response.phone_number),
            message_count=len(conversation_texts),
            intent=enhanced_response.intent,
            current_urgency=enhanced_response.urgency,
        )

    def _has_problem(self, problem_summary: str | None, combined_text: str) -> bool:
        """Check if problem has been identified."""
        if problem_summary and problem_summary.lower() not in ("noma'lum", "unknown"):
            return True
        # Check for meaningful content in conversation
        return len(combined_text.split()) >= 5

    def _has_location(self, location: str | None) -> bool:
        """Check if location has been provided."""
        if not location:
            return False
        return location.lower() not in ("noma'lum", "unknown")

    def _has_documents(self, documents: bool | None, combined_text: str) -> bool:
        """Check if documents have been mentioned."""
        if documents is True:
            return True
        doc_keywords = ["hujjat", "shartnoma", "qaror", "ariza", "dalil", "документ", "договор"]
        return any(keyword in combined_text for keyword in doc_keywords)

    def _has_urgency(self, urgency: str, combined_text: str) -> bool:
        """Check if urgency has been indicated."""
        if urgency.lower() == "high":
            return True
        urgency_keywords = ["bugun", "ertaga", "shoshilinch", "muddat", "срочно", "сегодня"]
        return any(keyword in combined_text for keyword in urgency_keywords)

    def _has_phone(self, phone: str | None) -> bool:
        """Check if phone number has been provided."""
        if not phone:
            return False
        return phone.lower() not in ("noma'lum", "unknown") and len(phone) > 5

    def needs_web_search(self, message: str) -> bool:
        """Determine if message requires web search based on patterns."""
        message_lower = message.lower().strip()
        
        # Check if message contains search-worthy patterns
        if any(re.search(pattern, message_lower) for pattern in self.SEARCH_WORTHY_PATTERNS):
            return True
        
        # Check if message contains legal terms (likely needs current info)
        if any(re.search(rf"\b{term}\b", message_lower) for term in self.LEGAL_TERMS):
            return True
        
        return False
