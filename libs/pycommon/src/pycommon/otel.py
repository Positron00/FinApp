"""OpenTelemetry setup: providers, instrumentors, helpers (platform LLD §3.3; NFR-09, NFR-10).

``OTEL_SEMCONV_STABILITY_OPT_IN=http`` must be in the process environment before any
``opentelemetry.instrumentation`` import, or the HTTP instrumentations keep the old conventions.
"""

from __future__ import annotations

import os

os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http")

# The imports below must follow the setdefault above.
import contextlib
import logging
import secrets
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit, urlunsplit

from opentelemetry import _logs, metrics, trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.instrumentation.system_metrics import SystemMetricsInstrumentor
from opentelemetry.metrics import CallbackOptions, Histogram, Meter, ObservableGauge
from opentelemetry.metrics import Observation as OtelObservation
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import (
    Counter,
    MeterProvider,
    ObservableCounter,
    ObservableUpDownCounter,
    UpDownCounter,
)
from opentelemetry.sdk.metrics import Histogram as SdkHistogram
from opentelemetry.sdk.metrics import ObservableGauge as SdkObservableGauge
from opentelemetry.sdk.metrics.export import (
    AggregationTemporality,
    MetricReader,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
)
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased, Sampler
from opentelemetry.trace import Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from pycommon.logging import get_logger, log_event, setup_logging

if TYPE_CHECKING:
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncEngine

    from pycommon.settings import BaseServiceSettings

__all__ = [
    "SECONDS_BUCKETS",
    "SETTLEMENT_BUCKETS",
    "Observation",
    "Telemetry",
    "build_resource",
    "context_from_traceparent",
    "histogram",
    "inject_traceparent",
    "meter",
    "new_traceparent",
    "observable_gauge",
    "setup",
    "tracer",
]

_log = get_logger("pycommon.otel")

DEFAULT_SAMPLER: Final = "parentbased_always_on"
DEFAULT_OTLP_ENDPOINT: Final = "http://otel-collector:4317"
DEFAULT_METRIC_INTERVAL_MS: Final = 15000
SECONDS_BUCKETS: Final = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 15, 30)
SETTLEMENT_BUCKETS: Final = (1, 2, 5, 10, 15, 20, 30, 45, 60, 90, 120, 300, 600, 1800, 3600)

Observation = Mapping[frozenset[tuple[str, str]], float]

_PROPAGATOR: Final = TraceContextTextMapPropagator()


@dataclass(frozen=True)
class Telemetry:
    tracer_provider: TracerProvider
    meter_provider: MeterProvider
    logger_provider: LoggerProvider
    instrumentors: tuple[Any, ...] = field(default=(), repr=False)
    owns_providers: bool = field(default=True, repr=False)

    def shutdown(self, timeout_s: float = 5.0) -> None:
        """Flush and stop the providers; remove the global instrumentors this setup installed."""
        for inst in self.instrumentors:
            if getattr(inst, "is_instrumented_by_opentelemetry", False):
                inst.uninstrument()
        if not self.owns_providers:  # shared providers (pycommon.testing.otel_memory)
            return
        timeout_ms = int(timeout_s * 1000)
        self.tracer_provider.force_flush(timeout_ms)
        self.tracer_provider.shutdown()
        self.meter_provider.shutdown(timeout_millis=timeout_ms)
        self.logger_provider.shutdown()


def build_resource(settings: BaseServiceSettings) -> Resource:
    return Resource.create(
        {
            "service.name": settings.service_name,
            "service.version": settings.version,
            "deployment.environment.name": str(settings.env),
        }
    )


def _sampler() -> Sampler | None:
    """``ParentBased(ALWAYS_ON)`` unless ``OTEL_TRACES_SAMPLER`` names another one (R27, C23)."""
    name = os.environ.get("OTEL_TRACES_SAMPLER", "").strip()
    if not name or name == DEFAULT_SAMPLER:
        return ParentBased(ALWAYS_ON)
    log_event(_log, logging.WARNING, "otel.sampler.overridden", sampler=name)
    return None  # the SDK reads OTEL_TRACES_SAMPLER itself


def _endpoint() -> str:
    return os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", DEFAULT_OTLP_ENDPOINT)


