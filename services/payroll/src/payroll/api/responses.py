"""OpenAPI error responses: every route documents its codes with the shared error schema."""

from __future__ import annotations

from typing import Any

from pycommon.errors import ErrorBody

__all__ = ["errors"]

_COMMON = (401, 403, 422, 429, 500, 503)
_RETRY_AFTER: dict[str, Any] = {
    "Retry-After": {
        "description": "Whole seconds to wait before the request can succeed (sent with the "
        "codes that carry details.retry_after_s, e.g. resolve_too_early, C48).",
        "schema": {"type": "integer", "minimum": 1},
    }
}


def errors(*statuses: int, retry_after: tuple[int, ...] = ()) -> dict[int | str, dict[str, Any]]:
    """``responses=`` for a route: the common codes plus ``statuses``. Routes that take a JSON body
    list 400: a body that isn't valid JSON is 400 ``bad_request`` (Schemathesis ST103, S8).
    The statuses in ``retry_after`` also document the ``Retry-After`` header (PAY-502)."""
    out: dict[int | str, dict[str, Any]] = {
        s: {"model": ErrorBody} for s in sorted({*_COMMON, *statuses, *retry_after})
    }
    for s in retry_after:
        out[s]["headers"] = _RETRY_AFTER
    return out
