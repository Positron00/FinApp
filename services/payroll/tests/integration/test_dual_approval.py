"""Dual approval against a real database (design §3.4, §4.4; PAY-401, PAY-500, ADR 0005, C73).

The cast: Alice is an owner with ``payroll_admin`` and ``approver``; Bob is a preparer
(``payroll_admin``); Grace is a second approver. Before C73, a bank edit counted only when made
after the run's ``created_at``, so Alice could redirect a salary on Monday, let Bob create and
calculate the run on Tuesday, and approve it alone.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import pytest
from pycommon.testing import PgBootstrapped

from tests.helpers import employee_body
from tests.integration.conftest import APPROVER, PAYROLL_ADMIN, Api, admin_connect

pytestmark = pytest.mark.integration

ALICE = "0193a000-0000-7000-8000-0000000000a5"
ALICE_ACCOUNT = "999988887777"
_REJECTED = {"attempt": 1, "outcome": "rejected", "reason": "account_closed"}


def _alice(api: Api) -> dict[str, str]:
    return api.headers(ALICE, ("payroll_admin", "approver"), name="Alice Owner")


def _err(r: Any) -> tuple[int, str, Any]:
    e = r.json()["error"]
    return r.status_code, e["code"], e.get("details")


async def _employee(api: Api, i: int, headers: dict[str, str] | None = None, **kw: Any) -> str:
    body = employee_body(name=f"Emp {i:02d}", email=f"e{i}@example.com", **kw)
    r = await api.client.post("/v1/employees", json=body, headers=headers or api.payroll_admin())
    assert r.status_code == 201, r.text
    emp_id: str = r.json()["id"]
    return emp_id


async def _redirect_bank(api: Api, emp_id: str, headers: dict[str, str]) -> None:
    r = await api.client.patch(
        f"/v1/employees/{emp_id}", json={"bank_account": ALICE_ACCOUNT}, headers=headers
    )
    assert r.status_code == 200, r.text


async def _run(api: Api, period_start: str, headers: dict[str, str] | None = None) -> str:
    """Create (as ``headers``, default Bob) and calculate (as Bob) a monthly run."""
    r = await api.client.post(
        "/v1/pay-runs",
        json={"schedule": "monthly", "period_start": period_start},
        headers=headers or api.payroll_admin(),
    )
    assert r.status_code == 201, r.text
    run_id: str = r.json()["id"]
    await api.calculate(run_id)
    return run_id


async def _denials(db: Any) -> list[dict[str, Any]]:
    rows = await db.fetch(
        "SELECT actor_id, entity_type, entity_id, details::text AS d FROM payroll.audit_events "
        "WHERE event = 'payrun.approve_denied' ORDER BY id"
    )
    return [{**dict(r), "entity_id": str(r["entity_id"]), "d": json.loads(r["d"])} for r in rows]


async def _set_dual(api: Api, on: bool) -> None:
    settings = {
        "name": "Acme", "tax_rate_bps": 0, "retirement_bps": 0, "timezone": "America/New_York",
        "biweekly_anchor": "2026-01-05", "require_dual_approval": on,
    }  # fmt: skip
    r = await api.client.put("/v1/company/settings", json=settings, headers=api.admin())
    assert r.status_code == 200, r.text


async def test_bank_edit_before_the_run_blocks_its_editor(api: Api, db: Any) -> None:
    """PAY-401 (d), C73, the bypass: Alice redirects an employee's account on Monday, Bob creates
    and calculates the run on Tuesday; Alice's approve → 403 approver_must_differ (bank_editor,
    that employee), nothing is written but a payrun.approve_denied audit row (rule, actor), and
    Grace can still approve."""
    victim = await _employee(api, 0)
    await _employee(api, 1)
    await _redirect_bank(api, victim, _alice(api))
    run_id = await _run(api, "2026-01-01")
    r = await api.approve(run_id, headers=_alice(api))
    assert _err(r) == (
        403, "approver_must_differ", {"rule": "bank_editor", "employee_ids": [victim]},
    )  # fmt: skip
    assert await db.fetchval("SELECT status FROM payroll.pay_runs WHERE id = $1", run_id) == (
        "calculated"
    )
    assert await db.fetchval("SELECT count(*) FROM payroll.payouts") == 0
    assert await db.fetchval("SELECT count(*) FROM payroll.outbox") == 0
    assert await _denials(db) == [
        {
            "actor_id": ALICE, "entity_type": "pay_run", "entity_id": run_id,
            "d": {"rule": "bank_editor", "employee_count": 1},
        }
    ]  # fmt: skip
    assert (await api.approve(run_id)).status_code == 200


async def test_ghost_employee_blocks_its_creator(api: Api, db: Any) -> None:
    """PAY-401 (d), C73: creating an employee sets bank_updated_by, so Alice's ghost employee,
    never paid under four-eyes, blocks Alice from approving the next run Bob prepares."""
    await _employee(api, 0)
    ghost = await _employee(api, 1, headers=_alice(api), bank_account=ALICE_ACCOUNT)
    run_id = await _run(api, "2026-01-01")
    r = await api.approve(run_id, headers=_alice(api))
    assert _err(r) == (
        403, "approver_must_differ", {"rule": "bank_editor", "employee_ids": [ghost]},
    )  # fmt: skip


async def test_edit_after_the_last_four_eyes_payout_still_blocks(api: Api, db: Any) -> None:
    """PAY-401 (d), C73: the window starts at the employee's last payout released by someone
    else, not at the run's creation: after Grace paid January, Alice's edit blocks her from
    approving February."""
    emp = await _employee(api, 0)
    assert (await api.approve(await _run(api, "2026-01-01"))).status_code == 200
    await _redirect_bank(api, emp, _alice(api))
    feb = await _run(api, "2026-02-01")
    r = await api.approve(feb, headers=_alice(api))
    assert _err(r) == (403, "approver_must_differ", {"rule": "bank_editor", "employee_ids": [emp]})


async def test_edit_released_by_a_second_approver_no_longer_blocks(api: Api, db: Any) -> None:
    """PAY-401 (d), C73: once Grace released a payout with Alice's edited details (January), the
    edit is vetted, so Alice may approve February's run that Bob prepared."""
    emp = await _employee(api, 0)
    await _redirect_bank(api, emp, _alice(api))
    jan = await _run(api, "2026-01-01")
    assert (await api.approve(jan, headers=_alice(api))).status_code == 403
    assert (await api.approve(jan)).status_code == 200
    feb = await _run(api, "2026-02-01")
    r = await api.approve(feb, headers=_alice(api))
    assert r.status_code == 200, r.text


