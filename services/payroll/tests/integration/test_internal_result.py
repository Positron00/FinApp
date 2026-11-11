"""``POST /v1/internal/payouts/{id}/result`` (design §4.7; PAY-405, PAY-406, PAY-409, R7, C26,
C48)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from payroll.domain.state import PayoutStatus, run_status_from_payouts
from pycommon.testing import OtelMemory

from tests.integration.conftest import Api, ApiFactory, other_company

pytestmark = pytest.mark.integration

SETTLED = {"attempt": 1, "outcome": "settled", "bank_reference": "bank-ref-1"}
REJECTED = {"attempt": 1, "outcome": "rejected", "reason": "account_closed"}
INDET = {"attempt": 1, "outcome": "indeterminate"}
POISON = {"attempt": 1, "outcome": "poison", "reason": "schema_invalid"}


async def _state(db: Any, pid: str) -> dict[str, Any]:
    row = await db.fetchrow(
        "SELECT p.status AS payout, p.final_at AS payout_final, a.status AS attempt, "
        "a.failure_reason, a.bank_reference, a.final_at, a.needs_operator_at, r.status AS run, "
        "r.processing_since, r.first_final_at, r.id AS run_id "
        "FROM payroll.payouts p JOIN payroll.payout_attempts a ON a.payout_id = p.id "
        "JOIN payroll.pay_runs r ON r.id = p.pay_run_id WHERE p.id = $1",
        pid,
    )
    return dict(row)


async def _assert_run_rule(db: Any, run_id: Any) -> None:
    statuses = [
        PayoutStatus(s["status"])
        for s in await db.fetch("SELECT status FROM payroll.payouts WHERE pay_run_id = $1", run_id)
    ]
    stored = await db.fetchval("SELECT status FROM payroll.pay_runs WHERE id = $1", run_id)
    assert stored == run_status_from_payouts(statuses)


async def _one(api: Api) -> str:
    run = await api.approved_run(1)
    [p] = await api.payouts(run["id"])
    return str(p["id"])


@pytest.mark.parametrize(
    ("body", "attempt", "payout", "run"),
    [
        (SETTLED, "settled", "paid", "paid"),
        (REJECTED, "rejected", "failed", "partially_failed"),
        (INDET, "indeterminate", "requested", "processing"),
        (POISON, "needs_operator", "requested", "processing"),
    ],
)
async def test_each_outcome(
    api: Api, db: Any, *, body: dict[str, Any], attempt: str, payout: str, run: str
) -> None:
    """PAY-405, PAY-406: each outcome → attempt, payout and run state; the response is exactly
    {payout_id, attempt, outcome} with outcome = the stored attempt status."""
    pid = await _one(api)
    r = await api.result(pid, body)
    assert r.status_code == 200, r.text
    assert r.json() == {"payout_id": pid, "attempt": 1, "outcome": attempt}
    s = await _state(db, pid)
    assert (s["attempt"], s["payout"], s["run"]) == (attempt, payout, run)
    assert (s["final_at"] is not None) == (attempt in ("settled", "rejected"))
    assert (s["payout_final"] is not None) == (payout != "requested")
    assert (s["needs_operator_at"] is not None) == (attempt == "needs_operator")
    assert (s["first_final_at"] is not None) == (run != "processing")
    if attempt == "settled":
        assert s["bank_reference"] == "bank-ref-1"
    if attempt in ("rejected", "needs_operator"):
        assert s["failure_reason"] == body["reason"]
    await _assert_run_rule(db, s["run_id"])
    events = {r["event"] for r in await db.fetch("SELECT event FROM payroll.audit_events")}
    assert "payout.result_recorded" in events
    assert ("payout.needs_operator" in events) == (attempt == "needs_operator")


async def test_final_attempt_is_stored_and_unchanged(api: Api, db: Any) -> None:
    """PAY-405: after settled, any outcome → 200 with the stored status and no change."""
    pid = await _one(api)
    await api.result(pid, SETTLED)
    before = await _state(db, pid)
    for body in (REJECTED, INDET, POISON, SETTLED):
        r = await api.result(pid, body)
        assert r.json()["outcome"] == "settled"
    assert await _state(db, pid) == before


async def test_settled_after_indeterminate_applies(api: Api, db: Any) -> None:
    """PAY-405: indeterminate is never final; a later settled applies."""
    pid = await _one(api)
    assert (await api.result(pid, INDET)).json()["outcome"] == "indeterminate"
    r = await api.client.get(
        f"/v1/pay-runs/{(await _state(db, pid))['run_id']}", headers=api.approver()
    )
    assert r.json()["counts"]["awaiting_bank"] == 1
    assert (await api.result(pid, SETTLED)).json()["outcome"] == "settled"
    assert (await _state(db, pid))["run"] == "paid"


async def test_needs_operator_rules(api: Api, db: Any) -> None:
    """PAY-405, R7, C48: on needs_operator, indeterminate/poison are 200 unchanged (re-publishing
    stops); a definite rejected applies and clears needs_operator_at."""
    pid = await _one(api)
    await api.result(pid, {"attempt": 1, "outcome": "poison"})
    s = await _state(db, pid)
    assert (s["attempt"], s["failure_reason"]) == ("needs_operator", "poison")
    run = (await api.client.get(f"/v1/pay-runs/{s['run_id']}", headers=api.approver())).json()
    assert run["counts"]["blocked"] == 1
    for body in (INDET, POISON):
        assert (await api.result(pid, body)).json()["outcome"] == "needs_operator"
    assert await _state(db, pid) == s
    assert (await api.result(pid, REJECTED)).json()["outcome"] == "rejected"
    after = await _state(db, pid)
    assert after["needs_operator_at"] is None
    assert after["payout"] == "failed"


async def test_needs_operator_then_settled_applies(api: Api, db: Any) -> None:
    """PAY-405, R7: the bank's settled answer wins over needs_operator."""
    pid = await _one(api)
    await api.result(pid, POISON)
    assert (await api.result(pid, SETTLED)).json()["outcome"] == "settled"
    assert (await _state(db, pid))["payout"] == "paid"


