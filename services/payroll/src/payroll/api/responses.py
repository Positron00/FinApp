"""OpenAPI error responses: every route documents its codes with the shared error schema."""

from __future__ import annotations

from typing import Any

from pycommon.errors import ErrorBody

__all__ = ["errors"]

_COMMON = (401, 403, 422, 429, 500, 503)


def errors(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """``responses=`` for a route: the common codes plus ``statuses``. Routes that take a JSON body
    list 400: a body that isn't valid JSON is 400 ``bad_request`` (Schemathesis ST103, S8)."""
    return {s: {"model": ErrorBody} for s in sorted({*_COMMON, *statuses})}
