"""Delivery numbers on Redis 7 (design §3.3.3; PAY-408)."""

from __future__ import annotations

from typing import Any

import redis.asyncio as aioredis
from pycommon.redis_client import create_redis
from pycommon.testing import RedisAcl

from tests.integration.conftest import GROUP, STREAM, worker_settings


async def _times(r: aioredis.Redis, entry_id: bytes | str) -> int:
    [row] = await r.xpending_range(STREAM, GROUP, min=entry_id, max=entry_id, count=1)
    return int(row["times_delivered"])


async def test_delivery_counts(
    migrated: Any, redis_acl: RedisAcl, admin_db: Any, redis_admin: aioredis.Redis
) -> None:
    """PAY-408: ">" is delivery 1; a pending-first (history) read increments the counter on
    Redis 7 (as Redis documents), and so does XAUTOCLAIM. Pinned (C63): the worker's delivery
    number after a restart is the count XPENDING reports after the history read."""
    r = create_redis(worker_settings(migrated, redis_acl))
    try:
        await r.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
        entry_id = await redis_admin.xadd(STREAM, {"envelope": "{}"})
        await r.xreadgroup(GROUP, "c1", {STREAM: ">"}, count=1)
        assert await _times(r, entry_id) == 1
        await r.xreadgroup(GROUP, "c1", {STREAM: "0"}, count=1)
        assert await _times(r, entry_id) == 2
        await r.xautoclaim(STREAM, GROUP, "c2", min_idle_time=0, start_id="0-0", count=1)
        assert await _times(r, entry_id) == 3
    finally:
        await r.aclose()
