"""The reset request's timing floor on error paths (identity LLD §6.3, §6.6; PAY-105, C74)."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from identity.runtime import IdentityRuntime
from identity.usecases.resets import REQUEST_FLOOR_S, RequestCommand, request_reset
from pycommon.errors import RateLimited


class DatabaseDownError(Exception):
    """Stands in for a driver error or a lock timeout."""


class _FailingSession:
    async def __aenter__(self) -> Any:
        raise DatabaseDownError("connection refused")

    async def __aexit__(self, *exc: object) -> None:
        return None


def _runtime(*, retry_after: int | None = None) -> IdentityRuntime:
    async def check_reset(ip_key: str, email_key: str) -> int | None:
        return retry_after

    stub = SimpleNamespace(
        ip_hash=lambda ip: None if ip is None else "h",
        email_key=lambda email: "e",
        limits=SimpleNamespace(check_reset=check_reset),
        sessions=_FailingSession,
    )
    return cast(IdentityRuntime, stub)


async def test_a_database_error_still_waits_out_the_floor() -> None:
    """C74: the pad runs in finally, so a failing lookup (database down, lock timeout) answers no
    sooner than REQUEST_FLOOR_S, like a match or a miss; the error still propagates."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(DatabaseDownError):
        await request_reset(_runtime(), RequestCommand(uuid4(), "who@example.com", "198.51.100.7"))
    # asyncio may wake a timer up to one clock tick early
    assert loop.time() - started >= REQUEST_FLOOR_S - time.get_clock_info("monotonic").resolution


async def test_rate_limited_is_not_padded() -> None:
    """C74: the limits run before the timer, so a 429 answers at once, the same for every email."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(RateLimited):
        await request_reset(_runtime(retry_after=30), RequestCommand(uuid4(), "who@example.com"))
    assert loop.time() - started < REQUEST_FLOOR_S
