"""Envelope, producer and relay loop with fakes (platform LLD §3.9; PAY-402, R7, C22)."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, cast

import pydantic
import pytest
from opentelemetry import trace
from pycommon import streams
from pycommon.streams import Envelope, RelayLoop, StreamItem, StreamProducer
from pycommon.testing import FakeClock, JsonLogCapture, OtelMemory
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

STREAM = "stream:payout.requested"


def _envelope(**over: Any) -> dict[str, Any]:
    e: dict[str, Any] = {
        "event_id": str(uuid.uuid4()),
        "type": "payout.requested",
        "version": 1,
        "occurred_at": "2026-11-10T12:00:00.123Z",
        "producer": "payroll",
        "traceparent": "00-" + "a" * 32 + "-" + "b" * 16 + "-01",
        "data": {"payout_id": "p"},
    }
    e.update(over)
    return e


def test_envelope_new_fields(fake_clock: FakeClock) -> None:
    """PAY-402: Envelope.new sets a UUIDv7 id, UTC occurred_at with ms + Z, and a traceparent."""
    env = Envelope.new(
        type="payout.requested", version=1, producer="payroll", data={"a": 1}, clock=fake_clock
    )
    assert env.event_id.version == 7
    assert env.occurred_at == fake_clock.now()
    doc = json.loads(env.to_json())
    assert doc["occurred_at"] == "2026-11-10T12:00:00.000Z"
    assert set(doc) == {
        "event_id",
        "type",
        "version",
        "occurred_at",
        "producer",
        "traceparent",
        "data",
    }
    assert streams.TRACEPARENT_RE.match(doc["traceparent"])
    assert " " not in env.to_json()
    assert Envelope.model_validate_json(env.to_json()) == env


def test_envelope_rejects_extra_fields_and_bad_traceparent() -> None:
    """C22: extra fields are forbidden; traceparent is required and must be W3C."""
    assert Envelope.model_validate(_envelope()).version == 1
    with pytest.raises(pydantic.ValidationError):
        Envelope.model_validate(_envelope(extra="x"))
    no_tp = _envelope()
    del no_tp["traceparent"]
    with pytest.raises(pydantic.ValidationError):
        Envelope.model_validate(no_tp)
    for bad in ["", "garbage", "00-" + "0" * 32 + "-" + "b" * 16 + "-01", "01-abc"]:
        with pytest.raises(pydantic.ValidationError):
            Envelope.model_validate(_envelope(traceparent=bad))
    with pytest.raises(pydantic.ValidationError):
        Envelope.model_validate(_envelope(occurred_at=datetime(2026, 1, 1)))  # noqa: DTZ001


def test_envelope_traceparent_from_active_span(otel_memory: OtelMemory) -> None:
    """C22 / NFR-09: inside a span the envelope carries that span's context."""
    tracer = otel_memory.tracer_provider.get_tracer("t")
    with tracer.start_as_current_span("approve") as span:
        env = Envelope.new(type="t", version=1, producer="payroll", data={})
        ctx = span.get_span_context()
    assert env.traceparent.startswith(f"00-{ctx.trace_id:032x}-{ctx.span_id:016x}-")
    outside = Envelope.new(type="t", version=1, producer="payroll", data={})
    assert outside.traceparent.endswith("-00")
    assert outside.occurred_at.tzinfo is UTC


class FakePipeline:
    def __init__(self, owner: FakeRedis) -> None:
        self.owner = owner
        self.calls: list[tuple[str, dict[str, str], int, bool]] = []

    async def __aenter__(self) -> FakePipeline:
        return self

    async def __aexit__(self, *a: object) -> None:
        return None

    def xadd(self, stream: str, fields: dict[str, str], *, maxlen: int, approximate: bool) -> None:
        self.calls.append((stream, fields, maxlen, approximate))

    async def execute(self, *, raise_on_error: bool = True) -> list[Any]:
        assert raise_on_error is False
        if self.owner.pipeline_error is not None:
            raise self.owner.pipeline_error
        self.owner.xadds.extend(self.calls)
        out: list[Any] = []
        for i, _ in enumerate(self.calls):
            fail = self.owner.fail_items.get(i)
            out.append(fail if fail is not None else f"1-{i}".encode())
        return out


class FakeRedis:
    def __init__(self) -> None:
        self.xadds: list[tuple[str, dict[str, str], int, bool]] = []
        self.fail_items: dict[int, Exception] = {}
        self.pipeline_error: Exception | None = None
        self.transactions: list[bool] = []

    async def xadd(
        self, stream: str, fields: dict[str, str], *, maxlen: int, approximate: bool
    ) -> bytes:
        self.xadds.append((stream, fields, maxlen, approximate))
        return b"1-0"

    def pipeline(self, *, transaction: bool) -> FakePipeline:
        self.transactions.append(transaction)
        return FakePipeline(self)


