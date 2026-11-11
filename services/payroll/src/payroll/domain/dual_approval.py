"""Dual approval (design §3.4; PAY-401, PAY-500, ADR 0005)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from payroll.domain import ApproverMustDiffer

__all__ = ["BankEdit", "DualApprovalRule", "check_approve_dual", "check_retry_dual"]


@dataclass(frozen=True, slots=True)
class BankEdit:
    employee_id: UUID
    bank_updated_by: str | None
    bank_updated_at: datetime | None


class DualApprovalRule(StrEnum):
    CALCULATOR = "calculator"
    BANK_EDITOR = "bank_editor"


def check_approve_dual(
    *,
    require_dual_approval: bool,
    approver_id: str,
    calculated_by: str,
    run_created_at: datetime,
    bank_edits: Iterable[BankEdit],
) -> None:
    """With the snapshot on, the approver is neither the calculator nor a recent bank editor."""
    if not require_dual_approval:
        return
    if approver_id == calculated_by:
        raise ApproverMustDiffer(rule=DualApprovalRule.CALCULATOR, employee_ids=[])
    hits = sorted(
        e.employee_id
        for e in bank_edits
        if e.bank_updated_by == approver_id
        and e.bank_updated_at is not None
        and e.bank_updated_at > run_created_at
    )
    if hits:
        raise ApproverMustDiffer(rule=DualApprovalRule.BANK_EDITOR, employee_ids=hits)


def check_retry_dual(
    *, require_dual_approval: bool, retrier_id: str, bank_updated_by: str | None, employee_id: UUID
) -> None:
    if require_dual_approval and bank_updated_by == retrier_id:
        raise ApproverMustDiffer(rule=DualApprovalRule.BANK_EDITOR, employee_ids=[employee_id])
