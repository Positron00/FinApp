"""The service app factory (platform LLD §3.14; PAY-601, PAY-604, PAY-605).

Assembly, outermost first: OTel instrumentation, request context (request id, ``http.request``
log, ``x-request-id`` / ``x-trace-id``), body-size limit, error handlers, routers, lifespan.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from fastapi import APIRouter, Depends, FastAPI, Request
from opentelemetry import trace
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from pycommon import otel
from pycommon.auth import Principal, TokenVerifier, authenticate
from pycommon.db import create_engine, session_factory
from pycommon.errors import PayloadTooLarge, error_response, install_handlers, unhandled_response
from pycommon.health import (
    HEALTH_PATHS,
    HealthCheck,
    db_check,
    health_router,
    migrations_check,
    redis_check,
)
from pycommon.logging import get_logger, log_event, request_id_var, setup_logging
from pycommon.ratelimit import RateLimiter, caller_rate_limit
from pycommon.redis_client import create_redis
from pycommon.settings import BaseServiceSettings
from pycommon.startup_checks import CheckFailure, StartupCheck, run_startup_checks

__all__ = ["AppContext", "BackgroundTask", "build_openapi", "create_app", "get_ctx"]

_log = get_logger("pycommon.app")

REQUEST_ID_RE: Final = re.compile(r"^[A-Za-z0-9-]{1,64}$")
RESTART_INITIAL_S: Final = 1.0
RESTART_MAX_S: Final = 30.0
LOOP_LAG_INTERVAL_S: Final = 1.0


@dataclass
class AppContext:
    settings: BaseServiceSettings
    engine: AsyncEngine | None
    sessions: async_sessionmaker[AsyncSession] | None
    redis: Redis | None
    verifier: TokenVerifier | None
    limiter: RateLimiter | None
    telemetry: otel.Telemetry | None


BackgroundTask = Callable[[AppContext, asyncio.Event], Awaitable[None]]


def get_ctx(request: Request) -> AppContext:
    ctx = getattr(request.app.state, "ctx", None)
    if not isinstance(ctx, AppContext):
        raise RuntimeError("the app has not started (no AppContext)")
    return ctx


# --- middleware --------------------------------------------------------------------------------


def _caller_hash(principal: object) -> str | None:
    if isinstance(principal, Principal):
        return hashlib.sha256(principal.caller_key.encode()).hexdigest()[:16]
    return None


class RequestContextMiddleware:
    """Request id, ``x-request-id``/``x-trace-id``, one ``http.request`` log, last-resort 500."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = ""
        for name, value in scope.get("headers", []):
            if name == b"x-request-id":
                incoming = value.decode("latin-1")
                break
        request_id = incoming if REQUEST_ID_RE.match(incoming) else str(uuid.uuid4())
        token = request_id_var.set(request_id)
        state: dict[str, Any] = scope.setdefault("state", {})
        started = time.monotonic()
        status = 500
        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status = int(message["status"])
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode()))
                ctx = trace.get_current_span().get_span_context()
                if ctx.is_valid:
                    headers.append((b"x-trace-id", f"{ctx.trace_id:032x}".encode()))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            if response_started:
                raise
            response = unhandled_response(exc)
            await response(scope, receive, send_wrapper)
        finally:
            path = scope.get("path", "")
            if path not in HEALTH_PATHS:
                route = getattr(scope.get("route"), "path", None) or "unmatched"
                log_event(
                    _log,
                    logging.WARNING if status >= 500 else logging.INFO,
                    "http.request",
                    method=scope.get("method"),
                    route=route,
                    status=status,
                    duration_ms=round((time.monotonic() - started) * 1000, 1),
                    caller=_caller_hash(state.get("principal")),
                )
            request_id_var.reset(token)


