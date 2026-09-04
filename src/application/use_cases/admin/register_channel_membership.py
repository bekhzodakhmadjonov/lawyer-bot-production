"""
RegisterChannelMembership Use Case — Foydalanuvchining kanalga a'zoligini qayd etish.
Foydalanuvchi majburiy Telegram kanalga a'zo bo'lganda ushbu use case chaqiriladi
va u foydalanuvchi entity statusini (has_joined_channel=True) saqlaydi.
"""

from __future__ import annotations

from domain.entities import User


class RegisterChannelMembershipUseCase:
    """Foydalanuvchi kanalga a'zo bo'lganini ro'yxatga oluvchi use case."""

    async def execute(self, user: User) -> User:
        """
        Foydalanuvchi a'zolik holatini True ga o'tkazadi va qaytaradi.
        Args:
            user: A'zo bo'lgan foydalanuvchi entity.
        Returns:
            Yangilangan User obyekti.
        """

        user.has_joined_channel = True
        return user
