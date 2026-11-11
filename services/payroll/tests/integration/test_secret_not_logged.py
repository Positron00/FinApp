"""No personal data or secrets in payroll's logs (design §7.1, §9.2; PAY-NFR-07)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from payroll.domain.calc import apply_bps, salaried_gross
from pycommon.testing import JsonLogCapture

from tests.helpers import STRONG_SECRET, VALID_ROUTING
from tests.integration.conftest import Api

pytestmark = pytest.mark.integration

NAME = "Zelda Quixote"
EMAIL = "zelda.quixote@example.com"
ACCOUNT = "98765432109"
SALARY = 7_777_777_31
STAFF_NAMES = ("Carl Calculator", "Grace Approver")


async def test_full_flow_logs_no_personal_data_or_secrets(
    api: Api, caplog_json: JsonLogCapture
) -> None:
    """PAY-NFR-07: over settings PUT → create employee → create run → calculate → approve →
    instruction → result (and a rejected request with a body), the captured JSON logs hold no
    salary or amount, employee or staff name, email, routing or account number, field key,
    DB password or JWT."""
    r = await api.client.put(
        "/v1/company/settings",
        json={
            "name": "Acme", "tax_rate_bps": 2000, "retirement_bps": 500,
            "timezone": "America/New_York", "biweekly_anchor": "2026-01-05",
            "require_dual_approval": True,
        },
        headers=api.admin(),
    )  # fmt: skip
    assert r.status_code == 200
    await api.create_employee(
        name=NAME, email=EMAIL, annual_salary_cents=SALARY, bank_account=ACCOUNT
    )
    dup = await api.client.post(
        "/v1/employees",
        json={"name": NAME, "email": EMAIL.upper(), "pay_type": "salaried",
              "annual_salary_cents": SALARY, "schedule": "monthly",
              "bank_routing": VALID_ROUTING, "bank_account": ACCOUNT},
        headers=api.payroll_admin(),
    )  # fmt: skip
    assert dup.status_code == 409
    run = await api.create_run()
    await api.calculate(run["id"], name=STAFF_NAMES[0])
    approve_headers = api.approver(name=STAFF_NAMES[1])
    r = await api.approve(run["id"], headers=approve_headers)
    assert r.status_code == 200
    [payout] = await api.payouts(run["id"])
    worker = api.worker()
    instr = await api.client.get(
        f"/v1/internal/payouts/{payout['id']}/instruction?attempt=1", headers=worker
    )
    assert instr.json()["account"] == ACCOUNT  # the test really served the secret values
    settled = {"attempt": 1, "outcome": "settled", "bank_reference": "BR-1"}
    assert (await api.result(payout["id"], settled)).status_code == 200

    gross = salaried_gross(SALARY, 1, 12)
    net = gross - apply_bps(gross, 2000) - apply_bps(gross, 500)
    keys: dict[str, Any] = json.loads(api.rt.settings.payroll_field_keys.get_secret_value())
    forbidden = {
        "salary": str(SALARY),
        "gross": str(gross),
        "net": str(net),
        "employee name": NAME,
        "email": EMAIL.split("@", maxsplit=1)[0],
        "routing": VALID_ROUTING,
        "account": ACCOUNT,
        "db password": STRONG_SECRET,
        "jwt": approve_headers["Authorization"].removeprefix("Bearer "),
        "worker jwt": worker["Authorization"].removeprefix("Bearer "),
        **{f"staff name {n}": n for n in STAFF_NAMES},
        **{f"field key {kid}": value for kid, value in keys.items()},
    }
    text = caplog_json.text
    assert caplog_json.events("payrun.approved"), "the flow was logged at all"
    leaked = sorted(label for label, value in forbidden.items() if value in text)
    assert leaked == []
