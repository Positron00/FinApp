"""Concurrent approves (PAY-400; design §4.8, §4.10)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.integration.conftest import Api

pytestmark = pytest.mark.integration


async def test_two_parallel_approves_with_different_keys_one_wins(api: Api, db: Any) -> None:
    """PAY-400: exactly one 200 and one 409 pay_run_not_approvable; one set of payouts."""
    run = await api.calculated_run(3)
    a, b = await asyncio.gather(api.approve(run["id"], "key-a"), api.approve(run["id"], "key-b"))
    assert sorted((a.status_code, b.status_code)) == [200, 409]
    loser = a if a.status_code == 409 else b
    assert loser.json()["error"]["code"] == "pay_run_not_approvable"
    assert await db.fetchval("SELECT count(*) FROM payroll.payouts") == 3
    assert await db.fetchval("SELECT count(*) FROM payroll.outbox") == 3


async def test_two_parallel_approves_with_the_same_key_both_succeed(api: Api, db: Any) -> None:
    """PAY-400: same key → both 200 with the same body (one replayed); one set of payouts."""
    run = await api.calculated_run(3)
    a, b = await asyncio.gather(api.approve(run["id"], "same"), api.approve(run["id"], "same"))
    assert (a.status_code, b.status_code) == (200, 200)
    assert a.json() == b.json()
    replayed = sorted(str(r.headers.get("idempotent-replayed")) for r in (a, b))
    assert replayed == ["None", "true"]
    assert await db.fetchval("SELECT count(*) FROM payroll.payouts") == 3
