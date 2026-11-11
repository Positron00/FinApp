"""Field-key rotation while employees exist (design §3.6; PAY-204, ADR 0004).

The app restarts with a second key added and made active. Old-kid ciphertexts stay readable
(approve decrypts them into the payout snapshot, the worker's instruction decrypts that), and
every new write uses the active kid. Untouched values keep their old kid: there's no re-key pass.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID

import pytest
from payroll.crypto import employee_aad, snapshot_aad
from pydantic import SecretStr

from tests.helpers import VALID_ROUTING, field_keys_json
from tests.integration.conftest import Api, ApiFactory

pytestmark = pytest.mark.integration

OLD, NEW = "k2026a", "k2026b"
ACCOUNT_A, ACCOUNT_B, ACCOUNT_C = "000111222333", "000444555666", "000777888999"


def _keys(*kids: str, material: dict[str, bytes]) -> dict[str, Any]:
    return {
        "payroll_field_keys": SecretStr(field_keys_json({k: material[k] for k in kids})),
        "payroll_field_active_kid": kids[-1],
    }


async def _row(db: Any, emp_id: str) -> Any:
    return await db.fetchrow("SELECT * FROM payroll.employees WHERE id = $1", UUID(emp_id))


async def test_rotation_with_existing_employees(api_factory: ApiFactory, db: Any) -> None:
    """PAY-204: employees written under the old kid are still read, approved and paid after the
    active kid moves; a PATCHed bank field and a new employee use the new kid; the untouched
    field keeps the old one; the payout snapshot is written under the new kid."""
    material = {OLD: os.urandom(32), NEW: os.urandom(32)}
    before = await api_factory(**_keys(OLD, material=material))
    a = await before.create_employee(name="Ann", email="ann@example.com", bank_account=ACCOUNT_A)
    b = await before.create_employee(name="Ben", email="ben@example.com", bank_account=ACCOUNT_B)
    assert (await _row(db, a["id"]))["bank_account_enc"].startswith(f"{OLD}:")

    after: Api = await api_factory(**_keys(OLD, NEW, material=material))
    assert after.rt.keyring.active_kid == NEW
    r = await after.client.get(f"/v1/employees/{a['id']}", headers=after.admin())
    assert r.status_code == 200
    assert r.json()["account_last4"] == ACCOUNT_A[-4:]

    r = await after.client.patch(
        f"/v1/employees/{b['id']}", json={"bank_account": ACCOUNT_C},
        headers=after.payroll_admin(),
    )  # fmt: skip
    assert r.status_code == 200, r.text
    row_b = await _row(db, b["id"])
    ring = after.rt.keyring
    assert ring.kid_of(row_b["bank_account_enc"]) == NEW
    assert ring.kid_of(row_b["bank_routing_enc"]) == OLD  # not supplied: not re-encrypted
    assert ring.decrypt(row_b["bank_routing_enc"], aad=employee_aad("bank_routing", row_b["id"])) \
        == VALID_ROUTING  # fmt: skip
    c = await after.create_employee(name="Cy", email="cy@example.com")
    row_c = await _row(db, c["id"])
    assert {ring.kid_of(row_c["bank_account_enc"]), ring.kid_of(row_c["bank_routing_enc"])} == {NEW}

    run = await after.create_run()
    await after.calculate(run["id"])
    approved = await after.approve(run["id"])
    assert approved.status_code == 200, approved.text
    payouts = {p["employee_id"]: p for p in await after.payouts(run["id"])}
    worker = after.worker()
    for emp_id, account in ((a["id"], ACCOUNT_A), (b["id"], ACCOUNT_C)):
        payout = payouts[emp_id]
        snap = await db.fetchval(
            "SELECT bank_snapshot_enc FROM payroll.payout_attempts "
            "WHERE payout_id = $1 AND attempt = 1",
            UUID(payout["id"]),
        )
        assert ring.kid_of(snap) == NEW
        assert account in ring.decrypt(snap, aad=snapshot_aad(UUID(payout["id"]), 1))
        r = await after.client.get(
            f"/v1/internal/payouts/{payout['id']}/instruction?attempt=1", headers=worker
        )
        assert r.status_code == 200, r.text
        assert (r.json()["account"], r.json()["routing"]) == (account, VALID_ROUTING)


async def test_dropping_a_kid_still_in_use_is_a_500_not_a_leak(
    api_factory: ApiFactory, db: Any
) -> None:
    """PAY-204: if an old kid is removed while ciphertexts still use it, approve can't decrypt:
    500 internal_error, no payouts are created, and the response holds no bank digits."""
    material = {OLD: os.urandom(32), NEW: os.urandom(32)}
    before = await api_factory(**_keys(OLD, material=material))
    await before.create_employee(bank_account=ACCOUNT_A)
    run = await before.create_run()
    await before.calculate(run["id"])
    only_new = await api_factory(**_keys(NEW, material=material))
    r = await only_new.approve(run["id"])
    assert (r.status_code, r.json()["error"]["code"]) == (500, "internal_error")
    assert ACCOUNT_A not in r.text
    assert await db.fetchval("SELECT count(*) FROM payroll.payouts") == 0
