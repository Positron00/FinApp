"""Company settings API (design §4.2; PAY-200, R33)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from tests.helpers import OTHER_COMPANY_ID
from tests.integration.conftest import ADMIN, Api

pytestmark = pytest.mark.integration

BODY: dict[str, Any] = {
    "name": "Acme Payroll",
    "tax_rate_bps": 2000,
    "retirement_bps": 500,
    "timezone": "Europe/Berlin",
    "biweekly_anchor": "2026-01-05",
    "require_dual_approval": True,
}


async def _audits(db: Any, event: str) -> list[Any]:
    return list(await db.fetch("SELECT * FROM payroll.audit_events WHERE event = $1", event))


async def test_get_returns_the_seed_for_any_role(api: Api) -> None:
    """PAY-200: GET for any role returns the seeded defaults."""
    for headers in (api.admin(), api.payroll_admin(), api.approver()):
        r = await api.client.get("/v1/company/settings", headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert (body["tax_rate_bps"], body["retirement_bps"]) == (0, 0)
        assert body["biweekly_anchor"] == "2026-01-05"
        assert body["require_dual_approval"] is True
        assert body["updated_by"] is None


async def test_put_updates_and_audits_the_changed_fields(api: Api, db: Any) -> None:
    """PAY-200: PUT (admin) replaces; settings.updated lists changed fields with old and new."""
    r = await api.client.put("/v1/company/settings", json=BODY, headers=api.admin())
    assert r.status_code == 200, r.text
    assert r.json()["updated_by"] == ADMIN
    assert r.json()["tax_rate_bps"] == 2000
    [audit] = await _audits(db, "settings.updated")
    changed = json.loads(audit["details"])["changed"]
    assert changed == {
        "name": {"old": "PayRun Demo Company", "new": "Acme Payroll"},
        "tax_rate_bps": {"old": 0, "new": 2000},
        "retirement_bps": {"old": 0, "new": 500},
        "timezone": {"old": "America/New_York", "new": "Europe/Berlin"},
    }
    assert audit["actor_id"] == ADMIN
    r = await api.client.put("/v1/company/settings", json=BODY, headers=api.admin())
    assert r.status_code == 200
    assert len(await _audits(db, "settings.updated")) == 1  # nothing changed, no audit row


async def test_put_is_admin_only(api: Api) -> None:
    """PAY-200, PAY-601: payroll_admin and approver get 403 forbidden_role."""
    for headers in (api.payroll_admin(), api.approver()):
        r = await api.client.put("/v1/company/settings", json=BODY, headers=headers)
        assert (r.status_code, r.json()["error"]["code"]) == (403, "forbidden_role")


async def test_out_of_range_is_422_without_echo(api: Api) -> None:
    """PAY-200, C14: 422 validation_error with [{field, issue}] and no input value."""
    bad = {**BODY, "tax_rate_bps": 987654, "biweekly_anchor": "2026-01-06", "timezone": "Mars/X"}
    r = await api.client.put("/v1/company/settings", json=bad, headers=api.admin())
    assert r.status_code == 422
    error = r.json()["error"]
    assert error["code"] == "validation_error"
    assert {(d["field"], d["issue"]) for d in error["details"]} == {
        ("body.tax_rate_bps", "less_than_equal"),
        ("body.biweekly_anchor", "not_monday"),
        ("body.timezone", "timezone"),
    }
    assert "987654" not in r.text
    assert "Mars/X" not in r.text


async def test_anchor_locked_while_a_biweekly_run_exists(api: Api, db: Any) -> None:
    """PAY-200, R33: anchor change → 409 anchor_locked with a live biweekly run; nothing changes;
    allowed once that run is deleted, and with only monthly runs."""
    await api.create_employee(schedule="monthly", email="m@example.com")
    await api.create_run("monthly", "2026-01-01")
    moved = {**BODY, "biweekly_anchor": "2026-01-12"}
    r = await api.client.put("/v1/company/settings", json=moved, headers=api.admin())
    assert r.status_code == 200  # monthly runs only
    await api.create_employee(schedule="biweekly", email="b@example.com")
    run = await api.create_run("biweekly", "2026-01-12")
    back = {**BODY, "biweekly_anchor": "2026-01-05", "tax_rate_bps": 100}
    r = await api.client.put("/v1/company/settings", json=back, headers=api.admin())
    assert (r.status_code, r.json()["error"]["code"]) == (409, "anchor_locked")
    row = await db.fetchrow("SELECT biweekly_anchor, tax_rate_bps FROM payroll.company_settings")
    assert (str(row["biweekly_anchor"]), row["tax_rate_bps"]) == ("2026-01-12", 2000)
    same_anchor = {**BODY, "biweekly_anchor": "2026-01-12", "tax_rate_bps": 100}
    r = await api.client.put("/v1/company/settings", json=same_anchor, headers=api.admin())
    assert r.status_code == 200  # other fields may change while the anchor stays
    await db.execute("UPDATE payroll.pay_runs SET deleted_at = now() WHERE id = $1", run["id"])
    r = await api.client.put("/v1/company/settings", json=back, headers=api.admin())
    assert r.status_code == 200


async def test_put_racing_a_biweekly_create_never_leaves_a_run_on_the_old_anchor(
    api: Api, db: Any
) -> None:
    """PAY-200, R33: a PUT moving the anchor and a biweekly create on the old anchor, in parallel.
    The settings row lock (PUT ``FOR UPDATE``, create ``FOR SHARE``) serialises them, so either
    the run exists on the old anchor and the PUT got 409 anchor_locked, or the anchor moved and the
    create got 422 invalid_period_start; never both succeeding."""
    await api.create_employee(schedule="biweekly", email="b@example.com")
    moved = {**BODY, "biweekly_anchor": "2026-01-12"}
    for start in ("2026-01-05", "2026-01-19", "2026-02-02", "2026-02-16"):
        await db.execute("UPDATE payroll.pay_runs SET deleted_at = now() WHERE deleted_at IS NULL")
        await db.execute("UPDATE payroll.company_settings SET biweekly_anchor = '2026-01-05'")
        put, create = await asyncio.gather(
            api.client.put("/v1/company/settings", json=moved, headers=api.admin()),
            api.client.post(
                "/v1/pay-runs", json={"schedule": "biweekly", "period_start": start},
                headers=api.payroll_admin(),
            ),
        )  # fmt: skip
        anchor = str(await db.fetchval("SELECT biweekly_anchor FROM payroll.company_settings"))
        live = await db.fetchval("SELECT count(*) FROM payroll.pay_runs WHERE deleted_at IS NULL")
        if create.status_code == 201:
            assert (put.status_code, put.json()["error"]["code"]) == (409, "anchor_locked")
            assert (anchor, live) == ("2026-01-05", 1)
        else:
            assert put.status_code == 200
            assert create.json()["error"]["code"] == "invalid_period_start"
            assert (anchor, live) == ("2026-01-12", 0)


async def test_missing_settings_row_is_404(api: Api) -> None:
    """PAY-200: another company without a settings row → 404 company_settings_not_found."""
    r = await api.client.get("/v1/company/settings", headers=api.admin(cid=OTHER_COMPANY_ID))
    assert (r.status_code, r.json()["error"]["code"]) == (404, "company_settings_not_found")
    r = await api.client.put(
        "/v1/company/settings", json=BODY, headers=api.admin(cid=OTHER_COMPANY_ID)
    )
    assert r.status_code == 404
