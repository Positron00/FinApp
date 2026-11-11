"""Two worker instances on one consumer group (design §3.1, §3.3.5, §6; PAY-403, PAY-NFR-02,
ADR 0003 a)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from payout_worker.classify import BankCallResult
from pycommon.testing import JsonLogCapture

from tests.helpers import ACCOUNT
from tests.integration.conftest import DLQ, GROUP, STREAM, Stack, runtime, wait_for


async def _acked(stack: Stack) -> None:
    async def none_pending() -> bool:
        return await stack.pending() == 0

    await wait_for(none_pending)


async def test_two_workers_share_a_run_one_transfer_each(stack: Stack) -> None:
    """PAY-403, PAY-NFR-02: two workers (2 slots each) consume one group; every payout is paid
    with exactly one bank transfer per attempt; every disbursement took one delivery (no message
    was held past PAYOUT_CLAIM_MIN_IDLE_MS and reclaimed); both consumers read; nothing pending."""
    first = await stack.worker(payout_consumer_name="w-a", payout_concurrency=2)
    second = await stack.worker(payout_consumer_name="w-b", payout_concurrency=2)
    accounts = [f"0001234567{i:02d}" for i in range(10)]
    run = await stack.approved_run(accounts)

    async def all_paid() -> bool:
        return all(p["status"] == "paid" for p in await stack.payouts(run["id"]))

    await wait_for(all_paid, within=30)
    await _acked(stack)
    assert await stack.transfers() == 10
    assert (
        await stack.count(
            "SELECT count(*) FROM (SELECT idempotency_key FROM bank_stub.transfers "
            "GROUP BY idempotency_key HAVING count(*) > 1) d"
        )
        == 0
    )
    rows = await stack.db.fetch("SELECT status, outcome, deliveries FROM payouts.disbursements")
    assert len(rows) == 10
    assert {(r["status"], r["outcome"], r["deliveries"]) for r in rows} == {("done", "settled", 1)}
    assert await stack.count("SELECT count(*) FROM payouts.processed_events") == 10
    consumers: Any = await stack.redis.xinfo_consumers(STREAM, GROUP)
    assert {c["name"].decode() for c in consumers} >= {"w-a", "w-b"}
    assert runtime(first).consumer.is_alive()
    assert runtime(second).consumer.is_alive()


async def test_reclaimed_while_paused_after_the_bank_call(
    stack: Stack, caplog_json: JsonLogCapture
) -> None:
    """PAY-403, PAY-NFR-02: the first worker is paused right after its bank call for longer than
    the claim idle; the second XAUTOCLAIMs the entry (delivery 2), POSTs with the same key (the
    bank replays the original), reports settled and ACKs. Released, the first reports too and
    gets 200 with the stored outcome. One bank row, one applied result, the payout paid."""
    first = await stack.worker(payout_consumer_name="paused")
    gate = asyncio.Event()
    after_bank = asyncio.Event()
    bank = runtime(first).bank
    original: Callable[..., Awaitable[BankCallResult]] = bank.create_transfer

    async def pause_after(*args: Any, **kwargs: Any) -> BankCallResult:
        result = await original(*args, **kwargs)
        after_bank.set()
        await gate.wait()
        return result

    bank.create_transfer = pause_after  # type: ignore[method-assign]
    run = await stack.approved_run([ACCOUNT])
    await asyncio.wait_for(after_bank.wait(), 20)
    assert await stack.transfers() == 1

    await stack.worker(payout_consumer_name="claimer")

    async def paid() -> bool:
        return [p["status"] for p in await stack.payouts(run["id"])] == ["paid"]

    await wait_for(paid)
    second_result = await stack.db.fetchrow("SELECT status, deliveries FROM payouts.disbursements")
    assert second_result["status"] == "done"
    assert second_result["deliveries"] == 2
    await _acked(stack)
    posts_before = await stack.count(
        "SELECT count(*) FROM payroll.idempotency_keys WHERE key LIKE '%:settled'"
    )

    gate.set()

    async def first_reported() -> bool:
        return (
            len(
                [
                    e
                    for e in caplog_json.events("queue.message.processed")
                    if e["outcome"] == "settled"
                ]
            )
            == 2
        )

    await wait_for(first_reported)
    assert await stack.transfers() == 1
    applied = await stack.count(
        "SELECT count(*) FROM payroll.audit_events WHERE event = 'payout.result_recorded' "
        "AND (details->>'applied')::boolean"
    )
    assert applied == 1
    # The same event_id → the same result key <event_id>:settled: payroll replays its answer.
    assert (
        await stack.count(
            "SELECT count(*) FROM payroll.idempotency_keys WHERE key LIKE '%:settled'"
        )
        == posts_before
    )
    assert await stack.count("SELECT count(*) FROM payouts.processed_events") == 1
    assert await stack.redis.exists(DLQ) == 0
    await _acked(stack)
