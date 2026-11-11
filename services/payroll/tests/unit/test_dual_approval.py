"""Dual approval rules (design §3.4; PAY-401, PAY-500, PAY-502, ADR 0005, C73)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from payroll.domain import ApproverMustDiffer
from payroll.domain.dual_approval import (
    BankEdit,
    DualApprovalRule,
    HoursEdit,
    check_approve_dual,
    check_retry_dual,
)

T0 = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
CALC, CREATOR, APPROVER, OTHER = "calc-user", "creator-user", "approver-user", "other-user"


def _approve(**over: Any) -> None:
    kw: dict[str, Any] = {
        "require_dual_approval": True,
        "approver_id": APPROVER,
        "calculated_by": CALC,
        "created_by": CREATOR,
        "hours_edits": [],
        "bank_edits": [],
    }
    kw.update(over)
    check_approve_dual(**kw)


def _rule(err: pytest.ExceptionInfo[ApproverMustDiffer]) -> Any:
    return err.value.details


def test_rule_values_are_stable() -> None:
    """PAY-401, C73: the details.rule strings (internal; free-form `details` in the OpenAPI)."""
    assert [str(r) for r in DualApprovalRule] == [
        "calculator", "creator", "hours_editor", "bank_editor", "resolver",
    ]  # fmt: skip


def test_off_always_passes() -> None:
    """PAY-401: with the run's snapshot off, the calculator, creator, hours and bank editor may
    approve."""
    _approve(
        require_dual_approval=False,
        approver_id=CALC,
        created_by=CALC,
        hours_edits=[HoursEdit(uuid4(), CALC)],
        bank_edits=[BankEdit(uuid4(), CALC, T0)],
    )


def test_calculator_cannot_approve() -> None:
    """PAY-401 (a): approver == calculated_by → 403 approver_must_differ, rule calculator."""
    with pytest.raises(ApproverMustDiffer) as err:
        _approve(approver_id=CALC)
    assert _rule(err) == {"rule": "calculator", "employee_ids": []}


def test_creator_cannot_approve() -> None:
    """PAY-401 (b), C73: approver == the run's created_by → rule creator."""
    with pytest.raises(ApproverMustDiffer) as err:
        _approve(approver_id=CREATOR)
    assert _rule(err) == {"rule": "creator", "employee_ids": []}


def test_hours_editor_cannot_approve() -> None:
    """PAY-401 (c), C73: the last editor of any hours row of the run → rule hours_editor, with
    those employees; rows entered by someone else don't count."""
    mine = [uuid4(), uuid4()]
    edits = [HoursEdit(mine[0], APPROVER), HoursEdit(uuid4(), OTHER), HoursEdit(mine[1], APPROVER)]
    with pytest.raises(ApproverMustDiffer) as err:
        _approve(hours_edits=edits)
    assert _rule(err) == {"rule": "hours_editor", "employee_ids": sorted(map(str, mine))}
    _approve(hours_edits=[HoursEdit(uuid4(), OTHER)])


def test_bank_edit_never_vetted_blocks_however_old() -> None:
    """PAY-401 (d), C73: the bypass. A bank edit made long before the run was created (Monday;
    run Tuesday), never paid out under four-eyes, still blocks its editor."""
    ghost = uuid4()
    with pytest.raises(ApproverMustDiffer) as err:
        _approve(bank_edits=[BankEdit(ghost, APPROVER, T0 - timedelta(days=30), vetted_at=None)])
    assert _rule(err) == {"rule": "bank_editor", "employee_ids": [str(ghost)]}


