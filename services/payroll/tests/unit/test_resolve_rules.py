"""Operator resolution rules (design §3.5a; PAY-502, R8, C13, C48, C73)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from payroll.domain import InvalidFields, PayoutNotBlocked, ResolveTooEarly
from payroll.domain.resolve import (
    KEY_WINDOW_EXPIRED,
    OPERATOR_REJECTED,
    ResolveAction,
    check_mark_rejected_wait,
    check_resolve,
    check_resolve_action,
    resolve_transition,
)
from payroll.domain.state import AttemptStatus, PayoutStatus

AT = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)


def test_only_a_blocked_payout_resolves() -> None:
    """PAY-502: a requested payout whose current attempt is needs_operator passes."""
    check_resolve(payout_status=PayoutStatus.REQUESTED, attempt_status=AttemptStatus.NEEDS_OPERATOR)


@pytest.mark.parametrize(
    ("payout", "attempt"),
    [
        (PayoutStatus.REQUESTED, AttemptStatus.REQUESTED),
        (PayoutStatus.REQUESTED, AttemptStatus.INDETERMINATE),
        (PayoutStatus.PAID, AttemptStatus.SETTLED),
        (PayoutStatus.FAILED, AttemptStatus.REJECTED),
        (PayoutStatus.PAID, None),  # no_transfer
    ],
)
def test_anything_else_is_not_blocked(payout: PayoutStatus, attempt: AttemptStatus | None) -> None:
    """PAY-502: every other state → payout_not_blocked with the attempt status."""
    with pytest.raises(PayoutNotBlocked) as info:
        check_resolve(payout_status=payout, attempt_status=attempt)
    assert info.value.details == {"attempt_status": str(attempt) if attempt else None}


def test_redrive_transition() -> None:
    """PAY-502, C13: redrive → requested, reason cleared, publish_count 1, publish (seq 0)."""
    t = resolve_transition(ResolveAction.REDRIVE)
    assert (t.attempt_status, t.failure_reason, t.attempt_final) == (
        AttemptStatus.REQUESTED,
        None,
        False,
    )
    assert (t.payout_status, t.publish, t.publish_count) == (PayoutStatus.REQUESTED, True, 1)


def test_mark_rejected_transition() -> None:
    """PAY-502: mark_rejected → rejected / operator_rejected, final, payout failed, no publish."""
    t = resolve_transition(ResolveAction.MARK_REJECTED)
    assert (t.attempt_status, t.failure_reason, t.attempt_final) == (
        AttemptStatus.REJECTED,
        OPERATOR_REJECTED,
        True,
    )
    assert (t.payout_status, t.publish, t.publish_count) == (PayoutStatus.FAILED, False, None)


def test_mark_rejected_wait() -> None:
    """PAY-502, C48: refused until min_wait_s after needs_operator_at; Retry-After rounds up."""
    with pytest.raises(ResolveTooEarly) as info:
        check_mark_rejected_wait(needs_operator_at=AT, now=AT, min_wait_s=120)
    assert info.value.retry_after_s == 120
    with pytest.raises(ResolveTooEarly) as info:
        check_mark_rejected_wait(
            needs_operator_at=AT, now=AT + timedelta(seconds=119, milliseconds=1), min_wait_s=120
        )
    assert (info.value.retry_after_s, info.value.headers) == (1, {"Retry-After": "1"})
    check_mark_rejected_wait(needs_operator_at=AT, now=AT + timedelta(seconds=120), min_wait_s=120)
    check_mark_rejected_wait(needs_operator_at=AT, now=AT + timedelta(hours=1), min_wait_s=120)


def test_redrive_refused_after_key_window_expired() -> None:
    """PAY-410, PAY-502, C73: redrive keeps requested_at, so a key_window_expired attempt would
    only GET the same key, 404 and come back to needs_operator: refused (422 validation_error at
    body.action); mark_rejected stays open."""
    with pytest.raises(InvalidFields) as err:
        check_resolve_action(action=ResolveAction.REDRIVE, failure_reason=KEY_WINDOW_EXPIRED)
    assert err.value.status == 422
    assert err.value.details == [{"field": "body.action", "issue": "redrive_not_allowed"}]
    check_resolve_action(action=ResolveAction.MARK_REJECTED, failure_reason=KEY_WINDOW_EXPIRED)


@pytest.mark.parametrize(
    "reason", ["poison", "schema_invalid", "amount_mismatch", "republish_limit", "envelope_invalid"]
)
def test_redrive_allowed_for_other_reasons(reason: str) -> None:
    """PAY-502, C73: only key_window_expired refuses a redrive."""
    check_resolve_action(action=ResolveAction.REDRIVE, failure_reason=reason)
