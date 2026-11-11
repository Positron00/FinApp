"""Hours, calculate, delete and the run reads (design §4.4, §9.2; PAY-301, PAY-302, PAY-303,
PAY-304, PAY-401), plus the end-to-end hourly path: create → hours → calculate → approve → paid.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import uuid4

import httpx
import pytest
from payroll.domain.calc import apply_bps, hourly_gross, salaried_gross
from pycommon.testing import JsonLogCapture

from tests.integration.conftest import PAYROLL_ADMIN, Api, other_company

pytestmark = pytest.mark.integration

COUNT_KEYS = {"requested", "awaiting_bank", "blocked", "paid", "no_transfer", "failed"}
RUN_KEYS = {
    "id", "schedule", "period_start", "period_end", "pay_date", "period_index",
    "periods_per_year", "status", "employee_count", "tax_bps", "retirement_bps",
    "require_dual_approval", "totals", "counts", "created_by", "created_at", "calculated_by",
    "calculated_by_name", "calculated_at", "approved_by", "approved_by_name", "approved_at",
    "processing_since", "updated_at",
}  # fmt: skip
CALC_FIELDS = (
    "calculated_by", "calculated_by_name", "calculated_at", "tax_bps", "retirement_bps",
    "require_dual_approval", "totals",
)  # fmt: skip
SETTLED = {"attempt": 1, "outcome": "settled", "bank_reference": "BR-1"}


async def _set_rates(api: Api, tax: int = 2000, retirement: int = 500, dual: bool = True) -> None:
    r = await api.client.put(
        "/v1/company/settings",
        json={
            "name": "Acme",
            "tax_rate_bps": tax,
            "retirement_bps": retirement,
            "timezone": "America/New_York",
            "biweekly_anchor": "2026-01-05",
            "require_dual_approval": dual,
        },
        headers=api.admin(),
    )
    assert r.status_code == 200, r.text


async def _hourly(api: Api, name: str, rate: int, schedule: str = "monthly") -> str:
    emp = await api.create_employee(
        name=name, email=f"{name.lower().replace(' ', '.')}@example.com", pay_type="hourly",
        annual_salary_cents=None, hourly_rate_cents=rate, schedule=schedule,
    )  # fmt: skip
    return str(emp["id"])


async def _put_hours(
    api: Api, run_id: str, entries: dict[str, int], headers: dict[str, str] | None = None
) -> httpx.Response:
    body = {"entries": [{"employee_id": e, "hours_hundredths": h} for e, h in entries.items()]}
    return await api.client.put(
        f"/v1/pay-runs/{run_id}/hours", json=body, headers=headers or api.payroll_admin()
    )


def _error(r: httpx.Response) -> tuple[int, str, Any]:
    err = r.json()["error"]
    return r.status_code, err["code"], err.get("details")


async def _get_run(api: Api, run_id: str) -> dict[str, Any]:
    r = await api.client.get(f"/v1/pay-runs/{run_id}", headers=api.approver())
    assert r.status_code == 200, r.text
    out: dict[str, Any] = r.json()
    return out


async def _payslips(api: Api, run_id: str) -> list[dict[str, Any]]:
    r = await api.client.get(f"/v1/pay-runs/{run_id}/payslips?limit=100", headers=api.admin())
    assert r.status_code == 200, r.text
    items: list[dict[str, Any]] = r.json()["items"]
    return items


# --- hours (PAY-301) ---------------------------------------------------------------------------


async def test_hours_upsert_returns_hours_out(
    api: Api, db: Any, caplog_json: JsonLogCapture
) -> None:
    """PAY-301, C7: PUT …/hours upserts per employee and returns HoursOut, i.e. every hours row
    stored for the run ordered by employee_id; the run stays draft; audit and log
    payrun.hours_updated carry counts only."""
    a = await _hourly(api, "Hal One", 1_999)
    b = await _hourly(api, "Hal Two", 2_500)
    await api.create_employee(name="Sal", email="sal@example.com")
    run = await api.create_run()
    r = await _put_hours(api, run["id"], {a: 8_000})
    assert r.status_code == 200, r.text
    assert r.json() == {
        "pay_run_id": run["id"],
        "status": "draft",
        "entries": [{"employee_id": a, "hours_hundredths": 8_000}],
    }
    r = await _put_hours(api, run["id"], {b: 200, a: 150})
    assert r.status_code == 200, r.text
    assert r.json()["entries"] == sorted(
        [{"employee_id": a, "hours_hundredths": 150}, {"employee_id": b, "hours_hundredths": 200}],
        key=lambda e: str(e["employee_id"]),
    )
    rows = await db.fetch(
        "SELECT employee_id, hours_hundredths, entered_by FROM payroll.pay_run_hours "
        "WHERE pay_run_id = $1 ORDER BY employee_id",
        run["id"],
    )
    assert len(rows) == 2
    assert {row["entered_by"] for row in rows} == {PAYROLL_ADMIN}
    assert (await _get_run(api, run["id"]))["status"] == "draft"
    audits = await db.fetch(
        "SELECT details FROM payroll.audit_events WHERE event = 'payrun.hours_updated' ORDER BY id"
    )
    assert [json.loads(x["details"]) for x in audits] == [
        {"entries": 1, "reverted_to_draft": False},
        {"entries": 2, "reverted_to_draft": False},
    ]
    events = caplog_json.events("payrun.hours_updated")
    assert [(e["pay_run_id"], e["entries"], e["reverted_to_draft"]) for e in events] == [
        (run["id"], 1, False),
        (run["id"], 2, False),
    ]


async def test_hours_not_hourly_and_not_in_run(api: Api, db: Any) -> None:
    """PAY-301: a salaried payslip → 422 not_hourly; an employee without a payslip in the run
    → 422 employee_not_in_run (checked first); both list details.employee_ids; nothing is
    stored when any entry is refused."""
    hourly = await _hourly(api, "Hal", 1_999)
    salaried = str((await api.create_employee(name="Sal", email="sal@example.com"))["id"])
    biweekly = await _hourly(api, "Bia", 1_000, schedule="biweekly")  # in no monthly run
    run = await api.create_run()
    r = await _put_hours(api, run["id"], {hourly: 100, salaried: 100})
    assert _error(r) == (422, "not_hourly", {"employee_ids": [salaried]})
    stranger = str(uuid4())
    r = await _put_hours(api, run["id"], {hourly: 100, stranger: 1, biweekly: 1, salaried: 1})
    assert _error(r) == (
        422, "employee_not_in_run", {"employee_ids": sorted([stranger, biweekly])},
    )  # fmt: skip
    assert await db.fetchval("SELECT count(*) FROM payroll.pay_run_hours") == 0


BOUNDS = [
    # (schedule, period_start, the bound, the first value over it, its issue code)
    ("biweekly", "2026-01-05", 33_600, 33_601, "out_of_range"),
    ("monthly", "2026-01-01", 74_400, 74_401, "less_than_equal"),
]


@pytest.mark.parametrize("case", BOUNDS, ids=[c[0] for c in BOUNDS])
async def test_hours_bounds_per_schedule(
    api: Api, db: Any, case: tuple[str, str, int, int, str]
) -> None:
    """PAY-301, C14: 0 to 33,600 biweekly and 0 to 74,400 monthly; out of bounds (or negative)
    → 422 validation_error at body.entries.<i>.hours_hundredths, and nothing is stored."""
    schedule, start, ok, over, issue = case
    a = await _hourly(api, "Hal One", 1_999, schedule)
    b = await _hourly(api, "Hal Two", 1_999, schedule)
    run = await api.create_run(schedule, start)
    r = await _put_hours(api, run["id"], {a: ok, b: 0})
    assert r.status_code == 200, r.text
    r = await _put_hours(api, run["id"], {a: 1, b: over})
    assert _error(r) == (
        422, "validation_error", [{"field": "body.entries.1.hours_hundredths", "issue": issue}],
    )  # fmt: skip
    r = await _put_hours(api, run["id"], {a: -1})
    assert _error(r)[:2] == (422, "validation_error")
    stored = await db.fetch(
        "SELECT hours_hundredths FROM payroll.pay_run_hours ORDER BY hours_hundredths"
    )
    assert [x["hours_hundredths"] for x in stored] == [0, ok]


async def test_hours_body_rules(api: Api) -> None:
    """PAY-301, PAY-602: duplicate employee IDs, an empty list and a missing body are 422
    validation_error with the field's location."""
    a = await _hourly(api, "Hal", 1_999)
    run = await api.create_run()
    url = f"/v1/pay-runs/{run['id']}/hours"
    dup = {"entries": [{"employee_id": a, "hours_hundredths": 1}] * 2}
    r = await api.client.put(url, json=dup, headers=api.payroll_admin())
    assert _error(r) == (
        422, "validation_error", [{"field": "body.entries", "issue": "duplicate_employee_id"}],
    )  # fmt: skip
    r = await api.client.put(url, json={"entries": []}, headers=api.payroll_admin())
    assert _error(r) == (422, "validation_error", [{"field": "body.entries", "issue": "too_short"}])
    r = await api.client.put(url, headers=api.payroll_admin())
    assert _error(r)[:2] == (422, "validation_error")


