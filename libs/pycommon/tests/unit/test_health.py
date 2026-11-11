"""Liveness and readiness (platform LLD §3.6; PAY-605)."""

import asyncio
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pycommon.app import AppContext, create_app
from pycommon.health import (
    HealthCheck,
    MigrationsMismatchError,
    db_check,
    health_router,
    migrations_check,
    redis_check,
    task_check,
)
from pycommon.testing import JsonLogCapture, TokenFactory
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.helpers import make_settings


class FakeCheck:
    def __init__(self, name: str, *, fail: bool = False, delay: float = 0.0) -> None:
        self.name = name
        self.fail = fail
        self.delay = delay
        self.calls = 0

    async def __call__(self) -> None:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise ConnectionError("password=hunter2 at db:5432")


def _client(checks: Sequence[HealthCheck], timeout_s: float = 1.0) -> TestClient:
    app = FastAPI()
    app.include_router(health_router(checks, timeout_s=timeout_s))
    return TestClient(app)


def test_livez_always_ok() -> None:
    """PAY-605: /livez is 200 with no I/O, even when every check fails."""
    check = FakeCheck("db", fail=True)
    resp = _client([check]).get("/livez")
    assert (resp.status_code, resp.json()) == (200, {"status": "ok"})
    assert check.calls == 0


def test_readyz_ok_and_failing() -> None:
    """PAY-605: /readyz is 200 with per-check ok, or 503 with "fail" and no error text."""
    ok = _client([FakeCheck("db"), FakeCheck("redis")]).get("/readyz")
    assert ok.status_code == 200
    assert ok.json() == {"status": "ok", "checks": {"db": "ok", "redis": "ok"}}
    bad = _client([FakeCheck("db", fail=True), FakeCheck("redis")]).get("/readyz")
    assert bad.status_code == 503
    assert bad.json() == {"status": "fail", "checks": {"db": "fail", "redis": "ok"}}
    assert "hunter2" not in bad.text
    assert "ConnectionError" not in bad.text


def test_hanging_check_times_out_and_checks_run_concurrently() -> None:
    """PAY-605: each check runs under the timeout; checks run concurrently."""
    hanging = FakeCheck("db", delay=30)
    slow_a, slow_b = FakeCheck("redis", delay=0.3), FakeCheck("migrations", delay=0.3)
    client = _client([hanging, slow_a, slow_b], timeout_s=0.5)
    start = time.monotonic()
    resp = client.get("/readyz")
    elapsed = time.monotonic() - start
    assert resp.status_code == 503
    assert resp.json()["checks"] == {"db": "fail", "redis": "ok", "migrations": "ok"}
    assert elapsed < 1.0


def test_failures_are_logged_at_most_every_30_s(
    caplog_json: JsonLogCapture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PAY-605: health.check_failed {check, error_type} at warning, rate-limited per check."""
    now = [1000.0]
    monkeypatch.setattr("pycommon.health.time.monotonic", lambda: now[0])
    client = _client([FakeCheck("db", fail=True)])
    client.get("/readyz")
    client.get("/readyz")
    now[0] += 31
    client.get("/readyz")
    events = caplog_json.events("health.check_failed")
    assert [(e["check"], e["error_type"], e["level"]) for e in events] == [
        ("db", "ConnectionError", "warning")
    ] * 2
    assert "hunter2" not in caplog_json.text


def test_health_routes_have_no_auth(caplog_json: JsonLogCapture) -> None:
    """PAY-605: health routes need no JWT while other routers do; no http.request log for them."""
    tf = TokenFactory.create()
    router = APIRouter()

    @router.get("/v1/thing")
    async def thing() -> dict[str, str]:
        return {}

    app = create_app(
        make_settings(internal_jwt_public_keys=tf.keyset),
        routers=[router],
        use_db=False,
        use_redis=False,
        instrument=False,
        extra_health_checks=lambda ctx: [task_check("consumer", lambda: True)],
    )
    with TestClient(app) as client:
        assert client.get("/livez").status_code == 200
        ready = client.get("/readyz")
        assert ready.status_code == 200
        assert ready.json()["checks"] == {"consumer": "ok"}
        assert client.get("/v1/thing").status_code == 401
    routes = [e["route"] for e in caplog_json.events("http.request")]
    assert routes == ["/v1/thing"]


async def test_task_and_redis_checks() -> None:
    """PAY-605: task_check and redis_check raise on failure."""
    alive = {"v": True}
    check = task_check("consumer", lambda: alive["v"])
    assert check.name == "consumer"
    await check()
    alive["v"] = False
    with pytest.raises(RuntimeError):
        await check()

    class FakeRedis:
        def __init__(self, result: bool) -> None:
            self.result = result

        async def ping(self) -> bool:
            return self.result

    await redis_check(cast(Redis, FakeRedis(True)))()
    with pytest.raises(ConnectionError):
        await redis_check(cast(Redis, FakeRedis(False)))()


class FakeResult:
    def __init__(self, rows: list[str]) -> None:
        self.rows = rows

    def scalars(self) -> FakeResult:
        return self

    def all(self) -> list[str]:
        return self.rows


class FakeConn:
    def __init__(self, rows: list[str], log: list[str]) -> None:
        self.rows = rows
        self.log = log

    async def execute(self, stmt: Any) -> FakeResult:
        self.log.append(str(stmt))
        return FakeResult(self.rows)


class FakeEngine:
    def __init__(self, rows: list[str]) -> None:
        self.rows = rows
        self.log: list[str] = []

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[FakeConn]:
        yield FakeConn(self.rows, self.log)


def _scripts(tmp_path: Path) -> str:
    versions = tmp_path / "migrations" / "versions"
    versions.mkdir(parents=True)
    (tmp_path / "migrations" / "script.py.mako").write_text("")
    (versions / "a1_init.py").write_text('revision = "a1"\ndown_revision = None\n')
    (versions / "b2_next.py").write_text('revision = "b2"\ndown_revision = "a1"\n')
    return str(tmp_path / "migrations")


async def test_db_and_migration_checks(tmp_path: Path) -> None:
    """PAY-605: db_check runs SELECT 1; migrations_check compares the DB version with the head."""
    engine = FakeEngine(["b2"])
    await db_check(cast(AsyncEngine, engine))()
    assert engine.log == ["SELECT 1"]
    location = _scripts(tmp_path)
    at_head = migrations_check(
        cast(AsyncEngine, engine), schema="payroll", script_location=location
    )
    assert at_head.name == "migrations"
    await at_head()
    assert engine.log[-1] == 'SELECT version_num FROM "payroll".alembic_version'
    behind = migrations_check(
        cast(AsyncEngine, FakeEngine(["a1"])), schema="payroll", script_location=location
    )
    with pytest.raises(MigrationsMismatchError):
        await behind()
    with pytest.raises(ValueError, match="identifier"):
        migrations_check(cast(AsyncEngine, engine), schema='x"; drop', script_location=location)


def test_extra_checks_see_the_context() -> None:
    """PAY-605: extra_health_checks receives the AppContext built in the lifespan."""
    seen: list[AppContext] = []

    def extra(ctx: AppContext) -> list[HealthCheck]:
        seen.append(ctx)
        return [FakeCheck("custom", fail=True)]

    app = create_app(
        make_settings(),
        use_db=False,
        use_redis=False,
        use_jwt=False,
        instrument=False,
        extra_health_checks=extra,
    )
    with TestClient(app) as client:
        resp = client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["checks"] == {"custom": "fail"}
    assert seen
    assert seen[0].settings.service_name == "testsvc"