def _build_providers(
    resource: Resource,
    *,
    span_exporter: SpanExporter | None,
    metric_reader: MetricReader | None,
) -> tuple[TracerProvider, MeterProvider, LoggerProvider, bool]:
    """Create the three providers (and ``True``: the caller owns them).

    ``pycommon.testing.otel_memory`` replaces this seam with shared in-memory providers.
    """
    tp = TracerProvider(resource=resource, sampler=_sampler())
    if span_exporter is not None:
        tp.add_span_processor(SimpleSpanProcessor(span_exporter))
    else:
        tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=_endpoint())))
    if metric_reader is None:
        interval = float(
            os.environ.get("OTEL_METRIC_EXPORT_INTERVAL", str(DEFAULT_METRIC_INTERVAL_MS))
        )
        temporality: dict[type, AggregationTemporality] = dict.fromkeys(
            _INSTRUMENT_TYPES, AggregationTemporality.CUMULATIVE
        )
        metric_reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=_endpoint(), preferred_temporality=temporality),
            export_interval_millis=interval,
        )
    mp = MeterProvider(resource=resource, metric_readers=[metric_reader])
    lp = LoggerProvider(resource=resource)
    if span_exporter is None:  # an injected span exporter means a test: logs stay local
        lp.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter(endpoint=_endpoint())))
    return tp, mp, lp, True


_INSTRUMENT_TYPES: Final = (
    Counter,
    UpDownCounter,
    SdkHistogram,
    ObservableCounter,
    ObservableUpDownCounter,
    SdkObservableGauge,
)


def _set_globals(tp: TracerProvider, mp: MeterProvider, lp: LoggerProvider) -> None:
    if trace.get_tracer_provider() is not tp:
        trace.set_tracer_provider(tp)
    if metrics.get_meter_provider() is not mp:
        metrics.set_meter_provider(mp)
    if _logs.get_logger_provider() is not lp:
        _logs.set_logger_provider(lp)


# --- hooks: never record query strings, headers or Redis arguments (PAY-NFR-07) ---------------


def _drop_attribute(span: Any, key: str) -> None:
    attrs = getattr(span, "_attributes", None)
    if attrs is not None and key in attrs:
        with contextlib.suppress(TypeError):  # immutable once the span ended
            del attrs[key]


