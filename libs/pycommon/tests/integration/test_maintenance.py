"""MaintenanceRunner with real advisory locks (platform LLD §3.13; PAY-608, C44).

Each service's own suite proves its purge windows; this proves the runner.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest
from pycommon.db import advisory_lock_key, create_engine, session_factory
from pycommon.maintenance import MaintenanceRunner, MaintenanceTask
from pycommon.testing import JsonLogCapture
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from tests.integration.conftest import Pg

pytestmark = pytest.mark.integration

LOCK = "pyc:maintenance"


@pytest.fixture
async def engines(pg: Pg) -> AsyncIterator[tuple[AsyncEngine, AsyncEngine]]:
    a, b = create_engine(pg.settings()), create_engine(pg.settings())
    try:
        yield a, b
    finally:
        await a.dispose()
        await b.dispose()


def _runner(engine: AsyncEngine, tasks: list[MaintenanceTask]) -> MaintenanceRunner:
    return MaintenanceRunner(engine, session_factory(engine), lock_name=LOCK, tasks=tasks)


async def test_only_one_replica_runs_per_tick(
    engines: tuple[AsyncEngine, AsyncEngine], caplog_json: JsonLogCapture
) -> None:
    """PAY-608: two runners on one DB: one holds the advisory lock and runs, the other skips."""
    runs: list[str] = []
    started = asyncio.Event()

    async def slow(session: AsyncSession) -> int:
        runs.append("ran")
        started.set()
        await asyncio.sleep(0.5)
        return 0

    a = _runner(engines[0], [MaintenanceTask("slow", slow)])
    b = _runner(engines[1], [MaintenanceTask("slow", slow)])
    first = asyncio.create_task(a.tick())
    await started.wait()
    assert await b.tick() is False
    assert await first is True
    assert runs == ["ran"]
    assert caplog_json.events("maintenance.skipped")
    assert await b.tick() is True  # released after the tick
    assert runs == ["ran", "ran"]


async def test_lock_released_when_its_connection_dies(
    pg: Pg, engines: tuple[AsyncEngine, AsyncEngine]
) -> None:
    """PAY-608: a killed lock holder releases the lock; the next tick runs."""
    holder = await asyncpg.connect(**pg.dsn("pyc_app"))
    admin = await asyncpg.connect(**pg.dsn())
    try:
        assert await holder.fetchval("SELECT pg_try_advisory_lock($1)", advisory_lock_key(LOCK))
        runner = _runner(engines[0], [])
        assert await runner.tick() is False
        pid = await holder.fetchval("SELECT pg_backend_pid()")
        assert await admin.fetchval("SELECT pg_terminate_backend($1)", pid)
        for _ in range(50):
            if await runner.tick():
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("lock was not released")
    finally:
        await admin.close()
        if not holder.is_closed():
            await holder.close()


async def test_task_affected_count_is_logged_and_committed(
    pg: Pg, engines: tuple[AsyncEngine, AsyncEngine], caplog_json: JsonLogCapture
) -> None:
    """PAY-608 / C44: maintenance.task.completed carries the task's affected count; work commits."""
    admin: Any = await asyncpg.connect(**pg.dsn())
    try:
        await admin.execute("TRUNCATE pyc.outbox")
        await admin.execute(
            "INSERT INTO pyc.outbox (id, type, envelope, sent_at, created_at) "
            "SELECT gen_random_uuid(), 't', '{}', now(), now() - interval '8 days' "
            "FROM generate_series(1, 4)"
        )
        await admin.execute(
            "INSERT INTO pyc.outbox (id, type, envelope, sent_at) "
            "VALUES (gen_random_uuid(), 't', '{}', now())"
        )

        async def purge(session: AsyncSession) -> int:
            result = await session.execute(
                text("DELETE FROM outbox WHERE created_at < now() - interval '7 days'")
            )
            return int(result.rowcount)  # type: ignore[attr-defined]

        async def broken(session: AsyncSession) -> int:
            await session.execute(text("DELETE FROM no_such_table"))
            return 0

        runner = _runner(
            engines[0], [MaintenanceTask("broken", broken), MaintenanceTask("purge_outbox", purge)]
        )
        assert await runner.tick() is True
        (done,) = caplog_json.events("maintenance.task.completed")
        assert (done["task"], done["affected"]) == ("purge_outbox", 4)
        (failed,) = caplog_json.events("maintenance.task.failed")
        assert failed["task"] == "broken"
        assert await admin.fetchval("SELECT count(*) FROM pyc.outbox") == 1
    finally:
        await admin.execute("TRUNCATE pyc.outbox")
        await admin.close()
