"""
SendAdminReply Use Case — Admin javobini foydalanuvchiga yetkazish.

Bu use case admin to'g'ridan-to'g'ri foydalanuvchiga xabar yuborganda
ishga tushadi:
  1. Notifier orqali foydalanuvchiga javobni Telegram'da yuboradi.
"""

from __future__ import annotations

import structlog

from infrastructure.notifications.telegram_admin_notifier import TelegramAdminNotifier
from infrastructure.security.logging_utils import mask_telegram_id

logger = structlog.get_logger()


class SendAdminReplyUseCase:
    """Admin javobini foydalanuvchiga yetkazuvchi use case."""

    def __init__(
        self,
        notifier: TelegramAdminNotifier,
    ) -> None:
        self._notifier = notifier

    async def execute(
        self,
        *,
        user_telegram_id: int,
        admin_text: str,
    ) -> None:
        """
        Admin javobini qayta ishlab, foydalanuvchiga yetkazadi.

        Args:
            user_telegram_id: Javob yuboriladigan foydalanuvchi Telegram IDsi.
            admin_text: Admin tomonidan yozilgan javob matni.
        """
        # 1. Foydalanuvchiga javobni yuborish
        await self._notifier.send_reply_to_user(
            user_telegram_id=user_telegram_id,
            reply_text=admin_text,
        )

        logger.info(
            "Admin reply sent to user",
            user_telegram_id=mask_telegram_id(user_telegram_id),
        )
