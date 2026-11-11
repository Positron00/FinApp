"""Event envelope, stream producer and outbox relay loop (platform LLD §3.9; PAY-402, R7, C22).

pycommon defines no tables and no consumer: the owning service implements
:class:`OutboxRepository` over its own outbox table, and the payout-worker owns its consumer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Protocol
from uuid import UUID

from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, field_serializer, field_validator
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pycommon import Clock, SystemClock
from pycommon.db import new_id
from pycommon.logging import get_logger, log_event
from pycommon.otel import inject_traceparent, new_traceparent, tracer

__all__ = [
    "Envelope",
    "OutboxRepository",
    "RelayLoop",
    "StreamItem",
    "StreamProducer",
]

TRACEPARENT_RE: Final = re.compile(r"^00-(?!0{32})[0-9a-f]{32}-(?!0{16})[0-9a-f]{16}-[0-9a-f]{2}$")
BACKOFF_BASE_S: Final = 0.2

_log = get_logger("pycommon.streams")
_rng = random.SystemRandom()


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID
    type: str
    version: int
    occurred_at: datetime
    producer: str
    traceparent: str
    data: dict[str, Any]

    @field_validator("occurred_at")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware")
        return v.astimezone(UTC)

    @field_validator("traceparent")
    @classmethod
    def _traceparent(cls, v: str) -> str:
        if not TRACEPARENT_RE.match(v):
            raise ValueError("traceparent must be a W3C traceparent")
        return v

    @field_serializer("occurred_at")
    def _ser_occurred_at(self, v: datetime) -> str:
        v = v.astimezone(UTC)
        return v.strftime("%Y-%m-%dT%H:%M:%S.") + f"{v.microsecond // 1000:03d}Z"

    @classmethod
    def new(
        cls,
        *,
        type: str,
        version: int,
        producer: str,
        data: Mapping[str, Any],
        clock: Clock | None = None,
    ) -> Envelope:
        return cls(
            event_id=new_id(),
            type=type,
            version=version,
            occurred_at=(clock or SystemClock()).now(),
            producer=producer,
            traceparent=inject_traceparent() or new_traceparent(),
            data=dict(data),
        )

    def to_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), separators=(",", ":"))


@dataclass(frozen=True)
class StreamItem:
    event_id: UUID
    type: str
    envelope_json: str
    ref: Any = field(default=None, compare=False)


class StreamProducer:
    """``XADD <stream> MAXLEN ~ <maxlen> * event_id <id> type <type> envelope <json>`` (R7)."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    @staticmethod
    def _fields(event_id: UUID | str, type_: str, envelope_json: str) -> dict[str, str]:
        return {"event_id": str(event_id), "type": type_, "envelope": envelope_json}

    async def publish(
        self,
        stream: str,
        *,
        event_id: UUID | str,
        type: str,
        envelope_json: str,
        maxlen: int,
    ) -> bytes:
        with tracer().start_as_current_span(
            f"{stream} publish",
            kind=trace.SpanKind.PRODUCER,
            attributes={
                "messaging.system": "redis",
                "messaging.destination.name": stream,
                "messaging.message.id": str(event_id),
            },
        ):
            entry_id = await self._redis.xadd(
                stream,
                self._fields(event_id, type, envelope_json),  # type: ignore[arg-type]
                maxlen=maxlen,
                approximate=True,
            )
        return entry_id if isinstance(entry_id, bytes) else str(entry_id).encode()

    async def publish_many(
        self, stream: str, items: Sequence[StreamItem], *, maxlen: int
    ) -> list[bytes | Exception]:
        """One non-transactional pipeline (``raise_on_error=False``): per-item entry id or error."""
        if not items:
            return []
        with tracer().start_as_current_span(
            f"{stream} publish",
            kind=trace.SpanKind.PRODUCER,
            attributes={
                "messaging.system": "redis",
                "messaging.destination.name": stream,
                "messaging.batch.message_count": len(items),
            },
        ):
            async with self._redis.pipeline(transaction=False) as pipe:
                for item in items:
                    pipe.xadd(
                        stream,
                        self._fields(item.event_id, item.type, item.envelope_json),  # type: ignore[arg-type]
                        maxlen=maxlen,
                        approximate=True,
                    )
                results = await pipe.execute(raise_on_error=False)
        out: list[bytes | Exception] = []
        for r in results:
            if isinstance(r, Exception):
                out.append(r)
            else:
                out.append(r if isinstance(r, bytes) else str(r).encode())
        return out


