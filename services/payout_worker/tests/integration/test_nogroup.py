"""NOGROUP recovery on real Redis 7 (design §3.3.6; PAY-408, PAY-407; pulled forward into S6)."""

from __future__ import annotations

import pytest
import redis.asyncio as aioredis
from pycommon.testing import JsonLogCapture

from tests.helpers import ACCOUNT
from tests.integration.conftest import GROUP, STREAM, Stack, runtime, wait_for


async def _group_exists(r: aioredis.Redis) -> bool:
    try:
        groups = await r.xinfo_groups(STREAM)
    except aioredis.ResponseError:
        return False
    return any(g["name"] in {GROUP, GROUP.encode()} for g in groups)


async def _reading(r: aioredis.Redis, consumer: str) -> bool:
    """The reader has run XREADGROUP (its consumer exists), so it is past its own group check."""
    try:
        consumers = await r.xinfo_consumers(STREAM, GROUP)
    except aioredis.ResponseError:
        return False
    return any(c["name"] in {consumer, consumer.encode()} for c in consumers)


@pytest.mark.parametrize("loss", ["xgroup_destroy", "flushall"])
async def test_group_lost_is_recreated_and_consumption_continues(
    stack: Stack, caplog_json: JsonLogCapture, loss: str
) -> None:
    """PAY-408: the group (XGROUP DESTROY) or the whole keyspace (FLUSHALL) is lost while the
    worker runs → NOGROUP → XGROUP CREATE … 0 MKSTREAM, queue.group.recreated logged with the
    stream and group, and entries published afterwards are consumed (from 0, so an entry
    written before the group is back is delivered too)."""
    app = await stack.worker()
    await wait_for(lambda: _reading(stack.redis, runtime(app).consumer.name))
    if loss == "flushall":
        await stack.redis.flushall()
    else:
        await stack.redis.xgroup_destroy(STREAM, GROUP)
    run = await stack.approved_run([ACCOUNT])

    async def paid() -> bool:
        return [p["status"] for p in await stack.payouts(run["id"])] == ["paid"]

    await wait_for(paid)
    events = caplog_json.events("queue.group.recreated")
    assert events
    assert (events[0]["stream"], events[0]["group"]) == (STREAM, GROUP)
    assert await _group_exists(stack.redis)
    assert await stack.transfers() == 1
    assert runtime(app).consumer.is_alive()

    async def acked() -> bool:
        return await stack.pending() == 0

    await wait_for(acked)
