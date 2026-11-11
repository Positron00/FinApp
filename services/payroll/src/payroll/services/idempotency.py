"""Idempotency-Key handling (design §4.8; PAY-400, PAY-405, C34).

The first statements of the handler's transaction, before any domain lock: delete an expired
row, insert a placeholder (a concurrent duplicate blocks on the unique index until the first
transaction ends), else compare hashes and replay. A 2xx stores the response in the same
transaction; any error rolls the key back, so errors are never replayed.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

from pycommon.logging import get_logger, log_event
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from payroll.api.errors import IdempotencyKeyReused

__all__ = ["IdemRequest", "Replay", "begin", "canonical_params", "request_hash", "scope", "store"]

_log: Final = get_logger("payroll.idempotency")

_DELETE_EXPIRED: Final = text(
    "DELETE FROM payroll.idempotency_keys WHERE company_id = :cid AND scope = :scope "
    "AND key = :key AND expires_at < now()"
)
_INSERT: Final = text(
    "INSERT INTO payroll.idempotency_keys "
    "(company_id, scope, key, request_hash, response_status, response_body, expires_at) "
    "VALUES (:cid, :scope, :key, :hash, 0, 'null'::jsonb, now() + make_interval(hours => :ttl)) "
    "ON CONFLICT DO NOTHING RETURNING key"
)
_SELECT: Final = text(
    "SELECT request_hash, response_status, response_body FROM payroll.idempotency_keys "
    "WHERE company_id = :cid AND scope = :scope AND key = :key"
)
_STORE: Final = text(
    "UPDATE payroll.idempotency_keys SET response_status = :status, "
    "response_body = CAST(:body AS jsonb), updated_at = now() "
    "WHERE company_id = :cid AND scope = :scope AND key = :key"
)


def canonical_params(params: Mapping[str, Any]) -> str:
    return ",".join(f"{k}={params[k]}" for k in sorted(params))


def scope(iss: str, sub: str, method: str, route: str, params: Mapping[str, Any]) -> str:
    """``<iss>|<sub>|<METHOD> <route template>|<sorted path params k=v joined by ','>``."""
    return f"{iss}|{sub}|{method.upper()} {route}|{canonical_params(params)}"


def request_hash(method: str, route: str, params: Mapping[str, Any], body: Any | None) -> str:
    canonical_body = "" if body is None else json.dumps(body, sort_keys=True, separators=(",", ":"))
    material = "\n".join((method.upper(), route, canonical_params(params), canonical_body))
    return hashlib.sha256(material.encode()).hexdigest()


@dataclass(frozen=True)
class IdemRequest:
    company_id: UUID
    scope: str
    key: str
    request_hash: str
    route: str
    ttl_h: int


@dataclass(frozen=True)
class Replay:
    status: int
    body: Any


def _params(req: IdemRequest) -> dict[str, Any]:
    return {"cid": req.company_id, "scope": req.scope, "key": req.key}


async def begin(session: AsyncSession, req: IdemRequest) -> Replay | None:
    """``None``: run the handler. A ``Replay``: return it and run nothing else."""
    await session.execute(_DELETE_EXPIRED, _params(req))
    inserted = (
        await session.execute(_INSERT, {**_params(req), "hash": req.request_hash, "ttl": req.ttl_h})
    ).first()
    if inserted is not None:
        return None
    row = (await session.execute(_SELECT, _params(req))).one()
    if row.request_hash != req.request_hash:
        log_event(_log, logging.WARNING, "idempotency.key_reused", route=req.route)
        raise IdempotencyKeyReused()
    log_event(
        _log, logging.INFO, "idempotency.replayed", route=req.route, status=row.response_status
    )
    return Replay(int(row.response_status), row.response_body)


async def store(session: AsyncSession, req: IdemRequest, status: int, body: Any) -> None:
    await session.execute(
        _STORE, {**_params(req), "status": status, "body": json.dumps(body, default=str)}
    )
