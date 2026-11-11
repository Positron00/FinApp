"""The ``payout-worker`` Redis ACL user (design §3.3.1; PAY-607, R12, C27)."""

from __future__ import annotations

from typing import Any

import pytest
import redis.asyncio as aioredis
from payout_worker.settings import Settings
from pycommon.redis_client import create_redis
from pycommon.testing import RedisAcl
from redis.exceptions import NoPermissionError

from tests.integration.conftest import DLQ, GROUP, STREAM, worker_settings


@pytest.fixture
async def worker_redis(migrated: Any, redis_acl: RedisAcl, admin_db: Any) -> Any:
    s: Settings = worker_settings(migrated, redis_acl)
    client = create_redis(s)
    try:
        yield client
    finally:
        await client.aclose()


async def test_every_worker_command_is_granted(
    worker_redis: aioredis.Redis, redis_admin: aioredis.Redis
) -> None:
    """PAY-607, R12, C27: PING, XGROUP CREATE … MKSTREAM on a missing stream, XREADGROUP,
    XPENDING, XAUTOCLAIM, XACK, XINFO GROUPS/STREAM/CONSUMERS, XGROUP DELCONSUMER, and XADD and
    XINFO on the .dlq key all work as payout-worker."""
    r = worker_redis
    assert await r.ping()
    assert await redis_admin.exists(STREAM) == 0
    await r.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    await redis_admin.xadd(STREAM, {"event_id": "e", "type": "payout.requested", "envelope": "{}"})
    read: Any = await r.xreadgroup(GROUP, "c1", {STREAM: ">"}, count=1)
    entry_id = read[0][1][0][0]
    assert await r.xpending_range(STREAM, GROUP, min=entry_id, max=entry_id, count=1)
    claimed = await r.xautoclaim(STREAM, GROUP, "c2", min_idle_time=0, start_id="0-0", count=1)
    assert claimed[1]
    assert await r.xinfo_groups(STREAM)
    assert await r.xinfo_stream(STREAM)
    assert await r.xinfo_consumers(STREAM, GROUP)
    assert await r.xack(STREAM, GROUP, entry_id) == 1
    assert await r.xgroup_delconsumer(STREAM, GROUP, "c1") == 0
    await r.xadd(DLQ, {"envelope": "", "source_id": "1-0"}, maxlen=10_000, approximate=True)
    assert (await r.xinfo_stream(DLQ))["length"] == 1


async def test_worker_cannot_publish_or_read_other_keys(worker_redis: aioredis.Redis) -> None:
    """PAY-607: the worker can't XADD to stream:payout.requested, can't read web:sess:* and
    can't consume the DLQ or run scripts."""
    r = worker_redis
    with pytest.raises(NoPermissionError):
        await r.xadd(STREAM, {"event_id": "e"})
    with pytest.raises(NoPermissionError):
        await r.get("web:sess:abc")
    with pytest.raises(NoPermissionError):
        await r.set("payout-worker:x", "1")
    with pytest.raises(NoPermissionError):
        await r.xreadgroup(GROUP, "c", {DLQ: ">"})
    with pytest.raises(NoPermissionError):
        await r.eval("return 1", 0)
