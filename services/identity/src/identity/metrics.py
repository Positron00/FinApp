"""Identity instruments (identity LLD §5.2; PAY-104, PAY-110, PAY-603, PAY-NFR-10).

Created at app start-up (not import time), so they bind to the meter provider that is current
then. The two email gauges are computed **at collection time** (R26, C63): the OTel callback runs
one query through ``EmailGaugeSource``, which owns a private event loop on its own thread, so the
callback works from the exporter's thread and from a test thread that is running an event loop.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Final

from opentelemetry.metrics import Counter, Histogram
from pycommon.otel import SECONDS_BUCKETS, Observation, histogram, meter, observable_gauge
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from identity.settings import IdentitySettings

__all__ = [
    "EMAILS_OLDEST_PENDING_AGE",
    "EMAILS_PENDING",
    "EmailGaugeSource",
    "EmailStats",
    "IdentityMetrics",
    "create_metrics",
    "register_email_gauges",
]

EMAILS_PENDING: Final = "identity_emails_pending"
EMAILS_OLDEST_PENDING_AGE: Final = "identity_email_oldest_pending_age_seconds"
# One query serves both gauges of a collection cycle.
STATS_REUSE_S: Final = 1.0
CLOSE_TIMEOUT_S: Final = 5.0

_STATS_SQL: Final = text(
    "SELECT count(*), COALESCE(EXTRACT(EPOCH FROM now() - min(created_at)), 0) "
    "FROM identity.email_outbox WHERE status = 'pending'"
)


@dataclass(frozen=True)
class IdentityMetrics:
    signins: Counter
    emails_failed: Counter
    hash_seconds: Histogram


def create_metrics() -> IdentityMetrics:
    m = meter("identity")
    signins = m.create_counter(
        "auth_signins", unit="1", description="Login responses by outcome (C19)"
    )
    emails_failed = m.create_counter(
        "identity_emails_failed", unit="1", description="Emails that reached status failed (R25)"
    )
    emails_failed.add(0)
    hash_seconds = histogram(
        "identity_password_hash_seconds",
        unit="s",
        description="argon2 hash and verify duration",
        buckets=SECONDS_BUCKETS,
    )
    return IdentityMetrics(signins, emails_failed, hash_seconds)


@dataclass(frozen=True)
class EmailStats:
    pending: int
    oldest_pending_age_s: float


class EmailGaugeSource:
    """Runs the outbox query on a private loop thread with a one-connection engine.

    Started lazily by the first collection, so an app that is never scraped starts no thread.
    """

    def __init__(
        self,
        settings: IdentitySettings,
        *,
        engine_factory: Callable[[], AsyncEngine] | None = None,
    ) -> None:
        self._settings = settings
        self._timeout_s = settings.identity_gauge_query_timeout_ms / 1000
        self._engine_factory = engine_factory or self._default_engine
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._engine: AsyncEngine | None = None
        self._cached: tuple[float, EmailStats] | None = None
        self.closed = False

    def _default_engine(self) -> AsyncEngine:
        s = self._settings
        return create_async_engine(
            s.db_url(),
            pool_size=1,
            max_overflow=0,
            pool_pre_ping=True,
            connect_args={
                "timeout": self._timeout_s,
                "server_settings": {"statement_timeout": str(s.identity_gauge_query_timeout_ms)},
            },
        )

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self.closed:
                raise RuntimeError("the gauge source is closed")
            if self._loop is None:
                loop = asyncio.new_event_loop()
                thread = threading.Thread(
                    target=loop.run_forever, name="identity-email-gauges", daemon=True
                )
                thread.start()
                self._loop, self._thread = loop, thread
            return self._loop

    def _run[T](self, coro: Coroutine[Any, Any, T], timeout_s: float) -> T:
        future = asyncio.run_coroutine_threadsafe(coro, self._ensure_loop())
        try:
            return future.result(timeout=timeout_s)
        except TimeoutError:
            future.cancel()
            raise

    async def _query(self) -> EmailStats:
        if self._engine is None:
            self._engine = self._engine_factory()
        async with self._engine.connect() as conn:
            row = (await conn.execute(_STATS_SQL)).one()
        return EmailStats(pending=int(row[0]), oldest_pending_age_s=max(0.0, float(row[1])))

    def stats(self) -> EmailStats:
        """The current pending count and oldest pending age (raises on failure or timeout)."""
        now = time.monotonic()
        cached = self._cached
        if cached is not None and now - cached[0] < STATS_REUSE_S:
            return cached[1]
        # The engine connect timeout plus the statement timeout bound the wait.
        result = self._run(self._query(), timeout_s=2 * self._timeout_s)
        self._cached = (time.monotonic(), result)
        return result

    async def _dispose(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None

    def close(self) -> None:
        """Dispose the engine and stop the loop thread (idempotent)."""
        with self._lock:
            if self.closed:
                return
            self.closed = True
            loop, thread = self._loop, self._thread
        if loop is None or thread is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._dispose(), loop).result(CLOSE_TIMEOUT_S)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(CLOSE_TIMEOUT_S)
            loop.close()


def _observe(
    source: EmailGaugeSource, pick: Callable[[EmailStats], float]
) -> Callable[[], Observation]:
    def callback() -> Observation:
        if source.closed:
            return {}
        return {frozenset(): pick(source.stats())}

    return callback


def register_email_gauges(source: EmailGaugeSource) -> None:
    """Point the two gauges at ``source``. pycommon keeps one callback per name and meter
    provider, so a later app instance in the same process replaces the earlier one."""
    observable_gauge(
        EMAILS_PENDING,
        unit="{email}",
        description="Emails waiting in the outbox (status pending)",
        callback=_observe(source, lambda st: float(st.pending)),
    )
    observable_gauge(
        EMAILS_OLDEST_PENDING_AGE,
        unit="s",
        description="Age of the oldest pending email; 0 when none (EmailOutboxStuck)",
        callback=_observe(source, lambda st: st.oldest_pending_age_s),
    )
