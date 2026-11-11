"""``GET /v1/audit-events`` (design §4.5, §4.9; admin only, newest first, cursor pages)."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pycommon.auth import Principal

from payroll.api.deps import ADMIN, get_runtime, require_roles, require_web_user
from payroll.api.responses import errors
from payroll.api.schemas import AuditEventName, AuditEventOut, EntityType, Limit, Page
from payroll.services import reads
from payroll.services.runtime import Runtime

__all__ = ["router"]

router = APIRouter(
    prefix="/v1/audit-events", tags=["audit"], dependencies=[Depends(require_web_user)]
)


@router.get("", response_model=Page[AuditEventOut], responses=errors())
async def list_audit_events(
    *,
    rt: Annotated[Runtime, Depends(get_runtime)],
    principal: Annotated[Principal, Depends(require_roles(ADMIN))],
    entity_type: Annotated[EntityType | None, Query()] = None,
    entity_id: Annotated[UUID | None, Query()] = None,
    event: Annotated[AuditEventName | None, Query()] = None,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[Limit, Query()] = 50,
) -> Page[AuditEventOut]:
    return await reads.list_audit_events(
        rt,
        principal.cid,
        entity_type=entity_type,
        entity_id=entity_id,
        event=event,
        cursor=cursor,
        limit=limit,
    )
