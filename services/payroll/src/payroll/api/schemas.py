"""Request and response models (design §4).

Request models forbid extra keys; response models read ORM attributes.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from payroll.domain.validation import aba_checksum_ok, is_monday, is_valid_timezone

__all__ = [
    "Account",
    "AttemptOut",
    "AuditEventName",
    "AuditEventOut",
    "CompanySettingsIn",
    "CompanySettingsOut",
    "EmployeeCreate",
    "EmployeeOut",
    "EmployeeUpdate",
    "EntityType",
    "HoursEntry",
    "HoursIn",
    "HoursOut",
    "IdemKey",
    "InstructionOut",
    "Page",
    "PayRunCreate",
    "PayRunOut",
    "PayoutCounts",
    "PayoutOut",
    "PayslipOut",
    "ResolveIn",
    "ResultIn",
    "ResultOut",
    "Routing",
    "RunTotalsOut",
]

IDEM_KEY_PATTERN = r"^[A-Za-z0-9._:-]{1,200}$"


def _no_nul(v: str) -> str:
    """Postgres ``text`` can't hold U+0000: reject it as a 422, never a 500 (PAY-602, C63)."""
    if "\x00" in v:
        raise PydanticCustomError("nul_character", "The value must not contain a NUL character.")
    return v


NoNul = AfterValidator(_no_nul)
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200), NoNul]
Email = Annotated[EmailStr, StringConstraints(max_length=320)]
SalaryCents = Annotated[int, Field(ge=1, le=1_000_000_000)]
RateCents = Annotated[int, Field(ge=1, le=100_000)]
Routing = Annotated[str, StringConstraints(pattern=r"^[0-9]{9}$")]
Account = Annotated[str, StringConstraints(pattern=r"^[0-9]{4,17}$")]
HoursHundredths = Annotated[int, Field(ge=0, le=74_400)]
Limit = Annotated[int, Field(ge=1, le=100)]
IdemKey = Annotated[str, StringConstraints(pattern=IDEM_KEY_PATTERN)]
Schedule = Literal["biweekly", "monthly"]
PayType = Literal["salaried", "hourly"]
RunStatusName = Literal["draft", "calculated", "processing", "paid", "partially_failed"]
PayoutStatusName = Literal["requested", "paid", "failed"]
AttemptStatusName = Literal["requested", "indeterminate", "needs_operator", "settled", "rejected"]

_REQUEST = ConfigDict(extra="forbid")
_RESPONSE = ConfigDict(from_attributes=True)


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None


# --- company settings (PAY-200) ----------------------------------------------------------------


class CompanySettingsIn(BaseModel):
    model_config = _REQUEST

    name: Name
    tax_rate_bps: Annotated[int, Field(ge=0, le=5000)]
    retirement_bps: Annotated[int, Field(ge=0, le=2000)]
    timezone: Annotated[str, StringConstraints(min_length=1, max_length=64), NoNul]
    biweekly_anchor: date
    require_dual_approval: bool

    @field_validator("timezone")
    @classmethod
    def _iana(cls, v: str) -> str:
        if not is_valid_timezone(v):
            raise PydanticCustomError("timezone", "Not an IANA time zone.")
        return v

    @field_validator("biweekly_anchor")
    @classmethod
    def _monday(cls, v: date) -> date:
        if not is_monday(v):
            raise PydanticCustomError("not_monday", "The biweekly anchor must be a Monday.")
        return v


class CompanySettingsOut(BaseModel):
    model_config = _RESPONSE

    name: str
    tax_rate_bps: int
    retirement_bps: int
    timezone: str
    biweekly_anchor: date
    require_dual_approval: bool
    updated_at: datetime
    updated_by: str | None


# --- employees (PAY-201, PAY-202, PAY-203) -----------------------------------------------------


class EmployeeCreate(BaseModel):
    model_config = _REQUEST

    name: Name
    email: Email
    pay_type: PayType
    annual_salary_cents: SalaryCents | None = None
    hourly_rate_cents: RateCents | None = None
    schedule: Schedule
    bank_routing: Routing
    bank_account: Account

    @field_validator("bank_routing")
    @classmethod
    def _aba(cls, v: str) -> str:
        if not aba_checksum_ok(v):
            raise PydanticCustomError("aba_checksum", "The routing number checksum is invalid.")
        return v

    @model_validator(mode="after")
    def _pay_basis(self) -> Self:
        if self.pay_type == "salaried":
            if self.annual_salary_cents is None:
                raise PydanticCustomError("salary_required", "annual_salary_cents is required.")
            if self.hourly_rate_cents is not None:
                raise PydanticCustomError("pay_basis", "hourly_rate_cents must be empty.")
        else:
            if self.hourly_rate_cents is None:
                raise PydanticCustomError("rate_required", "hourly_rate_cents is required.")
            if self.annual_salary_cents is not None:
                raise PydanticCustomError("pay_basis", "annual_salary_cents must be empty.")
        return self


