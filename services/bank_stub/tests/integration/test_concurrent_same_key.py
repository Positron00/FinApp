"""Concurrent POSTs with one key through two app instances (design §3.1 step 7; PAY-412,
PAY-NFR-02, R6)."""

from __future__ import annotations

import asyncio
from typing import Any

from pycommon.testing import OtelMemory

from tests.helpers import KEY, body, headers
from tests.integration.conftest import Harness


def _counts(otel: OtelMemory) -> dict[str, float]:
    return {str(a["outcome"]): float(v) for a, v in otel.metric_points("bank_stub_transfers")}


async def test_fifty_concurrent_posts_one_row(
    harness: Harness, admin_db: Any, otel_memory: OtelMemory
) -> None:
    """PAY-412: 50 concurrent POSTs, two replicas (separate engines and pools) → one row, one
    201, the same transfer_id/status/bank_reference everywhere; the counter grows by one
    settled and 49 replayed."""
    async with harness() as (_, c1), harness() as (_, c2):
        clients = [c1, c2]
        responses = await asyncio.gather(
            *(
                clients[i % 2].post("/v1/transfers", json=body(), headers=headers())
                for i in range(50)
            )
        )
    statuses = [r.status_code for r in responses]
    assert statuses.count(201) == 1
    assert statuses.count(200) == 49
    bodies = {
        (r.json()["transfer_id"], r.json()["status"], r.json()["bank_reference"]) for r in responses
    }
    assert len(bodies) == 1
    assert (
        await admin_db.fetchval(
            "SELECT count(*) FROM bank_stub.transfers WHERE idempotency_key = $1", KEY
        )
        == 1
    )
    assert _counts(otel_memory) == {"settled": 1.0, "replayed": 49.0}


async def test_metric_outcomes(harness: Harness, otel_memory: OtelMemory) -> None:
    """PAY-412, R6: bank_stub_transfers_total{outcome} for created, rejected and replayed."""
    async with harness() as (_, client):
        await client.post("/v1/transfers", json=body(), headers=headers(key="m:1"))
        await client.post("/v1/transfers", json=body("11110000"), headers=headers(key="m:2"))
        await client.post("/v1/transfers", json=body(), headers=headers(key="m:1"))
        await client.get("/v1/transfers/m:1", headers=headers(None))  # GET isn't counted
    assert _counts(otel_memory) == {"settled": 1.0, "rejected": 1.0, "replayed": 1.0}
