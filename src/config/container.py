"""Composition root for the application's infrastructure dependencies."""

from __future__ import annotations

from typing import Self

import httpx
from openai import AsyncOpenAI

from application.use_cases.conversation.escalate_conversation import (
    EscalateConversationUseCase,
)
from application.use_cases.conversation.handle_user_message import (
    HandledUserMessageUseCase,
)
from config.settings import Settings
from infrastructure.ai.openai_chat_adapter import OpenAIChatAdapter


class Container:
    """Own and compose long-lived provider clients and their adapters.

    Use the container as an async context manager, or call :meth:`aclose` during
    application shutdown, to release the shared external clients.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._closed = False

        self.openai_client = AsyncOpenAI(
            api_key=settings.openai_api_key.get_secret_value()
        )
        self.http_client = httpx.AsyncClient(timeout=10)
        # GPT faqat suhbat (Lead Gen) uchun ishlaydi
        self.openai_chat = OpenAIChatAdapter(self.openai_client)

    def build_handle_user_message_use_case(
        self,
        *,
        conversation_repo,
        rate_limiter,
        notifier,
        lead_repo,
    ) -> HandledUserMessageUseCase:
        """Compose the message flow from AI dependencies and outer-layer ports."""
        escalate_conversation = EscalateConversationUseCase(
            conversation_repo=conversation_repo,
            notifier=notifier,
            lead_repo=lead_repo,
        )
        return HandledUserMessageUseCase(
            conversation_repo=conversation_repo,
            rate_limiter=rate_limiter,
            chat_llm=self.openai_chat,
            escalate_conversation=escalate_conversation,
            notifier=notifier,
            lead_repo=lead_repo,
        )

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
        await self.openai_client.close()
        await self.http_client.aclose()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(settings=Settings(...))"
