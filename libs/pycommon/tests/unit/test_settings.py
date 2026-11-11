"""Settings: names, precedence, LOG_LEVEL, error hygiene (platform LLD §3.1; C1, C24, C29)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from pycommon.auth import Keyset
from pycommon.settings import SettingsError, load_settings, normalise_pem
from pycommon.testing import JsonLogCapture, TokenFactory
from pydantic import SecretStr

from tests.helpers import COMPANY_ID, STRONG_SECRET, SvcSettings, make_settings


def _required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMPANY_ID", str(COMPANY_ID))
    monkeypatch.setenv("DB_USER", "testsvc_app")
    monkeypatch.setenv("DB_PASSWORD", STRONG_SECRET)
    monkeypatch.setenv("DB_SCHEMA", "testsvc")


def test_precedence_env_over_dotenv_over_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """PAY-605 / C1: process env > .env > code defaults."""
    _required_env(monkeypatch)
    (tmp_path / ".env").write_text("DB_HOST=from-dotenv\nDB_PORT=6543\n")
    monkeypatch.setenv("DB_HOST", "from-env")
    s = load_settings(SvcSettings)
    assert s.db_host == "from-env"
    assert s.db_port == 6543
    assert s.db_name == "payrun"


def test_c1_names_are_read_unprefixed_and_urls_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """C1: DB_HOST … REDIS_PASSWORD are the contract; DATABASE_URL and REDIS_URL are not read."""
    _required_env(monkeypatch)
    env = {
        "DB_HOST": "pg.internal",
        "DB_PORT": "5433",
        "DB_NAME": "payrun",
        "REDIS_HOST": "redis.internal",
        "REDIS_PORT": "6380",
        "REDIS_USERNAME": "payroll",
        "REDIS_PASSWORD": STRONG_SECRET,
        "DATABASE_URL": "postgresql://evil:evil@evil-host/evil",
        "REDIS_URL": "redis://evil-host:1/0",
        "TESTSVC_DB_HOST": "prefixed-host",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    s = load_settings(SvcSettings)
    assert (s.db_host, s.db_port) == ("pg.internal", 5433)
    assert (s.redis_host, s.redis_port, s.redis_username) == ("redis.internal", 6380, "payroll")
    assert s.redis_password is not None
    assert s.redis_password.get_secret_value() == STRONG_SECRET
    assert "evil" not in s.db_url()
    assert not hasattr(s, "database_url")
    assert not hasattr(s, "redis_url")


@pytest.mark.parametrize("value", ["info", "Info", "INFO", " warning "])
def test_log_level_is_case_insensitive(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """C24: LOG_LEVEL accepts any case and is upper-cased before the Literal check."""
    _required_env(monkeypatch)
    monkeypatch.setenv("LOG_LEVEL", value)
    assert load_settings(SvcSettings).log_level == value.strip().upper()


def test_log_level_verbose_fails_with_exit_2(monkeypatch: pytest.MonkeyPatch) -> None:
    """C24 / C29: an unknown LOG_LEVEL fails validation; load_settings exits 2."""
    _required_env(monkeypatch)
    monkeypatch.setenv("LOG_LEVEL", "verbose")
    with pytest.raises(SystemExit) as info:
        load_settings(SvcSettings)
    assert info.value.code == 2
    cause = info.value.__cause__
    assert isinstance(cause, SettingsError)
    assert cause.fields == ["LOG_LEVEL"]


def test_settings_error_lists_names_never_values(
    monkeypatch: pytest.MonkeyPatch, caplog_json: JsonLogCapture
) -> None:
    """PAY-605 / PAY-NFR-07: a secret put in an invalid field never appears in the error or log."""
    _required_env(monkeypatch)
    leaked = "hunter2-do-not-print-me"
    monkeypatch.setenv("DB_PORT", leaked)
    monkeypatch.delenv("DB_SCHEMA")
    with pytest.raises(SystemExit) as info:
        load_settings(SvcSettings)
    cause = info.value.__cause__
    assert isinstance(cause, SettingsError)
    assert set(cause.fields) == {"DB_PORT", "DB_SCHEMA"}
    assert leaked not in str(cause)
    assert "DB_PORT (int_parsing)" in str(cause)
    events = caplog_json.events("settings.invalid")
    assert len(events) == 1
    assert events[0]["level"] == "error"
    assert set(events[0]["fields"]) == {"DB_PORT", "DB_SCHEMA"}
    assert leaked not in caplog_json.text


def test_settings_invalid_goes_to_stderr_before_logging_is_configured(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """C29: with no handlers yet, ``settings.invalid`` is one JSON line on stderr; exit 2."""
    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    _required_env(monkeypatch)
    monkeypatch.setenv("DB_PASSWORD", "")
    monkeypatch.setenv("DEBUG", "not-a-bool-secret")
    with pytest.raises(SystemExit) as info:
        load_settings(SvcSettings)
    assert info.value.code == 2
    line = json.loads(capsys.readouterr().err.strip())
    assert line == {"level": "error", "event": "settings.invalid", "fields": ["DEBUG"]}


def test_keyset_env_is_parsed_and_bad_keyset_names_the_field_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PAY-600: INTERNAL_JWT_PUBLIC_KEYS loads as a Keyset; a bad one fails by name only."""
    _required_env(monkeypatch)
    tf = TokenFactory.create()
    monkeypatch.setenv("INTERNAL_JWT_PUBLIC_KEYS", tf.keyset_json)
    s = load_settings(SvcSettings)
    assert isinstance(s.internal_jwt_public_keys, Keyset)
    assert s.internal_jwt_public_keys.kids == {"test-web-1", "test-worker-1"}

    bad = json.dumps({"k1": {"iss": "web", "pem": "-----BEGIN PUBLIC KEY-----\nAAAA\n"}})
    monkeypatch.setenv("INTERNAL_JWT_PUBLIC_KEYS", bad)
    with pytest.raises(SystemExit) as info:
        load_settings(SvcSettings)
    cause = info.value.__cause__
    assert isinstance(cause, SettingsError)
    assert cause.fields == ["INTERNAL_JWT_PUBLIC_KEYS"]
    assert "AAAA" not in str(cause)

    monkeypatch.setenv("INTERNAL_JWT_PUBLIC_KEYS", "  ")
    assert load_settings(SvcSettings).internal_jwt_public_keys is None


