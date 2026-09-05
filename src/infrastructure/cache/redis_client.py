"""
Redis client for caching and session management.
Provides connection pooling and caching utilities.
"""

from __future__ import annotations

import json
from datetime import timedelta

import redis.asyncio as redis
from config.settings import Settings


class RedisClient:
    """Redis client with connection pooling and caching utilities."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._client: redis.Redis | None = None

    async def connect(self) -> None:
        """Establish Redis connection with connection pooling."""
        if self._client is None:
            self._client = redis.from_url(
                self.settings.redis_url,
                encoding="utf-8",
                decode_responses=True,
                max_connections=20,
                socket_connect_timeout=5,
                socket_timeout=5,
            )

    async def disconnect(self) -> None:
        """Close Redis connection."""
        if self._client:
            await self._client.close()
            self._client = None

    @property
    def client(self) -> redis.Redis:
        """Get Redis client (raises error if not connected)."""
        if self._client is None:
            raise RuntimeError("Redis client not connected. Call connect() first.")
        return self._client

    async def get(self, key: str) -> str | None:
        """Get value from cache."""
        return await self.client.get(key)

    async def set(
        self,
        key: str,
        value: str,
        ttl: int | timedelta | None = None,
    ) -> bool:
        """Set value in cache with optional TTL."""
        if ttl is None:
            return await self.client.set(key, value)
        return await self.client.setex(key, ttl, value)

    async def delete(self, key: str) -> int:
        """Delete key from cache."""
        return await self.client.delete(key)

    async def exists(self, key: str) -> bool:
        """Check if key exists in cache."""
        return await self.client.exists(key) > 0

    async def get_json(self, key: str) -> dict | list | None:
        """Get JSON value from cache."""
        value = await self.get(key)
        if value is None:
            return None
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None

    async def set_json(
        self,
        key: str,
        value: dict | list,
        ttl: int | timedelta | None = None,
    ) -> bool:
        """Set JSON value in cache with optional TTL."""
        json_str = json.dumps(value)
        return await self.set(key, json_str, ttl)

    async def increment(self, key: str, amount: int = 1) -> int:
        """Increment counter in cache."""
        return await self.client.incrby(key, amount)

    async def expire(self, key: str, ttl: int | timedelta) -> bool:
        """Set TTL on existing key."""
        if isinstance(ttl, timedelta):
            ttl = int(ttl.total_seconds())
        return await self.client.expire(key, ttl)

    async def ttl(self, key: str) -> int:
        """Get remaining TTL for key."""
        return await self.client.ttl(key)

    async def keys(self, pattern: str = "*") -> list[str]:
        """Get keys matching pattern."""
        return await self.client.keys(pattern)

    async def flush_db(self) -> bool:
        """Flush all keys in current database (use with caution!)."""
        return await self.client.flushdb()


# Global Redis client instance
_redis_client: RedisClient | None = None


async def get_redis_client(settings: Settings) -> RedisClient:
    """Get or create global Redis client instance."""
    global _redis_client
    if _redis_client is None:
        _redis_client = RedisClient(settings)
        await _redis_client.connect()
    return _redis_client


async def close_redis_client() -> None:
    """Close global Redis client instance."""
    global _redis_client
    if _redis_client:
        await _redis_client.disconnect()
        _redis_client = None
