"""Outbox atomicity and the relay (design §5.1, §5.2; PAY-402, PAY-607, PAY-NFR-09, R7, C22)."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import pytest
import redis.asyncio as aioredis
import redis.exceptions
from payroll.db.repositories import AuditRepo
from payroll.events.envelope import STREAM, validate_envelope
from payroll.events.relay import build_relay
from pycommon.streams import RelayLoop, StreamProducer
from pycommon.testing import JsonLogCapture, OtelMemory

from tests.integration.conftest import Api, ApiFactory

pytestmark = pytest.mark.integration


async def _entries(client: aioredis.Redis) -> list[tuple[bytes, dict[bytes, bytes]]]:
    return cast(list[tuple[bytes, dict[bytes, bytes]]], await client.xrange(STREAM))


def _relay(api: Api, **kw: Any) -> RelayLoop:
    ctx = api.app.state.ctx
    return build_relay(
        api.rt.sessions, StreamProducer(ctx.redis), maxlen=100_000,
        batch_size=kw.get("batch_size", 100), poll_ms=20, validate=kw.get("validate", True),
    )  # fmt: skip


async def test_rollback_leaves_neither_state_nor_event(
    api: Api, db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PAY-402: a failure after the outbox rows were added rolls back the run change, payouts,
    attempts and events together."""
    run = await api.calculated_run(2)

    async def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("forced failure after the outbox insert")

    monkeypatch.setattr(AuditRepo, "add", boom)
    r = await api.approve(run["id"])
    assert (r.status_code, r.json()["error"]["code"]) == (500, "internal_error")
    for table in ("payouts", "payout_attempts", "outbox", "idempotency_keys"):
        assert await db.fetchval(f"SELECT count(*) FROM payroll.{table}") == 0, table
    assert await db.fetchval("SELECT status FROM payroll.pay_runs") == "calculated"


async def test_relay_publishes_three_fields_that_validate(
    api: Api, db: Any, redis_admin: aioredis.Redis
) -> None:
    """PAY-402, R7, C22: XADD fields are exactly event_id, type, envelope; the envelope validates
    against the schema; rows are marked sent and last_published_at is updated."""
    run = await api.approved_run(2)
    before = {
        r["payout_id"]: r["last_published_at"]
        for r in await db.fetch("SELECT payout_id, last_published_at FROM payroll.payout_attempts")
    }
    assert await _relay(api).relay_once() == 2
    entries = await _entries(redis_admin)
    assert len(entries) == 2
    outbox_ids = {str(r["id"]) for r in await db.fetch("SELECT id FROM payroll.outbox")}
    for _, fields in entries:
        assert set(fields) == {b"event_id", b"type", b"envelope"}
        assert fields[b"type"] == b"payout.requested"
        envelope = json.loads(fields[b"envelope"])
        validate_envelope(envelope)
        assert envelope["event_id"] == fields[b"event_id"].decode()
        assert envelope["data"]["pay_run_id"] == run["id"]
        assert envelope["event_id"] in outbox_ids
    assert await db.fetchval("SELECT count(*) FROM payroll.outbox WHERE sent_at IS NULL") == 0
    for row in await db.fetch("SELECT payout_id, last_published_at FROM payroll.payout_attempts"):
        assert row["last_published_at"] > before[row["payout_id"]]
    assert await _relay(api).relay_once() == 0  # nothing left


async def test_two_relays_never_send_a_row_twice(
    api: Api, db: Any, redis_admin: aioredis.Redis
) -> None:
    """PAY-402: two relays in parallel (SKIP LOCKED) publish each row exactly once."""
    await api.approved_run(12)
    a, b = _relay(api, batch_size=3), _relay(api, batch_size=3)
    for _ in range(12):  # each round runs both batches concurrently; at most 4 rounds are needed
        sent = list(await asyncio.gather(a.relay_once(), b.relay_once()))
        if sent == [0, 0]:
            break
    assert await db.fetchval("SELECT count(*) FROM payroll.outbox WHERE sent_at IS NULL") == 0
    ids = [f[b"event_id"] for _, f in await _entries(redis_admin)]
    assert len(ids) == len(set(ids)) == 12


