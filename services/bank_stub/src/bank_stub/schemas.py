"""API models (design §2.3; PAY-412, R6, C26)."""

from __future__ import annotations

import re
from typing import Any, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "IDEMPOTENCY_KEY_PATTERN",
    "IDEMPOTENCY_KEY_RE",
    "TransferRequest",
    "TransferResponse",
]

IDEMPOTENCY_KEY_PATTERN: Final = r"^[A-Za-z0-9:_-]{1,255}$"  # R6; the worker sends <payout_id>:<n>
IDEMPOTENCY_KEY_RE: Final = re.compile(IDEMPOTENCY_KEY_PATTERN)  # always fullmatch (C63)
_MASKED: Final = frozenset({"routing", "account"})


class TransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount_cents: int = Field(gt=0, le=1_000_000_000)
    currency: Literal["USD"]
    routing: str = Field(pattern=r"^\d{9}$")
    account: str = Field(pattern=r"^\d{4,17}$")

    def __repr_args__(self) -> Any:  # routing and account never reach a repr (PAY-NFR-07)
        for name, value in super().__repr_args__():
            yield name, ("***" if name in _MASKED else value)


class TransferResponse(BaseModel):
    """Exactly ``{transfer_id, status, reason, bank_reference}`` (R6, PAY-412)."""

    transfer_id: UUID
    status: Literal["settled", "rejected"]
    reason: Literal["account_closed", "insufficient_funds_at_bank"] | None
    bank_reference: str | None = Field(max_length=64)
