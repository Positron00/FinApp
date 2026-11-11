"""Route guards and request helpers (design §4.8, §8; PAY-600, PAY-601, PAY-411, C30, C34).

Every public router declares ``Depends(require_web_user)`` and every route a role check; the
internal router declares ``Depends(require_worker)``. pycommon has already verified the token.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Final

from fastapi import Depends, Header, Request
from pycommon.auth import Principal, require
from pycommon.errors import Forbidden

from payroll.api.errors import IdempotencyKeyRequired
from payroll.api.schemas import IDEM_KEY_PATTERN
from payroll.services.idempotency import IdemRequest, request_hash, scope
from payroll.services.runtime import Runtime

__all__ = [
    "ADMIN",
    "ANY_ROLE",
    "APPROVER",
    "KNOWN_ROLES",
    "PAYROLL_ADMIN",
    "IdempotencyHeader",
    "get_runtime",
    "idem_request",
    "optional_idem_request",
    "require_roles",
    "require_web_user",
    "require_worker",
]

ADMIN: Final = "admin"
PAYROLL_ADMIN: Final = "payroll_admin"
APPROVER: Final = "approver"
KNOWN_ROLES: Final = frozenset({ADMIN, PAYROLL_ADMIN, APPROVER})
ANY_ROLE: Final = (ADMIN, PAYROLL_ADMIN, APPROVER)
_IDEM_RE: Final = re.compile(IDEM_KEY_PATTERN)

_web_user = require(issuers=("web",), subjects=("user",))
_worker = require(issuers=("payout-worker",), subjects=("service:payout-worker",))


async def require_web_user(principal: Principal = Depends(_web_user)) -> Principal:  # noqa: B008
    """``iss=web`` user tokens only: another issuer 401, ``service:web`` 403, unknown role 403."""
    if not principal.roles <= KNOWN_ROLES:
        raise Forbidden()
    return principal


async def require_worker(principal: Principal = Depends(_worker)) -> Principal:  # noqa: B008
    """``iss=payout-worker`` and ``sub=service:payout-worker`` (PAY-411, C30)."""
    return principal


def require_roles(*roles: str) -> Callable[..., Awaitable[Principal]]:
    """403 ``forbidden_role`` unless the token holds one of ``roles`` (roles are additive)."""
    wanted = frozenset(roles)

    async def _require_roles(
        principal: Principal = Depends(require_web_user),  # noqa: B008
    ) -> Principal:
        if not principal.roles & wanted:
            raise Forbidden()
        return principal

    return _require_roles


def get_runtime(request: Request) -> Runtime:
    rt = getattr(request.app.state, "payroll", None)
    if not isinstance(rt, Runtime):
        raise RuntimeError("the payroll app has not started")
    return rt


# The pattern is checked by FastAPI, so a malformed key is 422 with
# ``[{field: "header.Idempotency-Key", issue: "string_pattern_mismatch"}]`` (C34, C56).
IdempotencyHeader = Annotated[
    str | None, Header(alias="Idempotency-Key", pattern=IDEM_KEY_PATTERN, max_length=200)
]


def idem_request(
    request: Request,
    principal: Principal,
    key: str | None,
    rt: Runtime,
    *,
    body: Any | None = None,
) -> IdemRequest:
    """Absent → 400 ``idempotency_key_required``; scope and hash per §4.8."""
    if key is None:
        raise IdempotencyKeyRequired()
    return _idem(request, principal, key, rt, body)


def optional_idem_request(
    request: Request, principal: Principal, key: str | None, rt: Runtime
) -> IdemRequest | None:
    """Retry's optional key (§4.8, C52): honoured when present, ``None`` when absent."""
    return None if key is None else _idem(request, principal, key, rt, None)


def _idem(
    request: Request, principal: Principal, key: str, rt: Runtime, body: Any | None
) -> IdemRequest:
    route = str(getattr(request.scope.get("route"), "path", request.url.path))
    params = {k: str(v) for k, v in request.path_params.items()}
    return IdemRequest(
        company_id=principal.cid,
        scope=scope(principal.iss, principal.sub, request.method, route, params),
        key=key,
        request_hash=request_hash(request.method, route, params, body),
        route=route,
        ttl_h=rt.settings.idempotency_ttl_h,
    )
