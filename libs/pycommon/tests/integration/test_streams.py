"""Stream publish and read as ACL users on real Redis (platform LLD §3.8-3.9; PAY-402, 403, 607)."""

import json
import time
import uuid
from typing import Any

import pytest
import redis.exceptions
from pycommon.otel import context_from_traceparent
from pycommon.redis_client import create_redis
from pycommon.streams import Envelope, StreamItem, StreamProducer
from pycommon.testing import OtelMemory

from tests.helpers import make_settings
from tests.integration.conftest import RedisServer

pytestmark = pytest.mark.integration

STREAM = "stream:payout.requested"
GROUP = "payout-worker"


async def test_payroll_publishes_three_fields_and_worker_continues_the_trace(
    redis_server: RedisServer, flush_redis: None, otel_memory: OtelMemory
) -> None:
    """R7 / PAY-402 / NFR-09: payroll XADDs exactly three fields; the worker continues the trace."""
    payroll = create_redis(redis_server.settings("payroll"))
    worker = create_redis(redis_server.settings("payout-worker"))
    try:
        await worker.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
        tracer = otel_memory.tracer_provider.get_tracer("payroll")
        with tracer.start_as_current_span("approve") as span:
            env = Envelope.new(
                type="payout.requested", version=1, producer="payroll", data={"payout_id": "p1"}
            )
            producer_trace = span.get_span_context().trace_id
        producer = StreamProducer(payroll)
        entry = await producer.publish(
            STREAM, event_id=env.event_id, type=env.type, envelope_json=env.to_json(), maxlen=100000
        )
        more = await producer.publish_many(
            STREAM, [StreamItem(uuid.uuid4(), "payout.requested", env.to_json())], maxlen=100000
        )
        assert isinstance(more[0], bytes)
        with redis_server.client() as admin:
            entries: Any = admin.xrange(STREAM)
        (first_id, fields), _ = entries
        assert first_id == entry
        assert set(fields) == {b"event_id", b"type", b"envelope"}
        assert fields[b"event_id"].decode() == str(env.event_id)

        read: Any = await worker.xreadgroup(GROUP, "c1", {STREAM: ">"}, count=1)
        ((_, messages),) = read
        ((_, msg),) = messages
        received = Envelope.model_validate_json(msg[b"envelope"])
        assert received.to_json() == env.to_json()  # occurred_at is kept to the millisecond
        consumer_tracer = otel_memory.tracer_provider.get_tracer("worker")
        with consumer_tracer.start_as_current_span(
            "process", context=context_from_traceparent(received.traceparent)
        ) as child:
            assert child.get_span_context().trace_id == producer_trace
        assert json.loads(msg[b"envelope"])["traceparent"] == env.traceparent
    finally:
        await payroll.aclose()
        await worker.aclose()


async def test_acl_blocks_other_users_from_the_stream(
    redis_server: RedisServer, flush_redis: None
) -> None:
    """PAY-607: the web ACL user cannot XADD to stream:payout.requested."""
    web = create_redis(redis_server.settings("web"))
    try:
        with pytest.raises(redis.exceptions.NoPermissionError):
            await StreamProducer(web).publish(
                STREAM, event_id=uuid.uuid4(), type="t", envelope_json="{}", maxlen=10
            )
        assert await web.ping()
    finally:
        await web.aclose()


async def test_blocking_read_needs_the_long_timeout_client(
    redis_server: RedisServer, flush_redis: None
) -> None:
    """PAY-403 / C21: BLOCK 5000 times out on the 500 ms client but not on a 5.5 s client."""
    settings = redis_server.settings("payout-worker")
    default = create_redis(settings)
    blocking = create_redis(settings, socket_timeout_ms=5500, client_name="worker-blocking")
    try:
        await default.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
        start = time.monotonic()
        with pytest.raises(redis.exceptions.TimeoutError):
            await default.xreadgroup(GROUP, "c1", {STREAM: ">"}, count=1, block=5000)
        assert time.monotonic() - start < 1.5
        start = time.monotonic()
        result = await blocking.xreadgroup(GROUP, "c1", {STREAM: ">"}, count=1, block=5000)
        assert not result
        assert 4.8 < time.monotonic() - start < 5.6
    finally:
        await default.aclose()
        await blocking.aclose()


def test_create_redis_needs_a_host() -> None:
    """PAY-607: a service without Redis (bank-stub) can't build a client by accident."""
    with pytest.raises(ValueError, match="REDIS_HOST"):
        create_redis(make_settings(redis_host=None))
