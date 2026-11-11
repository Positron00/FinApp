"""Idempotency-Key handling (design §4.8; PAY-400, PAY-405, C34)."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from tests.integration.conftest import Api

pytestmark = pytest.mark.integration


async def test_replay_returns_the_stored_response(api: Api, db: Any) -> None:
    """PAY-400: a repeat with the same key returns the original 200 body, Idempotent-Replayed."""
    run = await api.calculated_run(1)
    first = await api.approve(run["id"], "k-1")
    second = await api.approve(run["id"], "k-1")
    assert second.status_code == 200
    assert second.json() == first.json()
    assert second.headers["idempotent-replayed"] == "true"
    assert "idempotent-replayed" not in first.headers
    assert await db.fetchval("SELECT count(*) FROM payroll.payouts") == 1


async def test_same_key_on_another_run_does_not_collide(api: Api) -> None:
    """PAY-400 (§4.8): the scope holds the path params, so a key reused on another run is new."""
    await api.create_employee()
    runs = [await api.create_run("monthly", f"2026-0{m}-01") for m in (1, 2)]
    for run in runs:
        await api.calculate(run["id"])
        assert (await api.approve(run["id"], "shared-key")).status_code == 200


async def test_expired_key_is_treated_as_new(api: Api, db: Any) -> None:
    """PAY-400: past expires_at the key is deleted and re-evaluated (here: 409 not approvable)."""
    run = await api.calculated_run(1)
    assert (await api.approve(run["id"], "old")).status_code == 200
    await db.execute("UPDATE payroll.idempotency_keys SET expires_at = now() - interval '1h'")
    r = await api.approve(run["id"], "old")
    assert (r.status_code, r.json()["error"]["code"]) == (409, "pay_run_not_approvable")


async def test_errors_are_not_stored(api: Api, db: Any) -> None:
    """PAY-400: a 4xx rolls back the key; the same key later succeeds."""
    await api.create_employee()
    run = await api.create_run()
    assert (await api.approve(run["id"], "retry-me")).status_code == 409  # draft
    assert await db.fetchval("SELECT count(*) FROM payroll.idempotency_keys") == 0
    await api.calculate(run["id"])
    assert (await api.approve(run["id"], "retry-me")).status_code == 200


async def test_result_key_rules(api: Api) -> None:
    """PAY-405, C34: missing key → 400; malformed → 422; a different body under one key → 422
    idempotency_key_reused; <event_id>:indeterminate then <event_id>:settled both apply."""
    run = await api.approved_run(1)
    [payout] = await api.payouts(run["id"])
    url = f"/v1/internal/payouts/{payout['id']}/result"
    body = {"attempt": 1, "outcome": "indeterminate"}
    r = await api.client.post(url, json=body, headers=api.worker())
    assert (r.status_code, r.json()["error"]["code"]) == (400, "idempotency_key_required")
    r = await api.client.post(url, json=body, headers={**api.worker(), "Idempotency-Key": "a b"})
    assert r.json()["error"]["details"] == [
        {"field": "header.Idempotency-Key", "issue": "string_pattern_mismatch"}
    ]
    event = uuid4()
    r = await api.result(payout["id"], body, key=f"{event}:indeterminate")
    assert r.json()["outcome"] == "indeterminate"
    replay = await api.result(payout["id"], body, key=f"{event}:indeterminate")
    assert replay.headers["idempotent-replayed"] == "true"
    reused = await api.result(
        payout["id"], {"attempt": 1, "outcome": "poison"}, key=f"{event}:indeterminate"
    )
    assert (reused.status_code, reused.json()["error"]["code"]) == (422, "idempotency_key_reused")
    settled = {"attempt": 1, "outcome": "settled", "bank_reference": "ref"}
    r = await api.result(payout["id"], settled, key=f"{event}:settled")
    assert r.json()["outcome"] == "settled"
