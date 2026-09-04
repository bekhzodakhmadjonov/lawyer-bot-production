"""Tests for OpenAIChatAdapter with GPT tool-calling support."""

import pytest

from infrastructure.ai.openai_chat_adapter import (
    ConversationTurn,
    OpenAIChatAdapter,
    OpenAIChatAdapterError,
)


class FakeMessage:
    def __init__(
        self,
        content: str | None = None,
        tool_calls: list | None = None,
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls

    def model_dump(self) -> dict:
        result: dict = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            result["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in self.tool_calls
            ]
        return result


class FakeFunction:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class FakeToolCall:
    def __init__(self, tool_id: str, name: str, arguments: str) -> None:
        self.id = tool_id
        self.function = FakeFunction(name, arguments)


class FakeChoice:
    def __init__(
        self,
        message: FakeMessage,
        finish_reason: str = "stop",
    ) -> None:
        self.message = message
        self.finish_reason = finish_reason


class FakeCompletion:
    def __init__(self, choices: list[FakeChoice] | None = None) -> None:
        self.choices = choices or []


class SequenceChat:
    """Returns a sequence of completions for multi-turn tool-calling tests."""

    def __init__(self, completions: list[FakeCompletion]) -> None:
        self._completions = list(completions)
        self.calls: list[dict] = []

    async def create(self, **kwargs: object) -> FakeCompletion:
        self.calls.append(kwargs)
        if self._completions:
            return self._completions.pop(0)
        return FakeCompletion([FakeChoice(FakeMessage("fallback"))])


class FakeOpenAIClient:
    def __init__(
        self,
        completions: list[FakeCompletion] | None = None,
        error: Exception | None = None,
    ) -> None:
        if error:

            class ErrorChat:
                async def create(self, **kwargs: object) -> FakeCompletion:
                    raise error

            self.chat = type("CN", (), {"completions": ErrorChat()})()
        else:
            chat = SequenceChat(completions or [])
            self.chat = type("CN", (), {"completions": chat})()
            self._spy = chat


# ──────────── Basic Response Tests ────────────


def test_returns_response_text() -> None:
    from asyncio import run

    client = FakeOpenAIClient([FakeCompletion([FakeChoice(FakeMessage("Salom!"))])])
    adapter = OpenAIChatAdapter(client)  # type: ignore[arg-type]

    response = run(adapter.answer(user_message="Salom"))

    assert response.text == "Salom!"
    assert response.citations == ()


def test_raises_on_empty_completion() -> None:
    from asyncio import run

    client = FakeOpenAIClient([FakeCompletion([])])
    adapter = OpenAIChatAdapter(client)  # type: ignore[arg-type]

    with pytest.raises(OpenAIChatAdapterError, match="empty"):
        run(adapter.answer(user_message="Test"))


def test_raises_on_api_error() -> None:
    from asyncio import run

    from openai import OpenAIError

    client = FakeOpenAIClient(error=OpenAIError("fail"))
    adapter = OpenAIChatAdapter(client)  # type: ignore[arg-type]

    with pytest.raises(OpenAIChatAdapterError, match="failed"):
        run(adapter.answer(user_message="Test"))


def test_passes_history_to_api() -> None:
    from asyncio import run

    client = FakeOpenAIClient([FakeCompletion([FakeChoice(FakeMessage("Ok"))])])
    adapter = OpenAIChatAdapter(client)  # type: ignore[arg-type]

    run(
        adapter.answer(
            user_message="Follow up",
            history=(ConversationTurn(role="user", text="First"),),
        )
    )

    messages = client._spy.calls[0]["messages"]
    # system + history + user = 3
    assert len(messages) == 3
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert messages[1]["content"] == "First"
    assert messages[2]["role"] == "user"
    assert messages[2]["content"] == "Follow up"


# ──────────── HTML Sanitization ────────────


def test_closes_unclosed_html_tags() -> None:
    from asyncio import run

    client = FakeOpenAIClient(
        [FakeCompletion([FakeChoice(FakeMessage("<b>Bold text"))])]
    )
    adapter = OpenAIChatAdapter(client)  # type: ignore[arg-type]

    response = run(adapter.answer(user_message="Test"))

    assert response.text == "<b>Bold text</b>"


def test_removes_orphaned_closing_tags() -> None:
    from asyncio import run

    client = FakeOpenAIClient(
        [FakeCompletion([FakeChoice(FakeMessage("text</b> more"))])]
    )
    adapter = OpenAIChatAdapter(client)  # type: ignore[arg-type]

    response = run(adapter.answer(user_message="Test"))

    assert response.text == "text more"
