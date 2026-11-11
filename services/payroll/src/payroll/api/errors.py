"""Error codes and the ``DomainError`` → PAY-602 body mapping (design §3.7, §4.1)."""

# ruff: noqa: N818  (exception names are the design's error names)

from __future__ import annotations

import logging
from enum import StrEnum

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pycommon.errors import AppError, error_response
from pycommon.logging import get_logger, log_event

from payroll.domain import DomainError

__all__ = [
    "ErrorCode",
    "IdempotencyKeyRequired",
    "IdempotencyKeyReused",
    "domain_error_response",
    "install_domain_handler",
]

_log = get_logger("payroll.api")


class ErrorCode(StrEnum):
    INVALID_TOKEN = "invalid_token"  # noqa: S105  (an error code)
    FORBIDDEN_ROLE = "forbidden_role"
    NOT_FOUND = "not_found"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    VALIDATION_ERROR = "validation_error"
    RATE_LIMITED = "rate_limited"
    INTERNAL_ERROR = "internal_error"
    DB_TIMEOUT = "db_timeout"
    DB_UNAVAILABLE = "db_unavailable"
    COMPANY_SETTINGS_NOT_FOUND = "company_settings_not_found"
    ANCHOR_LOCKED = "anchor_locked"
    EMPLOYEE_NOT_FOUND = "employee_not_found"
    EMPLOYEE_EMAIL_TAKEN = "employee_email_taken"
    PAY_RUN_NOT_FOUND = "pay_run_not_found"
    PAY_RUN_EXISTS = "pay_run_exists"
    INVALID_PERIOD_START = "invalid_period_start"
    NO_EMPLOYEES = "no_employees"
    EMPLOYEE_IN_OVERLAPPING_RUN = "employee_in_overlapping_run"
    PAY_RUN_LOCKED = "pay_run_locked"
    NOT_HOURLY = "not_hourly"
    EMPLOYEE_NOT_IN_RUN = "employee_not_in_run"
    HOURS_MISSING = "hours_missing"
    PAY_RUN_NOT_APPROVABLE = "pay_run_not_approvable"
    APPROVER_MUST_DIFFER = "approver_must_differ"
    IDEMPOTENCY_KEY_REQUIRED = "idempotency_key_required"
    IDEMPOTENCY_KEY_REUSED = "idempotency_key_reused"
    PAYSLIP_NOT_FOUND = "payslip_not_found"
    PAYOUT_NOT_FOUND = "payout_not_found"
    PAYOUT_NOT_RETRYABLE = "payout_not_retryable"
    PAYOUT_NOT_BLOCKED = "payout_not_blocked"
    RETRY_LIMIT_REACHED = "retry_limit_reached"
    EMPLOYEE_BANK_DETAILS_MISSING = "employee_bank_details_missing"
    ATTEMPT_NOT_CURRENT = "attempt_not_current"
    RESOLVE_TOO_EARLY = "resolve_too_early"


class IdempotencyKeyRequired(AppError):
    status = 400
    code = ErrorCode.IDEMPOTENCY_KEY_REQUIRED
    default_message = "The Idempotency-Key header is required."


class IdempotencyKeyReused(AppError):
    status = 422
    code = ErrorCode.IDEMPOTENCY_KEY_REUSED
    default_message = "The Idempotency-Key was already used for a different request."


def domain_error_response(exc: DomainError) -> JSONResponse:
    log_event(_log, logging.INFO, "http.error", code=exc.error_code, status=exc.status)
    return error_response(
        exc.status, exc.error_code, exc.message, details=exc.details, headers=exc.headers
    )


async def _handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, DomainError):  # pragma: no cover  (registered for DomainError only)
        raise exc
    return domain_error_response(exc)


def install_domain_handler(app: FastAPI) -> None:
    app.add_exception_handler(DomainError, _handler)
