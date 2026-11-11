"""The identity app (identity LLD §1; PAY-605).

``build_app(settings)`` needs no env or I/O (``scripts/export_openapi.py`` calls it with
placeholder settings). ``app`` — what ``python -m pycommon.serve`` loads via
``PYCOMMON_APP=identity.main:app`` — is built from the environment on first access.
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI
from pycommon import Clock, SystemClock
from pycommon.app import AppContext, create_app
from pycommon.settings import BaseServiceSettings, load_settings
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from identity.api import auth, invites, users
from identity.domain.passwords import PasswordHasher
from identity.email.relay import EmailRelay
from identity.email.smtp import EmailSender, SmtpSender
from identity.limits import AuthLimits
from identity.metrics import create_metrics
from identity.paths import MIGRATIONS_DIR
from identity.runtime import IdentityRuntime
from identity.settings import IdentitySettings, Settings
from identity.startup_checks import IDENTITY_CHECKS

__all__ = ["MIGRATIONS_DIR", "build_app"]


class NoStoreMiddleware:
    """``Cache-Control: no-store`` on every response (§3.4)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"
                ]
                headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)


def build_app(
    settings: BaseServiceSettings,
    *,
    instrument: bool = True,
    clock: Clock | None = None,
    sender: EmailSender | None = None,
    run_relay: bool = True,
) -> FastAPI:
    """The identity app. ``clock``, ``sender`` and ``run_relay`` are test seams."""
    if not isinstance(settings, IdentitySettings):
        raise TypeError("build_app needs IdentitySettings")
    s = settings
    clk = clock or SystemClock()

    async def on_startup(ctx: AppContext) -> None:
        if ctx.sessions is None or ctx.redis is None:  # pragma: no cover  (use_db/use_redis on)
            raise RuntimeError("identity needs the DB and Redis")
        metrics = create_metrics()
        hasher = PasswordHasher(
            time_cost=s.identity_argon2_time_cost,
            memory_kib=s.identity_argon2_memory_kib,
            parallelism=s.identity_argon2_parallelism,
            max_concurrency=s.identity_argon2_max_concurrency,
            metrics=metrics,
        )
        await hasher.start()
        relay = EmailRelay(ctx.sessions, s, sender or SmtpSender(s, clock=clk), metrics)
        app.state.identity = IdentityRuntime(
            settings=s,
            sessions=ctx.sessions,
            redis=ctx.redis,
            hasher=hasher,
            limits=AuthLimits(ctx.redis, s, metrics, clock=clk),
            metrics=metrics,
            relay=relay,
            clock=clk,
        )

    async def email_relay(_ctx: AppContext, stop: asyncio.Event) -> None:
        rt: IdentityRuntime = app.state.identity
        await rt.relay.run(stop)

    app = create_app(
        s,
        routers=[auth.router, invites.router, users.router],
        migrations_script_location=str(MIGRATIONS_DIR),
        startup_checks=IDENTITY_CHECKS,
        background=[email_relay] if run_relay else [],
        on_startup=on_startup,
        instrument=instrument,
    )
    app.add_middleware(NoStoreMiddleware)
    return app


_built: dict[str, FastAPI] = {}


def __getattr__(name: str) -> FastAPI:
    """``identity.main:app``: built from the environment on first access (serve path only)."""
    if name != "app":
        raise AttributeError(name)
    if "app" not in _built:
        _built["app"] = build_app(load_settings(Settings))
    return _built["app"]
