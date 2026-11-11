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
from pycommon.testing import JsonLogCapture
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


class CrashedError(Exception):
    """Stands in for the relay process dying: what its claim sees once the connection is gone."""


class CrashingRepo(OutboxRepo):
    """Claims a batch, then stops before publishing (no ``mark_sent``) until ``die`` is set; then
    stops its loop and fails, so the claiming transaction ends without a commit."""

    def __init__(self, stop: asyncio.Event) -> None:
        super().__init__()
        self.claimed = asyncio.Event()
        self.die = asyncio.Event()
        self.backend_pid: int | None = None
        self._stop = stop

    async def claim_unsent(self, session: AsyncSession, *, limit: int) -> Sequence[StreamItem]:
        await super().claim_unsent(session, limit=limit)
        self.backend_pid = (await session.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        self.claimed.set()
        await self.die.wait()  # the rows stay locked and unpublished meanwhile
        self._stop.set()  # a dead process claims nothing more
        raise CrashedError


async def _terminate(pg: Pg, pid: int) -> bool:
    conn = await _admin(pg)
    try:
        return bool(await conn.fetchval("SELECT pg_terminate_backend($1)", pid))
    finally:
        await conn.close()


async def _oldest_unsent_age_s(pg: Pg) -> float:
    """What the services' ``outbox_oldest_age_seconds`` gauge reads: 0 when nothing is unsent."""
    conn = await _admin(pg)
    try:
        age = await conn.fetchval(
            "SELECT coalesce(extract(epoch FROM now() - min(created_at)), 0) "
            "FROM pyc.outbox WHERE sent_at IS NULL"
        )
        return float(age)
    finally:
        await conn.close()


async def test_relay_crash_mid_batch_another_relay_sends_each_row_once(
    pg: Pg, redis_server: RedisServer, clean: None, caplog_json: JsonLogCapture
) -> None:
    """HLD §4 "Outbox relay crash" (PAY-402, NFR-02): a relay claims a batch (rows locked FOR
    UPDATE) and dies before publishing or ``mark_sent``. While its transaction is open the rows stay
    unsent and a second relay skips them (SKIP LOCKED), so the oldest-unsent age grows. The server
    then drops the dead relay's connection (as when its process is killed), which rolls the claim
    back; the second relay publishes every row exactly once and the oldest-unsent age returns
    to 0."""
    await _insert(pg, 7)
    with redis_server.client() as admin:
        admin.delete(STREAM)
    dead_stop = asyncio.Event()
    crashing = CrashingRepo(dead_stop)
    dead, dead_res = _loop(pg, redis_server, crashing, batch_size=100)
    dead_task = asyncio.create_task(dead.run(dead_stop))
    survivor_repo = OutboxRepo()
    survivor, survivor_res = _loop(pg, redis_server, survivor_repo, poll_interval_s=0.1)
    stop = asyncio.Event()
    task: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(crashing.claimed.wait(), timeout=10)
        assert crashing.backend_pid is not None
        task = asyncio.create_task(survivor.run(stop))

        # The survivor polls but claims nothing while the dead relay holds the row locks.
        async def polled_empty() -> bool:
            return len(survivor_repo.claims) >= 3

        await _wait(polled_empty)
        assert all(n == 0 for _, n in survivor_repo.claims)
        assert all(r["sent_at"] is None for r in await _status(pg))
        assert await _oldest_unsent_age_s(pg) > 0
        with redis_server.client() as admin:
            assert admin.xlen(STREAM) == 0

        # The crash: the connection drops; Postgres rolls the claim back and frees the rows.
        assert await _terminate(pg, crashing.backend_pid)
        crashing.die.set()
        await asyncio.wait_for(dead_task, timeout=10)

        async def all_sent() -> bool:
            return all(r["sent_at"] is not None for r in await _status(pg))

        await _wait(all_sent)
    finally:
        stop.set()
        crashing.die.set()  # a failed assertion above: end the claim, free the locks for teardown
        await asyncio.wait_for(asyncio.gather(dead_task, return_exceptions=True), timeout=10)
        if task is not None:
            await task
        await _close(survivor_res)
        await _close(dead_res)

    with redis_server.client() as admin:
        entries = admin.xrange(STREAM)
    ids = [fields[b"event_id"] for _, fields in entries]
    assert len(ids) == 7
    assert len(set(ids)) == 7
    # The dead relay's claim left no trace: one attempt per row, all by the survivor.
    assert {r["relay_attempts"] for r in await _status(pg)} == {1}
    assert await _oldest_unsent_age_s(pg) == 0
    # HLD §4's log event: the dying loop logs its failed batch as outbox.relay.error (the error is
    # the dropped connection's, raised while the claim's transaction ends)
    (error,) = caplog_json.events("outbox.relay.error")
    assert error["error_type"]
