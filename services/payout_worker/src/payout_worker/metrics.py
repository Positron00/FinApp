"""The worker's own instruments (design §5.2; PAY-403, PAY-404, PAY-408, PAY-NFR-10/11, C21).

Counters are created without ``_total`` (the Collector appends it). ``preinit`` adds 0 to every
``queue_messages_total`` series before the first read, so the first DLQ after a restart is a
visible 0 → 1 increase (C21). The gauges read values the poller caches.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Literal

from opentelemetry.metrics import Counter, Histogram
from pycommon.otel import Observation, histogram, meter, observable_gauge

__all__ = ["PROCESSING_BUCKETS", "QUEUE_OUTCOMES", "Metrics", "QueueOutcome"]

QueueOutcome = Literal["ok", "retry", "dlq"]
QUEUE_OUTCOMES: Final[tuple[QueueOutcome, ...]] = ("ok", "retry", "dlq")
PROCESSING_BUCKETS: Final = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 15, 30, 60)
BANK_REASONS: Final = frozenset({"account_closed", "insufficient_funds_at_bank"})


@dataclass
class Metrics:
    stream: str
    group: str
    dlq_stream: str
    messages: Counter
    processing: Histogram
    bank_transfers: Counter
    lag: float | None = None
    dlq_length: float = 0.0
    _gauges: list[object] = field(default_factory=list, repr=False)

    @classmethod
    def create(cls, *, stream: str, group: str, dlq_stream: str) -> Metrics:
        m = meter("payout_worker")
        out = cls(
            stream=stream,
            group=group,
            dlq_stream=dlq_stream,
            messages=m.create_counter(
                "queue_messages", unit="1", description="Handled stream messages by outcome"
            ),
            processing=histogram(
                "queue_processing_seconds",
                unit="s",
                description="Time to handle one stream message",
                buckets=PROCESSING_BUCKETS,
            ),
            bank_transfers=m.create_counter(
                "payouts_bank_transfers", unit="1", description="Classified bank calls"
            ),
        )
        out._gauges = [
            observable_gauge(
                "queue_consumer_lag",
                unit="{entry}",  # an annotation unit: Prometheus would add _ratio to "1" (C63)
                description="Entries not yet delivered to the group (XINFO GROUPS lag)",
                callback=out._lag_observation,
            ),
            observable_gauge(
                "queue_dlq_messages",
                unit="{entry}",
                description="Length of the dead-letter stream",
                callback=out._dlq_observation,
            ),
        ]
        return out

    @property
    def _queue_attrs(self) -> dict[str, str]:
        return {"stream": self.stream, "group": self.group}

    def _lag_observation(self) -> Observation:
        if self.lag is None:
            return {}
        return {frozenset(self._queue_attrs.items()): self.lag}

    def _dlq_observation(self) -> Observation:
        return {frozenset({("stream", self.dlq_stream)}): self.dlq_length}

    def preinit(self) -> None:
        for outcome in QUEUE_OUTCOMES:
            self.messages.add(0, {**self._queue_attrs, "outcome": outcome})

    def message(self, outcome: QueueOutcome) -> None:
        self.messages.add(1, {**self._queue_attrs, "outcome": outcome})

    def processed_in(self, seconds: float) -> None:
        self.processing.record(max(0.0, seconds), self._queue_attrs)

    def bank_transfer(
        self, outcome: Literal["settled", "rejected", "no_answer", "not_found"], reason: str
    ) -> None:
        if outcome == "rejected" and reason not in BANK_REASONS:
            reason = "other"
        self.bank_transfers.add(1, {"outcome": outcome, "reason": reason})
