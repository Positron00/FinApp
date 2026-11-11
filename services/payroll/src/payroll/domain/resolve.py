"""Operator resolution of a ``needs_operator`` attempt (design §3.5a; PAY-502, R8, C13, C48,
C73)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

from payroll.domain import InvalidFields, PayoutNotBlocked, ResolveTooEarly
from payroll.domain.state import AttemptStatus, PayoutStatus

__all__ = [
    "KEY_WINDOW_EXPIRED",
    "OPERATOR_REJECTED",
    "ResolveAction",
    "ResolveTransition",
    "check_mark_rejected_wait",
    "check_resolve",
    "check_resolve_action",
    "resolve_transition",
]

OPERATOR_REJECTED: Final = "operator_rejected"
KEY_WINDOW_EXPIRED: Final = "key_window_expired"
_SECOND: Final = timedelta(seconds=1)


class ResolveAction(StrEnum):
    REDRIVE = "redrive"
    MARK_REJECTED = "mark_rejected"


def check_resolve(*, payout_status: PayoutStatus, attempt_status: AttemptStatus | None) -> None:
    """Only a ``requested`` payout whose current attempt is ``needs_operator`` (409 otherwise)."""
    if (
        payout_status is not PayoutStatus.REQUESTED
        or attempt_status is not AttemptStatus.NEEDS_OPERATOR
    ):
        raise PayoutNotBlocked(str(attempt_status) if attempt_status is not None else None)


def check_resolve_action(*, action: ResolveAction, failure_reason: str | None) -> None:
    """C73: ``redrive`` is refused for a ``key_window_expired`` attempt (422 ``validation_error``,
    ``body.action`` / ``redrive_not_allowed``). A redrive keeps ``requested_at`` (it dates the
    bank key), so the worker would take the GET-only path again (PAY-410), get the same 404 and
    report the same poison: the attempt would only come back to ``needs_operator``. The operator
    reconciles with the bank statement and uses ``mark_rejected``."""
    if action is ResolveAction.REDRIVE and failure_reason == KEY_WINDOW_EXPIRED:
        raise InvalidFields([("body.action", "redrive_not_allowed")])


def check_mark_rejected_wait(
    *, needs_operator_at: datetime, now: datetime, min_wait_s: int
) -> None:
    """C48: ``mark_rejected`` only once ``now - needs_operator_at >= min_wait_s``; earlier raises
    ``ResolveTooEarly`` with the whole seconds left (rounded up) for ``Retry-After``."""
    remaining = timedelta(seconds=min_wait_s) - (now - needs_operator_at)
    if remaining > timedelta(0):
        raise ResolveTooEarly(-(-remaining // _SECOND))


@dataclass(frozen=True, slots=True)
class ResolveTransition:
    attempt_status: AttemptStatus  # REQUESTED (redrive) or REJECTED (mark_rejected)
    failure_reason: str | None  # None (redrive clears it) or "operator_rejected"
    attempt_final: bool  # True for mark_rejected: set final_at
    payout_status: PayoutStatus  # REQUESTED (unchanged) or FAILED
    publish: bool  # True for redrive: write an outbox row, republish_seq = 0
    publish_count: int | None  # 1 for redrive (C13); None = unchanged


_TRANSITIONS: Final = {
    ResolveAction.REDRIVE: ResolveTransition(
        attempt_status=AttemptStatus.REQUESTED,
        failure_reason=None,
        attempt_final=False,
        payout_status=PayoutStatus.REQUESTED,
        publish=True,
        publish_count=1,
    ),
    ResolveAction.MARK_REJECTED: ResolveTransition(
        attempt_status=AttemptStatus.REJECTED,
        failure_reason=OPERATOR_REJECTED,
        attempt_final=True,
        payout_status=PayoutStatus.FAILED,
        publish=False,
        publish_count=None,
    ),
}


def resolve_transition(action: ResolveAction) -> ResolveTransition:
    return _TRANSITIONS[action]