async def test_hours_revert_a_calculated_run_to_draft(api: Api, db: Any) -> None:
    """PAY-301: hours on a calculated run move it back to draft: calculated_by and
    calculated_by_name (and every calculate snapshot) are cleared and the payslips reset to
    pending; it can't be approved until recalculated, which uses the new hours."""
    await _set_rates(api)
    a = await _hourly(api, "Hal", 1_999)
    await api.create_employee(name="Sal", email="sal@example.com")
    run = await api.create_run()
    assert (await _put_hours(api, run["id"], {a: 150})).status_code == 200
    calculated = await api.calculate(run["id"], name="Carl Calculator")
    assert calculated["calculated_by_name"] == "Carl Calculator"
    r = await _put_hours(api, run["id"], {a: 300})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "draft"
    out = await _get_run(api, run["id"])
    assert out["status"] == "draft"
    assert {k: out[k] for k in CALC_FIELDS} == dict.fromkeys(CALC_FIELDS)
    for slip in await _payslips(api, run["id"]):
        assert slip["status"] == "pending"
        assert (slip["hours_hundredths"], slip["gross_cents"], slip["net_cents"]) == (
            None, None, None,
        )  # fmt: skip
        assert (slip["tax_cents"], slip["retirement_cents"]) == (None, None)
    audit = await db.fetchval(
        "SELECT details FROM payroll.audit_events WHERE event = 'payrun.hours_updated' "
        "ORDER BY id DESC LIMIT 1"
    )
    assert json.loads(audit) == {"entries": 1, "reverted_to_draft": True}
    r = await api.approve(run["id"])
    assert _error(r) == (409, "pay_run_not_approvable", {"status": "draft"})
    again = await api.calculate(run["id"])
    slip = next(s for s in await _payslips(api, run["id"]) if s["employee_id"] == a)
    assert (slip["hours_hundredths"], slip["gross_cents"]) == (300, hourly_gross(1_999, 300))
    assert again["calculated_by_name"] is None  # no name claim this time (R9)


