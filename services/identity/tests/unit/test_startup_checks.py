"""Identity startup checks (identity LLD §4.3)."""

from __future__ import annotations

import json
import logging

import pytest
from identity.startup_checks import (
    IDENTITY_CHECKS,
    check_audit_key,
    check_base_url,
    check_limits,
    check_smtp,
    check_web_issuer,
)
from pycommon.startup_checks import CheckFailure, run_startup_checks
from pycommon.testing import JsonLogCapture, TokenFactory
from pydantic import SecretStr

from tests.helpers import make_settings, web_only_keyset

GOOD = {
    "env": "uat",
    "audit_ip_hash_key": SecretStr("u" * 40),
    "identity_public_base_url": "https://pay.example",
}


def test_web_issuer(token_factory: TokenFactory) -> None:
    """PAY-600: every kid must be bound to iss=web (reason wrong_issuer)."""
    ok = make_settings(internal_jwt_public_keys=web_only_keyset(token_factory))
    assert list(check_web_issuer(ok)) == []
    both = make_settings(internal_jwt_public_keys=token_factory.keyset_json)
    assert list(check_web_issuer(both)) == [
        CheckFailure("INTERNAL_JWT_PUBLIC_KEYS", "wrong_issuer")
    ]
    assert list(check_web_issuer(make_settings())) == []


def test_audit_key() -> None:
    """PAY-NFR-08: AUDIT_IP_HASH_KEY >= 32 bytes and no dev-/test- prefix."""
    assert list(check_audit_key(make_settings(audit_ip_hash_key=SecretStr("x" * 32)))) == []
    assert list(check_audit_key(make_settings(audit_ip_hash_key=SecretStr("short")))) == [
        CheckFailure("AUDIT_IP_HASH_KEY", "too_short")
    ]
    dev = make_settings(audit_ip_hash_key=SecretStr("dev-" + "x" * 40))
    assert list(check_audit_key(dev)) == [CheckFailure("AUDIT_IP_HASH_KEY", "dev_default")]


def test_base_url_https() -> None:
    """PAY-103: IDENTITY_PUBLIC_BASE_URL must be https (not_https)."""
    assert list(check_base_url(make_settings(identity_public_base_url="https://a"))) == []
    assert list(check_base_url(make_settings())) == [
        CheckFailure("IDENTITY_PUBLIC_BASE_URL", "not_https")
    ]


def test_smtp_prod_only() -> None:
    """PAY-110: prod needs STARTTLS and SMTP credentials; other envs don't."""
    assert list(check_smtp(make_settings(env="uat"))) == []
    failures = list(check_smtp(make_settings(env="prod")))
    assert [f.setting for f in failures] == ["SMTP_STARTTLS", "SMTP_USERNAME", "SMTP_PASSWORD"]
    assert {f.reason for f in failures} == {"missing"}
    ok = make_settings(
        env="prod", smtp_starttls=True, smtp_username="u", smtp_password=SecretStr("p" * 20)
    )
    assert list(check_smtp(ok)) == []


def test_limits_refuse_test_values() -> None:
    """PAY-104: raised test limits can't reach uat or prod (test_value)."""
    assert list(check_limits(make_settings())) == []
    raised = make_settings(
        auth_login_limit_account=1000, auth_login_limit_ip=1000, identity_lockout_base_s=1
    )
    assert [f.setting for f in check_limits(raised)] == [
        "AUTH_LOGIN_LIMIT_ACCOUNT",
        "AUTH_LOGIN_LIMIT_IP",
        "IDENTITY_LOCKOUT_BASE_S",
    ]


def test_uat_failure_exits_2_and_never_logs_the_value(caplog_json: JsonLogCapture) -> None:
    """PAY-605, C29: uat logs startup.check_failed {setting, reason} and exits 2."""
    s = make_settings(env="uat", audit_ip_hash_key=SecretStr("dev-tiny"))
    with pytest.raises(SystemExit) as exc:
        run_startup_checks(s, IDENTITY_CHECKS)
    assert exc.value.code == 2
    events = caplog_json.events("startup.check_failed")
    assert {"setting": "AUDIT_IP_HASH_KEY", "reason": "too_short"} in [
        {"setting": e["setting"], "reason": e["reason"]} for e in events
    ]
    assert "dev-tiny" not in caplog_json.text


def test_dev_only_warns(caplog_json: JsonLogCapture) -> None:
    """PAY-605: in dev the same failures are warnings and start-up continues."""
    run_startup_checks(make_settings(), IDENTITY_CHECKS)
    levels = {json.loads(line)["level"] for line in caplog_json.lines}
    assert "warning" in levels
    assert logging.getLevelName(logging.ERROR).lower() not in levels


def test_good_uat_identity_checks_pass() -> None:
    """PAY-605: a well-configured uat passes every identity check."""
    s = make_settings(**GOOD)
    assert [f for check in IDENTITY_CHECKS for f in check(s)] == []