async def test_a_payout_without_four_eyes_does_not_vet(api: Api, db: Any) -> None:
    """PAY-401 (d), C73: a payout Alice released herself, with dual approval off, doesn't vet
    her edit: with the setting back on she still can't approve the next run alone."""
    emp = await _employee(api, 0)
    await _redirect_bank(api, emp, _alice(api))
    await _set_dual(api, on=False)
    jan = await _run(api, "2026-01-01")
    assert (await api.approve(jan, headers=_alice(api))).status_code == 200
    await _set_dual(api, on=True)
    feb = await _run(api, "2026-02-01")
    r = await api.approve(feb, headers=_alice(api))
    assert _err(r) == (403, "approver_must_differ", {"rule": "bank_editor", "employee_ids": [emp]})


async def test_creator_cannot_approve(api: Api, db: Any) -> None:
    """PAY-401 (b), C73: Alice creates the run, Bob calculates it; Alice's approve → 403
    creator (audited); Grace's passes."""
    await _employee(api, 0)
    run_id = await _run(api, "2026-01-01", headers=_alice(api))
    r = await api.approve(run_id, headers=_alice(api))
    assert _err(r) == (403, "approver_must_differ", {"rule": "creator", "employee_ids": []})
    [denied] = await _denials(db)
    assert (denied["actor_id"], denied["d"]) == (ALICE, {"rule": "creator", "employee_count": 0})
    assert (await api.approve(run_id)).status_code == 200


