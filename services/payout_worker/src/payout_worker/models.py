"""Wire models (design §2.3; PAY-402, PAY-405, PAY-411, PAY-412, R6, R7, C22, C26).

What the worker sends is ``extra="forbid"``; what it receives is ``extra="ignore"``, so additive
provider changes don't break it. Money is integer cents. Routing and account are ``SecretStr``
until ``BankClient`` builds the request body.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Final, Literal, Self
from uuid import UUID

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from payout_worker.errors import InvalidMessage

__all__ = [
    "FINAL_STATUSES",
    "TRACEPARENT_PATTERN",
    "AttemptStatus",
    "Envelope",
    "Instruction",
    "NoAnswer",
    "NoAnswerReason",
    "NotFound",
    "Outcome",
    "PayoutRequestedData",
    "Rejected",
    "ResultRequest",
    "ResultResponse",
    "Settled",
    "StreamMessage",
    "TransferRequest",
    "TransferResponse",
    "parse_envelope",
    "schema_path",
]

TRACEPARENT_PATTERN: Final = r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$"
REASON_PATTERN: Final = r"^[a-z0-9_]{1,100}$"
ROUTING_PATTERN: Final = r"^\d{9}$"
ACCOUNT_PATTERN: Final = r"^\d{4,17}$"
_ROUTING_RE: Final = re.compile(r"[0-9]{9}")
_ACCOUNT_RE: Final = re.compile(r"[0-9]{4,17}")
SCHEMA_RELATIVE_PATH: Final = Path("contracts/events/payout.requested.v1.json")
_TRACEPARENT_RE: Final = re.compile(TRACEPARENT_PATTERN)
_RECEIVED = ConfigDict(extra="ignore", frozen=True)
_SENT = ConfigDict(extra="forbid", frozen=True)

Outcome = Literal["settled", "rejected", "indeterminate", "poison"]
AttemptStatus = Literal["requested", "indeterminate", "needs_operator", "settled", "rejected"]
FINAL_STATUSES: Final[frozenset[str]] = frozenset({"settled", "rejected"})
POISON_REASONS: Final = frozenset({"schema_invalid", "amount_mismatch", "key_window_expired"})


# --- the consumed event ------------------------------------------------------------------------


class PayoutRequestedData(BaseModel):
    model_config = _SENT  # matches the JSON Schema's additionalProperties: false

    payout_id: UUID
    pay_run_id: UUID
    company_id: UUID
    attempt: int = Field(ge=1)
    amount_cents: int = Field(gt=0, le=1_000_000_000)
    currency: Literal["USD"]
    republish_seq: int = Field(ge=0)


class Envelope(BaseModel):
    model_config = _SENT

    event_id: UUID
    type: Literal["payout.requested"]
    version: Literal[1]
    occurred_at: AwareDatetime
    producer: Literal["payroll"]
    traceparent: str = Field(pattern=TRACEPARENT_PATTERN)  # required (C22)
    data: PayoutRequestedData

    @property
    def bank_key(self) -> str:
        """``<payout_id>:<attempt>``: one bank key per attempt (ADR 0003 a)."""
        return f"{self.data.payout_id}:{self.data.attempt}"


@dataclass(frozen=True)
class StreamMessage:
    """One stream entry: its ID and raw fields (``event_id``, ``type``, ``envelope``; R7)."""

    id: str
    fields: Mapping[bytes | str, bytes | str]

    def field(self, name: str) -> bytes | None:
        for k, v in self.fields.items():
            key = k.decode() if isinstance(k, bytes) else k
            if key == name:
                return v if isinstance(v, bytes) else v.encode()
        return None


def schema_path() -> Path:
    """The schema next to the nearest ancestor ``contracts/`` (repo root or ``/app``)."""
    for base in (*Path(__file__).resolve().parents, Path.cwd()):
        candidate = base / SCHEMA_RELATIVE_PATH
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(str(SCHEMA_RELATIVE_PATH))  # pragma: no cover


@cache
def _validator() -> Draft202012Validator:
    schema = json.loads(schema_path().read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)


def _uuid(value: object) -> UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _partial(raw: object, entry_event_id: bytes | None) -> InvalidMessage:
    """Whatever IDs can be read from an invalid payload (for the poison report, §3.1 step 0)."""
    doc: Mapping[str, Any] = raw if isinstance(raw, dict) else {}
    data = doc.get("data")
    d: Mapping[str, Any] = data if isinstance(data, dict) else {}
    attempt = d.get("attempt")
    tp = doc.get("traceparent")
    event_id = _uuid(doc.get("event_id"))
    if event_id is None and entry_event_id is not None:
        event_id = _uuid(entry_event_id.decode(errors="replace"))
    return InvalidMessage(
        event_id=event_id,
        payout_id=_uuid(d.get("payout_id")),
        attempt=attempt
        if isinstance(attempt, int) and not isinstance(attempt, bool) and attempt >= 1
        else None,
        company_id=_uuid(d.get("company_id")),
        traceparent=tp if isinstance(tp, str) and _TRACEPARENT_RE.fullmatch(tp) else None,
    )


def parse_envelope(msg: StreamMessage) -> Envelope:
    """JSON Schema first, then Pydantic; any failure raises ``InvalidMessage`` (PAY-402, R5)."""
    entry_event_id = msg.field("event_id")
    raw_bytes = msg.field("envelope")
    if raw_bytes is None:
        raise _partial(None, entry_event_id)
    try:
        raw = json.loads(raw_bytes)
    except ValueError:
        raise _partial(None, entry_event_id) from None
    if not isinstance(raw, dict) or any(True for _ in _validator().iter_errors(raw)):
        raise _partial(raw, entry_event_id)
    try:
        return Envelope.model_validate(raw)
    except ValidationError:  # pragma: no cover  (the schema is stricter than the model)
        raise _partial(raw, entry_event_id) from None


# --- payroll: instruction and result -------------------------------------------------------------


class Instruction(BaseModel):
    model_config = _RECEIVED

    payout_id: UUID
    attempt: int = Field(ge=1)
    amount_cents: int = Field(gt=0)
    currency: Literal["USD"]
    routing: SecretStr  # never logged, never in spans
    account: SecretStr
    requested_at: AwareDatetime

    # The bank's patterns, checked here so a bad snapshot is a malformed payroll response
    # (``PayrollRejected("malformed_response")``, raised ``from None``) and never a
    # ValidationError with the digits in its text (C63, PAY-NFR-07). The messages are fixed.
    @field_validator("routing")
    @classmethod
    def _routing(cls, v: SecretStr) -> SecretStr:
        if not _ROUTING_RE.fullmatch(v.get_secret_value()):
            raise ValueError("routing must be 9 digits")
        return v

    @field_validator("account")
    @classmethod
    def _account(cls, v: SecretStr) -> SecretStr:
        if not _ACCOUNT_RE.fullmatch(v.get_secret_value()):
            raise ValueError("account must be 4 to 17 digits")
        return v


class ResultRequest(BaseModel):
    model_config = _SENT

    attempt: int = Field(ge=1)
    outcome: Outcome
    reason: str | None = Field(default=None, pattern=REASON_PATTERN)
    bank_reference: str | None = Field(default=None, min_length=1, max_length=64)  # C26

    @model_validator(mode="after")
    def _per_outcome(self) -> Self:
        if self.outcome == "settled":
            ok = self.bank_reference is not None and self.reason is None
        elif self.outcome == "rejected":
            ok = self.reason is not None and self.bank_reference is None
        elif self.outcome == "poison":
            ok = self.reason in POISON_REASONS and self.bank_reference is None
        else:  # indeterminate
            ok = self.reason is None and self.bank_reference is None
        if not ok:
            raise ValueError(f"fields don't fit outcome {self.outcome}")
        return self


class ResultResponse(BaseModel):
    """200 body (R7, PAY-405); ``outcome`` is the stored attempt status, any of five (C26)."""

    model_config = _RECEIVED

    payout_id: UUID
    attempt: int
    outcome: AttemptStatus


# --- bank ----------------------------------------------------------------------------------------


class TransferRequest(BaseModel):
    model_config = _SENT

    amount_cents: int = Field(gt=0)  # the instruction's amount, never the event's
    currency: Literal["USD"]
    routing: str = Field(pattern=ROUTING_PATTERN)
    account: str = Field(pattern=ACCOUNT_PATTERN)

    def __repr_args__(self) -> Any:  # routing and account never reach a repr (PAY-NFR-07)
        for name, value in super().__repr_args__():
            yield name, ("***" if name in {"routing", "account"} else value)


class TransferResponse(BaseModel):
    """R6: 201 new, 200 replay; ``{transfer_id, status, reason, bank_reference}``."""

    model_config = _RECEIVED

    transfer_id: UUID
    status: Literal["settled", "rejected"]
    reason: str | None = Field(default=None, pattern=REASON_PATTERN)
    bank_reference: str | None = Field(default=None, min_length=1, max_length=64)  # C26


NoAnswerReason = Literal[
    "timeout",
    "connect_error",
    "breaker_open",
    "http_429",
    "http_5xx",
    "http_401",
    "http_403",
    "http_404",
    "http_409",
    "http_422",
    "http_4xx_other",
    "http_other",
    "malformed_body",
]


@dataclass(frozen=True)
class Settled:
    transfer_id: UUID
    bank_reference: str


@dataclass(frozen=True)
class Rejected:
    transfer_id: UUID
    reason: str


@dataclass(frozen=True)
class NoAnswer:
    reason: NoAnswerReason
    http_status: int | None = None


@dataclass(frozen=True)
class NotFound:
    pass
