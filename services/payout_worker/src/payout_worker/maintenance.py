"""Maintenance tasks for pycommon's ``MaintenanceRunner`` (design §3.8; PAY-608, C44).

Lock ``payout-worker:maintenance``, every ``MAINTENANCE_INTERVAL_S``. S6 ships the
``processed_events`` purge; ``prune_consumers`` (XGROUP DELCONSUMER) comes with F4b.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from pycommon import Clock, SystemClock
from pycommon.maintenance import MaintenanceRunner, MaintenanceTask
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from payout_worker.db.repo import purge_processed_events
from payout_worker.settings import Settings

__all__ = ["LOCK_NAME", "build_runner", "purge_task"]

LOCK_NAME: Final = "payout-worker:maintenance"


def purge_task(settings: Settings, clock: Clock | None = None) -> MaintenanceTask:
    c = clock or SystemClock()
    days = settings.retention_processed_events_days

    async def run(session: AsyncSession) -> int:
        return await purge_processed_events(session, c.now() - timedelta(days=days))

    return MaintenanceTask("purge_processed_events", run)


def build_runner(
    settings: Settings,
    engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
    clock: Clock | None = None,
) -> MaintenanceRunner:
    return MaintenanceRunner(
        engine,
        sessions,
        lock_name=LOCK_NAME,
        tasks=[purge_task(settings, clock)],
        interval_s=settings.maintenance_interval_s,
        clock=clock,
    )
