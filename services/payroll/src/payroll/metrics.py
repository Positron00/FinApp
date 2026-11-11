"""Business instruments (design §7.2; PAY-NFR-10, PAY-406).

Counters are created without ``_total`` (the Collector appends it) and with annotation units, so
the Prometheus names are exactly ``payroll_pay_runs_total`` and ``payroll_payouts_total``. The
collection-time gauges (``payroll_*`` gauges and ``outbox_*``) arrive with F4a.
"""

from __future__ import annotations

from dataclasses import dataclass

from opentelemetry.metrics import Counter, Histogram
from pycommon.otel import SETTLEMENT_BUCKETS, meter

__all__ = ["Metrics"]

METER_NAME = "payroll"


@dataclass(frozen=True)
class Metrics:
    pay_runs: Counter
    payouts: Counter
    settlement: Histogram

    @classmethod
    def create(cls) -> Metrics:
        m = meter(METER_NAME)
        return cls(
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
        )

    def run_outcome(self, outcome: str, n: int = 1) -> None:
        self.pay_runs.add(n, {"outcome": outcome})

    def payout_outcome(self, outcome: str, n: int = 1) -> None:
        if n:
            self.payouts.add(n, {"outcome": outcome})
