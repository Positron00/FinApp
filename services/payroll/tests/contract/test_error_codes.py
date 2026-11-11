"""Error codes (design §4.1, §9.3 ``test_error_codes.py``; PAY-602, R3)."""

from __future__ import annotations

import re
from pathlib import Path

import payroll
from payroll import domain
from payroll.api.errors import ErrorCode, IdempotencyKeyRequired, IdempotencyKeyReused

SRC = Path(payroll.__file__).parent
# The full list of design §4.1, in order.
DESIGN_CODES = [
    "invalid_token", "forbidden_role", "not_found", "method_not_allowed", "payload_too_large",
    "validation_error", "rate_limited", "internal_error", "db_timeout", "db_unavailable",
    "company_settings_not_found", "anchor_locked", "employee_not_found", "employee_email_taken",
    "pay_run_not_found", "pay_run_exists", "invalid_period_start", "no_employees",
    "employee_in_overlapping_run", "pay_run_locked", "not_hourly", "employee_not_in_run",
    "hours_missing", "pay_run_not_approvable", "approver_must_differ", "idempotency_key_required",
    "idempotency_key_reused", "payslip_not_found", "payout_not_found", "payout_not_retryable",
    "payout_not_blocked", "retry_limit_reached", "employee_bank_details_missing",
    "attempt_not_current", "resolve_too_early",
]  # fmt: skip


def test_error_code_enum_is_the_design_list() -> None:
    """PAY-602, R3: ErrorCode holds exactly the design §4.1 codes."""
    assert [c.value for c in ErrorCode] == DESIGN_CODES


def test_every_raised_code_is_in_the_list() -> None:
    """PAY-602: every DomainError and AppError code, and every NotFound code in the source,
    is in ErrorCode, so no route can return a code outside the list."""
    known = {c.value for c in ErrorCode}
    classes = [getattr(domain, n) for n in domain.__all__ if isinstance(getattr(domain, n), type)]
    for cls in classes:
        if issubclass(cls, domain.DomainError) and cls not in (domain.DomainError, domain.NotFound):
            assert cls.code in known, cls.__name__
    assert {IdempotencyKeyRequired.code, IdempotencyKeyReused.code} <= known
    used = set()
    for path in SRC.rglob("*.py"):
        used |= set(re.findall(r'NotFound\("([a-z_]+)"\)', path.read_text()))
    assert used
    assert used <= known
