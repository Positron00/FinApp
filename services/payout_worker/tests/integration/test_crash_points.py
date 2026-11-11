"""Crash injection and redelivery (design §3.1 step 8, §6; PAY-403, PAY-NFR-02, ADR 0003 a)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from tests.helpers import ACCOUNT
from tests.integration.conftest import Stack, lifespan, runtime, wait_for


def _crash_once(target: Any, name: str) -> list[int]:
    """Wrap ``target.name``: the first call completes, then the handler task is cancelled."""
    original: Callable[..., Awaitable[Any]] = getattr(target, name)
    calls: list[int] = []

    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        result = await original(*args, **kwargs)
        calls.append(1)
        if len(calls) == 1:
            raise asyncio.CancelledError  # the process "dies" right after this step
        return result

    setattr(target, name, wrapper)
    return calls


@pytest.mark.parametrize("point", ["upsert", "bank", "result", "finish"])
async def test_crash_then_redelivery_gives_one_transfer(stack: Stack, point: str) -> None:
    """PAY-403, PAY-NFR-02: a crash after step 2, after the bank call, after the result or
    after the DB transaction, then a new consumer reclaiming the message → exactly one bank
    transfer, the payout paid, one applied result, and nothing left pending."""
    first = stack.build_worker(payout_consumer_name="crashing")
    async with lifespan(first):
        rt = runtime(first)
        repo = rt.handler._repo
        target, name = {
            "upsert": (repo, "upsert_submitting"),
            "bank": (rt.bank, "create_transfer"),
            "result": (rt.payroll, "post_result"),
            "finish": (repo, "finish"),
        }[point]
        calls = _crash_once(target, name)
        run = await stack.approved_run([ACCOUNT])

        async def crashed() -> bool:
            return bool(calls)

        await wait_for(crashed)
    assert await stack.pending() == 1  # the "dead" consumer never ACKed

    await stack.worker(payout_consumer_name="survivor")

    async def paid() -> bool:
        return [p["status"] for p in await stack.payouts(run["id"])] == ["paid"]

    await wait_for(paid)

    async def acked() -> bool:
        return await stack.pending() == 0

    await wait_for(acked)
    assert await stack.transfers() == 1
    applied = await stack.count(
        "SELECT count(*) FROM payroll.audit_events WHERE event = 'payout.result_recorded' "
        "AND (details->>'applied')::boolean"
    )
    assert applied == 1
    assert await stack.count("SELECT count(*) FROM payouts.processed_events") == 1
