from datetime import UTC, datetime

from domain.entities import Lead
from domain.value_objects import LeadScore, LeadStatus
from infrastructure.telegram.handlers.message_handlers import (
    _greeting_message,
    _subscription_confirmed_message,
    _subscription_required_message,
    cmd_admin_stats,
)


class FakeConversationStats:
    total_conversations = 10
    active_ai_conversations = 6
    escalated_conversations = 2
    closed_conversations = 2
    total_messages = 80
    conversations_since = 3


class FakeConversationRepo:
    async def get_stats(self, *, since: object) -> FakeConversationStats:
        return FakeConversationStats()


class FakeLeadRepo:
    def __init__(self, leads: tuple[Lead, ...] = ()) -> None:
        self.leads = leads

    async def count_all(self) -> int:
        return 4

    async def count_since(self, since: object) -> int:
        return 1

    async def count_by_status(self, status: LeadStatus) -> int:
        values = {
            LeadStatus.CONTACTED: 2,
            LeadStatus.BOOKED: 1,
            LeadStatus.PAID: 1,
            LeadStatus.LOST: 0,
        }
        return values.get(status, 0)

    async def list_open(self, *, limit: int = 10) -> tuple[Lead, ...]:
        return self.leads[:limit]


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[tuple[str, str | None]] = []
        
        class Chat:
            id = 12345
        self.chat = Chat()

    async def reply(self, text: str, parse_mode: str | None = None) -> None:
        self.replies.append((text, parse_mode))


def test_greeting_message_is_compact_and_actionable() -> None:
    text = _greeting_message()

    assert "Advokat Jasurbek" in text
    assert "pullik" in text
    assert "<b>" in text
    assert "**" not in text
    # Placeholder examples have been removed — no Masalan block expected
    assert "Masalan:" not in text
    assert "ishdan bo'shatdi" not in text


def test_subscription_messages_include_clear_next_steps() -> None:
    required_text = _subscription_required_message()
    confirmed_text = _subscription_confirmed_message()

    assert "kanalimizga" in required_text
    assert "shildim" in required_text  # "✅ Qo'shildim" button text
    # Placeholder examples have been removed — no Masalan block expected
    assert "Masalan:" not in confirmed_text
    assert "ishdan bo'shatdi" not in confirmed_text and "ishdan bo\u2019shatdi" not in confirmed_text
    # Paid disclaimer must be present
    assert "pullik" in confirmed_text
    assert "**" not in required_text
    assert "**" not in confirmed_text


def test_admin_stats_message_is_operational_and_html_safe() -> None:
    from asyncio import run

    from config.settings import Settings

    class FakeUserRepo:
        async def count_all(self) -> int:
            return 5
            
        async def count_new_today(self) -> int:
            return 2
            
        async def count_since(self, since: object) -> int:
            return 2
            
        async def count_by_status(self, has_joined: bool) -> int:
            return 3 if has_joined else 2

    message = FakeMessage()
    settings = Settings(_env_file=None, telegram_lead_chat_id=message.chat.id if hasattr(message, 'chat') else 0)

    run(cmd_admin_stats(message, settings, FakeConversationRepo(), FakeLeadRepo(), FakeUserRepo()))  # type: ignore[arg-type]

    text, parse_mode = message.replies[0]
    assert parse_mode == "HTML"
    assert "Lead Bot Statistikasi" in text
    assert "24 soat" in text
    assert "3 suhbat, 1 lead" in text
    assert "Belgilangan" in text
    assert "Yopilgan" in text
    assert "Lead konversiyasi" in text
    assert "40%" in text
    assert "**" not in text



