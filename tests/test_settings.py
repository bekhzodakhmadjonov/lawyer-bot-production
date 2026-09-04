import subprocess
from pathlib import Path

import pytest

from config.settings import Settings


def test_loads_provider_keys_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-test-key")
    settings = Settings(_env_file=None)

    assert settings.openai_api_key.get_secret_value() == "openai-test-key"


def test_loads_provider_keys_from_dotenv_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "OPENAI_API_KEY=openai-from-dotenv\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert settings.openai_api_key.get_secret_value() == "openai-from-dotenv"


def test_masks_credentials_in_settings_representation() -> None:
    settings = Settings(
        _env_file=None,
        OPENAI_API_KEY="openai-secret",
    )

    representation = repr(settings)

    assert "openai-secret" not in representation
    assert "**********" in representation


def test_default_values_are_empty() -> None:
    """Settings with no env file should have empty defaults (no validation error)."""
    settings = Settings(_env_file=None)

    assert settings.openai_api_key.get_secret_value() == ""
    assert settings.telegram_bot_token == ""
    assert settings.telegram_lead_chat_id == 0


def test_dotenv_example_has_only_empty_provider_placeholders() -> None:
    example_file = Path(__file__).parents[1] / ".env.example"
    entries = {}
    for line in example_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, _, value = stripped.partition("=")
        entries[key] = value

    # All secret/credential values should be empty
    assert entries["OPENAI_API_KEY"] == ""
    assert entries["TELEGRAM_BOT_TOKEN"] == ""
    assert entries["TELEGRAM_WEBHOOK_SECRET"] == ""
    assert "TAVILY_API_KEYS" not in entries
    assert "TAVILY_KEY_ROTATION_DAYS" not in entries


def test_dotenv_is_ignored_by_git() -> None:
    project_root = Path(__file__).parents[1]
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", ".env"],
        cwd=project_root,
        check=False,
    )

    assert result.returncode == 0