async def test_hours_locked_once_processing_or_final(api: Api) -> None:
    """PAY-301: 409 pay_run_locked (details.status) once the run is processing, and once final;
    the lock is checked before the entries."""
    a = await _hourly(api, "Hal", 1_999)
    run = await api.create_run()
    assert (await _put_hours(api, run["id"], {a: 100})).status_code == 200
    await api.calculate(run["id"])
    assert (await api.approve(run["id"])).status_code == 200
    r = await _put_hours(api, run["id"], {a: 200})
    assert _error(r) == (409, "pay_run_locked", {"status": "processing"})
    [payout] = await api.payouts(run["id"])
    assert (await api.result(payout["id"], SETTLED)).status_code == 200
    r = await _put_hours(api, run["id"], {str(uuid4()): 200})
    assert _error(r) == (409, "pay_run_locked", {"status": "paid"})


async def test_hours_racing_approve_is_consistent(api: Api, db: Any) -> None:
    """PAY-301, PAY-400: hours and approve both lock the run first (§4.10), so of a parallel
    hours PUT and approve exactly one applies: either the run reverts to draft with no payouts,
    or it is processing and the hours are refused as locked."""
    a = await _hourly(api, "Hal", 1_999)
    run = await api.create_run()
    assert (await _put_hours(api, run["id"], {a: 100})).status_code == 200
    await api.calculate(run["id"])
    hours, approve = await asyncio.gather(
        _put_hours(api, run["id"], {a: 200}), api.approve(run["id"])
    )
    payouts = await db.fetchval("SELECT count(*) FROM payroll.payouts")
    status = (await _get_run(api, run["id"]))["status"]
    if hours.status_code == 200:
        assert _error(approve) == (409, "pay_run_not_approvable", {"status": "draft"})
        assert (status, payouts) == ("draft", 0)
    else:
        assert approve.status_code == 200
        assert _error(hours) == (409, "pay_run_locked", {"status": "processing"})
        assert (status, payouts) == ("processing", 1)


