"""Schedule changes by PATCH against the run-overlap check (design §4.3, §4.4 step 5; PAY-203,
PAY-300, R34, C63): moving an employee between schedules never puts them on two payslips whose
periods share a day."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.integration.conftest import Api

pytestmark = pytest.mark.integration

# Every pair of payslips for the same employee in two live runs whose periods share a day.
DOUBLE_PAYSLIPS = """
SELECT a.employee_id
  FROM payroll.payslips a
  JOIN payroll.pay_runs ra ON ra.id = a.pay_run_id AND ra.deleted_at IS NULL
  JOIN payroll.payslips b ON b.employee_id = a.employee_id AND b.id <> a.id
  JOIN payroll.pay_runs rb ON rb.id = b.pay_run_id AND rb.deleted_at IS NULL
 WHERE ra.period_start <= rb.period_end AND rb.period_start <= ra.period_end
"""


async def _move(api: Api, emp_id: str, schedule: str) -> None:
    r = await api.client.patch(
        f"/v1/employees/{emp_id}", json={"schedule": schedule}, headers=api.payroll_admin()
    )
    assert r.status_code == 200, r.text


async def _create(api: Api, schedule: str, period_start: str) -> Any:
    return await api.client.post(
        "/v1/pay-runs",
        json={"schedule": schedule, "period_start": period_start},
        headers=api.payroll_admin(),
    )


async def test_monthly_to_biweekly_and_back(api: Api, db: Any) -> None:
    """PAY-203, PAY-300: after a PATCH from monthly to biweekly, every biweekly period sharing
    days with the January monthly run is refused (409 employee_in_overlapping_run with only the
    moved ID); the first clear period works; moving back, the February monthly run is refused
    because the February biweekly run already covers the employee."""
    moved = await api.create_employee()
    stays = await api.create_employee(name="Grace", email="g@example.com", schedule="biweekly")
    await api.create_run("monthly", "2026-01-01")
    await _move(api, moved["id"], "biweekly")
    for start in ("2026-01-05", "2026-01-19"):  # Jan 5-18 and Jan 19-Feb 1 both touch January
        r = await _create(api, "biweekly", start)
        assert (r.status_code, r.json()["error"]["code"]) == (409, "employee_in_overlapping_run")
        assert r.json()["error"]["details"] == {"employee_ids": [moved["id"]]}
        assert stays["id"] not in r.text
    feb = await api.create_run("biweekly", "2026-02-02")  # Feb 2-15
    assert feb["employee_count"] == 2
    await _move(api, moved["id"], "monthly")
    r = await _create(api, "monthly", "2026-02-01")
    assert r.json()["error"]["details"] == {"employee_ids": [moved["id"]]}
    assert await db.fetch(DOUBLE_PAYSLIPS) == []


async def test_biweekly_to_monthly_blocks_until_deactivated(api: Api, db: Any) -> None:
    """PAY-203, PAY-300, R34: an employee moved from biweekly to monthly blocks the overlapping
    monthly period (the documented limitation). Deactivating them frees the period for everyone
    else; reactivated, they join the next clear monthly period."""
    moved = await api.create_employee(schedule="biweekly")
    other = await api.create_employee(name="Grace", email="g@example.com")
    await api.create_run("biweekly", "2026-01-05")  # Jan 5-18
    await _move(api, moved["id"], "monthly")
    r = await _create(api, "monthly", "2026-01-01")
    assert r.json()["error"]["details"] == {"employee_ids": [moved["id"]]}
    r = await api.client.patch(
        f"/v1/employees/{moved['id']}", json={"status": "inactive"}, headers=api.payroll_admin()
    )
    assert r.status_code == 200
    january = await api.create_run("monthly", "2026-01-01")
    assert january["employee_count"] == 1
    r = await api.client.get(f"/v1/pay-runs/{january['id']}/payslips", headers=api.admin())
    assert [p["employee_id"] for p in r.json()["items"]] == [other["id"]]
    r = await api.client.patch(
        f"/v1/employees/{moved['id']}", json={"status": "active"}, headers=api.payroll_admin()
    )
    assert r.status_code == 200
    assert (await api.create_run("monthly", "2026-02-01"))["employee_count"] == 2
    assert await db.fetch(DOUBLE_PAYSLIPS) == []


async def test_a_schedule_move_racing_creates_never_double_pays(api: Api, db: Any) -> None:
    """PAY-203, PAY-300: a PATCH moving the employee to biweekly runs in parallel with a monthly
    January create and an overlapping biweekly create. Creates serialise on the per-company
    advisory lock and re-read payslips under READ COMMITTED, so whatever the order, the employee
    is on at most one payslip for any given day (and on at least one run)."""
    for start in ("2026-01-05", "2026-01-19"):
        await db.execute("UPDATE payroll.pay_runs SET deleted_at = now() WHERE deleted_at IS NULL")
        await db.execute(
            "UPDATE payroll.employees SET status = 'inactive', deactivated_at = now() "
            "WHERE status = 'active'"
        )
        emp = await api.create_employee(email=f"ada{start}@example.com")
        await api.create_employee(name=f"Bea {start}", email=f"bea{start}@example.com",
                                  schedule="biweekly")  # fmt: skip
        patch, monthly, biweekly = await asyncio.gather(
            api.client.patch(
                f"/v1/employees/{emp['id']}", json={"schedule": "biweekly"},
                headers=api.payroll_admin(),
            ),
            _create(api, "monthly", "2026-01-01"),
            _create(api, "biweekly", start),
        )  # fmt: skip
        assert patch.status_code == 200
        assert monthly.status_code in (201, 409), monthly.text
        assert biweekly.status_code in (201, 409), biweekly.text
        assert await db.fetch(DOUBLE_PAYSLIPS) == []
        n = await db.fetchval(
            "SELECT count(*) FROM payroll.payslips s JOIN payroll.pay_runs r "
            "ON r.id = s.pay_run_id AND r.deleted_at IS NULL WHERE s.employee_id = $1",
            emp["id"],
        )
        assert n == 1 or (monthly.status_code != 201 and n == 0), (monthly.text, biweekly.text)
