"""``X-Api-Key`` check (design §3.3; PAY-412). The key is never logged."""

# ruff: noqa: N818  (the design's error name)

from __future__ import annotations

import logging
from hmac import compare_digest
from typing import Annotated, Final

from fastapi import Depends, Header, Request
from pycommon.errors import AppError
from pycommon.logging import get_logger, log_event

from bank_stub.runtime import Runtime, get_runtime

__all__ = ["InvalidApiKey", "require_api_key"]

_log: Final = get_logger("bank_stub.auth")


class InvalidApiKey(AppError):
    status = 401
    code = "invalid_api_key"
    default_message = "API key missing or invalid."


async def require_api_key(
    request: Request,
    rt: Annotated[Runtime, Depends(get_runtime)],
    x_api_key: Annotated[str | None, Header(alias="X-Api-Key")] = None,
) -> None:
    expected = rt.settings.bank_stub_api_key.get_secret_value().encode()
    if x_api_key is None:
        reason = "missing"
    elif compare_digest(x_api_key.encode(), expected):
        return
    else:
        reason = "mismatch"
    route = getattr(request.scope.get("route"), "path", None) or request.url.path
    log_event(_log, logging.WARNING, "bank_stub.auth.rejected", reason=reason, path=route)
    raise InvalidApiKey()
