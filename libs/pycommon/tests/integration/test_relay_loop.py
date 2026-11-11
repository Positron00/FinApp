"""RelayLoop over a throwaway outbox table the test creates (platform LLD §3.9; PAY-402, NFR-02).

pycommon ships no outbox table (C22): ``pyc.outbox`` is created by the integration conftest.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Sequence
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest
from pycommon.db import create_engine, new_id, session_factory
from pycommon.redis_client import create_redis
from pycommon.streams import Envelope, RelayLoop, StreamItem, StreamProducer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.conftest import Pg, RedisServer

pytestmark = pytest.mark.integration

STREAM = "stream:payout.requested"


class OutboxRepo:
    def __init__(self) -> None:
        self.claims: list[tuple[float, int]] = []

    async def claim_unsent(self, session: AsyncSession, *, limit: int) -> Sequence[StreamItem]:
        rows = (
            await session.execute(
                text(
                    "SELECT id, type, envelope FROM outbox WHERE sent_at IS NULL "
                    "ORDER BY created_at, id LIMIT :limit FOR UPDATE SKIP LOCKED"
                ),
                {"limit": limit},
            )
        ).all()
        self.claims.append((time.monotonic(), len(rows)))
        return [StreamItem(r.id, r.type, r.envelope, ref=r.id) for r in rows]

    async def mark_sent(self, session: AsyncSession, items: Sequence[StreamItem]) -> None:
        await session.execute(
            text(
                "UPDATE outbox SET sent_at = now(), relay_attempts = relay_attempts + 1 "
                "WHERE id = ANY(:ids)"
            ),
            {"ids": [i.ref for i in items]},
        )

    async def mark_failed(
        self, session: AsyncSession, items: Sequence[StreamItem], *, error_type: str
    ) -> None:
        await session.execute(
            text(
                "UPDATE outbox SET relay_attempts = relay_attempts + 1, last_error = :e "
                "WHERE id = ANY(:ids)"
            ),
            {"ids": [i.ref for i in items], "e": error_type},
        )


async def _admin(pg: Pg) -> Any:
    return await asyncpg.connect(**pg.dsn())


async def _insert(pg: Pg, n: int) -> None:
    conn = await _admin(pg)
    try:
        rows = []
        for _ in range(n):
            env = Envelope.new(type="payout.requested", version=1, producer="payroll", data={})
            rows.append((new_id(), env.type, env.to_json()))
        await conn.executemany(
            "INSERT INTO pyc.outbox (id, type, envelope) VALUES ($1, $2, $3)", rows
        )
    finally:
        await conn.close()


async def _status(pg: Pg) -> list[Any]:
    conn = await _admin(pg)
    try:
        return list(await conn.fetch("SELECT sent_at, relay_attempts, last_error FROM pyc.outbox"))
    finally:
        await conn.close()


@pytest.fixture
async def clean(pg: Pg, redis_server: RedisServer) -> AsyncIterator[None]:
    yield
    conn = await _admin(pg)
    try:
        await conn.execute("TRUNCATE pyc.outbox")
    finally:
        await conn.close()
    with redis_server.client() as admin:
        admin.flushall()


async def _wait(cond: Any, timeout_s: float = 20) -> None:
    deadline = time.monotonic() + timeout_s
    while not await cond():
        if time.monotonic() > deadline:
            raise TimeoutError("condition not met")
        await asyncio.sleep(0.05)


def _loop(pg: Pg, server: RedisServer, repo: OutboxRepo, **kw: Any) -> tuple[RelayLoop, Any]:
    engine = create_engine(pg.settings())
    redis = create_redis(server.settings("payroll"))
    loop = RelayLoop(
        session_factory(engine), StreamProducer(redis), repo, stream=STREAM, maxlen=100000, **kw
    )
    return loop, (engine, redis)


async def _close(resources: Any) -> None:
    engine, redis = resources
    await redis.aclose()
    await engine.dispose()


async def test_two_loops_never_send_a_row_twice(
    pg: Pg, redis_server: RedisServer, clean: None
) -> None:
    """PAY-402 / NFR-02: two relays on one table (SKIP LOCKED) publish every row exactly once."""
    await _insert(pg, 300)
    (a, ra), (b, rb) = (
        _loop(pg, redis_server, OutboxRepo(), batch_size=25),
        _loop(pg, redis_server, OutboxRepo(), batch_size=25),
    )
    stop = asyncio.Event()
    tasks = [asyncio.create_task(a.run(stop)), asyncio.create_task(b.run(stop))]

    async def all_sent() -> bool:
        return all(r["sent_at"] is not None for r in await _status(pg))

    try:
        await _wait(all_sent)
    finally:
        stop.set()
        await asyncio.gather(*tasks)
        await _close(ra)
        await _close(rb)
    with redis_server.client() as admin:
        entries = admin.xrange(STREAM)
    ids = [fields[b"event_id"] for _, fields in entries]
    assert len(ids) == 300
    assert len(set(ids)) == 300
    assert {r["relay_attempts"] for r in await _status(pg)} == {1}


async def test_redis_down_rows_stay_unsent_then_catch_up(
    pg: Pg, restartable_redis: RedisServer, clean: None
) -> None:
    """PAY-402 / NFR-03: Redis stopped: rows stay unsent (attempts rise), then catch up."""
    await _insert(pg, 5)
    loop, res = _loop(pg, restartable_redis, OutboxRepo(), max_backoff_s=0.3)
    restartable_redis.stop()
    stop = asyncio.Event()
    task = asyncio.create_task(loop.run(stop))
    try:

        async def retried() -> bool:
            rows = await _status(pg)
            return all(r["sent_at"] is None and r["relay_attempts"] >= 2 for r in rows)

        await _wait(retried)
        assert {r["last_error"] for r in await _status(pg)} <= {"ConnectionError", "TimeoutError"}
        await asyncio.to_thread(restartable_redis.start)

        async def sent() -> bool:
            return all(r["sent_at"] is not None for r in await _status(pg))

        await _wait(sent)
    finally:
        stop.set()
        await task
        await _close(res)
    with restartable_redis.client() as admin:
        assert admin.xlen(STREAM) == 5
        admin.flushall()


async def test_full_batch_then_immediate_loop(
    pg: Pg, redis_server: RedisServer, clean: None
) -> None:
    """PAY-402: a full batch of 100 is followed at once by the next claim (no poll wait)."""
    await _insert(pg, 150)
    repo = OutboxRepo()
    loop, res = _loop(pg, redis_server, repo, batch_size=100, poll_interval_s=2.0)
    stop = asyncio.Event()
    task = asyncio.create_task(loop.run(stop))
    try:

        async def two_claims() -> bool:
            return len(repo.claims) >= 2

        await _wait(two_claims)
    finally:
        stop.set()
        await task
        await _close(res)
    (t1, n1), (t2, n2) = repo.claims[:2]
    assert (n1, n2) == (100, 50)
    assert t2 - t1 < 1.0
