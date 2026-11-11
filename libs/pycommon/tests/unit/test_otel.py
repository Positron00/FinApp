"""OpenTelemetry setup and helpers (platform LLD §3.3; NFR-09, NFR-10, R26, R27, C18, C23)."""

from __future__ import annotations

import io
import logging
import os
import subprocess
import sys
from collections.abc import Iterator

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.sdk.metrics.export import (
    AggregationTemporality,
    InMemoryMetricReader,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pycommon import otel
from pycommon.otel import (
    SECONDS_BUCKETS,
    SETTLEMENT_BUCKETS,
    build_resource,
    context_from_traceparent,
    histogram,
    inject_traceparent,
    new_traceparent,
    observable_gauge,
)
from pycommon.testing import JsonLogCapture, OtelMemory

from tests.helpers import make_settings


@pytest.fixture
def quiet_stdout(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    monkeypatch.setattr("sys.stdout", io.StringIO())
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def test_resource_attributes() -> None:
    """NFR-10: every signal carries service.name, service.version, deployment.environment.name."""
    res = build_resource(make_settings(version="1.4.0", env="uat"))
    assert res.attributes["service.name"] == "testsvc"
    assert res.attributes["service.version"] == "1.4.0"
    assert res.attributes["deployment.environment.name"] == "uat"


def test_semconv_opt_in_is_set_before_instrumentation_import() -> None:
    """NFR-10: importing pycommon.otel sets OTEL_SEMCONV_STABILITY_OPT_IN=http first."""
    code = (
        "import os, sys\n"
        "import pycommon.otel\n"
        "from opentelemetry.instrumentation._semconv import (\n"
        "    _OpenTelemetrySemanticConventionStability as S,\n"
        "    _OpenTelemetryStabilitySignalType as T,\n"
        "    _StabilityMode as M)\n"
        "S._initialize()\n"
        "print(os.environ['OTEL_SEMCONV_STABILITY_OPT_IN'])\n"
        "print(S._get_opentelemetry_stability_opt_in_mode(T.HTTP) == M.HTTP)\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "OTEL_SEMCONV_STABILITY_OPT_IN"}
    out = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True
    )
    assert out.stdout.split() == ["http", "True"]
    env["OTEL_SEMCONV_STABILITY_OPT_IN"] = "http/dup"
    out = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True
    )
    assert out.stdout.split()[0] == "http/dup"


@pytest.mark.parametrize("value", [None, "parentbased_always_on"])
def test_sampler_is_parent_based_always_on(
    monkeypatch: pytest.MonkeyPatch,
    quiet_stdout: None,
    otel_memory: OtelMemory,
    caplog_json: JsonLogCapture,
    value: str | None,
) -> None:
    """C23 / R27: ParentBased(ALWAYS_ON) when OTEL_TRACES_SAMPLER is unset or the default."""
    if value is None:
        monkeypatch.delenv("OTEL_TRACES_SAMPLER", raising=False)
    else:
        monkeypatch.setenv("OTEL_TRACES_SAMPLER", value)
    tel = otel.setup(
        None,
        make_settings(),
        span_exporter=InMemorySpanExporter(),
        metric_reader=InMemoryMetricReader(),
    )
    try:
        desc = tel.tracer_provider.sampler.get_description()
        assert desc.startswith("ParentBased{root:AlwaysOnSampler")
        assert not caplog_json.events("otel.sampler.overridden")
    finally:
        tel.shutdown()


def test_other_sampler_is_logged(
    monkeypatch: pytest.MonkeyPatch,
    quiet_stdout: None,
    otel_memory: OtelMemory,
    caplog_json: JsonLogCapture,
) -> None:
    """C23: any other OTEL_TRACES_SAMPLER logs otel.sampler.overridden {sampler} at warning."""
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "always_off")
    tel = otel.setup(
        None,
        make_settings(),
        span_exporter=InMemorySpanExporter(),
        metric_reader=InMemoryMetricReader(),
    )
    try:
        events = caplog_json.events("otel.sampler.overridden")
        assert [(e["level"], e["sampler"]) for e in events] == [("warning", "always_off")]
        assert tel.tracer_provider.sampler.get_description() == "AlwaysOffSampler"
    finally:
        tel.shutdown()


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/items/{item_id}")
    async def item(item_id: int) -> dict[str, int]:
        return {"id": item_id}

    @app.get("/livez")
    async def livez() -> dict[str, str]:
        return {"status": "ok"}

    return app


