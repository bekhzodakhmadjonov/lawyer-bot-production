import pytest
from infrastructure.ai.gemini_chat_adapter import GeminiChatAdapter

from config.container import Container
from config.settings import Settings


class FakeGenAIClient:
    def __init__(self, *, api_key: str) -> None:
        self.api_key = api_key


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        GEMINI_API_KEY="gemini-test-key",
    )


def test_composes_shared_clients(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import config.container as container_module
    from google import genai

    created_client = None

    def create_genai(*, api_key: str) -> FakeGenAIClient:
        nonlocal created_client
        created_client = FakeGenAIClient(api_key=api_key)
        return created_client

    monkeypatch.setattr(genai, "Client", create_genai)
    container = Container(settings)

    assert isinstance(container.gemini_chat, GeminiChatAdapter)
    assert container.gemini_chat._client is created_client
    assert created_client.api_key == "gemini-test-key"


@pytest.mark.asyncio
async def test_closes_shared_clients_once(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from google import genai

    fake_client = FakeGenAIClient(api_key="test")
    monkeypatch.setattr(genai, "Client", lambda *, api_key: fake_client)

    container = Container(settings)
    async with container as active_container:
        assert active_container is container

    # Second close is a no-op
    await container.aclose()


def test_repr_does_not_expose_api_keys(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from google import genai

    monkeypatch.setattr(
        genai,
        "Client",
        lambda *, api_key: FakeGenAIClient(api_key=api_key),
    )
    representation = repr(Container(settings))
    assert "gemini-test-key" not in representation
