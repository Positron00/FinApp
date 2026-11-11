"""The ``payout.requested`` v1 envelope (design §5.1; PAY-402, C22, C28).

``validate_envelope`` checks a payload against the committed JSON Schema
(``contracts/events/payout.requested.v1.json``; also copied to ``/app/contracts`` in the image).
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any, Final
from uuid import UUID

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from pycommon import Clock
from pycommon.streams import Envelope

__all__ = [
    "EVENT_TYPE",
    "EVENT_VERSION",
    "SCHEMA_RELATIVE_PATH",
    "STREAM",
    "EnvelopeInvalidError",
    "build_payout_requested",
    "schema_path",
    "validate_envelope",
]

EVENT_TYPE: Final = "payout.requested"
EVENT_VERSION: Final = 1
PRODUCER: Final = "payroll"
STREAM: Final = "stream:payout.requested"
CURRENCY: Final = "USD"
SCHEMA_RELATIVE_PATH: Final = Path("contracts/events/payout.requested.v1.json")


class EnvelopeInvalidError(Exception):
    """A built envelope fails the schema: a bug, surfaced as 500 (never a user error)."""


def schema_path() -> Path:
    """The schema next to the nearest ancestor ``contracts/`` (repo root or ``/app``)."""
    for base in (*Path(__file__).resolve().parents, Path.cwd()):
        candidate = base / SCHEMA_RELATIVE_PATH
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(str(SCHEMA_RELATIVE_PATH))


@cache
def _validator() -> Draft202012Validator:
    schema = json.loads(schema_path().read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)


def validate_envelope(payload: dict[str, Any]) -> None:
    errors = sorted(_validator().iter_errors(payload), key=lambda e: list(e.path))
    if errors:
        where = ".".join(str(p) for p in errors[0].absolute_path) or "<root>"
        raise EnvelopeInvalidError(f"payout.requested envelope invalid at {where}")


def build_payout_requested(
    *,
    company_id: UUID,
    payout_id: UUID,
    pay_run_id: UUID,
    attempt: int,
    amount_cents: int,
    republish_seq: int,
    clock: Clock | None = None,
) -> Envelope:
    """A fresh envelope: UUIDv7 ``event_id``; ``traceparent`` of the active span, or synthesised."""
    return Envelope.new(
        type=EVENT_TYPE,
        version=EVENT_VERSION,
        producer=PRODUCER,
        clock=clock,
        data={
            "payout_id": str(payout_id),
            "pay_run_id": str(pay_run_id),
            "company_id": str(company_id),
            "attempt": attempt,
            "amount_cents": amount_cents,
            "currency": CURRENCY,
            "republish_seq": republish_seq,
        },
    )
