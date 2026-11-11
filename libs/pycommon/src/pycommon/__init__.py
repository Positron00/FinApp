"""PayRun shared library.

Public names live in their own modules (``pycommon.settings``, ``pycommon.auth``, …) and are not
re-exported here, which keeps import cost and import cycles low (platform LLD §3). The one
exception is the injectable clock, which every time-dependent module shares.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

__all__ = ["Clock", "SystemClock"]


@runtime_checkable
class Clock(Protocol):
    """Source of wall time, monotonic time and sleeping; ``pycommon.testing.FakeClock`` fakes it."""

    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """The real clock: UTC wall time, ``time.monotonic`` and ``asyncio.sleep``."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
