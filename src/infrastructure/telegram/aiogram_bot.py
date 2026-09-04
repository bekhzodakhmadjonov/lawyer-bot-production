"""
AiogramBot — Bot va Dispatcher factory.

Bu fayl faqat aiogram obyektlarini YARATADI va sozlaydi.
Hech qanday biznes logika, handler yoki use case bu yerda bo'lmaydi.
Handler'lar alohida router'da (handlers/message_handlers.py) joylashgan.
"""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config.settings import Settings
from infrastructure.telegram.handlers.message_handlers import router


def create_bot(settings: Settings) -> Bot:
    """Telegram Bot instance'ini yaratadi (HTML parse mode bilan)."""
    return Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def create_dispatcher() -> Dispatcher:
    """Dispatcher yaratadi va barcha router'larni ulaydi."""
    dp = Dispatcher()
    dp.include_router(router)
    return dp
