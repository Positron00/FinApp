"""PAY-NFR-15: restarting Redis mid-run loses no payout (requirements §NFR; HLD §4; ADR 0007).

Redis runs with AOF (deploy/compose/redis/redis.conf), so the payout stream and the worker's
consumer group survive a restart. With the worker paused, payroll publishes the run; Redis is
restarted while the messages wait in the stream; the worker then reads them and the run is paid
without any re-publish (every attempt's publish_count stays 1), well inside the 360 s re-publish
window. Nightly.

HLD §4 has no row for a restart (the "Redis down" row covers the outage). The test checks the
restart's own evidence: Redis logs that it loaded the append-only file, and redis_exporter's
``redis_start_time_seconds`` moves past the restart.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from _stack import (
    DLQ,
    METRIC_WAIT_S,
    REPUBLISH_AFTER_S,
    STREAM,
    Containers,
    Payroll,
    Stack,
    assert_paid_once,
    payout_ids,
    publish_counts,
    unsent_outbox,
    wait_until,
)


@pytest.mark.timeout(REPUBLISH_AFTER_S + METRIC_WAIT_S)
def test_redis_restart_mid_run_loses_no_payout(
    stack: Stack, containers: Containers, payroll: Payroll
) -> None:
    """PAY-NFR-15: the queued payouts survive a Redis restart and the run completes; no message
    is dead-lettered and none needed a re-publish."""
    run = payroll.calculated_run()
    dlq_before = stack.redis_int("XLEN", DLQ)
    containers.pause("payout-worker")
    payroll.approve(run["id"])
    ids = payout_ids(stack, run["id"])
    wait_until("the relay published the run", lambda: unsent_outbox(stack, run["id"]) == 0, 30)
    queued = stack.redis_int("XLEN", STREAM)
    assert queued >= len(ids)

    restarted = datetime.now(UTC)
    containers.restart("redis")
    assert stack.redis_int("XLEN", STREAM) == queued  # AOF: the stream came back whole
    assert "DB loaded from append only file" in stack.logs_since("redis", restarted)
    assert "payout-worker" in stack.redis("XINFO", "GROUPS", STREAM)
    containers.unpause("payout-worker")

    payroll.wait_final(run["id"], 120)
    assert_paid_once(stack, payroll, run["id"])
    assert publish_counts(stack, run["id"]) == [1] * len(ids)
    assert stack.redis_int("XLEN", DLQ) == dlq_before
    stack.wait_prom(
        "redis_exporter sees the restart",
        "max(redis_start_time_seconds)",
        lambda v: v >= restarted.timestamp() - 1,
    )
