"""DLQ entries against ``payout.requested.dlq.v1.json`` (design §3.2; PAY-408, PAY-NFR-12, C21,
C57)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from payout_worker.consumer import build_dlq_fields
from payout_worker.handler import error_text
from payout_worker.models import schema_path
from pycommon.testing import FAKE_EPOCH

from tests.helpers import message

DLQ_SCHEMA = schema_path().parent / "payout.requested.dlq.v1.json"
DLQ_EXAMPLES = sorted((schema_path().parent / "examples").glob("payout.requested.dlq.v1.*.json"))
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


def _validator() -> Draft202012Validator:
    schema = json.loads(DLQ_SCHEMA.read_text())
    return Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)


def _decoded(fields: dict[str, bytes | str]) -> dict[str, Any]:
    """As the schema describes: strings, with delivery_count as an integer."""
    out: dict[str, Any] = {
        k: (v.decode() if isinstance(v, bytes) else v) for k, v in fields.items()
    }
    out["delivery_count"] = int(out["delivery_count"])
    return out


@pytest.mark.parametrize(("kind", "code"), CODES)
@pytest.mark.parametrize("drop_envelope", [False, True])
def test_worker_dlq_entries_validate(kind: str, code: str, drop_envelope: bool) -> None:
    """C21: every error_code the worker writes, with and without an envelope field."""
    fields = build_dlq_fields(
        message(drop_envelope=drop_envelope),
        kind=kind,
        code=code,
        error=error_text(RuntimeError, code),
        delivery=5 if kind != "invalid" else 1,
        consumer="payout-worker-7c9d5b6f4-x2k8p",
        now=FAKE_EPOCH,
    )
    _validator().validate(_decoded(fields))


@pytest.mark.parametrize("path", [DLQ_SCHEMA, *DLQ_EXAMPLES], ids=lambda p: Path(p).name)
def test_schema_examples_validate(path: Path) -> None:
    """C57: the schema's own example and the lead-owned example files validate."""
    doc = json.loads(path.read_text())
    for example in doc.get("examples", [doc]):
        _validator().validate(example)
