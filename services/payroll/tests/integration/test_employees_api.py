"""Employees: create, list, read and update (design §4.3; PAY-201 to PAY-204, PAY-NFR-08)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from payroll.crypto import employee_aad

from tests.helpers import OTHER_ROUTING, VALID_ROUTING, employee_body
from tests.integration.conftest import APPROVER, PAYROLL_ADMIN, Api, other_company

pytestmark = pytest.mark.integration

ACCOUNT = "000987654321"


async def test_create_returns_last4_only_and_encrypts_at_rest(api: Api, db: Any) -> None:
    """PAY-201, PAY-202, PAY-204: 201 with account_last4 only; the DB holds AES-GCM tokens."""
    r = await api.client.post(
        "/v1/employees", json=employee_body(bank_account=ACCOUNT), headers=api.payroll_admin()
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["account_last4"] == "4321"
    assert body["has_bank_details"] is True
    assert body["bank_updated_by"] == PAYROLL_ADMIN
    assert body["bank_updated_at"] is not None
    assert body["status"] == "active"
    assert body["deactivated_at"] is None
    assert ACCOUNT not in r.text
    assert VALID_ROUTING not in r.text
    assert "bank_routing" not in body
    assert "bank_account" not in body
    row = await db.fetchrow("SELECT * FROM payroll.employees WHERE id = $1", body["id"])
    assert ACCOUNT not in row["bank_account_enc"]
    assert VALID_ROUTING not in row["bank_routing_enc"]
    eid = row["id"]
    keyring = api.rt.keyring
    assert (
        keyring.decrypt(row["bank_account_enc"], aad=employee_aad("bank_account", eid)) == ACCOUNT
    )
    assert (
        keyring.decrypt(row["bank_routing_enc"], aad=employee_aad("bank_routing", eid))
        == VALID_ROUTING
    )
    assert row["bank_account_enc"].startswith(keyring.active_kid + ":")
    audit = await db.fetchrow("SELECT * FROM payroll.audit_events WHERE event = 'employee.created'")
    details = json.loads(audit["details"])
    assert details["account_last4"] == "4321"
    assert "bank_account" in details["fields"]
    assert ACCOUNT not in audit["details"]
    assert "Ada" not in audit["details"]


async def test_email_taken_case_insensitive(api: Api) -> None:
    """PAY-201: a second employee with the same email (any case) → 409 employee_email_taken."""
    await api.create_employee(email="dup@example.com")
    r = await api.client.post(
        "/v1/employees", json=employee_body(email="DUP@Example.com"), headers=api.payroll_admin()
    )
    assert (r.status_code, r.json()["error"]["code"]) == (409, "employee_email_taken")


async def test_validation_details_never_echo_input(api: Api) -> None:
    """PAY-201, C14: 422 lists field names and issues; the bad values aren't echoed."""
    body = employee_body(bank_routing="021000022", bank_account="12")
    r = await api.client.post("/v1/employees", json=body, headers=api.payroll_admin())
    assert r.status_code == 422
    details = r.json()["error"]["details"]
    assert {(d["field"], d["issue"]) for d in details} == {
        ("body.bank_routing", "aba_checksum"),
        ("body.bank_account", "string_pattern_mismatch"),
    }
    assert "021000022" not in r.text


async def test_list_pages_in_name_order_with_status_filter(api: Api, db: Any) -> None:
    """PAY-202, C8: ordered by name then id; cursor pages until next_cursor is null; status."""
    names = ["Eve", "Bob", "Dan", "Ann", "Cat"]
    for i, n in enumerate(names):
        await api.create_employee(name=n, email=f"{n.lower()}{i}@example.com")
    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        url = "/v1/employees?limit=2" + (f"&cursor={cursor}" if cursor else "")
        r = await api.client.get(url, headers=api.approver())
        assert r.status_code == 200, r.text
        page = r.json()
        seen += [e["name"] for e in page["items"]]
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == sorted(names)
    assert pages == 3
    await db.execute(
        "UPDATE payroll.employees SET status = 'inactive', deactivated_at = now() "
        "WHERE name = 'Bob'"
    )
    r = await api.client.get("/v1/employees?status=inactive", headers=api.admin())
    assert [e["name"] for e in r.json()["items"]] == ["Bob"]
    r = await api.client.get("/v1/employees?status=active&limit=100", headers=api.admin())
    assert len(r.json()["items"]) == 4
    assert all(ACCOUNT not in json.dumps(e) for e in r.json()["items"])