async def test_publish_sends_exactly_three_fields_with_maxlen(otel_memory: OtelMemory) -> None:
    """R7: XADD MAXLEN ~ with exactly event_id, type, envelope; a PRODUCER span is recorded."""
    fake = FakeRedis()
    producer = StreamProducer(cast(Redis, fake))
    eid = uuid.uuid4()
    entry = await producer.publish(
        STREAM, event_id=eid, type="payout.requested", envelope_json='{"x":1}', maxlen=100000
    )
    assert entry == b"1-0"
    ((stream, fields, maxlen, approximate),) = fake.xadds
    assert stream == STREAM
    assert fields == {"event_id": str(eid), "type": "payout.requested", "envelope": '{"x":1}'}
    assert (maxlen, approximate) == (100000, True)
    (span,) = otel_memory.spans()
    assert span.name == f"{STREAM} publish"
    assert span.kind == trace.SpanKind.PRODUCER
    attrs = dict(span.attributes or {})
    assert attrs["messaging.system"] == "redis"
    assert attrs["messaging.destination.name"] == STREAM
    assert attrs["messaging.message.id"] == str(eid)


async def test_publish_many_uses_one_non_transactional_pipeline() -> None:
    """R7 / PAY-402: publish_many is one pipeline (transaction=False) with per-item results."""
    fake = FakeRedis()
    fake.fail_items = {1: RuntimeError("OOM")}
    producer = StreamProducer(cast(Redis, fake))
    items = [StreamItem(uuid.uuid4(), "payout.requested", f'{{"n":{i}}}') for i in range(3)]
    results = await producer.publish_many(STREAM, items, maxlen=10)
    assert fake.transactions == [False]
    assert results[0] == b"1-0"
    assert isinstance(results[1], RuntimeError)
    assert results[2] == b"1-2"
    assert [f["envelope"] for _, f, _, _ in fake.xadds] == ['{"n":0}', '{"n":1}', '{"n":2}']
    assert await producer.publish_many(STREAM, [], maxlen=10) == []


class FakeSession:
    def __init__(self, log: list[str], fail_commit: bool) -> None:
        self.log = log
        self.fail_commit = fail_commit

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *a: object) -> None:
        return None

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[None]:
        self.log.append("begin")
        yield
        if self.fail_commit:
            raise ConnectionError("commit failed")
        self.log.append("commit")


class FakeSessionMaker:
    def __init__(self) -> None:
        self.log: list[str] = []
        self.fail_commit = False

    def __call__(self) -> FakeSession:
        return FakeSession(self.log, self.fail_commit)


class FakeRepo:
    def __init__(self, batches: list[list[StreamItem]]) -> None:
        self.batches = batches
        self.sent: list[StreamItem] = []
        self.failed: list[tuple[str, list[StreamItem]]] = []
        self.claims: list[int] = []

    async def claim_unsent(self, session: AsyncSession, *, limit: int) -> Sequence[StreamItem]:
        self.claims.append(limit)
        return self.batches.pop(0) if self.batches else []

    async def mark_sent(self, session: AsyncSession, items: Sequence[StreamItem]) -> None:
        self.sent.extend(items)

    async def mark_failed(
        self, session: AsyncSession, items: Sequence[StreamItem], *, error_type: str
    ) -> None:
        self.failed.append((error_type, list(items)))


def _items(n: int, tag: str = "") -> list[StreamItem]:
    return [StreamItem(uuid.uuid4(), "payout.requested", "{}", ref=(tag, i)) for i in range(n)]


def _loop(
    repo: FakeRepo, fake: FakeRedis, clock: FakeClock, *, batch_size: int = 3
) -> tuple[RelayLoop, FakeSessionMaker]:
    sf = FakeSessionMaker()
    loop = RelayLoop(
        cast(async_sessionmaker[AsyncSession], sf),
        StreamProducer(cast(Redis, fake)),
        repo,
        stream=STREAM,
        maxlen=100,
        batch_size=batch_size,
        clock=clock,
    )
    return loop, sf


async def test_relay_marks_sent_and_failed_by_class_name(fake_clock: FakeClock) -> None:
    """PAY-402: items with an entry id are marked sent; the rest failed with the error class."""
    batch = _items(3, "b")
    repo = FakeRepo([batch])
    fake = FakeRedis()
    fake.fail_items = {2: ValueError("bad")}
    loop, sf = _loop(repo, fake, fake_clock)
    assert await loop.relay_once() == 2
    assert repo.sent == batch[:2]
    assert repo.failed == [("ValueError", [batch[2]])]
    assert repo.sent[0].ref == ("b", 0)
    assert sf.log == ["begin", "commit"]
    assert await loop.relay_once() == 0
    assert repo.claims == [3, 3]


async def test_whole_pipeline_failure_marks_every_item_failed(fake_clock: FakeClock) -> None:
    """PAY-402: if the pipeline raises, every claimed item is marked failed."""
    batch = _items(2)
    repo = FakeRepo([batch])
    fake = FakeRedis()
    fake.pipeline_error = ConnectionError("redis down")
    loop, _ = _loop(repo, fake, fake_clock)
    assert await loop.relay_once() == 0
    assert repo.failed == [("ConnectionError", batch)]


