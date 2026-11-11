"""``/livez`` and ``/readyz`` (identity LLD §3.4; PAY-605)."""

from __future__ import annotations

from typing import Any

from tests.integration.conftest import HarnessFactory, free_port


async def test_ready_when_db_redis_and_migrations_are_ok(harness: HarnessFactory) -> None:
    """PAY-605: /readyz checks Postgres, Redis and alembic_version = head."""
    async with harness() as h:
        r = await h.client.get("/readyz")
        assert r.status_code == 200
        assert r.json() == {
            "status": "ok",
            "checks": {"db": "ok", "migrations": "ok", "redis": "ok"},
        }
        assert (await h.client.get("/livez")).status_code == 200


async def test_not_ready_when_migrations_are_not_at_head(
    harness: HarnessFactory, admin_db: Any
) -> None:
    """PAY-605: /readyz is 503 until identity.alembic_version equals the script head."""
    async with harness() as h:
        await admin_db.execute("UPDATE identity.alembic_version SET version_num = 'older'")
        try:
            r = await h.client.get("/readyz")
        finally:
            await admin_db.execute(
                "UPDATE identity.alembic_version SET version_num = '0001_initial'"
            )
        assert r.status_code == 503
        assert r.json()["checks"]["migrations"] == "fail"
        assert (await h.client.get("/livez")).status_code == 200


async def test_not_ready_when_db_or_redis_is_down(harness: HarnessFactory) -> None:
    """PAY-605: /readyz is 503 with the DB or Redis unreachable; /livez stays up."""
    async with harness(db_port=free_port(), redis_port=free_port()) as h:
        r = await h.client.get("/readyz")
        assert r.status_code == 503
        assert r.json()["checks"] == {"db": "fail", "migrations": "fail", "redis": "fail"}
        assert (await h.client.get("/livez")).status_code == 200
