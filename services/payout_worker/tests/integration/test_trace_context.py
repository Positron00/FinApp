"""Trace context across the stream (design §5.3; PAY-NFR-09, PAY-NFR-07)."""

from __future__ import annotations

import json

from opentelemetry.trace import SpanKind
from pycommon.testing import OtelMemory

from tests.helpers import ACCOUNT, API_KEY, ROUTING
from tests.integration.conftest import Stack, wait_for


async def test_consume_span_is_a_child_of_the_envelope(
    otel_memory: OtelMemory, stack: Stack
) -> None:
    """PAY-NFR-09: the consume span's parent is the envelope traceparent's span and trace; the
    children and the per-try payroll and bank client spans share the trace; no span holds an
    amount, routing, account or the API key."""
    await stack.worker(instrument=True)
    run = await stack.approved_run([ACCOUNT])

    async def paid() -> bool:
        return [p["status"] for p in await stack.payouts(run["id"])] == ["paid"]

    await wait_for(paid)
    [(entry_id, fields)] = await stack.entries()
    env = json.loads(fields[b"envelope"])
    _, trace_id, parent_id, _ = env["traceparent"].split("-")

    async def finished() -> bool:
        return any(s.name == "payout.requested process" for s in otel_memory.spans())

    await wait_for(finished)
    spans = otel_memory.spans()
    [consume] = [s for s in spans if s.name == "payout.requested process"]
    assert consume.kind is SpanKind.CONSUMER
    assert consume.parent is not None
    assert f"{consume.parent.span_id:016x}" == parent_id
    assert f"{consume.context.trace_id:032x}" == trace_id
    assert [f"{link.context.span_id:016x}" for link in consume.links] == [parent_id]
    attrs = dict(consume.attributes or {})
    assert attrs["messaging.message.id"] == entry_id.decode()
    assert attrs["payrun.outcome"] == "settled"
    assert attrs["payrun.delivery"] == 1
    names = {s.name for s in spans if s.context.trace_id == consume.context.trace_id}
    assert {
        "payout.instruction",
        "payout.bank_call",
        "payout.report_result",
        "payout.finish",
        "payroll get_instruction",
        "bank create_transfer",
        "payroll post_result",
    } <= names
    # The per-try client spans of the resilient clients are in the same trace. (The httpx
    # instrumentation, which injects traceparent, wraps AsyncHTTPTransport only, so with the
    # in-process ASGITransport used here there are no httpx spans or injected headers.)
    tries = [
        s for s in spans if s.kind is SpanKind.CLIENT and s.name.startswith(("payroll ", "bank "))
    ]
    assert len(tries) == 3
    assert all(s.context.trace_id == consume.context.trace_id for s in tries)
    dump = json.dumps([dict(s.attributes or {}) for s in spans], default=str)
    for secret in (ROUTING, ACCOUNT, API_KEY, str(env["data"]["amount_cents"])):
        assert secret not in dump
