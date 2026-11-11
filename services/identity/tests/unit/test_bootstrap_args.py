"""``bootstrap_owner`` arguments and exit code 2 (identity LLD §3.8)."""

from __future__ import annotations

import pytest
from identity.bootstrap_owner import EXIT_INVALID, InvalidArgsError, main, parse_args
from pycommon.testing import JsonLogCapture

from tests.helpers import COMPANY_ID, STRONG_SECRET

BASE = ["--email", "owner@payrun.example.com", "--company-id", str(COMPANY_ID)]


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for k, v in {
        "ENV": "dev",
        "COMPANY_ID": str(COMPANY_ID),
        "DB_HOST": "127.0.0.1",
        "DB_PORT": "1",
        "DB_USER": "identity_migrator",
        "DB_PASSWORD": STRONG_SECRET,
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.chdir("/")  # no stray .env
    return monkeypatch


def test_parse_defaults_and_flags() -> None:
    """PAY-100, PAY-609: --name defaults to Owner; --if-empty and --password-env parse."""
    a = parse_args(BASE)
    assert (a.email, a.company_id, a.name, a.if_empty, a.password_env) == (
        "owner@payrun.example.com",
        COMPANY_ID,
        "Owner",
        False,
        None,
    )
    b = parse_args([*BASE, "--name", "  Ann  ", "--if-empty", "--password-env", "PW"])
    assert (b.name, b.if_empty, b.password_env) == ("Ann", True, "PW")
    assert "owner@payrun.example.com" not in repr(a)


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--email", "owner@payrun.example.com"],
        ["--email", "not-an-email", "--company-id", str(COMPANY_ID)],
        ["--email", "owner@payrun.example.com", "--company-id", "nope"],
        [*BASE, "--name", "   "],
        [*BASE, "--name", "x" * 201],
        [*BASE, "--password-env", " "],
        [*BASE, "--unknown", "secret-value"],
    ],
)
def test_parse_rejects(argv: list[str]) -> None:
    """PAY-100: argparse errors and bad email/UUID/name are exit 2 (InvalidArgsError)."""
    with pytest.raises(InvalidArgsError):
        parse_args(argv)


@pytest.mark.parametrize(
    ("argv", "extra_env"),
    [
        (["--email", "x@y.z", "--company-id", "0193a000-0000-7000-8000-000000000002"], {}),
        ([*BASE, "--password-env", "SEED_OWNER_PASSWORD"], {"ENV": "uat"}),
        ([*BASE, "--password-env", "SEED_OWNER_PASSWORD"], {}),
        ([*BASE, "--password-env", "SEED_OWNER_PASSWORD"], {"SEED_OWNER_PASSWORD": "short"}),
        ([*BASE, "--unknown", "secret-value"], {}),
    ],
    ids=["company_mismatch", "password_env_outside_dev", "env_unset", "weak_password", "usage"],
)
def test_main_exit_2(
    env: pytest.MonkeyPatch,
    caplog_json: JsonLogCapture,
    argv: list[str],
    extra_env: dict[str, str],
) -> None:
    """PAY-100, PAY-609: each invalid case exits 2 before touching the DB, and logs no value."""
    env.delenv("SEED_OWNER_PASSWORD", raising=False)
    for k, v in extra_env.items():
        env.setenv(k, v)
    assert main(argv) == EXIT_INVALID
    [failed] = caplog_json.events("identity.bootstrap.failed")
    assert failed["reason"] == "invalid_args"
    assert "secret-value" not in caplog_json.text
    assert "short" not in caplog_json.text.replace("too_short", "")


def test_main_exit_2_on_invalid_settings(env: pytest.MonkeyPatch) -> None:
    """PAY-100: missing configuration (COMPANY_ID) exits 2."""
    env.delenv("COMPANY_ID")
    assert main(BASE) == EXIT_INVALID
