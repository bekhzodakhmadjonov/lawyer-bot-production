"""Tests for HandledUserMessageUseCase.

Search orchestration is now handled inside OpenAIChatAdapter via GPT tool
calling. These tests focus on the use case's business logic only:
rate limiting, channel membership, conversation management, lawyer
request detection, escalation, and AI failure handling.
"""

import pytest
from infrastructure.ai.gemini_chat_adapter import (
    ChatLlmResponse,
    ConversationTurn,
    GeminiChatAdapterError,
)

from application.use_cases.conversation.handle_user_message import (
    HandledUserMessageUseCase,
)
from domain.entities import Conversation, Message, User
from domain.exceptions import ChannelMembershipRequiredError, RateLimitExceededError
from domain.value_objects import (
    ConversationStatus,
    MessageSender,
)
from application.scoring import DynamicLeadScorer
from application.context import ContextAwareResponseGenerator


class FakeConversationRepo:
    def __init__(self, conversation: Conversation | None = None) -> None:
        self.conversation = conversation
        self.messages: list[Message] = []

    async def get(self, conversation_id: object) -> Conversation | None:
        if self.conversation and self.conversation.id == conversation_id:
            return self.conversation
        return None

    async def get_active_for_user(self, user_id: object) -> Conversation | None:
        return self.conversation

    async def save(self, conversation: Conversation) -> None:
        self.conversation = conversation

    async def add_message(self, message: Message) -> None:
        self.messages.append(message)
        if self.conversation is not None:
            self.conversation.message_count += 1

    async def get_recent_messages(
        self, conversation_id: object, limit: int = 20
    ) -> tuple[Message, ...]:
        return tuple(self.messages[-limit:])


class FakeRateLimiter:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls = 0

    async def check_and_increment_user(self, user_id: object) -> tuple[bool, None, None]:
        self.calls += 1
        return self.allowed, None, None


class FakeChatLlm:
    """Fake GeminiChatAdapter — simplified interface."""

    def __init__(
        self,
        response: str = "AI javob",
        citations: tuple = (),
        error: Exception | None = None,
    ) -> None:
        self.response = response
        self.citations = citations
        self.error = error
        self.calls: list[tuple[str, tuple[ConversationTurn, ...]]] = []

    async def answer(
        self,
        *,
        user_message: str,
        history: tuple[ConversationTurn, ...] = (),
    ) -> ChatLlmResponse:
        self.calls.append((user_message, history))
        if self.error is not None:
            raise self.error
        return ChatLlmResponse(text=self.response, citations=self.citations)

    async def answer_enhanced(
        self,
        *,
        user_message: str,
        history: tuple[ConversationTurn, ...] = (),
        enable_search: bool | None = None,
    ):
        from infrastructure.ai.gemini_chat_adapter import EnhancedResponse
        
        self.calls.append((user_message, history))
        if self.error is not None:
            raise self.error
            
        intent = "legal_question"
        needs_lawyer = False
        
        lower_msg = user_message.lower()
        lower_resp = self.response.lower()
        
        if "advokat" in lower_msg or "yurist" in lower_msg or "bog'lang" in lower_msg or "konsultatsiya" in lower_msg:
            intent = "service_request"
            needs_lawyer = True
            
        if "advokat" in lower_resp or "bog'layman" in lower_resp:
            intent = "service_request"
            needs_lawyer = True
            
        problem_description = "Test problem"
        phone_number = "+998901234567"
        if intent == "service_request" and len(history) == 0:
            problem_description = ""
            phone_number = ""
            
        return EnhancedResponse(
            ai_response=self.response,
            intent=intent,
            needs_lawyer=needs_lawyer,
            problem_description=problem_description,
            phone_number=phone_number,
            conversation_score=1.0,
        )


class FakeEscalateConversation:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def execute(self, **kwargs: object) -> Conversation:
        self.calls.append(kwargs)
        # Return a dummy conversation
        conv = Conversation.start(
            kwargs.get("user", User.new(1, None)).id,  # type: ignore[union-attr]
            user_telegram_id=1,
        )
        conv.status = ConversationStatus.ESCALATED_LEAD
        return conv


class FakeNotifier:
    def __init__(self) -> None:
        self.followups: list[dict] = []
        self.returned: list[object] = []
        self.new_leads: list[dict] = []

    async def notify_user_followup(self, **kwargs: object) -> None:
        self.followups.append(kwargs)

    async def notify_returned_to_ai(self, conversation: object, *, user: object = None) -> None:
        self.returned.append(conversation)
        
    async def notify_new_lead(self, **kwargs: object) -> None:
        self.new_leads.append(kwargs)