class EmployeeUpdate(BaseModel):
    """PATCH body (PAY-203): any PAY-201 field, or ``status``. At least one field; an explicit
    ``null`` is refused (omit the field instead). The merged pay basis is checked by the service
    against the stored row (design §4.3)."""

    model_config = _REQUEST

    name: Name | None = None
    email: Email | None = None
    pay_type: PayType | None = None
    annual_salary_cents: SalaryCents | None = None
    hourly_rate_cents: RateCents | None = None
    schedule: Schedule | None = None
    bank_routing: Routing | None = None
    bank_account: Account | None = None
    status: Literal["active", "inactive"] | None = None

    @field_validator("*")
    @classmethod
    def _not_null(cls, v: Any) -> Any:
        if v is None:  # validators run only on supplied values, so None was an explicit null
            raise PydanticCustomError("null_not_allowed", "Omit the field instead of null.")
        return v

    @field_validator("bank_routing")
    @classmethod
    def _aba(cls, v: str) -> str:
        if not aba_checksum_ok(v):
            raise PydanticCustomError("aba_checksum", "The routing number checksum is invalid.")
        return v

    @model_validator(mode="after")
    def _not_empty(self) -> Self:
        if not self.model_fields_set:
            raise PydanticCustomError("no_fields", "The update has no fields.")
        return self


class EmployeeOut(BaseModel):
    model_config = _RESPONSE

    id: UUID
    name: str
    email: str
    pay_type: PayType
    annual_salary_cents: int | None
    hourly_rate_cents: int | None
    schedule: Schedule
    account_last4: str | None  # never the full numbers (PAY-202, PAY-NFR-08)
    has_bank_details: bool
    bank_updated_at: datetime | None
    bank_updated_by: str | None
    status: Literal["active", "inactive"]
    deactivated_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="before")
    @classmethod
    def _from_row(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return data
        names = (*cls.model_fields, "bank_account_enc")
        values = {n: getattr(data, n) for n in names if hasattr(data, n)}
        values["has_bank_details"] = values.pop("bank_account_enc", None) is not None
        return values


# --- audit events (design §4.5) ----------------------------------------------------------------

EntityType = Literal["company_settings", "employee", "pay_run", "payout"]
AuditEventName = Annotated[str, StringConstraints(pattern=r"^[a-z_]+\.[a-z_]+$", max_length=64)]


class AuditEventOut(BaseModel):
    """C5 ``PayrollAuditEvent``; ``details`` holds IDs, counts, codes and ``account_last4`` only."""

    model_config = _RESPONSE

    id: UUID
    at: datetime  # created_at
    actor_id: str
    event: str
    entity_type: EntityType
    entity_id: UUID
    details: dict[str, Any]

    @model_validator(mode="before")
    @classmethod
    def _from_row(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return data
        return {n: getattr(data, "created_at" if n == "at" else n) for n in cls.model_fields}


# --- pay runs (PAY-300 to PAY-304, PAY-400) ------------------------------------------------


class PayRunCreate(BaseModel):
    model_config = _REQUEST

    schedule: Schedule
    period_start: date


class RunTotalsOut(BaseModel):
    gross_cents: int
    tax_cents: int
    retirement_cents: int
    net_cents: int
    funding_required_cents: int  # == net_cents (PAY-303)


class PayoutCounts(BaseModel):
    """Exactly these six keys (C9); all zero before approval."""

    requested: int = 0
    awaiting_bank: int = 0
    blocked: int = 0
    paid: int = 0
    no_transfer: int = 0
    failed: int = 0


class PayRunOut(BaseModel):
    id: UUID
    schedule: Schedule
    period_start: date
    period_end: date
    pay_date: date
    period_index: int
    periods_per_year: int
    status: RunStatusName
    employee_count: int
    tax_bps: int | None
    retirement_bps: int | None
    require_dual_approval: bool | None
    totals: RunTotalsOut | None
    counts: PayoutCounts
    created_by: str
    created_at: datetime
    calculated_by: str | None
    calculated_by_name: str | None
    calculated_at: datetime | None
    approved_by: str | None
    approved_by_name: str | None
    approved_at: datetime | None
    processing_since: datetime | None
    updated_at: datetime


class HoursEntry(BaseModel):
    model_config = _REQUEST

    employee_id: UUID
    hours_hundredths: HoursHundredths  # the schedule bound (33,600 biweekly) is the domain's


class HoursIn(BaseModel):
    """PUT …/hours body (PAY-301): 1 to 500 entries with unique ``employee_id`` values."""

    model_config = _REQUEST

    entries: Annotated[list[HoursEntry], Field(min_length=1, max_length=500)]

    @field_validator("entries")
    @classmethod
    def _unique(cls, v: list[HoursEntry]) -> list[HoursEntry]:
        if len({e.employee_id for e in v}) != len(v):
            raise PydanticCustomError("duplicate_employee_id", "Each employee_id may appear once.")
        return v


class HoursOut(BaseModel):
    """PUT …/hours response (C7): every hours row now stored for the run, by ``employee_id``."""

    pay_run_id: UUID
    status: Literal["draft"]
    entries: list[HoursEntry]


class PayslipOut(BaseModel):
    model_config = _RESPONSE

    id: UUID
    pay_run_id: UUID
    employee_id: UUID
    employee_name: str
    pay_type: PayType
    annual_salary_cents: int | None
    hourly_rate_cents: int | None
    hours_hundredths: int | None
    status: Literal["pending", "calculated"]
    gross_cents: int | None
    tax_cents: int | None
    retirement_cents: int | None
    net_cents: int | None


class AttemptOut(BaseModel):
    model_config = _RESPONSE

    attempt: int
    status: AttemptStatusName
    failure_reason: str | None
    bank_reference: str | None
    account_last4: str
    requested_at: datetime
    final_at: datetime | None


class PayoutOut(BaseModel):
    id: UUID
    pay_run_id: UUID
    payslip_id: UUID
    employee_id: UUID
    employee_name: str
    amount_cents: int
    currency: Literal["USD"]
    status: PayoutStatusName
    no_transfer: bool
    current_attempt: int
    attempts: list[AttemptOut]  # ascending by attempt; [] when no_transfer (PAY-501)


class ResolveIn(BaseModel):
    """PAY-502, R8: the operator's action and a note (1 to 500 characters; only its length is ever
    stored or logged, PAY-NFR-07)."""

    model_config = _REQUEST

    action: Literal["redrive", "mark_rejected"]
    note: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500), NoNul
    ]


