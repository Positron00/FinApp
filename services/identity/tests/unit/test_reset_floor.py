"""The reset request's timing floor on error paths (identity LLD §6.3, §6.6; PAY-105, C74)."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
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
    """PAY-105, C74: the pad runs in finally, so a failing lookup (database down, lock timeout)
    answers no sooner than REQUEST_FLOOR_S, like a match or a miss; the error still propagates.
    Wall clock, end to end: fails if the pad moves out of finally."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(DatabaseDownError):
        await request_reset(_runtime(), RequestCommand(uuid4(), "who@example.com", "198.51.100.7"))
    # asyncio may wake a timer up to one clock tick early
    assert loop.time() - started >= REQUEST_FLOOR_S - time.get_clock_info("monotonic").resolution


async def test_a_database_error_sleeps_the_rest_of_the_floor_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PAY-105, C74: on the error path the pad is one sleep of what is left of the floor (nearly
    all of it: the failing lookup returns at once). Without the finally, the error skips it."""
    sleep = AsyncMock()
    monkeypatch.setattr("identity.usecases.resets.asyncio.sleep", sleep)
    with pytest.raises(DatabaseDownError):
        await request_reset(_runtime(), RequestCommand(uuid4(), "who@example.com", "198.51.100.7"))
    sleep.assert_awaited_once()
    assert sleep.await_args is not None
    [pad] = sleep.await_args.args
    assert 0 < pad <= REQUEST_FLOOR_S
    assert pad == pytest.approx(REQUEST_FLOOR_S, abs=0.02)


async def test_rate_limited_is_not_padded(monkeypatch: pytest.MonkeyPatch) -> None:
    """PAY-105, C74: the limits run before the timer, so a 429 answers at once, the same for every
    email: the pad's sleep is never awaited."""
    sleep = AsyncMock()
    monkeypatch.setattr("identity.usecases.resets.asyncio.sleep", sleep)
    with pytest.raises(RateLimited):
        await request_reset(_runtime(retry_after=30), RequestCommand(uuid4(), "who@example.com"))
    sleep.assert_not_awaited()
