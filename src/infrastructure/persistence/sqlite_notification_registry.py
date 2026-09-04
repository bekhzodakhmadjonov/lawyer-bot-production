"""
SQLiteNotificationRegistry — Admin xabar IDsi va foydalanuvchi IDsini
persistent ravishda SQLite'da saqlash.

Bu modul TelegramAdminNotifier'ning in-memory registrini almashtiradi.
Konteyner qayta ishga tushganda ham eski admin xabarlariga reply ishlaydi.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from infrastructure.persistence.database import AdminNotificationModel

logger = structlog.get_logger()


class SQLiteNotificationRegistry:
    """Admin xabarlari registrining SQLite implementatsiyasi."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(
        self,
        *,
        message_id: int,
        user_telegram_id: int,
        display_name: str,
        conversation_id: str | None = None,
        notification_type: str | None = None,
    ) -> None:
        """Admin guruhiga yuborilgan xabar IDsini foydalanuvchi bilan bog'laydi."""
        model = AdminNotificationModel(
            message_id=message_id,
            user_telegram_id=user_telegram_id,
            display_name=display_name,
            sent_at=datetime.now(UTC),
            conversation_id=conversation_id,
            notification_type=notification_type,
        )
        await self._session.merge(model)
        await self._session.flush()
        logger.debug(
            "Notification registry saved",
            message_id=message_id,
            user_telegram_id=user_telegram_id,
            conversation_id=conversation_id,
            notification_type=notification_type,
        )

    async def get_user_id(self, message_id: int) -> int | None:
        """Xabar IDsi bo'yicha foydalanuvchi Telegram IDsini qaytaradi."""
        model = await self._session.get(AdminNotificationModel, message_id)
        return model.user_telegram_id if model else None

    async def get_display_name(self, message_id: int) -> str | None:
        """Xabar IDsi bo'yicha foydalanuvchi ko'rsatma nomini qaytaradi."""
        model = await self._session.get(AdminNotificationModel, message_id)
        return model.display_name if model else None

    async def get_conversation_id(self, message_id: int) -> str | None:
        """Xabar IDsi bo'yicha conversation IDni qaytaradi."""
        model = await self._session.get(AdminNotificationModel, message_id)
        return model.conversation_id if model else None

    async def get_notification_type(self, message_id: int) -> str | None:
        """Xabar IDsi bo'yicha notification typeni qaytaradi."""
        model = await self._session.get(AdminNotificationModel, message_id)
        return model.notification_type if model else None
