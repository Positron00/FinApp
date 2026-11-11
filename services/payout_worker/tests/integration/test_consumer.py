"""End to end: payroll outbox → stream → worker → bank-stub → result (design §6; PAY-403,
PAY-404, PAY-405, PAY-412, PAY-NFR-02)."""

from __future__ import annotations

import json
from typing import Any

from pycommon.db import new_id
from pycommon.testing import JsonLogCapture

from tests.helpers import ACCOUNT, API_KEY, ROUTING
from tests.integration.conftest import STREAM, Stack, runtime, wait_for


async def _final(stack: Stack, run_id: str) -> list[dict[str, Any]]:
    async def done() -> bool:
        return all(p["status"] != "requested" for p in await stack.payouts(run_id))

    await wait_for(done)
    return await stack.payouts(run_id)


async def test_outbox_to_result_end_to_end(stack: Stack, caplog_json: JsonLogCapture) -> None:
    """PAY-403, PAY-404, PAY-405: an approved run's payouts flow through the real relay, the
    worker, bank-stub and payroll's result endpoint; one transfer each; done rows, processed
    events and no pending entries; account …0000 is a definite rejection."""
    await stack.worker()
    run = await stack.approved_run([ACCOUNT, "000111110000"])
    payouts = await _final(stack, run["id"])
    statuses = sorted(p["status"] for p in payouts)
    assert statuses == ["failed", "paid"]
    assert (await stack.run(run["id"]))["status"] == "partially_failed"
    assert await stack.transfers() == 2
    rows = await stack.db.fetch(
        "SELECT status, outcome, deliveries, bank_reference FROM payouts.disbursements"
    )
    assert sorted((r["status"], r["outcome"]) for r in rows) == [
        ("done", "rejected"),
        ("done", "settled"),
    ]
    assert all(r["deliveries"] == 1 for r in rows)
    assert await stack.count("SELECT count(*) FROM payouts.processed_events") == 2

    async def acked() -> bool:
        return await stack.pending() == 0

    await wait_for(acked)
    processed = caplog_json.events("queue.message.processed")
    assert sorted(e["outcome"] for e in processed) == ["rejected", "settled"]
    # PAY-NFR-07: no bank details or API key in any log line of the run.
    for secret in (ROUTING, ACCOUNT, API_KEY):
        assert secret not in caplog_json.text


async def test_duplicate_delivery_is_a_no_op(stack: Stack, caplog_json: JsonLogCapture) -> None:
    """PAY-403: the same event_id delivered again → skipped at processed_events: no second bank
    call, no second result, ACKed."""
    await stack.worker()
    run = await stack.approved_run([ACCOUNT])
    await _final(stack, run["id"])
    [(_, fields)] = await stack.entries()
    results_before = await stack.count(
        "SELECT count(*) FROM payroll.audit_events WHERE event = 'payout.result_recorded'"
    )
    idem_before = await stack.count("SELECT count(*) FROM payroll.idempotency_keys")
    await stack.redis.xadd(STREAM, fields)  # type: ignore[arg-type]

    async def skipped() -> bool:
        return any(
            e["outcome"] == "duplicate" for e in caplog_json.events("queue.message.processed")
        )

    await wait_for(skipped)
    assert await stack.transfers() == 1
    assert (
        await stack.count(
            "SELECT count(*) FROM payroll.audit_events WHERE event = 'payout.result_recorded'"
        )
        == results_before
    )
    assert await stack.count("SELECT count(*) FROM payroll.idempotency_keys") == idem_before
    assert await stack.count("SELECT count(*) FROM payouts.processed_events") == 1

    async def acked() -> bool:
        return await stack.pending() == 0

    await wait_for(acked)


async def test_republished_event_same_attempt(stack: Stack, caplog_json: JsonLogCapture) -> None:
    """PAY-405, PAY-407: a fresh event_id for an attempt already settled passes the processed
    check, gets 409 attempt_not_current on the instruction (the attempt is final), and makes no
    bank call."""
    await stack.worker()
    run = await stack.approved_run([ACCOUNT])
    await _final(stack, run["id"])
    [(_, fields)] = await stack.entries()
    env = json.loads(fields[b"envelope"])
    env["event_id"] = str(new_id())
    await stack.redis.xadd(
        STREAM,
        {"event_id": env["event_id"], "type": "payout.requested", "envelope": json.dumps(env)},
    )

    async def handled() -> bool:
        return (
            await stack.count(
                "SELECT count(*) FROM payouts.processed_events "
                "WHERE outcome = 'attempt_not_current'"
            )
            == 1
        )

    await wait_for(handled)
    assert await stack.transfers() == 1
    assert runtime(stack.workers[0]).consumer.is_alive()