def _without_query(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _server_request_hook(span: Any, scope: Mapping[str, Any] | None) -> None:
    if span is None or not span.is_recording():
        return
    _drop_attribute(span, "url.query")
    attrs = getattr(span, "attributes", None) or {}
    for key in ("url.full", "http.url", "http.target"):
        value = attrs.get(key)
        if isinstance(value, str) and "?" in value:
            span.set_attribute(key, value.split("?", 1)[0])


def _client_request_hook(span: Any, request: Any) -> None:
    if span is None or not span.is_recording():
        return
    _drop_attribute(span, "url.query")
    attrs = getattr(span, "attributes", None) or {}
    for key in ("url.full", "http.url"):
        value = attrs.get(key)
        if isinstance(value, str):
            span.set_attribute(key, _without_query(value))


async def _async_client_request_hook(span: Any, request: Any) -> None:
    _client_request_hook(span, request)


def _redis_request_hook(span: Any, instance: Any, args: Any, kwargs: Any) -> None:
    """Keep only the command name in the statement attribute: arguments can carry session IDs."""
    if span is None or not span.is_recording():
        return
    command = ""
    if isinstance(args, list | tuple) and args and isinstance(args[0], str | bytes):
        first = args[0]
        command = first.decode(errors="replace") if isinstance(first, bytes) else first
    attrs = getattr(span, "attributes", None) or {}
    for key in ("db.statement", "db.query.text"):
        if key in attrs:
            if command:
                span.set_attribute(key, command)
            else:
                value = str(attrs[key])
                span.set_attribute(key, "\n".join(ln.split(" ", 1)[0] for ln in value.splitlines()))


def _instrument_sqlalchemy(
    engine: AsyncEngine, tp: TracerProvider, mp: MeterProvider
) -> SQLAlchemyInstrumentor:
    inst = SQLAlchemyInstrumentor()
    if inst.is_instrumented_by_opentelemetry:
        inst.uninstrument()
    # opentelemetry-instrumentation-sqlalchemy 0.66b1 declares sqlalchemy < 2.1 and otherwise
    # instruments nothing. Its engine event hooks work on 2.1 (verified in S6 and by
    # tests/integration/test_db_spans.py), so skip the version gate until a release supports 2.1.
    inst.instrument(
        engine=engine.sync_engine,
        enable_commenter=False,
        tracer_provider=tp,
        meter_provider=mp,
        skip_dep_check=True,
    )
    return inst


def setup(
    app: FastAPI | None,
    settings: BaseServiceSettings,
    *,
    engine: AsyncEngine | None = None,
    redis_instrument: bool = True,
    span_exporter: SpanExporter | None = None,
    metric_reader: MetricReader | None = None,
) -> Telemetry:
    resource = build_resource(settings)
    tp, mp, lp, owned = _build_providers(
        resource, span_exporter=span_exporter, metric_reader=metric_reader
    )
    _set_globals(tp, mp, lp)
    instrumentors: list[Any] = []
    if app is not None:
        FastAPIInstrumentor.instrument_app(
            app,
            excluded_urls="livez,readyz",
            server_request_hook=_server_request_hook,
            tracer_provider=tp,
            meter_provider=mp,
            http_capture_headers_server_request=[],
            http_capture_headers_server_response=[],
        )
    httpx_inst = HTTPXClientInstrumentor()
    if not httpx_inst.is_instrumented_by_opentelemetry:
        httpx_inst.instrument(
            tracer_provider=tp,
            meter_provider=mp,
            request_hook=_client_request_hook,
            async_request_hook=_async_client_request_hook,
        )
        instrumentors.append(httpx_inst)
    if redis_instrument:
        redis_inst = RedisInstrumentor()
        if not redis_inst.is_instrumented_by_opentelemetry:
            redis_inst.instrument(tracer_provider=tp, request_hook=_redis_request_hook)
            instrumentors.append(redis_inst)
    if engine is not None:
        instrumentors.append(_instrument_sqlalchemy(engine, tp, mp))
    system = SystemMetricsInstrumentor(
        config={"process.cpu.time": ["user", "system"], "process.memory.usage": None}
    )
    if not system.is_instrumented_by_opentelemetry:
        system.instrument(meter_provider=mp)
        instrumentors.append(system)
    setup_logging(settings, resource)
    return Telemetry(tp, mp, lp, tuple(instrumentors), owns_providers=owned)


def meter(name: str = "pycommon") -> Meter:
    return metrics.get_meter(name)


def tracer(name: str = "pycommon") -> Tracer:
    return trace.get_tracer(name)


def histogram(name: str, *, unit: str, description: str, buckets: Sequence[float]) -> Histogram:
    return meter().create_histogram(
        name, unit=unit, description=description, explicit_bucket_boundaries_advisory=list(buckets)
    )


def observable_gauge(
    name: str, *, unit: str, description: str, callback: Callable[[], Observation]
) -> ObservableGauge:
    """Register a gauge whose ``callback`` runs at collection time on the reader thread (R26, C18).

    A raising callback logs ``metrics.gauge_query_failed`` and yields no observation that cycle.
    """

    def _observe(_options: CallbackOptions) -> Iterable[OtelObservation]:
        try:
            values = callback()
        except Exception as exc:
            log_event(
                _log,
                logging.WARNING,
                "metrics.gauge_query_failed",
                gauge=name,
                error_type=type(exc).__name__,
            )
            return []
        return [OtelObservation(float(v), attributes=dict(labels)) for labels, v in values.items()]

    return meter().create_observable_gauge(
        name, callbacks=[_observe], unit=unit, description=description
    )


def inject_traceparent() -> str | None:
    """The W3C ``traceparent`` of the current span, or ``None`` when no valid span is active."""
    if not trace.get_current_span().get_span_context().is_valid:
        return None
    carrier: dict[str, str] = {}
    _PROPAGATOR.inject(carrier)
    return carrier.get("traceparent")


def new_traceparent() -> str:
    """A fresh, valid, unsampled ``traceparent``: ``00-<32 hex>-<16 hex>-00``."""
    trace_id = secrets.token_hex(16)
    span_id = secrets.token_hex(8)
    while int(trace_id, 16) == 0:  # pragma: no cover  (probability 2**-128)
        trace_id = secrets.token_hex(16)
    while int(span_id, 16) == 0:  # pragma: no cover
        span_id = secrets.token_hex(8)
    return f"00-{trace_id}-{span_id}-00"


def context_from_traceparent(tp: str | None) -> Context:
    """The remote parent context for ``tp``; an empty ``Context`` when it is ``None`` or invalid."""
    if not tp:
        return Context()
    ctx = _PROPAGATOR.extract({"traceparent": tp}, context=Context())
    if not trace.get_current_span(ctx).get_span_context().is_valid:
        return Context()
    return ctx
