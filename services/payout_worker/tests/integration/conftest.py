"""Integration fixtures (design §6): Testcontainers Postgres 16 and Redis 7 via pycommon.

The real ``deploy/compose`` bootstrap SQL and Redis ACLs; migrations for ``payouts``,
``bank_stub`` and ``payroll`` run as their migrator roles. The stack is in-process:

* payroll's real app (``payroll.main.build_app``; relay on) as ``payroll_app`` and the Redis user
  ``payroll``, so approve → outbox → relay → ``stream:payout.requested`` is the real path;
* bank-stub's real app on the same Postgres;
* the worker as ``payouts_app`` and the Redis user ``payout-worker``, calling both apps through
  ``httpx.ASGITransport`` (or a fault transport).

Timings are lowered here only (C53): claim idle 1.5 s, claim interval 0.2 s, 3 deliveries.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import asyncpg  # type: ignore[import-untyped]
import httpx
import pytest
import redis.asyncio as aioredis
from bank_stub.main import build_app as build_bank
from bank_stub.settings import Settings as BankSettings
from fastapi import FastAPI
from payout_worker.main import Runtime
from payout_worker.main import build_app as build_worker
from payout_worker.settings import Settings
from payroll.main import build_app as build_payroll
from payroll.settings import Settings as PayrollSettings
from pycommon.testing import PgBootstrapped, RedisAcl
from pydantic import SecretStr

from tests.helpers import API_KEY, COMPANY_ID, TOKENS, make_settings

STREAM = "stream:payout.requested"
DLQ = "stream:payout.requested.dlq"
GROUP = "payout-worker"
VALID_ROUTING = "021000021"  # passes payroll's ABA checksum
PAYROLL_ADMIN = "0193a000-0000-7000-8000-0000000000a1"
APPROVER = "0193a000-0000-7000-8000-0000000000a2"
PAYROLL_TABLES = (
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
FIELD_KEYS = json.dumps({"k2026a": base64.b64encode(os.urandom(32)).decode()})


@pytest.fixture(scope="session")
def migrated(
    pg_bootstrapped: PgBootstrapped, alembic_upgrade: Callable[..., None]
) -> PgBootstrapped:
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("COMPANY_ID", str(COMPANY_ID))
        alembic_upgrade("payout_worker")
        alembic_upgrade("bank_stub")
        alembic_upgrade("payroll")
    return pg_bootstrapped


async def connect(pg: PgBootstrapped, role: str) -> Any:
    e = pg.env_for(role)
    return await asyncpg.connect(
        host=e["DB_HOST"],
        port=int(e["DB_PORT"]),
        user=e["DB_USER"],
        password=e["DB_PASSWORD"],
        database="payrun",
    )


def admin_redis(r: RedisAcl) -> aioredis.Redis:
    return aioredis.Redis(host=r.host, port=r.port, password=r.passwords["REDIS_ADMIN_PASSWORD"])


@pytest.fixture
async def admin_db(migrated: PgBootstrapped, redis_acl: RedisAcl) -> AsyncIterator[Any]:
    """Admin connection; every schema truncated (payroll reseeded) and Redis flushed."""
    conn = await connect(migrated, migrated.admin_user)
    await conn.execute("TRUNCATE payouts.disbursements, payouts.processed_events")
    await conn.execute("TRUNCATE bank_stub.transfers")
    await conn.execute(f"TRUNCATE {', '.join(f'payroll.{t}' for t in PAYROLL_TABLES)}")
    await conn.execute(SEED, COMPANY_ID)
    r = admin_redis(redis_acl)
    await r.flushall()
    await r.aclose()
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def redis_admin(redis_acl: RedisAcl) -> AsyncIterator[aioredis.Redis]:
    client = admin_redis(redis_acl)
    try:
        yield client
    finally:
        await client.aclose()


def worker_settings(pg: PgBootstrapped, r: RedisAcl, **over: Any) -> Settings:
    d, rr = pg.env_for("payouts_app"), r.env_for("payout-worker")
    values: dict[str, Any] = {
        "db_host": d["DB_HOST"],
        "db_port": int(d["DB_PORT"]),
        "db_user": d["DB_USER"],
        "db_password": SecretStr(d["DB_PASSWORD"]),
        "redis_host": rr["REDIS_HOST"],
        "redis_port": int(rr["REDIS_PORT"]),
        "redis_username": rr["REDIS_USERNAME"],
        "redis_password": SecretStr(rr["REDIS_PASSWORD"]),
        "payout_claim_min_idle_ms": 1500,
        "payout_claim_interval_s": 0.2,
        "payout_xread_block_ms": 200,
        "payout_max_deliveries": 3,
        "payout_lag_poll_s": 0.2,
        "payout_shutdown_drain_s": 5,
        "bank_total_deadline_s": 2.0,
        "bank_read_timeout_s": 1.0,
        "payout_payroll_call_deadline_s": 2.0,
        "breaker_reset_s": 1.0,
    }
    values.update(over)
    return make_settings(**values)


def payroll_settings(pg: PgBootstrapped, r: RedisAcl) -> PayrollSettings:
    d, rr = pg.env_for("payroll_app"), r.env_for("payroll")
    return PayrollSettings.model_validate(
        {
            "company_id": COMPANY_ID,
            "db_host": d["DB_HOST"],
            "db_port": int(d["DB_PORT"]),
            "db_user": d["DB_USER"],
            "db_password": SecretStr(d["DB_PASSWORD"]),
            "redis_host": rr["REDIS_HOST"],
            "redis_port": int(rr["REDIS_PORT"]),
            "redis_username": rr["REDIS_USERNAME"],
            "redis_password": SecretStr(rr["REDIS_PASSWORD"]),
            "internal_jwt_public_keys": TOKENS.keyset_json,
            "payroll_field_keys": SecretStr(FIELD_KEYS),
            "payroll_field_active_kid": "k2026a",
            "service_rate_limit_user": 100_000,
            "service_rate_limit_worker": 100_000,
            "payroll_outbox_poll_ms": 50,
        }
    )


def bank_settings(pg: PgBootstrapped) -> BankSettings:
    d = pg.env_for("bank_stub_app")
    return BankSettings.model_validate(
        {
            "company_id": COMPANY_ID,
            "db_host": d["DB_HOST"],
            "db_port": int(d["DB_PORT"]),
            "db_user": d["DB_USER"],
            "db_password": SecretStr(d["DB_PASSWORD"]),
            "bank_stub_api_key": SecretStr(API_KEY),
            "bank_stub_failure_rate": 0.0,
            "bank_stub_latency_ms": 0,
        }
    )


async def wait_for(
    check: Callable[[], Awaitable[bool]], *, within: float = 20.0, interval: float = 0.05
) -> None:
    deadline = asyncio.get_running_loop().time() + within
    while True:
        if await check():
            return
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(interval)


@dataclass
class Stack:
    pg: PgBootstrapped
    redis_acl: RedisAcl
    db: Any
    redis: aioredis.Redis
    payroll_app: FastAPI
    payroll: httpx.AsyncClient
    bank_app: FastAPI
    bank: httpx.AsyncClient
    stack: contextlib.AsyncExitStack
    workers: list[FastAPI] = field(default_factory=list)

    # --- tokens --------------------------------------------------------------------------------

    @staticmethod
    def headers(sub: str, role: str, **extra: str) -> dict[str, str]:
        token = TOKENS.mint(
            issuer="web", aud="payroll", sub=sub, cid=COMPANY_ID, roles=(role,), name="Tester"
        )
        return {"Authorization": f"Bearer {token}", **extra}

    # --- payroll flows -------------------------------------------------------------------------

    async def approved_run(self, accounts: list[str]) -> dict[str, Any]:
        admin = self.headers(PAYROLL_ADMIN, "payroll_admin")
        for i, account in enumerate(accounts):
            r = await self.payroll.post(
                "/v1/employees",
                headers=admin,
                json={
                    "name": f"Emp {i:02d}",
                    "email": f"e{i}-{uuid4().hex[:6]}@example.com",
                    "pay_type": "salaried",
                    "annual_salary_cents": 5_200_001 + i,
                    "schedule": "monthly",
                    "bank_routing": VALID_ROUTING,
                    "bank_account": account,
                },
            )
            assert r.status_code == 201, r.text
        r = await self.payroll.post(
            "/v1/pay-runs",
            headers=admin,
            json={"schedule": "monthly", "period_start": "2026-01-01"},
        )
        assert r.status_code == 201, r.text
        run_id = r.json()["id"]
        r = await self.payroll.post(f"/v1/pay-runs/{run_id}/calculate", headers=admin)
        assert r.status_code == 200, r.text
        r = await self.payroll.post(
            f"/v1/pay-runs/{run_id}/approve",
            headers=self.headers(APPROVER, "approver", **{"Idempotency-Key": f"a-{uuid4()}"}),
        )
        assert r.status_code == 200, r.text
        out: dict[str, Any] = r.json()
        return out

    async def payouts(self, run_id: str) -> list[dict[str, Any]]:
        r = await self.payroll.get(
            f"/v1/pay-runs/{run_id}/payouts?limit=100",
            headers=self.headers(APPROVER, "approver"),
        )
        assert r.status_code == 200, r.text
        items: list[dict[str, Any]] = r.json()["items"]
        return items

    async def run(self, run_id: str) -> dict[str, Any]:
        r = await self.payroll.get(
            f"/v1/pay-runs/{run_id}", headers=self.headers(APPROVER, "approver")
        )
        out: dict[str, Any] = r.json()
        return out

    # --- stream --------------------------------------------------------------------------------

    async def entries(self) -> list[tuple[bytes, dict[bytes, bytes]]]:
        out: Any = await self.redis.xrange(STREAM)
        return list(out)

    async def dlq(self) -> list[dict[str, str]]:
        out: Any = await self.redis.xrange(DLQ)
        return [{k.decode(): v.decode() for k, v in fields.items()} for _, fields in out]

    async def pending(self) -> int:
        try:
            info = await self.redis.xpending(STREAM, GROUP)
        except aioredis.ResponseError:
            return 0
        return int(info["pending"])

    async def count(self, sql: str, *args: Any) -> int:
        return int(await self.db.fetchval(sql, *args))

    async def transfers(self) -> int:
        return await self.count("SELECT count(*) FROM bank_stub.transfers")

    # --- the worker ----------------------------------------------------------------------------

    def build_worker(
        self,
        *,
        payroll_transport: httpx.AsyncBaseTransport | None = None,
        bank_transport: httpx.AsyncBaseTransport | None = None,
        instrument: bool = False,
        **over: Any,
    ) -> FastAPI:
        """A worker app wired to the in-process payroll and bank-stub (not started)."""
        over.setdefault("payout_consumer_name", f"w-{len(self.workers) + 1}")
        s = worker_settings(self.pg, self.redis_acl, **over)
        app = build_worker(
            s,
            instrument=instrument,
            maintenance=False,
            payroll_transport=payroll_transport
            or httpx.ASGITransport(app=self.payroll_app, raise_app_exceptions=False),
            bank_transport=bank_transport
            or httpx.ASGITransport(app=self.bank_app, raise_app_exceptions=False),
        )
        self.workers.append(app)
        return app

    async def worker(self, **kw: Any) -> FastAPI:
        """A started worker, stopped when the test ends."""
        app = self.build_worker(**kw)
        await self.stack.enter_async_context(app.router.lifespan_context(app))
        return app

    async def published(self, n: int) -> list[tuple[bytes, dict[bytes, bytes]]]:
        """Wait until the relay has written ``n`` entries; return them."""

        async def enough() -> bool:
            return len(await self.entries()) >= n

        await wait_for(enough)
        return await self.entries()


def runtime(app: FastAPI) -> Runtime:
    rt: Runtime = app.state.worker
    return rt


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[FastAPI]:
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def stack(
    admin_db: Any, migrated: PgBootstrapped, redis_acl: RedisAcl, redis_admin: aioredis.Redis
) -> AsyncIterator[Stack]:
    async with contextlib.AsyncExitStack() as es:
        payroll_app = build_payroll(payroll_settings(migrated, redis_acl), instrument=False)
        await es.enter_async_context(payroll_app.router.lifespan_context(payroll_app))
        payroll = await es.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=payroll_app, raise_app_exceptions=False),
                base_url="http://payroll",
            )
        )
        bank_app = build_bank(bank_settings(migrated), instrument=False)
        await es.enter_async_context(bank_app.router.lifespan_context(bank_app))
        bank = await es.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=bank_app, raise_app_exceptions=False),
                base_url="http://bank-stub",
                headers={"X-Api-Key": API_KEY},
            )
        )
        yield Stack(
            migrated, redis_acl, admin_db, redis_admin, payroll_app, payroll, bank_app, bank, es
        )
