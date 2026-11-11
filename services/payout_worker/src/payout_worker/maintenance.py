"""Maintenance tasks for pycommon's ``MaintenanceRunner`` (design §3.8; PAY-608, C44, C47).

Lock ``payout-worker:maintenance``, every ``MAINTENANCE_INTERVAL_S``, one replica at a time:

* ``purge_processed_events``: ``processed_events`` older than ``RETENTION_PROCESSED_EVENTS_DAYS``.
* ``prune_consumers``: ``XGROUP DELCONSUMER`` for consumers idle longer than
  ``PAYOUT_CONSUMER_GC_IDLE_S`` **with no pending entries**, never this consumer (R12, C27, C47).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Final

from pycommon import Clock, SystemClock
from pycommon.logging import get_logger, log_event
from pycommon.maintenance import MaintenanceRunner, MaintenanceTask
from redis.asyncio import Redis
from redis.exceptions import ResponseError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from payout_worker.db.repo import purge_processed_events
from payout_worker.settings import Settings

__all__ = ["LOCK_NAME", "build_runner", "prune_consumers", "prune_task", "purge_task"]

LOCK_NAME: Final = "payout-worker:maintenance"
_log: Final = get_logger("payout_worker.maintenance")


def purge_task(settings: Settings, clock: Clock | None = None) -> MaintenanceTask:
    c = clock or SystemClock()
    days = settings.retention_processed_events_days

    async def run(session: AsyncSession) -> int:
        return await purge_processed_events(session, c.now() - timedelta(days=days))

    return MaintenanceTask("purge_processed_events", run)


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


async def prune_consumers(redis: Redis, settings: Settings) -> int:
    """Delete idle consumers that hold no pending entries; returns the number deleted.

    ``XINFO CONSUMERS`` then ``XGROUP DELCONSUMER`` per candidate. A consumer with pending
    entries is never deleted (DELCONSUMER would drop its PEL entries, so they would never be
    redelivered); XAUTOCLAIM moves those entries to a live consumer first, and a later tick
    deletes the emptied one. XAUTOCLAIM only ever moves entries *to* the claimer, so an idle
    consumer can't gain entries between the XINFO and the DELCONSUMER. A lost stream or group
    (NOGROUP, no such key) means nothing to prune.
    """
    stream, group = settings.payout_stream, settings.payout_group
    threshold_ms = settings.payout_consumer_gc_idle_s * 1000
    try:
        consumers: Any = await redis.xinfo_consumers(stream, group)
    except ResponseError:
        return 0
    deleted = 0
    for c in consumers:
        name = _text(c.get("name", ""))
        pending = int(c.get("pending", 0))
        idle_ms = int(c.get("idle", 0))
        if name == settings.payout_consumer_name or pending > 0 or idle_ms <= threshold_ms:
            continue
        dropped = int(await redis.xgroup_delconsumer(stream, group, name))
        deleted += 1
        log_event(
            _log,
            logging.WARNING if dropped else logging.INFO,
            "queue.consumer.pruned",
            consumer=name,
            idle_ms=idle_ms,
            dropped_pending=dropped,
        )
    return deleted


def prune_task(settings: Settings, redis: Redis) -> MaintenanceTask:
    async def run(_session: AsyncSession) -> int:  # the session is unused (design §3.8)
        return await prune_consumers(redis, settings)

    return MaintenanceTask("prune_consumers", run)


def build_runner(
    settings: Settings,
    engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
    clock: Clock | None = None,
    *,
    redis: Redis | None = None,
) -> MaintenanceRunner:
    tasks = [purge_task(settings, clock)]
    if redis is not None:
        tasks.append(prune_task(settings, redis))
    return MaintenanceRunner(
        engine,
        sessions,
        lock_name=LOCK_NAME,
        tasks=tasks,
        interval_s=settings.maintenance_interval_s,
        clock=clock,
    )
