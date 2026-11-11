"""``/livez`` and ``/readyz`` (design §3.5; PAY-605, R6)."""

from __future__ import annotations

from typing import Any

from tests.integration.conftest import Harness, free_port


async def test_ready_checks_db_and_migrations_only(harness: Harness) -> None:
    """PAY-605, R6: /readyz checks the DB and the Alembic head; no Redis check."""
    async with harness() as (_, client):
        r = await client.get("/readyz")
        assert r.status_code == 200
        assert r.json()["checks"] == {"db": "ok", "migrations": "ok"}


async def test_not_ready_when_db_down(harness: Harness) -> None:
    """PAY-605: DB unreachable → 503; /livez stays 200."""
    async with harness(db_port=free_port()) as (_, client):
        r = await client.get("/readyz")
        assert r.status_code == 503
        assert r.json()["checks"] == {"db": "fail", "migrations": "fail"}
        assert (await client.get("/livez")).status_code == 200


async def test_not_ready_when_migrations_not_at_head(harness: Harness, admin_db: Any) -> None:
    """PAY-605: bank_stub.alembic_version ≠ head → 503."""
    async with harness() as (_, client):
        await admin_db.execute("UPDATE bank_stub.alembic_version SET version_num = 'older'")
        try:
            r = await client.get("/readyz")
        finally:
            await admin_db.execute(
                "UPDATE bank_stub.alembic_version SET version_num = '0001_bank_stub_transfers'"
            )
        assert r.status_code == 503
        assert r.json()["checks"]["migrations"] == "fail"
