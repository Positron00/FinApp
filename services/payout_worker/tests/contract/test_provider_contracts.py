"""The worker's provider models against payroll's and bank-stub's OpenAPI (design §6; PAY-405,
PAY-411, PAY-412, R6, R7, C26)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from payout_worker.models import (
    Instruction,
    ResultRequest,
    ResultResponse,
    TransferRequest,
    TransferResponse,
)

SERVICES = Path(__file__).resolve().parents[3]
PAYROLL = json.loads((SERVICES / "payroll" / "openapi.json").read_text())
BANK = json.loads((SERVICES / "bank_stub" / "openapi.json").read_text())


def _schema(spec: dict[str, Any], ref: dict[str, Any]) -> dict[str, Any]:
    name = ref["$ref"].rsplit("/", 1)[1]
    out: dict[str, Any] = spec["components"]["schemas"][name]
    return out


def _json_schema(op: dict[str, Any], status: str) -> dict[str, Any]:
    out: dict[str, Any] = op["responses"][status]["content"]["application/json"]["schema"]
    return out


def _enum(prop: dict[str, Any]) -> set[str]:
    if "enum" in prop:
        return set(prop["enum"])
    return {v for alt in prop.get("anyOf", []) for v in alt.get("enum", [])}


def test_instruction_matches_payroll() -> None:
    """PAY-411: the worker reads every field payroll's InstructionOut declares as required."""
    op = PAYROLL["paths"]["/v1/internal/payouts/{payout_id}/instruction"]["get"]
    schema = _schema(PAYROLL, _json_schema(op, "200"))
    assert set(schema["required"]) == set(Instruction.model_fields)
    assert {p["name"] for p in op["parameters"] if p["in"] == "query"} == {"attempt"}


def test_result_request_and_response_match_payroll() -> None:
    """PAY-405, C26: ResultIn has exactly the worker's fields and outcomes; bank_reference ≤ 64;
    ResultOut's outcome takes all five attempt statuses."""
    op = PAYROLL["paths"]["/v1/internal/payouts/{payout_id}/result"]["post"]
    req = _schema(PAYROLL, op["requestBody"]["content"]["application/json"]["schema"])
    assert set(req["properties"]) == set(ResultRequest.model_fields)
    assert _enum(req["properties"]["outcome"]) == {"settled", "rejected", "indeterminate", "poison"}
    ref = req["properties"]["bank_reference"]
    assert any(alt.get("maxLength") == 64 for alt in ref.get("anyOf", [ref]))
    out = _schema(PAYROLL, _json_schema(op, "200"))
    assert set(out["properties"]) == set(ResultResponse.model_fields)
    assert _enum(out["properties"]["outcome"]) == {
        "requested",
        "indeterminate",
        "needs_operator",
        "settled",
        "rejected",
    }
    assert any(p["name"] == "Idempotency-Key" for p in op["parameters"])


def test_bank_transfer_matches_bank_stub() -> None:
    """PAY-412, R6: the POST body, and the 201/200/GET bodies {transfer_id, status, reason,
    bank_reference}."""
    post = BANK["paths"]["/v1/transfers"]["post"]
    body = post["requestBody"]["content"]["application/json"]["schema"]
    assert set(body["properties"]) == set(TransferRequest.model_fields)
    for status in ("201", "200"):
        resp = _schema(BANK, _json_schema(post, status))
        assert set(resp["properties"]) == set(TransferResponse.model_fields)
    get = BANK["paths"]["/v1/transfers/{key}"]["get"]
    resp = _schema(BANK, _json_schema(get, "200"))
    assert set(resp["properties"]) == set(TransferResponse.model_fields)
    assert {p["name"] for p in post["parameters"]} == {"Idempotency-Key", "X-Api-Key"}