class FakeLeadRepo:
    def __init__(self) -> None:
        self.leads: dict = {}
        self.save_calls: list = []

    async def get_by_conversation(self, conversation_id: object) -> object | None:
        return self.leads.get(conversation_id)

    async def save(self, lead: object) -> None:
        self.save_calls.append(lead)
        if hasattr(lead, "conversation_id"):
            self.leads[lead.conversation_id] = lead

    async def get_by_user(self, user_id: object) -> object | None:
        for lead in self.leads.values():
            if getattr(lead, "user_id", None) == user_id:
                return lead
        return None


def _make_user(*, joined: bool = True) -> User:
    user = User.new(telegram_id=12345, username="testuser")
    user.has_joined_channel = joined
    return user


def build_use_case(
    *,
    conversation: Conversation | None = None,
    rate_allowed: bool = True,
    chat_response: str = "AI javob",
    chat_citations: tuple = (),
    chat_error: Exception | None = None,
) -> tuple[
    HandledUserMessageUseCase,
    FakeConversationRepo,
    FakeChatLlm,
    FakeEscalateConversation,
    FakeNotifier,
]:
    repo = FakeConversationRepo(conversation)
    llm = FakeChatLlm(
        response=chat_response, citations=chat_citations, error=chat_error
    )
    escalation = FakeEscalateConversation()
    notifier = FakeNotifier()
    lead_repo = FakeLeadRepo()
    class FakeLeadScorer:
        def calculate_from_enhanced_response(self, enhanced_response: object, conversation_history: list[str]) -> tuple[float, object]:
            class FakeFactors:
                conversation_depth = 1.0
                intent_clarity = 1.0
                urgency_indicators = 1.0
                budget_signals = 1.0
                geographic_relevance = 1.0
                sentiment_trajectory = 1.0
            score = 1.0 if getattr(enhanced_response, "needs_lawyer", False) else 0.1
            return score, FakeFactors()

    use_case = HandledUserMessageUseCase(
        conversation_repo=repo,
        rate_limiter=FakeRateLimiter(rate_allowed),
        chat_llm=llm,
        legal_llm=llm,
        escalate_conversation=escalation,
        notifier=notifier,
        lead_repo=lead_repo,
        lead_scorer=FakeLeadScorer(),
        context_generator=ContextAwareResponseGenerator(),
    )
    return use_case, repo, llm, escalation, notifier


# ──────────── Happy Path ────────────


@pytest.mark.asyncio
async def test_happy_path_ai_response() -> None:
    """Normal message flow: rate check → channel check → AI response."""
    use_case, repo, llm, escalation, _ = build_use_case(chat_response="Legal answer")
    user = _make_user()

    response = await use_case.execute(user, "My question")

    assert response == "Legal answer"
    assert len(llm.calls) == 1
    assert "My question" in llm.calls[0][0]
    assert repo.messages[-1].sender is MessageSender.AI
    assert escalation.calls == []


# ──────────── Rate Limiting ────────────


@pytest.mark.asyncio
async def test_rate_limit_raises() -> None:
    """Rate-limited user gets RateLimitExceededError."""
    use_case, _, llm, _, _ = build_use_case(rate_allowed=False)
    user = _make_user()

    with pytest.raises(RateLimitExceededError):
        await use_case.execute(user, "Blocked")

    assert llm.calls == []


# ──────────── Channel Membership ────────────


@pytest.mark.asyncio
async def test_channel_membership_required() -> None:
    """Non-member user gets ChannelMembershipRequiredError."""
    use_case, repo, llm, _, _ = build_use_case()
    user = _make_user(joined=False)

    with pytest.raises(ChannelMembershipRequiredError):
        await use_case.execute(user, "No channel")

    assert llm.calls == []
    assert len(repo.messages) == 0


# ──────────── Lawyer Request Detection ────────────


@pytest.mark.asyncio
async def test_first_lawyer_keyword_collects_intake_before_escalation() -> None:
    """Direct lawyer request on an empty conversation stays with AI intake."""
    use_case, _, llm, escalation, _ = build_use_case(
        chat_response="Muammoingizni qisqacha yozing."
    )
    user = _make_user()

    response = await use_case.execute(user, "Menga advokat kerak")

    assert response == "Muammoingizni qisqacha yozing."
    assert len(llm.calls) == 1
    assert escalation.calls == []


@pytest.mark.parametrize(
    "message_text",
    [
        "nega menga advokat kerak",
        "advokat menga kerakmi?",
        "advokat kim o'zi",
        "nega menga yurist kerak?",
    ],
)
@pytest.mark.asyncio
async def test_lawyer_information_questions_stay_with_ai(message_text: str) -> None:
    """Questions about lawyers should be answered by AI, not escalated."""
    use_case, _, llm, escalation, _ = build_use_case(chat_response="AI explanation")
    user = _make_user()

    response = await use_case.execute(user, message_text)

    assert response == "AI explanation"
    assert len(llm.calls) == 1
    assert escalation.calls == []