async def test_stale_attempt_is_409(api: Api, db: Any) -> None:
    """PAY-405: attempt != current_attempt → 409 attempt_not_current with current_attempt."""
    pid = await _one(api)
    r = await api.result(pid, {"attempt": 2, "outcome": "indeterminate"})
    assert r.json()["error"] == {
        **r.json()["error"],
        "code": "attempt_not_current",
        "details": {"current_attempt": 1},
    }
    stored = "SELECT count(*) FROM payroll.idempotency_keys WHERE scope LIKE '%/result|%'"
    assert await db.fetchval(stored) == 0  # errors are never stored


async def test_bank_reference_too_long_and_other_company(api: Api, db: Any) -> None:
    """PAY-405, C26: a 65-character bank_reference → 422; another cid → 404 payout_not_found."""
    pid = await _one(api)
    r = await api.result(pid, {**SETTLED, "bank_reference": "r" * 65})
    assert (r.status_code, r.json()["error"]["code"]) == (422, "validation_error")
    other = await other_company(db)
    h = {**api.worker(cid=other), "Idempotency-Key": "e:settled"}
    r = await api.client.post(f"/v1/internal/payouts/{pid}/result", json=SETTLED, headers=h)
    assert (r.status_code, r.json()["error"]["code"]) == (404, "payout_not_found")


async def test_run_status_through_mixed_results(api: Api, db: Any) -> None:
    """PAY-406: the stored run status equals the function after every call; processing until
    the last payout is final, then partially_failed with first_final_at set once."""
    run = await api.approved_run(3)
    payouts = await api.payouts(run["id"])
    bodies = [SETTLED, REJECTED, INDET, SETTLED]
    targets = [payouts[0], payouts[1], payouts[2], payouts[2]]
    for p, body in zip(targets, bodies, strict=True):
        assert (await api.result(p["id"], body)).status_code == 200
        await _assert_run_rule(db, run["id"])
    row = await db.fetchrow("SELECT * FROM payroll.pay_runs WHERE id = $1", run["id"])
    assert row["status"] == "partially_failed"
    assert row["first_final_at"] is not None
    detail = json.loads(
        await db.fetchval(
            "SELECT details FROM payroll.audit_events WHERE event = 'payout.result_recorded' "
            "ORDER BY id LIMIT 1"
        )
    )
    assert detail == {"attempt": 1, "outcome": "settled", "applied": True}


async def test_metrics_and_settlement_observed_once(
    otel_memory: OtelMemory, api_factory: ApiFactory
) -> None:
    """PAY-406, PAY-NFR-06, PAY-NFR-10: payouts_total by outcome; the settlement histogram is
    observed once per run; pay_runs_total counts approved and the final status."""
    api = await api_factory()
    run = await api.approved_run(2)
    p1, p2 = await api.payouts(run["id"])
    await api.result(p1["id"], INDET)
    await api.result(p1["id"], SETTLED)
    await api.result(p2["id"], SETTLED)
    await api.result(p2["id"], SETTLED)  # stored: no new observation
    hist = otel_memory.metric_points("payroll_payout_settlement_seconds")
    assert [p.count for _, p in hist] == [1]
    outcomes = {a["outcome"]: v for a, v in otel_memory.metric_points("payroll_payouts")}
    assert outcomes == {"indeterminate": 1, "paid": 2}
    runs = {a["outcome"]: v for a, v in otel_memory.metric_points("payroll_pay_runs")}
    assert runs == {"calculated": 1, "approved": 1, "paid": 1}
