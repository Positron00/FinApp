"""Dual approval (design §3.4; PAY-401, PAY-500, PAY-502, ADR 0005, C73).

With the run's ``require_dual_approval`` snapshot on, nobody may both prepare and release money:

- **approve:** the approver is not the run's calculator, nor its creator, nor the last editor of any
  hours row of the run, nor the last bank-detail editor of any employee in the run who gets a
  transfer (net above 0) and whose bank details changed since they were last vetted under
  four-eyes (C73);
- **retry:** the retrier is not the employee's last bank-detail editor under the same window, nor
  the operator who marked the current attempt rejected (C73).

A bank edit is *vetted* once a payout attempt took a snapshot of it in a dual-approval run and the
attempt was requested by someone other than the editor (``BankEdit.vetted_at``, computed by
``DualApprovalRepo.bank_edits``). An employee never paid that way has every edit unvetted.

Both times come from the database's ``clock_timestamp()`` read after the employee row lock (the
edit at its UPDATE, the snapshot after approve's or retry's ``FOR SHARE``), so the locks order
them: a snapshot that didn't see an edit is strictly earlier than it. A tie can only be clock
resolution, where the order is unknown, so it counts as unvetted (fail closed).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from payroll.domain import ApproverMustDiffer

__all__ = [
    "BankEdit",
    "DualApprovalRule",
    "HoursEdit",
    "check_approve_dual",
    "check_retry_dual",
]


@dataclass(frozen=True, slots=True)
class BankEdit:
    """The employee's last bank-detail edit and the time it was last vetted (C73).

    ``vetted_at`` is the latest ``requested_at`` of an attempt for this employee in a run with
    ``require_dual_approval`` that was requested by someone other than ``bank_updated_by``, or
    ``None`` when there is none."""

    employee_id: UUID
    bank_updated_by: str | None
    bank_updated_at: datetime | None
    vetted_at: datetime | None = None

    def unvetted_by(self, actor_id: str) -> bool:
        """``actor_id`` made the last bank edit and no four-eyes snapshot is later than it (a
        snapshot at the same instant doesn't vet: the order is unknown)."""
        return (
            self.bank_updated_by == actor_id
            and self.bank_updated_at is not None
            and (self.vetted_at is None or self.bank_updated_at >= self.vetted_at)
        )


@dataclass(frozen=True, slots=True)
class HoursEdit:
    """One ``pay_run_hours`` row: who last entered this employee's hours on the run."""

    employee_id: UUID
    entered_by: str


class DualApprovalRule(StrEnum):
    """The ``details.rule`` of 403 ``approver_must_differ``. Internal: the OpenAPI types
    ``details`` as free-form, and neither the BFF nor the UI reads it (C73)."""

    CALCULATOR = "calculator"
    CREATOR = "creator"
    HOURS_EDITOR = "hours_editor"
    BANK_EDITOR = "bank_editor"
    RESOLVER = "resolver"


def check_approve_dual(
    *,
    require_dual_approval: bool,
    approver_id: str,
    calculated_by: str,
    created_by: str,
    hours_edits: Iterable[HoursEdit],
    bank_edits: Iterable[BankEdit],
) -> None:
    """PAY-401, C73, in this fixed order: calculator → creator → hours editor → bank editor."""
    if not require_dual_approval:
        return
    if approver_id == calculated_by:
        raise ApproverMustDiffer(rule=DualApprovalRule.CALCULATOR, employee_ids=[])
    if approver_id == created_by:
        raise ApproverMustDiffer(rule=DualApprovalRule.CREATOR, employee_ids=[])
    hours = [h.employee_id for h in hours_edits if h.entered_by == approver_id]
    if hours:
        raise ApproverMustDiffer(rule=DualApprovalRule.HOURS_EDITOR, employee_ids=hours)
    banks = [e.employee_id for e in bank_edits if e.unvetted_by(approver_id)]
    if banks:
        raise ApproverMustDiffer(rule=DualApprovalRule.BANK_EDITOR, employee_ids=banks)


def check_retry_dual(
    *,
    require_dual_approval: bool,
    retrier_id: str,
    bank_edit: BankEdit,
    resolved_by: str | None,
) -> None:
    """PAY-500, PAY-502, C73: bank editor (same window as approve) → resolver. ``resolved_by`` is
    the operator who marked the current attempt rejected, or ``None`` for a bank rejection."""
    if not require_dual_approval:
        return
    if bank_edit.unvetted_by(retrier_id):
        raise ApproverMustDiffer(
            rule=DualApprovalRule.BANK_EDITOR, employee_ids=[bank_edit.employee_id]
        )
    if resolved_by is not None and resolved_by == retrier_id:
        raise ApproverMustDiffer(rule=DualApprovalRule.RESOLVER, employee_ids=[])
