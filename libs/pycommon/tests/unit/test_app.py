"""create_app assembly (platform LLD §3.14; PAY-601, PAY-603, PAY-604, PAY-605, C29)."""

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any, cast

import pytest
import redis.exceptions
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.testclient import TestClient
from opentelemetry import trace
from pycommon import app as app_module
from pycommon import otel as otel_module
from pycommon.app import AppContext, build_openapi, create_app, get_ctx
from pycommon.testing import JsonLogCapture, OtelMemory, TokenFactory

from tests.helpers import COMPANY_ID, make_settings

USER = "0193a000-0000-7000-8000-0000000000aa"


def _routers() -> tuple[APIRouter, APIRouter]:
    private, public = APIRouter(), APIRouter()

    @private.get("/v1/me")
    async def me(ctx: Annotated[AppContext, Depends(get_ctx)]) -> dict[str, str]:
        return {"service": ctx.settings.service_name}

    @private.post("/v1/echo")
    async def echo(request: Request) -> dict[str, int]:
        return {"bytes": len(await request.body())}

    @private.get("/v1/fail")
    async def fail() -> None:
        raise RuntimeError("boom")

    @public.get("/public/ping")
    async def ping() -> dict[str, str]:
        return {"pong": "yes"}

    @public.post("/public/upload")
    async def upload(request: Request) -> dict[str, int]:
        return {"bytes": len(await request.body())}

    return private, public


@pytest.fixture
def tf() -> TokenFactory:
    return TokenFactory.create()


def _app(tf: TokenFactory, **kw: Any) -> FastAPI:
    private, public = _routers()
    settings = kw.pop("settings", None) or make_settings(
        internal_jwt_public_keys=tf.keyset, max_body_bytes=100
    )
    opts: dict[str, Any] = {"use_db": False, "use_redis": False, "instrument": False}
    opts.update(kw)
    return create_app(settings, routers=[private], public_routers=[public], **opts)


@pytest.fixture
def client(tf: TokenFactory) -> Iterator[TestClient]:
    with TestClient(_app(tf), raise_server_exceptions=False) as c:
        yield c


def _auth(tf: TokenFactory, sub: str = USER) -> dict[str, str]:
    return {"Authorization": f"Bearer {tf.mint(aud='testsvc', sub=sub, cid=COMPANY_ID)}"}


def test_docs_off_unless_enabled(tf: TokenFactory) -> None:
    """PAY-605: /docs and /openapi.json exist only with DOCS_ENABLED; app.openapi() always works."""
    off = _app(tf)
    with TestClient(off) as c:
        assert c.get("/docs").status_code == 404
        assert c.get("/openapi.json").status_code == 404
    spec = off.openapi()
    assert "/v1/me" in spec["paths"]
    assert "/livez" not in spec["paths"]
    on = _app(tf, settings=make_settings(internal_jwt_public_keys=tf.keyset, docs_enabled=True))
    with TestClient(on) as c:
        assert c.get("/docs").status_code == 200
        assert c.get("/openapi.json").status_code == 200
        assert c.get("/redoc").status_code == 404
    built = build_openapi(lambda s: _app(tf, settings=s), make_settings())
    assert built["info"]["title"] == "testsvc"


def test_request_id_accepted_regenerated_and_echoed(client: TestClient) -> None:
    """PAY-604: a valid X-Request-ID is reused; an invalid one is replaced by a UUID4."""
    ok = client.get("/public/ping", headers={"X-Request-ID": "abc-123"})
    assert ok.headers["x-request-id"] == "abc-123"
    for bad in ("has space", "x" * 65, "semi;colon"):
        resp = client.get("/public/ping", headers={"X-Request-ID": bad})
        assert uuid.UUID(resp.headers["x-request-id"]).version == 4
    missing = client.get("/nope", headers={"X-Request-ID": "req-9"})
    assert missing.json()["error"]["request_id"] == "req-9"


def test_trace_id_header_matches_the_server_span(tf: TokenFactory, otel_memory: OtelMemory) -> None:
    """PAY-604 / NFR-09: x-trace-id is the trace id of the request's server span."""
    with TestClient(_app(tf, instrument=True)) as c:
        resp = c.get("/public/ping")
    server = [s for s in otel_memory.spans() if s.kind == trace.SpanKind.SERVER]
    assert resp.headers["x-trace-id"] == f"{server[-1].context.trace_id:032x}"


def test_body_limit_by_content_length_and_by_stream(client: TestClient) -> None:
    """PAY-604: 413 payload_too_large by Content-Length at once, and by counted streamed bytes."""
    big = client.post("/public/upload", content=b"x" * 101)
    assert big.status_code == 413
    assert big.json()["error"]["code"] == "payload_too_large"
    assert big.json()["error"]["request_id"] == big.headers["x-request-id"]

    def chunks() -> Iterator[bytes]:
        for _ in range(5):
            yield b"y" * 30

    streamed = client.post("/public/upload", content=chunks())
    assert streamed.status_code == 413
    assert streamed.json()["error"]["code"] == "payload_too_large"
    assert client.post("/public/upload", content=b"z" * 100).json() == {"bytes": 100}
    odd = client.post("/public/upload", content=b"ok", headers={"Content-Length": "2"})
    assert odd.status_code == 200


