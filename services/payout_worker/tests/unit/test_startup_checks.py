"""Startup checks (design §3.7; PAY-605, C29, R1, R11, R14)."""

from __future__ import annotations

from typing import Any

import pytest
from payout_worker.main import build_app
from payout_worker.settings import STARTUP_CHECKS
from pycommon.settings import BaseServiceSettings
from pycommon.startup_checks import run_startup_checks
from pycommon.testing import JsonLogCapture
from pydantic import SecretStr

from tests.helpers import API_KEY, STRONG_SECRET, make_settings

GOOD: dict[str, Any] = {
    "payout_worker_jwt_kid": "k2026-11",
    "bank_base_url": "https://api.bank.payrun.net",
    "bank_key_safe_window_s": 86_400,
}


def _run(**over: Any) -> None:
    run_startup_checks(make_settings(**{**GOOD, **over}), STARTUP_CHECKS)


def test_defaults_pass_in_uat_and_prod(caplog_json: JsonLogCapture) -> None:
    """PAY-605: claim idle 60 s and re-publish 360 s pass; strong secrets; real bank URL."""
    _run(env="uat")
    _run(env="prod")
    assert caplog_json.events("startup.check_failed") == []


@pytest.mark.parametrize(
    ("over", "setting"),
    [
        ({"payout_worker_jwt_private_key": SecretStr("changeme")}, "PAYOUT_WORKER_JWT_PRIVATE_KEY"),
        ({"bank_stub_api_key": SecretStr("short")}, "BANK_STUB_API_KEY"),
        ({"db_password": SecretStr("password")}, "DB_PASSWORD"),
        ({"redis_password": None}, "REDIS_PASSWORD"),
        ({"payout_worker_jwt_kid": "dev-2026"}, "PAYOUT_WORKER_JWT_KID"),
        ({"payout_worker_jwt_kid": "test-1"}, "PAYOUT_WORKER_JWT_KID"),
        ({"debug": True}, "DEBUG"),
        ({"docs_enabled": True}, "DOCS_ENABLED"),
        ({"payout_claim_min_idle_ms": 35_000}, "PAYOUT_CLAIM_MIN_IDLE_MS"),
        ({"payout_republish_after_s": 300}, "PAYOUT_REPUBLISH_AFTER_S"),
    ],
)
@pytest.mark.parametrize("env", ["uat", "prod"])
def test_refusals_exit_2(
    caplog_json: JsonLogCapture, over: dict[str, Any], setting: str, env: str
) -> None:
    """PAY-605, C29: each refusal logs startup.check_failed (name only) and exits 2."""
    with pytest.raises(SystemExit) as exc:
        _run(env=env, **over)
    assert exc.value.code == 2
    failed = caplog_json.events("startup.check_failed")
    assert [e["setting"] for e in failed] == [setting]
    for secret in ("changeme", "short", "password", STRONG_SECRET, API_KEY):
        assert secret not in caplog_json.text


@pytest.mark.parametrize(
    "url",
    [
        "http://bank-stub:8000",
        "http://localhost:8000",
        "https://bank.example.com",
        "https://bank.invalid",
        "https://placeholder.bank.net",
    ],
)
def test_prod_bank_url(caplog_json: JsonLogCapture, url: str) -> None:
    """PAY-605, R14: in prod a bank-stub or placeholder BANK_BASE_URL is refused."""
    _run(env="uat", bank_base_url=url)  # not checked outside prod
    with pytest.raises(SystemExit):
        _run(env="prod", bank_base_url=url)
    assert caplog_json.events("startup.check_failed")[-1]["reason"] == "bank_stub_url"


def test_prod_key_window_must_be_set(caplog_json: JsonLogCapture) -> None:
    """PAY-410: in prod BANK_KEY_SAFE_WINDOW_S must come from the environment."""
    values = {**GOOD}
    values.pop("bank_key_safe_window_s")
    s = make_settings(env="prod", **values)
    with pytest.raises(SystemExit):
        run_startup_checks(s, STARTUP_CHECKS)
    [event] = caplog_json.events("startup.check_failed")
    assert (event["setting"], event["reason"]) == ("BANK_KEY_SAFE_WINDOW_S", "missing")


def test_dev_only_warns(caplog_json: JsonLogCapture) -> None:
    """C29: with ENV=dev the same failures are warnings and start-up continues."""
    run_startup_checks(make_settings(payout_claim_min_idle_ms=1000), STARTUP_CHECKS)
    events = caplog_json.events("startup.check_failed")
    assert {e["setting"] for e in events} == {"PAYOUT_CLAIM_MIN_IDLE_MS", "PAYOUT_WORKER_JWT_KID"}
    assert {e["level"] for e in events} == {"warning"}


async def test_app_exits_2_before_connecting() -> None:
    """PAY-605: the lifespan runs the checks before any connection and exits 2 in uat."""
    app = build_app(make_settings(env="uat", payout_claim_min_idle_ms=1), instrument=False)
    with pytest.raises(SystemExit):
        async with app.router.lifespan_context(app):
            pass  # pragma: no cover
    assert app.state.startup_exit_code == 2


def test_build_app_needs_worker_settings() -> None:
    """Design §1: build_app takes payout-worker Settings only."""
    with pytest.raises(TypeError):
        build_app(BaseServiceSettings.model_construct(), instrument=False)
