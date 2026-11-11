"""The delivery budget (design §3.2, §3.3; PAY-404, PAY-408, PAY-NFR-11, C21)."""

from __future__ import annotations

import json

import httpx
from pycommon.testing import OtelMemory

from tests.helpers import ACCOUNT
from tests.integration.conftest import DLQ, Stack, wait_for


def _down(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("unreachable")


async def test_bank_unreachable_becomes_indeterminate(stack: Stack) -> None:
    """PAY-404, PAY-408: the bank unreachable on every delivery → at PAYOUT_MAX_DELIVERIES
    indeterminate is posted, the entry ACKed, no DLQ entry; the payout stays requested."""
    await stack.worker(bank_transport=httpx.MockTransport(_down), bank_total_deadline_s=0.5)
    run = await stack.approved_run([ACCOUNT])

    async def indeterminate() -> bool:
        return (
            await stack.count(
                "SELECT count(*) FROM payroll.payout_attempts WHERE status = 'indeterminate'"
            )
            == 1
        )

    await wait_for(indeterminate, within=30)

    # The report (step 7) commits before ``finish`` (step 8.1) marks the row done, and the XACK
    # follows ``finish``: wait for the ACK before reading the worker's row.
    async def acked() -> bool:
        return await stack.pending() == 0

    await wait_for(acked)
    [payout] = await stack.payouts(run["id"])
    assert payout["status"] == "requested"
    row = await stack.db.fetchrow("SELECT status, outcome, deliveries FROM payouts.disbursements")
    assert (row["status"], row["outcome"], row["deliveries"]) == ("done", "indeterminate", 3)
    assert await stack.redis.exists(DLQ) == 0
    assert await stack.transfers() == 0


async def test_payroll_unreachable_dead_letters_at_budget(
    otel_memory: OtelMemory, stack: Stack
) -> None:
    """PAY-408, C21: payroll unreachable → pending until delivery 3, then one DLQ entry with the
    eight fields, the entry ACKed, queue_messages_total{dlq} 0 → 1."""
    await stack.worker(payroll_transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    await stack.approved_run([ACCOUNT])

    async def dead_lettered() -> bool:
        return bool(await stack.redis.xlen(DLQ))

    await wait_for(dead_lettered, within=30)
    [decoded] = await stack.dlq()
    assert set(decoded) == {
        "envelope",
        "source_id",
        "error_kind",
        "error_code",
        "error",
        "delivery_count",
        "consumer",
        "failed_at",
    }
    assert (decoded["error_kind"], decoded["error_code"]) in {
        ("payroll", "payroll_unavailable"),
        ("payroll", "breaker_open"),
    }
    assert decoded["delivery_count"] == "3"
    [(source_id, original)] = await stack.entries()
    assert decoded["source_id"] == source_id.decode()
    assert decoded["envelope"] == original[b"envelope"].decode()
    assert json.loads(decoded["envelope"])["data"]["payout_id"]

    async def acked() -> bool:
        return await stack.pending() == 0

    await wait_for(acked)
    points = {a["outcome"]: v for a, v in otel_memory.metric_points("queue_messages")}
    assert points["dlq"] == 1
    assert points["retry"] == 2
    assert await stack.count("SELECT count(*) FROM payouts.processed_events") == 0
