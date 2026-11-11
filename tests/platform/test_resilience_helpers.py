"""The resilience suite's pure helpers (tests/resilience/_stack.py; platform LLD §6.2; PAY-NFR-03):
``compose_timing`` and ``web_breaker_failures`` read the Compose config they're given, and
``counter_increase`` sums a counter's rise per series between two Prometheus snapshots.

``_stack`` runs ``docker compose config`` on import; without .env (CI's platform job) that fails
and the module falls back to its defaults, which these tests don't depend on.
"""

from __future__ import annotations

import re
import sys
from typing import Any

import pytest
from pycommon.testing import find_repo_root

sys.path.insert(0, str(find_repo_root() / "tests" / "resilience"))

import _stack  # type: ignore[import-not-found]  # tests/resilience, added just above

TIMING = {
    "payout-worker": {
        "PAYOUT_CLAIM_MIN_IDLE_MS": "30000",
        "PAYOUT_MAX_DELIVERIES": "3",
        "BANK_TOTAL_DEADLINE_S": "10",
        "BANK_API_KEY": "not-read",
    },
    "payroll": {"PAYOUT_REPUBLISH_AFTER_S": "120", "MAINTENANCE_INTERVAL_S": "20"},
}


def _config(envs: dict[str, dict[str, str]]) -> dict[str, Any]:
    return {"services": {name: {"environment": env} for name, env in envs.items()}}


def _series(**labels: str) -> frozenset[tuple[str, str]]:
    return frozenset(labels.items())


# --------------------------------------------------------------------------- compose_timing
def test_compose_timing_reads_every_key_from_the_config() -> None:
    assert _stack.compose_timing(_config(TIMING)) == {
        "PAYOUT_CLAIM_MIN_IDLE_MS": 30000,
        "PAYOUT_MAX_DELIVERIES": 3,
        "BANK_TOTAL_DEADLINE_S": 10,
        "PAYOUT_REPUBLISH_AFTER_S": 120,
        "MAINTENANCE_INTERVAL_S": 20,
    }


@pytest.mark.parametrize(
    ("service", "key"),
    [("payout-worker", "PAYOUT_MAX_DELIVERIES"), ("payroll", "PAYOUT_REPUBLISH_AFTER_S")],
)
def test_compose_timing_raises_when_a_read_config_lacks_a_key(service: str, key: str) -> None:
    envs = {name: dict(env) for name, env in TIMING.items()}
    del envs[service][key]
    with pytest.raises(LookupError, match=re.escape(f"{service}.{key}")):
        _stack.compose_timing(_config(envs))


def test_compose_timing_raises_when_a_service_is_missing() -> None:
    with pytest.raises(LookupError, match=re.escape("payroll.PAYOUT_REPUBLISH_AFTER_S")):
        _stack.compose_timing(_config({"payout-worker": TIMING["payout-worker"]}))


def test_compose_timing_uses_the_defaults_only_without_a_config() -> None:
    flat = {k: v for d in _stack.TIMING_DEFAULTS.values() for k, v in d.items()}
    assert _stack.compose_timing(None) == flat


def test_web_breaker_failures_prefers_compose_then_the_web_default() -> None:
    assert _stack.web_breaker_failures(_config({"web": {"BREAKER_FAILURES": "7"}})) == 7
    default = _stack.web_breaker_failures(None)
    assert f"BREAKER_FAILURES: int({default}," in _stack.WEB_CONFIG_TS.read_text(encoding="utf-8")
    assert _stack.web_breaker_failures(_config({"web": {}})) == default


# --------------------------------------------------------------------------- counter_increase
def test_counter_increase_counts_a_new_series_in_full() -> None:
    """A restarted process has a new instance: its whole value is new since the snapshot."""
    before = {_series(instance="a"): 4.0}
    after = {_series(instance="a"): 6.0, _series(instance="b"): 3.0}
    assert _stack.counter_increase(before, after) == 5.0


def test_counter_increase_counts_an_expired_series_as_no_change() -> None:
    """A dead process's series leaves after metric_expiration: no negative contribution."""
    before = {_series(instance="a"): 4.0, _series(instance="b"): 9.0}
    after = {_series(instance="a"): 5.0}
    assert _stack.counter_increase(before, after) == 1.0


def test_counter_increase_counts_a_drop_on_the_same_labels_as_zero() -> None:
    before = {_series(instance="a"): 4.0, _series(instance="b"): 1.0}
    after = {_series(instance="a"): 2.0, _series(instance="b"): 3.0}
    assert _stack.counter_increase(before, after) == 2.0