# --- calculate (PAY-302) -----------------------------------------------------------------------


async def test_calculate_amounts_totals_and_snapshot(api: Api, db: Any) -> None:
    """PAY-302, PAY-303: integer formulas; totals = Σ stored payslips; rates and dual flag
    snapshotted; calculated_by_name from the token's name claim."""
    await _set_rates(api)
    for i, salary in enumerate((5_200_001, 1_234_567, 100)):
        await api.create_employee(
            name=f"E{i}", email=f"e{i}@example.com", annual_salary_cents=salary
        )
    run = await api.create_run("monthly", "2026-12-01")
    out = await api.calculate(run["id"], name="Carl Calculator")
    assert out["status"] == "calculated"
    assert (out["tax_bps"], out["retirement_bps"], out["require_dual_approval"]) == (
        2000,
        500,
        True,
    )
    assert (out["calculated_by"], out["calculated_by_name"]) == (PAYROLL_ADMIN, "Carl Calculator")
    assert out["calculated_at"] is not None
    rows = await db.fetch("SELECT * FROM payroll.payslips WHERE pay_run_id = $1", run["id"])
    for row in rows:
        gross = salaried_gross(row["annual_salary_cents"], 12, 12)
        assert row["gross_cents"] == gross
        assert row["tax_cents"] == apply_bps(gross, 2000)
        assert row["retirement_cents"] == apply_bps(gross, 500)
        assert row["gross_cents"] == row["net_cents"] + row["tax_cents"] + row["retirement_cents"]
        assert row["status"] == "calculated"
    totals = out["totals"]
    assert totals["gross_cents"] == sum(r["gross_cents"] for r in rows)
    assert totals["net_cents"] == sum(r["net_cents"] for r in rows)
    assert totals["funding_required_cents"] == totals["net_cents"]
    assert set(out["counts"]) == COUNT_KEYS
    assert out["approved_by_name"] is None
    audit = await db.fetchval(
        "SELECT details FROM payroll.audit_events WHERE event = 'payrun.calculated'"
    )
    assert json.loads(audit) == {
        "tax_bps": 2000, "retirement_bps": 500, "require_dual_approval": True, "payslips": 3,
    }  # fmt: skip
    # Recalculating after a rate change recomputes; without a name claim the name is NULL.
    await _set_rates(api, tax=1000)
    again = await api.calculate(run["id"])
    assert again["tax_bps"] == 1000
    assert again["calculated_by_name"] is None


