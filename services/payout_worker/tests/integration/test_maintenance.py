"""Maintenance: the processed_events purge and idle-consumer pruning (design §3.8; PAY-608, C44,
C47)."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import redis.asyncio as aioredis
from payout_worker.maintenance import build_runner, prune_consumers
from pycommon.db import create_engine, session_factory
from pycommon.redis_client import create_redis
from pycommon.testing import JsonLogCapture, PgBootstrapped, RedisAcl

from tests.integration.conftest import GROUP, STREAM, wait_for, worker_settings

INSERT = (
    "INSERT INTO payouts.processed_events (event_id, stream, payout_id, attempt, company_id, "
    "outcome, processed_at) VALUES ($1, 'stream:payout.requested', $2, 1, $3, 'settled', "
    "now() - make_interval(days => $4))"
)


async def test_purge_old_processed_events(
    admin_db: Any, migrated: PgBootstrapped, redis_acl: RedisAcl, caplog_json: JsonLogCapture
) -> None:
    """PAY-608: rows older than RETENTION_PROCESSED_EVENTS_DAYS (30) go, newer stay; the task
    logs maintenance.task.completed with the count (C44); a held lock skips the tick."""
    for days in (31, 45, 29, 1):
        await admin_db.execute(INSERT, uuid4(), uuid4(), uuid4(), days)
    s = worker_settings(migrated, redis_acl)
    engine = create_engine(s)
    try:
        runner = build_runner(s, engine, session_factory(engine))
        assert await runner.tick()
        assert await admin_db.fetchval("SELECT count(*) FROM payouts.processed_events") == 2
        [event] = caplog_json.events("maintenance.task.completed")
        assert (event["task"], event["affected"]) == ("purge_processed_events", 2)
        other = await engine.connect()
        try:
            from pycommon.db import advisory_lock_key  # noqa: PLC0415
            from sqlalchemy import text  # noqa: PLC0415

            await other.execute(
                text("SELECT pg_advisory_lock(:k)"),
                {"k": advisory_lock_key("payout-worker:maintenance")},
            )
            await other.commit()
            assert not await runner.tick()
        finally:
            await other.close()
    finally:
        await engine.dispose()


async def _consumers(r: aioredis.Redis) -> dict[str, dict[str, Any]]:
    rows: Any = await r.xinfo_consumers(STREAM, GROUP)
    return {(c["name"].decode() if isinstance(c["name"], bytes) else c["name"]): c for c in rows}


async def test_prune_consumers_on_real_redis(
    admin_db: Any,
    migrated: PgBootstrapped,
    redis_acl: RedisAcl,
    redis_admin: aioredis.Redis,
    caplog_json: JsonLogCapture,
) -> None:
    """PAY-608, R12, C27, C47: as the payout-worker ACL user, the tick removes a consumer idle
    longer than PAYOUT_CONSUMER_GC_IDLE_S with no pending entries (XGROUP DELCONSUMER), keeps an
    idle one that holds a pending entry, this consumer and a fresh one; the pending entry is
    still in the PEL; maintenance.task.completed reports the count (C44)."""
    s = worker_settings(migrated, redis_acl, payout_consumer_name="me", payout_consumer_gc_idle_s=1)
    await redis_admin.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    await redis_admin.xadd(STREAM, {"event_id": "e", "type": "payout.requested", "envelope": "{}"})
    await redis_admin.xreadgroup(GROUP, "old-pending", {STREAM: ">"}, count=1)
    for name in ("old-empty", "me"):
        await redis_admin.xreadgroup(GROUP, name, {STREAM: ">"}, count=1)

    async def idle_past_threshold() -> bool:
        cs = await _consumers(redis_admin)
        return all(int(cs[n]["idle"]) > 1000 for n in ("old-empty", "old-pending", "me"))

    await wait_for(idle_past_threshold, within=10)
    await redis_admin.xreadgroup(GROUP, "fresh", {STREAM: ">"}, count=1)
    engine = create_engine(s)
    redis = create_redis(s)
    try:
        runner = build_runner(s, engine, session_factory(engine), redis=redis)
        assert await runner.tick()
    finally:
        await redis.aclose()
        await engine.dispose()
    cs = await _consumers(redis_admin)
    assert set(cs) == {"old-pending", "me", "fresh"}
    assert int(cs["old-pending"]["pending"]) == 1
    assert (await redis_admin.xpending(STREAM, GROUP))["pending"] == 1
    done = {e["task"]: e["affected"] for e in caplog_json.events("maintenance.task.completed")}
    assert done == {"purge_processed_events": 0, "prune_consumers": 1}
    [pruned] = caplog_json.events("queue.consumer.pruned")
    assert (pruned["consumer"], pruned["dropped_pending"]) == ("old-empty", 0)


async def test_prune_lost_stream_or_group_on_real_redis(
    migrated: PgBootstrapped, redis_acl: RedisAcl, redis_admin: aioredis.Redis
) -> None:
    """PAY-608: the real Redis 7 errors for a missing stream (ERR no such key) and a missing
    group (NOGROUP), as the payout-worker ACL user, both mean nothing to prune."""
    s = worker_settings(migrated, redis_acl)
    redis = create_redis(s)
    try:
        await redis_admin.delete(STREAM)
        assert await prune_consumers(redis, s) == 0  # no stream
        await redis_admin.xadd(
            STREAM, {"event_id": "e", "type": "payout.requested", "envelope": "{}"}
        )
        assert await prune_consumers(redis, s) == 0  # stream without the group
    finally:
        await redis.aclose()