async def test_bad_cursor_and_limit(api: Api) -> None:
    """PAY-202, PAY-602: an undecodable or unknown cursor → 422 invalid_cursor; limit 1-100."""
    r = await api.client.get("/v1/employees?cursor=garbage", headers=api.admin())
    assert r.json()["error"]["details"] == [{"field": "query.cursor", "issue": "invalid_cursor"}]
    from uuid import uuid4  # noqa: PLC0415

    from payroll.api.pagination import encode_cursor  # noqa: PLC0415

    r = await api.client.get(f"/v1/employees?cursor={encode_cursor(uuid4())}", headers=api.admin())
    assert r.status_code == 422
    r = await api.client.get("/v1/employees?limit=101", headers=api.admin())
    assert r.json()["error"]["details"] == [{"field": "query.limit", "issue": "less_than_equal"}]


async def test_get_and_cross_company_404(api: Api, db: Any) -> None:
    """PAY-202, PAY-601: GET returns the employee; another company's token gets 404."""
    emp = await api.create_employee()
    r = await api.client.get(f"/v1/employees/{emp['id']}", headers=api.approver())
    assert r.json() == emp
    other = await other_company(db)
    r = await api.client.get(f"/v1/employees/{emp['id']}", headers=api.approver(cid=other))
    assert (r.status_code, r.json()["error"]["code"]) == (404, "employee_not_found")
    r = await api.client.get("/v1/employees", headers=api.approver(cid=other))
    assert r.json() == {"items": [], "next_cursor": None}


# --- PATCH (PAY-203) ---------------------------------------------------------------------------


async def _patch(api: Api, emp_id: str, body: dict[str, Any], **kw: Any) -> Any:
    headers = kw.pop("headers", None) or api.payroll_admin(**kw)
    return await api.client.patch(f"/v1/employees/{emp_id}", json=body, headers=headers)


async def _updates(db: Any, emp_id: str) -> list[dict[str, Any]]:
    rows = await db.fetch(
        "SELECT actor_id, details FROM payroll.audit_events "
        "WHERE event = 'employee.updated' AND entity_id = $1 ORDER BY id",
        emp_id,
    )
    return [{"actor_id": r["actor_id"], **json.loads(r["details"])} for r in rows]


async def test_patch_plain_fields_audits_names_only(api: Api, db: Any) -> None:
    """PAY-203: name, email, salary and schedule change; employee.updated lists the changed field
    names only (no values); bank tracking is untouched; an unchanged value isn't listed."""
    emp = await api.create_employee()
    body = {
        "name": "Augusta King",
        "email": "augusta@example.com",
        "annual_salary_cents": 6_100_000,
        "schedule": "biweekly",
        "pay_type": "salaried",  # unchanged: not listed
    }
    r = await _patch(api, emp["id"], body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["name"], out["email"], out["schedule"]) == (
        "Augusta King", "augusta@example.com", "biweekly",
    )  # fmt: skip
    assert out["annual_salary_cents"] == 6_100_000
    assert (out["bank_updated_at"], out["bank_updated_by"]) == (
        emp["bank_updated_at"], emp["bank_updated_by"],
    )  # fmt: skip
    assert out["updated_at"] > emp["updated_at"]
    [audit] = await _updates(db, emp["id"])
    assert audit == {
        "actor_id": PAYROLL_ADMIN,
        "fields": ["annual_salary_cents", "email", "name", "schedule"],
    }
    raw = await db.fetchval("SELECT details::text FROM payroll.audit_events WHERE event = $1",
                            "employee.updated")  # fmt: skip
    for value in ("Augusta", "augusta@", "6100000"):
        assert value not in raw


async def test_patch_that_changes_nothing_writes_no_audit(api: Api, db: Any) -> None:
    """PAY-203: a PATCH with the stored values returns 200 and writes no audit row."""
    emp = await api.create_employee()
    r = await _patch(api, emp["id"], {"name": emp["name"], "schedule": emp["schedule"]})
    assert r.status_code == 200
    assert r.json() == emp
    assert await _updates(db, emp["id"]) == []


