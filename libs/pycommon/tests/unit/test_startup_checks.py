"""Startup checks (platform LLD §3.12; PAY-605, R1, C29, C48)."""

import json

import pytest
from pycommon.settings import Env
from pycommon.startup_checks import (
    DEV_DEFAULTS,
    CheckFailure,
    check_bank_base_url,
    check_claim_idle,
    check_debug_and_docs,
    check_field_keys,
    check_jwt_kids,
    check_mark_rejected_wait,
    check_republish_after,
    check_secrets,
    run_startup_checks,
)
from pycommon.testing import JsonLogCapture, TokenFactory
from pydantic import SecretStr

from tests.helpers import STRONG_SECRET, SvcSettings, make_settings


class KidSettings(SvcSettings):
    payroll_field_active_kid: str = "prod-2026-11"


class OptionalSecretSettings(SvcSettings):
    smtp_password: SecretStr | None = None


def test_optional_service_secret_may_be_unset() -> None:
    """PAY-605, C59: a service's optional secret left unset passes; base secrets never do."""
    base = make_settings().model_dump()
    assert list(check_secrets(OptionalSecretSettings.model_validate(base))) == []
    weak = OptionalSecretSettings.model_validate(base | {"smtp_password": SecretStr("changeme")})
    assert list(check_secrets(weak)) == [CheckFailure("SMTP_PASSWORD", "dev_default")]
    no_redis = OptionalSecretSettings.model_validate(base | {"redis_password": None})
    assert list(check_secrets(no_redis)) == [CheckFailure("REDIS_PASSWORD", "missing")]


def test_secrets_pass_and_fail() -> None:
    """PAY-605: every SecretStr must be present, not a dev default and ≥ 16 characters."""
    assert list(check_secrets(make_settings())) == []
    assert list(check_secrets(make_settings(redis_password=None))) == [
        CheckFailure("REDIS_PASSWORD", "missing")
    ]
    assert list(check_secrets(make_settings(redis_password=None, redis_host=None))) == []
    assert list(check_secrets(make_settings(db_password=SecretStr("  ")))) == [
        CheckFailure("DB_PASSWORD", "missing")
    ]
    assert list(check_secrets(make_settings(db_password=SecretStr("short-but-real")))) == [
        CheckFailure("DB_PASSWORD", "dev_default")
    ]


@pytest.mark.parametrize("value", sorted(DEV_DEFAULTS - {""}))
def test_each_dev_default_is_refused(value: str) -> None:
    """PAY-605: each DEV_DEFAULTS value (any case) is refused."""
    for v in (value, value.upper()):
        assert list(check_secrets(make_settings(db_password=SecretStr(v)))) == [
            CheckFailure("DB_PASSWORD", "dev_default")
        ]


def test_dev_and_test_kids() -> None:
    """PAY-605: dev-/test- kids in the keyset or in *_KID fields are refused."""
    tf = TokenFactory.create()  # kids test-web-1 / test-worker-1
    s = KidSettings.model_validate(
        make_settings(internal_jwt_public_keys=tf.keyset).model_dump()
        | {"payroll_field_active_kid": "dev-1"}
    )
    assert set(check_jwt_kids(s)) == {
        CheckFailure("INTERNAL_JWT_PUBLIC_KEYS", "dev_kid"),
        CheckFailure("PAYROLL_FIELD_ACTIVE_KID", "dev_kid"),
    }
    clean = KidSettings.model_validate(make_settings().model_dump())
    assert list(check_jwt_kids(clean)) == []


def test_field_keys() -> None:
    """PAY-605: PAYROLL_FIELD_KEYS must hold the active kid; dev kids and dev keys are refused."""
    good = json.dumps({"k2026": "A" * 44})
    assert list(check_field_keys(good, "k2026")) == []
    assert list(check_field_keys(json.dumps({"dev-1": "A" * 44}), "dev-1")) == [
        CheckFailure("PAYROLL_FIELD_KEYS", "dev_kid"),
        CheckFailure("PAYROLL_FIELD_ACTIVE_KID", "dev_kid"),
    ]
    assert list(check_field_keys(good, "other")) == [
        CheckFailure("PAYROLL_FIELD_ACTIVE_KID", "missing")
    ]
    assert list(check_field_keys(json.dumps({"k": "changeme"}), "k")) == [
        CheckFailure("PAYROLL_FIELD_KEYS", "dev_default")
    ]
    for bad in ("", "not json", "[]", "{}"):
        assert list(check_field_keys(bad, "k")) == [CheckFailure("PAYROLL_FIELD_KEYS", "missing")]
    assert list(check_field_keys("", "k", setting="X_SECRET")) == [
        CheckFailure("X_SECRET", "missing")
    ]


