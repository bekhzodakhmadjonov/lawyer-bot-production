import pytest

from config.container import Container
from config.settings import Settings
from infrastructure.ai.openai_chat_adapter import OpenAIChatAdapter


class FakeOpenAIClient:
    def __init__(self, *, api_key: str) -> None:
        self.api_key = api_key
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        OPENAI_API_KEY="openai-test-key",
    )


def test_composes_shared_clients(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import config.container as container_module

    created_client = None

    def create_openai(*, api_key: str) -> FakeOpenAIClient:
        nonlocal created_client
        created_client = FakeOpenAIClient(api_key=api_key)
        return created_client

    monkeypatch.setattr(container_module, "AsyncOpenAI", create_openai)
    container = Container(settings)

    assert container.openai_client is created_client
    assert created_client.api_key == "openai-test-key"
    assert isinstance(container.openai_chat, OpenAIChatAdapter)
    assert container.openai_chat._client is container.openai_client


@pytest.mark.asyncio
async def test_closes_shared_clients_once(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import config.container as container_module

    fake_client = FakeOpenAIClient(api_key="test")
    monkeypatch.setattr(container_module, "AsyncOpenAI", lambda *, api_key: fake_client)

    container = Container(settings)
    async with container as active_container:
        assert active_container is container

    # Second close is a no-op
    await container.aclose()
    assert fake_client.closed is True


def test_repr_does_not_expose_api_keys(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import config.container as container_module

    monkeypatch.setattr(
        container_module,
        "AsyncOpenAI",
        lambda *, api_key: FakeOpenAIClient(api_key=api_key),
    )
    representation = repr(Container(settings))
    assert "openai-test-key" not in representation