async def test_patch_bank_change_tracks_editor_and_last4(api: Api, db: Any) -> None:
    """PAY-203, PAY-204, ADR 0005: a bank change records bank_updated_by and bank_updated_at,
    re-encrypts with the active kid, and the audit holds old and new account_last4 only."""
    emp = await api.create_employee(bank_account=ACCOUNT)
    editor = "0193a000-0000-7000-8000-0000000000b7"
    r = await _patch(
        api, emp["id"], {"bank_account": "5550001111"},
        headers=api.headers(editor, ("payroll_admin",)),
    )  # fmt: skip
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["account_last4"] == "1111"
    assert out["bank_updated_by"] == editor
    assert out["bank_updated_at"] > emp["bank_updated_at"]
    assert "5550001111" not in r.text
    row = await db.fetchrow("SELECT * FROM payroll.employees WHERE id = $1", emp["id"])
    eid = row["id"]
    keyring = api.rt.keyring
    assert keyring.kid_of(row["bank_account_enc"]) == keyring.active_kid
    assert (
        keyring.decrypt(row["bank_account_enc"], aad=employee_aad("bank_account", eid))
        == "5550001111"
    )
    assert (
        keyring.decrypt(row["bank_routing_enc"], aad=employee_aad("bank_routing", eid))
        == VALID_ROUTING
    )  # the routing number was not supplied, so it is kept
    [audit] = await _updates(db, emp["id"])
    assert audit == {
        "actor_id": editor,
        "fields": ["bank_account"],
        "account_last4": {"old": "4321", "new": "1111"},
    }
    raw = await db.fetchval("SELECT details::text FROM payroll.audit_events WHERE event = $1",
                            "employee.updated")  # fmt: skip
    assert "5550001111" not in raw
    assert ACCOUNT not in raw


async def test_patch_routing_only_is_a_bank_change(api: Api, db: Any) -> None:
    """PAY-203: a routing-only change is a bank change too (same last4 on both sides); the
    stored numbers are never decrypted to compare (ADR 0004)."""
    emp = await api.create_employee(bank_account=ACCOUNT)
    r = await _patch(api, emp["id"], {"bank_routing": OTHER_ROUTING})
    assert r.status_code == 200
    assert r.json()["bank_updated_at"] > emp["bank_updated_at"]
    [audit] = await _updates(db, emp["id"])
    assert audit["fields"] == ["bank_routing"]
    assert audit["account_last4"] == {"old": "4321", "new": "4321"}
    assert OTHER_ROUTING not in json.dumps(audit)


async def test_deactivate_and_reactivate(api: Api, db: Any) -> None:
    """PAY-203: status inactive sets deactivated_at; reactivation clears it; both audited with
    the status diff; the list filter follows."""
    emp = await api.create_employee()
    r = await _patch(api, emp["id"], {"status": "inactive"})
    assert r.status_code == 200
    assert r.json()["status"] == "inactive"
    assert r.json()["deactivated_at"] is not None
    r = await api.client.get("/v1/employees?status=inactive", headers=api.admin())
    assert [e["id"] for e in r.json()["items"]] == [emp["id"]]
    r = await _patch(api, emp["id"], {"status": "inactive"})  # already inactive: no change
    assert r.status_code == 200
    r = await _patch(api, emp["id"], {"status": "active"})
    assert r.status_code == 200
    assert (r.json()["status"], r.json()["deactivated_at"]) == ("active", None)
    assert r.json()["has_bank_details"] is True
    audits = await _updates(db, emp["id"])
    assert audits == [
        {"actor_id": PAYROLL_ADMIN, "fields": ["status"],
         "status": {"old": "active", "new": "inactive"}},
        {"actor_id": PAYROLL_ADMIN, "fields": ["status"],
         "status": {"old": "inactive", "new": "active"}},
    ]  # fmt: skip


async def test_reactivation_after_purge_needs_bank_details(api: Api, db: Any) -> None:
    """PAY-203, PAY-608: once the details were purged, reactivating needs both bank fields
    (422 required_on_reactivation, nothing changes); with them, the employee is active again,
    bank_purged_at is cleared and the reactivator is the bank editor."""
    emp = await api.create_employee()
    assert (await _patch(api, emp["id"], {"status": "inactive"})).status_code == 200
    await db.execute(
        "UPDATE payroll.employees SET bank_routing_enc = NULL, bank_account_enc = NULL, "
        "account_last4 = NULL, bank_purged_at = now() WHERE id = $1",
        emp["id"],
    )
    r = await _patch(api, emp["id"], {"status": "active"})
    assert r.status_code == 422
    assert r.json()["error"]["details"] == [
        {"field": "body.bank_routing", "issue": "required_on_reactivation"},
        {"field": "body.bank_account", "issue": "required_on_reactivation"},
    ]
    r = await _patch(api, emp["id"], {"status": "active", "bank_account": "4444"})
    assert r.json()["error"]["details"] == [
        {"field": "body.bank_routing", "issue": "required_on_reactivation"}
    ]
    r = await _patch(api, emp["id"], {"bank_account": "4444"})  # still inactive, half the pair
    assert r.json()["error"]["details"] == [
        {"field": "body.bank_routing", "issue": "bank_details_incomplete"}
    ]
    assert (
        await db.fetchval("SELECT status FROM payroll.employees WHERE id = $1", emp["id"])
        == "inactive"
    )
    editor = "0193a000-0000-7000-8000-0000000000b8"
    r = await _patch(
        api, emp["id"],
        {"status": "active", "bank_routing": VALID_ROUTING, "bank_account": "4444"},
        headers=api.headers(editor, ("payroll_admin",)),
    )  # fmt: skip
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["status"], out["deactivated_at"], out["account_last4"]) == ("active", None, "4444")
    assert out["bank_updated_by"] == editor
    row = await db.fetchrow("SELECT bank_purged_at FROM payroll.employees WHERE id = $1", emp["id"])
    assert row["bank_purged_at"] is None
    audit = (await _updates(db, emp["id"]))[-1]
    assert audit["fields"] == ["bank_account", "bank_routing", "status"]
    assert audit["account_last4"] == {"old": None, "new": "4444"}


