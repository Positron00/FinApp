"""AES-256-GCM field encryption with key IDs (design §3.6; PAY-204, ADR 0004).

Token format: ``"{kid}:{b64(nonce)}:{b64(ciphertext||tag)}"`` (standard base64 with padding),
a fresh 12-byte nonce per encryption, and an AAD that binds the ciphertext to its row and field.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
from collections.abc import Mapping
from typing import Final, Literal
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

__all__ = [
    "FieldDecryptError",
    "FieldKeyring",
    "employee_aad",
    "snapshot_aad",
]

KID_RE: Final = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
KEY_BYTES: Final = 32
NONCE_BYTES: Final = 12


class FieldDecryptError(Exception):
    """A field could not be decrypted. Carries the kid only: never plaintext or ciphertext."""

    def __init__(self, kid: str | None) -> None:
        self.kid = kid
        super().__init__("field decryption failed")


class FieldKeyring:
    def __init__(self, keys: Mapping[str, bytes], active_kid: str) -> None:
        if not keys:
            raise ValueError("the field keyring is empty")
        for kid, key in keys.items():
            if not KID_RE.match(kid):
                raise ValueError("a field key ID is malformed")
            if len(key) != KEY_BYTES:
                raise ValueError("a field key is not 32 bytes")
        if active_kid not in keys:
            raise ValueError("the active field key ID is not in the keyring")
        self._ciphers = {kid: AESGCM(key) for kid, key in keys.items()}
        self._active = active_kid

    @classmethod
    def from_settings(cls, keys_json: SecretStr, active_kid: str) -> FieldKeyring:
        """Parse ``PAYROLL_FIELD_KEYS`` (``{kid: base64(32 bytes)}``); errors never hold a key."""
        try:
            raw = json.loads(keys_json.get_secret_value())
        except ValueError:
            raise ValueError("PAYROLL_FIELD_KEYS is not valid JSON") from None
        if not isinstance(raw, dict):
            raise ValueError("PAYROLL_FIELD_KEYS must be a JSON object")
        keys: dict[str, bytes] = {}
        for kid, value in raw.items():
            if not isinstance(value, str):
                raise ValueError("a field key must be a base64 string")
            try:
                keys[str(kid)] = base64.b64decode(value, validate=True)
            except binascii.Error, ValueError:
                raise ValueError("a field key is not valid base64") from None
        return cls(keys, active_kid)

    @property
    def active_kid(self) -> str:
        return self._active

    def encrypt(self, plaintext: str, *, aad: str) -> str:
        nonce = os.urandom(NONCE_BYTES)
        ct = self._ciphers[self._active].encrypt(nonce, plaintext.encode(), aad.encode())
        return f"{self._active}:{base64.b64encode(nonce).decode()}:{base64.b64encode(ct).decode()}"

    def kid_of(self, token: str) -> str:
        kid, sep, _ = token.partition(":")
        if not sep or not KID_RE.match(kid):
            raise FieldDecryptError(None)
        return kid

    def decrypt(self, token: str, *, aad: str) -> str:
        parts = token.split(":", 2)
        if len(parts) != 3:
            raise FieldDecryptError(None)
        kid, nonce_b64, ct_b64 = parts
        cipher = self._ciphers.get(kid)
        if cipher is None:
            raise FieldDecryptError(kid if KID_RE.match(kid) else None)
        try:
            nonce = base64.b64decode(nonce_b64, validate=True)
            ct = base64.b64decode(ct_b64, validate=True)
            if len(nonce) != NONCE_BYTES:
                raise FieldDecryptError(kid)
            return cipher.decrypt(nonce, ct, aad.encode()).decode()
        except FieldDecryptError:
            raise
        except binascii.Error, ValueError, InvalidTag, UnicodeDecodeError:
            raise FieldDecryptError(kid) from None


def employee_aad(field: Literal["bank_routing", "bank_account"], employee_id: UUID) -> str:
    return f"payroll.employees.{field}:{employee_id}"


def snapshot_aad(payout_id: UUID, attempt: int) -> str:
    return f"payroll.payout_attempts.bank_snapshot:{payout_id}:{attempt}"
