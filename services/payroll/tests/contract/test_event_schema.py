"""The event contracts payroll produces (design §9.3; PAY-402, C21, C57)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from payroll.events.envelope import (
    EnvelopeInvalidError,
    build_payout_requested,
    schema_path,
    validate_envelope,
)

CONTRACTS = schema_path().parent
V1_EXAMPLES = sorted((CONTRACTS / "examples").glob("payout.requested.v1.*.json"))
DLQ_EXAMPLES = sorted((CONTRACTS / "examples").glob("payout.requested.dlq.v1.*.json"))


def _load(path: Path) -> Any:
    return json.loads(path.read_text())


def test_both_schemas_are_valid_draft_2020_12() -> None:
    """PAY-NFR-12: the v1 and DLQ schemas are valid draft 2020-12 schemas."""
    for name in ("payout.requested.v1.json", "payout.requested.dlq.v1.json"):
        Draft202012Validator.check_schema(_load(CONTRACTS / name))


def test_every_v1_example_validates() -> None:
    """PAY-402, C57: both lead-owned v1 examples (first publication and a re-publish) and the
    schema's embedded example validate, as does a freshly built envelope."""
    assert {p.name for p in V1_EXAMPLES} >= {
        "payout.requested.v1.first.json",
        "payout.requested.v1.republished.json",
    }
    for path in V1_EXAMPLES:
        validate_envelope(_load(path))
    for example in _load(CONTRACTS / "payout.requested.v1.json")["examples"]:
        validate_envelope(example)
    first = _load(CONTRACTS / "examples" / "payout.requested.v1.first.json")
    built = build_payout_requested(
        company_id=first["data"]["company_id"], payout_id=first["data"]["payout_id"],
        pay_run_id=first["data"]["pay_run_id"], attempt=1, amount_cents=1, republish_seq=0,
    )  # fmt: skip
    validate_envelope(json.loads(built.to_json()))


MUTATIONS: list[tuple[tuple[str, ...], Any]] = [
    (("extra",), 1),
    (("data", "extra"), 1),
    (("version",), 2),
    (("type",), "payout.sent"),
    (("producer",), "payout-worker"),
    (("data", "currency"), "EUR"),
    (("data", "amount_cents"), 0),
    (("data", "attempt"), 0),
    (("data", "republish_seq"), -1),
    (("data", "payout_id"), "not-a-uuid"),
    (("traceparent",), "00-bad"),
    (("occurred_at",), "2026-11-10T14:03:07+01:00"),
]


@pytest.mark.parametrize(("path", "value"), MUTATIONS, ids=str)
def test_mutated_examples_fail(path: tuple[str, ...], value: Any) -> None:
    """PAY-402: the schema rejects each mutation of a valid example."""
    doc = copy.deepcopy(_load(V1_EXAMPLES[0]))
    target = doc
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(EnvelopeInvalidError):
        validate_envelope(doc)


def test_missing_traceparent_fails() -> None:
    """PAY-402, C22: traceparent is required."""
    doc = _load(V1_EXAMPLES[0])
    del doc["traceparent"]
    with pytest.raises(EnvelopeInvalidError):
        validate_envelope(doc)


def test_dlq_examples_validate_and_carry_a_v1_envelope() -> None:
    """C21: every DLQ example validates against the DLQ schema, and its envelope string (when
    the error isn't a schema error) validates against the v1 schema payroll produces."""
    validator = Draft202012Validator(
        _load(CONTRACTS / "payout.requested.dlq.v1.json"),
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )
    assert DLQ_EXAMPLES
    for path in DLQ_EXAMPLES:
        entry = _load(path)
        assert not list(validator.iter_errors(entry)), path.name
        if entry["error_code"] != "schema_invalid":
            validate_envelope(json.loads(entry["envelope"]))
