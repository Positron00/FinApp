"""The DLQ on real Redis 7 (design §3.2, §3.3.4, §5.2; PAY-408, PAY-NFR-11, C21).

Fields and schema (``contracts/events/payout.requested.dlq.v1.json``), the pre-initialised
counter going 0 → n, XADD before XACK with a real XADD failure, and ``MAXLEN ~ 10000``.
"""

from __future__ import annotations

import json
import re
from typing import Any

import redis.asyncio as aioredis
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from payout_worker.models import schema_path
from pycommon.db import new_id
from pycommon.testing import JsonLogCapture, OtelMemory

from tests.helpers import envelope_dict
from tests.integration.conftest import DLQ, GROUP, STREAM, Stack, runtime, wait_for

DLQ_SCHEMA = json.loads((schema_path().parent / "payout.requested.dlq.v1.json").read_text())
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


def _points(otel: OtelMemory) -> dict[str, Any]:
    return {a["outcome"]: v for a, v in otel.metric_points("queue_messages")}


async def _reading(r: aioredis.Redis, consumer: str) -> bool:
    try:
        rows: Any = await r.xinfo_consumers(STREAM, GROUP)
    except aioredis.ResponseError:
        return False
    return any(c["name"] in {consumer, consumer.encode()} for c in rows)


async def _unreadable(r: aioredis.Redis) -> str:
    """An entry with no envelope field: poison with no readable IDs (DLQ only, R5)."""
    entry_id: Any = await r.xadd(STREAM, {"event_id": "nope", "type": "payout.requested"})
    return str(entry_id.decode())


async def test_dlq_entries_match_c21_and_the_counter_starts_at_zero(
    otel_memory: OtelMemory, stack: Stack
) -> None:
    """PAY-408, PAY-NFR-11, C21: before any message the three queue_messages series exist at 0;
    two poison entries (no envelope; payout_not_found) → two DLQ entries with exactly the eight
    string fields that validate against the DLQ schema (delivery_count decoded as an integer),
    the envelope byte for byte, the source ID and this consumer's name; the dlq series 0 → 2;
    both originals ACKed."""
    app = await stack.worker()
    name = runtime(app).consumer.name
    await wait_for(lambda: _reading(stack.redis, name))
    assert _points(otel_memory) == {"ok": 0, "retry": 0, "dlq": 0}

    no_envelope = await _unreadable(stack.redis)
    unknown = envelope_dict(event_id=new_id(), payout_id=str(new_id()))
    raw = json.dumps(unknown).encode()
    not_found_id: Any = await stack.redis.xadd(
        STREAM, {"event_id": unknown["event_id"], "type": "payout.requested", "envelope": raw}
    )
    not_found = str(not_found_id.decode())

    async def two() -> bool:
        return int(await stack.redis.xlen(DLQ)) == 2

    await wait_for(two)
    entries: Any = await stack.redis.xrange(DLQ)
    by_source = {fields[b"source_id"].decode(): fields for _, fields in entries}
    assert set(by_source) == {no_envelope, not_found}
    validator = Draft202012Validator(DLQ_SCHEMA, format_checker=Draft202012Validator.FORMAT_CHECKER)
    for fields in by_source.values():
        decoded: dict[str, Any] = {k.decode(): v.decode() for k, v in fields.items()}
        assert set(decoded) == FIELDS
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", decoded["failed_at"])
        decoded["delivery_count"] = int(decoded["delivery_count"])
        validator.validate(decoded)
        assert (decoded["consumer"], decoded["delivery_count"]) == (name, 1)
    assert by_source[no_envelope][b"envelope"] == b""
    assert by_source[no_envelope][b"error_code"] == b"schema_invalid"
    assert by_source[not_found][b"envelope"] == raw
    assert by_source[not_found][b"error_code"] == b"payout_not_found"

    async def acked() -> bool:
        return await stack.pending() == 0

    await wait_for(acked)
    assert _points(otel_memory) == {"ok": 0, "retry": 0, "dlq": 2}


async def test_failed_dlq_xadd_leaves_the_entry_pending(
    otel_memory: OtelMemory, stack: Stack, caplog_json: JsonLogCapture
) -> None:
    """PAY-408, C21: XADD before XACK. While the DLQ key can't take an XADD (a real WRONGTYPE
    error from Redis), the poison entry is not ACKed and the dlq counter stays 0; once the key
    is usable, XAUTOCLAIM redelivers it, the DLQ entry is written, the counter goes to 1 and the
    entry is ACKed."""
    await stack.redis.set(DLQ, "not-a-stream")
    app = await stack.worker()
    entry_id = await _unreadable(stack.redis)

    async def xadd_failed() -> bool:
        return any(
            e.get("dlq") is False and e.get("dlq_error") == "ResponseError"
            for e in caplog_json.events("queue.message.failed")
        )

    await wait_for(xadd_failed)
    assert await stack.pending() == 1
    assert _points(otel_memory)["dlq"] == 0
    assert runtime(app).consumer.is_alive()

    await stack.redis.delete(DLQ)

    async def dead_lettered() -> bool:
        return int(await stack.redis.xlen(DLQ)) == 1

    await wait_for(dead_lettered)
    [entry] = await stack.dlq()
    assert entry["source_id"] == entry_id
    assert int(entry["delivery_count"]) >= 2  # redelivered by XAUTOCLAIM

    async def acked() -> bool:
        return await stack.pending() == 0

    await wait_for(acked)
    assert _points(otel_memory)["dlq"] == 1


async def test_dlq_is_bounded_by_maxlen(stack: Stack) -> None:
    """PAY-408, C21: XADD … MAXLEN ~ 10000 trims the DLQ (approximately, by whole nodes) when a
    new entry is dead-lettered; the new entry is the last one."""
    filler = {"envelope": "", "source_id": "1-0", "error_kind": "invalid"}
    pipe = stack.redis.pipeline(transaction=False)
    for _ in range(10_200):
        pipe.xadd(DLQ, filler)  # type: ignore[arg-type]
    await pipe.execute()
    assert int(await stack.redis.xlen(DLQ)) == 10_200
    await stack.worker()
    entry_id = await _unreadable(stack.redis)

    async def written() -> bool:
        last: Any = await stack.redis.xrevrange(DLQ, count=1)
        return bool(last) and last[0][1].get(b"source_id") == entry_id.encode()

    await wait_for(written)
    length = int(await stack.redis.xlen(DLQ))
    assert 10_000 <= length < 10_201
