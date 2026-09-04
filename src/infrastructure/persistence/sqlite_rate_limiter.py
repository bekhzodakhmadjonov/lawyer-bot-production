"""SQLite asosidagi oddiy rate limiter."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from infrastructure.persistence.database import RateLimitModel


class SQLiteRateLimiter:
    """SQLite orqali foydalanuvchi xabarlarini limitlaydi."""

    MAX_MESSAGES_PER_HOUR = 60  # soatiga 60 xabar = taxminan har daqiqada 1
    RESET_WINDOW = timedelta(hours=1)

    # Burst detection settings
    MAX_BURST_MESSAGES = 10  # 1 daiqiqada maksimum 10 xabar
    BURST_WINDOW = timedelta(minutes=1)

    # Progressive penalty settings
    FIRST_VIOLATION_PENALTY = timedelta(minutes=0)  # Warning only
    SECOND_VIOLATION_PENALTY = timedelta(minutes=30)  # 30 minute cooldown
    REPEAT_VIOLATION_PENALTY = timedelta(hours=1)  # 1 hour cooldown
    VIOLATION_RESET_WINDOW = timedelta(hours=24)  # Reset violation count after 24h

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def check_and_increment_user(
        self, user_id: UUID
    ) -> tuple[bool, datetime | None, str | None]:
        """Foydalanuvchi so'rovi limitdan o'tganini tekshiradi.

        Returns:
            (ruxsat_berildi, reset_vaqti, sabab)
            reset_vaqti — limit tiklanadigan UTC datetime (limit oshganda),
            yoki None (ruxsat berilganda).
            sabab — limit oshganda sabab (hourly/burst/violation), yoki None.
        """
        now = datetime.now(UTC)
        stmt = select(RateLimitModel).where(RateLimitModel.user_id == user_id)
        result = await self._session.execute(stmt)
        rate_limit = result.scalar_one_or_none()

        if rate_limit is None:
            # Yangi foydalanuvchi - yaratish
            rate_limit = RateLimitModel(
                user_id=user_id,
                message_count=1,
                last_reset=now,
                recent_timestamps=json.dumps([now.isoformat()]),
                violation_count=0,
            )
            self._session.add(rate_limit)
            return True, None, None

        # Hourly limit tekshirish
        last_reset = rate_limit.last_reset
        if last_reset.tzinfo is None:
            last_reset = last_reset.replace(tzinfo=UTC)

        if now - last_reset >= self.RESET_WINDOW:
            # Hourly window reset
            rate_limit.message_count = 1
            rate_limit.last_reset = now
            rate_limit.recent_timestamps = json.dumps([now.isoformat()])
        else:
            # Hourly limit tekshirish
            if rate_limit.message_count >= self.MAX_MESSAGES_PER_HOUR:
                reset_at = last_reset + self.RESET_WINDOW
                return False, reset_at, "hourly"
            rate_limit.message_count += 1

        # Burst detection tekshirish
        recent_timestamps = json.loads(rate_limit.recent_timestamps or "[]")
        # Eski timestamplarni o'chirish (burst window dan tashqari)
        recent_timestamps = [
            ts
            for ts in recent_timestamps
            if datetime.fromisoformat(ts).replace(tzinfo=UTC) > now - self.BURST_WINDOW
        ]

        if len(recent_timestamps) >= self.MAX_BURST_MESSAGES:
            # Burst limit oshdi - cooldown qo'llash
            cooldown_end = now + self._get_penalty_cooldown(rate_limit)
            self._record_violation(rate_limit, now)
            return False, cooldown_end, "burst"

        # Yangi timestamp qo'shish
        recent_timestamps.append(now.isoformat())
        rate_limit.recent_timestamps = json.dumps(recent_timestamps)

        return True, None, None

    def _get_penalty_cooldown(self, rate_limit: RateLimitModel) -> timedelta:
        """Progressive penalty asosida cooldown vaqtini qaytaradi."""
        # Violation count reset tekshirish
        if rate_limit.last_violation_time:
            last_violation = rate_limit.last_violation_time
            if last_violation.tzinfo is None:
                last_violation = last_violation.replace(tzinfo=UTC)

            if datetime.now(UTC) - last_violation >= self.VIOLATION_RESET_WINDOW:
                # 24 soat o'tdi, violation countni reset qilish
                rate_limit.violation_count = 0
                rate_limit.last_violation_time = None
                return self.FIRST_VIOLATION_PENALTY

        # Progressive penalty
        violation_count = rate_limit.violation_count
        if violation_count == 0:
            return self.FIRST_VIOLATION_PENALTY
        elif violation_count == 1:
            return self.SECOND_VIOLATION_PENALTY
        else:
            return self.REPEAT_VIOLATION_PENALTY

    def _record_violation(self, rate_limit: RateLimitModel, now: datetime) -> None:
        """Violatsiyani qayd qiladi."""
        rate_limit.violation_count += 1
        rate_limit.last_violation_time = now
