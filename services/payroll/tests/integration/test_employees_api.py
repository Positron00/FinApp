"""Employees: create, list and read (design §4.3; PAY-201, PAY-202, PAY-204, PAY-NFR-08)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from payroll.crypto import employee_aad

from tests.helpers import VALID_ROUTING, employee_body
from tests.integration.conftest import PAYROLL_ADMIN, Api, other_company

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