def test_routers_require_jwt_but_public_and_health_do_not(
    client: TestClient, tf: TokenFactory
) -> None:
    """PAY-601: every router except health and public_routers requires a valid JWT."""
    assert client.get("/v1/me").status_code == 401
    assert client.get("/v1/me", headers=_auth(tf)).json() == {"service": "testsvc"}
    assert client.get("/public/ping").status_code == 200
    assert client.get("/livez").status_code == 200
    assert client.get("/readyz").status_code == 200


def test_http_request_log(
    client: TestClient, tf: TokenFactory, caplog_json: JsonLogCapture
) -> None:
    """NFR-07 / NFR-10: one http.request per non-health request, warning on 5xx, caller hashed."""
    client.get("/v1/me", headers=_auth(tf))
    client.get("/v1/fail", headers=_auth(tf))
    client.get("/livez")
    ok, failed = caplog_json.events("http.request")
    assert (ok["method"], ok["route"], ok["status"], ok["level"]) == ("GET", "/v1/me", 200, "info")
    assert len(ok["caller"]) == 16
    assert USER not in caplog_json.text
    assert (failed["status"], failed["level"]) == (500, "warning")
    assert caplog_json.events("http.unhandled")


def test_routers_without_jwt_are_refused() -> None:
    """PAY-601: routers need use_jwt=True (unauthenticated routes go in public_routers)."""
    with pytest.raises(ValueError, match="public_routers"):
        create_app(make_settings(), routers=[APIRouter()], use_jwt=False)


async def test_startup_checks_run_before_any_connection(
    tf: TokenFactory, monkeypatch: pytest.MonkeyPatch, caplog_json: JsonLogCapture
) -> None:
    """C29: a failing check in prod exits 2 before an engine or Redis client is created."""
    created: list[str] = []
    monkeypatch.setattr(app_module, "create_engine", lambda s: created.append("engine"))
    monkeypatch.setattr(app_module, "create_redis", lambda s: created.append("redis"))
    settings = make_settings(env="prod", debug=True, internal_jwt_public_keys=None)
    app = create_app(settings, use_db=True, use_redis=True, instrument=False)
    with pytest.raises(SystemExit) as info:
        async with app.router.lifespan_context(app):
            pass  # pragma: no cover
    assert info.value.code == 2
    assert created == []
    assert app.state.startup_exit_code == 2
    failures = {(e["setting"], e["reason"]) for e in caplog_json.events("startup.check_failed")}
    assert failures == {("DEBUG", "debug_on"), ("INTERNAL_JWT_PUBLIC_KEYS", "missing")}


def test_get_ctx_before_startup() -> None:
    """PAY-605: get_ctx refuses when the lifespan hasn't built the context."""
    request = cast(Request, SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())))
    with pytest.raises(RuntimeError, match="AppContext"):
        get_ctx(request)


def test_background_tasks_start_and_stop_within_timeout(
    tf: TokenFactory, caplog_json: JsonLogCapture
) -> None:
    """PAY-605: background tasks get the shared stop event; stragglers are cancelled."""
    events: list[str] = []

    async def polite(ctx: AppContext, stop: asyncio.Event) -> None:
        events.append("polite-start")
        await stop.wait()
        events.append("polite-stop")

    async def stubborn(ctx: AppContext, stop: asyncio.Event) -> None:
        events.append("stubborn-start")
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            events.append("stubborn-cancelled")
            raise

    async def started(ctx: AppContext) -> None:
        events.append(f"on_startup:{ctx.settings.service_name}")

    app = _app(tf, background=[polite, stubborn], on_startup=started, shutdown_timeout_s=0.2)
    with TestClient(app) as c:
        assert c.get("/livez").status_code == 200
    assert events[0] == "on_startup:testsvc"
    assert set(events[1:]) == {
        "polite-start",
        "stubborn-start",
        "polite-stop",
        "stubborn-cancelled",
    }
    assert [
        e["event"]
        for e in caplog_json.records()
        if e["event"] in {"service.started", "service.stopped"}
    ] == [
        "service.started",
        "service.stopped",
    ]