async def _run_until(loop: RelayLoop, stop: asyncio.Event, cond: Any) -> None:
    task = asyncio.create_task(loop.run(stop))
    for _ in range(500):
        await asyncio.sleep(0)
        if cond():
            break
    stop.set()
    await asyncio.wait_for(task, 2)


async def test_backoff_doubles_with_jitter_and_resets(
    fake_clock: FakeClock, caplog_json: JsonLogCapture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PAY-402: failures back off full-jitter min(0.2·2^k, 5) s; a clean batch resets k."""
    monkeypatch.setattr(streams._rng, "uniform", lambda lo, hi: hi)
    fake = FakeRedis()
    fake.pipeline_error = ConnectionError("down")
    repo = FakeRepo([_items(1) for _ in range(8)])
    loop, _ = _loop(repo, fake, fake_clock, batch_size=5)
    stop = asyncio.Event()
    await _run_until(loop, stop, lambda: len(fake_clock.sleeps) >= 7)
    assert fake_clock.sleeps[:7] == pytest.approx([0.2, 0.4, 0.8, 1.6, 3.2, 5.0, 5.0])
    events = caplog_json.events("outbox.relay.error")
    assert events[0]["error_type"] == "ConnectionError"
    assert (events[0]["batch_size"], events[0]["failed"], events[0]["backoff_ms"]) == (1, 1, 200)
    assert events[0]["level"] == "error"

    fake.pipeline_error = None
    fake_clock.sleeps.clear()
    repo.batches = [_items(1), [], _items(1)]
    fake.pipeline_error = ConnectionError("down")
    loop2, _ = _loop(repo, fake, fake_clock, batch_size=5)
    loop2._k = 4
    fake.pipeline_error = None
    stop2 = asyncio.Event()
    await _run_until(loop2, stop2, lambda: len(fake_clock.sleeps) >= 1)
    assert loop2._k == 0
    assert fake_clock.sleeps[0] == pytest.approx(0.2)  # the poll interval, not a backoff


async def test_jitter_is_bounded(fake_clock: FakeClock) -> None:
    """PAY-402: each backoff is in [0, min(0.2·2^k, 5)]."""
    loop, _ = _loop(FakeRepo([]), FakeRedis(), fake_clock)
    for k in range(10):
        delay = loop._next_backoff()
        assert 0 <= delay <= min(0.2 * 2**k, 5.0)


async def test_commit_failure_is_a_relay_error(
    fake_clock: FakeClock, caplog_json: JsonLogCapture
) -> None:
    """PAY-402: a failed commit after XADD is logged and backed off (delivery is at least once)."""
    repo = FakeRepo([_items(1)])
    loop, sf = _loop(repo, FakeRedis(), fake_clock)
    sf.fail_commit = True
    stop = asyncio.Event()
    await _run_until(loop, stop, lambda: caplog_json.events("outbox.relay.error"))
    (event, *_) = caplog_json.events("outbox.relay.error")
    assert event["error_type"] == "ConnectionError"


async def test_wake_skips_the_poll_wait(fake_clock: FakeClock) -> None:
    """PAY-402: wake() makes the loop claim again without waiting for the poll interval."""
    repo = FakeRepo([[], _items(1)])
    sleeps_when_sent: list[int] = []
    mark_sent = repo.mark_sent

    async def spy(session: AsyncSession, items: Sequence[StreamItem]) -> None:
        sleeps_when_sent.append(len(fake_clock.sleeps))
        await mark_sent(session, items)

    repo.mark_sent = spy  # type: ignore[method-assign]
    loop, _ = _loop(repo, FakeRedis(), fake_clock)
    loop.wake()
    stop = asyncio.Event()
    await _run_until(loop, stop, lambda: len(repo.sent) == 1)
    assert len(repo.sent) == 1
    assert sleeps_when_sent == [0]  # the empty claim did not wait for the poll interval


async def test_full_batch_loops_at_once_and_stop_finishes(fake_clock: FakeClock) -> None:
    """PAY-402: a full batch loops at once; after stop, run returns."""
    repo = FakeRepo([_items(3), _items(3), _items(1)])
    loop, _ = _loop(repo, FakeRedis(), fake_clock, batch_size=3)
    stop = asyncio.Event()
    await _run_until(loop, stop, lambda: len(repo.sent) == 7)
    assert len(repo.sent) == 7
    assert repo.claims[:3] == [3, 3, 3]
    assert fake_clock.sleeps[:1] == [0.2]


async def test_real_wait_returns_on_wake() -> None:
    """PAY-402: with the real clock, wake() ends the 200 ms poll wait early."""
    repo = FakeRepo([[], _items(1)])
    sf = FakeSessionMaker()
    loop = RelayLoop(
        cast(async_sessionmaker[AsyncSession], sf),
        StreamProducer(cast(Redis, FakeRedis())),
        repo,
        stream=STREAM,
        maxlen=10,
        poll_interval_s=30,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(loop.run(stop))
    await asyncio.sleep(0.05)
    loop.wake()
    for _ in range(100):
        await asyncio.sleep(0.01)
        if repo.sent:
            break
    stop.set()
    await asyncio.wait_for(task, 2)
    assert len(repo.sent) == 1
