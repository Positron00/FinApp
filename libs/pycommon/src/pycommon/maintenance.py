"""Advisory-lock periodic maintenance runner (platform LLD §3.13; PAY-608, C44).

pycommon owns no tables and ships no purge tasks: services supply them.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from pycommon import Clock, SystemClock
from pycommon.db import advisory_lock_key
from pycommon.logging import get_logger, log_event
from pycommon.otel import SECONDS_BUCKETS, histogram, meter

__all__ = ["FIRST_TICK_DELAY_S", "MaintenanceRunner", "MaintenanceTask"]

FIRST_TICK_DELAY_S: Final = 5.0

_log = get_logger("pycommon.maintenance")
_rng = random.SystemRandom()


@dataclass(frozen=True)
class MaintenanceTask:
    name: str
    fn: Callable[[AsyncSession], Awaitable[int]]


class MaintenanceRunner:
    def __init__(
        self,
        engine: AsyncEngine,
        sf: async_sessionmaker[AsyncSession],
        *,
        lock_name: str,
        tasks: Sequence[MaintenanceTask],
        interval_s: float = 60,
        jitter: float = 0.1,
        clock: Clock | None = None,
    ) -> None:
        self._engine = engine
        self._sf = sf
        self._lock_name = lock_name
        self._key = advisory_lock_key(lock_name)
        self._tasks = tuple(tasks)
        self._interval = interval_s
        self._jitter = jitter
        self._clock = clock or SystemClock()
        self._runs = meter().create_counter(
            "maintenance_runs", unit="1", description="Maintenance task runs per outcome"
        )
        self._duration = histogram(
            "maintenance_duration_seconds",
            unit="s",
            description="Maintenance task duration",
            buckets=SECONDS_BUCKETS,
        )

    def _next_delay(self) -> float:
        """``interval_s`` ± ``jitter`` (10%)."""
        return self._interval * _rng.uniform(1 - self._jitter, 1 + self._jitter)

    async def tick(self) -> bool:
        """Run every task once if this replica gets the lock; ``True`` when it ran."""
        async with self._engine.connect() as conn:
            got = (
                await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": self._key})
            ).scalar_one()
            # Session-level lock: end the transaction so the connection doesn't sit idle in one.
            await conn.commit()
            if not got:
                log_event(_log, logging.DEBUG, "maintenance.skipped", lock=self._lock_name)
                return False
            try:
                for task in self._tasks:
                    await self._run_task(task)
            finally:
                try:
                    await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": self._key})
                    await conn.commit()
                except Exception:
                    # The connection is broken; Postgres releases the lock with the session.
                    await conn.invalidate()
        return True

    async def _run_task(self, task: MaintenanceTask) -> None:
        start = self._clock.monotonic()
        try:
            async with self._sf() as session, session.begin():
                affected = await task.fn(session)
        except Exception as exc:
            duration = self._clock.monotonic() - start
            self._runs.add(1, {"task": task.name, "outcome": "error"})
            self._duration.record(duration, {"task": task.name})
            log_event(
                _log,
                logging.ERROR,
                "maintenance.task.failed",
                task=task.name,
                error_type=type(exc).__name__,
            )
            return
        duration = self._clock.monotonic() - start
        self._runs.add(1, {"task": task.name, "outcome": "ok"})
        self._duration.record(duration, {"task": task.name})
        log_event(
            _log,
            logging.INFO,
            "maintenance.task.completed",
            task=task.name,
            affected=affected,
            duration_ms=int(duration * 1000),
        )

    async def _sleep_or_stop(self, seconds: float, stop: asyncio.Event) -> None:
        sleeper = asyncio.ensure_future(self._clock.sleep(seconds))
        stopper = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait({sleeper, stopper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for f in (sleeper, stopper):
                f.cancel()
            await asyncio.gather(sleeper, stopper, return_exceptions=True)

    async def run(self, stop: asyncio.Event) -> None:
        """First tick 5 s after start-up, then every ``interval_s`` ± jitter until ``stop``."""
        await self._sleep_or_stop(FIRST_TICK_DELAY_S, stop)
        while not stop.is_set():
            await self.tick()
            await self._sleep_or_stop(self._next_delay(), stop)
