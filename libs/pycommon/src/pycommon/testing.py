"""Pytest plugin ``pycommon.testing`` (platform LLD §3.14), registered with ``pytest_plugins``.

Fixtures: ``pg_bootstrapped`` (Postgres 16 with ``deploy/compose/init`` mounted), ``db_env_for``,
``alembic_upgrade``, ``redis_acl`` (Redis 7 with ``deploy/compose/redis`` and its entrypoint),
``redis_env_for``, ``token_factory``, ``otel_memory``, ``fake_clock`` and ``caplog_json``.
Service suites reuse them, so the bootstrap SQL and ACLs run in every integration suite
(PAY-606, 607).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import secrets
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final
from uuid import UUID

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from opentelemetry import _logs, metrics, trace
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.metrics import Counter as SdkCounter
from opentelemetry.sdk.metrics import Histogram as SdkHistogram
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics import UpDownCounter as SdkUpDownCounter
from opentelemetry.sdk.metrics.export import (
    AggregationTemporality,
    HistogramDataPoint,
    InMemoryMetricReader,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased

from pycommon import Clock, SystemClock
from pycommon import otel as _otel
from pycommon.auth import Issuer, Keyset, TokenSigner
from pycommon.logging import JsonFormatter

__all__ = [
    "DB_ROLE_PASSWORD_ENV",
    "REDIS_USER_PASSWORD_ENV",
    "FakeClock",
    "JsonLogCapture",
    "OtelMemory",
    "PgBootstrapped",
    "RedisAcl",
    "TokenFactory",
    "find_repo_root",
    "reset_otel_globals",
    "session_otel",
]

FAKE_EPOCH: Final = datetime(2026, 11, 10, 12, 0, 0, tzinfo=UTC)

# Role → env var holding its password (platform LLD §2.1).
DB_ROLE_PASSWORD_ENV: Final[dict[str, str]] = {
    "identity_migrator": "IDENTITY_DB_MIGRATOR_PASSWORD",
    "identity_app": "IDENTITY_DB_PASSWORD",
    "payroll_migrator": "PAYROLL_DB_MIGRATOR_PASSWORD",
    "payroll_app": "PAYROLL_DB_PASSWORD",
    "payouts_migrator": "PAYOUTS_DB_MIGRATOR_PASSWORD",
    "payouts_app": "PAYOUTS_DB_PASSWORD",
    "bank_stub_migrator": "BANK_STUB_DB_MIGRATOR_PASSWORD",
    "bank_stub_app": "BANK_STUB_DB_PASSWORD",
    "postgres_exporter": "POSTGRES_EXPORTER_PASSWORD",
}
# ACL user → env var holding its password (platform LLD §2.2).
REDIS_USER_PASSWORD_ENV: Final[dict[str, str]] = {
    "default": "REDIS_ADMIN_PASSWORD",
    "web": "REDIS_WEB_PASSWORD",
    "identity": "REDIS_IDENTITY_PASSWORD",
    "payroll": "REDIS_PAYROLL_PASSWORD",
    "payout-worker": "REDIS_PAYOUT_WORKER_PASSWORD",
    "exporter": "REDIS_EXPORTER_PASSWORD",
}
# Service package → (schema, alembic dir under services/).
SERVICE_SCHEMAS: Final[dict[str, str]] = {
    "identity": "identity",
    "payroll": "payroll",
    "payout_worker": "payouts",
    "bank_stub": "bank_stub",
}

POSTGRES_IMAGE: Final = "postgres:16-alpine"
REDIS_IMAGE: Final = "redis:7-alpine"


# --- clock -------------------------------------------------------------------------------------


class FakeClock:
    """A controllable :class:`pycommon.Clock`: ``sleep`` advances time at once and records it."""

    def __init__(self, start: datetime = FAKE_EPOCH) -> None:
        self._start = start
        self._offset = 0.0
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self._start + timedelta(seconds=self._offset)

    def monotonic(self) -> float:
        return 1000.0 + self._offset

    def advance(self, seconds: float) -> None:
        self._offset += seconds

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self._offset += max(0.0, seconds)
        await asyncio.sleep(0)


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


# --- OpenTelemetry -----------------------------------------------------------------------------


def reset_otel_globals() -> None:
    """Allow ``set_*_provider`` again (OTel only allows it once per process).

    Only :func:`session_otel` calls this, once; tests must not reset the globals (C61).
    """
    trace._TRACER_PROVIDER_SET_ONCE._done = False
    trace._TRACER_PROVIDER = None
    metrics_internal: Any = __import__("opentelemetry.metrics._internal", fromlist=["_"])
    metrics_internal._METER_PROVIDER_SET_ONCE._done = False
    metrics_internal._METER_PROVIDER = None
    logs_internal: Any = __import__("opentelemetry._logs._internal", fromlist=["_"])
    logs_internal._LOGGER_PROVIDER_SET_ONCE._done = False
    logs_internal._LOGGER_PROVIDER = None


_DELTA_TYPES: Final = (SdkCounter, SdkUpDownCounter, SdkHistogram)


@dataclass
class _SessionOtel:
    """One in-memory provider set per test process (C61/C63: no global resets between tests)."""

    span_exporter: InMemorySpanExporter
    metric_reader: InMemoryMetricReader
    log_exporter: InMemoryLogRecordExporter
    tracer_provider: TracerProvider
    meter_provider: MeterProvider
    logger_provider: LoggerProvider


_SESSION_OTEL: _SessionOtel | None = None


def session_otel() -> _SessionOtel:
    """Create (once) and install the process-wide in-memory providers as the OTel globals.

    Synchronous counters and histograms use delta temporality, so each test sees only its own
    measurements (``OtelMemory`` adds the deltas up again); observable instruments stay
    cumulative because their callbacks report absolute values.
    """
    global _SESSION_OTEL  # noqa: PLW0603  (process-wide by design)
    if _SESSION_OTEL is not None:
        return _SESSION_OTEL
    resource = Resource.create({"service.name": "pycommon-test"})
    spans = InMemorySpanExporter()
    reader = InMemoryMetricReader(
        preferred_temporality=dict.fromkeys(_DELTA_TYPES, AggregationTemporality.DELTA)
    )
    logs = InMemoryLogRecordExporter()  # type: ignore[no-untyped-call]
    tp = TracerProvider(resource=resource, sampler=ParentBased(ALWAYS_ON))
    tp.add_span_processor(SimpleSpanProcessor(spans))
    mp = MeterProvider(resource=resource, metric_readers=[reader])
    lp = LoggerProvider(resource=resource)
    lp.add_log_record_processor(SimpleLogRecordProcessor(logs))
    reset_otel_globals()
    trace.set_tracer_provider(tp)
    metrics.set_meter_provider(mp)
    _logs.set_logger_provider(lp)
    _SESSION_OTEL = _SessionOtel(spans, reader, logs, tp, mp, lp)
    return _SESSION_OTEL


def pytest_configure(config: pytest.Config) -> None:
    """Install the session providers before any test, so no suite binds to a stale provider."""
    session_otel()


def _merge(old: Any, new: Any, *, delta: bool) -> Any:
    if not delta or old is None:
        return new
    if isinstance(new, HistogramDataPoint):
        return dataclasses.replace(
            new,
            start_time_unix_nano=old.start_time_unix_nano,
            count=old.count + new.count,
            sum=old.sum + new.sum,
            bucket_counts=[
                a + b for a, b in zip(old.bucket_counts, new.bucket_counts, strict=True)
            ],
            min=min(old.min, new.min),
            max=max(old.max, new.max),
        )
    return dataclasses.replace(new, value=old.value + new.value)


@dataclass
class OtelMemory:
    span_exporter: InMemorySpanExporter
    metric_reader: InMemoryMetricReader
    log_exporter: InMemoryLogRecordExporter
    tracer_provider: TracerProvider
    meter_provider: MeterProvider
    logger_provider: LoggerProvider
    _points: dict[tuple[str, frozenset[tuple[str, Any]]], Any] = field(
        default_factory=dict, repr=False
    )
    _units: dict[str, str] = field(default_factory=dict, repr=False)
    _delta: set[str] = field(default_factory=set, repr=False)

    def _collect(self) -> None:
        """Fold one collection into this test's points.

        Delta series (counters, histograms) are added up; every other series (gauges, observable
        counters) is replaced by what this collection reports, so a series that stops being
        observed disappears.
        """
        data = self.metric_reader.get_metrics_data()
        for k in [k for k in self._points if k[0] not in self._delta]:
            del self._points[k]
        if data is None:
            return
        for rm in data.resource_metrics:
            for sm in rm.scope_metrics:
                for m in sm.metrics:
                    self._units[m.name] = m.unit or ""
                    temporality = getattr(m.data, "aggregation_temporality", None)
                    delta = temporality is AggregationTemporality.DELTA
                    if delta:
                        self._delta.add(m.name)
                    for p in m.data.data_points:
                        key = (m.name, frozenset((p.attributes or {}).items()))
                        self._points[key] = _merge(self._points.get(key), p, delta=delta)

    def reset(self) -> None:
        """Forget everything recorded so far (called at the start of each test)."""
        self.span_exporter.clear()
        self.log_exporter.clear()
        self.metric_reader.get_metrics_data()  # discard pending deltas
        self._points.clear()
        self._units.clear()
        self._delta.clear()

    def spans(self) -> list[ReadableSpan]:
        return list(self.span_exporter.get_finished_spans())

    def span_names(self) -> list[str]:
        return [s.name for s in self.spans()]

    def metric_points(self, name: str) -> list[tuple[dict[str, Any], Any]]:
        """``(attributes, value)`` per data point; histograms give the point itself.

        Counters and histograms count from the start of the current test.
        """
        self._collect()
        out: list[tuple[dict[str, Any], Any]] = []
        for (metric, _), p in self._points.items():
            if metric == name:
                out.append((dict(p.attributes or {}), getattr(p, "value", p)))
        return out

    def metric_names(self) -> set[str]:
        self._collect()
        return {metric for metric, _ in self._points}

    def metric_unit(self, name: str) -> str | None:
        self._collect()
        return self._units.get(name)

    def log_records(self) -> list[Any]:
        return list(self.log_exporter.get_finished_logs())


@pytest.fixture
def otel_memory(monkeypatch: pytest.MonkeyPatch) -> Iterator[OtelMemory]:
    """The process-wide in-memory providers, cleared for this test (C61, C63).

    ``pycommon.otel.setup`` (and so ``create_app(instrument=True)``) uses these providers while
    the fixture is active. Globals are never reset: instruments created by earlier tests or at
    import time keep reporting to the same provider, so suites can share one process.
    """
    so = session_otel()
    _otel.clear_gauge_callbacks()
    mem = OtelMemory(
        so.span_exporter,
        so.metric_reader,
        so.log_exporter,
        so.tracer_provider,
        so.meter_provider,
        so.logger_provider,
    )
    mem.reset()
    original = _otel._build_providers

    def build(
        res: Resource, *, span_exporter: Any = None, metric_reader: Any = None
    ) -> tuple[TracerProvider, MeterProvider, LoggerProvider, bool]:
        if span_exporter is not None or metric_reader is not None:
            return original(res, span_exporter=span_exporter, metric_reader=metric_reader)
        return so.tracer_provider, so.meter_provider, so.logger_provider, False

    monkeypatch.setattr(_otel, "_build_providers", build)
    try:
        yield mem
    finally:
        _otel.clear_gauge_callbacks()


# --- logs --------------------------------------------------------------------------------------


class JsonLogCapture(logging.Handler):
    """Captures records as the JSON lines ``JsonFormatter`` writes to stdout."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []
        self.setFormatter(JsonFormatter(service="test", env="dev", version="test"))

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def records(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.lines]

    def events(self, name: str) -> list[dict[str, Any]]:
        return [r for r in self.records() if r.get("event") == name]

    def clear(self) -> None:
        self.lines.clear()


