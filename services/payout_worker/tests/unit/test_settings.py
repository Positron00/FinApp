"""Settings (design §4; PAY-605, C1, C24, C46, C47)."""

from __future__ import annotations

import pytest
from payout_worker.settings import Settings
from pycommon.settings import load_settings
from pycommon.testing import JsonLogCapture

from tests.helpers import API_KEY, COMPANY_ID, STRONG_SECRET, TOKENS

BASE_ENV = {
    "COMPANY_ID": str(COMPANY_ID),
    "DB_HOST": "db.internal",
    "DB_PORT": "6543",
    "DB_NAME": "payrun",
    "DB_USER": "payouts_app",
    "DB_PASSWORD": STRONG_SECRET,
    "DB_SCHEMA": "payouts",
    "REDIS_HOST": "redis.internal",
    "REDIS_PORT": "6380",
    "REDIS_USERNAME": "payout-worker",
    "REDIS_PASSWORD": STRONG_SECRET,
    "PAYOUT_WORKER_JWT_PRIVATE_KEY": TOKENS.private_pems["payout-worker"],
    "PAYOUT_WORKER_JWT_KID": "k2026",
    "BANK_STUB_API_KEY": API_KEY,
}


def _env(monkeypatch: pytest.MonkeyPatch, **extra: str) -> None:
    for k, v in {**BASE_ENV, **extra}.items():
        monkeypatch.setenv(k, v)


def test_c1_keys_and_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """PAY-605, C1, C24, C46: C1 keys read, DATABASE_URL/REDIS_URL ignored, LOG_LEVEL=info."""
    _env(
        monkeypatch,
        DATABASE_URL="postgresql://ignored",
        REDIS_URL="redis://ignored",
        LOG_LEVEL="info",
        PAYOUT_CONSUMER_NAME="pod-1",
    )
    s = load_settings(Settings)
    assert (s.db_host, s.db_port, s.db_user, s.db_schema) == (
        "db.internal",
        6543,
        "payouts_app",
        "payouts",
    )
    assert (s.redis_host, s.redis_port, s.redis_username) == (
        "redis.internal",
        6380,
        "payout-worker",
    )
    assert "ignored" not in s.db_url()
    assert s.log_level == "INFO"
    assert s.payout_consumer_name == "pod-1"
    assert s.payout_payroll_base_url == "http://payroll:8000"
    assert (s.payout_concurrency, s.payout_max_deliveries, s.payout_claim_min_idle_ms) == (
        16,
        5,
        60_000,
    )
    assert s.handler_deadline_s == 35
    assert s.payout_republish_after_s == 360


def test_consumer_name_defaults_to_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    """Design §4: PAYOUT_CONSUMER_NAME defaults to the hostname."""
    import socket  # noqa: PLC0415

    _env(monkeypatch)
    assert load_settings(Settings).payout_consumer_name == socket.gethostname()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PAYOUT_CONCURRENCY", "0"),
        ("PAYOUT_MAX_DELIVERIES", "0"),
        ("PAYOUT_WORKER_JWT_TTL_S", "61"),
        ("PAYOUT_WORKER_JWT_TTL_S", "0"),
    ],
)
def test_field_constraints_exit_2(
    monkeypatch: pytest.MonkeyPatch, caplog_json: JsonLogCapture, name: str, value: str
) -> None:
    """PAY-605: field constraints → settings.invalid naming the field only, exit 2."""
    _env(monkeypatch, **{name: value})
    with pytest.raises(SystemExit) as exc:
        load_settings(Settings)
    assert exc.value.code == 2
    [event] = caplog_json.events("settings.invalid")
    assert event["fields"] == [name]


def test_drain_must_fit_grace(monkeypatch: pytest.MonkeyPatch, caplog_json: JsonLogCapture) -> None:
    """Design §3.7: PAYOUT_SHUTDOWN_DRAIN_S + 10 ≤ 75 (model validator)."""
    _env(monkeypatch, PAYOUT_SHUTDOWN_DRAIN_S="66")
    with pytest.raises(SystemExit):
        load_settings(Settings)
    assert caplog_json.events("settings.invalid")
