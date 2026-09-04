"""Safe local diagnostic for provider credential configuration."""

from __future__ import annotations

from pydantic import ValidationError

from config.settings import Settings


def main() -> int:
    """Report whether all required provider credentials are configured."""
    try:
        settings = Settings()  # type: ignore[call-arg]
    except ValidationError:
        print(
            "Provider configuration is incomplete. Set all required API key variables."
        )
        return 1

    openai_key = settings.openai_api_key.get_secret_value()
    print(f"OPENAI_API_KEY: {'configured' if openai_key else 'MISSING'}")
    print("Lead intake mode: enabled")
    print("Search provider: disabled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