class BodyLimitMiddleware:
    """413 by ``Content-Length`` at once, or when the streamed body passes it."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = 0
                if declared > self.max_bytes:
                    response = error_response(
                        413, PayloadTooLarge.code, PayloadTooLarge.default_message
                    )
                    await response(scope, receive, send)
                    return
                break
        seen = 0

        async def limited_receive() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    # HTTPException passes through FastAPI's body parsing unchanged.
                    raise StarletteHTTPException(status_code=413)
            return message

        await self.app(scope, limited_receive, send)


# --- background tasks --------------------------------------------------------------------------


def _task_name(task: Any) -> str:
    inner = getattr(task, "func", task)
    return str(
        getattr(inner, "__qualname__", None) or getattr(inner, "__name__", None) or repr(inner)
    )


async def _sleep_or_stop(seconds: float, stop: asyncio.Event) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


async def _supervise(task: BackgroundTask, ctx: AppContext, stop: asyncio.Event) -> None:
    """Run ``task``; if it raises, log ``background.task.crashed`` and restart after 1 s … 30 s."""
    delay = RESTART_INITIAL_S
    name = _task_name(task)
    while not stop.is_set():
        try:
            await task(ctx, stop)
            return
        except Exception as exc:
            log_event(
                _log,
                logging.ERROR,
                "background.task.crashed",
                task=name,
                error_type=type(exc).__name__,
            )
        await _sleep_or_stop(delay, stop)
        delay = min(delay * 2, RESTART_MAX_S)


class _LoopLag:
    value: float = 0.0


async def _loop_lag_ticker(_ctx: AppContext, stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        t0 = loop.time()
        await _sleep_or_stop(LOOP_LAG_INTERVAL_S, stop)
        if stop.is_set():
            return
        _LoopLag.value = max(0.0, loop.time() - t0 - LOOP_LAG_INTERVAL_S)


def _register_loop_lag_gauge() -> None:
    otel.observable_gauge(
        "asyncio_event_loop_lag_seconds",
        unit="s",
        description="Event-loop lag measured by a 1 s ticker",
        callback=lambda: {frozenset(): _LoopLag.value},
    )


# --- the factory -------------------------------------------------------------------------------


def _jwt_keys_check(settings: BaseServiceSettings) -> list[CheckFailure]:
    if settings.internal_jwt_public_keys is None:
        return [CheckFailure(type(settings).env_name("internal_jwt_public_keys"), "missing")]
    return []


@dataclass
class _Lifecycle:
    """What ``create_app``'s lifespan starts and stops (step 7 of platform LLD §3.14)."""

    settings: BaseServiceSettings
    use_db: bool
    use_redis: bool
    use_jwt: bool
    migrations_script_location: str | None
    extra_health_checks: Callable[[AppContext], Sequence[HealthCheck]] | None
    startup_checks: Sequence[StartupCheck]
    background: Sequence[BackgroundTask]
    on_startup: Callable[[AppContext], Awaitable[None]] | None
    shutdown_timeout_s: float
    checks: list[HealthCheck] = field(default_factory=list)
    telemetry: otel.Telemetry | None = None
    ctx: AppContext | None = None
    tasks: list[asyncio.Task[None]] = field(default_factory=list)
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    sa_instrumentor: Any = None

    def _run_checks(self, app: FastAPI) -> None:
        checks: list[StartupCheck] = list(self.startup_checks)
        if self.use_jwt:
            checks.append(_jwt_keys_check)
        try:
            run_startup_checks(self.settings, checks)
        except SystemExit as exc:
            app.state.startup_exit_code = exc.code
            raise

    def _build_context(self) -> AppContext:
        s = self.settings
        engine = create_engine(s) if self.use_db else None
        redis = create_redis(s) if self.use_redis else None
        keys = s.internal_jwt_public_keys
        verifier = (
            TokenVerifier(keys, audience=s.service_name, leeway_s=s.jwt_leeway_s)
            if self.use_jwt and keys is not None
            else None
        )
        limiter = (
            RateLimiter(redis, prefix=f"{s.service_name}:rl", component="caller")
            if redis is not None and self.use_jwt
            else None
        )
        if self.telemetry is not None and engine is not None:
            tel = self.telemetry
            self.sa_instrumentor = otel._instrument_sqlalchemy(
                engine, tel.tracer_provider, tel.meter_provider
            )
        sessions = session_factory(engine) if engine is not None else None
        return AppContext(s, engine, sessions, redis, verifier, limiter, self.telemetry)

    def _health_checks(self, ctx: AppContext) -> list[HealthCheck]:
        out: list[HealthCheck] = []
        if ctx.engine is not None:
            out.append(db_check(ctx.engine))
            if self.migrations_script_location is not None:
                out.append(
                    migrations_check(
                        ctx.engine,
                        schema=self.settings.db_schema,
                        script_location=self.migrations_script_location,
                    )
                )
        if ctx.redis is not None:
            out.append(redis_check(ctx.redis))
        if self.extra_health_checks is not None:
            out.extend(self.extra_health_checks(ctx))
        return out

    async def start(self, app: FastAPI) -> None:
        setup_logging(self.settings)
        self._run_checks(app)  # before any connection is opened (C29)
        ctx = self._build_context()
        self.ctx = ctx
        app.state.ctx = ctx
        self.checks[:] = self._health_checks(ctx)
        _register_loop_lag_gauge()
        if self.on_startup is not None:
            await self.on_startup(ctx)
        s = self.settings
        log_event(_log, logging.INFO, "service.started", version=s.version, env=str(s.env))
        self.tasks = [
            asyncio.create_task(_supervise(bg, ctx, self.stop_event), name=_task_name(bg))
            for bg in (*self.background, _loop_lag_ticker)
        ]

    async def stop(self) -> None:
        self.stop_event.set()
        if self.tasks:
            _, pending = await asyncio.wait(self.tasks, timeout=self.shutdown_timeout_s)
            for t in pending:
                t.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
        ctx = self.ctx
        if ctx is not None and ctx.redis is not None:
            await ctx.redis.aclose()
        if ctx is not None and ctx.engine is not None:
            await ctx.engine.dispose()
        if self.sa_instrumentor is not None:
            self.sa_instrumentor.uninstrument()
        if self.telemetry is not None:
            self.telemetry.shutdown()
        s = self.settings
        log_event(_log, logging.INFO, "service.stopped", version=s.version, env=str(s.env))


