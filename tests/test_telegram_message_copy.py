from datetime import UTC, datetime

from domain.entities import Lead
from domain.value_objects import LeadScore, LeadStatus
from infrastructure.telegram.handlers.message_handlers import (
    _greeting_message,
    _subscription_confirmed_message,
    _subscription_required_message,
    cmd_admin_open_leads,
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

    async def reply(self, text: str, parse_mode: str | None = None) -> None:
        self.replies.append((text, parse_mode))


def test_greeting_message_is_compact_and_actionable() -> None:
    text = _greeting_message()

    assert "Advokat Jasurbek" in text
    assert "Namuna" not in text
    assert "Ishdan bo'shatishdi" in text
    assert "Aliment undirish" in text
    assert "<b>" in text
    assert "**" not in text


def test_subscription_messages_include_clear_next_steps() -> None:
    required_text = _subscription_required_message()
    confirmed_text = _subscription_confirmed_message()

    assert "kanalga a'zo bo'ling" in required_text
    assert "Qo'shildim" in required_text
    assert "Namuna savollar" in confirmed_text
    assert "Apellyatsiya" in confirmed_text
    assert "**" not in required_text
    assert "**" not in confirmed_text


def test_admin_stats_message_is_operational_and_html_safe() -> None:
    from asyncio import run

    message = FakeMessage()

    run(cmd_admin_stats(message, FakeConversationRepo(), FakeLeadRepo()))  # type: ignore[arg-type]

    text, parse_mode = message.replies[0]
    assert parse_mode == "HTML"
    assert "Lead Bot Statistikasi" in text
    assert "24 soat" in text
    assert "3 suhbat, 1 lead" in text
    assert "Belgilangan" in text
    assert "Paid" in text
    assert "Lead conversion" in text
    assert "40%" in text
    assert "**" not in text


def test_admin_open_leads_message_lists_pipeline_items() -> None:
    from asyncio import run
    from uuid import uuid4

    lead = Lead(
        id=uuid4(),
        conversation_id=uuid4(),
        user_id=uuid4(),
        score=LeadScore(value=0.9),
        topic_summary="Toshkentda qarz bo'yicha sud ertaga, tilxat bor.",
        contact_info="+998901234567",
        status=LeadStatus.BOOKED,
        created_at=datetime(2026, 8, 31, 10, 0, tzinfo=UTC),
    )
    message = FakeMessage()

    run(cmd_admin_open_leads(message, FakeLeadRepo((lead,))))  # type: ignore[arg-type]

    text, parse_mode = message.replies[0]
    assert parse_mode == "HTML"
    assert "Ochiq leadlar" in text
    assert "Konsultatsiya belgilandi" in text
    assert "+998901234567" in text
    assert "qarz" in text


def test_admin_open_leads_message_handles_empty_queue() -> None:
    from asyncio import run

    message = FakeMessage()

    run(cmd_admin_open_leads(message, FakeLeadRepo()))  # type: ignore[arg-type]

    text, parse_mode = message.replies[0]
    assert parse_mode == "HTML"
    assert "Ochiq leadlar yo'q" in text
