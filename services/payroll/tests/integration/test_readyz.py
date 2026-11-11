"""Health (design §4.11; PAY-605)."""

from __future__ import annotations

import pytest

from tests.integration.conftest import Api

pytestmark = pytest.mark.integration


async def test_readyz_checks_db_redis_and_migrations(api: Api) -> None:
    """PAY-605: /readyz is 200 with db, redis and migrations ok; /livez needs no token."""
    assert (await api.client.get("/livez")).json() == {"status": "ok"}
    r = await api.client.get("/readyz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "checks": {"db": "ok", "redis": "ok", "migrations": "ok"}}