def create_app(
    settings: BaseServiceSettings,
    *,
    routers: Sequence[APIRouter] = (),
    public_routers: Sequence[APIRouter] = (),
    use_db: bool = True,
    use_redis: bool = True,
    use_jwt: bool = True,
    migrations_script_location: str | None = None,
    extra_health_checks: Callable[[AppContext], Sequence[HealthCheck]] | None = None,
    startup_checks: Sequence[StartupCheck] = (),
    background: Sequence[BackgroundTask] = (),
    on_startup: Callable[[AppContext], Awaitable[None]] | None = None,
    shutdown_timeout_s: float = 60.0,
    instrument: bool = True,
) -> FastAPI:
    if routers and not use_jwt:
        raise ValueError(
            "routers need use_jwt=True; pass unauthenticated routers as public_routers"
        )
    life = _Lifecycle(
        settings,
        use_db,
        use_redis,
        use_jwt,
        migrations_script_location,
        extra_health_checks,
        startup_checks,
        background,
        on_startup,
        shutdown_timeout_s,
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            await life.start(app)
            yield
        finally:
            await life.stop()

    app = FastAPI(
        title=settings.service_name,
        version=settings.version,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        lifespan=lifespan,
    )
    app.state.shutdown_timeout_s = shutdown_timeout_s
    install_handlers(app)
    deps: list[Any] = [Depends(authenticate)]
    if use_redis:
        deps.append(Depends(caller_rate_limit(settings, exempt_subs=("service:web",))))
    for router in routers:
        app.include_router(router, dependencies=deps)
    for router in public_routers:
        app.include_router(router)
    app.include_router(health_router(life.checks))
    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_body_bytes)
    app.add_middleware(RequestContextMiddleware)
    if instrument:
        life.telemetry = otel.setup(app, settings, redis_instrument=use_redis)
    return app


def build_openapi(
    build_app: Callable[[BaseServiceSettings], FastAPI], settings: BaseServiceSettings
) -> dict[str, Any]:
    """The OpenAPI document of the app ``build_app`` makes (works with docs off)."""
    return build_app(settings).openapi()
