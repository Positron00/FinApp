"""Email retry schedule (identity LLD §6.9; PAY-110). Pure: no I/O."""

from __future__ import annotations

import random
from typing import Final

__all__ = ["JITTER", "is_exhausted", "retry_delay_s"]

JITTER: Final = 0.1
_rng = random.SystemRandom()


def retry_delay_s(attempts: int, base_s: int, max_s: int, *, jitter: float = JITTER) -> int:
    """``min(base_s * 2^(attempts-1), max_s)`` with ±``jitter``; at least 1 s."""
    n = max(1, attempts)
    delay = min(base_s * 2 ** min(n - 1, 32), max_s)
    if jitter:
        delay = round(delay * _rng.uniform(1 - jitter, 1 + jitter))
    return max(1, int(delay))


def is_exhausted(attempts: int, max_attempts: int) -> bool:
    return attempts >= max_attempts
