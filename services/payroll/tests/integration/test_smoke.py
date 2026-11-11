"""The skeleton flow end to end: settings → employees → run → calculate → approve → result."""

from __future__ import annotations

from typing import Any

import pytest

from tests.integration.conftest import Api

pytestmark = pytest.mark.integration


async def test_walking_skeleton(api: Api, db: Any) -> None:
    """PAY-200, 201, 300, 302, 400, 405, 406: one monthly run from creation to paid."""
    r = await api.client.put(
        "/v1/company/settings",
        json={
            "name": "Acme",
            "tax_rate_bps": 2000,
            "retirement_bps": 500,
            "timezone": "America/New_York",
            "biweekly_anchor": "2026-01-05",
            "require_dual_approval": True,
        },
        headers=api.admin(),
    )
    assert r.status_code == 200, r.text
    run = await api.approved_run(2)
    assert run["status"] == "processing"
    assert run["counts"]["requested"] == 2
    for p in await api.payouts(run["id"]):
        r = await api.result(p["id"], {"attempt": 1, "outcome": "settled", "bank_reference": "b1"})
        assert r.status_code == 200, r.text
        assert r.json() == {"payout_id": p["id"], "attempt": 1, "outcome": "settled"}
    r = await api.client.get(f"/v1/pay-runs/{run['id']}", headers=api.approver())
    assert r.json()["status"] == "paid"
    assert r.json()["counts"]["paid"] == 2
