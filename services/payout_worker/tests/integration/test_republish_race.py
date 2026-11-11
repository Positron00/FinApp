"""A re-published event racing a live consumer (design §3.1 steps 1, 3, 6, 7; PAY-405, PAY-407,
PAY-NFR-02, ADR 0003 a)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from payout_worker.classify import BankCallResult
from payout_worker.models import Instruction
from pycommon.db import new_id
from pycommon.testing import JsonLogCapture

from tests.helpers import ACCOUNT
from tests.integration.conftest import DLQ, STREAM, Stack, runtime, wait_for


async def test_republished_event_racing_a_live_consumer(
    stack: Stack, caplog_json: JsonLogCapture
) -> None:
    """PAY-405, PAY-407, PAY-NFR-02: the original event A is held by a live consumer right after
    its instruction check; payroll's re-publish B (fresh event_id, same attempt, republish_seq
    + 1) is consumed meanwhile and settles the attempt. Both pass the instruction check (the
    attempt is still requested for A), both POST with the same bank key, the bank keeps one
    transfer, A's result gets 200 with the stored outcome, and only one result is applied."""
    live = await stack.worker(payout_consumer_name="live")
    other = await stack.worker(payout_consumer_name="other")
    keys: list[str] = []
    for app in (live, other):
        bank = runtime(app).bank
        create: Callable[..., Awaitable[BankCallResult]] = bank.create_transfer

        async def record(key: str, body: Any, _create: Any = create) -> BankCallResult:
            keys.append(key)
            result: BankCallResult = await _create(key, body)
            return result

        bank.create_transfer = record  # type: ignore[method-assign]
    gate = asyncio.Event()
    held = asyncio.Event()
    calls: list[str | None] = []
    payroll = runtime(live).payroll
    original: Callable[..., Awaitable[Instruction]] = payroll.get_instruction

    async def hold_first(*args: Any, **kwargs: Any) -> Instruction:
        ins = await original(*args, **kwargs)
        calls.append(kwargs.get("request_id"))
        if len(calls) == 1:
            held.set()
            await gate.wait()
        return ins

    payroll.get_instruction = hold_first  # type: ignore[method-assign]
    run = await stack.approved_run([ACCOUNT])
    await asyncio.wait_for(held.wait(), 20)
    [(_, fields)] = await stack.entries()
    env = json.loads(fields[b"envelope"])
    assert calls == [env["event_id"]]
    republished = {**env, "event_id": str(new_id())}
    republished["data"] = {**env["data"], "republish_seq": env["data"]["republish_seq"] + 1}
    await stack.redis.xadd(
        STREAM,
        {
            "event_id": republished["event_id"],
            "type": "payout.requested",
            "envelope": json.dumps(republished),
        },
    )

    async def paid() -> bool:
        return [p["status"] for p in await stack.payouts(run["id"])] == ["paid"]

    await wait_for(paid)
    assert await stack.transfers() == 1
    gate.set()

    async def both_processed() -> bool:
        return await stack.count("SELECT count(*) FROM payouts.processed_events") == 2

    await wait_for(both_processed)
    assert await stack.transfers() == 1
    assert keys == [f"{env['data']['payout_id']}:1"] * 2  # both POSTed, with the same key
    outcomes = await stack.db.fetch("SELECT event_id, outcome FROM payouts.processed_events")
    assert {str(r["event_id"]) for r in outcomes} == {env["event_id"], republished["event_id"]}
    assert {r["outcome"] for r in outcomes} == {"settled"}
    applied = await stack.count(
        "SELECT count(*) FROM payroll.audit_events WHERE event = 'payout.result_recorded' "
        "AND (details->>'applied')::boolean"
    )
    assert applied == 1
    row = await stack.db.fetchrow("SELECT status, outcome FROM payouts.disbursements")
    assert (row["status"], row["outcome"]) == ("done", "settled")
    assert not caplog_json.events("payout.attempt_not_current")
    assert await stack.redis.exists(DLQ) == 0

    async def acked() -> bool:
        return await stack.pending() == 0

    await wait_for(acked)
