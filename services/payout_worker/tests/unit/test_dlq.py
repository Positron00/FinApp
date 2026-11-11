"""The DLQ write for every error code (design §3.2; PAY-408, PAY-NFR-07, C21).

Over a fake Redis: exactly the eight C21 fields, all strings; ``MAXLEN ~ 10000``; ``envelope``
byte for byte (``""`` when missing); ``error`` ≤ 500 characters with no amount, bank details,
token or body; XADD, then the ``dlq`` counter, then XACK; a failed XADD leaves the entry pending
with no counter increment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from payout_worker.consumer import Consumer
from payout_worker.handler import DeadLetter, PayoutHandler, Retry, error_text
from payout_worker.metrics import Metrics, QueueOutcome
from pycommon.testing import FakeClock, OtelMemory
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError

from tests.helpers import (
    ACCOUNT,
    AMOUNT,
    API_KEY,
    COMPANY_ID,
    ROUTING,
    TOKENS,
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
CODES = [
    ("invalid", "schema_invalid"),
    ("invalid", "amount_mismatch"),
    ("invalid", "payout_not_found"),
    ("payroll", "payroll_unavailable"),
    ("payroll", "breaker_open"),
    ("payroll", "invalid_token"),
    ("payroll", "forbidden_role"),
    ("payroll", "idempotency_key_required"),
    ("payroll", "validation_error"),
    ("payroll", "malformed_response"),
    ("internal", "db_timeout"),
    ("internal", "db_unavailable"),
    ("internal", "handler_deadline"),
    ("internal", "unexpected"),
]


@dataclass
class OrderedRedis:
    """Records XADD and XACK in one list shared with the counter, so the order is visible."""

    ops: list[tuple[str, Any]] = field(default_factory=list)
    xadd_error: Exception | None = None

    async def xadd(self, stream: str, fields: Any, **kw: Any) -> bytes:
        self.ops.append(("xadd", (stream, dict(fields), kw)))
        if self.xadd_error is not None:
            raise self.xadd_error
        return b"9-0"

    async def xack(self, stream: str, group: str, entry_id: str) -> int:
        self.ops.append(("xack", entry_id))
        return 1


def _consumer(redis: OrderedRedis, metrics: Metrics) -> Consumer:
    s = make_settings(payout_max_deliveries=5)
    handler = PayoutHandler(s, FakeRepo(), FakePayroll(), FakeBank(), metrics)
    r = cast(Redis, redis)
    return Consumer(s, handler, r, r, metrics, clock=FakeClock())


def _record_counter(metrics: Metrics, ops: list[tuple[str, Any]]) -> None:
    original = metrics.message

    def message_and_record(outcome: QueueOutcome) -> None:
        ops.append(("counter", outcome))
        original(outcome)

    metrics.message = message_and_record  # type: ignore[method-assign]


def _text(v: bytes | str) -> str:
    return v.decode() if isinstance(v, bytes) else v


@pytest.mark.parametrize(("kind", "code"), CODES)
@pytest.mark.parametrize("drop_envelope", [False, True])
async def test_every_error_code_writes_the_c21_entry(
    kind: str, code: str, drop_envelope: bool
) -> None:
    """PAY-408, C21: for every error_code, exactly the eight fields as strings, MAXLEN ~ 10000,
    the envelope byte for byte ("" when the entry has none), the delivery number, the consumer
    name and an RFC 3339 UTC time; then the counter, then XACK."""
    redis = OrderedRedis()
    metrics = make_metrics()
    _record_counter(metrics, redis.ops)
    consumer = _consumer(redis, metrics)
    msg = message(drop_envelope=drop_envelope, entry_id="1700000000000-7")
    delivery = 1 if kind == "invalid" else 5
    assert await consumer.dead_letter(msg, delivery, kind, code, error_text(RuntimeError, code))

    assert [op for op, _ in redis.ops] == ["xadd", "counter", "xack"]
    assert redis.ops[1] == ("counter", "dlq")
    assert redis.ops[2] == ("xack", "1700000000000-7")
    stream, fields, kw = redis.ops[0][1]
    assert stream == "stream:payout.requested.dlq"
    assert kw == {"maxlen": 10_000, "approximate": True}
    assert set(fields) == FIELDS
    assert all(isinstance(v, str | bytes) for v in fields.values())
    expected_env = b"" if drop_envelope else msg.field("envelope")
    assert (fields["envelope"] or b"") == expected_env
    assert (fields["source_id"], fields["error_kind"], fields["error_code"]) == (
        "1700000000000-7",
        kind,
        code,
    )
    assert (fields["delivery_count"], fields["consumer"]) == (str(delivery), "worker-test-1")
    assert fields["failed_at"] == "2026-11-10T12:00:00.000Z"
    assert fields["error"] == f"RuntimeError: {code}"


@pytest.mark.parametrize(("kind", "code"), CODES)
def test_error_text_is_short_and_clean(kind: str, code: str) -> None:
    """PAY-NFR-07, C21: the error field is the class name plus the fixed code, ≤ 500 characters,
    never an amount, bank details, a token or a response body."""
    text = error_text(RuntimeError(f"routing {ROUTING} account {ACCOUNT} {AMOUNT}"), code)
    assert text == f"RuntimeError: {code}"
    assert len(error_text(RuntimeError, "x" * 900)) <= 500
    token = TOKENS.mint(issuer="payout-worker", aud="payroll", sub="s", cid=COMPANY_ID)
    for secret in (ROUTING, ACCOUNT, str(AMOUNT), API_KEY, token):
        assert secret not in text


@pytest.mark.parametrize(
    "error", [RedisConnectionError("down"), ResponseError("WRONGTYPE Operation against a key")]
)
async def test_failed_xadd_leaves_the_entry_pending(
    otel_memory: OtelMemory, error: Exception
) -> None:
    """C21, PAY-408: when the DLQ XADD fails there is no counter increment and no XACK, so the
    entry stays pending and XAUTOCLAIM retries it."""
    redis = OrderedRedis(xadd_error=error)
    metrics = make_metrics()
    metrics.preinit()
    consumer = _consumer(redis, metrics)
    await consumer.execute(message(), 1, DeadLetter("invalid", "schema_invalid", "E: x"))
    await consumer.execute(
        message(), 5, Retry("payroll", "payroll_unavailable", "PayrollUnavailable: x")
    )
    assert [op for op, _ in redis.ops] == ["xadd", "xadd"]
    points = {a["outcome"]: v for a, v in otel_memory.metric_points("queue_messages")}
    assert points == {"ok": 0, "retry": 0, "dlq": 0}
