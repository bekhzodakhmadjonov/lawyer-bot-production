"""Redis-based response caching for LLM cost optimization."""

from __future__ import annotations

import hashlib
from enum import Enum

from infrastructure.cache.redis_client import RedisClient


class CacheType(str, Enum):
    """Cache types with different TTLs."""
    INFORMATIONAL = "informational"  # 24h TTL for legal information
    INTAKE = "intake"  # 1h TTL for intake responses
    SEARCH_RESULT = "search_result"  # 1h TTL for search results
    LEGAL_SOURCE = "legal_source"  # 7d TTL for legal source documents


class ResponseCache:
    """Cache LLM responses to reduce API costs with tiered TTLs."""

    # TTLs in seconds
    TTL_INFORMATIONAL = 86400  # 24 hours
    TTL_INTAKE = 3600  # 1 hour
    TTL_SEARCH_RESULT = 3600  # 1 hour
    TTL_LEGAL_SOURCE = 604800  # 7 days

    def __init__(self, redis_client: RedisClient) -> None:
        self._redis = redis_client

    def _generate_cache_key(self, message: str, context: str = "", cache_type: CacheType = CacheType.INFORMATIONAL) -> str:
        """Generate cache key from message, context, and cache type."""
        content = f"{cache_type.value}:{message}:{context}"
        return f"llm_response:{hashlib.sha256(content.encode()).hexdigest()[:16]}"

    def _get_ttl(self, cache_type: CacheType) -> int:
        """Get TTL for cache type."""
        ttl_map = {
            CacheType.INFORMATIONAL: self.TTL_INFORMATIONAL,
            CacheType.INTAKE: self.TTL_INTAKE,
            CacheType.SEARCH_RESULT: self.TTL_SEARCH_RESULT,
            CacheType.LEGAL_SOURCE: self.TTL_LEGAL_SOURCE,
        }
        return ttl_map.get(cache_type, self.TTL_INFORMATIONAL)

    async def get(self, message: str, context: str = "", cache_type: CacheType = CacheType.INFORMATIONAL) -> str | None:
        """Get cached response if available."""
        cache_key = self._generate_cache_key(message, context, cache_type)
        return await self._redis.get(cache_key)

    async def set(self, message: str, response: str, context: str = "", cache_type: CacheType = CacheType.INFORMATIONAL) -> None:
        """Cache response with TTL based on cache type."""
        cache_key = self._generate_cache_key(message, context, cache_type)
        ttl = self._get_ttl(cache_type)
        await self._redis.set(cache_key, response, ttl=ttl)

    async def invalidate(self, message: str, context: str = "", cache_type: CacheType = CacheType.INFORMATIONAL) -> None:
        """Invalidate cached response."""
        cache_key = self._generate_cache_key(message, context, cache_type)
        await self._redis.delete(cache_key)

    async def clear_all(self) -> None:
        """Clear all cached responses (use with caution)."""
        pattern = "llm_response:*"
        keys = await self._redis.keys(pattern)
        if keys:
            for key in keys:
                await self._redis.delete(key)

    async def clear_by_type(self, cache_type: CacheType) -> None:
        """Clear cached responses by type."""
        pattern = f"llm_response:{cache_type.value}:*"
        keys = await self._redis.keys(pattern)
        if keys:
            for key in keys:
                await self._redis.delete(key)