def test_debug_and_docs() -> None:
    """PAY-605: DEBUG or DOCS_ENABLED on is refused."""
    assert list(check_debug_and_docs(make_settings())) == []
    assert list(check_debug_and_docs(make_settings(debug=True, docs_enabled=True))) == [
        CheckFailure("DEBUG", "debug_on"),
        CheckFailure("DOCS_ENABLED", "docs_on"),
    ]


@pytest.mark.parametrize(
    "url",
    [
        "http://bank-stub:8000",
        "http://bank-stub.payrun.svc:8000",
        "http://localhost:4105",
        "http://127.0.0.1:8000",
        "http://[::1]:8000",
        "https://bank-provider.invalid",
        "https://placeholder.bank.com",
        "https://api.example.com",
        "not a url",
    ],
)
def test_bank_base_url_refused_in_prod_only(url: str) -> None:
    """PAY-605: in prod BANK_BASE_URL can't point at bank-stub, localhost or a placeholder."""
    assert list(check_bank_base_url(url, env=Env.prod)) == [
        CheckFailure("BANK_BASE_URL", "bank_stub_url")
    ]
    assert list(check_bank_base_url(url, env=Env.uat)) == []


def test_bank_base_url_good_and_missing() -> None:
    """PAY-605: a real provider URL passes in prod; an empty one is missing."""
    assert list(check_bank_base_url("https://api.realbank.com/v2", env=Env.prod)) == []
    assert list(check_bank_base_url(" ", env=Env.prod)) == [
        CheckFailure("BANK_BASE_URL", "missing")
    ]
    assert list(check_bank_base_url("http://10.0.0.5", env=Env.prod)) == []


def test_claim_idle_boundary() -> None:
    """PAY-605: claim idle must exceed bank + 2·payroll + 10 (35 s fails, 35.001 s passes)."""
    assert list(check_claim_idle(35_000, 15, 5)) == [
        CheckFailure("PAYOUT_CLAIM_MIN_IDLE_MS", "timing_invariant")
    ]
    assert list(check_claim_idle(35_001, 15, 5)) == []
    assert list(check_claim_idle(60_000, 15, 5)) == []


def test_republish_after_boundary() -> None:
    """PAY-605 / R1: republish must exceed 5 x 60 + 15 = 315; 360 passes, the old 300 fails."""
    assert list(check_republish_after(315, 5, 60_000, 15)) == [
        CheckFailure("PAYOUT_REPUBLISH_AFTER_S", "timing_invariant")
    ]
    assert list(check_republish_after(315.001, 5, 60_000, 15)) == []
    assert list(check_republish_after(360, 5, 60_000, 15)) == []
    assert list(check_republish_after(300, 5, 60_000, 15)) != []


def test_mark_rejected_wait_boundary() -> None:
    """C48: wait must exceed (15 + 2·5 + 10) + 30 = 65; the default 120 passes."""
    assert list(check_mark_rejected_wait(65, 15, 5)) == [
        CheckFailure("PAYOUT_MARK_REJECTED_MIN_WAIT_S", "timing_invariant")
    ]
    assert list(check_mark_rejected_wait(65.001, 15, 5)) == []
    assert list(check_mark_rejected_wait(120, 15, 5)) == []


@pytest.mark.parametrize("env", ["uat", "prod"])
def test_uat_and_prod_exit_2_with_one_line_per_failure(
    caplog_json: JsonLogCapture, env: str
) -> None:
    """C29: uat/prod log startup.check_failed {setting, reason} per failure at error, exit 2."""
    weak = "changeme"
    s = make_settings(env=env, debug=True, db_password=SecretStr(weak))

    def extra(_s: object) -> list[CheckFailure]:
        return [CheckFailure("BANK_BASE_URL", "bank_stub_url")]

    with pytest.raises(SystemExit) as info:
        run_startup_checks(s, [extra])
    assert info.value.code == 2
    events = caplog_json.events("startup.check_failed")
    assert {(e["setting"], e["reason"], e["level"]) for e in events} == {
        ("DB_PASSWORD", "dev_default", "error"),
        ("DEBUG", "debug_on", "error"),
        ("BANK_BASE_URL", "bank_stub_url", "error"),
    }
    assert len(events) == 3
    assert weak not in caplog_json.text
    assert STRONG_SECRET not in caplog_json.text


def test_dev_logs_warnings_and_continues(caplog_json: JsonLogCapture) -> None:
    """C29: in dev the same failures are warnings and start-up continues."""
    run_startup_checks(make_settings(env="dev", docs_enabled=True))
    (event,) = caplog_json.events("startup.check_failed")
    assert (event["setting"], event["reason"], event["level"]) == (
        "DOCS_ENABLED",
        "docs_on",
        "warning",
    )
    run_startup_checks(make_settings(env="prod"))  # nothing to report: returns