class OutboxRepository(Protocol):
    """Implemented by the owning service over its own outbox table (C22)."""

    async def claim_unsent(self, session: AsyncSession, *, limit: int) -> Sequence[StreamItem]: ...

    async def mark_sent(self, session: AsyncSession, items: Sequence[StreamItem]) -> None: ...

    async def mark_failed(
        self, session: AsyncSession, items: Sequence[StreamItem], *, error_type: str
    ) -> None: ...


@dataclass(frozen=True)
class _BatchResult:
    claimed: int
    sent: int
    failed: int
    error_type: str | None


class RelayLoop:
    """Claim unsent rows, publish them in one pipeline, mark them sent or failed (PAY-402)."""

    def __init__(
        self,
        sf: async_sessionmaker[AsyncSession],
        producer: StreamProducer,
        repo: OutboxRepository,
        *,
        stream: str,
        maxlen: int,
        batch_size: int = 100,
        poll_interval_s: float = 0.2,
        max_backoff_s: float = 5.0,
        clock: Clock | None = None,
    ) -> None:
        self._sf = sf
        self._producer = producer
        self._repo = repo
        self._stream = stream
        self._maxlen = maxlen
        self._batch_size = batch_size
        self._poll = poll_interval_s
        self._max_backoff = max_backoff_s
        self._clock = clock or SystemClock()
        self._wake = asyncio.Event()
        self._k = 0

    def wake(self) -> None:
        """Skip the current poll wait (call from the owner's after-commit hook)."""
        self._wake.set()

    async def relay_once(self) -> int:
        """Relay one batch; returns the number of rows sent."""
        return (await self._relay_batch()).sent

    async def _relay_batch(self) -> _BatchResult:
        async with self._sf() as session, session.begin():
            items = list(await self._repo.claim_unsent(session, limit=self._batch_size))
            if not items:
                return _BatchResult(0, 0, 0, None)
            try:
                results: list[bytes | Exception] = await self._producer.publish_many(
                    self._stream, items, maxlen=self._maxlen
                )
            except Exception as exc:
                results = [exc] * len(items)
            sent = [it for it, r in zip(items, results, strict=True) if isinstance(r, bytes)]
            failed_by_type: dict[str, list[StreamItem]] = {}
            for it, r in zip(items, results, strict=True):
                if isinstance(r, Exception):
                    failed_by_type.setdefault(type(r).__name__, []).append(it)
            if sent:
                await self._repo.mark_sent(session, sent)
            for error_type, group in failed_by_type.items():
                await self._repo.mark_failed(session, group, error_type=error_type)
        failed = len(items) - len(sent)
        first_error = next(iter(failed_by_type), None)
        return _BatchResult(len(items), len(sent), failed, first_error)

    def _next_backoff(self) -> float:
        delay = _rng.uniform(0, min(BACKOFF_BASE_S * 2**self._k, self._max_backoff))
        self._k += 1
        return delay

    async def _wait(self, seconds: float, stop: asyncio.Event, *, wakeable: bool) -> None:
        if wakeable and self._wake.is_set():
            self._wake.clear()
            return
        waiters = [
            asyncio.ensure_future(stop.wait()),
            asyncio.ensure_future(self._clock.sleep(seconds)),
        ]
        if wakeable:
            waiters.append(asyncio.ensure_future(self._wake.wait()))
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waiters:
                w.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)
        if wakeable:
            self._wake.clear()

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                result = await self._relay_batch()
                error_type = result.error_type
                batch, failed = result.claimed, result.failed
            except Exception as exc:
                error_type, batch, failed = type(exc).__name__, 0, 0
            if error_type is not None:
                delay = self._next_backoff()
                log_event(
                    _log,
                    logging.ERROR,
                    "outbox.relay.error",
                    error_type=error_type,
                    batch_size=batch,
                    failed=failed,
                    backoff_ms=int(delay * 1000),
                )
                await self._wait(delay, stop, wakeable=False)
                continue
            self._k = 0
            if batch >= self._batch_size:
                continue  # a full batch loops at once
            await self._wait(self._poll, stop, wakeable=True)
