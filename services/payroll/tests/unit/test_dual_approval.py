"""Dual approval rules (design §3.4; PAY-401, PAY-500, ADR 0005)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from payroll.domain import ApproverMustDiffer
from payroll.domain.dual_approval import BankEdit, check_approve_dual, check_retry_dual

CREATED = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
CALC, APPROVER = "calc-user", "approver-user"


def test_off_always_passes() -> None:
    """PAY-401: with the run's snapshot off, the calculator may approve."""
    check_approve_dual(
        require_dual_approval=False,
        approver_id=CALC,
        calculated_by=CALC,
        run_created_at=CREATED,
        bank_edits=[BankEdit(uuid4(), CALC, CREATED + timedelta(hours=1))],
    )


def test_calculator_cannot_approve() -> None:
    """PAY-401: approver == calculated_by → 403 approver_must_differ, rule calculator."""
    with pytest.raises(ApproverMustDiffer) as err:
        check_approve_dual(
            require_dual_approval=True,
            approver_id=CALC,
            calculated_by=CALC,
            run_created_at=CREATED,
            bank_edits=[],
        )
    assert err.value.details == {"rule": "calculator", "employee_ids": []}


def test_bank_editor_after_created_at_is_blocked_and_before_is_allowed() -> None:
    """PAY-401: only bank edits after the run's created_at count; every hit is listed."""
    late = [uuid4(), uuid4()]
    edits = [
        BankEdit(late[0], APPROVER, CREATED + timedelta(seconds=1)),
        BankEdit(late[1], APPROVER, CREATED + timedelta(days=1)),
        BankEdit(uuid4(), APPROVER, CREATED - timedelta(seconds=1)),  # before: allowed
        BankEdit(uuid4(), APPROVER, None),
        BankEdit(uuid4(), "someone-else", CREATED + timedelta(hours=1)),
    ]
    with pytest.raises(ApproverMustDiffer) as err:
        check_approve_dual(
            require_dual_approval=True,
            approver_id=APPROVER,
            calculated_by=CALC,
            run_created_at=CREATED,
            bank_edits=edits,
        )
    assert err.value.details == {"rule": "bank_editor", "employee_ids": sorted(map(str, late))}
    check_approve_dual(
        require_dual_approval=True,
        approver_id=APPROVER,
        calculated_by=CALC,
        run_created_at=CREATED,
        bank_edits=edits[2:],
    )


def test_retry_dual_rule() -> None:
    """PAY-500: with the snapshot on, the last bank editor can't retry."""
    emp = uuid4()
    check_retry_dual(
        require_dual_approval=False, retrier_id=APPROVER, bank_updated_by=APPROVER, employee_id=emp
    )
    check_retry_dual(
        require_dual_approval=True, retrier_id=APPROVER, bank_updated_by=CALC, employee_id=emp
    )
    with pytest.raises(ApproverMustDiffer):
        check_retry_dual(
            require_dual_approval=True,
            retrier_id=APPROVER,
            bank_updated_by=APPROVER,
            employee_id=emp,
        )
