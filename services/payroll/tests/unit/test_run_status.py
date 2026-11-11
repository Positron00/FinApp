"""The run status rule (PAY-406; design §3.3)."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from payroll.domain.state import (
    AttemptStatus,
    PayoutStatus,
    RunStatus,
    payout_status_for_attempt,
    run_status_from_payouts,
)

P = PayoutStatus


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([P.REQUESTED], RunStatus.PROCESSING),
        ([P.PAID, P.REQUESTED, P.FAILED], RunStatus.PROCESSING),
        ([P.PAID], RunStatus.PAID),
        ([P.PAID, P.PAID], RunStatus.PAID),
        ([P.FAILED], RunStatus.PARTIALLY_FAILED),
        ([P.PAID, P.FAILED], RunStatus.PARTIALLY_FAILED),
    ],
)
def test_table(statuses: list[PayoutStatus], expected: RunStatus) -> None:
    """PAY-406: processing if any requested, else paid if all paid, else partially_failed."""
    assert run_status_from_payouts(statuses) is expected


@given(st.lists(st.sampled_from(list(PayoutStatus)), min_size=1, max_size=40))
def test_any_multiset_follows_the_rule(statuses: list[PayoutStatus]) -> None:
    """PAY-406 (property): the result is the plan's rule for any multiset, in any order."""
    got = run_status_from_payouts(statuses)
    assert got is run_status_from_payouts(reversed(statuses))
    if P.REQUESTED in statuses:
        assert got is RunStatus.PROCESSING
    elif set(statuses) == {P.PAID}:
        assert got is RunStatus.PAID
    else:
        assert got is RunStatus.PARTIALLY_FAILED


def test_empty_input_raises() -> None:
    """PAY-406: an approved run always has at least one payout."""
    with pytest.raises(ValueError, match="at least one payout"):
        run_status_from_payouts([])


@pytest.mark.parametrize(
    ("attempt", "payout"),
    [
        (AttemptStatus.SETTLED, P.PAID),
        (AttemptStatus.REJECTED, P.FAILED),
        (AttemptStatus.REQUESTED, P.REQUESTED),
        (AttemptStatus.INDETERMINATE, P.REQUESTED),
        (AttemptStatus.NEEDS_OPERATOR, P.REQUESTED),
    ],
)
def test_payout_status_for_attempt(attempt: AttemptStatus, payout: PayoutStatus) -> None:
    """PAY-405, PAY-406: only settled and rejected make a payout final."""
    assert payout_status_for_attempt(attempt) is payout
