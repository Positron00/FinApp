"""No secrets or sensitive data in telemetry across a full run (design §5; PAY-NFR-07).

One run exercises every worker log path that touches a payout: settled, rejected, a bank
no-answer and its redelivery, poison with and without readable IDs (DLQ). Everything captured —
every log line of the in-process stack, every span (names, attributes, events, status) and every
metric label — must hold none of: the routing number, the account numbers, the amounts, the
bank API key, or the JWTs the worker minted.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from pycommon.db import new_id
from pycommon.testing import JsonLogCapture, OtelMemory

from tests.helpers import API_KEY, envelope_dict
from tests.integration.conftest import DLQ, STREAM, VALID_ROUTING, Stack, wait_for

ACCOUNTS = ("000987654321", "000555550000")  # settled; …0000 is a definite rejection


class Recording(httpx.AsyncBaseTransport):
    """Forwards to ``inner``; keeps the Authorization headers; the first ``fail`` POSTs carrying
    the first Idempotency-Key seen get 503 (one payout's first delivery has no answer)."""

    def __init__(self, inner: httpx.AsyncBaseTransport, *, fail: int = 0) -> None:
        self.inner = inner
        self.fail = fail
        self.auth: set[str] = set()
        self.failing_key: str | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if "authorization" in request.headers:
            self.auth.add(request.headers["authorization"].removeprefix("Bearer "))
        key = request.headers.get("idempotency-key")
        if request.method == "POST" and key is not None and self.fail > 0:
            self.failing_key = self.failing_key or key
            if key == self.failing_key:
                self.fail -= 1
                return httpx.Response(503)
        return await self.inner.handle_async_request(request)


def _span_dump(otel: OtelMemory) -> str:
    out: list[dict[str, Any]] = [
        {
            "name": s.name,
            "attributes": dict(s.attributes or {}),
            "events": [(e.name, dict(e.attributes or {})) for e in s.events],
            "status": s.status.description,
        }
        for s in otel.spans()
    ]
    return json.dumps(out, default=str)


def _metric_labels(otel: OtelMemory) -> str:
    labels = [attrs for name in otel.metric_names() for attrs, _ in otel.metric_points(name)]
    return json.dumps(labels, default=str)


async def test_full_flow_leaks_nothing(
    otel_memory: OtelMemory, caplog_json: JsonLogCapture, stack: Stack
) -> None:
    """PAY-NFR-07: logs, spans and metric labels of a full run with settled, rejected, a bank
    no-answer then redelivery, and two poison entries carry no routing or account number, no
    amount, no API key and no worker JWT; the DLQ entries carry no bank details or tokens."""
    payroll = Recording(httpx.ASGITransport(app=stack.payroll_app, raise_app_exceptions=False))
    # The first create_transfer gets 503 on every try (1 + BANK_MAX_RETRIES): no answer, retry.
    bank = Recording(httpx.ASGITransport(app=stack.bank_app, raise_app_exceptions=False), fail=4)
    await stack.worker(instrument=True, payroll_transport=payroll, bank_transport=bank)
    run = await stack.approved_run(list(ACCOUNTS))
    unknown = envelope_dict(event_id=new_id(), payout_id=str(new_id()))
    await stack.redis.xadd(
        STREAM,
        {
            "event_id": unknown["event_id"],
            "type": "payout.requested",
            "envelope": json.dumps(unknown),
        },
    )
    await stack.redis.xadd(STREAM, {"event_id": "nope", "type": "payout.requested"})

    async def final() -> bool:
        return all(p["status"] != "requested" for p in await stack.payouts(run["id"]))

    await wait_for(final, within=30)

    async def drained() -> bool:
        return await stack.pending() == 0 and int(await stack.redis.xlen(DLQ)) == 2

    await wait_for(drained)
    assert sorted(p["status"] for p in await stack.payouts(run["id"])) == ["failed", "paid"]
    assert caplog_json.events("bank.transfer.no_answer")  # the no-answer path ran
    assert caplog_json.events("payout.poison_reported") == []  # unknown payout: no report
    assert payroll.auth  # the worker's JWTs were seen on the wire

    amounts = {
        str(json.loads(fields[b"envelope"])["data"]["amount_cents"])
        for _, fields in await stack.entries()
        if b"envelope" in fields
    }
    assert len(amounts) >= 2
    secrets = {VALID_ROUTING, *ACCOUNTS, API_KEY, *payroll.auth}
    logs = caplog_json.text
    spans = _span_dump(otel_memory)
    labels = _metric_labels(otel_memory)
    assert "payout.requested process" in spans
    for secret in secrets | amounts:
        assert secret not in logs, "secret or amount in a log line"
        assert secret not in spans, "secret or amount in a span"
        assert secret not in labels, "secret or amount in a metric label"
    dlq = json.dumps(await stack.dlq())
    for secret in secrets:
        assert secret not in dlq, "secret in a DLQ entry"