@pytest.fixture
def caplog_json() -> Iterator[JsonLogCapture]:
    root = logging.getLogger()
    handler = JsonLogCapture()
    level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(level)


# --- tokens ------------------------------------------------------------------------------------


def _ed25519_pair() -> tuple[str, str]:
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private, public


@dataclass
class TokenFactory:
    """Throwaway Ed25519 keys for both issuers, their signers and the matching keyset."""

    keyset_json: str
    keyset: Keyset
    private_pems: dict[str, str]
    kids: dict[str, str]
    signers: dict[Issuer, TokenSigner]

    @classmethod
    def create(cls, *, clock: Clock | None = None) -> TokenFactory:
        issuers: tuple[Issuer, ...] = ("web", "payout-worker")
        kids = {"web": "test-web-1", "payout-worker": "test-worker-1"}
        pems: dict[str, str] = {}
        doc: dict[str, dict[str, str]] = {}
        for iss in issuers:
            private, public = _ed25519_pair()
            pems[iss] = private
            doc[kids[iss]] = {"iss": iss, "pem": public}
        raw = json.dumps(doc)
        signers = {
            iss: TokenSigner(pems[iss], kid=kids[iss], issuer=iss, clock=clock or SystemClock())
            for iss in issuers
        }
        return cls(raw, Keyset.from_json(raw), pems, kids, signers)

    def mint(
        self,
        *,
        issuer: Issuer = "web",
        aud: str,
        sub: str,
        cid: UUID,
        roles: Iterable[str] = (),
        name: str | None = None,
        sid: str | None = None,
        ttl_s: int = 60,
    ) -> str:
        return self.signers[issuer].mint(
            aud=aud, sub=sub, cid=cid, roles=roles, name=name, sid=sid, ttl_s=ttl_s
        )


