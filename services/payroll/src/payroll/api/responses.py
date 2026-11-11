"""OpenAPI error responses: every route documents its codes with the shared error schema."""

from __future__ import annotations

from typing import Any

from pycommon.errors import ErrorBody

__all__ = ["errors"]

_COMMON = (401, 403, 422, 429, 500, 503)


def errors(*statuses: int) -> dict[int | str, dict[str, Any]]:
    return {s: {"model": ErrorBody} for s in sorted({*_COMMON, *statuses})}
