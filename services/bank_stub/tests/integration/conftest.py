"""Integration fixtures (design §6): Testcontainers Postgres 16 via pycommon.

The bootstrap SQL is the real ``deploy/compose/init``; migrations run as ``bank_stub_migrator``;
the app connects as ``bank_stub_app`` through the C1 keys (C1, C2). Every test starts with an
empty ``bank_stub.transfers``.
"""

from __future__ import annotations

import contextlib
import socket
from collections.abc import AsyncIterator, Callable
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import httpx
import pytest
from bank_stub.settings import Settings
from fastapi import FastAPI
from pycommon.testing import PgBootstrapped
from pydantic import SecretStr

from tests.helpers import make_settings, running


@pytest.fixture(scope="session")
def migrated(
    pg_bootstrapped: PgBootstrapped, alembic_upgrade: Callable[..., None]
) -> PgBootstrapped:
    alembic_upgrade("bank_stub")
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


@pytest.fixture
async def admin_db(migrated: PgBootstrapped) -> AsyncIterator[Any]:
    conn = await connect(migrated, migrated.admin_user)
    await conn.execute("TRUNCATE bank_stub.transfers")
    try:
        yield conn
    finally:
        await conn.close()


def settings_for(pg: PgBootstrapped, **over: Any) -> Settings:
    d = pg.env_for("bank_stub_app")
    values: dict[str, Any] = {
        "db_host": d["DB_HOST"],
        "db_port": int(d["DB_PORT"]),
        "db_user": d["DB_USER"],
        "db_password": SecretStr(d["DB_PASSWORD"]),
    }
    values.update(over)
    return make_settings(**values)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
    return port


Harness = Callable[..., contextlib.AbstractAsyncContextManager[tuple[FastAPI, httpx.AsyncClient]]]


@pytest.fixture
def harness(admin_db: Any, migrated: PgBootstrapped) -> Harness:
    def make(
        *, instrument: bool = False, **over: Any
    ) -> contextlib.AbstractAsyncContextManager[tuple[FastAPI, httpx.AsyncClient]]:
        return running(settings_for(migrated, **over), instrument=instrument)

    return make
