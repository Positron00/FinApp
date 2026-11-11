"""Spans and redaction (design §5.3; PAY-NFR-09, PAY-NFR-07)."""

from __future__ import annotations

import json

from opentelemetry.trace import SpanKind, StatusCode
from pycommon.testing import JsonLogCapture, OtelMemory

from tests.helpers import ACCOUNT, AMOUNT, API_KEY, KEY, ROUTING, body, headers
from tests.integration.conftest import Harness

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT_ID = "00f067aa0ba902b7"
TRACEPARENT = f"00-{TRACE_ID}-{PARENT_ID}-01"


def _dump(otel: OtelMemory) -> str:
    return json.dumps(
        [
            {
                "name": s.name,
                "attributes": dict(s.attributes or {}),
                "events": [dict(e.attributes or {}) for e in s.events],
            }
            for s in otel.spans()
        ],
        default=str,
    )


async def test_server_span_joins_caller_trace(harness: Harness, otel_memory: OtelMemory) -> None:
    """PAY-NFR-09: the server span's parent is the caller's span; bank_stub.transfer is its
    child with the §5.3 attributes."""
    async with harness(instrument=True) as (_, client):
        h = {**headers(), "traceparent": TRACEPARENT}
        assert (await client.post("/v1/transfers", json=body(), headers=h)).status_code == 201
        assert (await client.post("/v1/transfers", json=body(), headers=h)).status_code == 200
        h_get = {**headers(None), "traceparent": TRACEPARENT}
        assert (await client.get("/v1/transfers/none:1", headers=h_get)).status_code == 404
    spans = otel_memory.spans()
    servers = [s for s in spans if s.kind is SpanKind.SERVER]
    assert servers
    for s in servers:
        assert s.parent is not None
        assert f"{s.parent.span_id:016x}" == PARENT_ID
        assert f"{s.context.trace_id:032x}" == TRACE_ID
    transfers = [s for s in spans if s.name == "bank_stub.transfer"]
    results = [t.attributes["payrun.bank_result"] for t in transfers if t.attributes]
    assert results == ["settled", "replayed", "not_found"]
    created = transfers[0]
    assert created.attributes is not None
    assert created.attributes["payrun.bank_operation"] == "create"
    assert created.attributes["payrun.idempotency_key"] == KEY
    assert "payrun.transfer_id" in created.attributes
    server_ids = {s.context.span_id for s in servers}
    assert all(t.parent is not None and t.parent.span_id in server_ids for t in transfers)
    # DB spans under bank_stub.transfer aren't asserted: opentelemetry-instrumentation-sqlalchemy
    # 0.66b1 refuses SQLAlchemy 2.1 ("instruments < 2.1.0"), so pycommon emits none (lead issue).
    assert all(t.status.status_code is not StatusCode.ERROR for t in transfers)


async def test_no_secrets_in_spans_or_logs(
    harness: Harness, otel_memory: OtelMemory, caplog_json: JsonLogCapture
) -> None:
    """PAY-NFR-07: spans and logs for POST/GET/401/422 carry no API key, routing, account,
    last 4 or amount."""
    async with harness(instrument=True) as (_, client):
        await client.post("/v1/transfers", json=body(), headers=headers())
        await client.get(f"/v1/transfers/{KEY}", headers=headers(None))
        await client.post("/v1/transfers", json=body(), headers=headers(api_key="x" * 20))
        bad = body(account="98", routing=ROUTING)
        await client.post("/v1/transfers", json=bad, headers=headers(key="v:1"))
    text = _dump(otel_memory) + caplog_json.text
    for secret in (API_KEY, ROUTING, ACCOUNT, str(AMOUNT), "6789", "x" * 20):
        assert secret not in text
    assert caplog_json.events("bank_stub.transfer.created")
    assert caplog_json.events("bank_stub.auth.rejected")
