"""Statuses, the run status rule and the result decision table (design §3.3).

PAY-405 (``decide_result``), PAY-NFR-02 (``is_late_settlement``), PAY-406
(``run_status_from_payouts``), PAY-411 (``instruction_allowed``), PAY-301/304/400 (the run
guards).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from payroll.domain import PayRunLocked, PayRunNotApprovable

__all__ = [
    "FINAL_ATTEMPT",
    "FINAL_RUN",
    "LIVE_ATTEMPT",
    "MUTABLE_RUN",
    "AttemptStatus",
    "PayoutStatus",
    "ResultDecision",
    "ResultKind",
    "ResultOutcome",
    "RunStatus",
    "assert_run_approvable",
    "assert_run_mutable",
    "decide_result",
    "instruction_allowed",
    "is_late_settlement",
    "payout_status_for_attempt",
    "run_status_from_payouts",
]


class RunStatus(StrEnum):
    DRAFT = "draft"
    CALCULATED = "calculated"
    PROCESSING = "processing"
    PAID = "paid"
    PARTIALLY_FAILED = "partially_failed"


class PayoutStatus(StrEnum):
    REQUESTED = "requested"
    PAID = "paid"
    FAILED = "failed"


class AttemptStatus(StrEnum):
    REQUESTED = "requested"
    INDETERMINATE = "indeterminate"
    NEEDS_OPERATOR = "needs_operator"
    SETTLED = "settled"
    REJECTED = "rejected"


class ResultOutcome(StrEnum):
    SETTLED = "settled"
    REJECTED = "rejected"
    INDETERMINATE = "indeterminate"
    POISON = "poison"


FINAL_ATTEMPT: Final = frozenset({AttemptStatus.SETTLED, AttemptStatus.REJECTED})
LIVE_ATTEMPT: Final = frozenset({AttemptStatus.REQUESTED, AttemptStatus.INDETERMINATE})
MUTABLE_RUN: Final = frozenset({RunStatus.DRAFT, RunStatus.CALCULATED})
FINAL_RUN: Final = frozenset({RunStatus.PAID, RunStatus.PARTIALLY_FAILED})


def run_status_from_payouts(statuses: Iterable[PayoutStatus]) -> RunStatus:
    """PAY-406: processing if any requested, else paid if all paid, else partially_failed."""
    s = list(statuses)
    if not s:
        raise ValueError("an approved run has at least one payout")
    if any(x is PayoutStatus.REQUESTED for x in s):
        return RunStatus.PROCESSING
    if all(x is PayoutStatus.PAID for x in s):
        return RunStatus.PAID
    return RunStatus.PARTIALLY_FAILED


_PAYOUT_FOR_ATTEMPT: Final = {
    AttemptStatus.SETTLED: PayoutStatus.PAID,
    AttemptStatus.REJECTED: PayoutStatus.FAILED,
    AttemptStatus.REQUESTED: PayoutStatus.REQUESTED,
    AttemptStatus.INDETERMINATE: PayoutStatus.REQUESTED,
    AttemptStatus.NEEDS_OPERATOR: PayoutStatus.REQUESTED,
}


def payout_status_for_attempt(a: AttemptStatus) -> PayoutStatus:
    """settled → paid; rejected → failed; requested | indeterminate | needs_operator → requested."""
    return _PAYOUT_FOR_ATTEMPT[a]


def assert_run_mutable(status: RunStatus) -> None:
    if status not in MUTABLE_RUN:
        raise PayRunLocked(str(status))


def assert_run_approvable(status: RunStatus) -> None:
    if status is not RunStatus.CALCULATED:
        raise PayRunNotApprovable(str(status))


def instruction_allowed(
    *, current_attempt: int, attempt: int, attempt_status: AttemptStatus | None
) -> bool:
    """PAY-411: only the current attempt, and only while it is deliverable."""
    return attempt == current_attempt and attempt_status in LIVE_ATTEMPT


class ResultKind(StrEnum):
    APPLY = "apply"
    STORED = "stored"
    NOT_CURRENT = "not_current"


@dataclass(frozen=True, slots=True)
class ResultDecision:
    kind: ResultKind
    new_status: AttemptStatus | None  # set only when kind == APPLY


_APPLY_FROM_LIVE: Final = {
    ResultOutcome.SETTLED: AttemptStatus.SETTLED,
    ResultOutcome.REJECTED: AttemptStatus.REJECTED,
    ResultOutcome.INDETERMINATE: AttemptStatus.INDETERMINATE,
    ResultOutcome.POISON: AttemptStatus.NEEDS_OPERATOR,
}
_STORED: Final = ResultDecision(ResultKind.STORED, None)


def decide_result(
    *,
    current_attempt: int,
    attempt: int,
    attempt_status: AttemptStatus | None,
    outcome: ResultOutcome,
) -> ResultDecision:
    """The PAY-405 decision table (design §3.3; R7 for ``needs_operator``)."""
    if attempt != current_attempt or attempt_status is None:
        return ResultDecision(ResultKind.NOT_CURRENT, None)
    if attempt_status in FINAL_ATTEMPT:
        return _STORED
    if attempt_status is AttemptStatus.NEEDS_OPERATOR:
        # A definite bank answer wins (R7); indeterminate and poison change nothing.
        if outcome in (ResultOutcome.SETTLED, ResultOutcome.REJECTED):
            return ResultDecision(ResultKind.APPLY, _APPLY_FROM_LIVE[outcome])
        return _STORED
    return ResultDecision(ResultKind.APPLY, _APPLY_FROM_LIVE[outcome])


def is_late_settlement(
    *,
    current_attempt: int,
    attempt: int,
    attempt_status: AttemptStatus | None,
    outcome: ResultOutcome,
) -> bool:
    """PAY-NFR-02: a ``settled`` answer for an attempt payroll no longer treats as payable.

    True for the current attempt when it is already ``rejected`` (for example
    ``operator_rejected`` after the C48 wait), and for any earlier attempt that is not
    ``settled`` (a retry has opened a later attempt, which may pay again). The response contract
    is unchanged (``decide_result``); the caller records the event (design §4.7 "Late
    settlement"). An unknown attempt number is never a late settlement.
    """
    if outcome is not ResultOutcome.SETTLED or attempt_status is None:
        return False
    if attempt == current_attempt:
        return attempt_status is AttemptStatus.REJECTED
    return attempt < current_attempt and attempt_status is not AttemptStatus.SETTLED
