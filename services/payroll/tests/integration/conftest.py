"""Integration fixtures (design §9.2): Testcontainers Postgres 16 and Redis 7 via pycommon.

The bootstrap SQL and Redis ACLs are the real ``deploy/compose`` assets; migrations run as
``payroll_migrator``; the app connects as ``payroll_app`` and the Redis ACL user ``payroll``
(C1, C2). Every test starts from truncated tables, the seeded settings row and an empty Redis.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import asyncpg  # type: ignore[import-untyped]
import httpx
import pytest
import redis.asyncio as aioredis
from fastapi import FastAPI
from payroll.main import build_app
from payroll.services.runtime import Runtime
from pycommon.testing import PgBootstrapped, RedisAcl, TokenFactory
from pydantic import SecretStr

from tests.helpers import COMPANY_ID, OTHER_COMPANY_ID, employee_body, make_settings

PAYROLL_ADMIN = str(UUID("0193a000-0000-7000-8000-0000000000a1"))
APPROVER = str(UUID("0193a000-0000-7000-8000-0000000000a2"))
ADMIN = str(UUID("0193a000-0000-7000-8000-0000000000a3"))
WORKER = "service:payout-worker"
TABLES = (
    "idempotency_keys",
    "outbox",
    "audit_events",
    "payout_attempts",
    "payouts",
    "pay_run_hours",
    "payslips",
    "pay_runs",
    "employees",
    "company_settings",
)
SEED = (
    "INSERT INTO payroll.company_settings (company_id, name, tax_rate_bps, retirement_bps) "
    "VALUES ($1, 'PayRun Demo Company', 0, 0)"
)


@pytest.fixture(scope="session")
def migrated(
    pg_bootstrapped: PgBootstrapped, alembic_upgrade: Callable[..., None]
) -> PgBootstrapped:
    """Migrate once to head as payroll_migrator (0002 seeds COMPANY_ID)."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("COMPANY_ID", str(COMPANY_ID))
        alembic_upgrade("payroll")
    return pg_bootstrapped


async def admin_connect(pg: PgBootstrapped) -> Any:
    return await asyncpg.connect(
        host=pg.host,
        port=pg.port,
        user=pg.admin_user,
        password=pg.admin_password,
        database="payrun",
    )


@pytest.fixture
async def db(migrated: PgBootstrapped, redis_acl: RedisAcl) -> AsyncIterator[Any]:
    """An admin connection; tables truncated and reseeded, Redis flushed, before each test."""
    conn = await admin_connect(migrated)
    await conn.execute(f"TRUNCATE {', '.join(f'payroll.{t}' for t in TABLES)}")
    await conn.execute(SEED, COMPANY_ID)
    admin = aioredis.Redis(
        host=redis_acl.host,
        port=redis_acl.port,
        password=redis_acl.passwords["REDIS_ADMIN_PASSWORD"],
    )
    await admin.flushall()
    await admin.aclose()
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def redis_admin(redis_acl: RedisAcl) -> AsyncIterator[aioredis.Redis]:
    client = aioredis.Redis(
        host=redis_acl.host,
        port=redis_acl.port,
        password=redis_acl.passwords["REDIS_ADMIN_PASSWORD"],
    )
    try:
        yield client
    finally:
        await client.aclose()


def settings_for(pg: PgBootstrapped, redis: RedisAcl, tokens: TokenFactory, **over: Any) -> Any:
    d, r = pg.env_for("payroll_app"), redis.env_for("payroll")
    values: dict[str, Any] = {
        "db_host": d["DB_HOST"],
        "db_port": int(d["DB_PORT"]),
        "db_user": d["DB_USER"],
        "db_password": SecretStr(d["DB_PASSWORD"]),
        "redis_host": r["REDIS_HOST"],
        "redis_port": int(r["REDIS_PORT"]),
        "redis_username": r["REDIS_USERNAME"],
        "redis_password": SecretStr(r["REDIS_PASSWORD"]),
        "internal_jwt_public_keys": tokens.keyset_json,
        "service_rate_limit_user": 100_000,
        "service_rate_limit_worker": 100_000,
    }
    values.update(over)
    return make_settings(**values)


