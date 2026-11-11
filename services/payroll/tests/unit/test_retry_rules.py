"""Retry and re-publish rules (design §3.5; PAY-500, PAY-407, R1, R34)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from payroll.domain import EmployeeBankDetailsMissing, PayoutNotRetryable, RetryLimitReached
from payroll.domain.retry import (
    RepublishAction,
    check_retry,
    republish_backoff_s,
    republish_decision,
)
from payroll.domain.state import AttemptStatus, PayoutStatus

E1 = uuid4()
NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)


def _retry(**over: Any) -> None:
    kw: dict[str, Any] = {
        "payout_status": PayoutStatus.FAILED,
        "attempt_status": AttemptStatus.REJECTED,
        "current_attempt": 1,
        "max_attempts": 5,
        "employee_id": E1,
        "employee_active": True,
        "employee_has_bank": True,
    }
    kw.update(over)
    check_retry(**kw)


def test_failed_rejected_attempt_passes() -> None:
    """PAY-500: a failed payout whose current attempt is rejected can be retried."""
    _retry()
    _retry(current_attempt=4)


@pytest.mark.parametrize(
    ("payout", "attempt"),
    [
        (PayoutStatus.REQUESTED, AttemptStatus.REQUESTED),
        (PayoutStatus.REQUESTED, AttemptStatus.INDETERMINATE),
        (PayoutStatus.REQUESTED, AttemptStatus.NEEDS_OPERATOR),
        (PayoutStatus.PAID, AttemptStatus.SETTLED),
        (PayoutStatus.PAID, None),
    ],
)
def test_not_retryable(payout: PayoutStatus, attempt: AttemptStatus | None) -> None:
    """PAY-500: indeterminate, needs_operator, requested and paid payouts are not retryable."""
    with pytest.raises(PayoutNotRetryable) as info:
        _retry(payout_status=payout, attempt_status=attempt)
    assert info.value.details == {
        "payout_status": str(payout),
        "attempt_status": str(attempt) if attempt else None,
    }


def test_limit_at_max_attempts() -> None:
    """PAY-500: refused at current_attempt = PAYOUT_MAX_ATTEMPTS (5, including the first)."""
    with pytest.raises(RetryLimitReached) as info:
        _retry(current_attempt=5)
    assert info.value.details == {"current_attempt": 5, "max_attempts": 5}


@pytest.mark.parametrize(("active", "bank"), [(False, True), (True, False), (False, False)])
def test_bank_details_missing(active: bool, bank: bool) -> None:
    """PAY-500: an inactive or purged employee → employee_bank_details_missing with the ID."""
    with pytest.raises(EmployeeBankDetailsMissing) as info:
        _retry(employee_active=active, employee_has_bank=bank)
    assert info.value.employee_ids == [E1]


def test_check_order_is_fixed() -> None:
    """PAY-500: not retryable → limit → bank details, whatever else is also wrong."""
    with pytest.raises(PayoutNotRetryable):
        _retry(
            payout_status=PayoutStatus.REQUESTED,
            attempt_status=AttemptStatus.INDETERMINATE,
            current_attempt=5,
            employee_active=False,
        )
    with pytest.raises(RetryLimitReached):
        _retry(current_attempt=5, employee_active=False)


def test_backoff_sequence_at_the_defaults() -> None:
    """PAY-407, R1: 360, 720, 900, 900… s; very large counts stay capped."""
    seq = [republish_backoff_s(n, after_s=360, max_backoff_s=900) for n in range(1, 7)]
    assert seq == [360, 720, 900, 900, 900, 900]
    assert republish_backoff_s(10_000, after_s=360, max_backoff_s=900) == 900
    with pytest.raises(ValueError, match="publish_count"):
        republish_backoff_s(0, after_s=360, max_backoff_s=900)


def _decide(**over: Any) -> RepublishAction:
    kw: dict[str, Any] = {
        "status": AttemptStatus.REQUESTED,
        "is_current": True,
        "publish_count": 1,
        "last_published_at": NOW - timedelta(seconds=361),
        "now": NOW,
        "has_unsent_outbox": False,
        "after_s": 360,
        "max_backoff_s": 900,
        "max_republish": 10,
    }
    kw.update(over)
    return republish_decision(**kw)


def test_republish_decision() -> None:
    """PAY-407: due → REPUBLISH; not due (boundary inclusive), unsent row, stale or non-live →
    SKIP; the 11th publication due → NEEDS_OPERATOR."""
    assert _decide() is RepublishAction.REPUBLISH
    assert _decide(status=AttemptStatus.INDETERMINATE) is RepublishAction.REPUBLISH
    assert _decide(last_published_at=NOW - timedelta(seconds=360)) is RepublishAction.SKIP
    assert _decide(has_unsent_outbox=True) is RepublishAction.SKIP
    assert _decide(is_current=False) is RepublishAction.SKIP
    for status in (AttemptStatus.NEEDS_OPERATOR, AttemptStatus.SETTLED, AttemptStatus.REJECTED):
        assert _decide(status=status) is RepublishAction.SKIP
    late = NOW - timedelta(seconds=901)
    assert _decide(publish_count=2, last_published_at=late) is RepublishAction.REPUBLISH
    assert _decide(publish_count=10, last_published_at=late) is RepublishAction.REPUBLISH
    assert _decide(publish_count=11, last_published_at=late) is RepublishAction.NEEDS_OPERATOR
    assert _decide(publish_count=11, last_published_at=NOW) is RepublishAction.SKIP


def test_republish_decision_envelope_invalid() -> None:
    """PAY-NFR-03, PAY-407: when the only unsent rows were refused by the relay, a due attempt
    goes to the operator instead of re-publishing the same envelope; not due, a sendable
    unsent row or a non-live attempt still SKIP."""
    assert _decide(envelope_invalid=True) is RepublishAction.NEEDS_OPERATOR
    not_due = NOW - timedelta(seconds=360)
    assert _decide(envelope_invalid=True, last_published_at=not_due) is RepublishAction.SKIP
    assert _decide(envelope_invalid=True, has_unsent_outbox=True) is RepublishAction.SKIP
    assert (
        _decide(envelope_invalid=True, status=AttemptStatus.NEEDS_OPERATOR) is RepublishAction.SKIP
    )