# --- internal (PAY-405, PAY-411) ---------------------------------------------------------------


class InstructionOut(BaseModel):
    payout_id: UUID
    attempt: int
    amount_cents: Annotated[int, Field(ge=1)]
    currency: Literal["USD"]
    routing: str
    account: str
    requested_at: datetime


Reason = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,100}$")]
BankReference = Annotated[str, StringConstraints(min_length=1, max_length=64), NoNul]


class ResultIn(BaseModel):
    model_config = _REQUEST

    attempt: Annotated[int, Field(ge=1, le=32767)]  # payout_attempts.attempt is smallint (C63)
    outcome: Literal["settled", "rejected", "indeterminate", "poison"]
    reason: Reason | None = None
    bank_reference: BankReference | None = None

    @model_validator(mode="after")
    def _per_outcome(self) -> Self:
        if self.outcome == "settled":
            if self.bank_reference is None:
                raise PydanticCustomError("bank_reference_required", "bank_reference is required.")
            if self.reason is not None:
                raise PydanticCustomError("reason_not_allowed", "reason must be empty.")
        elif self.outcome == "rejected":
            if self.reason is None:
                raise PydanticCustomError("reason_required", "reason is required.")
            if self.bank_reference is not None:
                raise PydanticCustomError("bank_reference_not_allowed", "No bank_reference.")
        elif self.outcome == "indeterminate":
            if self.reason is not None or self.bank_reference is not None:
                raise PydanticCustomError("fields_not_allowed", "No reason or bank_reference.")
        elif self.bank_reference is not None:  # poison: reason optional
            raise PydanticCustomError("bank_reference_not_allowed", "No bank_reference.")
        return self


class ResultOut(BaseModel):
    """Exactly ``{payout_id, attempt, outcome}`` (R7); ``outcome`` is the stored status (C26)."""

    payout_id: UUID
    attempt: int
    outcome: AttemptStatusName
