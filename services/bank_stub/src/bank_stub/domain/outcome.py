"""The deterministic outcome function and bank reference (design §3.4; PAY-412).

The same key always gives the same outcome, whatever the replica, restart or body. Integer
arithmetic only.
"""

from __future__ import annotations

import hashlib
from typing import Final, Literal

__all__ = ["BUCKETS", "Outcome", "bank_reference_for", "bucket_of", "decide_outcome"]

BUCKETS: Final = 10_000

Status = Literal["settled", "rejected"]
Reason = Literal["account_closed", "insufficient_funds_at_bank"]
Outcome = tuple[Status, Reason | None]


def bucket_of(key: str) -> int:
    """``sha256(key)`` as a big-endian integer, mod 10 000."""
    return int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest(), "big") % BUCKETS


def decide_outcome(key: str, account: str, failure_rate: float) -> Outcome:
    if account.endswith("0000"):
        return ("rejected", "account_closed")
    if bucket_of(key) < round(failure_rate * BUCKETS):
        return ("rejected", "insufficient_funds_at_bank")
    return ("settled", None)


def bank_reference_for(key: str) -> str:
    """``BSR-`` plus 16 uppercase hex characters (20 in all; C26 allows 64)."""
    return "BSR-" + hashlib.sha256(b"ref:" + key.encode("utf-8")).hexdigest()[:16].upper()
