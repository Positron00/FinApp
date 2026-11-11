"""Integration fixtures: Testcontainers Postgres 16 and Redis 7 (platform LLD §6.1).

pycommon owns no schema, so these suites create a throwaway ``pyc`` schema with a migrator and a
runtime role (the same shape as the bootstrap SQL), and Redis ACL users with the §2.2 rules.
"""

import asyncio
import contextlib
import secrets
import socket
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest
import redis
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.container import DockerContainer

from tests.helpers import SvcSettings, make_settings

PG_IMAGE = "postgres:16-alpine"
REDIS_IMAGE = "redis:7-alpine"

BASE_RULES = [
    "+@connection",
    "+ping",
    "+@read",
    "+@write",
    "+@keyspace",
    "+@transaction",
    "-@stream",
    "-@scripting",
    "-@pubsub",
    "-@admin",
    "-@dangerous",
]
WORKER_MAIN = (
    "(~stream:payout.requested +xreadgroup +xack +xautoclaim +xpending +xinfo "
    "+xgroup|create +xgroup|delconsumer)"
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _pw() -> str:
    return secrets.token_urlsafe(18)


@dataclass
class Pg:
    container: Any
    host: str
    port: int
    admin_password: str
    passwords: dict[str, str] = field(default_factory=dict)

    def dsn(self, user: str = "postgres") -> dict[str, Any]:
        password = self.admin_password if user == "postgres" else self.passwords[user]
        return {
            "host": self.host,
            "port": self.port,
            "user": user,
            "password": password,
            "database": "payrun",
        }

    def settings(self, user: str = "pyc_app", **over: Any) -> SvcSettings:
        from pydantic import SecretStr  # noqa: PLC0415

        d = self.dsn(user)
        values: dict[str, Any] = {
            "db_host": d["host"],
            "db_port": d["port"],
            "db_user": user,
            "db_password": SecretStr(d["password"]),
            "db_schema": "pyc",
        }
        values.update(over)
        return make_settings(**values)

    def env(self, user: str) -> dict[str, str]:
        d = self.dsn(user)
        return {
            "DB_HOST": d["host"],
            "DB_PORT": str(d["port"]),
            "DB_NAME": "payrun",
            "DB_USER": user,
            "DB_PASSWORD": d["password"],
        }

    def stop(self) -> None:
        self.container.get_wrapped_container().stop(timeout=1)

    def start(self) -> None:
        self.container.get_wrapped_container().start()
        wait_pg(self)


async def _connect_once(pg: Pg) -> None:
    conn = await asyncpg.connect(**pg.dsn(), timeout=2)
    await conn.close()


def wait_pg(pg: Pg, timeout_s: float = 60) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            asyncio.run(_connect_once(pg))
            return
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.3)


SETUP_SQL = """
CREATE ROLE pyc_migrator LOGIN PASSWORD '{m}';
CREATE ROLE pyc_app LOGIN PASSWORD '{a}';
CREATE SCHEMA pyc AUTHORIZATION pyc_migrator;
GRANT USAGE ON SCHEMA pyc TO pyc_app;
ALTER DEFAULT PRIVILEGES FOR ROLE pyc_migrator IN SCHEMA pyc
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO pyc_app;
CREATE TABLE pyc.outbox (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL DEFAULT now(),
  type text NOT NULL,
  envelope text NOT NULL,
  sent_at timestamptz,
  relay_attempts int NOT NULL DEFAULT 0,
  last_error text
);
ALTER TABLE pyc.outbox OWNER TO pyc_migrator;
GRANT SELECT, INSERT, UPDATE, DELETE ON pyc.outbox TO pyc_app;
"""


def start_pg(*, fixed_port: bool = False) -> Pg:
    admin = _pw()
    container = PostgresContainer(
        PG_IMAGE, username="postgres", password=admin, dbname="payrun", driver=None
    )
    if fixed_port:
        container.with_bind_ports(5432, free_port())
    container.start()
    pg = Pg(
        container,
        container.get_container_host_ip(),
        int(container.get_exposed_port(5432)),
        admin,
        {"pyc_migrator": _pw(), "pyc_app": _pw()},
    )
    wait_pg(pg)

    async def setup() -> None:
        conn = await asyncpg.connect(**pg.dsn())
        try:
            sql = SETUP_SQL.format(m=pg.passwords["pyc_migrator"], a=pg.passwords["pyc_app"])
            await conn.execute(sql)
        finally:
            await conn.close()

    asyncio.run(setup())
    return pg


