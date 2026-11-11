"""Maintenance (design §5.4; PAY-608, PAY-407, PAY-NFR-08, ADR 0004, C44)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from payroll.maintenance import LOCK_NAME, TASK_NAMES, tasks
from pycommon.db import advisory_lock_key
from pycommon.maintenance import MaintenanceRunner, MaintenanceTask
from pycommon.testing import JsonLogCapture
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.conftest import Api

pytestmark = pytest.mark.integration


async def _tick(api: Api) -> bool:
    assert api.rt.maintenance is not None
    return await api.rt.maintenance.tick()


async def test_advisory_lock_lets_one_replica_tick(api: Api, db: Any) -> None:
    """PAY-608: while another session holds payroll:maintenance the tick is skipped; once it is
    released the tick runs."""
    key = advisory_lock_key(LOCK_NAME)
    assert await db.fetchval("SELECT pg_try_advisory_lock($1)", key)
    try:
        assert await _tick(api) is False
    finally:
        await db.execute("SELECT pg_advisory_unlock($1)", key)
    assert await _tick(api) is True


async def test_every_task_logs_and_a_failure_does_not_stop_the_others(
    api: Api, caplog_json: JsonLogCapture
) -> None:
    """PAY-608, C44: each task logs maintenance.task.completed; a raising task logs
    maintenance.task.failed and the later tasks still run."""
    assert await _tick(api)
    done = [e["task"] for e in caplog_json.events("maintenance.task.completed")]
    assert done == list(TASK_NAMES)
    caplog_json.clear()

    async def boom(_session: AsyncSession) -> int:
        raise RuntimeError("boom")

    real = tasks(api.rt)
    runner = MaintenanceRunner(
        api.app.state.ctx.engine,
        api.rt.sessions,
        lock_name=LOCK_NAME,
        tasks=[real[0], MaintenanceTask("boom", boom), *real[1:]],
    )
    assert await runner.tick()
    [failed] = caplog_json.events("maintenance.task.failed")
    assert (failed["task"], failed["error_type"]) == ("boom", "RuntimeError")
    assert [e["task"] for e in caplog_json.events("maintenance.task.completed")] == list(TASK_NAMES)


async def test_outbox_and_idempotency_purges(api: Api, db: Any) -> None:
    """PAY-608: sent outbox rows older than RETENTION_OUTBOX_DAYS (7) go; younger sent rows and
    every unsent row stay. Idempotency keys past expires_at go; live ones stay."""
    run = await api.approved_run(3)
    ids = [r["id"] for r in await db.fetch("SELECT id FROM payroll.outbox ORDER BY id")]
    await db.execute(
        "UPDATE payroll.outbox SET sent_at = now() - interval '8 days' WHERE id = $1", ids[0]
    )
    await db.execute(
        "UPDATE payroll.outbox SET sent_at = now() - interval '6 days' WHERE id = $1", ids[1]
    )
    await db.execute(
        "UPDATE payroll.outbox SET created_at = now() - interval '30 days' WHERE id = $1", ids[2]
    )
    assert (await api.approve(run["id"], "k-live")).status_code == 409  # not stored (error)
    await db.execute(
        "INSERT INTO payroll.idempotency_keys (company_id, scope, key, request_hash, "
        "response_status, response_body, expires_at) VALUES "
        "($1, 's', 'old', repeat('a', 64), 200, '{}', now() - interval '1 minute'), "
        "($1, 's', 'new', repeat('a', 64), 200, '{}', now() + interval '1 hour')",
        api.cid,
    )
    before = await db.fetchval("SELECT count(*) FROM payroll.idempotency_keys")
    assert await _tick(api)
    left = {r["id"] for r in await db.fetch("SELECT id FROM payroll.outbox")}
    assert left == {ids[1], ids[2]}
    keys = {r["key"] for r in await db.fetch("SELECT key FROM payroll.idempotency_keys")}
    assert "old" not in keys
    assert "new" in keys
    assert await db.fetchval("SELECT count(*) FROM payroll.idempotency_keys") == before - 1


async def _deactivate(api: Api, db: Any, employee_id: str, days: int) -> None:
    r = await api.client.patch(
        f"/v1/employees/{employee_id}", json={"status": "inactive"}, headers=api.payroll_admin()
    )
    assert r.status_code == 200
    await db.execute(
        "UPDATE payroll.employees SET deactivated_at = now() - make_interval(days => $2) "
        "WHERE id = $1",
        employee_id,
        days,
    )


async def test_bank_detail_purge(api: Api, db: Any, caplog_json: JsonLogCapture) -> None:
    """PAY-608, PAY-NFR-08, ADR 0004: 31 days after deactivation, with every attempt final, the
    employee's bank columns are nulled (bank_purged_at set) and the final attempts' snapshots
    too, with an audit row; 29 days → kept; a non-final attempt → skipped; the employee API then
    shows no bank details."""
    run = await api.approved_run(3)
    ps = await api.payouts(run["id"])
    old, young, busy = ps
    await api.result(old["id"], {"attempt": 1, "outcome": "rejected", "reason": "x"})
    await api.result(young["id"], {"attempt": 1, "outcome": "settled", "bank_reference": "B"})
    await api.result(busy["id"], {"attempt": 1, "outcome": "indeterminate"})
    await _deactivate(api, db, old["employee_id"], 31)
    await _deactivate(api, db, young["employee_id"], 29)
    await _deactivate(api, db, busy["employee_id"], 31)
    assert await _tick(api)

    rows = {
        str(r["id"]): r
        for r in await db.fetch(
            "SELECT id, bank_routing_enc, bank_account_enc, account_last4, bank_purged_at "
            "FROM payroll.employees"
        )
    }
    purged = rows[old["employee_id"]]
    assert (purged["bank_routing_enc"], purged["bank_account_enc"], purged["account_last4"]) == (
        None,
        None,
        None,
    )
    assert purged["bank_purged_at"] is not None
    for kept in (young["employee_id"], busy["employee_id"]):
        assert rows[kept]["bank_account_enc"] is not None
    snapshot = await db.fetchval(
        "SELECT bank_snapshot_enc FROM payroll.payout_attempts WHERE payout_id = $1", old["id"]
    )
    assert snapshot is None
    for other in (young["id"], busy["id"]):
        assert await db.fetchval(
            "SELECT bank_snapshot_enc FROM payroll.payout_attempts WHERE payout_id = $1", other
        )
    audit = await db.fetchrow(
        "SELECT actor_id, entity_id, details::text AS d FROM payroll.audit_events "
        "WHERE event = 'employee.bank_details_purged'"
    )
    assert (audit["actor_id"], str(audit["entity_id"])) == (
        "system:maintenance",
        old["employee_id"],
    )
    assert json.loads(audit["d"])["attempts_nulled"] == 1
    [log] = caplog_json.events("employee.bank_details_purged")
    assert (log["employee_id"], log["attempts_nulled"]) == (old["employee_id"], 1)
    emp = await api.client.get(f"/v1/employees/{old['employee_id']}", headers=api.admin())
    assert (emp.json()["has_bank_details"], emp.json()["account_last4"]) == (False, None)
    # PAY-500 / plan J5: a retry for the purged employee is refused
    r = await api.retry(run["id"], old["id"])
    assert (r.status_code, r.json()["error"]["code"]) == (409, "employee_bank_details_missing")
    assert await _tick(api)  # idempotent: nothing more to purge
    assert len(caplog_json.events("employee.bank_details_purged")) == 1


async def test_approve_after_purge_is_refused(api: Api, db: Any) -> None:
    """PAY-608, PAY-400, R34: a draft run that still holds a purged employee → approve 409
    employee_bank_details_missing with the ID, and no payouts."""
    emp = await api.create_employee()
    run = await api.create_run()
    await _deactivate(api, db, emp["id"], 31)
    assert await _tick(api)
    assert await db.fetchval(
        "SELECT bank_purged_at IS NOT NULL FROM payroll.employees WHERE id = $1", emp["id"]
    )
    await api.calculate(run["id"], name="Carl Calculator")
    r = await api.approve(run["id"])
    assert (r.status_code, r.json()["error"]["code"]) == (409, "employee_bank_details_missing")
    assert r.json()["error"]["details"] == {"employee_ids": [emp["id"]]}
    assert await db.fetchval("SELECT count(*) FROM payroll.payouts") == 0
