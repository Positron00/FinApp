"""Consumed event examples against the schema and the worker's models (design §6; PAY-402,
PAY-NFR-12, C28, C57)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from payout_worker.errors import InvalidMessage
from payout_worker.models import Envelope, parse_envelope, schema_path

from tests.helpers import COMPANY_ID, message

EXAMPLES = sorted(
    (schema_path().parents[0] / "examples").glob("payout.requested.v1.*.json"),
    key=lambda p: p.name,
)


def _schema() -> dict[str, Any]:
    out: dict[str, Any] = json.loads(schema_path().read_text())
    return out


def test_examples_exist() -> None:
    """C57: the lead-owned examples are present."""
    assert {p.name for p in EXAMPLES} >= {
        "payout.requested.v1.first.json",
        "payout.requested.v1.republished.json",
    }


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_example_validates_and_parses(path: Path) -> None:
    """PAY-402, C28: each example validates and parses into Envelope via the stream fields."""
    doc = json.loads(path.read_text())
    Draft202012Validator(_schema()).validate(doc)
    env = parse_envelope(message(doc))
    assert isinstance(env, Envelope)
    assert env.data.company_id == COMPANY_ID


def test_schema_embedded_examples_parse() -> None:
    """PAY-402: the schema's own examples parse too."""
    for doc in _schema()["examples"]:
        parse_envelope(message(doc))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.pop("traceparent"),
        lambda d: d["data"].update(amount_cents=0),
        lambda d: d["data"].update(account="123456789"),
        lambda d: d.update(event_id="not-a-uuid"),
    ],
)
def test_invalid_examples_fail_both(mutate: Any) -> None:
    """PAY-402: invalid variants fail the schema and the worker's parser alike."""
    doc = json.loads(EXAMPLES[0].read_text())
    mutate(doc)
    assert list(Draft202012Validator(_schema()).iter_errors(doc))
    with pytest.raises(InvalidMessage):
        parse_envelope(message(doc))


def test_model_matches_schema_fields() -> None:
    """PAY-402: the Pydantic models carry exactly the schema's properties."""
    schema = _schema()
    assert set(Envelope.model_fields) == set(schema["properties"])
    data_fields = set(Envelope.model_fields["data"].annotation.model_fields)  # type: ignore[union-attr]
    assert data_fields == set(schema["properties"]["data"]["properties"])