async def test_hours_editor_cannot_approve(api: Api, db: Any) -> None:
    """PAY-401 (c), C73: pay_run_hours.entered_by records who entered the hours. Alice enters
    them, Bob creates and calculates; Alice's approve → 403 hours_editor with that employee."""
    hourly = await _employee(
        api, 0, pay_type="hourly", annual_salary_cents=None, hourly_rate_cents=2_500
    )
    r = await api.client.post(
        "/v1/pay-runs",
        json={"schedule": "monthly", "period_start": "2026-01-01"},
        headers=api.payroll_admin(),
    )
    run_id = r.json()["id"]
    body = {"entries": [{"employee_id": hourly, "hours_hundredths": 16_000}]}
    r = await api.client.put(f"/v1/pay-runs/{run_id}/hours", json=body, headers=_alice(api))
    assert r.status_code == 200, r.text
    await api.calculate(run_id)
    r = await api.approve(run_id, headers=_alice(api))
    assert _err(r) == (
        403, "approver_must_differ", {"rule": "hours_editor", "employee_ids": [hourly]},
    )  # fmt: skip
    assert (await api.approve(run_id)).status_code == 200


async def test_legitimate_bank_edit_then_second_admin_approves(api: Api, db: Any) -> None:
    """PAY-401, C73: the normal flow still works. Bob edits a bank account, creates and
    calculates; Grace (or Alice, who touched nothing) approves; no denial is audited."""
    emp = await _employee(api, 0)
    r = await api.client.patch(
        f"/v1/employees/{emp}", json={"bank_account": "123412341234"}, headers=api.payroll_admin()
    )
    assert r.json()["bank_updated_by"] == PAYROLL_ADMIN
    run_id = await _run(api, "2026-01-01")
    r = await api.approve(run_id, headers=_alice(api))
    assert r.status_code == 200, r.text
    assert await _denials(db) == []


async def test_retry_after_a_vetted_edit_is_allowed_to_its_editor(api: Api, db: Any) -> None:
    """PAY-500, C73: retry uses the same window. Alice edited the details before January, Grace
    released them, the bank rejected the payout: Alice may retry (same, vetted details). After
    Alice edits again, her retry → 403 bank_editor and a payout.retry_denied row."""
    emp = await _employee(api, 0)
    await _redirect_bank(api, emp, _alice(api))
    jan = await _run(api, "2026-01-01")
    assert (await api.approve(jan)).status_code == 200
    [p] = await api.payouts(jan)
    rejected = {"attempt": 1, "outcome": "rejected", "reason": "account_closed"}
    assert (await api.result(p["id"], rejected)).json()["outcome"] == "rejected"
    r = await api.retry(jan, p["id"], headers=_alice(api))
    assert r.status_code == 200, r.text
    assert (await api.result(p["id"], {**rejected, "attempt": 2})).json()["outcome"] == "rejected"
    r = await api.client.patch(
        f"/v1/employees/{emp}", json={"bank_account": "111122223333"}, headers=_alice(api)
    )
    assert r.status_code == 200
    r = await api.retry(jan, p["id"], headers=_alice(api))
    assert _err(r) == (403, "approver_must_differ", {"rule": "bank_editor", "employee_ids": [emp]})
    row = await db.fetchrow(
        "SELECT actor_id, entity_id, details::text AS d FROM payroll.audit_events "
        "WHERE event = 'payout.retry_denied'"
    )
    assert (row["actor_id"], str(row["entity_id"])) == (ALICE, p["id"])
    assert json.loads(row["d"]) == {"rule": "bank_editor", "employee_count": 1, "pay_run_id": jan}
    assert (await api.retry(jan, p["id"], headers=api.approver())).status_code == 200


async def test_no_transfer_employee_edit_does_not_block(api: Api, db: Any) -> None:
    """PAY-401 (d), C73: only employees who get a transfer count. Alice's own employee earns
    1 cent a year, so their January payout is no_transfer (no attempt, nothing released, never
    vetted): it doesn't block Alice. Her edit of an employee who is paid still does, and only
    that employee is listed."""
    await _employee(api, 0, headers=_alice(api), annual_salary_cents=1)
    paid = await _employee(api, 1)
    jan = await _run(api, "2026-01-01")
    r = await api.approve(jan, headers=_alice(api))
    assert r.status_code == 200, r.text
    assert r.json()["counts"]["no_transfer"] == 1
    await _redirect_bank(api, paid, _alice(api))
    feb = await _run(api, "2026-02-01")
    r = await api.approve(feb, headers=_alice(api))
    assert _err(r) == (
        403, "approver_must_differ", {"rule": "bank_editor", "employee_ids": [paid]},
    )  # fmt: skip


