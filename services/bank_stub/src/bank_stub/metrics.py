"""``bank_stub_transfers_total{outcome}`` (design §5.2; PAY-412, R6).

Created as ``bank_stub_transfers``: the Collector appends ``_total`` (platform §3.3).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from opentelemetry.metrics import Counter
from pycommon.otel import meter

__all__ = ["Metrics", "TransferOutcome"]

TransferOutcome = Literal["settled", "rejected", "replayed"]


@dataclass(frozen=True)
class Metrics:
    transfers: Counter

    @classmethod
    def create(cls) -> Metrics:
        return cls(
            transfers=meter("bank_stub").create_counter(
                "bank_stub_transfers",
                unit="1",
                description="POST /v1/transfers answered 200/201, by outcome",
            )
        )

    def transfer(self, outcome: TransferOutcome) -> None:
        self.transfers.add(1, {"outcome": outcome})
