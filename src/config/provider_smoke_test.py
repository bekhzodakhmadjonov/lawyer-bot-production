"""Manual, opt-in connectivity check for configured AI provider.

Run with ``uv run python -m config.provider_smoke_test``. This module makes
a real provider request and is intentionally not part of the pytest suite.
"""

from __future__ import annotations

import asyncio

from pydantic import ValidationError

from config.container import Container
from config.settings import Settings


async def run() -> int:
    """Run one minimal request to OpenAI without exposing credential details."""
    try:
        settings = Settings()  # type: ignore[call-arg]
    except ValidationError:
        print("Provider configuration is incomplete.")
        return 1

    failures = 0
    async with Container(settings) as container:
        openai_ok = await _check_openai(container)
        failures += not openai_ok

    return int(failures > 0)


async def _check_openai(container: Container) -> bool:
    try:
        response = await container.openai_chat.answer(user_message="Salom")
    except Exception:  # noqa: BLE001 - smoke checks must suppress provider details.
        print("OpenAI: FAILED")
        print("Reason: provider request failed")
        return False

    print("OpenAI: OK")
    print(f"Model: {container.openai_chat.DEFAULT_MODEL}")
    print(f"Response received: {'yes' if response.text.strip() else 'no'}")
    return bool(response.text.strip())


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