def test_keyset_field_accepts_objects_and_rejects_other_types() -> None:
    """PAY-600: the keyset field takes a Keyset, a JSON string or nothing."""
    tf = TokenFactory.create()
    assert make_settings(internal_jwt_public_keys=tf.keyset).internal_jwt_public_keys is tf.keyset
    with pytest.raises(ValueError, match="internal_jwt_public_keys"):
        make_settings(internal_jwt_public_keys=42)


def test_normalise_pem() -> None:
    """PAY-600: PEMs from env files arrive quoted with literal \\n; they are restored."""
    raw = '"-----BEGIN PUBLIC KEY-----\\nMCowBQYDK2VwAyEA\\n-----END PUBLIC KEY-----\\n"'
    assert normalise_pem(raw) == (
        "-----BEGIN PUBLIC KEY-----\nMCowBQYDK2VwAyEA\n-----END PUBLIC KEY-----\n"
    )
    assert normalise_pem("'abc'") == "abc\n"
    assert normalise_pem("   ") == ""


def test_db_url_quotes_user_and_password() -> None:
    """PAY-606 / C1: the asyncpg URL is built from the C1 keys with user and password quoted."""
    s = make_settings(db_user="we@ird", db_password=SecretStr("p:w/@#?"), db_host="h", db_port=1)
    assert s.db_url() == "postgresql+asyncpg://we%40ird:p%3Aw%2F%40%23%3F@h:1/payrun"
    assert (
        s.db_url(user="m", password=SecretStr("x y")) == "postgresql+asyncpg://m:x%20y@h:1/payrun"
    )


def test_secret_fields_lists_set_secrets_by_env_name() -> None:
    """PAY-NFR-07: secret_fields gives every set SecretStr by its env name."""
    s = make_settings()
    secrets = s.secret_fields()
    assert set(secrets) == {"DB_PASSWORD", "REDIS_PASSWORD"}
    assert all(isinstance(v, SecretStr) for v in secrets.values())
    assert set(make_settings(redis_password=None).secret_fields()) == {"DB_PASSWORD"}
    assert SvcSettings.env_name("version") == "APP_VERSION"
    assert STRONG_SECRET not in repr(s)


def test_settings_error_default_message() -> None:
    """C29: a SettingsError without field errors still has a message."""
    assert str(SettingsError()) == "invalid settings"
    assert SettingsError(message="keyset: bad").fields == []
