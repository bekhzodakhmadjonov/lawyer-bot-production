"""Redis-based rate limiter for high-performance rate limiting."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from infrastructure.cache.redis_client import RedisClient


class RedisRateLimiter:
    """Redis-based rate limiter with burst detection and progressive penalties."""

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

    def __init__(self, redis_client: RedisClient) -> None:
        self._redis = redis_client

    def _get_user_key(self, user_id: UUID) -> str:
        """Get Redis key for user rate limit data."""
        return f"rate_limit:user:{user_id}"

    def _get_violation_key(self, user_id: UUID) -> str:
        """Get Redis key for user violation data."""
        return f"rate_limit:violation:{user_id}"

    async def check_and_increment_user(
        self, user_id: UUID
    ) -> tuple[bool, datetime | None, str | None]:
        """Check if user request exceeds rate limit using Redis.

        Returns:
            (ruxsat_berildi, reset_vaqti, sabab)
            reset_vaqti — limit tiklanadigan UTC datetime (limit oshganda),
            yoki None (ruxsat berilganda).
            sabab — limit oshganda sabab (hourly/burst/violation), yoki None.
        """
        now = datetime.now(UTC)
        user_key = self._get_user_key(user_id)
        violation_key = self._get_violation_key(user_id)

        # Get current rate limit data from Redis
        rate_data = await self._redis.get_json(user_key)

        if rate_data is None:
            # New user - initialize with first message
            rate_data = {
                "message_count": 1,
                "last_reset": now.isoformat(),
                "recent_timestamps": [now.isoformat()],
            }
            await self._redis.set_json(user_key, rate_data, ttl=int(self.RESET_WINDOW.total_seconds()))
            return True, None, None

        # Parse existing data
        last_reset = datetime.fromisoformat(rate_data["last_reset"]).replace(tzinfo=UTC)
        recent_timestamps = [
            datetime.fromisoformat(ts).replace(tzinfo=UTC)
            for ts in rate_data.get("recent_timestamps", [])
        ]

        # Check if hourly window needs reset
        if now - last_reset >= self.RESET_WINDOW:
            # Hourly window reset
            rate_data["message_count"] = 1
            rate_data["last_reset"] = now.isoformat()
            rate_data["recent_timestamps"] = [now.isoformat()]
        else:
            # Check hourly limit
            if rate_data["message_count"] >= self.MAX_MESSAGES_PER_HOUR:
                reset_at = last_reset + self.RESET_WINDOW
                return False, reset_at, "hourly"
            rate_data["message_count"] += 1

        # Burst detection - clean old timestamps
        recent_timestamps = [
            ts for ts in recent_timestamps if ts > now - self.BURST_WINDOW
        ]

        if len(recent_timestamps) >= self.MAX_BURST_MESSAGES:
            # Burst limit exceeded - apply cooldown
            cooldown_end = now + await self._get_penalty_cooldown(violation_key)
            await self._record_violation(violation_key)
            return False, cooldown_end, "burst"

        # Add new timestamp
        recent_timestamps.append(now)
        rate_data["recent_timestamps"] = [ts.isoformat() for ts in recent_timestamps]

        # Save updated data with TTL
        await self._redis.set_json(user_key, rate_data, ttl=int(self.RESET_WINDOW.total_seconds()))

        return True, None, None

    async def _get_penalty_cooldown(self, violation_key: str) -> timedelta:
        """Get progressive penalty cooldown based on violation history."""
        violation_data = await self._redis.get_json(violation_key)

        if violation_data is None:
            return self.FIRST_VIOLATION_PENALTY

        # Check if violation count should be reset (24 hours)
        last_violation = datetime.fromisoformat(violation_data["last_violation_time"]).replace(tzinfo=UTC)
        if datetime.now(UTC) - last_violation >= self.VIOLATION_RESET_WINDOW:
            await self._redis.delete(violation_key)
            return self.FIRST_VIOLATION_PENALTY

        # Progressive penalty based on violation count
        violation_count = violation_data.get("violation_count", 0)
        if violation_count == 0:
            return self.FIRST_VIOLATION_PENALTY
        elif violation_count == 1:
            return self.SECOND_VIOLATION_PENALTY
        else:
            return self.REPEAT_VIOLATION_PENALTY

    async def _record_violation(self, violation_key: str) -> None:
        """Record violation in Redis."""
        violation_data = await self._redis.get_json(violation_key)

        if violation_data is None:
            violation_data = {
                "violation_count": 1,
                "last_violation_time": datetime.now(UTC).isoformat(),
            }
        else:
            violation_data["violation_count"] += 1
            violation_data["last_violation_time"] = datetime.now(UTC).isoformat()

        # Store with 24 hour TTL
        await self._redis.set_json(violation_key, violation_data, ttl=int(self.VIOLATION_RESET_WINDOW.total_seconds()))

    async def reset_user_limit(self, user_id: UUID) -> None:
        """Reset rate limit for a specific user (admin function)."""
        user_key = self._get_user_key(user_id)
        violation_key = self._get_violation_key(user_id)
        await self._redis.delete(user_key)
        await self._redis.delete(violation_key)
