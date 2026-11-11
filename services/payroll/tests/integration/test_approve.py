"""Approve (design §4.4 "Approve"; PAY-400, PAY-401, PAY-402, PAY-406)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from payroll.crypto import snapshot_aad
from payroll.events.envelope import validate_envelope

from tests.helpers import VALID_ROUTING
from tests.integration.conftest import APPROVER, PAYROLL_ADMIN, Api

pytestmark = pytest.mark.integration


async def test_approve_creates_payouts_attempts_snapshots_and_outbox(api: Api, db: Any) -> None:
    """PAY-400, PAY-402: one payout per payslip with attempt 1 (encrypted bank snapshot), one
    outbox row per transfer, status processing with processing_since; approved_by_name stored."""
    run = await api.calculated_run(2, bank_account="000111222333")
    r = await api.approve(run["id"])
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "processing"
    assert (out["approved_by"], out["approved_by_name"]) == (APPROVER, "Grace Approver")
    assert out["processing_since"] is not None
    assert out["approved_at"] is not None
    assert out["counts"] == {
        "requested": 2, "awaiting_bank": 0, "blocked": 0, "paid": 0, "no_transfer": 0, "failed": 0,
    }  # fmt: skip
    payouts = await db.fetch("SELECT * FROM payroll.payouts WHERE pay_run_id = $1", run["id"])
    assert len(payouts) == 2
    for p in payouts:
        assert (p["status"], p["current_attempt"], p["no_transfer"]) == ("requested", 1, False)
        a = await db.fetchrow("SELECT * FROM payroll.payout_attempts WHERE payout_id = $1", p["id"])
        assert (a["attempt"], a["status"], a["publish_count"]) == (1, "requested", 1)
        assert a["account_last4"] == "2333"
        assert a["requested_by"] == APPROVER
        assert a["needs_operator_at"] is None
        assert a["final_at"] is None
        snap = json.loads(
            api.rt.keyring.decrypt(a["bank_snapshot_enc"], aad=snapshot_aad(p["id"], 1))
        )
        assert snap == {"routing": VALID_ROUTING, "account": "000111222333"}
        [o] = await db.fetch("SELECT * FROM payroll.outbox WHERE payout_id = $1", p["id"])
        payload = json.loads(o["payload"])
        validate_envelope(payload)
        assert str(o["id"]) == payload["event_id"]
        assert payload["data"] == {
            "payout_id": str(p["id"]), "pay_run_id": run["id"], "company_id": str(api.cid),
            "attempt": 1, "amount_cents": p["amount_cents"], "currency": "USD", "republish_seq": 0,
        }  # fmt: skip
        assert o["sent_at"] is None
        assert o["attempt"] == 1
    audit = await db.fetchrow("SELECT * FROM payroll.audit_events WHERE event = 'payrun.approved'")
    assert json.loads(audit["details"]) == {
        "payouts": 2, "transfers": 2, "no_transfer": 0, "status": "processing",
    }  # fmt: skip
    r = await api.client.get(f"/v1/pay-runs/{run['id']}", headers=api.approver())
    assert r.json()["approved_by_name"] == "Grace Approver"


async def test_second_approve_with_a_new_key_is_409(api: Api) -> None:
    """PAY-400: a second approve with a new key → 409 pay_run_not_approvable."""
    run = await api.approved_run(1)
    r = await api.approve(run["id"])
    assert r.json()["error"] == {
        **r.json()["error"],
        "code": "pay_run_not_approvable",
        "details": {"status": "processing"},
    }


async def test_approving_a_draft_is_409(api: Api) -> None:
    """PAY-400: only calculated runs approve."""
    await api.create_employee()
    run = await api.create_run()
    r = await api.approve(run["id"])
    assert (r.status_code, r.json()["error"]["code"]) == (409, "pay_run_not_approvable")


async def test_zero_net_payout_is_paid_no_transfer(api: Api, db: Any) -> None:
    """PAY-400: net = 0 → payout paid + no_transfer, attempt 0, no outbox row; the run stays
    processing while another payout is requested."""
    await api.create_employee(name="Zero", email="z@example.com", annual_salary_cents=1)
    await api.create_employee(name="Real", email="r@example.com")
    run = await api.create_run("monthly", "2026-01-01")  # k=1: 1 cent/year pays 0 in January
    await api.calculate(run["id"])
    out = (await api.approve(run["id"])).json()
    assert out["status"] == "processing"
    assert (out["counts"]["paid"], out["counts"]["no_transfer"], out["counts"]["requested"]) == (
        1,
        1,
        1,
    )
    zero = await db.fetchrow("SELECT * FROM payroll.payouts WHERE amount_cents = 0")
    assert (zero["status"], zero["no_transfer"], zero["current_attempt"]) == ("paid", True, 0)
    assert zero["final_at"] is not None
    assert await db.fetchval("SELECT count(*) FROM payroll.outbox") == 1
    assert (
        await db.fetchval(
            "SELECT count(*) FROM payroll.payout_attempts WHERE payout_id = $1", zero["id"]
        )
        == 0
    )
    items = await api.payouts(run["id"])
    assert [p["attempts"] for p in items if p["no_transfer"]] == [[]]


async def test_all_zero_run_is_paid_at_once(api: Api, db: Any) -> None:
    """PAY-400, PAY-406: every payout no_transfer → the run is paid straight away."""
    await api.create_employee(annual_salary_cents=1)
    run = await api.create_run("monthly", "2026-01-01")
    await api.calculate(run["id"])
    out = (await api.approve(run["id"])).json()
    assert out["status"] == "paid"
    assert out["processing_since"] is None
    row = await db.fetchrow("SELECT first_final_at FROM payroll.pay_runs WHERE id = $1", run["id"])
    assert row["first_final_at"] is not None
    assert await db.fetchval("SELECT count(*) FROM payroll.outbox") == 0


async def test_dual_approval_calculator_cannot_approve(api: Api) -> None:
    """PAY-401: with the snapshot on, the calculator gets 403 approver_must_differ."""
    run = await api.calculated_run(1)
    both = api.headers(PAYROLL_ADMIN, ("payroll_admin", "approver"))
    r = await api.approve(run["id"], headers=both)
    assert r.json()["error"] == {
        **r.json()["error"],
        "code": "approver_must_differ",
        "details": {"rule": "calculator", "employee_ids": []},
    }


async def test_dual_approval_unvetted_bank_editor(api: Api, db: Any) -> None:
    """PAY-401, C73: the approver's bank edit that no second person has released (unvetted)
    → 403 bank_editor (IDs)."""
    run = await api.calculated_run(2)
    emp = await db.fetchval("SELECT employee_id FROM payroll.payslips ORDER BY id LIMIT 1")
    await db.execute(
        "UPDATE payroll.employees SET bank_updated_by = $1, "
        "bank_updated_at = now() + interval '1s' WHERE id = $2",
        APPROVER, emp,
    )  # fmt: skip
    r = await api.approve(run["id"])
    assert r.json()["error"]["details"] == {"rule": "bank_editor", "employee_ids": [str(emp)]}


async def test_snapshot_wins_over_the_current_setting(api: Api) -> None:
    """PAY-401: turning dual approval off after calculate keeps the run's snapshot (still 403);
    a run calculated with it off lets the calculator approve."""
    run = await api.calculated_run(1)
    settings = {
        "name": "Acme", "tax_rate_bps": 0, "retirement_bps": 0, "timezone": "America/New_York",
        "biweekly_anchor": "2026-01-05", "require_dual_approval": False,
    }  # fmt: skip
    assert (
        await api.client.put("/v1/company/settings", json=settings, headers=api.admin())
    ).status_code == 200
    both = api.headers(PAYROLL_ADMIN, ("payroll_admin", "approver"))
    assert (await api.approve(run["id"], headers=both)).status_code == 403
    await api.calculate(run["id"])  # recalculating takes the new snapshot
    r = await api.approve(run["id"], headers=both)
    assert r.status_code == 200, r.text


async def test_missing_bank_details_is_409(api: Api, db: Any) -> None:
    """PAY-400, C40: an employee in the run with purged bank details → 409 with the IDs."""
    run = await api.calculated_run(2)
    emp = await db.fetchval("SELECT employee_id FROM payroll.payslips ORDER BY id LIMIT 1")
    await db.execute(
        "UPDATE payroll.employees SET status = 'inactive', deactivated_at = now(), "
        "bank_routing_enc = NULL, bank_account_enc = NULL, account_last4 = NULL, "
        "bank_purged_at = now() WHERE id = $1",
        emp,
    )
    r = await api.approve(run["id"])
    assert r.json()["error"] == {
        **r.json()["error"],
        "code": "employee_bank_details_missing",
        "details": {"employee_ids": [str(emp)]},
    }
    assert await db.fetchval("SELECT count(*) FROM payroll.payouts") == 0


async def test_tampered_bank_ciphertext_is_500_and_rolls_back(api: Api, db: Any) -> None:
    """PAY-204: a ciphertext that fails to decrypt → 500 internal_error; nothing is written."""
    run = await api.calculated_run(1)
    await db.execute(
        "UPDATE payroll.employees SET bank_account_enc = regexp_replace(bank_account_enc, "
        "':[^:]*$', ':AAAAAAAAAAAAAAAAAAAAAAAA')"
    )
    r = await api.approve(run["id"])
    assert (r.status_code, r.json()["error"]["code"]) == (500, "internal_error")
    assert await db.fetchval("SELECT count(*) FROM payroll.payouts") == 0
    assert await db.fetchval("SELECT status FROM payroll.pay_runs") == "calculated"
