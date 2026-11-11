"""The worker's own consumer (design §3.2, §3.3; PAY-403, PAY-408, C21, C27).

* ``XGROUP CREATE <stream> <group> 0 MKSTREAM`` (BUSYGROUP ignored).
* Pending entries first (``XREADGROUP … <last>`` from ``0``) at start and after NOGROUP, then new
  ones (``>``, BLOCK); after any other Redis error the reader resumes with ``>`` (C63).
* ``XAUTOCLAIM`` every ``PAYOUT_CLAIM_INTERVAL_S`` for entries idle ≥ ``PAYOUT_CLAIM_MIN_IDLE_MS``.
* Delivery numbers: 1 for ``>``; ``XPENDING`` times-delivered for pending and claimed entries.
* At most ``PAYOUT_CONCURRENCY`` messages held: slots are reserved before each XREADGROUP and
  XAUTOCLAIM, which ask for no more entries than were reserved (C63).
* After each message: ``Ack`` → XACK; ``DeadLetter`` → DLQ XADD then XACK; ``Retry`` →
  ``decide_after_failure``: leave pending, or dead-letter at the delivery budget.

Only commands granted to the ``payout-worker`` ACL user are used (platform §2.2; no MULTI).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Literal

from opentelemetry.trace import Status, StatusCode
from pycommon import Clock, SystemClock
from pycommon.logging import get_logger, log_event
from pycommon.otel import tracer
from redis.asyncio import Redis
from redis.exceptions import RedisError, ResponseError

from payout_worker.handler import Ack, DeadLetter, ErrorKind, HandleResult, PayoutHandler
from payout_worker.metrics import Metrics
from payout_worker.models import StreamMessage
from payout_worker.settings import DLQ_MAXLEN, Settings

__all__ = [
    "BACKOFF_INITIAL_S",
    "BACKOFF_MAX_S",
    "Consumer",
    "Slots",
    "build_dlq_fields",
    "decide_after_failure",
]

_log: Final = get_logger("payout_worker.consumer")
_rng = random.SystemRandom()
BACKOFF_INITIAL_S: Final = 0.5
BACKOFF_MAX_S: Final = 30.0

Entry = tuple[bytes | str, Mapping[bytes | str, bytes | str] | None]


def decide_after_failure(
    kind: ErrorKind, delivery: int, max_deliveries: int
) -> Literal["retry", "dlq"]:
    """Design §3.2: poison at once; payroll and internal at the budget; bank never (the handler
    turns a bank no-answer at the budget into ``indeterminate``)."""
    if kind == "invalid":
        return "dlq"
    if kind == "bank":
        return "retry"
    return "dlq" if delivery >= max_deliveries else "retry"


def _rfc3339(now: datetime) -> str:
    now = now.astimezone(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def build_dlq_fields(
    msg: StreamMessage,
    *,
    kind: str,
    code: str,
    error: str,
    delivery: int,
    consumer: str,
    now: datetime,
) -> dict[str, bytes | str]:
    """Exactly the eight C21 fields, all strings; ``envelope`` byte for byte (``""`` if absent)."""
    envelope = msg.field("envelope")
    return {
        "envelope": envelope if envelope is not None else "",
        "source_id": msg.id,
        "error_kind": kind,
        "error_code": code,
        "error": error[:500],
        "delivery_count": str(delivery),
        "consumer": consumer,
        "failed_at": _rfc3339(now),
    }


def _text(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


class Slots:
    """Handler slots shared by the reader and the claim loop (``PAYOUT_CONCURRENCY``).

    Slots are **reserved before** an XREADGROUP or XAUTOCLAIM, and the read asks for at most the
    reserved number, so the process never holds more messages than slots (C63): an entry never
    waits for a slot outside the handler deadline while its idle time runs towards
    ``PAYOUT_CLAIM_MIN_IDLE_MS``. Each spawned handler owns one reserved slot and releases it.
    Reservations are served first come, first served, so the claim loop isn't starved by the
    reader re-reserving every free slot before each blocking read.
    """

    def __init__(self, n: int) -> None:
        self._n = n
        self._used = 0
        self._cond = asyncio.Condition()
        self._next_ticket = 0
        self._serving = 0
        self._abandoned: set[int] = set()

    @property
    def free(self) -> int:
        return self._n - self._used

    @property
    def used(self) -> int:
        return self._used

    def _skip_abandoned(self) -> None:
        while self._serving in self._abandoned:
            self._abandoned.discard(self._serving)
            self._serving += 1

    async def reserve(self, max_n: int | None = None) -> int:
        """Wait (in turn) for at least one free slot, then reserve up to ``max_n`` (default: all
        free slots). Returns the number reserved, always ≥ 1."""
        ticket = self._next_ticket
        self._next_ticket += 1
        async with self._cond:
            try:
                await self._cond.wait_for(lambda: self._serving == ticket and self.free > 0)
            except asyncio.CancelledError:
                self._abandoned.add(ticket)
                self._skip_abandoned()
                self._cond.notify_all()
                raise
            n = self.free if max_n is None else max(1, min(self.free, max_n))
            self._used += n
            self._serving += 1
            self._skip_abandoned()
            self._cond.notify_all()
            return n

    def try_reserve(self) -> int:
        """Reserve every free slot without waiting; 0 when none is free or someone is queued."""
        if self._serving != self._next_ticket:
            return 0
        n = self.free
        self._used += n
        return n

    async def release(self, n: int = 1) -> None:
        if n <= 0:
            return
        async with self._cond:
            self._used -= n
            self._cond.notify_all()


@dataclass
class _State:
    in_flight: set[str] = field(default_factory=set)
    tasks: set[asyncio.Task[None]] = field(default_factory=set)
    stopping: bool = False
    reader: asyncio.Task[None] | None = None


class Consumer:
    def __init__(
        self,
        settings: Settings,
        handler: PayoutHandler,
        redis: Redis,
        reader: Redis,
        metrics: Metrics,
        *,
        clock: Clock | None = None,
        on_message_done: Callable[[str, HandleResult], Awaitable[None]] | None = None,
    ) -> None:
        self._s = settings
        self._handler = handler
        self._redis = redis
        self._reader = reader
        self._metrics = metrics
        self._clock = clock or SystemClock()
        self._slots = Slots(settings.payout_concurrency)
        self._state = _State()
        self._on_done = on_message_done
        self.name = settings.payout_consumer_name

    # --- health ---------------------------------------------------------------------------------

    def is_alive(self) -> bool:
        reader = self._state.reader
        return reader is not None and not reader.done()

    @property
    def stopping(self) -> bool:
        return self._state.stopping

    @property
    def in_flight(self) -> int:
        return len(self._state.in_flight)

    # --- group ----------------------------------------------------------------------------------

    async def ensure_group(self) -> None:
        """``XGROUP CREATE … 0 MKSTREAM``: ``0`` so earlier events are delivered; BUSYGROUP ok."""
        try:
            await self._redis.xgroup_create(
                self._s.payout_stream, self._s.payout_group, id="0", mkstream=True
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    # --- run and stop ---------------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        """Read and claim until ``stop``; then drain in-flight handlers (§3.3.7)."""
        self._state.stopping = False
        self._state.reader = asyncio.create_task(self._read_loop(), name="payout-reader")
        claimer = asyncio.create_task(self._claim_loop(), name="payout-claimer")
        try:
            await stop.wait()
        finally:
            await self.stop(claimer)

    async def stop(self, claimer: asyncio.Task[None] | None = None) -> None:
        st = self._state
        st.stopping = True
        started = self._clock.monotonic()
        log_event(_log, logging.INFO, "worker.shutdown.started", in_flight=len(st.in_flight))
        for task in (st.reader, claimer):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *(t for t in (st.reader, claimer) if t is not None), return_exceptions=True
        )
        pending = set(st.tasks)
        drained = cancelled = 0
        if pending:
            done, still = await asyncio.wait(pending, timeout=self._s.payout_shutdown_drain_s)
            drained = len(done)
            for t in still:
                t.cancel()
            cancelled = len(still)
            await asyncio.gather(*still, return_exceptions=True)
        log_event(
            _log,
            logging.INFO,
            "worker.shutdown.completed",
            drained=drained,
            cancelled=cancelled,
            duration_s=round(self._clock.monotonic() - started, 3),
        )

    async def _backoff(self, attempt: int, exc: BaseException) -> None:
        delay = _rng.uniform(0, min(BACKOFF_MAX_S, BACKOFF_INITIAL_S * 2**attempt))
        code = type(exc).__name__
        log_event(
            _log, logging.WARNING, "queue.read.error", error_code=code, backoff_s=round(delay, 3)
        )
        await self._clock.sleep(delay)

    async def _recover_nogroup(self, exc: BaseException) -> bool:
        """NOGROUP (group or stream lost): recreate from ``0`` with MKSTREAM (§3.3.6)."""
        if not (isinstance(exc, ResponseError) and "NOGROUP" in str(exc)):
            return False
        with contextlib.suppress(RedisError):
            await self.ensure_group()
            log_event(
                _log,
                logging.WARNING,
                "queue.group.recreated",
                stream=self._s.payout_stream,
                group=self._s.payout_group,
            )
        return True

    # --- reading --------------------------------------------------------------------------------

    async def _read_loop(self) -> None:
        """Pending-first at start and after NOGROUP only; after any other Redis error the
        reader resumes with ``>`` (C63), so retrying entries keep their claim-idle spacing."""
        errors = 0
        group_ready = False
        pending_first = True
        while True:
            try:
                if not group_ready:
                    await self.ensure_group()
                    group_ready = True
                if pending_first:
                    await self._read_pending()
                    pending_first = False
                while True:
                    await self._read_new()
                    errors = 0
            except asyncio.CancelledError:
                raise
            except RedisError as exc:
                if await self._recover_nogroup(exc):
                    pending_first = True  # restart at pending-first (§3.3.6)
                    continue
                await self._backoff(errors, exc)
                errors += 1

    async def _xreadgroup(self, last: str, count: int, block_ms: int | None) -> list[Entry]:
        resp: Any = await self._reader.xreadgroup(
            self._s.payout_group,
            self.name,
            {self._s.payout_stream: last},
            count=count,
            block=block_ms,
        )
        if not resp:
            return []
        if isinstance(resp, dict):  # RESP3
            entries: list[Any] = next(iter(resp.values()), [])
            return list(entries[0] if entries and isinstance(entries[0], list) else entries)
        return list(resp[0][1])

    async def _read_pending(self) -> None:
        """Pending first: this consumer's PEL from ``0`` until nothing is returned (§3.3.2)."""
        last = "0"
        while True:
            reserved = await self._slots.reserve()
            spawned = 0
            try:
                entries = await self._xreadgroup(last, reserved, None)
                if not entries:
                    return
                for entry_id, fields in entries:
                    eid = _text(entry_id)
                    last = eid
                    if not fields:
                        await self._trimmed(eid, ack=True)
                        continue
                    if eid in self._state.in_flight:
                        continue
                    delivery = await self._delivery_count(eid)
                    if delivery is not None:
                        self._spawn(StreamMessage(eid, fields), delivery)
                        spawned += 1
            finally:
                await self._slots.release(reserved - spawned)

    async def _read_new(self) -> None:
        reserved = await self._slots.reserve()
        spawned = 0
        try:
            entries = await self._xreadgroup(">", reserved, self._s.payout_xread_block_ms)
            for entry_id, fields in entries:
                eid = _text(entry_id)
                if not fields:  # pragma: no cover  (">" never returns a nil body)
                    await self._trimmed(eid, ack=True)
                    continue
                self._spawn(StreamMessage(eid, fields), 1)
                spawned += 1
        finally:
            await self._slots.release(reserved - spawned)

    async def _trimmed(self, entry_id: str, *, ack: bool) -> None:
        log_event(_log, logging.WARNING, "queue.message.trimmed", stream_id=entry_id)
        if ack:
            with contextlib.suppress(RedisError):
                await self._redis.xack(self._s.payout_stream, self._s.payout_group, entry_id)

    async def _delivery_count(self, entry_id: str) -> int | None:
        """``XPENDING <stream> <group> <id> <id> 1`` → times delivered; None if already ACKed."""
        rows = await self._redis.xpending_range(
            self._s.payout_stream, self._s.payout_group, min=entry_id, max=entry_id, count=1
        )
        if not rows:
            return None
        return int(rows[0]["times_delivered"])

    # --- claiming -------------------------------------------------------------------------------

    async def _claim_loop(self) -> None:
        errors = 0
        while True:
            await self._clock.sleep(self._s.payout_claim_interval_s)
            try:
                await self.claim_once()
                errors = 0
            except asyncio.CancelledError:
                raise
            except RedisError as exc:
                if await self._recover_nogroup(exc):
                    continue
                await self._backoff(errors, exc)
                errors += 1

    async def claim_once(self) -> int:
        """One XAUTOCLAIM sweep from ``0-0``: waits its turn for a slot, then follows the cursor
        while slots are free; returns the messages handed over.

        Each XAUTOCLAIM asks for at most the slots reserved just before it (C63)."""
        cursor = "0-0"
        handed = 0
        reserved = await self._slots.reserve()
        while True:
            spawned = 0
            try:
                resp: Any = await self._redis.xautoclaim(
                    self._s.payout_stream,
                    self._s.payout_group,
                    self.name,
                    min_idle_time=self._s.payout_claim_min_idle_ms,
                    start_id=cursor,
                    count=reserved,
                )
                cursor = _text(resp[0])
                claimed: list[Entry] = list(resp[1])
                deleted = list(resp[2]) if len(resp) > 2 else []
                for d in deleted:
                    await self._trimmed(_text(d), ack=False)
                for entry_id, fields in claimed:
                    eid = _text(entry_id)
                    if not fields:  # pragma: no cover  (Redis 7 reports these as deleted)
                        await self._trimmed(eid, ack=False)
                        continue
                    if eid in self._state.in_flight:
                        continue
                    delivery = await self._delivery_count(eid)
                    if delivery is None:
                        continue
                    self._spawn(StreamMessage(eid, fields), delivery)
                    spawned += 1
            finally:
                await self._slots.release(reserved - spawned)
            handed += spawned
            if cursor in {"0-0", "0"}:
                break
            reserved = self._slots.try_reserve()  # next page only with slots free right now
            if reserved == 0:
                break
        return handed

    # --- one message ----------------------------------------------------------------------------

    def _spawn(self, msg: StreamMessage, delivery: int) -> None:
        """Start a handler on a slot the caller has already reserved (C63)."""
        self._state.in_flight.add(msg.id)
        task = asyncio.create_task(self._run_one(msg, delivery), name=f"payout-{msg.id}")
        self._state.tasks.add(task)
        task.add_done_callback(self._state.tasks.discard)

    async def _run_one(self, msg: StreamMessage, delivery: int) -> None:
        started = self._clock.monotonic()
        result: HandleResult | None = None
        try:
            result = await self._handler.handle(msg, delivery)
            await self.execute(msg, delivery, result)
        except asyncio.CancelledError:
            raise  # shutdown: the message stays pending and is reclaimed elsewhere
        except Exception as exc:  # pragma: no cover  (handle() and execute() don't raise)
            log_event(
                _log,
                logging.ERROR,
                "queue.message.failed",
                error_kind="internal",
                error_code="unexpected",
                error_type=type(exc).__name__,
                delivery=delivery,
                dlq=False,
                source_id=msg.id,
            )
        finally:
            self._metrics.processed_in(self._clock.monotonic() - started)
            self._state.in_flight.discard(msg.id)
            await self._slots.release()
            if self._on_done is not None and result is not None:
                await self._on_done(msg.id, result)

    async def execute(self, msg: StreamMessage, delivery: int, result: HandleResult) -> None:
        """Design §3.3.4: the consumer's action for a handler result."""
        started = self._clock.monotonic()
        if isinstance(result, Ack):
            await self._ack(msg)
            self._metrics.message("ok")
            log_event(
                _log,
                logging.INFO,
                "queue.message.processed",
                outcome=result.outcome,
                delivery=delivery,
                duration_ms=round((self._clock.monotonic() - started) * 1000, 1),
                stream_id=msg.id,
            )
            return
        if isinstance(result, DeadLetter):
            await self.dead_letter(msg, delivery, result.kind, result.code, result.error)
            return
        if decide_after_failure(result.kind, delivery, self._s.payout_max_deliveries) == "dlq":
            kind: Literal["invalid", "payroll", "internal"] = (
                "payroll" if result.kind == "payroll" else "internal"
            )
            await self.dead_letter(msg, delivery, kind, result.code, result.error)
            return
        self._metrics.message("retry")
        log_event(
            _log,
            logging.WARNING,
            "queue.message.failed",
            error_kind=result.kind,
            error_code=result.code,
            delivery=delivery,
            dlq=False,
            source_id=msg.id,
        )

    async def _ack(self, msg: StreamMessage) -> bool:
        try:
            await self._redis.xack(self._s.payout_stream, self._s.payout_group, msg.id)
        except RedisError as exc:
            # The entry stays pending; its redelivery stops at processed_events and ACKs.
            log_event(
                _log,
                logging.WARNING,
                "queue.read.error",
                error_code=f"xack_{type(exc).__name__}",
                backoff_s=0,
                stream_id=msg.id,
            )
            return False
        return True

    async def dead_letter(
        self, msg: StreamMessage, delivery: int, kind: str, code: str, error: str
    ) -> bool:
        """XADD to the DLQ (``MAXLEN ~ 10000``), then XACK (C21). No XACK if the XADD fails."""
        fields = build_dlq_fields(
            msg,
            kind=kind,
            code=code,
            error=error,
            delivery=delivery,
            consumer=self.name,
            now=self._clock.now(),
        )
        with tracer("payout_worker").start_as_current_span(
            "payout.dead_letter", attributes={"payrun.error_code": code}
        ) as span:
            try:
                await self._redis.xadd(
                    self._s.payout_dlq_stream,
                    fields,  # type: ignore[arg-type]
                    maxlen=DLQ_MAXLEN,
                    approximate=True,
                )
            except RedisError as exc:
                span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                log_event(
                    _log,
                    logging.ERROR,
                    "queue.message.failed",
                    error_kind=kind,
                    error_code=code,
                    delivery=delivery,
                    dlq=False,
                    source_id=msg.id,
                    dlq_error=type(exc).__name__,
                )
                return False
        self._metrics.message("dlq")
        await self._ack(msg)
        log_event(
            _log,
            logging.ERROR,
            "queue.message.failed",
            error_kind=kind,
            error_code=code,
            delivery=delivery,
            dlq=True,
            source_id=msg.id,
        )
        return True

    # --- gauges ---------------------------------------------------------------------------------

    async def poll_gauges_once(self) -> None:
        """XINFO GROUPS lag (nil → keep the last value) and the DLQ length (no key → 0)."""
        try:
            groups: Any = await self._redis.xinfo_groups(self._s.payout_stream)
        except ResponseError:
            groups = []
        for g in groups:
            if _text(g.get("name", b"")) == self._s.payout_group and g.get("lag") is not None:
                self._metrics.lag = float(g["lag"])
        try:
            info: Any = await self._redis.xinfo_stream(self._s.payout_dlq_stream)
            self._metrics.dlq_length = float(info.get("length", 0))
        except ResponseError:
            self._metrics.dlq_length = 0.0

    async def poll_gauges(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            with contextlib.suppress(RedisError):
                await self.poll_gauges_once()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._s.payout_lag_poll_s)
