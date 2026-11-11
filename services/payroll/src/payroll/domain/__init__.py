"""Domain errors (design §3.7). The domain layer imports only the standard library.

Each error carries its HTTP status, its error code and the ``details`` object of §3.7;
``payroll.api.errors`` turns them into the PAY-602 error body.
"""

# ruff: noqa: N818  (the class names are the design's contract)

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, ClassVar
from uuid import UUID

__all__ = [
    "AnchorLocked",
    "ApproverMustDiffer",
    "AttemptNotCurrent",
    "DomainError",
    "EmployeeBankDetailsMissing",
    "EmployeeEmailTaken",
    "EmployeeInOverlappingRun",
    "EmployeeNotInRun",
    "HoursMissing",
    "HoursOutOfBounds",
    "InvalidCursor",
    "InvalidPeriodStart",
    "NoEmployees",
    "NotFound",
    "NotHourly",
    "PayRunExists",
    "PayRunLocked",
    "PayRunNotApprovable",
]


def _ids(ids: Iterable[UUID]) -> list[str]:
    return [str(i) for i in sorted(set(ids))]


class DomainError(Exception):
    status: ClassVar[int] = 409
    code: ClassVar[str] = "conflict"
    message: ClassVar[str] = "The request conflicts with the current state."

    def __init__(self) -> None:
        super().__init__(self.code)

    @property
    def error_code(self) -> str:
        return self.code

    @property
    def details(self) -> Mapping[str, Any] | Sequence[Mapping[str, Any]] | None:
        return None

    @property
    def headers(self) -> Mapping[str, str]:
        return {}


class _EmployeeIdsError(DomainError):
    def __init__(self, employee_ids: Iterable[UUID]) -> None:
        super().__init__()
        self.employee_ids: list[UUID] = sorted(set(employee_ids))

    @property
    def details(self) -> Mapping[str, Any]:
        return {"employee_ids": _ids(self.employee_ids)}


class InvalidPeriodStart(DomainError):
    status = 422
    code = "invalid_period_start"
    message = "The period start date is not valid for this schedule."

    def __init__(self, schedule: str, period_start: str) -> None:
        super().__init__()
        self.schedule = schedule
        self.period_start = period_start

    @property
    def details(self) -> Mapping[str, Any]:
        return {"schedule": self.schedule, "period_start": self.period_start}


class PayRunExists(DomainError):
    code = "pay_run_exists"
    message = "A pay run already exists for this schedule and period."

    def __init__(self, pay_run_id: UUID | None) -> None:
        super().__init__()
        self.pay_run_id = pay_run_id

    @property
    def details(self) -> Mapping[str, Any]:
        return {"pay_run_id": str(self.pay_run_id) if self.pay_run_id else None}


class NoEmployees(DomainError):
    code = "no_employees"
    message = "No active employees are on this schedule."


class EmployeeInOverlappingRun(_EmployeeIdsError):
    code = "employee_in_overlapping_run"
    message = "Some employees are already in a pay run that overlaps this period."


class PayRunLocked(DomainError):
    code = "pay_run_locked"
    message = "The pay run can no longer be changed."

    def __init__(self, status: str) -> None:
        super().__init__()
        self.run_status = status

    @property
    def details(self) -> Mapping[str, Any]:
        return {"status": self.run_status}


class NotHourly(_EmployeeIdsError):
    status = 422
    code = "not_hourly"
    message = "Hours can only be entered for hourly employees."


class EmployeeNotInRun(_EmployeeIdsError):
    status = 422
    code = "employee_not_in_run"
    message = "Some employees are not in this pay run."


class HoursOutOfBounds(DomainError):
    status = 422
    code = "validation_error"
    message = "The request failed validation."

    def __init__(self, indexes: Iterable[int]) -> None:
        super().__init__()
        self.indexes = sorted(set(indexes))

    @property
    def details(self) -> Sequence[Mapping[str, Any]]:
        return [
            {"field": f"body.entries.{i}.hours_hundredths", "issue": "out_of_range"}
            for i in self.indexes
        ]


class InvalidCursor(DomainError):
    """An undecodable cursor, a wrong ``v``, or an ``after`` ID not in scope (§4.9, C14)."""

    status = 422
    code = "validation_error"
    message = "The request failed validation."

    @property
    def details(self) -> Sequence[Mapping[str, Any]]:
        return [{"field": "query.cursor", "issue": "invalid_cursor"}]


class HoursMissing(_EmployeeIdsError):
    status = 422
    code = "hours_missing"
    message = "Some hourly employees have no hours entered."


class PayRunNotApprovable(DomainError):
    code = "pay_run_not_approvable"
    message = "Only a calculated pay run can be approved."

    def __init__(self, status: str) -> None:
        super().__init__()
        self.run_status = status

    @property
    def details(self) -> Mapping[str, Any]:
        return {"status": self.run_status}


class ApproverMustDiffer(DomainError):
    status = 403
    code = "approver_must_differ"
    message = "Dual approval requires a different approver."

    def __init__(self, rule: str, employee_ids: Iterable[UUID]) -> None:
        super().__init__()
        self.rule = rule
        self.employee_ids: list[UUID] = sorted(set(employee_ids))

    @property
    def details(self) -> Mapping[str, Any]:
        return {"rule": self.rule, "employee_ids": _ids(self.employee_ids)}


class EmployeeBankDetailsMissing(_EmployeeIdsError):
    code = "employee_bank_details_missing"
    message = "Some employees have no bank details."


class AttemptNotCurrent(DomainError):
    code = "attempt_not_current"
    message = "The attempt is not the payout's current deliverable attempt."

    def __init__(self, current_attempt: int) -> None:
        super().__init__()
        self.current_attempt = current_attempt

    @property
    def details(self) -> Mapping[str, Any]:
        return {"current_attempt": self.current_attempt}


class AnchorLocked(DomainError):
    code = "anchor_locked"
    message = "The biweekly anchor can't change while a biweekly pay run exists."


class EmployeeEmailTaken(DomainError):
    code = "employee_email_taken"
    message = "An employee with this email already exists."


class NotFound(DomainError):
    """404 with a resource-specific code (``employee_not_found``, ``pay_run_not_found``, …)."""

    status = 404
    code = "not_found"
    message = "Not found."

    def __init__(self, code: str) -> None:
        super().__init__()
        self._code = code

    @property
    def error_code(self) -> str:
        return self._code