async def _until_a_patch_waits_on_the_row_lock(db: Any) -> None:
    await _until_waiting_on_a_row_lock(db, "%payroll.employees%FOR UPDATE%", "the PATCH")


async def _until_waiting_on_a_row_lock(db: Any, query_like: str, who: str) -> None:
    for _ in range(1000):
        if await db.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
            "AND query ILIKE $1",
            query_like,
        ):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{who} never waited on a row lock ({query_like})")


async def _cancel(task: asyncio.Task[Any] | None) -> None:
    """Cancel a request task a failed assertion left running, bounded."""
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(task, 5)


async def test_edit_waiting_on_an_approve_lock_is_not_vetted_by_it(
    api: Api, db: Any, migrated: PgBootstrapped
) -> None:
    """PAY-401 (d), C73, the timestamp race: approve #1 holds the employee FOR SHARE; Alice's
    PATCH starts and waits for FOR UPDATE; Grace's approve #2 starts later, takes FOR SHARE too
    (compatible) and snapshots the OLD details; then the PATCH commits. bank_updated_at is
    clock_timestamp() at the UPDATE and requested_at is read after the FOR SHARE lock, so the edit
    is later than Grace's snapshot and stays unvetted: Alice can't approve February alone. (With
    now(), the PATCH's transaction start was earlier than approve #2's, so it counted as vetted.)"""
    emp = await _employee(api, 0)
    jan = await _run(api, "2026-01-01")
    patch: asyncio.Task[Any] | None = None
    holder = await admin_connect(migrated)  # stands in for approve #1
    try:
        tx = holder.transaction()
        await tx.start()
        await holder.execute("SELECT 1 FROM payroll.employees WHERE id = $1 FOR SHARE", emp)
        patch = asyncio.create_task(
            api.client.patch(
                f"/v1/employees/{emp}", json={"bank_account": ALICE_ACCOUNT}, headers=_alice(api)
            )
        )
        await _until_a_patch_waits_on_the_row_lock(db)
        async with asyncio.timeout(10):
            r = await api.approve(jan)  # Grace: FOR SHARE is granted past the waiting PATCH
        assert r.status_code == 200, r.text
        assert not patch.done()
        await tx.commit()
        assert (await asyncio.wait_for(patch, 10)).status_code == 200
    finally:
        await holder.close()
        await _cancel(patch)
    snap = await db.fetchrow(
        "SELECT a.account_last4, a.requested_at, e.bank_updated_at FROM payroll.payout_attempts a "
        "JOIN payroll.payouts p ON p.id = a.payout_id JOIN payroll.employees e "
        "ON e.id = p.employee_id WHERE e.id = $1",
        emp,
    )
    assert snap["account_last4"] == "6789"  # Grace released the old details
    assert snap["bank_updated_at"] > snap["requested_at"]
    feb = await _run(api, "2026-02-01")
    r = await api.approve(feb, headers=_alice(api))
    assert _err(r) == (403, "approver_must_differ", {"rule": "bank_editor", "employee_ids": [emp]})


async def _failed_payout(api: Api, emp: str) -> tuple[str, str]:
    """January for ``emp``, approved by Grace; the bank rejects attempt 1 → (run id, payout id)."""
    jan = await _run(api, "2026-01-01")
    assert (await api.approve(jan)).status_code == 200
    [p] = await api.payouts(jan)
    assert (await api.result(p["id"], _REJECTED)).json()["outcome"] == "rejected"
    return jan, str(p["id"])


async def _attempt(db: Any, payout_id: str, attempt: int) -> Any:
    return await db.fetchrow(
        "SELECT a.account_last4, a.requested_by, a.requested_at, e.bank_updated_at "
        "FROM payroll.payout_attempts a JOIN payroll.payouts p ON p.id = a.payout_id "
        "JOIN payroll.employees e ON e.id = p.employee_id "
        "WHERE a.payout_id = $1 AND a.attempt = $2",
        payout_id,
        attempt,
    )


