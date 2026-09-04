"""
SQLiteUserRepo — User entity'ni SQLite orqali saqlash.

Bu fayl domain User entity'si bilan SQLite UserModel o'rtasida
tarjima qiladi.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.entities import User
from infrastructure.persistence.database import UserModel


class SQLiteUserRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_telegram_id(self, telegram_id: int) -> User | None:
        """Telegram ID bo'yicha foydalanuvchini topadi."""
        stmt = select(UserModel).where(UserModel.telegram_id == telegram_id)
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return self._to_entity(model) if model else None

    async def save(self, user: User) -> None:
        """Foydalanuvchini saqlaydi (upsert)."""
        await self._session.merge(self._to_model(user))
        await self._session.flush()

    async def get_all(self) -> list[User]:
        """Barcha foydalanuvchilarni olish."""
        stmt = select(UserModel).order_by(UserModel.created_at.desc())
        result = await self._session.execute(stmt)
        models = result.scalars().all()
        return [self._to_entity(m) for m in models]

    async def count_all(self) -> int:
        """Jami foydalanuvchilar soni."""
        result = await self._session.execute(
            select(func.count()).select_from(UserModel)
        )
        return int(result.scalar_one())

    async def count_since(self, since: datetime) -> int:
        """Berilgan sanadan keyin qo'shilgan foydalanuvchilar soni."""
        result = await self._session.execute(
            select(func.count())
            .select_from(UserModel)
            .where(UserModel.created_at >= since)
        )
        return int(result.scalar_one())

    @staticmethod
    def _to_entity(model: UserModel) -> User:
        return User(
            id=model.id,
            telegram_id=model.telegram_id,
            username=model.username,
            first_name=model.first_name,
            last_name=model.last_name,
            has_joined_channel=model.has_joined_channel,
            created_at=model.created_at,
        )

    @staticmethod
    def _to_model(user: User) -> UserModel:
        return UserModel(
            id=user.id,
            telegram_id=user.telegram_id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            has_joined_channel=user.has_joined_channel,
            created_at=user.created_at,
        )
