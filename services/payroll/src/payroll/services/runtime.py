"""What the services need at run time, built once in the app's startup hook."""

from __future__ import annotations

from dataclasses import dataclass

from pycommon import Clock, SystemClock
from pycommon.streams import RelayLoop
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from payroll.crypto import FieldKeyring
from payroll.events.outbox import OutboxWriter
from payroll.metrics import Metrics
from payroll.settings import Settings

__all__ = ["Runtime"]


@dataclass
class Runtime:
    settings: Settings
    sessions: async_sessionmaker[AsyncSession]
    keyring: FieldKeyring
    metrics: Metrics
    outbox: OutboxWriter
    relay: RelayLoop | None = None
    clock: Clock = SystemClock()  # noqa: RUF009  (stateless default)

    def wake_relay(self) -> None:
        if self.relay is not None:
            self.relay.wake()
