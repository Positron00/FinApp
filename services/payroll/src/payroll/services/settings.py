"""Company settings use cases (design §4.2; PAY-200, R33)."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Final
from uuid import UUID

from pycommon.auth import Principal
from pycommon.logging import get_logger, log_event

from payroll.api.schemas import CompanySettingsIn, CompanySettingsOut
from payroll.db.repositories import AuditRepo, SettingsRepo
from payroll.db.session import session_scope
from payroll.domain import AnchorLocked
from payroll.services.runtime import Runtime

__all__ = ["get_settings", "put_settings"]

_log: Final = get_logger("payroll.settings")
FIELDS: Final = (
    "name",
    "tax_rate_bps",
    "retirement_bps",
    "timezone",
    "biweekly_anchor",
    "require_dual_approval",
)


def _jsonable(v: Any) -> Any:
    return v.isoformat() if isinstance(v, date) else v


async def get_settings(rt: Runtime, company_id: UUID) -> CompanySettingsOut:
    async with session_scope(rt.sessions, "settings.get") as session:
        row = await SettingsRepo.get(session, company_id)
        return CompanySettingsOut.model_validate(row)


async def put_settings(
    rt: Runtime, principal: Principal, body: CompanySettingsIn
) -> CompanySettingsOut:
    """Full replace under ``FOR UPDATE``; ``anchor_locked`` is checked after the row lock."""
    cid = principal.cid
    async with session_scope(rt.sessions, "settings.put") as session:
        row = await SettingsRepo.get(session, cid, lock="update")
        if body.biweekly_anchor != row.biweekly_anchor and await SettingsRepo.biweekly_run_exists(
            session, cid
        ):
            log_event(
                _log, logging.INFO, "settings.anchor_locked", company_id=str(cid),
                actor_id=principal.sub,
            )  # fmt: skip
            raise AnchorLocked()
        changed: dict[str, dict[str, Any]] = {}
        for field in FIELDS:
            old, new = getattr(row, field), getattr(body, field)
            if old != new:
                changed[field] = {"old": _jsonable(old), "new": _jsonable(new)}
                setattr(row, field, new)
        if changed:
            row.updated_by = principal.sub
            await session.flush()
            await session.refresh(row)
            await AuditRepo.add(
                session,
                cid,
                actor_id=principal.sub,
                event="settings.updated",
                entity_type="company_settings",
                entity_id=cid,
                details={"changed": changed},
            )
            log_event(
                _log,
                logging.INFO,
                "settings.updated",
                company_id=str(cid),
                actor_id=principal.sub,
                fields=sorted(changed),
            )
        return CompanySettingsOut.model_validate(row)
