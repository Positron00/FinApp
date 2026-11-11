"""Audit a dual-approval denial (design §2.8, §4.4; PAY-401, PAY-500, ADR 0005, C73).

The 403 ``approver_must_differ`` rolls the approve or retry transaction back, so the denial is
appended in a transaction of its own afterwards: ``payrun.approve_denied`` on the run or
``payout.retry_denied`` on the payout, with the rule, the employee count and the action. The actor
is the row's ``actor_id``. If that write fails, the failure is logged and the 403 still stands.
"""

from __future__ import annotations

import logging
from typing import Final, Literal
from uuid import UUID

from pycommon.auth import Principal
from pycommon.logging import get_logger, log_event

from payroll.db.repositories import AuditRepo
from payroll.db.session import session_scope
from payroll.domain import ApproverMustDiffer
from payroll.services.runtime import Runtime

__all__ = ["APPROVE_DENIED", "RETRY_DENIED", "record_dual_denied"]

APPROVE_DENIED: Final = "payrun.approve_denied"
RETRY_DENIED: Final = "payout.retry_denied"
_log: Final = get_logger("payroll.audit")


async def record_dual_denied(
    rt: Runtime,
    principal: Principal,
    exc: ApproverMustDiffer,
    *,
    action: Literal["approve", "retry"],
    entity_id: UUID,
    pay_run_id: UUID,
) -> None:
    """Append the denial row (IDs and codes only, plan §6). Never raises."""
    event = APPROVE_DENIED if action == "approve" else RETRY_DENIED
    details: dict[str, object] = {
        "rule": str(exc.rule),
        "employee_count": len(exc.employee_ids),
    }
    if action == "retry":
        details["pay_run_id"] = str(pay_run_id)
    try:
        async with session_scope(rt.sessions, f"audit.{action}_denied") as session:
            await AuditRepo.add(
                session, principal.cid, actor_id=principal.sub, event=event,
                entity_type="pay_run" if action == "approve" else "payout",
                entity_id=entity_id, details=details,
            )  # fmt: skip
    except Exception as err:  # the 403 must stand; the write failure is logged
        log_event(
            _log, logging.ERROR, "audit.write_failed", audit_event=event,
            entity_id=str(entity_id), error=type(err).__name__,
        )  # fmt: skip
