"""Business instruments and the collection-time gauges (design §7.2; PAY-NFR-10, PAY-NFR-11,
PAY-406, PAY-407, R26, C18, C63).

Counters are created without ``_total`` (the Collector appends it) and with annotation units, so
the Prometheus names are exactly ``payroll_pay_runs_total``, ``payroll_payouts_total``,
``payroll_payouts_republished_total`` and ``payroll_late_settlements_total``. The last one is
pre-initialised at 0 so the ``PayoutLateSettlement`` alert's ``increase()`` sees its first
increment (C21).

The five gauges (``payroll_runs_processing_oldest_age_seconds``, ``payroll_payouts_stuck``,
``payroll_attempts_needs_operator``, ``outbox_unsent``, ``outbox_oldest_age_seconds``) are
computed **at collection time** (R26): each OTel callback reads ``PayrollGaugeSource.stats()``,
which runs one query on a private event-loop thread with a one-connection engine. So the callback
works from the exporter's reader thread and from a test thread that is running an event loop, and
it never blocks the app's loop. A failed query raises, pycommon logs
``metrics.gauge_query_failed`` and the cycle has no observation (never a stale or zero value).
Units are ``s`` or curly-brace annotations, never ``"1"`` (C63).
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Final

from opentelemetry.metrics import Counter, Histogram
from pycommon.otel import SETTLEMENT_BUCKETS, Observation, meter, observable_gauge
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from payroll.settings import Settings

__all__ = [
    "GAUGES",
    "GaugeStats",
    "Metrics",
    "PayrollGaugeSource",
    "register_collection_gauges",
]

METER_NAME = "payroll"
# One query serves the five gauges of a collection cycle.
STATS_REUSE_S: Final = 1.0
CLOSE_TIMEOUT_S: Final = 5.0

RUNS_PROCESSING_OLDEST_AGE: Final = "payroll_runs_processing_oldest_age_seconds"
PAYOUTS_STUCK: Final = "payroll_payouts_stuck"
ATTEMPTS_NEEDS_OPERATOR: Final = "payroll_attempts_needs_operator"
OUTBOX_UNSENT: Final = "outbox_unsent"
OUTBOX_OLDEST_AGE: Final = "outbox_oldest_age_seconds"

_STATS_SQL: Final = text(
    """
    SELECT
      (SELECT COALESCE(EXTRACT(EPOCH FROM now() - min(processing_since)), 0)
         FROM payroll.pay_runs WHERE status = 'processing' AND deleted_at IS NULL),
      (SELECT count(*) FROM payroll.payout_attempts
        WHERE status IN ('requested', 'indeterminate', 'needs_operator')
          AND requested_at < now() - make_interval(secs => :stuck_after_s)),
      (SELECT count(*) FROM payroll.payout_attempts WHERE status = 'needs_operator'),
      (SELECT count(*) FROM payroll.outbox WHERE sent_at IS NULL),
      (SELECT COALESCE(EXTRACT(EPOCH FROM now() - min(created_at)), 0)
         FROM payroll.outbox WHERE sent_at IS NULL)
    """
)


@dataclass(frozen=True)
class Metrics:
    pay_runs: Counter
    payouts: Counter
    settlement: Histogram
    republished: Counter
    late_settlements: Counter

    @classmethod
    def create(cls) -> Metrics:
        m = meter(METER_NAME)
        late_settlements = m.create_counter(
            "payroll_late_settlements",
            unit="{attempt}",
            description=(
                "Settled answers for an attempt already rejected or no longer current: a "
                "possible double payment (PAY-NFR-02, PayoutLateSettlement)"
            ),
        )
        late_settlements.add(0)
        return cls(
            late_settlements=late_settlements,
            pay_runs=m.create_counter(
                "payroll_pay_runs", unit="{run}", description="Pay-run transitions by outcome"
            ),
            payouts=m.create_counter(
                "payroll_payouts", unit="{payout}", description="Payout outcomes"
            ),
            settlement=m.create_histogram(
                "payroll_payout_settlement_seconds",
                unit="s",
                description="Approval to the run's first final status",
                explicit_bucket_boundaries_advisory=list(SETTLEMENT_BUCKETS),
            ),
            republished=m.create_counter(
                "payroll_payouts_republished",
                unit="{event}",
                description="Re-publish outbox rows written for live attempts (PAY-407)",
            ),
        )

    def run_outcome(self, outcome: str, n: int = 1) -> None:
        self.pay_runs.add(n, {"outcome": outcome})

    def late_settlement(self) -> None:
        self.late_settlements.add(1)

    def payout_outcome(self, outcome: str, n: int = 1) -> None:
        if n:
            self.payouts.add(n, {"outcome": outcome})


@dataclass(frozen=True)
class GaugeStats:
    runs_processing_oldest_age_s: float
    payouts_stuck: int
    attempts_needs_operator: int
    outbox_unsent: int
    outbox_oldest_age_s: float


class PayrollGaugeSource:
    """Runs the gauge query on a private loop thread with a one-connection engine whose
    ``statement_timeout`` is ``PAYROLL_GAUGE_QUERY_TIMEOUT_MS``. Started lazily by the first
    collection, so an app that is never collected starts no thread and opens no connection."""

    def __init__(
        self,
        settings: Settings,
        *,
        engine_factory: Callable[[], AsyncEngine] | None = None,
    ) -> None:
        self._settings = settings
        self._timeout_s = settings.payroll_gauge_query_timeout_ms / 1000
        self._engine_factory = engine_factory or self._default_engine
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._engine: AsyncEngine | None = None
        self._cached: tuple[float, GaugeStats] | None = None
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
                "server_settings": {"statement_timeout": str(s.payroll_gauge_query_timeout_ms)},
            },
        )

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self.closed:
                raise RuntimeError("the gauge source is closed")
            if self._loop is None:
                loop = asyncio.new_event_loop()
                thread = threading.Thread(
                    target=loop.run_forever, name="payroll-gauges", daemon=True
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

    async def _query(self) -> GaugeStats:
        if self._engine is None:
            self._engine = self._engine_factory()
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    _STATS_SQL, {"stuck_after_s": self._settings.payout_stuck_after_s}
                )
            ).one()
        return GaugeStats(
            runs_processing_oldest_age_s=max(0.0, float(row[0])),
            payouts_stuck=int(row[1]),
            attempts_needs_operator=int(row[2]),
            outbox_unsent=int(row[3]),
            outbox_oldest_age_s=max(0.0, float(row[4])),
        )

    def stats(self) -> GaugeStats:
        """Fresh values (raises on failure or timeout); reused for ``STATS_REUSE_S`` so the five
        callbacks of one collection share one query."""
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


# (name, unit, description, value) for each collection-time gauge (design §7.2).
GAUGES: Final[tuple[tuple[str, str, str, Callable[[GaugeStats], float]], ...]] = (
    (
        RUNS_PROCESSING_OLDEST_AGE,
        "s",
        "Age of the oldest run in processing (from processing_since); 0 when none",
        lambda st: st.runs_processing_oldest_age_s,
    ),
    (
        PAYOUTS_STUCK,
        "{attempt}",
        "Non-final attempts requested more than PAYOUT_STUCK_AFTER_S ago",
        lambda st: float(st.payouts_stuck),
    ),
    (
        ATTEMPTS_NEEDS_OPERATOR,
        "{attempt}",
        "Attempts waiting for an operator (needs_operator)",
        lambda st: float(st.attempts_needs_operator),
    ),
    (
        OUTBOX_UNSENT,
        "{event}",
        "Outbox rows not yet published",
        lambda st: float(st.outbox_unsent),
    ),
    (
        OUTBOX_OLDEST_AGE,
        "s",
        "Age of the oldest unsent outbox row; 0 when none (OutboxStuck)",
        lambda st: st.outbox_oldest_age_s,
    ),
)


def _observe(
    source: PayrollGaugeSource, pick: Callable[[GaugeStats], float]
) -> Callable[[], Observation]:
    def callback() -> Observation:
        if source.closed:
            return {}
        return {frozenset(): pick(source.stats())}

    return callback


def register_collection_gauges(source: PayrollGaugeSource) -> None:
    """Point the five gauges at ``source``. pycommon keeps one callback per name and meter
    provider, so a later app instance in the same process replaces the earlier one."""
    for name, unit, description, pick in GAUGES:
        observable_gauge(name, unit=unit, description=description, callback=_observe(source, pick))
