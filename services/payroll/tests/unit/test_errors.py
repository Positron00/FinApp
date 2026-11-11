"""Error mapping (design §3.7, §4.1; PAY-602, PAY-204, PAY-604)."""

# ruff: noqa: E501  (one case per line in the parameter tables)

from __future__ import annotations

from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from payroll.api.errors import ErrorCode, install_domain_handler
from payroll.crypto import FieldKeyring
from payroll.domain import (
    AnchorLocked,
    ApproverMustDiffer,
    AttemptNotCurrent,
    DomainError,
    EmployeeBankDetailsMissing,
    EmployeeEmailTaken,
    EmployeeInOverlappingRun,
    EmployeeNotInRun,
    HoursMissing,
    HoursOutOfBounds,
    InvalidCursor,
    InvalidPeriodStart,
    NoEmployees,
    NotFound,
    NotHourly,
    PayRunExists,
    PayRunLocked,
    PayRunNotApprovable,
)
from payroll.services.common import DecryptFailed, decrypt_or_500
from pycommon.errors import install_handlers
from pycommon.testing import JsonLogCapture

E1 = uuid4()

DESIGN_CODES = {
    "invalid_token", "forbidden_role", "not_found", "method_not_allowed", "payload_too_large",
    "validation_error", "rate_limited", "internal_error", "db_timeout", "db_unavailable",
    "company_settings_not_found", "anchor_locked", "employee_not_found", "employee_email_taken",
    "pay_run_not_found", "pay_run_exists", "invalid_period_start", "no_employees",
    "employee_in_overlapping_run", "pay_run_locked", "not_hourly", "employee_not_in_run",
    "hours_missing", "pay_run_not_approvable", "approver_must_differ", "idempotency_key_required",
    "idempotency_key_reused", "payslip_not_found", "payout_not_found", "payout_not_retryable",
    "payout_not_blocked", "retry_limit_reached", "employee_bank_details_missing",
    "attempt_not_current", "resolve_too_early",
}  # fmt: skip

CASES: list[tuple[DomainError, int, str, Any]] = [
    (InvalidPeriodStart("monthly", "2026-01-02"), 422, "invalid_period_start",
     {"schedule": "monthly", "period_start": "2026-01-02"}),
    (PayRunExists(E1), 409, "pay_run_exists", {"pay_run_id": str(E1)}),
    (NoEmployees(), 409, "no_employees", None),
    (EmployeeInOverlappingRun([E1]), 409, "employee_in_overlapping_run", {"employee_ids": [str(E1)]}),
    (PayRunLocked("processing"), 409, "pay_run_locked", {"status": "processing"}),
    (NotHourly([E1]), 422, "not_hourly", {"employee_ids": [str(E1)]}),
    (EmployeeNotInRun([E1]), 422, "employee_not_in_run", {"employee_ids": [str(E1)]}),
    (HoursOutOfBounds([3]), 422, "validation_error",
     [{"field": "body.entries.3.hours_hundredths", "issue": "out_of_range"}]),
    (HoursMissing([E1, E1]), 422, "hours_missing", {"employee_ids": [str(E1)]}),
    (PayRunNotApprovable("draft"), 409, "pay_run_not_approvable", {"status": "draft"}),
    (ApproverMustDiffer("calculator", []), 403, "approver_must_differ",
     {"rule": "calculator", "employee_ids": []}),
    (EmployeeBankDetailsMissing([E1]), 409, "employee_bank_details_missing", {"employee_ids": [str(E1)]}),
    (AttemptNotCurrent(2), 409, "attempt_not_current", {"current_attempt": 2}),
    (AnchorLocked(), 409, "anchor_locked", None),
    (EmployeeEmailTaken(), 409, "employee_email_taken", None),
    (NotFound("payout_not_found"), 404, "payout_not_found", None),
    (InvalidCursor(), 422, "validation_error", [{"field": "query.cursor", "issue": "invalid_cursor"}]),
]  # fmt: skip


def test_error_code_enum_equals_the_design_list() -> None:
    """PAY-602: ErrorCode is exactly the §4.1 list."""
    assert {c.value for c in ErrorCode} == DESIGN_CODES


@pytest.mark.parametrize(("exc", "status", "code", "details"), CASES, ids=lambda v: str(v)[:30])
async def test_domain_errors_map_to_status_code_and_details(
    exc: DomainError, status: int, code: str, details: Any
) -> None:
    """PAY-602: each DomainError maps to the §3.7 status, code and details shape."""
    app = FastAPI()
    install_handlers(app)
    install_domain_handler(app)

    @app.get("/boom")
    async def boom() -> None:
        raise exc

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        r = await client.get("/boom")
    assert r.status_code == status
    error = r.json()["error"]
    assert error["code"] == code
    assert error.get("details") == details
    assert error["code"] in DESIGN_CODES


def test_decrypt_failure_logs_and_is_internal_error(caplog_json: JsonLogCapture) -> None:
    """PAY-204: FieldDecryptError → log crypto.decrypt_failed {kid, entity, entity_id} → 500."""
    ring = FieldKeyring({"k1": b"\x01" * 32}, "k1")
    entity_id = uuid4()
    with pytest.raises(DecryptFailed) as err:
        decrypt_or_500(ring, "k1:AAAA:AAAA", aad="x", entity="employee", entity_id=entity_id)
    assert (err.value.status, err.value.code) == (500, "internal_error")
    [event] = caplog_json.events("crypto.decrypt_failed")
    assert (event["kid"], event["entity"], event["entity_id"]) == ("k1", "employee", str(entity_id))