@pytest.mark.parametrize(
    "message_text",
    [
        "konsultatsiya kerak",
    ],
)
@pytest.mark.asyncio
async def test_direct_lawyer_request_rule_based(
    message_text: str,
) -> None:
    use_case, _, llm, escalation, _ = build_use_case(
        chat_response="Qaysi masala bo'yicha yordam kerak?"
    )
    user = _make_user()

    response = await use_case.execute(user, message_text)

    expected = (
        "💼 <b>Advokat kerakligini aytdingiz.</b>\n\n"
        "Qanday huquqiy muammoingiz bor? Qisqacha bayon qiling.\n\n"
        "📍 <b>Qaysi shaharda yashaysiz?</b>\n\n"
        "📄 <b>Qo'lingizda hujjatlar bormi?</b>"
    )
    assert response == expected
    assert len(llm.calls) == 0
    assert escalation.calls == []


@pytest.mark.parametrize(
    "message_text",
    [
        "yurist bilan gaplashmoqchiman",
        "meni advokatga ulang",
        "iltimos bog'lang",
    ],
)
@pytest.mark.asyncio
async def test_direct_lawyer_request_ai_based(
    message_text: str,
) -> None:
    use_case, _, llm, escalation, _ = build_use_case(
        chat_response="Qaysi masala bo'yicha yordam kerak?"
    )
    user = _make_user()

    response = await use_case.execute(user, message_text)

    assert response == "Qaysi masala bo'yicha yordam kerak?"
    assert len(llm.calls) == 1
    assert escalation.calls == []


@pytest.mark.asyncio
async def test_lawyer_request_escalates_after_intake_is_ready() -> None:
    """High-intent user is escalated once problem context has been collected."""
    user = _make_user()
    conversation = Conversation.start(user.id, user_telegram_id=user.telegram_id)
    use_case, repo, llm, escalation, _ = build_use_case(conversation=conversation)
    repo.messages.append(
        Message.new(
            conversation.id,
            MessageSender.USER,
            "Toshkentdaman, qarz bo'yicha sud ertaga. Tilxat va shartnoma bor.",
        )
    )
    repo.messages.append(
        Message.new(
            conversation.id,
            MessageSender.AI,
            "Tushunarli. Advokat bilan pullik konsultatsiyaga ulashim mumkin.",
        )
    )

    response = await use_case.execute(user, "Ha, advokatga ulang")

    assert "yuborildi" in response.lower()
    assert len(escalation.calls) == 1
    assert len(escalation.calls) == 1
    assert "📞 <b>Telefon:</b> +998901234567" in escalation.calls[0]["user_message"]


# ──────────── Escalation State ────────────


@pytest.mark.asyncio
async def test_escalated_conversation_forwards_to_admin() -> None:
    """Messages in escalated state go to admin, not AI."""
    user = _make_user()
    conversation = Conversation.start(user.id, user_telegram_id=user.telegram_id)
    conversation.status = ConversationStatus.ESCALATED_LEAD
    use_case, _, llm, _, notifier = build_use_case(conversation=conversation)

    response = await use_case.execute(user, "Follow up question")

    assert "mutaxassis" in response.lower() or "yuborildi" in response.lower()
    assert llm.calls == []
    assert len(notifier.followups) == 1


@pytest.mark.asyncio
async def test_return_to_ai_keyword() -> None:
    """User can type 'ai' to return to AI mode from escalated state."""
    user = _make_user()
    conversation = Conversation.start(user.id, user_telegram_id=user.telegram_id)
    conversation.status = ConversationStatus.ESCALATED_LEAD
    use_case, repo, _, _, notifier = build_use_case(conversation=conversation)

    response = await use_case.execute(user, "ai")

    assert "AI" in response or "qaytarildi" in response
    assert repo.conversation.status == ConversationStatus.AI_HANDLED
    assert len(notifier.returned) == 1


# ──────────── AI Failure ────────────


@pytest.mark.asyncio
async def test_ai_failure_escalates() -> None:
    """When AI provider fails, conversation is escalated with fallback message."""
    use_case, repo, _, escalation, _ = build_use_case(
        chat_error=GeminiChatAdapterError("provider down")
    )
    user = _make_user()

    response = await use_case.execute(user, "My question")

    assert response == HandledUserMessageUseCase._AI_FAILURE_FALLBACK
    assert "provider" not in response  # No secrets leaked
    assert len(escalation.calls) == 1
    assert repo.messages[-1].sender is MessageSender.SYSTEM


# ──────────── Conversation History ────────────


