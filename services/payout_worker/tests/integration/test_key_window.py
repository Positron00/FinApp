"""The key safe window: GET only (design §3.1 step 5; PAY-410)."""

from __future__ import annotations

from uuid import UUID

from tests.helpers import ACCOUNT, API_KEY
from tests.integration.conftest import Stack, wait_for


async def _age_attempts(stack: Stack) -> None:
    await stack.db.execute(
        "UPDATE payroll.payout_attempts SET requested_at = now() - interval '25 hours'"
    )


async def test_missing_transfer_is_poison_without_post(stack: Stack) -> None:
    """PAY-410: a 25-hour-old attempt with no bank transfer → poison key_window_expired, the
    attempt needs_operator, and the bank gets no POST."""
    await stack.approved_run([ACCOUNT])
    await stack.published(1)
    await _age_attempts(stack)
    await stack.worker()

    # Wait on the worker's last write, not payroll's: step 7 (the poison report, which makes the
    # attempt needs_operator) commits before step 8.1 (``finish`` → processed_events), so
    # needs_operator alone can be observed before the processed event exists.
    async def finished() -> bool:
        return (
            await stack.count(
                "SELECT count(*) FROM payouts.processed_events WHERE outcome = 'poison'"
            )
            == 1
        )

    await wait_for(finished)
    assert (
        await stack.count(
            "SELECT count(*) FROM payroll.payout_attempts WHERE status = 'needs_operator'"
        )
        == 1
    )
    assert await stack.transfers() == 0


async def test_existing_transfer_is_reported(stack: Stack) -> None:
    """PAY-410: with a transfer present at the bank, its status is recorded; no new row."""
    run = await stack.approved_run([ACCOUNT])
    await stack.published(1)
    [payout] = await stack.payouts(run["id"])
    key = f"{UUID(payout['id'])}:1"
    r = await stack.bank.post(
        "/v1/transfers",
        headers={"Idempotency-Key": key, "X-Api-Key": API_KEY},
        json={
            "amount_cents": payout["amount_cents"],
            "currency": "USD",
            "routing": "021000021",
            "account": ACCOUNT,
        },
    )
    assert r.status_code == 201
    await _age_attempts(stack)
    await stack.worker()

    async def paid() -> bool:
        return [p["status"] for p in await stack.payouts(run["id"])] == ["paid"]

    await wait_for(paid)
    assert await stack.transfers() == 1
