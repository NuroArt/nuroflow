from __future__ import annotations

import secrets
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis

from nuroflow.core.rate_limit import RedisRateLimiter

pytestmark = pytest.mark.integration


@pytest.fixture
async def redis(redis_url: str) -> AsyncIterator[Redis]:
    client = Redis.from_url(redis_url, decode_responses=True)
    yield client
    await client.aclose()


async def test_counts_within_window_and_blocks_over_limit(redis: Redis) -> None:
    limiter = RedisRateLimiter(redis, namespace=f"test-{secrets.token_hex(4)}")
    results = [await limiter.hit("login:x", limit=3, window_seconds=60) for _ in range(4)]
    assert [r.allowed for r in results] == [True, True, True, False]
    assert [r.remaining for r in results] == [2, 1, 0, 0]
    assert 0 < results[-1].retry_after_seconds <= 60


async def test_keys_expire_with_the_window(redis: Redis) -> None:
    namespace = f"test-{secrets.token_hex(4)}"
    await RedisRateLimiter(redis, namespace=namespace).hit("k", limit=5, window_seconds=30)
    keys = [k async for k in redis.scan_iter(f"{namespace}:*")]
    assert len(keys) == 1
    assert 0 < await redis.ttl(keys[0]) <= 30


async def test_fails_open_when_redis_is_down() -> None:
    dead = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    try:
        result = await RedisRateLimiter(dead).hit("k", limit=1, window_seconds=60)
        assert result.allowed
    finally:
        await dead.aclose()
