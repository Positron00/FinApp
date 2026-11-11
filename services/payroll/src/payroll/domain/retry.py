"""Retry rules and the re-publish backoff (design §3.5; PAY-500, PAY-407, R1, R34)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from payroll.domain import EmployeeBankDetailsMissing, PayoutNotRetryable, RetryLimitReached
from payroll.domain.state import LIVE_ATTEMPT, AttemptStatus, PayoutStatus

__all__ = ["RepublishAction", "check_retry", "republish_backoff_s", "republish_decision"]


def check_retry(
    *,
    payout_status: PayoutStatus,
    attempt_status: AttemptStatus | None,
    current_attempt: int,
    max_attempts: int,
    employee_id: UUID,
    employee_active: bool,
    employee_has_bank: bool,
) -> None:
    """PAY-500, in this fixed order: not retryable → limit → bank details. The caller then runs
    ``check_retry_dual`` (dual approval comes last)."""
    if payout_status is not PayoutStatus.FAILED or attempt_status is not AttemptStatus.REJECTED:
        # covers requested, indeterminate and needs_operator attempts, and paid payouts
        raise PayoutNotRetryable(
            str(payout_status), str(attempt_status) if attempt_status is not None else None
        )
    if current_attempt >= max_attempts:  # 5 including the first: refused at current_attempt = 5
        raise RetryLimitReached(current_attempt, max_attempts)
    if not (employee_active and employee_has_bank):
        raise EmployeeBankDetailsMissing([employee_id])


def republish_backoff_s(publish_count: int, *, after_s: int, max_backoff_s: int) -> int:
    """``min(after_s * 2^(publish_count-1), max_backoff_s)``: 360, 720, 900, 900… at the
    defaults (R1)."""
    if publish_count < 1:
        raise ValueError("publish_count starts at 1")
    # Capping the exponent keeps the integer small; the cap is reached long before it matters.
    factor: int = 1 << min(publish_count - 1, 32)
    return min(after_s * factor, max_backoff_s)


class RepublishAction(StrEnum):
    SKIP = "skip"
    REPUBLISH = "republish"
    NEEDS_OPERATOR = "needs_operator"


def republish_decision(
    *,
    status: AttemptStatus,
    is_current: bool,
    publish_count: int,
    last_published_at: datetime,
    now: datetime,
    has_unsent_outbox: bool,
    after_s: int,
    max_backoff_s: int,
    max_republish: int,
) -> RepublishAction:
    """PAY-407: re-publish a live current attempt once its backoff has passed and no unsent
    outbox row exists for it; after ``max_republish`` re-publishes it goes to the operator."""
    if status not in LIVE_ATTEMPT or not is_current or has_unsent_outbox:
        return RepublishAction.SKIP
    elapsed = (now - last_published_at).total_seconds()
    backoff = republish_backoff_s(publish_count, after_s=after_s, max_backoff_s=max_backoff_s)
    if elapsed <= backoff:
        return RepublishAction.SKIP
    if publish_count - 1 >= max_republish:  # max_republish re-publishes already made
        return RepublishAction.NEEDS_OPERATOR
    return RepublishAction.REPUBLISH
