"""Rate limiting behind a Protocol: Redis in production, in-memory in unit tests.

Algorithm: fixed window counter (``INCR`` + ``EXPIRE NX`` in one atomic pipeline).

Alternatives considered:

* **Sliding window log** (sorted set per key): exact, but O(n) memory per key.
* **Token bucket** (Lua script): smoother bursts, more code.
* **Fixed window** (chosen): two O(1) commands. The known weakness is a 2x burst at
  the window edge, which is acceptable for login throttling. Swap the implementation
  behind the Protocol if an endpoint ever needs precise shaping.

Fail-open vs fail-closed: if Redis is down, login keeps working (fail-open) and an
error is logged. Locking every user out because a cache is unavailable is the worse
failure for this product. A bank would choose differently.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int


class RateLimiter(Protocol):
    async def hit(self, key: str, *, limit: int, window_seconds: int) -> RateLimitResult:
        """Count one event for ``key`` and report whether it's within ``limit``."""
        ...


class RedisRateLimiter:
    def __init__(self, redis: Redis, *, namespace: str = "ratelimit") -> None:
        self._redis = redis
        self._namespace = namespace

    async def hit(self, key: str, *, limit: int, window_seconds: int) -> RateLimitResult:
        window = int(time.time() // window_seconds)
        redis_key = f"{self._namespace}:{key}:{window}"
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incr(redis_key)
                pipe.expire(redis_key, window_seconds, nx=True)
                count, _ = await pipe.execute()
        except RedisError:
            logger.exception("rate limiter unavailable; failing open", extra={"key": key})
            return RateLimitResult(True, limit, limit, 0)

        count = int(count)
        retry_after = window_seconds - int(time.time() % window_seconds)
        return RateLimitResult(
            allowed=count <= limit,
            limit=limit,
            remaining=max(0, limit - count),
            retry_after_seconds=retry_after if count > limit else 0,
        )


class InMemoryRateLimiter:
    """Single-process limiter for tests and local tooling. Not for production."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    async def hit(self, key: str, *, limit: int, window_seconds: int) -> RateLimitResult:
        window = math.floor(time.time() / window_seconds)
        bucket = f"{key}:{window}"
        self._counts[bucket] = self._counts.get(bucket, 0) + 1
        count = self._counts[bucket]
        return RateLimitResult(
            allowed=count <= limit,
            limit=limit,
            remaining=max(0, limit - count),
            retry_after_seconds=window_seconds if count > limit else 0,
        )

    def reset(self) -> None:
        self._counts.clear()
