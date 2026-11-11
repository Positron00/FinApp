"""``decide_result``, ``instruction_allowed`` and the run guards (design §3.3)."""

from __future__ import annotations

import pytest
from payroll.domain import PayRunLocked, PayRunNotApprovable
from payroll.domain.state import (
    AttemptStatus,
    ResultKind,
    ResultOutcome,
    RunStatus,
    assert_run_approvable,
    assert_run_mutable,
    decide_result,
    instruction_allowed,
)

A, OC, K = AttemptStatus, ResultOutcome, ResultKind

TABLE = [
    # status requested / indeterminate: every outcome applies
    *[(s, OC.SETTLED, K.APPLY, A.SETTLED) for s in (A.REQUESTED, A.INDETERMINATE)],
    *[(s, OC.REJECTED, K.APPLY, A.REJECTED) for s in (A.REQUESTED, A.INDETERMINATE)],
    *[(s, OC.INDETERMINATE, K.APPLY, A.INDETERMINATE) for s in (A.REQUESTED, A.INDETERMINATE)],
    *[(s, OC.POISON, K.APPLY, A.NEEDS_OPERATOR) for s in (A.REQUESTED, A.INDETERMINATE)],
    # final: stored, whatever the outcome
    *[(s, o, K.STORED, None) for s in (A.SETTLED, A.REJECTED) for o in OC],
    # needs_operator: a definite bank answer applies; indeterminate and poison are stored (R7)
    (A.NEEDS_OPERATOR, OC.SETTLED, K.APPLY, A.SETTLED),
    (A.NEEDS_OPERATOR, OC.REJECTED, K.APPLY, A.REJECTED),
    (A.NEEDS_OPERATOR, OC.INDETERMINATE, K.STORED, None),
    (A.NEEDS_OPERATOR, OC.POISON, K.STORED, None),
]


@pytest.mark.parametrize(("status", "outcome", "kind", "new"), TABLE)
def test_decide_result_table(
    status: AttemptStatus, outcome: ResultOutcome, kind: ResultKind, new: AttemptStatus | None
) -> None:
    """PAY-405: every row and column of the §3.3 decision table, including needs_operator."""
    d = decide_result(current_attempt=1, attempt=1, attempt_status=status, outcome=outcome)
    assert (d.kind, d.new_status) == (kind, new)


@pytest.mark.parametrize("outcome", list(OC))
@pytest.mark.parametrize("status", [*A, None])
def test_stale_or_missing_attempt_is_not_current(
    outcome: ResultOutcome, status: AttemptStatus | None
) -> None:
    """PAY-405: attempt != current_attempt, or a missing attempt row → NOT_CURRENT (409)."""
    stale = decide_result(current_attempt=2, attempt=1, attempt_status=status, outcome=outcome)
    missing = decide_result(current_attempt=1, attempt=1, attempt_status=None, outcome=outcome)
    assert stale.kind is K.NOT_CURRENT
    assert missing.kind is K.NOT_CURRENT


@pytest.mark.parametrize("status", [*A, None])
def test_instruction_allowed(status: AttemptStatus | None) -> None:
    """PAY-411: only the current attempt in requested or indeterminate is served."""
    live = status in (A.REQUESTED, A.INDETERMINATE)
    assert instruction_allowed(current_attempt=1, attempt=1, attempt_status=status) is live
    assert instruction_allowed(current_attempt=2, attempt=1, attempt_status=status) is False


@pytest.mark.parametrize("status", list(RunStatus))
def test_run_guards(status: RunStatus) -> None:
    """PAY-301/304: only draft or calculated runs are mutable; PAY-400: only calculated approves."""
    if status in (RunStatus.DRAFT, RunStatus.CALCULATED):
        assert_run_mutable(status)
    else:
        with pytest.raises(PayRunLocked) as locked:
            assert_run_mutable(status)
        assert locked.value.details == {"status": str(status)}
    if status is RunStatus.CALCULATED:
        assert_run_approvable(status)
    else:
        with pytest.raises(PayRunNotApprovable):
            assert_run_approvable(status)
