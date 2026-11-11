"""``POST /v1/pay-runs/{id}/payouts/{payout_id}/resolve`` (design §4.4 "Resolve", §3.5a;
PAY-502, PAY-406, PAY-409, R8, C13, C48; plan §12.1 "Poison message")."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any
from uuid import uuid4

import pytest
from payroll.domain.state import PayoutStatus, run_status_from_payouts
from pycommon.testing import FakeClock, JsonLogCapture, OtelMemory

from tests.integration.conftest import APPROVER, Api, ApiFactory, other_company

pytestmark = pytest.mark.integration

POISON = {"attempt": 1, "outcome": "poison", "reason": "schema_invalid"}
NOTE = "GET /v1/transfers/key shows nothing; ticket OPS-9921"


def _code(r: Any) -> tuple[int, str]:
    return r.status_code, r.json()["error"]["code"]


async def _blocked(api: Api, db: Any, n: int = 1) -> tuple[dict[str, Any], dict[str, Any]]:
    """An approved run whose first payout's attempt 1 was reported poison (needs_operator), with
    the runtime clock frozen at needs_operator_at."""
    run = await api.approved_run(n)
    p = (await api.payouts(run["id"]))[0]
    assert (await api.result(p["id"], POISON)).json()["outcome"] == "needs_operator"
    at: datetime = await db.fetchval(
        "SELECT needs_operator_at FROM payroll.payout_attempts WHERE payout_id = $1", p["id"]
    )
    api.rt.clock = FakeClock(start=at)
    return run, p


async def _attempt(db: Any, pid: str, attempt: int = 1) -> dict[str, Any]:
    row = await db.fetchrow(
        "SELECT * FROM payroll.payout_attempts WHERE payout_id = $1 AND attempt = $2", pid, attempt
    )
    return dict(row)


async def _assert_run_rule(db: Any, run_id: Any) -> None:
    statuses = [
        PayoutStatus(s["status"])
        for s in await db.fetch("SELECT status FROM payroll.payouts WHERE pay_run_id = $1", run_id)
    ]
    stored = await db.fetchval("SELECT status FROM payroll.pay_runs WHERE id = $1", run_id)
    assert stored == run_status_from_payouts(statuses)


async def test_redrive_then_settled(api: Api, db: Any, caplog_json: JsonLogCapture) -> None:
    """PAY-502, C13: redrive → attempt requested, reason and needs_operator_at cleared,
    publish_count 1, last_published_at = now, requested_at unchanged, a new outbox row with a
    fresh event_id and republish_seq 0; a settled result then pays. Audit holds note_len only."""
    run, p = await _blocked(api, db)
    before = await _attempt(db, p["id"])
    await db.execute("UPDATE payroll.outbox SET sent_at = now()")
    api.rt.clock.advance(5)  # type: ignore[attr-defined]
    r = await api.resolve(run["id"], p["id"], "redrive", NOTE)
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["attempts"][0]["status"]) == ("requested", "requested")
    after = await _attempt(db, p["id"])
    assert after["status"] == "requested"
    assert (after["failure_reason"], after["needs_operator_at"]) == (None, None)
    assert after["publish_count"] == 1
    assert after["last_published_at"] == api.rt.clock.now()
    assert after["requested_at"] == before["requested_at"]
    [row] = await db.fetch(
        "SELECT id, payload FROM payroll.outbox WHERE payout_id = $1 AND sent_at IS NULL", p["id"]
    )
    payload = json.loads(row["payload"])
    assert payload["data"]["republish_seq"] == 0
    assert payload["data"]["attempt"] == 1
    [resolved] = caplog_json.events("payout.operator_resolved")
    assert resolved["event_id"] == str(row["id"])
    assert (resolved["action"], resolved["note_len"]) == ("redrive", len(NOTE))
    audit = await db.fetchrow(
        "SELECT actor_id, details::text AS d FROM payroll.audit_events "
        "WHERE event = 'payout.operator_resolved'"
    )
    assert audit["actor_id"] == APPROVER
    assert json.loads(audit["d"]) == {"attempt": 1, "action": "redrive", "note_len": len(NOTE)}
    assert "OPS-9921" not in caplog_json.text
    assert await db.fetchval("SELECT count(*) FROM payroll.audit_events WHERE details::text "
                             "LIKE '%OPS-9921%'") == 0  # fmt: skip
    settled = {"attempt": 1, "outcome": "settled", "bank_reference": "BR-1"}
    assert (await api.result(p["id"], settled)).json()["outcome"] == "settled"
    assert await db.fetchval("SELECT status FROM payroll.pay_runs WHERE id = $1", run["id"]) == (
        "paid"
    )


async def test_mark_rejected_waits_then_retry_pays(
    api_factory: ApiFactory, db: Any, otel_memory: OtelMemory
) -> None:
    """PAY-502, C48, PAY-406: mark_rejected inside PAYOUT_MARK_REJECTED_MIN_WAIT_S → 409
    resolve_too_early with Retry-After and no change; 120 s later → attempt rejected /
    operator_rejected, payout failed, the run final (first_final_at, histogram once,
    payroll_payouts_total{operator_rejected}); then a retry → attempt 2 → paid."""
    api = await api_factory(instrument=True)
    run, p = await _blocked(api, db)
    api.rt.clock.advance(100)  # type: ignore[attr-defined]
    r = await api.resolve(run["id"], p["id"], "mark_rejected")
    assert _code(r) == (409, "resolve_too_early")
    assert r.headers["retry-after"] == "20"
    assert r.json()["error"]["details"] == {"retry_after_s": 20}
    assert (await _attempt(db, p["id"]))["status"] == "needs_operator"
    api.rt.clock.advance(20)  # type: ignore[attr-defined]
    r = await api.resolve(run["id"], p["id"], "mark_rejected")
    assert r.status_code == 200, r.text
    a = await _attempt(db, p["id"])
    assert (a["status"], a["failure_reason"], a["needs_operator_at"]) == (
        "rejected",
        "operator_rejected",
        None,
    )
    assert a["final_at"] is not None
    run_row = await db.fetchrow("SELECT * FROM payroll.pay_runs WHERE id = $1", run["id"])
    assert run_row["status"] == "partially_failed"
    assert run_row["first_final_at"] is not None
    points = {attrs.get("outcome"): v for attrs, v in otel_memory.metric_points("payroll_payouts")}
    assert points.get("operator_rejected") == 1
    [hist] = [v for _, v in otel_memory.metric_points("payroll_payout_settlement_seconds")]
    assert hist.count == 1
    assert (await api.retry(run["id"], p["id"])).status_code == 200
    settled = {"attempt": 2, "outcome": "settled", "bank_reference": "BR-2"}
    assert (await api.result(p["id"], settled)).json()["outcome"] == "settled"
    assert await db.fetchval("SELECT status FROM payroll.pay_runs WHERE id = $1", run["id"]) == (
        "paid"
    )
    [hist] = [v for _, v in otel_memory.metric_points("payroll_payout_settlement_seconds")]
    assert hist.count == 1  # observed once per run, not again on the second final entry


async def test_redrive_has_no_wait(api: Api, db: Any) -> None:
    """PAY-502, C48: redrive succeeds at once (same bank key, so it can't pay twice)."""
    run, p = await _blocked(api, db)
    assert (await api.resolve(run["id"], p["id"], "redrive")).status_code == 200


async def test_not_blocked_states(api: Api, db: Any, caplog_json: JsonLogCapture) -> None:
    """PAY-502: requested, indeterminate, settled, rejected and no_transfer payouts → 409
    payout_not_blocked; logged payout.resolve_rejected; nothing changes."""
    run = await api.approved_run(4)
    zero = await api.create_employee(
        name="Zero", email="zero@example.com", annual_salary_cents=None,
        pay_type="hourly", hourly_rate_cents=1000,
    )  # fmt: skip
    ps = await api.payouts(run["id"])
    await api.result(ps[1]["id"], {"attempt": 1, "outcome": "indeterminate"})
    await api.result(ps[2]["id"], {"attempt": 1, "outcome": "settled", "bank_reference": "B"})
    await api.result(ps[3]["id"], {"attempt": 1, "outcome": "rejected", "reason": "x"})
    for p, status in zip(ps, ["requested", "indeterminate", "settled", "rejected"], strict=True):
        for action in ("redrive", "mark_rejected"):
            r = await api.resolve(run["id"], p["id"], action)
            assert _code(r) == (409, "payout_not_blocked")
            assert r.json()["error"]["details"] == {"attempt_status": status}
    assert len(caplog_json.events("payout.resolve_rejected")) == 8
    # a no_transfer payout (net 0): an hourly employee with 0 hours
    run2 = await api.create_run("monthly", "2026-02-01")
    await api.client.put(
        f"/v1/pay-runs/{run2['id']}/hours",
        json={"entries": [{"employee_id": zero["id"], "hours_hundredths": 0}]},
        headers=api.payroll_admin(),
    )
    await api.calculate(run2["id"], name="Carl Calculator")
    assert (await api.approve(run2["id"])).status_code == 200
    [nt] = [x for x in await api.payouts(run2["id"]) if x["no_transfer"]]
    r = await api.resolve(run2["id"], nt["id"], "redrive")
    assert _code(r) == (409, "payout_not_blocked")
    assert r.json()["error"]["details"] == {"attempt_status": None}


async def test_not_found_validation_and_role(api: Api, db: Any) -> None:
    """PAY-502, PAY-601, C14: another run or company → 404; an unknown action or a missing
    note → 422; a payroll_admin → 403."""
    run, p = await _blocked(api, db)
    assert _code(await api.resolve(str(uuid4()), p["id"], "redrive")) == (404, "pay_run_not_found")
    cid = await other_company(db)
    r = await api.resolve(run["id"], p["id"], "redrive", headers=api.approver(cid=cid))
    assert _code(r) == (404, "pay_run_not_found")
    await api.create_employee(name="B", email="b@example.com", schedule="biweekly")
    other_run = await api.create_run("biweekly", "2026-01-05")
    assert _code(await api.resolve(other_run["id"], p["id"], "redrive")) == (
        404,
        "payout_not_found",
    )
    url = f"/v1/pay-runs/{run['id']}/payouts/{p['id']}/resolve"
    for body in (
        {"action": "void", "note": "x"},
        {"action": "redrive"},
        {"action": "redrive", "note": ""},
    ):
        r = await api.client.post(url, json=body, headers=api.approver())
        assert _code(r) == (422, "validation_error")
    r = await api.resolve(run["id"], p["id"], "redrive", headers=api.payroll_admin())
    assert _code(r) == (403, "forbidden_role")
    assert (await _attempt(db, p["id"]))["status"] == "needs_operator"


async def test_resolve_racing_a_late_result(api: Api, db: Any) -> None:
    """PAY-502, R7, §4.10: a redrive and a late settled result for the blocked attempt in
    parallel end consistently: the payout is paid exactly once, whichever runs first."""
    run, p = await _blocked(api, db)
    resolve, late = await asyncio.gather(
        api.resolve(run["id"], p["id"], "redrive"),
        api.result(p["id"], {"attempt": 1, "outcome": "settled", "bank_reference": "late"}),
    )
    assert late.json()["outcome"] == "settled"
    assert resolve.status_code in (200, 409)
    if resolve.status_code == 409:
        assert resolve.json()["error"]["code"] == "payout_not_blocked"
    a = await _attempt(db, p["id"])
    assert (a["status"], a["bank_reference"]) == ("settled", "late")
    await _assert_run_rule(db, run["id"])


async def _mark_rejected(api: Api, db: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """A blocked payout marked rejected by the operator after the C48 wait."""
    run, p = await _blocked(api, db)
    api.rt.clock.advance(120)  # type: ignore[attr-defined]
    assert (await api.resolve(run["id"], p["id"], "mark_rejected")).status_code == 200
    return run, p


async def _late_audits(db: Any, pid: str) -> list[dict[str, Any]]:
    rows = await db.fetch(
        "SELECT actor_id, details::text AS d FROM payroll.audit_events "
        "WHERE event = 'payout.late_settlement' AND entity_id = $1",
        pid,
    )
    return [{"actor_id": r["actor_id"], **json.loads(r["d"])} for r in rows]


def _late_count(otel_memory: OtelMemory) -> float:
    [(_, value)] = otel_memory.metric_points("payroll_late_settlements")
    return float(value)


async def test_late_settled_after_mark_rejected_is_recorded(
    api_factory: ApiFactory, db: Any, otel_memory: OtelMemory, caplog_json: JsonLogCapture
) -> None:
    """PAY-NFR-02, PAY-405, C48: a settled answer for the attempt the operator marked rejected
    keeps the contract (200, outcome rejected, nothing applied) but is never dropped: an audit row
    payout.late_settlement (attempt, bank_reference, no later settled attempt), an error log and
    payroll_late_settlements_total + 1 (pre-initialised at 0). A second report with another key
    logs again but neither audits nor counts twice."""
    api = await api_factory(instrument=True)
    assert _late_count(otel_memory) == 0
    run, p = await _mark_rejected(api, db)
    late = {"attempt": 1, "outcome": "settled", "bank_reference": "BR-LATE-1"}
    r = await api.result(p["id"], late)
    assert (r.status_code, r.json()["outcome"]) == (200, "rejected")
    a = await _attempt(db, p["id"])
    assert (a["status"], a["failure_reason"], a["bank_reference"]) == (
        "rejected",
        "operator_rejected",
        None,
    )
    assert await _late_audits(db, p["id"]) == [
        {
            "actor_id": "service:payout-worker",
            "attempt": 1,
            "current_attempt": 1,
            "attempt_status": "rejected",
            "failure_reason": "operator_rejected",
            "bank_reference": "BR-LATE-1",
            "later_attempt_settled": False,
        }
    ]
    [log] = caplog_json.events("payout.late_settlement")
    assert log["level"] == "error"
    assert (log["payout_id"], log["pay_run_id"], log["recorded"]) == (p["id"], run["id"], True)
    assert _late_count(otel_memory) == 1
    again = await api.result(p["id"], late)
    assert (again.status_code, again.json()["outcome"]) == (200, "rejected")
    assert len(await _late_audits(db, p["id"])) == 1
    assert [e["recorded"] for e in caplog_json.events("payout.late_settlement")] == [True, False]
    assert _late_count(otel_memory) == 1


async def test_late_settled_for_a_superseded_attempt_is_recorded(
    api_factory: ApiFactory, db: Any, otel_memory: OtelMemory, caplog_json: JsonLogCapture
) -> None:
    """PAY-NFR-02, PAY-500, PAY-405: after mark_rejected and a retry whose attempt 2 settled, a
    late settled answer for attempt 1 still gets 409 attempt_not_current, and the rollback of
    that 409 does not lose it: payout.late_settlement is audited in its own transaction with
    later_attempt_settled (attempt 2: paid twice), logged at error and counted."""
    api = await api_factory(instrument=True)
    run, p = await _mark_rejected(api, db)
    assert (await api.retry(run["id"], p["id"])).status_code == 200
    settled = {"attempt": 2, "outcome": "settled", "bank_reference": "BR-2"}
    assert (await api.result(p["id"], settled)).json()["outcome"] == "settled"
    late = {"attempt": 1, "outcome": "settled", "bank_reference": "BR-LATE-1"}
    r = await api.result(p["id"], late)
    assert _code(r) == (409, "attempt_not_current")
    assert r.json()["error"]["details"] == {"current_attempt": 2}
    assert await _late_audits(db, p["id"]) == [
        {
            "actor_id": "service:payout-worker",
            "attempt": 1,
            "current_attempt": 2,
            "attempt_status": "rejected",
            "failure_reason": "operator_rejected",
            "bank_reference": "BR-LATE-1",
            "later_attempt_settled": True,
            "later_settled_attempt": 2,
        }
    ]
    [log] = caplog_json.events("payout.late_settlement")
    assert (log["level"], log["later_attempt_settled"], log["recorded"]) == ("error", True, True)
    assert _late_count(otel_memory) == 1
    # nothing else changed: attempt 1 stays rejected, the payout stays paid by attempt 2
    assert (await _attempt(db, p["id"]))["status"] == "rejected"
    assert await db.fetchval("SELECT status FROM payroll.payouts WHERE id = $1", p["id"]) == "paid"
    # the same answer again: still 409, no second audit row or count
    assert _code(await api.result(p["id"], late)) == (409, "attempt_not_current")
    assert len(await _late_audits(db, p["id"])) == 1
    assert _late_count(otel_memory) == 1


async def test_ordinary_answers_are_not_late_settlements(api: Api, db: Any) -> None:
    """PAY-NFR-02, PAY-405: a duplicate settled for a settled attempt, a rejected answer for an
    operator-rejected attempt and a result for an unknown attempt number are not late
    settlements."""
    run, p = await _mark_rejected(api, db)
    rejected = {"attempt": 1, "outcome": "rejected", "reason": "account_closed"}
    assert (await api.result(p["id"], rejected)).json()["outcome"] == "rejected"
    unknown = {"attempt": 7, "outcome": "settled", "bank_reference": "BR-7"}
    assert _code(await api.result(p["id"], unknown)) == (409, "attempt_not_current")
    assert (await api.retry(run["id"], p["id"])).status_code == 200
    settled = {"attempt": 2, "outcome": "settled", "bank_reference": "BR-2"}
    for _ in range(2):
        assert (await api.result(p["id"], settled)).json()["outcome"] == "settled"
    assert await _late_audits(db, p["id"]) == []