def test_server_request_metric_uses_stable_name_and_drops_query(
    quiet_stdout: None, otel_memory: OtelMemory
) -> None:
    """NFR-10 / PAY-NFR-07: http.server.request.duration with http.route; url.query dropped."""
    app = _app()
    tel = otel.setup(app, make_settings(), redis_instrument=False)
    try:
        with TestClient(app) as client:
            assert client.get("/items/7?token=secret-q").status_code == 200
            assert client.get("/livez").status_code == 200
        points = otel_memory.metric_points("http.server.request.duration")
        routes = {p[0].get("http.route") for p in points}
        assert routes == {"/items/{item_id}"}
        server = [s for s in otel_memory.spans() if s.kind == trace.SpanKind.SERVER]
        assert len(server) == 1  # /livez is excluded from tracing
        attrs = dict(server[0].attributes or {})
        assert "url.query" not in attrs
        assert "secret-q" not in str(attrs)
        assert attrs.get("http.route") == "/items/{item_id}"
        assert "http_server_duration" not in str(otel_memory.metric_names())
    finally:
        tel.shutdown()


async def test_client_spans_drop_query_and_headers(
    quiet_stdout: None, otel_memory: OtelMemory
) -> None:
    """PAY-NFR-07: httpx client spans carry no query string and no headers."""
    tel = otel.setup(None, make_settings(), redis_instrument=False)
    try:
        with respx.mock:
            respx.get("https://bank.test/v1/x").mock(return_value=httpx.Response(200))
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    "https://bank.test/v1/x?api_key=q-secret", headers={"Authorization": "Bearer t"}
                )
        assert r.status_code == 200
        clients = [s for s in otel_memory.spans() if s.kind == trace.SpanKind.CLIENT]
        assert clients
        attrs = dict(clients[-1].attributes or {})
        assert attrs.get("url.full") == "https://bank.test/v1/x"
        assert "q-secret" not in str(attrs)
        assert "Bearer" not in str(attrs)
    finally:
        tel.shutdown()


def test_hooks_ignore_non_recording_spans() -> None:
    """PAY-NFR-07: the hooks are safe on non-recording spans."""
    span = trace.INVALID_SPAN
    otel._server_request_hook(span, {})
    otel._client_request_hook(span, None)
    otel._redis_request_hook(span, None, ("GET", "k"), {})


def test_server_hook_strips_query_from_url_attributes(otel_memory: OtelMemory) -> None:
    """PAY-NFR-07: url.query is removed and full URLs lose their query."""
    tracer = otel_memory.tracer_provider.get_tracer("t")
    with tracer.start_as_current_span(
        "s", attributes={"url.query": "a=1", "url.full": "http://h/p?a=1", "http.target": "/p"}
    ) as span:
        otel._server_request_hook(span, {})
    attrs = dict(otel_memory.spans()[-1].attributes or {})
    assert attrs == {"url.full": "http://h/p", "http.target": "/p"}
    otel._drop_attribute(otel_memory.spans()[-1], "url.full")  # ended span: no error


def test_redis_hook_records_only_the_command(otel_memory: OtelMemory) -> None:
    """PAY-NFR-07: Redis spans keep the command name, never its arguments."""
    tracer = otel_memory.tracer_provider.get_tracer("t")
    with tracer.start_as_current_span("SET", attributes={"db.statement": "SET ? ?"}) as span:
        otel._redis_request_hook(span, None, ("SET", "web:sess:abc", "v"), {})
    with tracer.start_as_current_span("XADD", attributes={"db.query.text": "XADD ? ?"}) as span:
        otel._redis_request_hook(span, None, (b"XADD", "stream", "x"), {})
    with tracer.start_as_current_span(
        "pipe", attributes={"db.statement": "INCR ?\nEXPIRE ? ? ?"}
    ) as span:
        otel._redis_request_hook(span, None, [], {})
    a, b, c = (dict(s.attributes or {}) for s in otel_memory.spans()[-3:])
    assert a["db.statement"] == "SET"
    assert b["db.query.text"] == "XADD"
    assert c["db.statement"] == "INCR\nEXPIRE"