async def test_hours_missing_lists_every_hourly_employee_without_hours(api: Api) -> None:
    """PAY-302: hourly payslips without hours → 422 hours_missing with exactly those IDs."""
    await api.create_employee()
    a = await _hourly(api, "Hal One", 1_999)
    b = await _hourly(api, "Hal Two", 1_999)
    c = await _hourly(api, "Hal Three", 1_999)
    run = await api.create_run()
    url = f"/v1/pay-runs/{run['id']}/calculate"
    r = await api.client.post(url, headers=api.payroll_admin())
    assert _error(r) == (422, "hours_missing", {"employee_ids": sorted([a, b, c])})
    assert (await _put_hours(api, run["id"], {b: 0})).status_code == 200
    r = await api.client.post(url, headers=api.payroll_admin())
    assert _error(r) == (422, "hours_missing", {"employee_ids": sorted([a, c])})
    assert (await _get_run(api, run["id"]))["status"] == "draft"


async def test_calculate_after_approval_is_locked(api: Api) -> None:
    """PAY-302: calculate on a processing run → 409 pay_run_locked."""
    run = await api.approved_run(1)
    r = await api.client.post(f"/v1/pay-runs/{run['id']}/calculate", headers=api.payroll_admin())
    assert _error(r) == (409, "pay_run_locked", {"status": "processing"})


# --- end to end (PAY-301, PAY-302, PAY-303, PAY-400, PAY-406) ----------------------------------


async def test_hourly_end_to_end_create_hours_calculate_approve_paid(api: Api, db: Any) -> None:
    """PAY-301, PAY-302, PAY-303, PAY-400, PAY-NFR-01: a biweekly run with two hourly and one
    salaried employee goes create → hours → calculate → approve → paid. Hourly gross is
    (rate * hundredths + 50) // 100, salaried gross is period k of 26, tax and retirement round
    half up; each payslip has gross = net + tax + retirement, the run totals are the sums,
    funding = Σ net, and each payout carries its payslip's net."""
    await _set_rates(api, tax=2000, retirement=500)
    h1 = await _hourly(api, "Hal One", 1_999, "biweekly")
    h2 = await _hourly(api, "Hal Two", 2_537, "biweekly")
    sal = await api.create_employee(
        name="Sal", email="sal@example.com", annual_salary_cents=5_200_001, schedule="biweekly"
    )
    run = await api.create_run("biweekly", "2026-02-02")  # anchor 2026-01-05 + 28 days: k = 3
    assert (run["period_index"], run["periods_per_year"]) == (3, 26)
    r = await _put_hours(api, run["id"], {h1: 150, h2: 8_633})
    assert r.status_code == 200, r.text
    out = await api.calculate(run["id"], name="Carl Calculator")
    assert out["status"] == "calculated"

    expected_gross = {h1: 2_999, h2: 219_019, str(sal["id"]): 200_000}
    assert expected_gross[h1] == hourly_gross(1_999, 150)
    assert expected_gross[h2] == hourly_gross(2_537, 8_633)
    assert expected_gross[str(sal["id"])] == salaried_gross(5_200_001, 3, 26)
    slips = await _payslips(api, run["id"])
    assert len(slips) == 3
    for s in slips:
        gross = expected_gross[s["employee_id"]]
        assert s["status"] == "calculated"
        assert s["gross_cents"] == gross
        assert (s["tax_cents"], s["retirement_cents"]) == (
            apply_bps(gross, 2000), apply_bps(gross, 500),
        )  # fmt: skip
        assert s["gross_cents"] == s["net_cents"] + s["tax_cents"] + s["retirement_cents"]
        assert s["net_cents"] >= 0
    assert {s["employee_id"]: s["hours_hundredths"] for s in slips} == {
        h1: 150, h2: 8_633, str(sal["id"]): None,
    }  # fmt: skip
    h1_slip = next(s for s in slips if s["employee_id"] == h1)
    assert (h1_slip["tax_cents"], h1_slip["retirement_cents"], h1_slip["net_cents"]) == (
        600, 150, 2_249,
    )  # fmt: skip
    totals = out["totals"]
    for key in ("gross_cents", "tax_cents", "retirement_cents", "net_cents"):
        assert totals[key] == sum(s[key] for s in slips), key
    assert totals["funding_required_cents"] == totals["net_cents"]

    r = await api.approve(run["id"])
    assert r.status_code == 200, r.text
    approved = r.json()
    assert approved["status"] == "processing"
    assert approved["approved_by_name"] == "Grace Approver"
    assert approved["counts"] == {**dict.fromkeys(COUNT_KEYS, 0), "requested": 3}
    payouts = await api.payouts(run["id"])
    net_by_slip = {s["id"]: s["net_cents"] for s in slips}
    assert {p["payslip_id"]: p["amount_cents"] for p in payouts} == net_by_slip
    for p in payouts:
        assert (await api.result(p["id"], SETTLED)).status_code == 200
    done = await _get_run(api, run["id"])
    assert done["status"] == "paid"
    assert done["counts"] == {**dict.fromkeys(COUNT_KEYS, 0), "paid": 3}
    assert done["totals"] == totals
    assert _error(await _put_hours(api, run["id"], {h1: 1}))[:2] == (409, "pay_run_locked")
    r = await api.client.delete(f"/v1/pay-runs/{run['id']}", headers=api.payroll_admin())
    assert _error(r) == (409, "pay_run_locked", {"status": "paid"})


