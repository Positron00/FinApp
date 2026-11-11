"""Poison messages go to the DLQ at once (design §3.1 steps 0, 3, 4; PAY-408, PAY-409, R5)."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import pytest

from tests.helpers import ACCOUNT
from tests.integration.conftest import DLQ, STREAM, Stack, wait_for


async def _replace_event(stack: Stack, change: Any) -> dict[str, Any]:
    """Take the relay's real entry off the stream and publish a modified copy instead."""
    [(entry_id, fields)] = await stack.published(1)
    await stack.redis.xdel(STREAM, entry_id)
    env: dict[str, Any] = json.loads(fields[b"envelope"])
    change(env)
    await stack.redis.xadd(
        STREAM,
        {
            "event_id": env.get("event_id", ""),
            "type": "payout.requested",
            "envelope": json.dumps(env),
        },
    )
    return env


async def _dlq_entry(stack: Stack) -> dict[str, str]:
    async def present() -> bool:
        return bool(await stack.redis.xlen(DLQ))

    await wait_for(present)
    [entry] = await stack.dlq()
    return entry


@pytest.mark.parametrize(
    ("change", "code", "reported"),
    [
        (lambda e: e["data"].update(currency="EUR"), "schema_invalid", True),
        (
            lambda e: e["data"].update(amount_cents=e["data"]["amount_cents"] + 1),
            "amount_mismatch",
            True,
        ),
        (
            lambda e: e["data"].update(payout_id="0193f1d2-6c4a-7000-8000-00000000dead"),
            "payout_not_found",
            False,
        ),
        (lambda e: e.update(traceparent="broken"), "schema_invalid", True),
    ],
)
async def test_poison_dead_lettered_on_first_delivery(
    stack: Stack, change: Any, code: str, reported: bool
) -> None:
    """PAY-408, PAY-409, R5: schema-invalid, amount mismatch and payout_not_found → DLQ on
    delivery 1 and ACK; poison reported (attempt needs_operator) when the payout is readable;
    no processed_event; no bank call."""
    run = await stack.approved_run([ACCOUNT])
    await _replace_event(stack, change)
    await stack.worker()
    entry = await _dlq_entry(stack)
    assert (entry["error_kind"], entry["error_code"], entry["delivery_count"]) == (
        "invalid",
        code,
        "1",
    )

    async def acked() -> bool:
        return await stack.pending() == 0

    await wait_for(acked)
    status = await stack.db.fetchval(
        "SELECT a.status FROM payroll.payout_attempts a "
        "JOIN payroll.payouts p ON p.id = a.payout_id WHERE p.pay_run_id = $1",
        UUID(run["id"]),
    )
    assert status == ("needs_operator" if reported else "requested")
    assert await stack.count("SELECT count(*) FROM payouts.processed_events") == 0
    assert await stack.transfers() == 0


async def test_unreadable_entry_dlq_only(stack: Stack) -> None:
    """R5: an entry with no readable IDs → DLQ only (envelope "" when the field is missing)."""
    await stack.redis.xadd(STREAM, {"event_id": "nope", "type": "payout.requested"})
    await stack.worker()
    entry = await _dlq_entry(stack)
    assert (entry["envelope"], entry["error_code"]) == ("", "schema_invalid")
