"""Password policy and the argon2id hasher (identity LLD §6.1-6.3; PAY-103, PAY-104)."""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass
from typing import Final

import argon2
from argon2 import Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from pycommon.otel import tracer

from identity.metrics import IdentityMetrics

__all__ = [
    "MIN_PASSWORD_CHARS",
    "PasswordHasher",
    "PasswordTooWeakError",
    "VerifyResult",
    "check_password_policy",
]

MIN_PASSWORD_CHARS: Final = 12


class PasswordTooWeakError(ValueError):
    """The password is shorter than 12 code points (422 ``password_too_weak``)."""


def check_password_policy(pw: str) -> None:
    """At least 12 Unicode code points; no composition rules, no trimming (§6.1)."""
    if len(pw) < MIN_PASSWORD_CHARS:
        raise PasswordTooWeakError("password too short")


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    needs_rehash: bool


class PasswordHasher:
    """argon2id in a worker thread behind a semaphore, so hashing never blocks the event loop."""

    def __init__(
        self,
        *,
        time_cost: int,
        memory_kib: int,
        parallelism: int,
        max_concurrency: int,
        metrics: IdentityMetrics | None = None,
    ) -> None:
        self._ph = argon2.PasswordHasher(
            time_cost=time_cost,
            memory_cost=memory_kib,
            parallelism=parallelism,
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )
        self._sem = asyncio.Semaphore(max_concurrency)
        self._metrics = metrics
        self._dummy_hash: str | None = None

    async def start(self) -> None:
        """Compute the dummy hash with the live parameters (§6.3)."""
        await self._dummy()

    async def _dummy(self) -> str:
        if self._dummy_hash is None:
            self._dummy_hash = await self.hash(secrets.token_urlsafe(32))
        return self._dummy_hash

    def _record(self, op: str, started: float) -> None:
        if self._metrics is not None:
            self._metrics.hash_seconds.record(time.perf_counter() - started, {"op": op})

    async def hash(self, pw: str) -> str:
        with tracer("identity").start_as_current_span("identity.password.hash"):
            async with self._sem:
                started = time.perf_counter()
                encoded = await asyncio.to_thread(self._ph.hash, pw)
                self._record("hash", started)
        return encoded

    async def verify(self, encoded: str | None, pw: str) -> VerifyResult:
        """One argon2 verify; ``encoded=None`` verifies against the dummy hash and fails."""
        dummy = encoded is None
        target = await self._dummy() if encoded is None else encoded
        with tracer("identity").start_as_current_span(
            "identity.password.verify", attributes={"identity.dummy": dummy}
        ):
            async with self._sem:
                started = time.perf_counter()
                ok = await asyncio.to_thread(self._verify_sync, target, pw)
                self._record("verify", started)
        if dummy or not ok:
            return VerifyResult(ok=False, needs_rehash=False)
        return VerifyResult(ok=True, needs_rehash=self._ph.check_needs_rehash(target))

    def _verify_sync(self, encoded: str, pw: str) -> bool:
        try:
            return self._ph.verify(encoded, pw)
        except VerifyMismatchError, VerificationError, InvalidHashError:
            return False
