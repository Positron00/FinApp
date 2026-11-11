"""``GET /v1/audit-events`` (design §4.5, §4.9; PAY-200, PAY-203, PAY-601, PAY-602, C5, C8)."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest
from payroll.api.pagination import encode_cursor

from tests.integration.conftest import ADMIN, PAYROLL_ADMIN, Api, other_company

pytestmark = pytest.mark.integration

FIELDS = {"id", "at", "actor_id", "event", "entity_type", "entity_id", "details"}


async def _seed(api: Api) -> tuple[dict[str, Any], dict[str, Any]]:
    """employee.created x2, employee.updated, settings.updated, payrun.created (in that order)."""
    a = await api.create_employee(name="Ann", email="ann@example.com")
    b = await api.create_employee(name="Ben", email="ben@example.com", bank_account="99991234")
    r = await api.client.patch(
        f"/v1/employees/{a['id']}", json={"bank_account": "55556789"}, headers=api.payroll_admin()
    )
    assert r.status_code == 200
    r = await api.client.put(
        "/v1/company/settings",
        json={"name": "Acme", "tax_rate_bps": 100, "retirement_bps": 0,
              "timezone": "America/New_York", "biweekly_anchor": "2026-01-05",
              "require_dual_approval": True},
        headers=api.admin(),
    )  # fmt: skip
    assert r.status_code == 200
    await api.create_run()
    return a, b


async def _all(api: Api, query: str = "", limit: int = 2) -> tuple[list[dict[str, Any]], int]:
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    pages = 0
    while True:
        url = f"/v1/audit-events?limit={limit}{query}" + (f"&cursor={cursor}" if cursor else "")
        r = await api.client.get(url, headers=api.admin())
        assert r.status_code == 200, r.text
        page = r.json()
        items += page["items"]
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            return items, pages


async def test_lists_newest_first_in_pages(api: Api) -> None:
    """PAY-200, PAY-203, C5, C8: admin reads every event newest first (id DESC) with the C5
    fields, paging with the cursor until next_cursor is null."""
    a, _ = await _seed(api)
    items, pages = await _all(api)
    assert [e["event"] for e in items] == [
        "payrun.created", "settings.updated", "employee.updated",
        "employee.created", "employee.created",
    ]  # fmt: skip
    assert pages == 3
    assert all(set(e) == FIELDS for e in items)
    assert [e["id"] for e in items] == sorted((e["id"] for e in items), reverse=True)
    updated = items[2]
    assert (updated["entity_type"], updated["entity_id"]) == ("employee", a["id"])
    assert updated["actor_id"] == PAYROLL_ADMIN
    assert updated["details"] == {
        "fields": ["bank_account"],
        "account_last4": {"old": a["account_last4"], "new": "6789"},
    }
    assert items[1]["actor_id"] == ADMIN
    text = json.dumps(items)
    for secret in ("55556789", "99991234", "Ann", "ann@example.com"):
        assert secret not in text


async def test_filters(api: Api) -> None:
    """PAY-203: entity_type, entity_id and event filter the list, alone or together."""
    a, b = await _seed(api)
    items, _ = await _all(api, "&entity_type=employee")
    assert len(items) == 3
    items, _ = await _all(api, f"&entity_id={a['id']}")
    assert [e["event"] for e in items] == ["employee.updated", "employee.created"]
    items, _ = await _all(api, f"&entity_id={b['id']}&event=employee.created")
    assert [e["entity_id"] for e in items] == [b["id"]]
    items, _ = await _all(api, "&event=settings.updated&entity_type=company_settings")
    assert len(items) == 1
    items, _ = await _all(api, "&event=payout.retry_requested")
    assert items == []


async def test_admin_only_scoped_and_validated(api: Api, db: Any) -> None:
    """PAY-601, PAY-602, C14: payroll_admin and approver get 403; another company sees nothing;
    a bad cursor, an unknown or in-scope-missing cursor ID, a bad filter or limit → 422."""
    await _seed(api)
    for headers in (api.payroll_admin(), api.approver()):
        r = await api.client.get("/v1/audit-events", headers=headers)
        assert (r.status_code, r.json()["error"]["code"]) == (403, "forbidden_role")
    other = await other_company(db)
    r = await api.client.get("/v1/audit-events", headers=api.admin(cid=other))
    assert r.json() == {"items": [], "next_cursor": None}
    mine = (await api.client.get("/v1/audit-events?limit=1", headers=api.admin())).json()
    r = await api.client.get(
        f"/v1/audit-events?cursor={mine['next_cursor']}", headers=api.admin(cid=other)
    )
    assert r.json()["error"]["details"] == [{"field": "query.cursor", "issue": "invalid_cursor"}]
    for query, field in (
        ("cursor=nope", "query.cursor"),
        (f"cursor={encode_cursor(uuid4())}", "query.cursor"),
        ("entity_type=user", "query.entity_type"),
        ("entity_id=not-a-uuid", "query.entity_id"),
        ("event=Bad Event", "query.event"),
        ("limit=0", "query.limit"),
    ):
        r = await api.client.get(f"/v1/audit-events?{query}", headers=api.admin())
        assert r.status_code == 422, query
        assert [d["field"] for d in r.json()["error"]["details"]] == [field]
