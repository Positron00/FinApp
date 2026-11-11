"""``POST /v1/pay-runs/{id}/payouts/{payout_id}/retry`` and the payout list (design §4.4;
PAY-500, PAY-501, PAY-406, PAY-NFR-02, R34, C5, C8, C52; plan §12.1 "Redelivered old attempt
after a retry")."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import uuid4

import pytest
from payroll.domain.state import PayoutStatus, run_status_from_payouts

from tests.helpers import OTHER_ROUTING, VALID_ROUTING
from tests.integration.conftest import APPROVER, Api, other_company

pytestmark = pytest.mark.integration

REJECTED = {"outcome": "rejected", "reason": "account_closed"}
NEW_ACCOUNT = "55554444333"


async def _failed(api: Api, n: int = 1) -> tuple[dict[str, Any], dict[str, Any]]:
    """An approved run whose first payout's attempt 1 was rejected by the bank."""
    run = await api.approved_run(n)
    payouts = await api.payouts(run["id"])
    p = payouts[0]
    r = await api.result(p["id"], {"attempt": 1, **REJECTED})
    assert r.json()["outcome"] == "rejected"
    return run, p


async def _reject(api: Api, pid: str, attempt: int) -> None:
    r = await api.result(pid, {"attempt": attempt, **REJECTED})
    assert (r.status_code, r.json()["outcome"]) == (200, "rejected"), r.text


def _code(r: Any) -> tuple[int, str]:
    return r.status_code, r.json()["error"]["code"]


async def _assert_run_rule(db: Any, run_id: Any) -> None:
    statuses = [
        PayoutStatus(s["status"])
        for s in await db.fetch("SELECT status FROM payroll.payouts WHERE pay_run_id = $1", run_id)
    ]
    stored = await db.fetchval("SELECT status FROM payroll.pay_runs WHERE id = $1", run_id)
    assert stored == run_status_from_payouts(statuses)


