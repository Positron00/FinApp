"""Create, calculate and read pay runs (design §4.4; PAY-300, PAY-302, PAY-303)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from payroll.domain.calc import apply_bps, salaried_gross

from tests.integration.conftest import PAYROLL_ADMIN, Api, other_company

pytestmark = pytest.mark.integration

COUNT_KEYS = {"requested", "awaiting_bank", "blocked", "paid", "no_transfer", "failed"}


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


async def test_create_monthly_snapshots_payslips(api: Api, db: Any) -> None:
    """PAY-300: a monthly draft with period fields, one pending payslip per active employee,
    snapshotting name, pay type and salary; audit payrun.created."""
    await api.create_employee(name="Ann", email="ann@example.com", annual_salary_cents=1_200_000)
    await api.create_employee(
        name="Bob", email="bob@example.com", pay_type="hourly", annual_salary_cents=None,
        hourly_rate_cents=2_500,
    )  # fmt: skip
    await api.create_employee(name="Bi", email="bi@example.com", schedule="biweekly")
    run = await api.create_run("monthly", "2026-02-01")
    assert run["status"] == "draft"
    assert (run["period_start"], run["period_end"], run["pay_date"]) == (
        "2026-02-01",
        "2026-02-28",
        "2026-02-28",
    )
    assert (run["period_index"], run["periods_per_year"], run["employee_count"]) == (2, 12, 2)
    assert run["totals"] is None
    assert run["created_by"] == PAYROLL_ADMIN
    assert run["counts"] == dict.fromkeys(COUNT_KEYS, 0)
    rows = await db.fetch(
        "SELECT * FROM payroll.payslips WHERE pay_run_id = $1 ORDER BY employee_name", run["id"]
    )
    assert [(r["employee_name"], r["pay_type"], r["status"]) for r in rows] == [
        ("Ann", "salaried", "pending"),
        ("Bob", "hourly", "pending"),
    ]
    assert (rows[0]["annual_salary_cents"], rows[1]["hourly_rate_cents"]) == (1_200_000, 2_500)
    audit = await db.fetchrow(
        "SELECT details FROM payroll.audit_events WHERE event = 'payrun.created'"
    )
    assert json.loads(audit["details"]) == {
        "schedule": "monthly",
        "period_start": "2026-02-01",
        "employee_count": 2,
    }


async def test_create_biweekly(api: Api) -> None:
    """PAY-300: biweekly start on the anchor grid; end = start + 13; index from the anchor."""
    await api.create_employee(schedule="biweekly")
    run = await api.create_run("biweekly", "2026-02-02")  # anchor 2026-01-05 + 28 days
    assert (run["period_end"], run["period_index"], run["periods_per_year"]) == (
        "2026-02-15",
        3,
        26,
    )


async def test_create_rejections(api: Api, db: Any) -> None:
    """PAY-300: pay_run_exists (with its id), invalid_period_start, no_employees; a deleted run
    frees its period."""
    r = await api.client.post(
        "/v1/pay-runs", json={"schedule": "monthly", "period_start": "2026-01-01"},
        headers=api.payroll_admin(),
    )  # fmt: skip
    assert (r.status_code, r.json()["error"]["code"]) == (409, "no_employees")
    await api.create_employee()
    run = await api.create_run()
    r = await api.client.post(
        "/v1/pay-runs", json={"schedule": "monthly", "period_start": "2026-01-01"},
        headers=api.payroll_admin(),
    )  # fmt: skip
    assert r.status_code == 409
    assert r.json()["error"] | {} == {
        **r.json()["error"],
        "code": "pay_run_exists",
        "details": {"pay_run_id": run["id"]},
    }
    for schedule, start in (("monthly", "2026-01-02"), ("biweekly", "2026-01-06")):
        r = await api.client.post(
            "/v1/pay-runs", json={"schedule": schedule, "period_start": start},
            headers=api.payroll_admin(),
        )  # fmt: skip
        assert (r.status_code, r.json()["error"]["code"]) == (422, "invalid_period_start")
    await db.execute("UPDATE payroll.pay_runs SET deleted_at = now() WHERE id = $1", run["id"])
    again = await api.create_run()
    assert again["id"] != run["id"]


async def test_two_concurrent_creates_one_wins(api: Api, db: Any) -> None:
    """PAY-300: two parallel creates for one period → one 201 and one 409 pay_run_exists."""
    await api.create_employee()

    async def create() -> int:
        r = await api.client.post(
            "/v1/pay-runs", json={"schedule": "monthly", "period_start": "2026-03-01"},
            headers=api.payroll_admin(),
        )  # fmt: skip
        return r.status_code

    assert sorted(await asyncio.gather(create(), create())) == [201, 409]
    assert await db.fetchval("SELECT count(*) FROM payroll.pay_runs") == 1


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
    # Recalculating after a rate change recomputes; without a name claim the name is NULL.
    await _set_rates(api, tax=1000)
    again = await api.calculate(run["id"])
    assert again["tax_bps"] == 1000
    assert again["calculated_by_name"] is None


async def test_hourly_without_hours_is_hours_missing(api: Api) -> None:
    """PAY-302: an hourly payslip without hours → 422 hours_missing with the employee IDs."""
    await api.create_employee()
    hourly = await api.create_employee(
        name="H", email="h@example.com", pay_type="hourly", annual_salary_cents=None,
        hourly_rate_cents=1_999,
    )  # fmt: skip
    run = await api.create_run()
    r = await api.client.post(f"/v1/pay-runs/{run['id']}/calculate", headers=api.payroll_admin())
    assert r.status_code == 422
    assert r.json()["error"] == {
        **r.json()["error"],
        "code": "hours_missing",
        "details": {"employee_ids": [hourly["id"]]},
    }


async def test_hourly_with_hours_uses_the_hourly_formula(api: Api, db: Any) -> None:
    """PAY-302: hours stored for the run feed (rate * hundredths + 50) // 100 (hours entry
    itself is F3; the row is inserted directly here)."""
    hourly = await api.create_employee(
        pay_type="hourly", annual_salary_cents=None, hourly_rate_cents=1_999
    )
    run = await api.create_run()
    await db.execute(
        "INSERT INTO payroll.pay_run_hours (id, company_id, pay_run_id, employee_id, "
        "hours_hundredths, entered_by) VALUES (gen_random_uuid(), $1, $2, $3, 150, 'x')",
        api.cid, run["id"], hourly["id"],
    )  # fmt: skip
    await api.calculate(run["id"])
    row = await db.fetchrow("SELECT * FROM payroll.payslips WHERE pay_run_id = $1", run["id"])
    assert (row["hours_hundredths"], row["gross_cents"], row["net_cents"]) == (150, 2_999, 2_999)


async def test_calculate_after_approval_is_locked(api: Api) -> None:
    """PAY-302: calculate on a processing run → 409 pay_run_locked."""
    run = await api.approved_run(1)
    r = await api.client.post(f"/v1/pay-runs/{run['id']}/calculate", headers=api.payroll_admin())
    assert r.json()["error"] == {
        **r.json()["error"],
        "code": "pay_run_locked",
        "details": {"status": "processing"},
    }


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


async def test_list_runs_newest_first_with_filter_and_cursor(api: Api, db: Any) -> None:
    """PAY-303: runs newest period first; status filter; cursor paging; deleted runs hidden."""
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
    await db.execute("UPDATE payroll.pay_runs SET deleted_at = now() WHERE id = $1", ids[0])
    r = await api.client.get("/v1/pay-runs", headers=api.approver())
    assert ids[0] not in [x["id"] for x in r.json()["items"]]
    r = await api.client.get(f"/v1/pay-runs/{ids[0]}", headers=api.approver())
    assert (r.status_code, r.json()["error"]["code"]) == (404, "pay_run_not_found")


async def test_other_company_cannot_see_runs(api: Api, db: Any) -> None:
    """PAY-601: a run of another company is indistinguishable from a missing one."""
    run = await api.calculated_run(1)
    other = await other_company(db)
    for path in ("", "/payslips", "/payouts"):
        r = await api.client.get(f"/v1/pay-runs/{run['id']}{path}", headers=api.admin(cid=other))
        assert (r.status_code, r.json()["error"]["code"]) == (404, "pay_run_not_found")
    r = await api.client.post(
        f"/v1/pay-runs/{run['id']}/calculate", headers=api.payroll_admin(cid=other)
    )
    assert r.status_code == 404
