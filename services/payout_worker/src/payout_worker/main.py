"""The payout-worker app (design §1, §3.3, §3.7).

A health-only app from pycommon's ``create_app`` (``/livez`` and ``/readyz``; no routers, no
JWT verification). The lifespan runs the startup checks before any connection, opens the DB
engine and the command Redis client; ``on_startup`` opens the blocking-read client, builds the
clients, handler and consumer, and pre-initialises the counters (C21). The background tasks are
the consumer (reader, claim loop, handlers), the gauge poller and the maintenance runner.
``app`` is built lazily from the environment, for ``PYCOMMON_APP=payout_worker.main:app``.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from pycommon import Clock
from pycommon.app import AppContext, BackgroundTask, create_app
from pycommon.health import HealthCheck, task_check
from pycommon.redis_client import create_redis
from pycommon.settings import BaseServiceSettings, load_settings
from redis.asyncio import Redis

from payout_worker.clients.bank import BankClient
from payout_worker.clients.payroll import PayrollClient
from payout_worker.consumer import Consumer
from payout_worker.db.repo import DisbursementRepo
from payout_worker.handler import PayoutHandler
from payout_worker.jwt_signer import WorkerJwtSigner
from payout_worker.maintenance import build_runner
from payout_worker.metrics import Metrics
from payout_worker.settings import STARTUP_CHECKS, Settings

__all__ = ["Runtime", "build_app", "migrations_dir"]

READER_CLIENT_NAME = "payout-worker-reader"


def migrations_dir() -> str:
    """``services/payout_worker/migrations`` in the repo, ``/app/migrations`` in the image."""
    for base in (*Path(__file__).resolve().parents, Path.cwd()):
        if (base / "alembic.ini").is_file() and (base / "migrations" / "env.py").is_file():
            return str(base / "migrations")
    raise FileNotFoundError("payout-worker migrations directory not found")  # pragma: no cover


@dataclass
class Runtime:
    settings: Settings
    metrics: Metrics
    payroll: PayrollClient
    bank: BankClient
    handler: PayoutHandler
    consumer: Consumer
    reader: Redis

    async def aclose(self) -> None:
        with contextlib.suppress(Exception):
            await self.reader.aclose()
        await self.payroll.aclose()
        await self.bank.aclose()


def _build_runtime(
    s: Settings,
    ctx: AppContext,
    payroll_transport: httpx.AsyncBaseTransport | None,
    bank_transport: httpx.AsyncBaseTransport | None,
    clock: Clock | None,
) -> Runtime:
    if ctx.sessions is None or ctx.redis is None:  # pragma: no cover  (db and redis on)
        raise RuntimeError("payout-worker needs Postgres and Redis")
    metrics = Metrics.create(
        stream=s.payout_stream, group=s.payout_group, dlq_stream=s.payout_dlq_stream
    )
    metrics.preinit()  # C21: ok, retry, dlq at 0 before the first read
    signer = WorkerJwtSigner(
        s.payout_worker_jwt_private_key, s.payout_worker_jwt_kid, s.payout_worker_jwt_ttl_s
    )
    payroll = PayrollClient(s, signer, transport=payroll_transport, clock=clock)
    bank = BankClient(s, transport=bank_transport, clock=clock)
    repo = DisbursementRepo(ctx.sessions, stream=s.payout_stream)
    handler = PayoutHandler(s, repo, payroll, bank, metrics, clock=clock)
    reader = create_redis(
        s, socket_timeout_ms=s.payout_xread_block_ms + 1000, client_name=READER_CLIENT_NAME
    )
    consumer = Consumer(s, handler, ctx.redis, reader, metrics, clock=clock)
    return Runtime(s, metrics, payroll, bank, handler, consumer, reader)


def build_app(
    settings: BaseServiceSettings,
    *,
    instrument: bool = True,
    consume: bool = True,
    maintenance: bool = True,
    payroll_transport: httpx.AsyncBaseTransport | None = None,
    bank_transport: httpx.AsyncBaseTransport | None = None,
    clock: Clock | None = None,
) -> FastAPI:
    if not isinstance(settings, Settings):
        raise TypeError("build_app needs payout-worker Settings")
    s = settings
    holder: dict[str, Any] = {}

    def runtime() -> Runtime | None:
        rt = holder.get("rt")
        return rt if isinstance(rt, Runtime) else None

    async def on_startup(ctx: AppContext) -> None:
        rt = _build_runtime(s, ctx, payroll_transport, bank_transport, clock)
        holder["rt"] = rt
        holder["app"].state.worker = rt
        if consume:  # the reader retries with backoff if Redis is down now
            with contextlib.suppress(Exception):
                await rt.consumer.ensure_group()

    async def consume_task(_ctx: AppContext, stop: asyncio.Event) -> None:
        rt = runtime()
        if rt is None:  # pragma: no cover  (on_startup ran first)
            return
        try:
            await rt.consumer.run(stop)
        finally:
            await rt.aclose()

    async def gauges_task(_ctx: AppContext, stop: asyncio.Event) -> None:
        rt = runtime()
        if rt is not None:
            await rt.consumer.poll_gauges(stop)

    async def maintenance_task(ctx: AppContext, stop: asyncio.Event) -> None:
        if ctx.engine is not None and ctx.sessions is not None:
            await build_runner(s, ctx.engine, ctx.sessions, clock).run(stop)

    def health(_ctx: AppContext) -> Sequence[HealthCheck]:
        def consumer_alive() -> bool:
            rt = runtime()
            return rt is not None and rt.consumer.is_alive()

        def not_stopping() -> bool:
            rt = runtime()
            return rt is not None and not rt.consumer.stopping

        return [task_check("consumer", consumer_alive), task_check("stopping", not_stopping)]

    background: list[BackgroundTask] = []
    if consume:
        background += [consume_task, gauges_task]
    if maintenance:
        background.append(maintenance_task)

    app = create_app(
        s,
        use_jwt=False,
        migrations_script_location=migrations_dir(),
        extra_health_checks=health,
        startup_checks=STARTUP_CHECKS,
        background=background,
        on_startup=on_startup,
        shutdown_timeout_s=s.payout_shutdown_drain_s + 5,
        instrument=instrument,
    )
    holder["app"] = app
    return app


def __getattr__(name: str) -> Any:
    """``payout_worker.main:app``: built on first access from the environment."""
    if name == "app":
        built = build_app(load_settings(Settings))
        globals()["app"] = built
        return built
    raise AttributeError(name)
