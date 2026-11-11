"""``/livez`` and ``/readyz`` (design §3.7; PAY-605)."""

from __future__ import annotations

import socket
from typing import Any

import httpx

from tests.integration.conftest import Stack, lifespan, runtime


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
    return port


async def _readyz(app: Any) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://w") as c:
        return await c.get("/readyz")


async def test_ready_then_not_ready_while_stopping(stack: Stack, admin_db: Any) -> None:
    """PAY-605: /readyz needs db, migrations, redis, the consumer task and not stopping; 503
    once the consumer has stopped; 503 when migrations aren't at head."""
    app = stack.build_worker()
    async with lifespan(app):
        r = await _readyz(app)
        assert r.status_code == 200, r.text
        assert r.json()["checks"] == {
            "db": "ok",
            "migrations": "ok",
            "redis": "ok",
            "consumer": "ok",
            "stopping": "ok",
        }
        await admin_db.execute("UPDATE payouts.alembic_version SET version_num = 'older'")
        try:
            assert (await _readyz(app)).json()["checks"]["migrations"] == "fail"
        finally:
            await admin_db.execute(
                "UPDATE payouts.alembic_version SET version_num = '0001_payouts_initial'"
            )
        await runtime(app).consumer.stop()
        r = await _readyz(app)
        assert r.status_code == 503
        assert r.json()["checks"]["stopping"] == "fail"
        assert r.json()["checks"]["consumer"] == "fail"


async def test_not_ready_when_db_and_redis_down(stack: Stack) -> None:
    """PAY-605: Postgres or Redis unreachable → 503; /livez stays 200."""
    app = stack.build_worker(db_port=free_port(), redis_port=free_port())
    async with lifespan(app):
        r = await _readyz(app)
        assert r.status_code == 503
        checks = r.json()["checks"]
        assert (checks["db"], checks["redis"], checks["migrations"]) == ("fail", "fail", "fail")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://w"
        ) as c:
            assert (await c.get("/livez")).status_code == 200
