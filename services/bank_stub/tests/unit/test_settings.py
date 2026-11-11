"""Settings (design §4; PAY-605, C1, C24)."""

from __future__ import annotations

import pytest
from bank_stub.settings import Settings
from pycommon.settings import load_settings
from pycommon.testing import JsonLogCapture

from tests.helpers import API_KEY, COMPANY_ID, STRONG_SECRET

BASE_ENV = {
    "COMPANY_ID": str(COMPANY_ID),
    "DB_HOST": "db.internal",
    "DB_PORT": "6543",
    "DB_NAME": "payrun",
    "DB_USER": "bank_stub_app",
    "DB_PASSWORD": STRONG_SECRET,
    "DB_SCHEMA": "bank_stub",
    "BANK_STUB_API_KEY": API_KEY,
}


def _env(monkeypatch: pytest.MonkeyPatch, **extra: str) -> None:
    for k, v in {**BASE_ENV, **extra}.items():
        monkeypatch.setenv(k, v)


def test_loads_c1_keys_and_ignores_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """PAY-605, C1: DB_HOST … DB_SCHEMA are read; DATABASE_URL is ignored; no Redis."""
    _env(monkeypatch, DATABASE_URL="postgresql://ignored", LOG_LEVEL="info")
    s = load_settings(Settings)
    assert (s.db_host, s.db_port, s.db_user, s.db_schema) == (
        "db.internal",
        6543,
        "bank_stub_app",
        "bank_stub",
    )
    assert s.db_url().startswith("postgresql+asyncpg://bank_stub_app:")
    assert "ignored" not in s.db_url()
    assert s.log_level == "INFO"
    assert s.redis_host is None
    assert s.service_name == "bank-stub"
    assert (s.bank_stub_failure_rate, s.bank_stub_latency_ms) == (0.05, 20)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("BANK_STUB_FAILURE_RATE", "1.5"),
        ("BANK_STUB_FAILURE_RATE", "-0.1"),
        ("BANK_STUB_LATENCY_MS", "10001"),
        ("BANK_STUB_LATENCY_MS", "-4242"),
    ],
)
def test_out_of_bounds_exits_2(
    monkeypatch: pytest.MonkeyPatch, caplog_json: JsonLogCapture, name: str, value: str
) -> None:
    """PAY-605: rate and latency bounds → settings.invalid (names only) and exit 2."""
    _env(monkeypatch, **{name: value})
    with pytest.raises(SystemExit) as exc:
        load_settings(Settings)
    assert exc.value.code == 2
    [event] = caplog_json.events("settings.invalid")
    assert event["fields"] == [name]
    assert value not in caplog_json.text