async def test_patch_pay_type_merges_the_pay_basis(api: Api) -> None:
    """PAY-203: switching to hourly needs hourly_rate_cents (422 rate_required) and clears the
    salary; sending the other type's amount is 422 pay_basis."""
    emp = await api.create_employee()
    r = await _patch(api, emp["id"], {"pay_type": "hourly"})
    assert r.status_code == 422
    assert r.json()["error"]["details"] == [
        {"field": "body.hourly_rate_cents", "issue": "rate_required"}
    ]
    r = await _patch(api, emp["id"], {"hourly_rate_cents": 2500})
    assert r.json()["error"]["details"] == [
        {"field": "body.hourly_rate_cents", "issue": "pay_basis"}
    ]
    r = await _patch(api, emp["id"], {"pay_type": "hourly", "hourly_rate_cents": 2500})
    assert r.status_code == 200
    out = r.json()
    assert (out["pay_type"], out["hourly_rate_cents"], out["annual_salary_cents"]) == (
        "hourly", 2500, None,
    )  # fmt: skip


async def test_patch_email_taken_404_and_roles(api: Api, db: Any) -> None:
    """PAY-203, PAY-601: another employee's email (any case) → 409 employee_email_taken; an
    unknown or another company's employee → 404; only payroll_admin may PATCH; an empty or
    null body → 422."""
    emp = await api.create_employee()
    await api.create_employee(name="Grace", email="grace@example.com")
    r = await _patch(api, emp["id"], {"email": "GRACE@example.com"})
    assert (r.status_code, r.json()["error"]["code"]) == (409, "employee_email_taken")
    r = await _patch(api, emp["id"], {"email": "ADA@example.com"})  # own email, other case
    assert r.status_code == 200
    other = await other_company(db)
    r = await _patch(api, emp["id"], {"name": "X"}, cid=other)
    assert (r.status_code, r.json()["error"]["code"]) == (404, "employee_not_found")
    for headers in (api.admin(), api.approver()):
        r = await _patch(api, emp["id"], {"name": "X"}, headers=headers)
        assert (r.status_code, r.json()["error"]["code"]) == (403, "forbidden_role")
    r = await _patch(api, emp["id"], {})
    assert r.json()["error"]["details"] == [{"field": "body", "issue": "no_fields"}]
    r = await _patch(api, emp["id"], {"name": None})
    assert r.json()["error"]["details"] == [{"field": "body.name", "issue": "null_not_allowed"}]


async def test_unvetted_bank_edit_blocks_the_editor_from_approving(api: Api) -> None:
    """PAY-203 → PAY-401, ADR 0005, C73: an approver who also edited an employee's bank details,
    not yet released by a second person, can't approve the run (403 approver_must_differ, rule
    bank_editor); another approver can."""
    emp = await api.create_employee()
    run = await api.create_run()
    r = await _patch(
        api, emp["id"], {"bank_account": "7777"},
        headers=api.headers(APPROVER, ("approver", "payroll_admin")),
    )  # fmt: skip
    assert r.status_code == 200
    assert r.json()["bank_updated_by"] == APPROVER
    await api.calculate(run["id"])
    r = await api.approve(run["id"])
    assert (r.status_code, r.json()["error"]["code"]) == (403, "approver_must_differ")
    assert r.json()["error"]["details"] == {"rule": "bank_editor", "employee_ids": [emp["id"]]}
    other = api.headers("0193a000-0000-7000-8000-0000000000b9", ("approver",))
    r = await api.approve(run["id"], headers=other)
    assert r.status_code == 200, r.text