def test_histogram_buckets_are_applied(otel_memory: OtelMemory) -> None:
    """NFR-10: histogram() uses the given explicit buckets."""
    h = histogram("work_duration_seconds", unit="s", description="d", buckets=SECONDS_BUCKETS)
    h.record(0.3, {"task": "x"})
    s = histogram("settle_seconds", unit="s", description="d", buckets=SETTLEMENT_BUCKETS)
    s.record(42)
    ((attrs, point),) = otel_memory.metric_points("work_duration_seconds")
    assert attrs == {"task": "x"}
    assert tuple(point.explicit_bounds) == SECONDS_BUCKETS
    ((_, settle),) = otel_memory.metric_points("settle_seconds")
    assert tuple(settle.explicit_bounds) == SETTLEMENT_BUCKETS


def test_observable_gauge_reports_at_collection_and_skips_failures(
    otel_memory: OtelMemory, caplog_json: JsonLogCapture
) -> None:
    """R26 / C18: the callback runs at collection; a raising callback yields no observation."""
    state = {"value": 3.0, "fail": False}

    def callback() -> dict[frozenset[tuple[str, str]], float]:
        if state["fail"]:
            raise RuntimeError("db down")
        return {frozenset({("status", "pending")}): state["value"]}

    observable_gauge("queue_depth", unit="{entry}", description="d", callback=callback)
    assert otel_memory.metric_points("queue_depth") == [({"status": "pending"}, 3.0)]
    state["value"] = 5.0
    assert otel_memory.metric_points("queue_depth") == [({"status": "pending"}, 5.0)]
    state["fail"] = True
    assert otel_memory.metric_points("queue_depth") == []
    (event,) = caplog_json.events("metrics.gauge_query_failed")
    assert (event["gauge"], event["error_type"], event["level"]) == (
        "queue_depth",
        "RuntimeError",
        "warning",
    )


def test_gauges_refuse_unit_one(otel_memory: OtelMemory) -> None:
    """C63: a gauge with unit "1" would be exported as <name>_ratio, so it is refused."""
    with pytest.raises(ValueError, match="_ratio"):
        observable_gauge("x_gauge", unit="1", description="d", callback=dict)


def test_pycommon_gauges_have_no_ratio_unit(otel_memory: OtelMemory) -> None:
    """C63: circuit_breaker_state uses a curly-brace unit, so Prometheus keeps its name."""
    from pycommon.http import CircuitBreaker  # noqa: PLC0415

    breaker = CircuitBreaker("unit-check", failures=5, reset_s=30)
    assert otel_memory.metric_points("circuit_breaker_state") == [({"target": "unit-check"}, 0)]
    assert breaker.state == 0
    assert otel_memory.metric_unit("circuit_breaker_state") == "{state}"


def test_traceparent_round_trip(otel_memory: OtelMemory) -> None:
    """NFR-09: inject_traceparent / context_from_traceparent continue the same trace."""
    assert inject_traceparent() is None
    tracer = otel_memory.tracer_provider.get_tracer("t")
    with tracer.start_as_current_span("producer") as span:
        tp = inject_traceparent()
        producer_ctx = span.get_span_context()
    assert tp is not None
    version, trace_id, span_id, flags = tp.split("-")
    assert (version, trace_id, span_id) == (
        "00",
        f"{producer_ctx.trace_id:032x}",
        f"{producer_ctx.span_id:016x}",
    )
    assert int(flags, 16) & 1  # sampled
    with tracer.start_as_current_span("consumer", context=context_from_traceparent(tp)) as child:
        assert child.get_span_context().trace_id == producer_ctx.trace_id


def test_new_traceparent_and_invalid_input() -> None:
    """C22: new_traceparent is valid and unsampled; invalid input gives an empty context."""
    tp = new_traceparent()
    parts = tp.split("-")
    assert [len(p) for p in parts] == [2, 32, 16, 2]
    assert parts[0] == "00"
    assert parts[3] == "00"
    assert new_traceparent() != tp
    ctx = context_from_traceparent(tp)
    span_ctx = trace.get_current_span(ctx).get_span_context()
    assert span_ctx.is_valid
    assert not span_ctx.trace_flags.sampled
    for bad in (None, "", "garbage", "00-" + "0" * 32 + "-" + "0" * 16 + "-01"):
        assert dict(context_from_traceparent(bad)) == {}


