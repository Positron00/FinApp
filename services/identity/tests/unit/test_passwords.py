"""Password policy and the argon2id hasher (identity LLD §6.1-6.3)."""

from __future__ import annotations

import pytest
from argon2 import extract_parameters
from argon2.low_level import Type
from identity.domain.passwords import PasswordHasher, PasswordTooWeakError, check_password_policy


def _hasher(t: int = 1, m: int = 1024, p: int = 1) -> PasswordHasher:
    return PasswordHasher(time_cost=t, memory_kib=m, parallelism=p, max_concurrency=2)


@pytest.mark.parametrize("pw", ["a" * 12, "a" * 256, "é" * 12, "🔑" * 12])
def test_policy_accepts_12_code_points_and_more(pw: str) -> None:
    """PAY-103: at least 12 code points (not bytes); no composition rules."""
    check_password_policy(pw)


@pytest.mark.parametrize("pw", ["", "a" * 11, "🔑" * 11])
def test_policy_rejects_fewer_than_12(pw: str) -> None:
    """PAY-103: 11 code points -> PasswordTooWeakError, even when it is 44 bytes."""
    with pytest.raises(PasswordTooWeakError):
        check_password_policy(pw)


async def test_production_parameters_are_encoded_in_the_hash() -> None:
    """PAY-103, PAY-104: argon2id with m=65536, t=3, p=4 (RFC 9106 low-memory profile)."""
    hasher = _hasher(3, 65536, 4)
    encoded = await hasher.hash("correct horse battery staple")
    params = extract_parameters(encoded)
    assert encoded.startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    assert params.type is Type.ID
    assert (params.time_cost, params.memory_cost, params.parallelism) == (3, 65536, 4)
    assert params.hash_len == 32
    assert params.salt_len == 16


async def test_verify_ok_wrong_and_none() -> None:
    """PAY-104: a real hash verifies; a wrong password and None (dummy hash) both fail."""
    hasher = _hasher()
    await hasher.start()
    encoded = await hasher.hash("correct horse battery staple")
    assert (await hasher.verify(encoded, "correct horse battery staple")).ok
    assert not (await hasher.verify(encoded, "wrong password!!")).ok
    dummy = await hasher.verify(None, "correct horse battery staple")
    assert not dummy.ok
    assert not dummy.needs_rehash
    assert not (await hasher.verify("not-a-hash", "whatever")).ok


async def test_dummy_hash_is_computed_lazily_without_start() -> None:
    """PAY-104: verify(None) still runs one argon2 verify when start() wasn't called."""
    assert not (await _hasher().verify(None, "x" * 12)).ok


async def test_needs_rehash_after_parameter_change() -> None:
    """PAY-104 (§6.2): a hash made with old parameters reports needs_rehash."""
    old = await _hasher(1, 1024, 1).hash("correct horse battery staple")
    result = await _hasher(2, 1024, 1).verify(old, "correct horse battery staple")
    assert result.ok
    assert result.needs_rehash
