"""Security utilities for logging and data masking."""

from __future__ import annotations


def mask_telegram_id(telegram_id: int) -> str:
    """Mask Telegram ID for logging (show only first 3 and last 3 digits)."""
    id_str = str(telegram_id)
    if len(id_str) <= 6:
        return "***"
    return f"{id_str[:3]}***{id_str[-3:]}"


def mask_message(text: str) -> str:
    """Mask sensitive content in messages for logging."""
    if len(text) > 50:
        return f"{text[:20]}...{text[-20:]}"
    return text