def test_crashing_background_task_is_restarted_with_backoff(
    tf: TokenFactory, monkeypatch: pytest.MonkeyPatch, caplog_json: JsonLogCapture
) -> None:
    """PAY-605: a crashing task logs background.task.crashed and restarts after a doubling delay."""
    monkeypatch.setattr(app_module, "RESTART_INITIAL_S", 0.01)
    runs: list[int] = []
    done = asyncio.Event()

    async def flaky(ctx: AppContext, stop: asyncio.Event) -> None:
        runs.append(len(runs))
        if len(runs) < 3:
            raise ValueError("bad tick")
        done.set()
        await stop.wait()

    async def wait_done(ctx: AppContext) -> None:
        return None

    app = _app(tf, background=[flaky], on_startup=wait_done)
    with TestClient(app) as c:
        for _ in range(200):
            if done.is_set():
                break
            c.get("/livez")
            asyncio.run(asyncio.sleep(0.01))
    assert len(runs) == 3
    crashed = caplog_json.events("background.task.crashed")
    assert [(e["error_type"], e["level"]) for e in crashed] == [("ValueError", "error")] * 2
    assert "flaky" in crashed[0]["task"]


class FakePipe:
    async def __aenter__(self) -> FakePipe:
        return self

    async def __aexit__(self, *a: object) -> None:
        return None

    def incr(self, key: str) -> None:
        pass

    def expire(self, key: str, ttl: int, *, nx: bool) -> None:
        pass

    async def execute(self) -> list[int]:
        raise redis.exceptions.ConnectionError("redis down")


class FakeRedis:
    closed = False

    def pipeline(self, *, transaction: bool) -> FakePipe:
        return FakePipe()

    async def ping(self) -> bool:
        raise redis.exceptions.ConnectionError("down")

    async def aclose(self) -> None:
        FakeRedis.closed = True


class FakeEngine:
    disposed = False
    down = True

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[None]:
        if self.down:
            raise ConnectionRefusedError(61, "refused")
        yield  # pragma: no cover

    async def dispose(self) -> None:
        FakeEngine.disposed = True


def test_full_context_with_db_and_redis(
    tf: TokenFactory,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    otel_memory: OtelMemory,
    caplog_json: JsonLogCapture,
) -> None:
    """PAY-603 / PAY-605: DB, Redis, migrations checks in /readyz; caller limit fails open."""
    versions = tmp_path / "migrations" / "versions"
    versions.mkdir(parents=True)
    (tmp_path / "migrations" / "script.py.mako").write_text("")
    (versions / "a1.py").write_text('revision = "a1"\ndown_revision = None\n')
    instrumented: list[object] = []

    class FakeInstrumentor:
        def uninstrument(self) -> None:
            instrumented.append("uninstrumented")

    def fake_instrument(engine: object, tp: object, mp: object) -> FakeInstrumentor:
        instrumented.append(engine)
        return FakeInstrumentor()

    monkeypatch.setattr(app_module, "create_engine", lambda s: FakeEngine())
    monkeypatch.setattr(app_module, "create_redis", lambda s: FakeRedis())
    monkeypatch.setattr(otel_module, "_instrument_sqlalchemy", fake_instrument)
    monkeypatch.setattr(app_module, "LOOP_LAG_INTERVAL_S", 0.01)
    app = _app(
        tf,
        use_db=True,
        use_redis=True,
        instrument=True,
        migrations_script_location=str(tmp_path / "migrations"),
    )
    with TestClient(app) as c:
        ready = c.get("/readyz")
        assert ready.status_code == 503
        assert ready.json()["checks"] == {"db": "fail", "migrations": "fail", "redis": "fail"}
        assert c.get("/v1/me", headers=_auth(tf)).status_code == 200  # fail open (PAY-603)
        ctx: AppContext = app.state.ctx
        assert ctx.limiter is not None
        assert ctx.verifier is not None
        assert ctx.telemetry is not None
        asyncio.run(asyncio.sleep(0.05))
    assert FakeRedis.closed
    assert FakeEngine.disposed
    assert isinstance(instrumented[0], FakeEngine)
    assert instrumented[-1] == "uninstrumented"
    errors = otel_memory.metric_points("rate_limiter_errors")
    assert errors == [({"component": "caller"}, 1)]
    assert otel_memory.metric_points("asyncio_event_loop_lag_seconds")
    assert caplog_json.events("ratelimit.backend_error")


def test_service_web_is_exempt_from_the_caller_limit(
    tf: TokenFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C17: create_app wires caller_rate_limit exempting service:web."""
    hits: list[str] = []

    class CountingPipe(FakePipe):
        def incr(self, key: str) -> None:
            hits.append(key)

        async def execute(self) -> list[int]:
            return [1, 1]

    class CountingRedis(FakeRedis):
        def pipeline(self, *, transaction: bool) -> FakePipe:
            return CountingPipe()

    monkeypatch.setattr(app_module, "create_redis", lambda s: CountingRedis())
    with TestClient(_app(tf, use_redis=True)) as c:
        c.get("/v1/me", headers=_auth(tf, sub="service:web"))
        assert hits == []
        c.get("/v1/me", headers=_auth(tf))
        assert len(hits) == 1
        assert hits[0].startswith("testsvc:rl:caller:")
