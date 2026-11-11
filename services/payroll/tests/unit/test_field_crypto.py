"""AES-256-GCM field encryption (design §3.6; PAY-204, ADR 0004)."""

from __future__ import annotations

import base64
import json
import os
import re
from uuid import uuid4

import pytest
from payroll.crypto import FieldDecryptError, FieldKeyring, employee_aad, snapshot_aad
from pydantic import SecretStr

EMP = uuid4()
AAD = employee_aad("bank_account", EMP)
ACCOUNT_NUMBER = "000123456789"


def _ring(**keys: bytes) -> FieldKeyring:
    keys = keys or {"k1": os.urandom(32)}
    return FieldKeyring(keys, next(iter(keys)))


def test_round_trip_and_token_format() -> None:
    """PAY-204: decrypt(encrypt(x)) == x; token is kid:b64(12-byte nonce):b64(ct||tag)."""
    ring = _ring()
    token = ring.encrypt(ACCOUNT_NUMBER, aad=AAD)
    kid, nonce, ct = token.split(":")
    assert kid == "k1" == ring.kid_of(token) == ring.active_kid
    assert len(base64.b64decode(nonce)) == 12
    assert len(base64.b64decode(ct)) == len(ACCOUNT_NUMBER) + 16
    assert ACCOUNT_NUMBER not in token
    assert ring.decrypt(token, aad=AAD) == ACCOUNT_NUMBER


def test_fresh_nonce_per_call() -> None:
    """PAY-204: the same plaintext encrypts differently each time."""
    ring = _ring()
    tokens = {ring.encrypt(ACCOUNT_NUMBER, aad=AAD) for _ in range(20)}
    assert len(tokens) == 20


def _flip(b64: str) -> str:
    raw = bytearray(base64.b64decode(b64))
    raw[0] ^= 0x01
    return base64.b64encode(bytes(raw)).decode()


@pytest.mark.parametrize("part", ["nonce", "ciphertext"])
def test_tampered_token_raises(part: str) -> None:
    """PAY-204: a tampered nonce or ciphertext raises FieldDecryptError."""
    ring = _ring()
    kid, nonce, ct = ring.encrypt(ACCOUNT_NUMBER, aad=AAD).split(":")
    token = f"{kid}:{_flip(nonce)}:{ct}" if part == "nonce" else f"{kid}:{nonce}:{_flip(ct)}"
    with pytest.raises(FieldDecryptError) as err:
        ring.decrypt(token, aad=AAD)
    assert err.value.kid == "k1"


def test_aad_binds_the_row_and_field() -> None:
    """PAY-204: swapping a ciphertext between employees or fields fails (AAD)."""
    ring = _ring()
    token = ring.encrypt(ACCOUNT_NUMBER, aad=AAD)
    for other in (
        employee_aad("bank_account", uuid4()),
        employee_aad("bank_routing", EMP),
        snapshot_aad(EMP, 1),
    ):
        with pytest.raises(FieldDecryptError):
            ring.decrypt(token, aad=other)


@pytest.mark.parametrize(
    "token",
    ["", "nocolons", "k1:onlyone", "k1:!!!:???", "k1:AAAA:AAAA", "zz:AAAAAAAAAAAAAAAA:AAAA"],
)
def test_malformed_or_unknown_kid_raises(token: str) -> None:
    """PAY-204: bad format, bad base64, a short nonce or an unknown kid → FieldDecryptError."""
    with pytest.raises(FieldDecryptError):
        _ring().decrypt(token, aad=AAD)


def test_tampered_kid_raises_with_the_kid() -> None:
    """PAY-204: a kid swapped to another known kid fails authentication."""
    ring = FieldKeyring({"k1": os.urandom(32), "k2": os.urandom(32)}, "k1")
    _, nonce, ct = ring.encrypt(ACCOUNT_NUMBER, aad=AAD).split(":")
    with pytest.raises(FieldDecryptError) as err:
        ring.decrypt(f"k2:{nonce}:{ct}", aad=AAD)
    assert err.value.kid == "k2"
    with pytest.raises(FieldDecryptError):
        ring.kid_of("bad kid!:x:y")


def test_rotation_old_kid_still_decrypts() -> None:
    """PAY-204: after rotation to a new active kid, values under the old kid still decrypt."""
    old_key, new_key = os.urandom(32), os.urandom(32)
    before = FieldKeyring({"2026a": old_key}, "2026a")
    token = before.encrypt(ACCOUNT_NUMBER, aad=AAD)
    after = FieldKeyring({"2026a": old_key, "2026b": new_key}, "2026b")
    assert after.decrypt(token, aad=AAD) == ACCOUNT_NUMBER
    assert after.kid_of(after.encrypt(ACCOUNT_NUMBER, aad=AAD)) == "2026b"


@pytest.mark.parametrize(
    ("keys", "active"),
    [
        ({"k1": os.urandom(16)}, "k1"),
        ({"k1": os.urandom(33)}, "k1"),
        ({"k1": os.urandom(32)}, "k2"),
        ({"bad:kid": os.urandom(32)}, "bad:kid"),
        ({}, "k1"),
    ],
)
def test_bad_keyring_is_refused(keys: dict[str, bytes], active: str) -> None:
    """PAY-204: a key that isn't 32 bytes, an unknown active kid or a bad kid refuses startup."""
    with pytest.raises(ValueError, match="key"):
        FieldKeyring(keys, active)


def test_from_settings_parses_and_never_echoes_the_key() -> None:
    """PAY-204: PAYROLL_FIELD_KEYS is {kid: base64(32 bytes)}; errors don't contain key bytes."""
    key = base64.b64encode(os.urandom(32)).decode()
    ring = FieldKeyring.from_settings(SecretStr(json.dumps({"k1": key})), "k1")
    assert ring.active_kid == "k1"
    for raw in ("not json", "[]", json.dumps({"k1": 5}), json.dumps({"k1": "***"})):
        with pytest.raises(ValueError, match=r"PAYROLL_FIELD_KEYS|base64") as err:
            FieldKeyring.from_settings(SecretStr(raw), "k1")
        assert key not in str(err.value)


def test_error_message_holds_no_plaintext_or_ciphertext() -> None:
    """PAY-204, PAY-NFR-07: FieldDecryptError carries the kid only."""
    ring = _ring()
    token = ring.encrypt(ACCOUNT_NUMBER, aad=AAD)
    with pytest.raises(FieldDecryptError) as err:
        ring.decrypt(token, aad="wrong")
    text = f"{err.value!s} {err.value!r} {err.value.args}"
    assert ACCOUNT_NUMBER not in text
    assert not re.search(re.escape(token.split(":")[2][:12]), text)
