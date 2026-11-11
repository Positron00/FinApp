"""``prune_consumers`` over a fake Redis (design §3.8; PAY-608, R12, C27, C47)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from payout_worker.maintenance import build_runner, prune_consumers, prune_task
from pycommon.testing import JsonLogCapture
from redis.asyncio import Redis
from redis.exceptions import NoPermissionError, ResponseError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tests.helpers import make_settings

HOUR_MS = 3_600_000


@dataclass
class ConsumersRedis:
    consumers: list[dict[str, Any]] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    xinfo_error: Exception | None = None
    dropped: int = 0

    async def xinfo_consumers(self, stream: str, group: str) -> list[dict[str, Any]]:
        assert (stream, group) == ("stream:payout.requested", "payout-worker")
        if self.xinfo_error is not None:
            raise self.xinfo_error
        return self.consumers

    async def xgroup_delconsumer(self, stream: str, group: str, name: str) -> int:
        self.deleted.append(name)
        return self.dropped


def _c(name: str, pending: int, idle_ms: int) -> dict[str, Any]:
    return {"name": name.encode(), "pending": pending, "idle": idle_ms, "inactive": idle_ms}


async def test_only_idle_consumers_without_pending_are_deleted(
    caplog_json: JsonLogCapture,
) -> None:
    """PAY-608, C47: XGROUP DELCONSUMER only for consumers idle longer than
    PAYOUT_CONSUMER_GC_IDLE_S with pending == 0; never this consumer; never one with pending
    entries (its PEL would be lost); never one at or under the threshold."""
    s = make_settings(payout_consumer_name="me", payout_consumer_gc_idle_s=3600)
    redis = ConsumersRedis(
        [
            _c("old-empty", 0, HOUR_MS + 1),
            _c("old-pending", 2, 10 * HOUR_MS),
            _c("me", 0, 10 * HOUR_MS),
            _c("at-threshold", 0, HOUR_MS),
            _c("fresh", 0, 5),
            _c("older-empty", 0, 48 * HOUR_MS),
        ]
    )
    assert await prune_consumers(cast(Redis, redis), s) == 2
    assert redis.deleted == ["old-empty", "older-empty"]
    events = caplog_json.events("queue.consumer.pruned")
    assert [(e["consumer"], e["dropped_pending"], e["level"]) for e in events] == [
        ("old-empty", 0, "info"),
        ("older-empty", 0, "info"),
    ]


@pytest.mark.parametrize(
    "error",
    [
        ResponseError("NOGROUP No such consumer group 'payout-worker' for key name 'stream'"),
        ResponseError("no such key"),  # ERR no such key, as redis-py raises it
    ],
)
async def test_missing_group_prunes_nothing(error: ResponseError) -> None:
    """PAY-608: NOGROUP or no stream (lost keyspace) → nothing to prune, no error."""
    redis = ConsumersRedis(xinfo_error=error)
    assert await prune_consumers(cast(Redis, redis), make_settings()) == 0
    assert redis.deleted == []


@pytest.mark.parametrize(
    "error",
    [
        NoPermissionError("this user has no permissions to run the 'xinfo|consumers' command"),
        ResponseError("WRONGTYPE Operation against a key holding the wrong kind of value"),
    ],
)
async def test_other_errors_propagate(error: ResponseError) -> None:
    """PAY-608, R12: a missing +xinfo grant (NoPermissionError is a ResponseError) or any other
    Redis error is raised, so the runner logs maintenance.task.failed instead of pruning
    nothing forever."""
    redis = ConsumersRedis(xinfo_error=error)
    with pytest.raises(type(error)):
        await prune_consumers(cast(Redis, redis), make_settings())
    assert redis.deleted == []


async def test_dropped_pending_is_a_warning(caplog_json: JsonLogCapture) -> None:
    """PAY-608: if DELCONSUMER reports dropped PEL entries (not expected: XAUTOCLAIM never moves
    entries to an idle consumer), the log is a warning so the loss is visible."""
    redis = ConsumersRedis([_c("ghost", 0, 2 * HOUR_MS)], dropped=1)
    task = prune_task(make_settings(), cast(Redis, redis))
    assert task.name == "prune_consumers"
    assert await task.fn(cast(AsyncSession, None)) == 1
    [event] = caplog_json.events("queue.consumer.pruned")
    assert (event["level"], event["dropped_pending"]) == ("warning", 1)


def test_runner_runs_both_tasks_with_redis() -> None:
    """PAY-608: with the command client the runner has the purge and the prune task."""
    s = make_settings()
    engine = cast(AsyncEngine, object())
    sessions = cast(async_sessionmaker[AsyncSession], object())
    with_redis = build_runner(s, engine, sessions, redis=cast(Redis, ConsumersRedis()))
    without = build_runner(s, engine, sessions)
    assert [t.name for t in with_redis._tasks] == ["purge_processed_events", "prune_consumers"]
    assert [t.name for t in without._tasks] == ["purge_processed_events"]
