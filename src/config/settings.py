"""
Settings — butun tizimning yagona konfiguratsiya manbai.

Nega pydantic-settings: har bir sozlama TIPLANGAN va TEKSHIRILADI.
Agar .env faylida OPENAI_API_KEY yozilishni unutilgan bo'lsa, ilova
production'da "kutilmagan joyda" xato bermaydi — u umuman ISHGA
TUSHMAYDI, xatolik darhol, aniq xabar bilan chiqadi.
"""

from __future__ import annotations

from enum import Enum

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(str, Enum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Umumiy ---
    environment: Environment = Environment.LOCAL

    # --- Database ---
    database_url: str = "postgresql+asyncpg://user:password@localhost/lawyer_bot"

    # --- Redis ---
    redis_url: str = "redis://localhost:6379/0"

    # --- Telegram ---
    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""
    telegram_webhook_url: str = ""
    telegram_lead_chat_id: int = 0
    """Adminlar yo'naltiriladigan Telegram guruh/topic ID."""
    required_channel_username: str = ""
    """Foydalanuvchi a'zo bo'lishi shart bo'lgan kanal."""
    required_channel_id: int = 0

    # --- AI ---
    openai_api_key: SecretStr = SecretStr("")

    # --- Chat History ---
    chat_history_detail_limit: int = 15
    """Number of messages to show in /leads {index} detail view."""
    chat_history_notification_limit: int = 8
    """Number of messages to show in notifications (Yangi Lead, Yangi habar)."""

    @property
    def is_production(self) -> bool:
        return self.environment == Environment.PRODUCTION
