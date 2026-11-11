"""payroll down during processing (HLD §4; plan §12.1; PAY-407, PAY-408, PAY-NFR-03).

Toxiproxy's ``payroll`` proxy carries the worker's (and the web's) calls to payroll; disabling
it takes payroll away from the worker while the test still drives payroll on its own published
port. The worker can't fetch instructions, so each message stays pending, is reclaimed every
60 s and is dead-lettered at delivery 5 (``payroll_unavailable``). After recovery, payroll's
re-publish (360 s after the last publish) redrives every attempt and the run completes. About
8 minutes: nightly only (C53).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from _stack import (
    DLQ,
    DLQ_WAIT_S,
    METRIC_WAIT_S,
    REPUBLISH_WAIT_S,
    Payroll,
    Stack,
    Toxiproxy,
    assert_paid_once,
    log_events,
    payout_ids,
    settled_count,
    wait_until,
)

DEAD_LETTERED = 'queue_messages_total{service_name="payout-worker", outcome="dlq"}'


@pytest.mark.timeout(DLQ_WAIT_S + REPUBLISH_WAIT_S + METRIC_WAIT_S + 120)
def test_payroll_down_dead_letters_then_republish_redrives(
    stack: Stack, toxiproxy: Toxiproxy, payroll: Payroll
) -> None:
    """PAY-407, PAY-408, PAY-NFR-03, HLD §4 "payroll down during processing": pending, then DLQ
    after the delivery budget; the re-publish after recovery redrives and the run is paid once."""
    t0 = datetime.now(UTC)
    run = payroll.calculated_run()
    dlq_before = stack.redis_int("XLEN", DLQ)
    dead_lettered = stack.prom(DEAD_LETTERED)
    toxiproxy.set_enabled("payroll", False)
    payroll.approve(run["id"])
    ids = payout_ids(stack, run["id"])

    wait_until(
        "every message dead-lettered",
        lambda: stack.redis_int("XLEN", DLQ) - dlq_before >= len(ids),
        DLQ_WAIT_S,
        interval=5,
    )
    lines = stack.redis("XREVRANGE", DLQ, "+", "-", "COUNT", str(len(ids))).splitlines()
    fields = [(lines[i], lines[i + 1]) for i in range(len(lines) - 1)]
    assert [v for k, v in fields if k == "error_kind"] == ["payroll"] * len(ids)
    # worker LLD §3.3.4: connection errors, then the open breaker on the payroll client
    assert {v for k, v in fields if k == "error_code"} <= {"payroll_unavailable", "breaker_open"}
    assert settled_count(stack, ids) == 0  # nothing reached the bank without an instruction
    assert payroll.run(run["id"])["status"] == "processing"
    worker_logs = stack.logs_since("payout-worker", t0)
    assert log_events(worker_logs, "queue.message.failed") >= 5 * len(ids)
    # HLD §4: queue_messages_total{outcome="dlq"} (what PayoutDLQ pages on)
    stack.wait_increase("dead-lettered messages", DEAD_LETTERED, dead_lettered, len(ids))

    toxiproxy.set_enabled("payroll", True)
    payroll.wait_final(run["id"], REPUBLISH_WAIT_S)
    assert_paid_once(stack, payroll, run["id"])
    assert log_events(stack.logs_since("payroll", t0), "payout.republished") >= len(ids)