async def test_dual_approval_follows_the_latest_calculation(api: Api) -> None:
    """PAY-401, PAY-301: the calculator rule uses the run's current calculated_by. A user who
    calculated can't approve; once hours revert the run and another user recalculates, the
    first user may approve and the new calculator may not."""
    first = str(uuid4())
    second = str(uuid4())
    both = ("payroll_admin", "approver")
    a = await _hourly(api, "Hal", 1_999)
    run = await api.create_run()
    assert (await _put_hours(api, run["id"], {a: 100})).status_code == 200
    r = await api.client.post(
        f"/v1/pay-runs/{run['id']}/calculate", headers=api.headers(first, both)
    )
    assert r.json()["calculated_by"] == first
    r = await api.approve(run["id"], headers=api.headers(first, both))
    assert _error(r) == (403, "approver_must_differ", {"rule": "calculator", "employee_ids": []})
    r = await _put_hours(api, run["id"], {a: 200}, headers=api.headers(second, both))
    assert r.status_code == 200
    assert (await _get_run(api, run["id"]))["calculated_by"] is None
    r = await api.client.post(
        f"/v1/pay-runs/{run['id']}/calculate", headers=api.headers(second, both)
    )
    assert r.json()["calculated_by"] == second
    r = await api.approve(run["id"], headers=api.headers(second, both))
    assert _error(r)[:2] == (403, "approver_must_differ")
    r = await api.approve(run["id"], headers=api.headers(first, both))
    assert r.status_code == 200, r.text
    assert r.json()["approved_by"] == first


# --- delete (PAY-304) --------------------------------------------------------------------------


