"""The auth audit log, newest first (identity LLD §3.4; PAY-109, PAY-NFR-08)."""

from __future__ import annotations

from uuid import UUID

from identity.api.pagination import decode_cursor, encode_cursor
from identity.api.schemas import AuditEntryOut, AuditListOut
from identity.db.repositories import AuditRepo
from identity.runtime import IdentityRuntime

__all__ = ["list_audit"]


async def list_audit(
    rt: IdentityRuntime, cid: UUID, cursor: str | None, limit: int
) -> AuditListOut:
    """Rows hold IDs, ``ip_hash`` and non-personal ``details`` only (§2.6)."""
    before = decode_cursor(cursor)
    async with rt.sessions() as session:
        rows = await AuditRepo(session).list_page(cid, before=before, limit=limit)
    page = rows[:limit]
    items = [
        AuditEntryOut(
            id=r.id,
            at=r.at,
            actor_user_id=r.actor_user_id,
            event=r.event,
            target_user_id=r.target_user_id,
            ip_hash=r.ip_hash,
            details=dict(r.details),
        )
        for r in page
    ]
    next_cursor = encode_cursor(page[-1].id) if len(rows) > limit else None
    return AuditListOut(items=items, next_cursor=next_cursor)