@pytest.mark.asyncio
async def test_conversation_history_passed_to_llm() -> None:
    """Previous messages in conversation are sent as history to LLM."""
    user = _make_user()
    conversation = Conversation.start(user.id, user_telegram_id=user.telegram_id)
    use_case, repo, llm, _, _ = build_use_case(
        conversation=conversation, chat_response="Answer"
    )
    # Pre-populate history
    repo.messages.append(
        Message.new(conversation.id, MessageSender.USER, "First question")
    )
    repo.messages.append(Message.new(conversation.id, MessageSender.AI, "First answer"))

    await use_case.execute(user, "Follow up")

    # History should contain previous messages (excluding the current one)
    call_history = llm.calls[0][1]
    assert len(call_history) >= 2


# ──────────── Conversation Creation ────────────


@pytest.mark.asyncio
async def test_new_conversation_created_when_none_exists() -> None:
    """A new conversation is started if user has no active one."""
    use_case, repo, _, _, _ = build_use_case(chat_response="Hello")
    user = _make_user()

    await use_case.execute(user, "Hello")

    assert repo.conversation is not None
    assert repo.conversation.status == ConversationStatus.AI_HANDLED


# ──────────── AI Escalation Signals ────────────


@pytest.mark.asyncio
async def test_ai_escalation_signal_triggers_escalation() -> None:
    """If AI response contains escalation signal words, auto-escalate."""
    user = _make_user()
    conversation = Conversation.start(user.id, user_telegram_id=user.telegram_id)
    use_case, repo, _, escalation, _ = build_use_case(
        conversation=conversation,
        chat_response="Sizni bog'layman advokat bilan."
    )
    repo.messages.append(
        Message.new(
            conversation.id,
            MessageSender.USER,
            "Toshkentdaman, aliment bo'yicha yordam bering.",
        )
    )

    await use_case.execute(user, "Menga yordam kerak")

    assert len(escalation.calls) == 1


@pytest.mark.asyncio
async def test_normal_ai_response_no_escalation() -> None:
    """Normal AI response without escalation signals should not escalate."""
    use_case, _, _, escalation, _ = build_use_case(
        chat_response="Bu masala bo'yicha quyidagilarni aytishim mumkin."
    )
    user = _make_user()

    await use_case.execute(user, "Mehnat qonuni haqida")

    assert escalation.calls == []


@pytest.mark.asyncio
async def test_existing_lead_updated_with_new_topic_summary() -> None:
    """When a lead already exists, it should be updated with new topic_summary."""
    from domain.entities import Lead
    from domain.value_objects import LeadScore

    user = _make_user()
    conversation = Conversation.start(user.id, user_telegram_id=user.telegram_id)

    repo = FakeConversationRepo(conversation)
    llm = FakeChatLlm(response="AI javob")
    
    from application.use_cases.conversation.escalate_conversation import EscalateConversationUseCase
    notifier = FakeNotifier()
    lead_repo = FakeLeadRepo()
    escalation = EscalateConversationUseCase(
        conversation_repo=repo, lead_repo=lead_repo, notifier=notifier
    )
    
    class FakeLeadScorer:
        def calculate_from_enhanced_response(self, enhanced_response: object, conversation_history: list[str]) -> tuple[float, object]:
            class FakeFactors:
                conversation_depth = 1.0
                intent_clarity = 1.0
                urgency_indicators = 1.0
                budget_signals = 1.0
                geographic_relevance = 1.0
                sentiment_trajectory = 1.0
            score = 1.0 if getattr(enhanced_response, "needs_lawyer", False) else 0.8
            return score, FakeFactors()

    use_case = HandledUserMessageUseCase(
        conversation_repo=repo,
        rate_limiter=FakeRateLimiter(True),
        chat_llm=llm,
        legal_llm=llm,
        escalate_conversation=escalation,
        notifier=notifier,
        lead_repo=lead_repo,
        lead_scorer=FakeLeadScorer(),
        context_generator=ContextAwareResponseGenerator(),
    )

    # Simulate existing lead with old topic_summary
    old_lead = Lead.new(
        conversation_id=conversation.id,
        user_id=user.id,
        score=LeadScore(value=0.85, reasons=("Old reason",)),
        topic_summary="Old problem: yoshlar daftariga ro'yxatdan o'tish",
        contact_info="@oldusername",
    )
    lead_repo.leads[conversation.id] = old_lead

    # Add some conversation history
    repo.messages.append(
        Message.new(
            conversation.id,
            MessageSender.USER,
            "Toshkentdaman, aliment bo'yicha advokat kerak. 2 oy oldin bo'ldi.",
        )
    )

    # Trigger escalation
    await use_case.execute(user, "Ha, advokatga ulang")

    # Verify lead was updated with new topic_summary
    assert len(lead_repo.save_calls) == 1
    updated_lead = lead_repo.save_calls[0]
    assert (
        updated_lead.topic_summary != "Old problem: yoshlar daftariga ro'yxatdan o'tish"
    )
    assert (
        "test problem" in updated_lead.topic_summary.lower()
        or "944456778" in updated_lead.topic_summary
    )
