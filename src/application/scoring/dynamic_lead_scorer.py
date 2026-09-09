"""Dynamic lead scoring system using weighted factors."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import structlog

from infrastructure.ai.gemini_chat_adapter import EnhancedResponse

logger = structlog.get_logger()


@dataclass
class LeadScoreFactors:
    """Factors contributing to lead score."""

    conversation_depth: float  # 20% - meaningful exchanges
    intent_clarity: float  # 25% - problem specificity
    urgency_indicators: float  # 20% - time sensitivity
    budget_signals: float  # 15% - payment ability
    geographic_relevance: float  # 10% - location match
    sentiment_trajectory: float  # 10% - mood changes


class DynamicLeadScorer:
    """Calculate dynamic lead scores using weighted factors."""

    # Target regions for geographic relevance
    TARGET_REGIONS = {
        "toshkent", "tashkent", "chilonzor", "samarqand", "buxoro",
        "andijon", "farg'ona", "fargona", "namangan", "navoiy",
        "jizzax", "sirdaryo", "qashqadaryo", "surxondaryo", "xorazm",
        "nukus", "qoraqalpog", "ташкент", "самарканд", "бухара",
        "андижан", "фергана", "наманган", "город", "вилоят", "viloyat",
    }

    # Budget signal keywords
    BUDGET_KEYWORDS = {
        "pul", "narx", "qancha", "haq", "to'lay", "to'layman", "kompensatsiya",
        "цена", "сколько", "оплат", "компенсация", "возмещение",
    }

    # Urgency keywords
    URGENCY_KEYWORDS = {
        "bugun", "ertaga", "tez", "shoshilinch", "muddat", "sud ertaga",
        "bu hafta", "hozir", "срочно", "сегодня", "завтра", "срок",
        "на этой неделе", "juda tez", "jiddiy", "jiddiy muammo",
    }

    def calculate_from_enhanced_response(
        self,
        enhanced_response: EnhancedResponse | None,
        conversation_history: list[str],
    ) -> tuple[float, LeadScoreFactors]:
        """Calculate lead score from enhanced AI response and conversation history."""
        if enhanced_response is None:
            # Return default score if no enhanced response available
            logger.debug("No enhanced response, returning default score")
            factors = LeadScoreFactors(
                conversation_depth=self._score_conversation_depth(len(conversation_history)),
                intent_clarity=0.3,
                urgency_indicators=0.3,
                budget_signals=0.2,
                geographic_relevance=0.5,
                sentiment_trajectory=0.5,
            )
            final_score = self._calculate_weighted_score(factors)
            return final_score, factors

        factors = self._calculate_factors(enhanced_response, conversation_history)
        final_score = self._calculate_weighted_score(factors)

        logger.debug(
            "Dynamic lead score calculated",
            score=final_score,
            factors=factors,
        )

        return final_score, factors

    def _calculate_factors(
        self,
        enhanced_response: EnhancedResponse,
        conversation_history: list[str],
    ) -> LeadScoreFactors:
        """Calculate individual scoring factors."""
        combined_text = " ".join(conversation_history).lower()

        return LeadScoreFactors(
            conversation_depth=self._score_conversation_depth(len(conversation_history)),
            intent_clarity=self._score_intent_clarity(enhanced_response.intent),
            urgency_indicators=self._score_urgency(enhanced_response.urgency, combined_text),
            budget_signals=self._score_budget(enhanced_response.problem_description, combined_text),
            geographic_relevance=self._score_geographic(enhanced_response.location),
            sentiment_trajectory=self._score_sentiment(enhanced_response.sentiment),
        )

    def _calculate_weighted_score(self, factors: LeadScoreFactors) -> float:
        """Calculate final weighted score."""
        return (
            factors.conversation_depth * 0.20 +
            factors.intent_clarity * 0.25 +
            factors.urgency_indicators * 0.20 +
            factors.budget_signals * 0.15 +
            factors.geographic_relevance * 0.10 +
            factors.sentiment_trajectory * 0.10
        )

    def _score_conversation_depth(self, message_count: int) -> float:
        """Score conversation depth (0.0-1.0)."""
        if message_count >= 6:
            return 1.0
        elif message_count >= 4:
            return 0.8
        elif message_count >= 2:
            return 0.5
        else:
            return 0.2

    def _score_intent_clarity(self, intent: str) -> float:
        """Score intent clarity based on intent type."""
        high_clarity_intents = {"consultation", "representation", "document_review", "service_request"}
        medium_clarity_intents = {"general_inquiry"}

        if intent in high_clarity_intents:
            return 0.9
        elif intent in medium_clarity_intents:
            return 0.6
        else:
            return 0.3

    def _score_urgency(self, ai_urgency: str, conversation_text: str) -> float:
        """Score urgency indicators."""
        ai_urgency = (ai_urgency or "").lower()
        # Check AI-provided urgency first
        if ai_urgency == "high":
            return 1.0
        elif ai_urgency == "medium":
            return 0.6
        elif ai_urgency == "low":
            return 0.3

        # Fallback to keyword detection
        urgency_count = sum(1 for keyword in self.URGENCY_KEYWORDS if keyword in conversation_text)
        if urgency_count >= 2:
            return 0.8
        elif urgency_count >= 1:
            return 0.5
        else:
            return 0.2

    def _score_budget(self, problem_summary: str, conversation_text: str) -> float:
        """Score budget/payment signals."""
        combined = ((problem_summary or "") + " " + conversation_text).lower()
        budget_count = sum(1 for keyword in self.BUDGET_KEYWORDS if keyword in combined)

        if budget_count >= 2:
            return 0.8
        elif budget_count >= 1:
            return 0.5
        else:
            return 0.2

    def _score_geographic(self, location: str) -> float:
        """Score geographic relevance."""
        if not location or location.lower() == "noma'lum" or location.lower() == "unknown":
            return 0.3

        location_lower = location.lower()
        if any(region in location_lower for region in self.TARGET_REGIONS):
            return 1.0
        else:
            return 0.5

    def _score_sentiment(self, ai_sentiment: str) -> float:
        """Score sentiment trajectory."""
        if ai_sentiment == "urgent":
            return 1.0
        elif ai_sentiment == "positive":
            return 0.8
        elif ai_sentiment == "neutral":
            return 0.5
        elif ai_sentiment == "negative":
            return 0.3
        else:
            return 0.5
