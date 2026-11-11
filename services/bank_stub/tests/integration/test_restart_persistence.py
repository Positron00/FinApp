"""The ledger survives an app restart (design §2.1, §3.1; PAY-412, PAY-NFR-02)."""

from __future__ import annotations

from tests.helpers import KEY, body, headers
from tests.integration.conftest import Harness


async def test_restart_keeps_the_ledger(harness: Harness) -> None:
    """PAY-412: POST, shut the app down (engine disposed), build a new one → GET returns the
    same row and a repeated POST returns 200 with the same transfer_id."""
    async with harness() as (_, client):
        first = (await client.post("/v1/transfers", json=body(), headers=headers())).json()
    async with harness() as (_, client):
        got = await client.get(f"/v1/transfers/{KEY}", headers=headers(None))
        assert got.status_code == 200
        assert got.json() == first
        again = await client.post("/v1/transfers", json=body(), headers=headers())
        assert again.status_code == 200
        assert again.json()["transfer_id"] == first["transfer_id"]
