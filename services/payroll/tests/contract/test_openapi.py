"""The committed OpenAPI contract (design §9.3 ``test_openapi_snapshot.py``; PAY-NFR-12)."""

from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any

import pytest
from payroll.main import build_app
from payroll.settings import Settings
from pycommon.app import build_openapi

OPENAPI = Path(__file__).resolve().parents[2] / "openapi.json"
# (method, path) → (request model or None, 2xx status, 2xx model)
EXPECTED: dict[tuple[str, str], tuple[str | None, str, str]] = {
    ("get", "/v1/company/settings"): (None, "200", "CompanySettingsOut"),
    ("put", "/v1/company/settings"): ("CompanySettingsIn", "200", "CompanySettingsOut"),
    ("get", "/v1/employees"): (None, "200", "Page_EmployeeOut_"),
    ("post", "/v1/employees"): ("EmployeeCreate", "201", "EmployeeOut"),
    ("get", "/v1/employees/{employee_id}"): (None, "200", "EmployeeOut"),
    ("patch", "/v1/employees/{employee_id}"): ("EmployeeUpdate", "200", "EmployeeOut"),
    ("get", "/v1/audit-events"): (None, "200", "Page_AuditEventOut_"),
    ("get", "/v1/pay-runs"): (None, "200", "Page_PayRunOut_"),
    ("post", "/v1/pay-runs"): ("PayRunCreate", "201", "PayRunOut"),
    ("get", "/v1/pay-runs/{run_id}"): (None, "200", "PayRunOut"),
    ("post", "/v1/pay-runs/{run_id}/calculate"): (None, "200", "PayRunOut"),
    ("post", "/v1/pay-runs/{run_id}/approve"): (None, "200", "PayRunOut"),
    ("get", "/v1/pay-runs/{run_id}/payslips"): (None, "200", "Page_PayslipOut_"),
    ("get", "/v1/pay-runs/{run_id}/payslips/{payslip_id}"): (None, "200", "PayslipOut"),
    ("get", "/v1/pay-runs/{run_id}/payouts"): (None, "200", "Page_PayoutOut_"),
    ("get", "/v1/internal/payouts/{payout_id}/instruction"): (None, "200", "InstructionOut"),
    ("post", "/v1/internal/payouts/{payout_id}/result"): ("ResultIn", "200", "ResultOut"),
}
HEALTH = {"/livez", "/readyz"}


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    build = functools.partial(build_app, instrument=False)
    out: dict[str, Any] = build_openapi(build, Settings.model_construct())
    return out


def test_committed_spec_is_current(spec: dict[str, Any]) -> None:
    """PAY-NFR-12: openapi.json equals what scripts/export_openapi.py would write."""
    assert OPENAPI.read_text() == json.dumps(spec, sort_keys=True, indent=2) + "\n"


def test_routes_models_and_error_responses(spec: dict[str, Any]) -> None:
    """PAY-NFR-12, PAY-602, PAY-203: exactly the S5 and F2 routes, each with its models, and
    every error status documented with the shared ErrorBody schema."""
    paths = {p: ops for p, ops in spec["paths"].items() if p not in HEALTH}
    assert {(m, p) for p, ops in paths.items() for m in ops} == set(EXPECTED)
    for (method, path), (body, ok, out) in EXPECTED.items():
        op = paths[path][method]
        if body is not None:
            ref = op["requestBody"]["content"]["application/json"]["schema"]["$ref"]
            assert ref.endswith(f"/{body}"), (method, path)
        ok_ref = op["responses"][ok]["content"]["application/json"]["schema"]["$ref"]
        assert ok_ref.endswith(f"/{out}"), (method, path)
        errors = {s for s in op["responses"] if s.startswith(("4", "5"))}
        assert {"401", "403", "422", "429", "500", "503"} <= errors, (method, path)
        for status in errors:
            schema = op["responses"][status]["content"]["application/json"]["schema"]
            assert schema["$ref"].endswith("/ErrorBody"), (method, path, status)


def test_idempotency_key_header_is_documented(spec: dict[str, Any]) -> None:
    """PAY-400, PAY-405, C34: approve and result take the Idempotency-Key header with its
    pattern, and document 400 for a missing key."""
    for path in ("/v1/pay-runs/{run_id}/approve", "/v1/internal/payouts/{payout_id}/result"):
        op = spec["paths"][path]["post"]
        [header] = [p for p in op["parameters"] if p["in"] == "header"]
        assert header["name"] == "Idempotency-Key"
        assert json.dumps(header["schema"]).count("A-Za-z0-9._:-") == 1
        assert "400" in op["responses"]


def test_no_bank_numbers_in_public_responses(spec: dict[str, Any]) -> None:
    """PAY-202, PAY-501, PAY-NFR-08: only InstructionOut (worker-only) carries routing/account."""
    schemas = spec["components"]["schemas"]
    for name, schema in schemas.items():
        props = set(schema.get("properties", {}))
        if name.endswith("Out") and name != "InstructionOut":
            assert not props & {"routing", "account", "bank_routing", "bank_account"}, name