@pytest.fixture
def token_factory() -> TokenFactory:
    return TokenFactory.create()


# --- containers --------------------------------------------------------------------------------


def find_repo_root(start: Path | None = None) -> Path:
    """The repo root: ``$PAYRUN_REPO_ROOT``, else the nearest ancestor with ``deploy/compose``."""
    env = os.environ.get("PAYRUN_REPO_ROOT")
    if env:
        return Path(env)
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "deploy" / "compose").is_dir():
            return candidate
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "deploy" / "compose").is_dir():
            return candidate
    raise FileNotFoundError("deploy/compose not found; set PAYRUN_REPO_ROOT")


def _password() -> str:
    return secrets.token_urlsafe(18)


@contextlib.contextmanager
def _patched_env(values: Mapping[str, str]) -> Iterator[None]:
    saved = {k: os.environ.get(k) for k in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _schema_of(role: str) -> str:
    for suffix in ("_migrator", "_app"):
        if role.endswith(suffix):
            return role.removesuffix(suffix)
    return "public"


@dataclass
class PgBootstrapped:
    host: str
    port: int
    admin_user: str
    admin_password: str
    passwords: dict[str, str]
    container: Any = field(repr=False)

    def env_for(self, role: str) -> dict[str, str]:
        """The C1 keys ``DB_HOST`` … ``DB_SCHEMA`` for ``role``."""
        if role in DB_ROLE_PASSWORD_ENV:
            password = self.passwords[DB_ROLE_PASSWORD_ENV[role]]
        elif role == self.admin_user:
            password = self.admin_password
        else:
            raise KeyError(role)
        return {
            "DB_HOST": self.host,
            "DB_PORT": str(self.port),
            "DB_NAME": "payrun",
            "DB_USER": role,
            "DB_PASSWORD": password,
            "DB_SCHEMA": _schema_of(role),
        }

    def url_for(self, role: str) -> str:
        from urllib.parse import quote  # noqa: PLC0415

        e = self.env_for(role)
        return (
            f"postgresql+asyncpg://{quote(e['DB_USER'], safe='')}:"
            f"{quote(e['DB_PASSWORD'], safe='')}"
            f"@{e['DB_HOST']}:{e['DB_PORT']}/{e['DB_NAME']}"
        )


def _wait_for_pg(host: str, port: int, user: str, password: str, timeout_s: float = 60.0) -> None:
    import asyncpg  # type: ignore[import-untyped]  # noqa: PLC0415

    async def attempt() -> None:
        conn = await asyncpg.connect(
            host=host, port=port, user=user, password=password, database="payrun", timeout=2
        )
        await conn.close()

    deadline = time.monotonic() + timeout_s
    while True:
        try:
            asyncio.run(attempt())
            return
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)


