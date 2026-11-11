"""Maintenance runner with fakes (platform LLD §3.13; PAY-608, C44)."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import pytest
from pycommon import maintenance
from pycommon.db import advisory_lock_key
from pycommon.maintenance import FIRST_TICK_DELAY_S, MaintenanceRunner, MaintenanceTask
from pycommon.testing import FakeClock, JsonLogCapture, OtelMemory
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


class FakeResult:
    def __init__(self, value: bool) -> None:
        self.value = value

    def scalar_one(self) -> bool:
        return self.value


class FakeConn:
    def __init__(self, engine: FakeEngine) -> None:
        self.engine = engine

    async def execute(self, stmt: Any, params: dict[str, Any]) -> FakeResult:
        sql = str(stmt)
        self.engine.log.append((sql, params["k"]))
        if "unlock" in sql and self.engine.unlock_error:
            raise ConnectionError("gone")
        return FakeResult(self.engine.lock_free)

    async def commit(self) -> None:
        self.engine.log.append(("commit", None))

    async def invalidate(self) -> None:
        self.engine.log.append(("invalidate", None))


class FakeEngine:
    def __init__(self, lock_free: bool = True) -> None:
        self.lock_free = lock_free
        self.unlock_error = False
        self.log: list[tuple[str, Any]] = []

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[FakeConn]:
        yield FakeConn(self)


class FakeSession:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *a: object) -> None:
        return None

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[None]:
        try:
            yield
        except Exception:
            self.log.append("rollback")
            raise
        self.log.append("commit")


class FakeSessions:
    def __init__(self) -> None:
        self.log: list[str] = []

    def __call__(self) -> FakeSession:
        return FakeSession(self.log)


def _runner(
    engine: FakeEngine, sf: FakeSessions, tasks: list[MaintenanceTask], clock: FakeClock
) -> MaintenanceRunner:
    return MaintenanceRunner(
        cast(AsyncEngine, engine),
        cast(async_sessionmaker[AsyncSession], sf),
        lock_name="payroll:maintenance",
        tasks=tasks,
        interval_s=60,
        clock=clock,
    )


async def _ok(session: AsyncSession) -> int:
    return 7


async def _boom(session: AsyncSession) -> int:
    raise RuntimeError("purge failed")


async def test_tick_runs_each_task_in_its_own_transaction(
    fake_clock: FakeClock, caplog_json: JsonLogCapture, otel_memory: OtelMemory
) -> None:
    """PAY-608 / C44: a failing task doesn't stop the next; completed/failed events and metrics."""
    engine, sf = FakeEngine(), FakeSessions()
    runner = _runner(
        engine,
        sf,
        [MaintenanceTask("purge_a", _boom), MaintenanceTask("purge_b", _ok)],
        fake_clock,
    )
    assert await runner.tick() is True
    assert sf.log == ["rollback", "commit"]
    key = advisory_lock_key("payroll:maintenance")
    sqls = [entry for entry in engine.log if entry[0] != "commit"]
    assert sqls[0] == ("SELECT pg_try_advisory_lock(:k)", key)
    assert sqls[-1] == ("SELECT pg_advisory_unlock(:k)", key)
    assert engine.log[1] == ("commit", None)  # lock taken, transaction ended at once
    (failed,) = caplog_json.events("maintenance.task.failed")
    assert (failed["task"], failed["error_type"], failed["level"]) == (
        "purge_a",
        "RuntimeError",
        "error",
    )
    (done,) = caplog_json.events("maintenance.task.completed")
    assert (done["task"], done["affected"], done["level"]) == ("purge_b", 7, "info")
    assert "duration_ms" in done
    runs = {(a["task"], a["outcome"]): v for a, v in otel_memory.metric_points("maintenance_runs")}
    assert runs == {("purge_a", "error"): 1, ("purge_b", "ok"): 1}
    durations = {a["task"] for a, _ in otel_memory.metric_points("maintenance_duration_seconds")}
    assert durations == {"purge_a", "purge_b"}


async def test_tick_skips_without_the_lock(
    fake_clock: FakeClock, caplog_json: JsonLogCapture
) -> None:
    """PAY-608: another replica holds the lock → skip, log maintenance.skipped at debug."""
    engine, sf = FakeEngine(lock_free=False), FakeSessions()
    runner = _runner(engine, sf, [MaintenanceTask("t", _ok)], fake_clock)
    assert await runner.tick() is False
    assert sf.log == []
    (skipped,) = caplog_json.events("maintenance.skipped")
    assert skipped["level"] == "debug"
    assert not any("unlock" in e[0] for e in engine.log)


async def test_broken_unlock_invalidates_the_connection(fake_clock: FakeClock) -> None:
    """PAY-608: if unlocking fails the connection is dropped, which releases the lock."""
    engine, sf = FakeEngine(), FakeSessions()
    engine.unlock_error = True
    assert await _runner(engine, sf, [], fake_clock).tick() is True
    assert engine.log[-1] == ("invalidate", None)


async def test_first_tick_after_5_s_then_interval_with_jitter(
    fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PAY-608: first tick 5 s after start, then MAINTENANCE_INTERVAL_S ± 10%."""
    draws = iter([0.9, 1.1, 1.0])
    monkeypatch.setattr(maintenance._rng, "uniform", lambda lo, hi: (lo, hi) and next(draws))
    engine, sf = FakeEngine(), FakeSessions()
    runner = _runner(engine, sf, [MaintenanceTask("t", _ok)], fake_clock)
    stop = asyncio.Event()
    task = asyncio.create_task(runner.run(stop))
    for _ in range(200):
        await asyncio.sleep(0)
        if len(fake_clock.sleeps) >= 4:
            break
    stop.set()
    await asyncio.wait_for(task, 2)
    assert fake_clock.sleeps[:4] == pytest.approx([FIRST_TICK_DELAY_S, 54.0, 66.0, 60.0])
    assert sf.log.count("commit") >= 3


async def test_jitter_bounds(fake_clock: FakeClock) -> None:
    """PAY-608: the delay stays within ±10% of the interval."""
    runner = _runner(FakeEngine(), FakeSessions(), [], fake_clock)
    for _ in range(50):
        assert 54.0 <= runner._next_delay() <= 66.0


async def test_run_returns_promptly_on_stop() -> None:
    """PAY-608: stop during the first-tick wait returns without ticking."""
    engine, sf = FakeEngine(), FakeSessions()
    runner = MaintenanceRunner(
        cast(AsyncEngine, engine),
        cast(async_sessionmaker[AsyncSession], sf),
        lock_name="x",
        tasks=[],
    )
    stop = asyncio.Event()
    task = asyncio.create_task(runner.run(stop))
    await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, 1)
    assert engine.log == []
