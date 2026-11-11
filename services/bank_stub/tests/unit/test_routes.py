"""Route behaviour over an in-memory repository (design §3.1 to §3.3, §3.6; PAY-412, C14, C34)."""

from __future__ import annotations

import hmac
import json
from typing import Any

import pytest

from tests.helpers import (
    CLOSED_ACCOUNT,
    KEY,
    body,
    headers,
    make_settings,
    stubbed,
)


@pytest.fixture
async def stub() -> Any:
    async with stubbed(make_settings(bank_stub_latency_ms=20)) as (app, client, rt):
        yield app, client, rt


async def test_post_new_then_replay(stub: Any) -> None:
    """PAY-412, R6: 201 for a new key, 200 with the same row for a repeated key."""
    _, client, rt = stub
    r1 = await client.post("/v1/transfers", json=body(), headers=headers())
    assert r1.status_code == 201
    out = r1.json()
    assert set(out) == {"transfer_id", "status", "reason", "bank_reference"}
    assert out["status"] == "settled"
    assert out["reason"] is None
    r2 = await client.post("/v1/transfers", content=b"not json", headers=headers())
    assert r2.status_code == 200
    assert r2.json() == out
    assert len(rt.repo.inserted) == 1


async def test_closed_account_rejected(stub: Any) -> None:
    """PAY-412: account ending 0000 → 201 rejected / account_closed, no reference."""
    _, client, _ = stub
    r = await client.post("/v1/transfers", json=body(CLOSED_ACCOUNT), headers=headers())
    assert r.status_code == 201
    assert r.json()["status"] == "rejected"
    assert r.json()["reason"] == "account_closed"
    assert r.json()["bank_reference"] is None


async def test_lost_insert_race_is_a_replay(stub: Any) -> None:
    """PAY-412: when ON CONFLICT finds a concurrent row, the answer is 200 with that row."""
    _, client, rt = stub
    rt.repo.lose_race = True
    r = await client.post("/v1/transfers", json=body(), headers=headers())
    assert r.status_code == 200


async def test_stored_key_only_no_account_digits(stub: Any) -> None:
    """PAY-NFR-07: only the last 4 digits of the account are stored; no routing."""
    _, client, rt = stub
    await client.post("/v1/transfers", json=body(), headers=headers())
    new = rt.repo.inserted[0]
    assert new.account_last4 == "6789"
    assert not hasattr(new, "routing")
    assert "6789" not in repr(new)


async def test_latency_sleep_on_post_and_get(stub: Any) -> None:
    """PAY-412: every /v1/transfers request awaits BANK_STUB_LATENCY_MS / 1000."""
    _, client, rt = stub
    await client.post("/v1/transfers", json=body(), headers=headers())
    await client.get(f"/v1/transfers/{KEY}", headers=headers(None))
    assert rt.sleep.calls == [0.02, 0.02]


async def test_no_sleep_before_auth_or_key_checks(stub: Any) -> None:
    """Design §3.6: the sleep comes after the API-key and key checks."""
    _, client, rt = stub
    await client.post("/v1/transfers", json=body(), headers=headers(api_key=None))
    await client.post("/v1/transfers", json=body(), headers=headers(key=None))
    await client.get("/v1/transfers/bad key", headers=headers(None))
    assert rt.sleep.calls == []


@pytest.mark.parametrize(("api_key", "reason"), [(None, "missing"), ("wrong", "mismatch")])
async def test_api_key_required(
    stub: Any, caplog_json: Any, api_key: str | None, reason: str
) -> None:
    """PAY-412: missing or wrong X-Api-Key → 401 invalid_api_key on both routes; no key logged."""
    _, client, _ = stub
    r1 = await client.post("/v1/transfers", json=body(), headers=headers(api_key=api_key))
    r2 = await client.get(f"/v1/transfers/{KEY}", headers=headers(None, api_key=api_key))
    for r in (r1, r2):
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "invalid_api_key"
    events = caplog_json.events("bank_stub.auth.rejected")
    assert [e["reason"] for e in events] == [reason, reason]
    assert "wrong" not in caplog_json.text or api_key is None


async def test_api_key_uses_compare_digest(stub: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """PAY-412: the API key is compared in constant time."""
    _, client, _ = stub
    calls: list[tuple[bytes, bytes]] = []
    original = hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return original(a, b)

    monkeypatch.setattr("bank_stub.api.auth.compare_digest", spy)  # module-local, not stdlib
    r = await client.get(f"/v1/transfers/{KEY}", headers=headers(None))
    assert r.status_code == 404
    assert len(calls) == 1


async def test_missing_idempotency_key_is_400(stub: Any) -> None:
    """C34: a missing Idempotency-Key → 400 idempotency_key_required."""
    _, client, _ = stub
    r = await client.post("/v1/transfers", json=body(), headers=headers(key=None))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "idempotency_key_required"


async def test_malformed_idempotency_key_is_422(stub: Any) -> None:
    """C34, C56: a malformed key → 422 validation_error at header.Idempotency-Key."""
    _, client, _ = stub
    r = await client.post("/v1/transfers", json=body(), headers=headers(key="bad key!"))
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "validation_error"
    assert err["details"] == [{"field": "header.Idempotency-Key", "issue": "pattern"}]


async def test_invalid_body_on_new_key_is_422_without_values(stub: Any) -> None:
    """C14: field names and error types only; the submitted values are never echoed.

    The values are checked in the body without its random ``request_id``: the old check of
    ``"-5"`` against the whole text failed whenever the request ID had a group starting with 5
    (the C61 flake)."""
    _, client, rt = stub
    bad = {"amount_cents": -987654, "currency": "USD", "routing": "98765", "account": "55556666"}
    r = await client.post("/v1/transfers", json=bad, headers=headers())
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "validation_error"
    fields = {d["field"] for d in err["details"]}
    assert fields == {"body.amount_cents", "body.routing"}
    echoed = json.dumps({k: v for k, v in err.items() if k != "request_id"})
    for value in ("98765", "-987654", "987654", "55556666"):
        assert value not in echoed
    assert rt.repo.inserted == []


async def test_invalid_json_on_new_key_is_422(stub: Any) -> None:
    """C14: unparseable JSON on a new key → 422 at "body"."""
    _, client, _ = stub
    r = await client.post("/v1/transfers", content=b"{", headers=headers())
    assert r.status_code == 422
    assert r.json()["error"]["details"][0]["field"] == "body"


async def test_get_found_not_found_and_malformed(stub: Any) -> None:
    """PAY-412: GET → 200 / 404 transfer_not_found; malformed key → 422 at path.key."""
    _, client, _ = stub
    r404 = await client.get(f"/v1/transfers/{KEY}", headers=headers(None))
    assert r404.status_code == 404
    assert r404.json()["error"]["code"] == "transfer_not_found"
    created = (await client.post("/v1/transfers", json=body(), headers=headers())).json()
    r200 = await client.get(f"/v1/transfers/{KEY}", headers=headers(None))
    assert r200.status_code == 200
    assert r200.json() == created
    bad = await client.get("/v1/transfers/a%20b", headers=headers(None))
    assert bad.status_code == 422
    newline = await client.get(f"/v1/transfers/{KEY}%0A", headers=headers(None))  # C63
    assert newline.status_code == 422
    assert bad.json()["error"]["details"] == [{"field": "path.key", "issue": "pattern"}]


async def test_health_needs_no_api_key(stub: Any) -> None:
    """PAY-605: /livez is open (no X-Api-Key)."""
    _, client, _ = stub
    assert (await client.get("/livez")).status_code == 200
