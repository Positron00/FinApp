"""Account lock duration (identity LLD §6.4; PAY-104). Pure: no I/O."""

from __future__ import annotations

__all__ = ["lock_seconds"]


def lock_seconds(level: int, base_s: int, max_s: int) -> int:
    """``min(base_s * 2^(level-1), max_s)``: 60, 120, 240 … 3600 with the production values."""
    if level < 1:
        raise ValueError("level starts at 1")
    # Cap the exponent so a very high level can't build a huge integer.
    return int(min(base_s * 2 ** min(level - 1, 32), max_s))
