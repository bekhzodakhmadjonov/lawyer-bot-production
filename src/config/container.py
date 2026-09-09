"""Composition root for the application's infrastructure dependencies."""

from __future__ import annotations

from typing import Self

import httpx

from application.use_cases.conversation.escalate_conversation import (
    EscalateConversationUseCase,
)
from application.use_cases.conversation.handle_user_message import (
    HandledUserMessageUseCase,
)
from application.scoring import DynamicLeadScorer
from application.context import ContextAwareResponseGenerator
from config.settings import Settings
from infrastructure.ai.gemini_chat_adapter import GeminiChatAdapter
from infrastructure.cache import close_redis_client, get_redis_client


class Container:
    """Own and compose long-lived provider clients and their adapters.

    Use the container as an async context manager, or call :meth:`aclose` during
    application shutdown, to release the shared external clients.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._closed = False

        self.http_client = httpx.AsyncClient(timeout=10)
        # Redis client will be initialized on first use
        self._redis_client = None
        # Response cache will be initialized on first use
        self._response_cache = None
        # Initialize Gemini chat adapter (for all responses)
        self.gemini_chat = GeminiChatAdapter(
            api_key=settings.gemini_api_key.get_secret_value(),
            model=settings.gemini_model,
            enable_search=settings.gemini_enable_search,
        )
        # Initialize lead scorer
        self.lead_scorer = DynamicLeadScorer()
        # Initialize context-aware response generator
        self.context_generator = ContextAwareResponseGenerator()

    def build_handle_user_message_use_case(
        self,
        *,
        conversation_repo,
        rate_limiter,
        notifier,
        lead_repo,
    ) -> HandledUserMessageUseCase:
        """Compose the message flow from AI dependencies and outer-layer ports."""
        return HandledUserMessageUseCase(
            conversation_repo=conversation_repo,
            rate_limiter=rate_limiter,
            chat_llm=self.gemini_chat,
            legal_llm=self.gemini_chat,
            escalate_conversation=self.build_escalate_conversation_use_case(
                conversation_repo=conversation_repo,
                lead_repo=lead_repo,
                notifier=notifier,
            ),
            notifier=notifier,
            lead_repo=lead_repo,
            lead_scorer=self.lead_scorer,
            context_generator=self.context_generator,
        )

    def build_escalate_conversation_use_case(
        self,
        *,
        conversation_repo,
        lead_repo,
        notifier,
    ) -> EscalateConversationUseCase:
        """Compose the escalate conversation use case."""
        return EscalateConversationUseCase(
            conversation_repo=conversation_repo,
            notifier=notifier,
            lead_repo=lead_repo,
        )

    async def redis_client(self):
        """Get Redis client (lazy initialization)."""
        if self._redis_client is None:
            self._redis_client = await get_redis_client(self.settings)
        return self._redis_client



    async def __aenter__(self) -> Self:
        if self._closed:
            raise RuntimeError("A closed container cannot be restarted.")
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close externally managed async clients exactly once."""
        if self._closed:
            return
        self._closed = True
        await self.http_client.aclose()
        await close_redis_client()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(settings=Settings(...))"