@pytest.fixture(scope="session")
def pg() -> Iterator[Pg]:
    p = start_pg()
    try:
        yield p
    finally:
        p.container.stop()


@pytest.fixture
async def pg_admin(pg: Pg) -> AsyncIterator[Any]:
    conn = await asyncpg.connect(**pg.dsn())
    try:
        yield conn
    finally:
        await conn.close()


@dataclass
class RedisServer:
    container: Any
    host: str
    port: int
    admin_password: str
    passwords: dict[str, str] = field(default_factory=dict)

    def client(self, user: str = "default", **kw: Any) -> redis.Redis:
        password = self.admin_password if user == "default" else self.passwords[user]
        return redis.Redis(
            host=self.host, port=self.port, username=user, password=password, socket_timeout=2, **kw
        )

    def settings(self, user: str = "default", **over: Any) -> SvcSettings:
        from pydantic import SecretStr  # noqa: PLC0415

        password = self.admin_password if user == "default" else self.passwords[user]
        values: dict[str, Any] = {
            "redis_host": self.host,
            "redis_port": self.port,
            "redis_username": user,
            "redis_password": SecretStr(password),
        }
        values.update(over)
        return make_settings(**values)

    def stop(self) -> None:
        self.container.get_wrapped_container().stop(timeout=1)

    def start(self) -> None:
        self.container.get_wrapped_container().start()
        wait_redis(self)
        self.apply_acl()

    def apply_acl(self) -> None:
        admin = self.client()
        try:
            for user in ("web", "payroll"):
                keys = f"~{user}:*"
                extra = ["(~stream:payout.requested +xadd)"] if user == "payroll" else []
                admin.execute_command(  # type: ignore[no-untyped-call]
                    "ACL", "SETUSER", user, "reset", "on", f">{self.passwords[user]}",
                    "-@all", keys, *BASE_RULES, *extra,
                )  # fmt: skip
            admin.execute_command(  # type: ignore[no-untyped-call]
                "ACL", "SETUSER", "payout-worker", "reset", "on",
                f">{self.passwords['payout-worker']}", "-@all", "+@connection", "+ping",
                "-@scripting", "-@dangerous", WORKER_MAIN,
            )  # fmt: skip
        finally:
            admin.close()


def wait_redis(server: RedisServer, timeout_s: float = 30) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            c = server.client()
            c.ping()
            c.close()
            return
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.2)


def start_redis(*, fixed_port: bool = False) -> RedisServer:
    admin = _pw()
    container = (
        DockerContainer(REDIS_IMAGE)
        .with_command(["redis-server", "--requirepass", admin, "--save", "", "--appendonly", "no"])
        .with_exposed_ports(6379)
    )
    if fixed_port:
        container.with_bind_ports(6379, free_port())
    container.start()
    server = RedisServer(
        container,
        container.get_container_host_ip(),
        int(container.get_exposed_port(6379)),
        admin,
        {"web": _pw(), "payroll": _pw(), "payout-worker": _pw()},
    )
    wait_redis(server)
    server.apply_acl()
    return server


@pytest.fixture(scope="session")
def redis_server() -> Iterator[RedisServer]:
    r = start_redis()
    try:
        yield r
    finally:
        r.container.stop()


@pytest.fixture
def flush_redis(redis_server: RedisServer) -> Iterator[None]:
    yield
    with contextlib.closing(redis_server.client()) as c:
        c.flushall()


@pytest.fixture(scope="module")
def restartable_pg() -> Iterator[Pg]:
    p = start_pg(fixed_port=True)
    try:
        yield p
    finally:
        p.container.stop()


@pytest.fixture(scope="module")
def restartable_redis() -> Iterator[RedisServer]:
    r = start_redis(fixed_port=True)
    try:
        yield r
    finally:
        r.container.stop()