async def test_edit_waiting_on_a_retry_lock_is_not_vetted_by_it(
    api: Api, db: Any, migrated: PgBootstrapped
) -> None:
    """PAY-500, C73, the retry twin of the approve race: a lock holder (another approve or retry)
    holds the employee FOR SHARE; Alice's PATCH waits for FOR UPDATE; Grace's retry takes FOR
    SHARE too and snapshots the OLD details as attempt 2; then the PATCH commits. The edit is later
    than attempt 2's requested_at, so it stays unvetted: when the bank rejects attempt 2, Alice's
    own retry → 403 bank_editor."""
    emp = await _employee(api, 0)
    jan, payout = await _failed_payout(api, emp)
    patch: asyncio.Task[Any] | None = None
    holder = await admin_connect(migrated)
    try:
        tx = holder.transaction()
        await tx.start()
        await holder.execute("SELECT 1 FROM payroll.employees WHERE id = $1 FOR SHARE", emp)
        patch = asyncio.create_task(
            api.client.patch(
                f"/v1/employees/{emp}", json={"bank_account": ALICE_ACCOUNT}, headers=_alice(api)
            )
        )
        await _until_a_patch_waits_on_the_row_lock(db)
        async with asyncio.timeout(10):
            r = await api.retry(jan, payout)  # Grace: FOR SHARE is granted past the waiting PATCH
        assert r.status_code == 200, r.text
        assert r.json()["current_attempt"] == 2
        assert not patch.done()
        await tx.commit()
        assert (await asyncio.wait_for(patch, 10)).status_code == 200
    finally:
        await holder.close()
        await _cancel(patch)
    snap = await _attempt(db, payout, 2)
    assert (snap["account_last4"], snap["requested_by"]) == ("6789", APPROVER)  # the old details
    assert snap["bank_updated_at"] > snap["requested_at"]
    rejected = {**_REJECTED, "attempt": 2}
    assert (await api.result(payout, rejected)).json()["outcome"] == "rejected"
    r = await api.retry(jan, payout, headers=_alice(api))
    assert _err(r) == (403, "approver_must_differ", {"rule": "bank_editor", "employee_ids": [emp]})


async def test_edit_committed_while_a_retry_waits_is_vetted_by_it(
    api: Api, db: Any, migrated: PgBootstrapped
) -> None:
    """PAY-500, C73, the other side of the retry race: Grace's retry starts and waits for the
    run's FOR UPDATE (held here); Alice's PATCH, which never locks pay_runs, commits meanwhile;
    the retry then snapshots Alice's NEW details as attempt 2 (all within the 1 s lock_timeout).
    Its requested_at is clock_timestamp() read after the employee lock, so it is later than the
    edit: Grace released those details, and Alice's own retry after the bank rejects attempt 2 is
    allowed. (With now(), requested_at was the retry's transaction start, before the edit, so the
    details Grace released counted as unvetted and Alice was refused.)"""
    emp = await _employee(api, 0)
    jan, payout = await _failed_payout(api, emp)
    retry: asyncio.Task[Any] | None = None
    holder = await admin_connect(migrated)
    try:
        tx = holder.transaction()
        await tx.start()
        await holder.execute("SELECT 1 FROM payroll.pay_runs WHERE id = $1 FOR UPDATE", jan)
        retry = asyncio.create_task(api.retry(jan, payout))  # Grace
        # pg_stat_activity keeps the first 1 kB of a query, so the run SELECT's FOR UPDATE is cut
        # off; it is the only statement that waits on a lock here.
        await _until_waiting_on_a_row_lock(db, "SELECT payroll.pay_runs.id%", "the retry")
        await _redirect_bank(api, emp, _alice(api))
        assert not retry.done()
        await tx.commit()
        r = await asyncio.wait_for(retry, 10)
        assert r.status_code == 200, r.text
    finally:
        await holder.close()
        await _cancel(retry)
    snap = await _attempt(db, payout, 2)
    assert (snap["account_last4"], snap["requested_by"]) == ("7777", APPROVER)  # Alice's details
    assert snap["requested_at"] > snap["bank_updated_at"]
    rejected = {**_REJECTED, "attempt": 2}
    assert (await api.result(payout, rejected)).json()["outcome"] == "rejected"
    r = await api.retry(jan, payout, headers=_alice(api))
    assert r.status_code == 200, r.text
