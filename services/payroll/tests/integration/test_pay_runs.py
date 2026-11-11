"""Create pay runs (design §4.4 "Create", §9.2; PAY-300). Hours, calculate, delete and the
reads are in ``test_hours_calculate.py``."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from payroll.db.repositories import PayRunRepo, PayslipRepo

from tests.integration.conftest import PAYROLL_ADMIN, Api

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


async def test_create_rejections(api: Api) -> None:
    """PAY-300, PAY-304: pay_run_exists (with its id), invalid_period_start, no_employees; a
    deleted run frees its period."""
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
    r = await api.client.delete(f"/v1/pay-runs/{run['id']}", headers=api.payroll_admin())
    assert r.status_code == 204
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


async def test_unique_index_race_returns_the_real_run_id(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PAY-300, C63: when the pre-check misses a live run (a race), the unique index still
    refuses the insert and the 409 carries the winner's pay_run_id, not null."""
    await api.create_employee()
    run = await api.create_run()
    real = PayRunRepo.find_live
    calls = 0

    async def miss_once(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return None if calls == 1 else await real(*args, **kwargs)

    async def no_coverage(*_a: Any, **_k: Any) -> list[Any]:
        return []  # a racing create's rows aren't visible yet either

    monkeypatch.setattr(PayRunRepo, "find_live", staticmethod(miss_once))
    monkeypatch.setattr(PayslipRepo, "coverage", staticmethod(no_coverage))
    r = await api.client.post(
        "/v1/pay-runs", json={"schedule": "monthly", "period_start": run["period_start"]},
        headers=api.payroll_admin(),
    )  # fmt: skip
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "pay_run_exists"
    assert r.json()["error"]["details"] == {"pay_run_id": run["id"]}
    assert calls == 2


async def test_employee_in_overlapping_run_after_a_schedule_change(api: Api, db: Any) -> None:
    """PAY-300, C63: an employee moved from monthly to biweekly is refused for a biweekly period
    that shares days with their January monthly run (409 with the IDs); a deleted run no longer
    counts (PAY-304), and a non-overlapping period is fine."""
    moved = await api.create_employee()
    stays = await api.create_employee(name="Grace Hopper", email="grace@example.com",
                                      schedule="biweekly")  # fmt: skip
    january = await api.create_run("monthly", "2026-01-01")
    await db.execute(
        "UPDATE payroll.employees SET schedule = 'biweekly' WHERE id = $1", moved["id"]
    )
    r = await api.client.post(
        "/v1/pay-runs", json={"schedule": "biweekly", "period_start": "2026-01-19"},
        headers=api.payroll_admin(),
    )  # fmt: skip
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "employee_in_overlapping_run"
    assert r.json()["error"]["details"] == {"employee_ids": [moved["id"]]}
    assert stays["id"] not in json.dumps(r.json())
    assert await db.fetchval("SELECT count(*) FROM payroll.pay_runs") == 1
    later = await api.create_run("biweekly", "2026-02-02")  # Feb 2 to 15: after January ends
    assert later["employee_count"] == 2
    r = await api.client.delete(f"/v1/pay-runs/{january['id']}", headers=api.payroll_admin())
    assert r.status_code == 204
    assert (await api.create_run("biweekly", "2026-01-19"))["employee_count"] == 2
