"""Opaque list cursors (identity LLD §3.3; R4, C8, C14).

A cursor is the base64url (no padding) of the last row's 16 UUID bytes: 22 characters, no
personal data. Anything else is 422 ``validation_error`` with ``query.cursor`` /
``invalid_cursor``, and the input is never echoed.
"""

from __future__ import annotations

import base64
import binascii
import re
from typing import Annotated, Final
from uuid import UUID

from fastapi import Query
from pycommon.errors import ValidationFailed

__all__ = ["Cursor", "Limit", "decode_cursor", "encode_cursor"]

_CURSOR_RE: Final = re.compile(r"[A-Za-z0-9_-]{22}")

Limit = Annotated[int, Query(ge=1, le=100)]
Cursor = Annotated[str | None, Query(max_length=64)]


def encode_cursor(last_id: UUID) -> str:
    return base64.urlsafe_b64encode(last_id.bytes).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> UUID | None:
    """The row ID to continue after; ``None`` for the first page."""
    if cursor is None:
        return None
    if _CURSOR_RE.fullmatch(cursor) is not None:
        try:
            raw = base64.urlsafe_b64decode(cursor + "==")
        except binascii.Error:  # pragma: no cover  (the pattern admits only valid base64url)
            raw = b""
        # Canonical form only, so one row has exactly one cursor.
        if len(raw) == 16 and encode_cursor(UUID(bytes=raw)) == cursor:
            return UUID(bytes=raw)
    raise ValidationFailed(details=[{"field": "query.cursor", "issue": "invalid_cursor"}])
