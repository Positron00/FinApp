"""The payroll app (design §1, §4.11, §5.2).

``build_app(settings)`` assembles pycommon's app (OTel, request logging, body limit, error
handlers, JWT auth and caller rate limit on every router, ``/livez`` and ``/readyz`` with the DB,
Redis and Alembic-head checks), mounts the routers and runs the outbox relay in the background.
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
from payroll.metrics import Metrics
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
    settings: BaseServiceSettings, *, instrument: bool = True, relay: bool = True
) -> FastAPI:
    if not isinstance(settings, Settings):
        raise TypeError("build_app needs payroll Settings")
    s = settings
    holder: dict[str, FastAPI] = {}

    async def on_startup(ctx: AppContext) -> None:
        if ctx.sessions is None or ctx.redis is None:  # pragma: no cover  (use_db/use_redis on)
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
        holder["app"].state.payroll = Runtime(
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

    async def outbox_relay(_ctx: AppContext, stop: asyncio.Event) -> None:
        rt: Runtime = holder["app"].state.payroll
        if rt.relay is not None:
            await rt.relay.run(stop)

    app = create_app(
        s,
        routers=all_routers(),
        migrations_script_location=migrations_dir(),
        startup_checks=STARTUP_CHECKS,
        background=(outbox_relay,) if relay else (),
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
