"""Documented error responses for the OpenAPI contract (identity LLD §3.4-3.5)."""

from __future__ import annotations

from typing import Any

from pycommon.errors import ErrorBody

__all__ = ["errors"]

_DESCRIPTIONS: dict[int, str] = {
    400: "Bad request (see the error code)",
    401: "invalid_token, or invalid_credentials on login",
    403: "forbidden_role",
    404: "Not found (see the error code)",
    409: "Conflict (see the error code)",
    413: "payload_too_large",
    422: "validation_error (details [{field, issue}], never the input) or password_too_weak",
    429: "rate_limited, with Retry-After",
    503: "service_unavailable, db_timeout or db_unavailable",
}
COMMON = (401, 413, 422, 503)


def errors(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """``responses=`` for a route: the common errors plus ``statuses``, all with ``ErrorBody``."""
    codes = sorted({*COMMON, *statuses})
    return {c: {"model": ErrorBody, "description": _DESCRIPTIONS[c]} for c in codes}
