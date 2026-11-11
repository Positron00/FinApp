"""Settings and startup checks (design §6; PAY-605, C1, C24, C29, C48)."""

# ruff: noqa: E501  (one case per line in the parameter tables)

from __future__ import annotations

import base64
import json
import os
from typing import Any

import pytest
from payroll.settings import STARTUP_CHECKS, Settings
from pycommon.settings import load_settings
from pycommon.startup_checks import run_startup_checks
from pycommon.testing import JsonLogCapture, TokenFactory
from pydantic import SecretStr

from tests.helpers import COMPANY_ID, STRONG_SECRET, make_settings


def _clean_settings(tokens: TokenFactory, **over: Any) -> Settings:
    """Settings that pass every startup check in prod (no dev kids, real secrets)."""
    keys = json.dumps({"prod-2026a": base64.b64encode(os.urandom(32)).decode()})
    raw = json.loads(tokens.keyset_json)
    renamed = json.dumps({f"prod-{k.removeprefix('test-')}": v for k, v in raw.items()})
    values: dict[str, Any] = {
        "env": "prod",
        "payroll_field_keys": SecretStr(keys),
        "payroll_field_active_kid": "prod-2026a",
        "internal_jwt_public_keys": renamed,
    }
    values.update(over)
    return make_settings(**values)


def test_loads_the_pycommon_and_payroll_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """PAY-605, C1, C24: DB_*, REDIS_*, DEBUG, DOCS_ENABLED, MAX_BODY_BYTES and PAYROLL_* keys."""
    env = {
        "COMPANY_ID": str(COMPANY_ID),
        "DB_HOST": "db.internal",
        "DB_PASSWORD": STRONG_SECRET,
        "REDIS_PASSWORD": STRONG_SECRET,
        "PAYROLL_FIELD_KEYS": json.dumps({"k": base64.b64encode(os.urandom(32)).decode()}),
        "PAYROLL_FIELD_ACTIVE_KID": "k",
        "PAYROLL_OUTBOX_BATCH": "250",
        "PAYOUT_REPUBLISH_AFTER_S": "400",
        "DOCS_ENABLED": "true",
        "MAX_BODY_BYTES": "1000",
        "LOG_LEVEL": "info",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    s = load_settings(Settings)
    assert (s.service_name, s.db_user, s.db_schema, s.redis_username) == (
        "payroll",
        "payroll_app",
        "payroll",
        "payroll",
    )
    assert s.db_host == "db.internal"
    assert s.payroll_outbox_batch == 250
    assert s.payout_republish_after_s == 400
    assert s.docs_enabled is True
    assert s.max_body_bytes == 1000
    assert s.log_level == "INFO"
    assert (s.stream_maxlen, s.payout_max_attempts, s.payout_mark_rejected_min_wait_s) == (
        100_000,
        5,
        120,
    )


def test_invalid_settings_exit_2_with_names_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """PAY-605: a validation failure logs settings.invalid (field names only) and exits 2."""
    monkeypatch.setenv("COMPANY_ID", str(COMPANY_ID))
    monkeypatch.setenv("PAYROLL_OUTBOX_BATCH", "5000")
    with pytest.raises(SystemExit) as exc:
        load_settings(Settings)
    assert exc.value.code == 2
    assert "5000" not in capsys.readouterr().err


def test_clean_prod_settings_pass(token_factory: TokenFactory) -> None:
    """PAY-605: a prod configuration with real keys passes every check."""
    run_startup_checks(_clean_settings(token_factory), STARTUP_CHECKS)


@pytest.mark.parametrize(
    ("over", "setting", "reason"),
    [
        ({"payroll_field_active_kid": "dev-1"}, "PAYROLL_FIELD_ACTIVE_KID", "missing"),
        ({"payout_republish_after_s": 300}, "PAYOUT_REPUBLISH_AFTER_S", "timing_invariant"),
        ({"payout_republish_max_backoff_s": 100}, "PAYOUT_REPUBLISH_MAX_BACKOFF_S", "timing_invariant"),
        ({"payout_mark_rejected_min_wait_s": 60}, "PAYOUT_MARK_REJECTED_MIN_WAIT_S", "timing_invariant"),
        ({"docs_enabled": True}, "DOCS_ENABLED", "docs_on"),
    ],
)  # fmt: skip
def test_failures_exit_2_in_uat_and_prod(
    token_factory: TokenFactory,
    caplog_json: JsonLogCapture,
    over: dict[str, Any],
    setting: str,
    reason: str,
) -> None:
    """PAY-605, C29: each failure logs startup.check_failed {setting, reason} and exits 2."""
    with pytest.raises(SystemExit) as exc:
        run_startup_checks(_clean_settings(token_factory, **over), STARTUP_CHECKS)
    assert exc.value.code == 2
    events = caplog_json.events("startup.check_failed")
    assert {"setting": setting, "reason": reason} in [
        {"setting": e["setting"], "reason": e["reason"]} for e in events
    ]


def test_dev_field_kid_and_bad_key_length(
    token_factory: TokenFactory, caplog_json: JsonLogCapture
) -> None:
    """PAY-605, ADR 0004: a dev- kid in uat and a 16-byte key both fail; the value isn't logged."""
    short = base64.b64encode(os.urandom(16)).decode()
    s = _clean_settings(
        token_factory,
        env="uat",
        payroll_field_keys=SecretStr(json.dumps({"dev-1": short})),
        payroll_field_active_kid="dev-1",
    )
    with pytest.raises(SystemExit):
        run_startup_checks(s, STARTUP_CHECKS)
    reasons = {(e["setting"], e["reason"]) for e in caplog_json.events("startup.check_failed")}
    assert ("PAYROLL_FIELD_KEYS", "dev_kid") in reasons
    assert ("PAYROLL_FIELD_KEYS", "invalid_key") in reasons
    assert short not in caplog_json.text


def test_jwt_keyset_needs_both_issuers(
    token_factory: TokenFactory, caplog_json: JsonLogCapture
) -> None:
    """PAY-600: at least one kid each for web and payout-worker."""
    raw = json.loads(token_factory.keyset_json)
    only_web = json.dumps({"prod-web": raw["test-web-1"]})
    with pytest.raises(SystemExit):
        run_startup_checks(
            _clean_settings(token_factory, internal_jwt_public_keys=only_web), STARTUP_CHECKS
        )
    assert ("INTERNAL_JWT_PUBLIC_KEYS", "issuer_missing") in {
        (e["setting"], e["reason"]) for e in caplog_json.events("startup.check_failed")
    }


def test_dev_only_warns(token_factory: TokenFactory, caplog_json: JsonLogCapture) -> None:
    """PAY-605, R1: in dev, PAYOUT_REPUBLISH_AFTER_S=300 is a warning and start-up continues."""
    run_startup_checks(
        _clean_settings(token_factory, env="dev", payout_republish_after_s=300), STARTUP_CHECKS
    )
    events = caplog_json.events("startup.check_failed")
    assert events
    assert all(e["level"] == "warning" for e in events)


def test_missing_keyset_is_left_to_pycommon(token_factory: TokenFactory) -> None:
    """PAY-600: without a keyset payroll's issuer check yields nothing (pycommon reports it)."""
    from payroll.settings import check_jwt_issuers  # noqa: PLC0415

    assert list(check_jwt_issuers(make_settings())) == []