async def test_background_relay_wakes_after_approve(
    api_factory: ApiFactory, redis_admin: aioredis.Redis
) -> None:
    """PAY-402: the in-process relay publishes right after the approve commits."""
    api = await api_factory(relay=True, payroll_outbox_poll_ms=60_000)
    await api.approved_run(1)
    # XREAD BLOCK returns as soon as the entry lands; the 60 s poll can't be what delivers it.
    got = await redis_admin.xread({STREAM: "0"}, count=1, block=5000)
    assert got, "the relay was not woken"


async def test_redis_failure_keeps_rows_unsent(api: Api, db: Any) -> None:
    """PAY-402: a failed XADD marks the rows failed (relay_attempts, last_error) and unsent."""

    class Broken:
        def pipeline(self, **_: Any) -> Any:
            raise redis.exceptions.ConnectionError("down")

    await api.approved_run(1)
    relay = build_relay(
        api.rt.sessions,
        StreamProducer(cast(Any, Broken())),
        maxlen=10,
        batch_size=10,
        poll_ms=10,
    )
    assert await relay.relay_once() == 0
    row = await db.fetchrow("SELECT * FROM payroll.outbox")
    assert row["sent_at"] is None
    assert (row["relay_attempts"], row["last_error"]) == (1, "ConnectionError")
    assert await _relay(api).relay_once() == 1  # catch-up once Redis is back


async def test_payroll_acl_user_may_only_xadd_its_stream(api: Api) -> None:
    """PAY-607: payroll's Redis user may XADD to stream:payout.requested, nothing else."""
    client = api.app.state.ctx.redis
    await client.set("payroll:probe", "1")
    with pytest.raises(redis.exceptions.NoPermissionError):
        await client.xadd("stream:other", {"a": "b"})
    with pytest.raises(redis.exceptions.NoPermissionError):
        await client.set("web:probe", "1")
    with pytest.raises(redis.exceptions.NoPermissionError):
        await client.xrange(STREAM)


async def test_traceparent_continues_the_approve_trace(
    otel_memory: OtelMemory, api_factory: ApiFactory, redis_admin: aioredis.Redis
) -> None:
    """PAY-NFR-09: the publish span is a child of the approve trace, and the published
    envelope's traceparent names the publish span."""
    api = await api_factory()
    await api.approved_run(1)
    await _relay(api).relay_once()
    [(_, fields)] = await _entries(redis_admin)
    tp = json.loads(fields[b"envelope"])["traceparent"]
    spans = otel_memory.spans()
    approve = next(s for s in spans if s.name == "payroll.pay_run.approve")
    publish = next(s for s in spans if s.name == "payroll.outbox.publish")
    assert publish.context.trace_id == approve.context.trace_id
    assert publish.parent is not None
    assert publish.parent.span_id == approve.context.span_id
    assert tp.startswith(f"00-{publish.context.trace_id:032x}-{publish.context.span_id:016x}-")
    assert publish.attributes is not None
    assert publish.attributes["messaging.destination.name"] == STREAM


async def test_relay_revalidates_and_holds_back_an_invalid_envelope(
    api: Api, db: Any, redis_admin: aioredis.Redis, caplog_json: JsonLogCapture
) -> None:
    """PAY-402, C60, C63: with PAYROLL_VALIDATE_EVENTS the relay re-validates each envelope after
    the traceparent rewrite. An invalid row isn't published: it's marked failed, logged and left
    unsent, while the valid rows of the batch go out. With validation off it is published."""
    await api.approved_run(2)
    bad = await db.fetchval("SELECT id FROM payroll.outbox ORDER BY id LIMIT 1")
    await db.execute(
        "UPDATE payroll.outbox SET payload = jsonb_set(payload, '{data,currency}', '\"EUR\"') "
        "WHERE id = $1",
        bad,
    )
    assert await _relay(api).relay_once() == 1
    row = await db.fetchrow("SELECT * FROM payroll.outbox WHERE id = $1", bad)
    assert row["sent_at"] is None
    assert (row["relay_attempts"], row["last_error"]) == (1, "EnvelopeInvalidError")
    published = [f[b"event_id"].decode() for _, f in await _entries(redis_admin)]
    assert len(published) == 1
    assert str(bad) not in published
    [event] = caplog_json.events("outbox.relay.error")
    assert (event["error_type"], event["failed"]) == ("EnvelopeInvalidError", 1)
    assert await _relay(api, validate=False).relay_once() == 1
    assert len(await _entries(redis_admin)) == 2
