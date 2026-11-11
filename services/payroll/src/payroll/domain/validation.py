"""Input rules shared by the API models (PAY-200, PAY-201)."""

from __future__ import annotations

import re
import zoneinfo
from datetime import date
from typing import Final

__all__ = ["aba_checksum_ok", "is_monday", "is_valid_timezone"]

_ROUTING_RE: Final = re.compile(r"^[0-9]{9}$")
_ABA_WEIGHTS: Final = (3, 7, 1, 3, 7, 1, 3, 7, 1)
MONDAY: Final = 0


def aba_checksum_ok(routing: str) -> bool:
    """The ABA routing checksum: Σ weight * digit ≡ 0 (mod 10) over the 9 digits."""
    if not _ROUTING_RE.match(routing):
        return False
    return sum(w * int(d) for w, d in zip(_ABA_WEIGHTS, routing, strict=True)) % 10 == 0


def is_monday(d: date) -> bool:
    return d.weekday() == MONDAY


def is_valid_timezone(tz: str) -> bool:
    try:
        zoneinfo.ZoneInfo(tz)
    except zoneinfo.ZoneInfoNotFoundError, ValueError, OSError:
        return False
    return True
