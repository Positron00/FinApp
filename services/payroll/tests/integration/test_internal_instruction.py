"""``GET /v1/internal/payouts/{id}/instruction`` (design §4.6; PAY-411, PAY-410, C30)."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from pycommon.testing import JsonLogCapture

from tests.helpers import VALID_ROUTING
from tests.integration.conftest import Api, other_company

pytestmark = pytest.mark.integration


async def _payout(api: Api) -> dict[str, Any]:
    run = await api.approved_run(1, bank_account="00099988877")
    [p] = await api.payouts(run["id"])
    return p


async def _get(api: Api, pid: str, attempt: Any = 1, **kw: Any) -> Any:
    q = f"?attempt={attempt}" if attempt is not None else ""
    return await api.client.get(
        f"/v1/internal/payouts/{pid}/instruction{q}", headers=kw.get("headers") or api.worker()
    )


async def test_current_requested_attempt_is_served(api: Api, db: Any) -> None:
    """PAY-411: 200 with decrypted routing and account, amount, requested_at; no-store."""
    p = await _payout(api)
    r = await _get(api, p["id"])
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert set(body) == {
        "payout_id", "attempt", "amount_cents", "currency", "routing", "account", "requested_at",
    }  # fmt: skip
    assert (body["routing"], body["account"]) == (VALID_ROUTING, "00099988877")
    assert (body["amount_cents"], body["currency"], body["attempt"]) == (
        p["amount_cents"],
        "USD",
        1,
    )
    assert body["requested_at"] == p["attempts"][0]["requested_at"]


async def test_indeterminate_is_served_and_final_or_blocked_is_409(api: Api) -> None:
    """PAY-411: indeterminate is deliverable; settled and needs_operator → 409."""
    p = await _payout(api)
    await api.result(p["id"], {"attempt": 1, "outcome": "indeterminate"})
    assert (await _get(api, p["id"])).status_code == 200
    await api.result(p["id"], {"attempt": 1, "outcome": "poison", "reason": "amount_mismatch"})
    r = await _get(api, p["id"])
    assert r.json()["error"] == {
        **r.json()["error"],
        "code": "attempt_not_current",
        "details": {"current_attempt": 1},
    }
    await api.result(p["id"], {"attempt": 1, "outcome": "settled", "bank_reference": "x"})
    assert (await _get(api, p["id"])).status_code == 409


async def test_stale_attempt_is_409_and_logged(api: Api, caplog_json: JsonLogCapture) -> None:
    """PAY-411: a non-current attempt → 409 with current_attempt, logged (source instruction)."""
    p = await _payout(api)
    r = await _get(api, p["id"], attempt=2)
    assert (r.status_code, r.json()["error"]["details"]) == (409, {"current_attempt": 1})
    [event] = caplog_json.events("payout.attempt_not_current")
    assert event["source"] == "instruction"


async def test_not_found_and_other_company(api: Api, db: Any) -> None:
    """PAY-411: unknown payout or another cid → 404 payout_not_found."""
    p = await _payout(api)
    other = await other_company(db)
    r = await _get(api, p["id"], headers=api.worker(cid=other))
    assert (r.status_code, r.json()["error"]["code"]) == (404, "payout_not_found")
    r = await _get(api, str(uuid4()))
    assert r.status_code == 404


@pytest.mark.parametrize("attempt", [None, 0, "x"])
async def test_bad_attempt_is_422(api: Api, attempt: Any) -> None:
    """PAY-411: a missing or invalid attempt → 422 validation_error."""
    r = await _get(api, str(uuid4()), attempt=attempt)
    assert (r.status_code, r.json()["error"]["code"]) == (422, "validation_error")


async def test_issuer_rules(api: Api) -> None:
    """PAY-411, C30: a web token → 401; a worker-issued token with another sub → 403."""
    p = await _payout(api)
    r = await _get(api, p["id"], headers=api.approver())
    assert (r.status_code, r.json()["error"]["code"]) == (401, "invalid_token")
    bad = api.headers("service:web", (), issuer="payout-worker")
    r = await _get(api, p["id"], headers=bad)
    assert (r.status_code, r.json()["error"]["code"]) == (403, "forbidden_role")


async def test_tampered_snapshot_is_500(api: Api, db: Any, caplog_json: JsonLogCapture) -> None:
    """PAY-204: a snapshot that fails to decrypt → 500 internal_error and crypto.decrypt_failed."""
    p = await _payout(api)
    await db.execute(
        "UPDATE payroll.payout_attempts SET bank_snapshot_enc = "
        "regexp_replace(bank_snapshot_enc, ':[^:]*$', ':AAAAAAAAAAAAAAAAAAAAAAAA')"
    )
    r = await _get(api, p["id"])
    assert (r.status_code, r.json()["error"]["code"]) == (500, "internal_error")
    [event] = caplog_json.events("crypto.decrypt_failed")
    assert event["entity"] == "payout_attempt"
    assert "00099988877" not in caplog_json.text