async def test_delete_draft_and_calculated_runs_frees_the_period(
    api: Api, db: Any, caplog_json: JsonLogCapture
) -> None:
    """PAY-304: DELETE soft-deletes a draft or calculated run (204, deleted_at and deleted_by
    set, payslips and hours kept, audit and log payrun.deleted {status_at_delete}); later reads
    and mutations are 404 pay_run_not_found, the list hides it, and the period is free again."""
    a = await _hourly(api, "Hal", 1_999)
    run = await api.create_run()
    assert (await _put_hours(api, run["id"], {a: 100})).status_code == 200
    r = await api.client.delete(f"/v1/pay-runs/{run['id']}", headers=api.payroll_admin())
    assert (r.status_code, r.content) == (204, b"")
    row = await db.fetchrow("SELECT * FROM payroll.pay_runs WHERE id = $1", run["id"])
    assert (row["status"], row["deleted_by"]) == ("draft", PAYROLL_ADMIN)
    assert row["deleted_at"] is not None
    assert (
        await db.fetchval("SELECT count(*) FROM payroll.payslips WHERE pay_run_id = $1", run["id"])
        == 1
    )
    assert await db.fetchval("SELECT count(*) FROM payroll.pay_run_hours") == 1
    base = f"/v1/pay-runs/{run['id']}"
    for method, path in (
        ("GET", ""), ("GET", "/payslips"), ("GET", "/payouts"), ("DELETE", ""),
        ("POST", "/calculate"),
    ):  # fmt: skip
        r = await api.client.request(method, base + path, headers=api.payroll_admin())
        assert _error(r)[:2] == (404, "pay_run_not_found"), (method, path)
    assert _error(await _put_hours(api, run["id"], {a: 1}))[:2] == (404, "pay_run_not_found")
    listed = (await api.client.get("/v1/pay-runs", headers=api.approver())).json()["items"]
    assert run["id"] not in [x["id"] for x in listed]

    again = await api.create_run()  # the same period is free again
    assert again["id"] != run["id"]
    assert (await _put_hours(api, again["id"], {a: 100})).status_code == 200
    await api.calculate(again["id"])
    r = await api.client.delete(f"/v1/pay-runs/{again['id']}", headers=api.payroll_admin())
    assert r.status_code == 204
    audits = await db.fetch(
        "SELECT entity_id, actor_id, details FROM payroll.audit_events "
        "WHERE event = 'payrun.deleted' ORDER BY id"
    )
    assert [(str(x["entity_id"]), x["actor_id"], json.loads(x["details"])) for x in audits] == [
        (run["id"], PAYROLL_ADMIN, {"status_at_delete": "draft"}),
        (again["id"], PAYROLL_ADMIN, {"status_at_delete": "calculated"}),
    ]
    events = caplog_json.events("payrun.deleted")
    assert [(e["pay_run_id"], e["status_at_delete"], e["actor_id"]) for e in events] == [
        (run["id"], "draft", PAYROLL_ADMIN),
        (again["id"], "calculated", PAYROLL_ADMIN),
    ]
    assert (await api.create_run())["status"] == "draft"


async def test_delete_locked_once_processing_or_final(api: Api, db: Any) -> None:
    """PAY-304: 409 pay_run_locked (details.status) for a processing or final run; the run is
    untouched and no audit row is written."""
    run = await api.approved_run(1)
    url = f"/v1/pay-runs/{run['id']}"
    r = await api.client.delete(url, headers=api.payroll_admin())
    assert _error(r) == (409, "pay_run_locked", {"status": "processing"})
    [payout] = await api.payouts(run["id"])
    assert (await api.result(payout["id"], SETTLED)).status_code == 200
    r = await api.client.delete(url, headers=api.payroll_admin())
    assert _error(r) == (409, "pay_run_locked", {"status": "paid"})
    assert (await _get_run(api, run["id"]))["status"] == "paid"
    assert (
        await db.fetchval("SELECT count(*) FROM payroll.pay_runs WHERE deleted_at IS NOT NULL") == 0
    )
    assert (
        await db.fetchval(
            "SELECT count(*) FROM payroll.audit_events WHERE event = 'payrun.deleted'"
        )
        == 0
    )


async def test_delete_racing_approve_is_consistent(api: Api, db: Any) -> None:
    """PAY-304, PAY-400: delete and approve both lock the run first (§4.10): either the run is
    deleted and approve is 404 with no payouts, or it is processing and delete is 409."""
    run = await api.calculated_run(1)
    delete, approve = await asyncio.gather(
        api.client.delete(f"/v1/pay-runs/{run['id']}", headers=api.payroll_admin()),
        api.approve(run["id"]),
    )
    payouts = await db.fetchval("SELECT count(*) FROM payroll.payouts")
    if delete.status_code == 204:
        assert _error(approve)[:2] == (404, "pay_run_not_found")
        assert payouts == 0
    else:
        assert approve.status_code == 200
        assert _error(delete) == (409, "pay_run_locked", {"status": "processing"})
        assert payouts == 1


# --- reads (PAY-303) ---------------------------------------------------------------------------


async def test_run_summary_shape(api: Api) -> None:
    """PAY-303, C9: the summary has exactly the PayRunOut keys, the six counts keys, the names
    from the token's name claim and funding_required_cents = Σ net."""
    run = await api.approved_run(2)
    out = await _get_run(api, run["id"])
    assert set(out) == RUN_KEYS
    assert set(out["counts"]) == COUNT_KEYS
    assert (out["calculated_by_name"], out["approved_by_name"]) == (
        "Carl Calculator", "Grace Approver",
    )  # fmt: skip
    assert out["require_dual_approval"] is True
    assert out["processing_since"] is not None
    assert out["approved_at"] is not None
    slips = await _payslips(api, run["id"])
    assert out["totals"]["funding_required_cents"] == sum(s["net_cents"] for s in slips)