def start_pg_bootstrapped(repo_root: Path | None = None) -> PgBootstrapped:
    from testcontainers.community.postgres import PostgresContainer  # noqa: PLC0415

    init_dir = (repo_root or find_repo_root()) / "deploy" / "compose" / "init"
    if not init_dir.is_dir():
        raise FileNotFoundError(str(init_dir))
    admin_password = _password()
    passwords = {env: _password() for env in DB_ROLE_PASSWORD_ENV.values()}
    container = PostgresContainer(
        POSTGRES_IMAGE, username="postgres", password=admin_password, dbname="payrun", driver=None
    ).with_volume_mapping(str(init_dir), "/docker-entrypoint-initdb.d", "ro")
    for k, v in passwords.items():
        container.with_env(k, v)
    container.start()
    host = container.get_container_host_ip()
    port = int(container.get_exposed_port(5432))
    try:
        _wait_for_pg(host, port, "identity_app", passwords["IDENTITY_DB_PASSWORD"])
    except Exception:
        container.stop()
        raise
    return PgBootstrapped(host, port, "postgres", admin_password, passwords, container)


@pytest.fixture(scope="session")
def pg_bootstrapped() -> Iterator[PgBootstrapped]:
    pg = start_pg_bootstrapped()
    try:
        yield pg
    finally:
        pg.container.stop()