def test_telemetry_shutdown_uninstruments(quiet_stdout: None, otel_memory: OtelMemory) -> None:
    """NFR-10: Telemetry.shutdown removes the global instrumentors it installed."""
    tel = otel.setup(None, make_settings())
    assert HTTPXClientInstrumentor().is_instrumented_by_opentelemetry
    assert RedisInstrumentor().is_instrumented_by_opentelemetry
    assert not tel.owns_providers
    tel.shutdown()
    assert not HTTPXClientInstrumentor().is_instrumented_by_opentelemetry
    assert not RedisInstrumentor().is_instrumented_by_opentelemetry


def test_default_providers_use_otlp_and_cumulative_temporality(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NFR-10: without injected exporters, OTLP to the endpoint every 15 s, cumulative."""
    seen: dict[str, object] = {}
    real_reader = PeriodicExportingMetricReader
    real_exporter = OTLPMetricExporter

    def reader(exporter: object, *, export_interval_millis: float) -> object:
        seen["interval"] = export_interval_millis
        return real_reader(exporter, export_interval_millis=export_interval_millis)  # type: ignore[arg-type]

    def exporter(*, endpoint: str, preferred_temporality: dict[type, object]) -> object:
        seen["endpoint"] = endpoint
        seen["temporality"] = set(preferred_temporality.values())
        return real_exporter(endpoint=endpoint, preferred_temporality=preferred_temporality)  # type: ignore[arg-type]

    monkeypatch.setattr("pycommon.otel.PeriodicExportingMetricReader", reader)
    monkeypatch.setattr("pycommon.otel.OTLPMetricExporter", exporter)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.invalid:4317")
    res = build_resource(make_settings())
    tp, mp, lp, owned = otel._build_providers(res, span_exporter=None, metric_reader=None)
    try:
        assert owned
        assert seen["interval"] == 15000
        assert seen["endpoint"] == "http://collector.invalid:4317"
        assert seen["temporality"] == {AggregationTemporality.CUMULATIVE}
    finally:
        tp.shutdown()
        mp.shutdown(timeout_millis=100)
        lp.shutdown()


_MODULE_COUNTER = otel.meter("isolation-test").create_counter("isolation_hits", unit="1")


@pytest.mark.parametrize("hits", [2, 3])
def test_otel_memory_isolates_tests_without_resetting_globals(
    otel_memory: OtelMemory, hits: int
) -> None:
    """C61, C63: an instrument created at import time reports to the same session provider in
    every test, and each test sees only its own measurements."""
    from opentelemetry import metrics  # noqa: PLC0415

    assert metrics.get_meter_provider() is otel_memory.meter_provider
    for _ in range(hits):
        _MODULE_COUNTER.add(1, {"k": "v"})
    assert otel_memory.metric_points("isolation_hits") == [({"k": "v"}, hits)]
    _MODULE_COUNTER.add(1, {"k": "v"})
    assert otel_memory.metric_points("isolation_hits") == [({"k": "v"}, hits + 1)]


def test_reregistered_gauge_uses_the_latest_callback(otel_memory: OtelMemory) -> None:
    """C63: a second app instance re-registering a gauge replaces the callback, not ignored."""
    first = observable_gauge("rereg_gauge", unit="{entry}", description="d", callback=dict)
    second = observable_gauge(
        "rereg_gauge", unit="{entry}", description="d", callback=lambda: {frozenset(): 7.0}
    )
    assert first is second
    assert otel_memory.metric_points("rereg_gauge") == [({}, 7.0)]


def test_otel_memory_adds_histogram_deltas(otel_memory: OtelMemory) -> None:
    """C63: histogram points accumulate across reads within one test."""
    h = histogram("iso_duration_seconds", unit="s", description="d", buckets=SECONDS_BUCKETS)
    h.record(0.2)
    ((_, first),) = otel_memory.metric_points("iso_duration_seconds")
    assert first.count == 1
    h.record(3.0)
    ((_, second),) = otel_memory.metric_points("iso_duration_seconds")
    assert (second.count, second.sum, second.min, second.max) == (2, 3.2, 0.2, 3.0)
    assert sum(second.bucket_counts) == 2