def test_bank_edit_window_is_since_last_vetted_snapshot() -> None:
    """PAY-401 (d), C73: an edit after the employee's last four-eyes snapshot blocks, and so does
    one at the same instant (both are clock_timestamp() after the row lock, so a tie is clock
    resolution with an unknown order: fail closed); an edit covered by a later snapshot doesn't;
    another editor's edits and no edit don't."""
    late = [uuid4(), uuid4(), uuid4()]
    edits = [
        BankEdit(late[0], APPROVER, T0 + timedelta(seconds=1), vetted_at=T0),
        BankEdit(late[1], APPROVER, T0, vetted_at=None),
        BankEdit(late[2], APPROVER, T0, vetted_at=T0),  # snapshot at the edit: not vetted
        BankEdit(uuid4(), APPROVER, T0, vetted_at=T0 + timedelta(microseconds=1)),
        BankEdit(uuid4(), APPROVER, T0, vetted_at=T0 + timedelta(days=1)),
        BankEdit(uuid4(), APPROVER, None),
        BankEdit(uuid4(), OTHER, T0 + timedelta(days=1)),
    ]
    with pytest.raises(ApproverMustDiffer) as err:
        _approve(bank_edits=edits)
    assert _rule(err) == {"rule": "bank_editor", "employee_ids": sorted(map(str, late))}
    _approve(bank_edits=edits[3:])


def test_approve_rule_order() -> None:
    """PAY-401, C73: calculator → creator → hours editor → bank editor; the first hit wins."""
    everything: dict[str, Any] = {
        "approver_id": APPROVER,
        "calculated_by": APPROVER,
        "created_by": APPROVER,
        "hours_edits": [HoursEdit(uuid4(), APPROVER)],
        "bank_edits": [BankEdit(uuid4(), APPROVER, T0)],
    }
    expected = ["calculator", "creator", "hours_editor", "bank_editor"]
    clears = ["calculated_by", "created_by", "hours_edits", None]
    for rule, clear in zip(expected, clears, strict=True):
        with pytest.raises(ApproverMustDiffer) as err:
            _approve(**everything)
        assert err.value.rule == rule
        if clear is not None:
            everything[clear] = [] if clear == "hours_edits" else OTHER
    everything["bank_edits"] = []
    _approve(**everything)


def _retry(**over: Any) -> None:
    kw: dict[str, Any] = {
        "require_dual_approval": True,
        "retrier_id": APPROVER,
        "bank_edit": BankEdit(uuid4(), OTHER, T0),
        "resolved_by": None,
    }
    kw.update(over)
    check_retry_dual(**kw)


def test_retry_off_always_passes() -> None:
    """PAY-500: with the snapshot off, the bank editor and the resolver may retry."""
    _retry(
        require_dual_approval=False,
        bank_edit=BankEdit(uuid4(), APPROVER, T0),
        resolved_by=APPROVER,
    )


def test_retry_bank_editor_uses_the_vetting_window() -> None:
    """PAY-500, C73: the retrier can't be the employee's bank editor since the last four-eyes
    snapshot; an edit a second person already released doesn't block."""
    emp = uuid4()
    with pytest.raises(ApproverMustDiffer) as err:
        _retry(bank_edit=BankEdit(emp, APPROVER, T0 + timedelta(hours=1), vetted_at=T0))
    assert _rule(err) == {"rule": "bank_editor", "employee_ids": [str(emp)]}
    with pytest.raises(ApproverMustDiffer):
        _retry(bank_edit=BankEdit(emp, APPROVER, T0, vetted_at=None))
    _retry(bank_edit=BankEdit(emp, APPROVER, T0, vetted_at=T0 + timedelta(hours=1)))
    _retry(bank_edit=BankEdit(emp, None, None))


def test_retry_resolver_cannot_retry() -> None:
    """PAY-500, PAY-502, C73: the operator who marked the attempt rejected can't retry it (rule
    resolver); someone else can; a bank rejection (no resolver) doesn't apply."""
    with pytest.raises(ApproverMustDiffer) as err:
        _retry(resolved_by=APPROVER)
    assert _rule(err) == {"rule": "resolver", "employee_ids": []}
    _retry(resolved_by=OTHER)
    _retry(resolved_by=None)


def test_retry_bank_editor_checked_before_resolver() -> None:
    """PAY-500, C73: the bank-editor rule is checked first."""
    with pytest.raises(ApproverMustDiffer) as err:
        _retry(bank_edit=BankEdit(uuid4(), APPROVER, T0), resolved_by=APPROVER)
    assert err.value.rule == "bank_editor"