@pytest.fixture(scope="session")
def db_env_for(pg_bootstrapped: PgBootstrapped) -> Callable[[str], dict[str, str]]:
    return pg_bootstrapped.env_for


@pytest.fixture(scope="session")
def alembic_upgrade(pg_bootstrapped: PgBootstrapped) -> Callable[..., None]:
    """``alembic_upgrade("payroll")``: run the service's migrations to head as its migrator."""

    def upgrade(service: str, revision: str = "head") -> None:
        from alembic import command  # noqa: PLC0415
        from alembic.config import Config  # noqa: PLC0415

        schema = SERVICE_SCHEMAS[service]
        service_dir = find_repo_root() / "services" / service
        env = pg_bootstrapped.env_for(f"{schema}_migrator")
        cwd = Path.cwd()
        with _patched_env(env):
            os.chdir(service_dir)
            try:
                command.upgrade(Config(str(service_dir / "alembic.ini")), revision)
            finally:
                os.chdir(cwd)

    return upgrade


@dataclass
class RedisAcl:
    host: str
    port: int
    passwords: dict[str, str]
    container: Any = field(repr=False)

    def env_for(self, user: str) -> dict[str, str]:
        """The C1 keys ``REDIS_HOST``, ``REDIS_PORT``, ``REDIS_USERNAME``, ``REDIS_PASSWORD``."""
        return {
            "REDIS_HOST": self.host,
            "REDIS_PORT": str(self.port),
            "REDIS_USERNAME": user,
            "REDIS_PASSWORD": self.passwords[REDIS_USER_PASSWORD_ENV[user]],
        }


def _wait_for_redis(host: str, port: int, password: str, timeout_s: float = 30.0) -> None:
    import redis  # noqa: PLC0415

    deadline = time.monotonic() + timeout_s
    while True:
        try:
            client = redis.Redis(host=host, port=port, password=password, socket_timeout=1)
            client.ping()
            client.close()
            return
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.3)


def start_redis_acl(repo_root: Path | None = None) -> RedisAcl:
    from testcontainers.core.container import DockerContainer  # noqa: PLC0415

    redis_dir = (repo_root or find_repo_root()) / "deploy" / "compose" / "redis"
    if not redis_dir.is_dir():
        raise FileNotFoundError(str(redis_dir))
    passwords = {env: _password() for env in REDIS_USER_PASSWORD_ENV.values()}
    container = (
        DockerContainer(REDIS_IMAGE)
        .with_volume_mapping(str(redis_dir), "/usr/local/etc/redis", "ro")
        .with_tmpfs_mount("/run/redis")
        .with_exposed_ports(6379)
        .with_kwargs(entrypoint=["/bin/sh", "/usr/local/etc/redis/entrypoint.sh"])
    )
    for k, v in passwords.items():
        container.with_env(k, v)
    container.start()
    host = container.get_container_host_ip()
    port = int(container.get_exposed_port(6379))
    try:
        _wait_for_redis(host, port, passwords["REDIS_ADMIN_PASSWORD"])
    except Exception:
        container.stop()
        raise
    return RedisAcl(host, port, passwords, container)


@pytest.fixture(scope="session")
def redis_acl() -> Iterator[RedisAcl]:
    r = start_redis_acl()
    try:
        yield r
    finally:
        r.container.stop()


@pytest.fixture(scope="session")
def redis_env_for(redis_acl: RedisAcl) -> Callable[[str], dict[str, str]]:
    return redis_acl.env_for
