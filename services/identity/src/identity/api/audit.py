"""``GET /v1/audit-log`` (identity LLD §3.4; PAY-109). HTTP mapping only."""

from __future__ import annotations

from fastapi import APIRouter

from identity.api.deps import AdminActor, Runtime
from identity.api.pagination import Cursor, Limit
from identity.api.responses import errors
from identity.api.schemas import AuditListOut
from identity.usecases.audit import list_audit

__all__ = ["router"]

router = APIRouter(prefix="/v1/audit-log", tags=["audit"])


@router.get(
    "",
    operation_id="audit_log_list",
    response_model=AuditListOut,
    responses=errors(403, 429),
    summary="The auth audit log, newest first (admin)",
)
async def audit_log_list(
    actor: AdminActor, rt: Runtime, limit: Limit = 50, cursor: Cursor = None
) -> AuditListOut:
    return await list_audit(rt, actor.company_id, cursor, limit)
