"""The transfers API against Postgres (design §3.1, §3.2; PAY-412, R6, C14, C34)."""

from __future__ import annotations

from typing import Any

from tests.helpers import CLOSED_ACCOUNT, KEY, body, headers
from tests.integration.conftest import Harness

FIELDS = {"transfer_id", "status", "reason", "bank_reference"}


async def test_new_settled_then_replay_with_any_body(harness: Harness, admin_db: Any) -> None:
    """PAY-412, R6: 201 settled with a reference; a repeated key → 200, same transfer_id,
    even with a different or invalid body; one ledger row."""
    async with harness() as (_, client):
        r1 = await client.post("/v1/transfers", json=body(), headers=headers())
        assert r1.status_code == 201
        first = r1.json()
        assert set(first) == FIELDS
        assert first["status"] == "settled"
        assert first["bank_reference"].startswith("BSR-")
        r2 = await client.post("/v1/transfers", json=body(amount=7), headers=headers())
        r3 = await client.post("/v1/transfers", content=b"{oops", headers=headers())
        for r in (r2, r3):
            assert r.status_code == 200
            assert r.json() == first
    row = await admin_db.fetchrow("SELECT * FROM bank_stub.transfers")
    assert row["account_last4"] == "6789"
    assert row["amount_cents"] == 412_345
    assert row["created_at"] == row["updated_at"]
    assert await admin_db.fetchval("SELECT count(*) FROM bank_stub.transfers") == 1


async def test_rejected_account_closed(harness: Harness) -> None:
    """PAY-412: account …0000 → 201 rejected account_closed (a rejection is still 2xx)."""
    async with harness() as (_, client):
        r = await client.post("/v1/transfers", json=body(CLOSED_ACCOUNT), headers=headers())
        assert r.status_code == 201
        assert r.json()["reason"] == "account_closed"


async def test_failure_rate_one_rejects(harness: Harness) -> None:
    """PAY-412: BANK_STUB_FAILURE_RATE=1 → insufficient_funds_at_bank."""
    async with harness(bank_stub_failure_rate=1.0) as (_, client):
        r = await client.post("/v1/transfers", json=body(), headers=headers())
        assert r.json()["reason"] == "insufficient_funds_at_bank"


async def test_get_200_and_404(harness: Harness) -> None:
    """PAY-412: GET by key → 200 with the four fields, 404 transfer_not_found."""
    async with harness() as (_, client):
        missing = await client.get(f"/v1/transfers/{KEY}", headers=headers(None))
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "transfer_not_found"
        created = (await client.post("/v1/transfers", json=body(), headers=headers())).json()
        found = await client.get(f"/v1/transfers/{KEY}", headers=headers(None))
        assert found.status_code == 200
        assert found.json() == created


async def test_errors_on_real_app(harness: Harness) -> None:
    """PAY-412, C34, C14: 401 without the API key, 400 without Idempotency-Key, 422 for a
    malformed key and for an invalid body on a new key (no values echoed)."""
    async with harness() as (_, client):
        no_key = await client.post("/v1/transfers", json=body(), headers=headers(api_key=None))
        assert no_key.status_code == 401
        no_idem = await client.post("/v1/transfers", json=body(), headers=headers(key=None))
        assert no_idem.json()["error"]["code"] == "idempotency_key_required"
        bad_idem = await client.post("/v1/transfers", json=body(), headers=headers(key="a b"))
        assert bad_idem.status_code == 422
        bad_body = await client.post(
            "/v1/transfers", json=body(account="12"), headers=headers(key="new:1")
        )
        assert bad_body.status_code == 422
        assert bad_body.json()["error"]["details"] == [
            {"field": "body.account", "issue": "string_pattern_mismatch"}
        ]
        assert '"12"' not in bad_body.text


async def test_db_down_is_503(harness: Harness) -> None:
    """Design §3.1: DB unreachable → 503 db_unavailable (pycommon mapping)."""
    from tests.integration.conftest import free_port  # noqa: PLC0415

    async with harness(db_port=free_port()) as (_, client):
        r = await client.post("/v1/transfers", json=body(), headers=headers())
        assert r.status_code == 503
        assert r.json()["error"]["code"] in {"db_unavailable", "db_timeout"}
