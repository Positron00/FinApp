"""An old attempt redelivered (design §6; PAY-403, PAY-411, ADR 0003 b)."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from payroll.crypto import snapshot_aad
from payroll.events.envelope import build_payout_requested
from payroll.services.runtime import Runtime as PayrollRuntime
from pycommon.db import new_id
from pycommon.testing import JsonLogCapture

from tests.helpers import ACCOUNT, COMPANY_ID
from tests.integration.conftest import PAYROLL_ADMIN, STREAM, Stack, wait_for

CLOSED = "000111110000"


async def _publish(stack: Stack, payout: dict[str, Any], attempt: int) -> None:
    env = build_payout_requested(
        company_id=COMPANY_ID,
        payout_id=UUID(payout["id"]),
        pay_run_id=UUID(payout["pay_run_id"]),
        attempt=attempt,
        amount_cents=payout["amount_cents"],
        republish_seq=1,
    )
    await stack.redis.xadd(
        STREAM,
        {"event_id": str(env.event_id), "type": "payout.requested", "envelope": env.to_json()},
    )


async def _retry_as_attempt_2(stack: Stack, payout: dict[str, Any]) -> None:
    """What payroll's retry (F4a) will do: attempt 2 current, with a fresh bank snapshot."""
    rt: PayrollRuntime = stack.payroll_app.state.payroll
    pid = UUID(payout["id"])
    snapshot = json.dumps({"routing": "021000021", "account": ACCOUNT}, separators=(",", ":"))
    await stack.db.execute(
        "UPDATE payroll.payouts SET status = 'requested', final_at = NULL, current_attempt = 2 "
        "WHERE id = $1",
        pid,
    )
    await stack.db.execute(
        "UPDATE payroll.pay_runs SET status = 'processing' WHERE id = $1",
        UUID(payout["pay_run_id"]),
    )
    await stack.db.execute(
        "INSERT INTO payroll.payout_attempts (id, company_id, payout_id, attempt, status, "
        "bank_snapshot_enc, account_last4, requested_by, requested_at, last_published_at) "
        "VALUES ($1, $2, $3, 2, 'requested', $4, '6789', $5, now(), now())",
        new_id(),
        COMPANY_ID,
        pid,
        rt.keyring.encrypt(snapshot, aad=snapshot_aad(pid, 2)),
        PAYROLL_ADMIN,
    )


async def test_old_attempt_redelivered(stack: Stack, caplog_json: JsonLogCapture) -> None:
    """PAY-403, PAY-411: attempt 1 rejected and recorded, attempt 2 settled; attempt 1
    redelivered (fresh event_id) → 409 attempt_not_current, no bank call, ACK."""
    await stack.worker()
    run = await stack.approved_run([CLOSED])

    async def failed() -> bool:
        return [p["status"] for p in await stack.payouts(run["id"])] == ["failed"]

    await wait_for(failed)
    [payout] = await stack.payouts(run["id"])
    await _retry_as_attempt_2(stack, payout)
    await _publish(stack, payout, 2)

    async def paid() -> bool:
        return [p["status"] for p in await stack.payouts(run["id"])] == ["paid"]

    await wait_for(paid)
    assert await stack.transfers() == 2  # one per attempt key

    await _publish(stack, payout, 1)

    async def not_current() -> bool:
        return (
            await stack.count(
                "SELECT count(*) FROM payouts.processed_events "
                "WHERE outcome = 'attempt_not_current' AND attempt = 1"
            )
            == 1
        )

    await wait_for(not_current)
    assert await stack.transfers() == 2  # no bank call for the old attempt
    mine = [
        e["source"]
        for e in caplog_json.events("payout.attempt_not_current")
        if e["logger"].startswith("payout_worker")
    ]
    assert mine == ["instruction"]
    keys = [
        r["idempotency_key"]
        for r in await stack.db.fetch("SELECT idempotency_key FROM bank_stub.transfers")
    ]
    assert sorted(k.rsplit(":", 1)[1] for k in keys) == ["1", "2"]

    async def acked() -> bool:
        return await stack.pending() == 0

    await wait_for(acked)
