"""The payout.requested envelope against the committed schema (design §5.1; PAY-402)."""

from __future__ import annotations

import copy
import json
from typing import Any
from uuid import UUID, uuid4

import pytest
from opentelemetry.sdk.trace import TracerProvider
from payroll.events.envelope import (
    EnvelopeInvalidError,
    build_payout_requested,
    schema_path,
    validate_envelope,
)

from tests.helpers import COMPANY_ID


def _payload(**kw: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "company_id": COMPANY_ID,
        "payout_id": uuid4(),
        "pay_run_id": uuid4(),
        "attempt": 1,
        "amount_cents": 412_345,
        "republish_seq": 0,
    }
    args.update(kw)
    payload: dict[str, Any] = json.loads(build_payout_requested(**args).to_json())
    return payload


def test_built_envelope_validates() -> None:
    """PAY-402: the built envelope validates; event_id is a UUIDv7; occurred_at has ms and Z."""
    p = _payload()
    validate_envelope(p)
    assert UUID(p["event_id"]).version == 7
    assert p["occurred_at"].endswith("Z")
    assert len(p["occurred_at"]) == 24
    assert p["data"]["currency"] == "USD"
    assert p["data"]["company_id"] == str(COMPANY_ID)


def test_committed_example_validates() -> None:
    """PAY-402, C57: the lead-owned example validates with the same validator."""
    example = schema_path().parent / "examples" / "payout.requested.v1.first.json"
    validate_envelope(json.loads(example.read_text()))


def test_traceparent_synthesised_without_a_span() -> None:
    """PAY-402: with no active span the traceparent is a fresh, valid, unsampled context."""
    tp = _payload()["traceparent"]
    assert tp.startswith("00-")
    assert tp.endswith("-00")


def test_traceparent_from_the_active_span() -> None:
    """PAY-402, PAY-NFR-09: with an active span the traceparent continues its trace."""
    tracer = TracerProvider().get_tracer("t")
    with tracer.start_as_current_span("approve") as span:
        tp = _payload()["traceparent"]
    ctx = span.get_span_context()
    assert tp.startswith(f"00-{ctx.trace_id:032x}-{ctx.span_id:016x}-")


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("extra",), 1),
        (("data", "extra"), 1),
        (("data", "currency"), "EUR"),
        (("data", "amount_cents"), 0),
        (("data", "republish_seq"), -1),
        (("version",), 2),
        (("producer",), "web"),
        (("traceparent",), "nope"),
    ],
)
def test_schema_rejects_mutations(path: tuple[str, ...], value: Any) -> None:
    """PAY-402: extra properties, a wrong currency, amount 0 and a negative seq are rejected."""
    p = copy.deepcopy(_payload())
    target = p
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(EnvelopeInvalidError):
        validate_envelope(p)