async def test_payslips_page_until_null(api: Api) -> None:
    """PAY-303, C8: payslips are {items, next_cursor} ordered by employee_name, id."""
    run = await api.calculated_run(5)
    names: list[str] = []
    cursor: str | None = None
    while True:
        url = f"/v1/pay-runs/{run['id']}/payslips?limit=2" + (f"&cursor={cursor}" if cursor else "")
        page = (await api.client.get(url, headers=api.approver())).json()
        names += [p["employee_name"] for p in page["items"]]
        for p in page["items"]:
            assert p["gross_cents"] == p["net_cents"] + p["tax_cents"] + p["retirement_cents"]
            assert "schedule" not in p
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert names == [f"Emp {i:02d}" for i in range(5)]
    first = (await api.client.get(f"/v1/pay-runs/{run['id']}/payslips", headers=api.admin())).json()
    pid = first["items"][0]["id"]
    r = await api.client.get(f"/v1/pay-runs/{run['id']}/payslips/{pid}", headers=api.admin())
    assert r.json() == first["items"][0]
    r = await api.client.get(f"/v1/pay-runs/{run['id']}/payslips/{run['id']}", headers=api.admin())
    assert (r.status_code, r.json()["error"]["code"]) == (404, "payslip_not_found")


async def test_list_runs_newest_first_with_filter_and_cursor(api: Api) -> None:
    """PAY-303, PAY-304: runs newest period first; status filter; cursor paging; deleted runs
    hidden."""
    await api.create_employee()
    ids = [(await api.create_run("monthly", f"2026-0{m}-01"))["id"] for m in (1, 2, 3)]
    await api.calculate(ids[1])
    r = await api.client.get("/v1/pay-runs?limit=2", headers=api.approver())
    page = r.json()
    assert [x["id"] for x in page["items"]] == [ids[2], ids[1]]
    r = await api.client.get(f"/v1/pay-runs?cursor={page['next_cursor']}", headers=api.approver())
    assert [x["id"] for x in r.json()["items"]] == [ids[0]]
    assert r.json()["next_cursor"] is None
    r = await api.client.get("/v1/pay-runs?status=calculated", headers=api.approver())
    assert [x["id"] for x in r.json()["items"]] == [ids[1]]
    r = await api.client.delete(f"/v1/pay-runs/{ids[0]}", headers=api.payroll_admin())
    assert r.status_code == 204
    r = await api.client.get("/v1/pay-runs", headers=api.approver())
    assert [x["id"] for x in r.json()["items"]] == [ids[2], ids[1]]


async def test_other_company_cannot_see_or_change_runs(api: Api, db: Any) -> None:
    """PAY-601, PAY-301, PAY-304: a run of another company is indistinguishable from a missing
    one, for reads, hours, calculate and delete."""
    run = await api.calculated_run(1)
    other = await other_company(db)
    for path in ("", "/payslips", "/payouts"):
        r = await api.client.get(f"/v1/pay-runs/{run['id']}{path}", headers=api.admin(cid=other))
        assert (r.status_code, r.json()["error"]["code"]) == (404, "pay_run_not_found")
    foreign = api.payroll_admin(cid=other)
    r = await api.client.post(f"/v1/pay-runs/{run['id']}/calculate", headers=foreign)
    assert _error(r)[:2] == (404, "pay_run_not_found")
    r = await api.client.delete(f"/v1/pay-runs/{run['id']}", headers=foreign)
    assert _error(r)[:2] == (404, "pay_run_not_found")
    r = await _put_hours(api, run["id"], {str(uuid4()): 1}, headers=foreign)
    assert _error(r)[:2] == (404, "pay_run_not_found")
    assert (await _get_run(api, run["id"]))["status"] == "calculated"
