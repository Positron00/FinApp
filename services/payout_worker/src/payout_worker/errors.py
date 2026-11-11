"""The worker's exception set (design §3.2).

Every exception carries a machine-readable ``code`` (``^[a-z0-9_]{1,100}$``) used as the DLQ
``error_code``; messages are fixed text, never response bodies, amounts or bank details.
"""

# ruff: noqa: N818  (the design's exception names)

from __future__ import annotations

import re
from typing import Final
from uuid import UUID

__all__ = [
    "AttemptNotCurrent",
    "InvalidMessage",
    "PayoutNotFound",
    "PayrollRejected",
    "PayrollUnavailable",
    "WorkerError",
    "WorkerInternalError",
    "safe_code",
]

_CODE_RE: Final = re.compile(r"^[a-z0-9_]{1,100}$")


def safe_code(code: object, fallback: str) -> str:
    """``code`` when it is a valid error code, else ``fallback``."""
    return code if isinstance(code, str) and _CODE_RE.match(code) else fallback


class WorkerError(Exception):
    code: str = "unexpected"

    def __init__(self, code: str | None = None) -> None:
        if code is not None:
            self.code = code
        super().__init__(self.code)


class InvalidMessage(WorkerError):
    """A stream entry that can never succeed (schema-invalid). Keeps whatever IDs were readable."""

    code = "schema_invalid"

    def __init__(
        self,
        *,
        event_id: UUID | None = None,
        payout_id: UUID | None = None,
        attempt: int | None = None,
        company_id: UUID | None = None,
        traceparent: str | None = None,
    ) -> None:
        super().__init__()
        self.event_id = event_id
        self.payout_id = payout_id
        self.attempt = attempt
        self.company_id = company_id
        self.traceparent = traceparent

    @property
    def ids_readable(self) -> bool:
        return (
            self.payout_id is not None and self.attempt is not None and self.company_id is not None
        )


class AttemptNotCurrent(WorkerError):
    code = "attempt_not_current"


class PayoutNotFound(WorkerError):
    code = "payout_not_found"


class PayrollUnavailable(WorkerError):
    """Timeout, connect error, 429/5xx after retries, deadline or open breaker."""

    code = "payroll_unavailable"


class PayrollRejected(WorkerError):
    """400/401/403, 404 on the result, other 409, 422, other 4xx, or a malformed 200 body."""

    code = "payroll_rejected"


class WorkerInternalError(WorkerError):
    """DB error or timeout, or the handler deadline."""

    code = "unexpected"