async def test_retry_creates_attempt_two_with_a_fresh_snapshot(api: Api, db: Any) -> None:
    """PAY-500: after a rejection and a bank-detail fix, retry inserts attempt 2 with the new
    snapshot (served by the instruction), sets the payout requested, writes one outbox row
    (republish_seq 0), moves the run back to processing and audits payout.retry_requested."""
    run, p = await _failed(api)
    assert (await api.client.get(f"/v1/pay-runs/{run['id']}", headers=api.approver())).json()[
        "status"
    ] == "partially_failed"
    r = await api.client.patch(
        f"/v1/employees/{p['employee_id']}",
        json={"bank_account": NEW_ACCOUNT, "bank_routing": OTHER_ROUTING},
        headers=api.payroll_admin(),
    )
    assert r.status_code == 200
    r = await api.retry(run["id"], p["id"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["status"], body["current_attempt"]) == ("requested", 2)
    assert [(a["attempt"], a["status"], a["account_last4"]) for a in body["attempts"]] == [
        (1, "rejected", "6789"),
        (2, "requested", "4333"),
    ]
    instr = await api.client.get(
        f"/v1/internal/payouts/{p['id']}/instruction?attempt=2", headers=api.worker()
    )
    assert (instr.json()["routing"], instr.json()["account"]) == (OTHER_ROUTING, NEW_ACCOUNT)
    rows = await db.fetch(
        "SELECT payload FROM payroll.outbox WHERE payout_id = $1 AND attempt = 2", p["id"]
    )
    assert len(rows) == 1
    assert json.loads(rows[0]["payload"])["data"]["republish_seq"] == 0
    run_row = await db.fetchrow(
        "SELECT status, processing_since FROM payroll.pay_runs WHERE id = $1", run["id"]
    )
    assert run_row["status"] == "processing"
    assert run_row["processing_since"] is not None
    payout_row = await db.fetchrow(
        "SELECT final_at, status FROM payroll.payouts WHERE id = $1", p["id"]
    )
    assert (payout_row["status"], payout_row["final_at"]) == ("requested", None)
    audit = await db.fetchrow(
        "SELECT actor_id, details FROM payroll.audit_events WHERE event = 'payout.retry_requested'"
    )
    assert audit["actor_id"] == APPROVER
    assert json.loads(audit["details"]) == {"attempt": 2, "account_last4": "4333"}
    settled = {"attempt": 2, "outcome": "settled", "bank_reference": "BR-2"}
    assert (await api.result(p["id"], settled)).json()["outcome"] == "settled"
    await _assert_run_rule(db, run["id"])


async def test_not_retryable_states(api: Api, db: Any) -> None:
    """PAY-500: requested, indeterminate, needs_operator and paid payouts → 409
    payout_not_retryable with the payout and attempt status; nothing is written."""
    run = await api.approved_run(4)
    ps = await api.payouts(run["id"])
    await api.result(ps[1]["id"], {"attempt": 1, "outcome": "indeterminate"})
    await api.result(ps[2]["id"], {"attempt": 1, "outcome": "poison"})
    await api.result(ps[3]["id"], {"attempt": 1, "outcome": "settled", "bank_reference": "B"})
    expected = ["requested", "indeterminate", "needs_operator", "settled"]
    before = await db.fetchval("SELECT count(*) FROM payroll.outbox")
    for p, attempt_status in zip(ps, expected, strict=True):
        r = await api.retry(run["id"], p["id"])
        assert _code(r) == (409, "payout_not_retryable")
        assert r.json()["error"]["details"]["attempt_status"] == attempt_status
    assert await db.fetchval("SELECT count(*) FROM payroll.outbox") == before
    assert await db.fetchval("SELECT max(attempt) FROM payroll.payout_attempts") == 1


async def test_retry_limit_reached_at_attempt_five(api: Api) -> None:
    """PAY-500: PAYOUT_MAX_ATTEMPTS = 5 including the first, so the retry after attempt 5 is
    409 retry_limit_reached."""
    run, p = await _failed(api)
    for attempt in range(2, 6):
        assert (await api.retry(run["id"], p["id"])).status_code == 200
        await _reject(api, p["id"], attempt)
    r = await api.retry(run["id"], p["id"])
    assert _code(r) == (409, "retry_limit_reached")
    assert r.json()["error"]["details"] == {"current_attempt": 5, "max_attempts": 5}


async def test_inactive_or_purged_employee(api: Api, db: Any) -> None:
    """PAY-500, PAY-608: an inactive employee, or one whose bank details were purged, → 409
    employee_bank_details_missing with the ID."""
    run, p = await _failed(api)
    r = await api.client.patch(
        f"/v1/employees/{p['employee_id']}",
        json={"status": "inactive"},
        headers=api.payroll_admin(),
    )
    assert r.status_code == 200
    r = await api.retry(run["id"], p["id"])
    assert _code(r) == (409, "employee_bank_details_missing")
    assert r.json()["error"]["details"] == {"employee_ids": [p["employee_id"]]}
    await db.execute(
        "UPDATE payroll.employees SET bank_routing_enc = NULL, bank_account_enc = NULL, "
        "account_last4 = NULL, bank_purged_at = now() WHERE id = $1",
        p["employee_id"],
    )
    assert _code(await api.retry(run["id"], p["id"])) == (409, "employee_bank_details_missing")


async def test_dual_approval_uses_the_run_snapshot(api: Api, db: Any) -> None:
    """PAY-500, R34: with the run's snapshot on, a retrier who last edited the employee's bank
    details → 403 approver_must_differ (bank_editor); with the snapshot off it passes even
    though the company setting is still on."""
    run, p = await _failed(api)
    await db.execute(
        "UPDATE payroll.employees SET bank_updated_by = $1 WHERE id = $2",
        APPROVER,
        p["employee_id"],
    )
    r = await api.retry(run["id"], p["id"])
    assert _code(r) == (403, "approver_must_differ")
    assert r.json()["error"]["details"] == {
        "rule": "bank_editor",
        "employee_ids": [p["employee_id"]],
    }
    await db.execute(
        "UPDATE payroll.pay_runs SET require_dual_approval = false WHERE id = $1", run["id"]
    )
    assert await db.fetchval("SELECT require_dual_approval FROM payroll.company_settings")
    assert (await api.retry(run["id"], p["id"])).status_code == 200


async def test_old_attempt_after_retry_is_not_current(api: Api) -> None:
    """PAY-NFR-02, plan §12.1 "Redelivered old attempt after a retry": the instruction for
    attempt 1 → 409 attempt_not_current (no bank call), and a late result for it → 409."""
    run, p = await _failed(api)
    assert (await api.retry(run["id"], p["id"])).status_code == 200
    r = await api.client.get(
        f"/v1/internal/payouts/{p['id']}/instruction?attempt=1", headers=api.worker()
    )
    assert _code(r) == (409, "attempt_not_current")
    assert r.json()["error"]["details"] == {"current_attempt": 2}
    late = await api.result(p["id"], {"attempt": 1, "outcome": "settled", "bank_reference": "X"})
    assert _code(late) == (409, "attempt_not_current")


async def test_retry_racing_a_late_result(api: Api, db: Any) -> None:
    """PAY-NFR-02: a retry and a late result for attempt 1 in parallel end consistently: one
    attempt 2, the late result either stored (200, rejected) or not current (409)."""
    run, p = await _failed(api)
    retry, late = await asyncio.gather(
        api.retry(run["id"], p["id"]),
        api.result(p["id"], {"attempt": 1, "outcome": "settled", "bank_reference": "late"}),
    )
    assert retry.status_code == 200
    assert (late.status_code, late.json().get("outcome")) in {(200, "rejected"), (409, None)}
    rows = await db.fetch(
        "SELECT attempt, status FROM payroll.payout_attempts WHERE payout_id = $1 ORDER BY 1",
        p["id"],
    )
    assert [(r["attempt"], r["status"]) for r in rows] == [(1, "rejected"), (2, "requested")]
    await _assert_run_rule(db, run["id"])


async def test_idempotency_key_is_honoured(api: Api, db: Any) -> None:
    """PAY-500, C52, §4.8: the same key replays the first 200 (Idempotent-Replayed) and
    creates nothing; a new key then gets 409; a malformed key → 422."""
    run, p = await _failed(api)
    first = await api.retry(run["id"], p["id"], key="click-1")
    again = await api.retry(run["id"], p["id"], key="click-1")
    assert (first.status_code, again.status_code) == (200, 200)
    assert again.json() == first.json()
    assert again.headers["idempotent-replayed"] == "true"
    assert await db.fetchval("SELECT max(attempt) FROM payroll.payout_attempts") == 2
    assert _code(await api.retry(run["id"], p["id"], key="click-2")) == (
        409,
        "payout_not_retryable",
    )
    r = await api.retry(run["id"], p["id"], key="bad key")
    assert _code(r) == (422, "validation_error")


async def test_not_found_cases(api: Api, db: Any) -> None:
    """PAY-500, PAY-601: a payout of another run, an unknown run, or another company → 404."""
    run, p = await _failed(api)
    await api.create_employee(name="Other", email="other@example.com", schedule="biweekly")
    other_run = await api.create_run("biweekly", "2026-01-05")
    r = await api.retry(other_run["id"], p["id"])
    assert _code(r) == (404, "payout_not_found")
    assert _code(await api.retry(str(uuid4()), p["id"])) == (404, "pay_run_not_found")
    cid = await other_company(db)
    r = await api.retry(run["id"], p["id"], headers=api.approver(cid=cid))
    assert _code(r) == (404, "pay_run_not_found")
    assert _code(await api.retry(run["id"], p["id"], headers=api.payroll_admin())) == (
        403,
        "forbidden_role",
    )


async def test_payout_list_with_history(api: Api) -> None:
    """PAY-501, C5, C8: pages with cursor/limit; each payout has payslip_id, amount_cents and
    every attempt with account_last4 only, ascending; no_transfer payouts have no attempts."""
    run, p = await _failed(api, 3)
    assert (await api.retry(run["id"], p["id"])).status_code == 200
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        url = f"/v1/pay-runs/{run['id']}/payouts?limit=2" + (f"&cursor={cursor}" if cursor else "")
        page = (await api.client.get(url, headers=api.admin())).json()
        items += page["items"]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(items) == 3
    assert set(items[0]) == {
        "id", "pay_run_id", "payslip_id", "employee_id", "employee_name", "amount_cents",
        "currency", "status", "no_transfer", "current_attempt", "attempts",
    }  # fmt: skip
    [mine] = [i for i in items if i["id"] == p["id"]]
    assert [a["attempt"] for a in mine["attempts"]] == [1, 2]
    assert set(mine["attempts"][0]) == {
        "attempt", "status", "failure_reason", "bank_reference", "account_last4",
        "requested_at", "final_at",
    }  # fmt: skip
    assert mine["attempts"][0]["failure_reason"] == "account_closed"
    dump = json.dumps(items)
    assert "000123456789" not in dump
    assert VALID_ROUTING not in dump
    failed = await api.client.get(
        f"/v1/pay-runs/{run['id']}/payouts?status=requested", headers=api.approver()
    )
    assert len(failed.json()["items"]) == 3
