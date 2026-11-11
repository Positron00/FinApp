"""The payroll app (design §1, §4.11, §5.2).

``build_app(settings)`` assembles pycommon's app (OTel, request logging, body limit, error
handlers, JWT auth and caller rate limit on every router, ``/livez`` and ``/readyz`` with the DB,
Redis and Alembic-head checks), mounts the routers, registers the collection-time gauges (§7.2)
and runs the outbox relay and the maintenance runner (§5.3, §5.4) in the background. The runner
is always built (``Runtime.maintenance``, so tests can ``tick()``); ``maintenance=False`` only
stops its background loop.
``app`` is built lazily from the environment, for ``PYCOMMON_APP=payroll.main:app``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from pycommon.app import AppContext, create_app
from pycommon.settings import BaseServiceSettings, load_settings
from pycommon.streams import StreamProducer

from payroll.api import all_routers
from payroll.api.errors import install_domain_handler
from payroll.crypto import FieldKeyring
from payroll.events.outbox import OutboxWriter
from payroll.events.relay import build_relay
from payroll.maintenance import build_maintenance
from payroll.metrics import Metrics, PayrollGaugeSource, register_collection_gauges
from payroll.services.runtime import Runtime
from payroll.settings import STARTUP_CHECKS, Settings

__all__ = ["build_app", "migrations_dir"]


def migrations_dir() -> str:
    """``services/payroll/migrations`` in the repo, ``/app/migrations`` in the image."""
    for base in (*Path(__file__).resolve().parents, Path.cwd()):
        if (base / "alembic.ini").is_file() and (base / "migrations" / "env.py").is_file():
            return str(base / "migrations")
    raise FileNotFoundError("payroll migrations directory not found")


def build_app(
    settings: BaseServiceSettings,
    *,
    instrument: bool = True,
    relay: bool = True,
    maintenance: bool = True,
) -> FastAPI:
    if not isinstance(settings, Settings):
        raise TypeError("build_app needs payroll Settings")
    s = settings
    holder: dict[str, FastAPI] = {}
    gauges = PayrollGaugeSource(s)

    async def on_startup(ctx: AppContext) -> None:
        if ctx.sessions is None or ctx.redis is None or ctx.engine is None:  # pragma: no cover
            raise RuntimeError("payroll needs Postgres and Redis")
        relay_loop = (
            build_relay(
                ctx.sessions,
                StreamProducer(ctx.redis),
                maxlen=s.stream_maxlen,
                batch_size=s.payroll_outbox_batch,
                poll_ms=s.payroll_outbox_poll_ms,
                validate=s.payroll_validate_events,
            )
            if relay
            else None
        )
        rt = Runtime(
            settings=s,
            sessions=ctx.sessions,
            keyring=FieldKeyring.from_settings(s.payroll_field_keys, s.payroll_field_active_kid),
            metrics=Metrics.create(),
            outbox=OutboxWriter(
                validate=s.payroll_validate_events,
                wake=relay_loop.wake if relay_loop is not None else None,
            ),
            relay=relay_loop,
        )
        rt.maintenance = build_maintenance(rt, ctx.engine)
        holder["app"].state.payroll = rt
        register_collection_gauges(gauges)

    async def outbox_relay(_ctx: AppContext, stop: asyncio.Event) -> None:
        rt: Runtime = holder["app"].state.payroll
        if rt.relay is not None:
            await rt.relay.run(stop)

    async def maintenance_loop(_ctx: AppContext, stop: asyncio.Event) -> None:
        """PAY-407, PAY-608: re-publish and purges, one replica at a time (advisory lock)."""
        rt: Runtime = holder["app"].state.payroll
        if rt.maintenance is not None:
            await rt.maintenance.run(stop)

    async def close_gauges(_ctx: AppContext, stop: asyncio.Event) -> None:
        """Dispose the gauge source's engine and thread on shutdown."""
        await stop.wait()
        await asyncio.to_thread(gauges.close)

    app = create_app(
        s,
        routers=all_routers(),
        migrations_script_location=migrations_dir(),
        startup_checks=STARTUP_CHECKS,
        background=(
            *((outbox_relay,) if relay else ()),
            *((maintenance_loop,) if maintenance else ()),
            close_gauges,
        ),
        on_startup=on_startup,
        instrument=instrument,
    )
    holder["app"] = app
    install_domain_handler(app)
    return app


def __getattr__(name: str) -> Any:
    """``payroll.main:app``: built on first access from the environment (``load_settings``)."""
    if name == "app":
        built = build_app(load_settings(Settings))
        globals()["app"] = built
        return built
    raise AttributeError(name)
