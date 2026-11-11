"""Consumer actions, DLQ writes and loops over a fake Redis (design §3.2, §3.3; PAY-403, PAY-408,
PAY-NFR-07, C21)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from payout_worker.consumer import Consumer, Slots, build_dlq_fields
from payout_worker.handler import Ack, DeadLetter, HandleResult, PayoutHandler, Retry
from payout_worker.metrics import Metrics
from payout_worker.models import StreamMessage
from pycommon.testing import FAKE_EPOCH, FakeClock, JsonLogCapture, OtelMemory
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError

from tests.helpers import (
    ACCOUNT,
    AMOUNT,
    API_KEY,
    ROUTING,
    FakeBank,
    FakePayroll,
    FakeRepo,
    make_metrics,
    make_settings,
    message,
)

FIELDS = {
    "envelope",
    "source_id",
    "error_kind",
    "error_code",
    "error",
    "delivery_count",
    "consumer",
    "failed_at",
}


@dataclass
class FakeRedis:
    ops: list[tuple[str, Any]] = field(default_factory=list)
    fail: dict[str, Exception] = field(default_factory=dict)
    pending: dict[str, int] = field(default_factory=dict)
    history: list[list[tuple[bytes, dict[bytes, bytes]]]] = field(default_factory=list)
    new: list[list[tuple[bytes, dict[bytes, bytes]]]] = field(default_factory=list)
    claims: list[list[Any]] = field(default_factory=list)
    groups: list[dict[bytes | str, Any]] = field(default_factory=list)
    dlq_info: dict[str, Any] | None = None

    def _maybe_fail(self, op: str) -> None:
        if op in self.fail:
            raise self.fail[op]

    async def xack(self, stream: str, group: str, entry_id: str) -> int:
        self.ops.append(("xack", entry_id))
        self._maybe_fail("xack")
        return 1

    async def xadd(self, stream: str, fields: Any, **kw: Any) -> bytes:
        self.ops.append(("xadd", (stream, dict(fields), kw)))
        self._maybe_fail("xadd")
        return b"9-0"

    async def xgroup_create(self, stream: str, group: str, **kw: Any) -> bool:
        self.ops.append(("xgroup_create", (stream, group, kw)))
        self._maybe_fail("xgroup_create")
        return True

    async def xpending_range(self, stream: str, group: str, **kw: Any) -> list[dict[str, Any]]:
        eid = kw["min"]
        if eid not in self.pending:
            return []
        return [{"message_id": eid.encode(), "times_delivered": self.pending[eid]}]

    async def xreadgroup(self, group: str, name: str, streams: dict[str, str], **kw: Any) -> Any:
        self._maybe_fail("xreadgroup")
        last = next(iter(streams.values()))
        self.ops.append(("xreadgroup", (last, kw.get("count"))))
        source = self.new if last == ">" else self.history
        if not source:
            if last == ">":
                await asyncio.sleep(0.01)
            return []
        return [[b"stream:payout.requested", source.pop(0)]]

    async def xautoclaim(self, *a: Any, **kw: Any) -> Any:
        self._maybe_fail("xautoclaim")
        self.ops.append(("xautoclaim", kw.get("start_id")))
        return self.claims.pop(0) if self.claims else [b"0-0", [], []]

    async def xinfo_groups(self, stream: str) -> Any:
        self._maybe_fail("xinfo_groups")
        return self.groups

    async def xinfo_stream(self, stream: str) -> Any:
        if self.dlq_info is None:
            raise ResponseError("no such key")
        return self.dlq_info


@dataclass
class Rig:
    consumer: Consumer
    redis: FakeRedis
    metrics: Metrics
    done: list[tuple[str, HandleResult]]


def make_rig(handler: Any = None, *, redis: FakeRedis | None = None, **settings: Any) -> Rig:
    s = make_settings(**settings)
    redis = redis or FakeRedis()
    metrics = make_metrics(s)
    done: list[tuple[str, HandleResult]] = []

    async def on_done(eid: str, result: HandleResult) -> None:
        done.append((eid, result))

    h = handler or PayoutHandler(s, FakeRepo(), FakePayroll(), FakeBank(), metrics)
    c = Consumer(
        s,
        h,
        cast(Redis, redis),
        cast(Redis, redis),
        metrics,
        clock=FakeClock(),
        on_message_done=on_done,
    )
    return Rig(c, redis, metrics, done)


def test_dlq_fields_exactly_c21() -> None:
    """C21: exactly the eight fields, all strings; envelope byte for byte."""
    msg = message()
    out = build_dlq_fields(
        msg,
        kind="payroll",
        code="payroll_unavailable",
        error="PayrollUnavailable: payroll_unavailable",
        delivery=5,
        consumer="w-1",
        now=FAKE_EPOCH,
    )
    assert set(out) == FIELDS
    assert out["envelope"] == msg.field("envelope")
    assert out["delivery_count"] == "5"
    assert out["failed_at"] == "2026-11-10T12:00:00.000Z"
    assert all(isinstance(v, str | bytes) for v in out.values())
    no_env = build_dlq_fields(
        message(drop_envelope=True),
        kind="invalid",
        code="schema_invalid",
        error="x" * 900,
        delivery=1,
        consumer="w-1",
        now=FAKE_EPOCH,
    )
    assert no_env["envelope"] == ""
    assert len(no_env["error"]) == 500


async def test_dead_letter_xadd_then_xack(
    otel_memory: OtelMemory, caplog_json: JsonLogCapture
) -> None:
    """C21, PAY-408: XADD (MAXLEN ~ 10000) to the DLQ, then XACK; dlq counter +1."""
    rig = make_rig()
    rig.metrics.preinit()
    ok = await rig.consumer.dead_letter(message(), 1, "invalid", "schema_invalid", "E: x")
    assert ok
    assert [op for op, _ in rig.redis.ops] == ["xadd", "xack"]
    stream, fields, kw = rig.redis.ops[0][1]
    assert stream == "stream:payout.requested.dlq"
    assert kw == {"maxlen": 10_000, "approximate": True}
    assert set(fields) == FIELDS
    points = {a["outcome"]: v for a, v in otel_memory.metric_points("queue_messages")}
    assert points == {"ok": 0, "retry": 0, "dlq": 1}
    [event] = caplog_json.events("queue.message.failed")
    assert (event["dlq"], event["level"]) == (True, "error")


async def test_failed_xadd_leaves_pending() -> None:
    """C21: if the DLQ XADD fails there is no XACK; XAUTOCLAIM retries the entry."""
    rig = make_rig()
    rig.redis.fail["xadd"] = RedisConnectionError("down")
    assert not await rig.consumer.dead_letter(message(), 5, "payroll", "x", "E: x")
    assert [op for op, _ in rig.redis.ops] == ["xadd"]


async def test_dlq_never_holds_bank_details() -> None:
    """PAY-NFR-07: the DLQ entry carries IDs and the event amount only, no bank details."""
    rig = make_rig()
    await rig.consumer.dead_letter(message(), 5, "payroll", "payroll_unavailable", "E: x")
    text = json.dumps(
        {k: (v.decode() if isinstance(v, bytes) else v) for k, v in rig.redis.ops[0][1][1].items()}
    )
    for secret in (ROUTING, ACCOUNT, API_KEY):
        assert secret not in text
    assert str(AMOUNT) not in rig.redis.ops[0][1][1]["error"]


@pytest.mark.parametrize(
    ("result", "delivery", "ops", "dlq_kind"),
    [
        (Ack("settled"), 1, ["xack"], None),
        (DeadLetter("invalid", "amount_mismatch", "E"), 1, ["xadd", "xack"], "invalid"),
        (Retry("payroll", "payroll_unavailable", "E"), 4, [], None),
        (Retry("payroll", "payroll_unavailable", "E"), 5, ["xadd", "xack"], "payroll"),
        (Retry("internal", "db_timeout", "E"), 5, ["xadd", "xack"], "internal"),
        (Retry("bank", "timeout", "E"), 5, [], None),
    ],
)
async def test_execute(
    result: HandleResult, delivery: int, ops: list[str], dlq_kind: str | None
) -> None:
    """Design §3.3.4: Ack → XACK; DeadLetter → DLQ+XACK; Retry → pending or DLQ at budget."""
    rig = make_rig()
    await rig.consumer.execute(message(), delivery, result)
    assert [op for op, _ in rig.redis.ops] == ops
    if dlq_kind is not None:
        assert rig.redis.ops[0][1][1]["error_kind"] == dlq_kind


async def test_xack_failure_is_logged(caplog_json: JsonLogCapture) -> None:
    """Design §3.2: a Redis error on XACK leaves the entry pending (redelivery stops at step 1)."""
    rig = make_rig()
    rig.redis.fail["xack"] = RedisConnectionError("down")
    await rig.consumer.execute(message(), 1, Ack("settled"))
    assert caplog_json.events("queue.read.error")[0]["error_code"] == "xack_ConnectionError"


async def test_ensure_group() -> None:
    """PAY-408: XGROUP CREATE … 0 MKSTREAM; BUSYGROUP is ignored, other errors raise."""
    rig = make_rig()
    await rig.consumer.ensure_group()
    assert rig.redis.ops[0] == (
        "xgroup_create",
        ("stream:payout.requested", "payout-worker", {"id": "0", "mkstream": True}),
    )
    rig.redis.fail["xgroup_create"] = ResponseError("BUSYGROUP Consumer Group name already exists")
    await rig.consumer.ensure_group()
    rig.redis.fail["xgroup_create"] = ResponseError("NOPERM")
    with pytest.raises(ResponseError):
        await rig.consumer.ensure_group()


async def test_slots() -> None:
    """PAY-403, C63: reservations take free slots; a full pool waits for a release."""
    slots = Slots(3)
    assert await slots.reserve(2) == 2
    assert await slots.reserve() == 1
    assert (slots.free, slots.used) == (0, 3)
    assert slots.try_reserve() == 0
    waiter = asyncio.create_task(slots.reserve())
    await asyncio.sleep(0)
    assert not waiter.done()
    await slots.release(2)
    assert await asyncio.wait_for(waiter, 1) == 2
    await slots.release(0)
    assert slots.free == 0


async def test_slots_are_served_in_turn() -> None:
    """C63: a queued reservation (the claim loop) is served before a later one (the reader),
    so the reader re-reserving after each read can't starve the claim loop; a cancelled
    waiter gives up its turn."""
    slots = Slots(2)
    assert await slots.reserve() == 2
    cancelled = asyncio.create_task(slots.reserve())
    first = asyncio.create_task(slots.reserve())
    await asyncio.sleep(0)
    second = asyncio.create_task(slots.reserve())
    await asyncio.sleep(0)
    cancelled.cancel()
    await asyncio.gather(cancelled, return_exceptions=True)
    assert slots.try_reserve() == 0  # someone is queued
    await slots.release(2)
    assert await asyncio.wait_for(first, 1) == 2
    assert not second.done()
    await slots.release(1)
    assert await asyncio.wait_for(second, 1) == 1


@dataclass
class HoldingRedis(FakeRedis):
    """Counts entries handed out (read with ``>`` or claimed) and not yet XACKed."""

    held: int = 0
    peak: int = 0
    counts: list[int] = field(default_factory=list)

    def _hand_out(self, n: int) -> None:
        self.held += n
        self.peak = max(self.peak, self.held)

    async def xreadgroup(self, group: str, name: str, streams: dict[str, str], **kw: Any) -> Any:
        self.counts.append(kw["count"])
        resp = await super().xreadgroup(group, name, streams, **kw)
        self._hand_out(sum(len(entries) for _, entries in resp))
        return resp

    async def xautoclaim(self, *a: Any, **kw: Any) -> Any:
        self.counts.append(kw["count"])
        resp = await super().xautoclaim(*a, **kw)
        self._hand_out(len(resp[1]))
        return resp

    async def xack(self, stream: str, group: str, entry_id: str) -> int:
        self.held -= 1
        return await super().xack(stream, group, entry_id)


async def test_never_holds_more_than_concurrency() -> None:
    """C63, PAY-403: slots are reserved before each XREADGROUP/XAUTOCLAIM, which never ask for
    more than the reservation, so the reader and the claim loop together never hold more than
    PAYOUT_CONCURRENCY messages (an entry never waits for a slot after it is delivered)."""
    gate = asyncio.Event()

    class Gated:
        async def handle(self, msg: StreamMessage, delivery: int) -> HandleResult:
            await gate.wait()
            return Ack("settled")

    redis = HoldingRedis()
    rig = make_rig(
        handler=Gated(), redis=redis, payout_concurrency=2, payout_claim_interval_s=0.001
    )
    env = message().field("envelope")
    assert env is not None
    redis.new = [
        [(b"1-0", {b"envelope": env}), (b"2-0", {b"envelope": env})],
        [(b"3-0", {b"envelope": env})],
    ]
    redis.pending = {"4-0": 2}
    redis.claims = [[b"0-0", [(b"4-0", {b"envelope": env})], []]]
    stop = asyncio.Event()
    runner = asyncio.create_task(rig.consumer.run(stop))
    for _ in range(30):
        await asyncio.sleep(0.01)
    assert rig.consumer.in_flight == 2
    assert redis.held == 2  # the claim loop and the reader wait: nothing read beyond the slots
    gate.set()
    for _ in range(200):
        if len(rig.done) >= 4:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(runner, 2)
    assert sorted(eid for eid, _ in rig.done) == ["1-0", "2-0", "3-0", "4-0"]
    assert redis.peak <= 2
    assert all(1 <= c <= 2 for c in redis.counts)


async def test_poll_gauges_once() -> None:
    """PAY-NFR-10: lag from XINFO GROUPS (nil keeps the last value); DLQ length (no key → 0)."""
    rig = make_rig()
    rig.redis.groups = [{"name": b"other", "lag": 9}, {"name": b"payout-worker", "lag": 3}]
    rig.redis.dlq_info = {"length": 7}
    await rig.consumer.poll_gauges_once()
    assert (rig.metrics.lag, rig.metrics.dlq_length) == (3.0, 7.0)
    rig.redis.groups = [{"name": b"payout-worker", "lag": None}]
    rig.redis.dlq_info = None
    await rig.consumer.poll_gauges_once()
    assert (rig.metrics.lag, rig.metrics.dlq_length) == (3.0, 0.0)
    rig.redis.fail["xinfo_groups"] = ResponseError("no such key")
    await rig.consumer.poll_gauges_once()
    assert rig.metrics.lag == 3.0


async def test_run_pending_first_then_new_then_claim(caplog_json: JsonLogCapture) -> None:
    """PAY-403, PAY-408: pending entries first (delivery from XPENDING; nil body → XACK and
    queue.message.trimmed), then new entries (delivery 1), and XAUTOCLAIM entries."""
    rig = make_rig(payout_claim_interval_s=0.001)
    env = message().field("envelope")
    assert env is not None
    rig.redis.history = [[(b"1-0", {b"envelope": env}), (b"2-0", {})]]
    rig.redis.pending = {"1-0": 3, "3-0": 2}
    rig.redis.new = [[(b"4-0", {b"envelope": env})]]
    rig.redis.claims = [
        [b"0-0", [(b"3-0", {b"envelope": env}), (b"5-0", {b"envelope": env})], [b"6-0"]]
    ]
    stop = asyncio.Event()
    runner = asyncio.create_task(rig.consumer.run(stop))
    for _ in range(200):
        if len(rig.done) >= 3:
            break
        await asyncio.sleep(0.01)
    assert rig.consumer.is_alive()
    stop.set()
    await asyncio.wait_for(runner, 2)
    handled = sorted(eid for eid, _ in rig.done)
    assert handled == ["1-0", "3-0", "4-0"]  # 5-0 had no PEL entry (ACKed elsewhere): skipped
    acked = [arg for op, arg in rig.redis.ops if op == "xack"]
    assert "2-0" in acked
    trimmed = {e["stream_id"] for e in caplog_json.events("queue.message.trimmed")}
    assert trimmed == {"2-0", "6-0"}
    assert caplog_json.events("worker.shutdown.completed")
    assert rig.consumer.stopping


async def test_read_errors_back_off_and_nogroup_recreates(caplog_json: JsonLogCapture) -> None:
    """PAY-408: a Redis error logs queue.read.error and backs off; NOGROUP recreates the group."""
    rig = make_rig(payout_claim_interval_s=0.001)
    rig.redis.fail["xreadgroup"] = RedisConnectionError("down")
    rig.redis.fail["xautoclaim"] = ResponseError("NOGROUP No such key")
    stop = asyncio.Event()
    runner = asyncio.create_task(rig.consumer.run(stop))
    for _ in range(100):
        if caplog_json.events("queue.read.error") and caplog_json.events("queue.group.recreated"):
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(runner, 2)
    assert caplog_json.events("queue.read.error")
    assert caplog_json.events("queue.group.recreated")


async def test_claim_skips_in_flight_and_follows_cursor() -> None:
    """Design §3.3.5: follow the cursor until 0-0; an in-flight ID isn't handed over twice."""
    rig = make_rig()
    rig.consumer._state.in_flight.add("7-0")
    env = message().field("envelope")
    assert env is not None
    rig.redis.claims = [[b"8-0", [(b"7-0", {b"envelope": env})], []], [b"0-0", [], []]]
    assert await rig.consumer.claim_once() == 0
    assert [a for op, a in rig.redis.ops if op == "xautoclaim"] == ["0-0", "8-0"]


async def test_shutdown_drains_then_cancels(caplog_json: JsonLogCapture) -> None:
    """PAY-408: stop() waits up to PAYOUT_SHUTDOWN_DRAIN_S, then cancels; entries stay pending."""

    class Slow:
        async def handle(self, msg: StreamMessage, delivery: int) -> HandleResult:
            await asyncio.sleep(10)
            return Ack("settled")  # pragma: no cover

    rig = make_rig(handler=Slow(), payout_shutdown_drain_s=0.05)
    env = message().field("envelope")
    assert env is not None
    rig.redis.new = [[(b"1-0", {b"envelope": env})]]
    stop = asyncio.Event()
    runner = asyncio.create_task(rig.consumer.run(stop))
    for _ in range(100):
        if rig.consumer.in_flight:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(runner, 2)
    [event] = caplog_json.events("worker.shutdown.completed")
    assert event["cancelled"] == 1
    assert "xack" not in [op for op, _ in rig.redis.ops]
