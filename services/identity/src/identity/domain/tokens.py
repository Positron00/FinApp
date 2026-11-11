"""Invite and reset tokens (identity LLD §6.7; PAY-101, PAY-103, PAY-105)."""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import secrets
from dataclasses import dataclass, field
from typing import Final

__all__ = ["TOKEN_BYTES", "IssuedToken", "digest_of", "new_token"]

TOKEN_BYTES: Final = 32
_TOKEN_RE: Final = re.compile(r"^[A-Za-z0-9_-]{43}$")


@dataclass(frozen=True)
class IssuedToken:
    raw: str = field(repr=False)
    digest: bytes = field(repr=False)


def new_token() -> IssuedToken:
    """32 random bytes as unpadded base64url (43 characters) and their SHA-256."""
    raw_bytes = secrets.token_bytes(TOKEN_BYTES)
    raw = base64.urlsafe_b64encode(raw_bytes).rstrip(b"=").decode()
    return IssuedToken(raw=raw, digest=hashlib.sha256(raw_bytes).digest())


def digest_of(raw: str) -> bytes | None:
    """SHA-256 of the decoded token, or ``None`` when it isn't a well-formed token."""
    if not _TOKEN_RE.match(raw):
        return None
    try:
        decoded = base64.urlsafe_b64decode(raw + "=")
    except binascii.Error, ValueError:  # pragma: no cover  (the regex admits only valid input)
        return None
    # 43 characters always decode to 32 bytes; a non-canonical last character (stray low bits)
    # would alias another token, so only the canonical spelling is accepted.
    if base64.urlsafe_b64encode(decoded).rstrip(b"=").decode() != raw:
        return None
    return hashlib.sha256(decoded).digest()
