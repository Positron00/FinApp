"""Liveness and readiness (platform LLD §3.6; PAY-605).

``/livez`` does no I/O. ``/readyz`` runs every check concurrently under a timeout and never puts
error text in the body. Health routes have no auth and no rate limit, and are excluded from
tracing and ``http.request`` logs.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Final, Protocol

from alembic.script import ScriptDirectory
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from pycommon.logging import get_logger, log_event

__all__ = [
    "HEALTH_PATHS",
    "HealthCheck",
    "db_check",
    "health_router",
    "migrations_check",
    "redis_check",
    "task_check",
]

HEALTH_PATHS: Final = frozenset({"/livez", "/readyz"})
LOG_EVERY_S: Final = 30.0
_IDENT: Final = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

_log = get_logger("pycommon.health")


class HealthCheck(Protocol):
    name: str

    async def __call__(self) -> None: ...


class _Check:
    def __init__(self, name: str, fn: Callable[[], Awaitable[None]]) -> None:
        self.name = name
        self._fn = fn

    async def __call__(self) -> None:
        await self._fn()


def db_check(engine: AsyncEngine) -> HealthCheck:
    async def run() -> None:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    return _Check("db", run)


def redis_check(redis: Redis) -> HealthCheck:
    async def run() -> None:
        if not await redis.ping():
            raise ConnectionError("PING failed")

    return _Check("redis", run)


class MigrationsMismatchError(Exception):
    """The DB's Alembic version is not the script directory's head."""


def migrations_check(engine: AsyncEngine, *, schema: str, script_location: str) -> HealthCheck:
    """Compare ``<schema>.alembic_version`` with the head(s) read once from the scripts."""
    if not _IDENT.match(schema):
        raise ValueError("schema must be a plain lower-case identifier")
    heads = frozenset(ScriptDirectory(script_location).get_heads())
    query = text(f'SELECT version_num FROM "{schema}".alembic_version')  # noqa: S608 (checked identifier)

    async def run() -> None:
        async with engine.connect() as conn:
            rows = (await conn.execute(query)).scalars().all()
        if frozenset(rows) != heads:
            raise MigrationsMismatchError()

    return _Check("migrations", run)


def task_check(name: str, is_alive: Callable[[], bool]) -> HealthCheck:
    async def run() -> None:
        if not is_alive():
            raise RuntimeError(f"{name} is not running")

    return _Check(name, run)


def health_router(checks: Sequence[HealthCheck], *, timeout_s: float = 1.0) -> APIRouter:
    """/livez and /readyz; ``checks`` is read per request, so it may be filled late."""
    router = APIRouter()
    last_logged: dict[str, float] = {}

    async def run_one(check: HealthCheck) -> bool:
        try:
            async with asyncio.timeout(timeout_s):
                await check()
        except Exception as exc:
            now = time.monotonic()
            if now - last_logged.get(check.name, -LOG_EVERY_S) >= LOG_EVERY_S:
                last_logged[check.name] = now
                log_event(
                    _log,
                    logging.WARNING,
                    "health.check_failed",
                    check=check.name,
                    error_type=type(exc).__name__,
                )
            return False
        return True

    @router.get("/livez", include_in_schema=False)
    async def livez() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @router.get("/readyz", include_in_schema=False)
    async def readyz() -> JSONResponse:
        current = list(checks)
        results = await asyncio.gather(*(run_one(c) for c in current))
        status = {c.name: ("ok" if ok else "fail") for c, ok in zip(current, results, strict=True)}
        healthy = all(results)
        return JSONResponse(
            {"status": "ok" if healthy else "fail", "checks": status},
            status_code=200 if healthy else 503,
        )

    return router
