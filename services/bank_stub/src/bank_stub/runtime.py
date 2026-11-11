"""What the routes need, built in the app's ``on_startup`` and kept on ``app.state.bank_stub``."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from fastapi import Request

from bank_stub.db.repo import TransferRepoPort
from bank_stub.metrics import Metrics
from bank_stub.settings import Settings

__all__ = ["Runtime", "get_runtime"]


@dataclass
class Runtime:
    settings: Settings
    repo: TransferRepoPort
    metrics: Metrics
    sleep: Callable[[float], Awaitable[None]] = field(default=asyncio.sleep)


def get_runtime(request: Request) -> Runtime:
    rt = getattr(request.app.state, "bank_stub", None)
    if not isinstance(rt, Runtime):
        raise RuntimeError("the bank-stub app has not started")
    return rt