@dataclass
class Api:
    app: FastAPI
    client: httpx.AsyncClient
    tokens: TokenFactory
    cid: UUID = COMPANY_ID
    names: dict[str, str] = field(default_factory=dict)

    @property
    def rt(self) -> Runtime:
        rt: Runtime = self.app.state.payroll
        return rt

    def headers(
        self,
        sub: str,
        roles: tuple[str, ...] = (),
        *,
        name: str | None = None,
        cid: UUID | None = None,
        issuer: str = "web",
    ) -> dict[str, str]:
        token = self.tokens.mint(
            issuer=issuer,  # type: ignore[arg-type]
            aud="payroll",
            sub=sub,
            cid=cid or self.cid,
            roles=roles,
            name=name,
        )
        return {"Authorization": f"Bearer {token}"}

    def payroll_admin(self, **kw: Any) -> dict[str, str]:
        return self.headers(PAYROLL_ADMIN, ("payroll_admin",), **kw)

    def approver(self, **kw: Any) -> dict[str, str]:
        return self.headers(APPROVER, ("approver",), **kw)

    def admin(self, **kw: Any) -> dict[str, str]:
        return self.headers(ADMIN, ("admin",), **kw)

    def worker(self, *, cid: UUID | None = None) -> dict[str, str]:
        return self.headers(WORKER, (), issuer="payout-worker", cid=cid)

    # --- flows ---------------------------------------------------------------------------------

    async def create_employee(self, **kw: Any) -> dict[str, Any]:
        body = employee_body(**kw)
        r = await self.client.post("/v1/employees", json=body, headers=self.payroll_admin())
        assert r.status_code == 201, r.text
        out: dict[str, Any] = r.json()
        return out

    async def create_run(
        self, schedule: str = "monthly", period_start: str = "2026-01-01"
    ) -> dict[str, Any]:
        r = await self.client.post(
            "/v1/pay-runs",
            json={"schedule": schedule, "period_start": period_start},
            headers=self.payroll_admin(),
        )
        assert r.status_code == 201, r.text
        out: dict[str, Any] = r.json()
        return out

    async def calculate(self, run_id: str, **kw: Any) -> dict[str, Any]:
        r = await self.client.post(
            f"/v1/pay-runs/{run_id}/calculate", headers=self.payroll_admin(**kw)
        )
        assert r.status_code == 200, r.text
        out: dict[str, Any] = r.json()
        return out

    async def approve(
        self, run_id: str, key: str | None = None, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        h = dict(headers or self.approver(name="Grace Approver"))
        h["Idempotency-Key"] = key or f"approve-{uuid4()}"
        return await self.client.post(f"/v1/pay-runs/{run_id}/approve", headers=h)

    async def calculated_run(self, n: int = 2, **emp: Any) -> dict[str, Any]:
        for i in range(n):
            await self.create_employee(name=f"Emp {i:02d}", email=f"e{i}@example.com", **emp)
        run = await self.create_run()
        return await self.calculate(run["id"], name="Carl Calculator")

    async def approved_run(self, n: int = 2, **emp: Any) -> dict[str, Any]:
        run = await self.calculated_run(n, **emp)
        r = await self.approve(run["id"])
        assert r.status_code == 200, r.text
        out: dict[str, Any] = r.json()
        return out

    async def payouts(self, run_id: str) -> list[dict[str, Any]]:
        r = await self.client.get(
            f"/v1/pay-runs/{run_id}/payouts?limit=100", headers=self.approver()
        )
        assert r.status_code == 200, r.text
        items: list[dict[str, Any]] = r.json()["items"]
        return items

    async def result(
        self, payout_id: str, body: dict[str, Any], *, key: str | None = None
    ) -> httpx.Response:
        h = self.worker()
        h["Idempotency-Key"] = key or f"{uuid4()}:{body['outcome']}"
        return await self.client.post(
            f"/v1/internal/payouts/{payout_id}/result", json=body, headers=h
        )


ApiFactory = Callable[..., Awaitable[Api]]


@pytest.fixture
async def api_factory(
    db: Any, migrated: PgBootstrapped, redis_acl: RedisAcl, token_factory: TokenFactory
) -> AsyncIterator[ApiFactory]:
    async with contextlib.AsyncExitStack() as stack:

        async def make(*, relay: bool = False, instrument: bool = False, **over: Any) -> Api:
            settings = settings_for(migrated, redis_acl, token_factory, **over)
            app = build_app(settings, instrument=instrument, relay=relay)
            await stack.enter_async_context(app.router.lifespan_context(app))
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            client = await stack.enter_async_context(
                httpx.AsyncClient(transport=transport, base_url="http://payroll")
            )
            return Api(app, client, token_factory)

        yield make


@pytest.fixture
async def api(api_factory: ApiFactory) -> Api:
    return await api_factory()


async def other_company(db: Any) -> UUID:
    await db.execute(SEED.replace("'PayRun Demo Company'", "'Other Co'"), OTHER_COMPANY_ID)
    return OTHER_COMPANY_ID


def jdump(v: Any) -> str:
    return json.dumps(v, sort_keys=True, default=str)
