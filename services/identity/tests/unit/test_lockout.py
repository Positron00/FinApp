"""Lock durations and the production defaults (identity LLD §6.4)."""

from __future__ import annotations

import pytest
from identity.domain.lockout import lock_seconds

from tests.helpers import make_settings


def test_lock_sequence_doubles_to_the_cap() -> None:
    """PAY-104: 60, 120, 240, 480, 960, 1920, 3600, 3600 s."""
    seq = [lock_seconds(n, 60, 3600) for n in range(1, 9)]
    assert seq == [60, 120, 240, 480, 960, 1920, 3600, 3600]
    assert lock_seconds(10_000, 60, 3600) == 3600


def test_lock_level_starts_at_one() -> None:
    """PAY-104: level 0 is not a lock."""
    with pytest.raises(ValueError, match="level"):
        lock_seconds(0, 60, 3600)


def test_settings_defaults_are_production_values() -> None:
    """PAY-104: 5 / 20 / 900 s / 60 s / 3600 s / 86400 s."""
    s = make_settings()
    assert s.auth_login_limit_account == 5
    assert s.auth_login_limit_ip == 20
    assert s.identity_login_window_s == 900
    assert s.identity_lockout_base_s == 60
    assert s.identity_lockout_max_s == 3600
    assert s.identity_lockout_level_ttl_s == 86400
