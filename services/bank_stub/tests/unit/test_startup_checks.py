"""Startup checks (design §3.5; PAY-605, C29, C47)."""

from __future__ import annotations

from typing import Any

import pytest
from bank_stub.main import build_app
from bank_stub.settings import STARTUP_CHECKS, check_not_prod
from pycommon.startup_checks import CheckFailure, run_startup_checks
from pycommon.testing import JsonLogCapture
from pydantic import SecretStr

from tests.helpers import API_KEY, STRONG_SECRET, make_settings


def test_not_prod() -> None:
    """ADR 0001, C47: ENV=prod → setting ENV, reason not_deployable."""
    assert list(check_not_prod(make_settings(env="prod"))) == [
        CheckFailure("ENV", "not_deployable")
    ]
    assert list(check_not_prod(make_settings(env="uat"))) == []


@pytest.mark.parametrize(
    ("over", "setting"),
    [
        ({"env": "prod"}, "ENV"),
        ({"bank_stub_api_key": SecretStr("changeme")}, "BANK_STUB_API_KEY"),
        ({"bank_stub_api_key": SecretStr("short")}, "BANK_STUB_API_KEY"),
        ({"db_password": SecretStr("")}, "DB_PASSWORD"),
        ({"db_password": SecretStr("password")}, "DB_PASSWORD"),
        ({"debug": True}, "DEBUG"),
        ({"docs_enabled": True}, "DOCS_ENABLED"),
    ],
)
def test_uat_refusals_exit_2(
    caplog_json: JsonLogCapture, over: dict[str, Any], setting: str
) -> None:
    """PAY-605, C29: each refusal logs startup.check_failed with the name only, then exit 2."""
    s = make_settings(**{"env": "uat", **over})
    with pytest.raises(SystemExit) as exc:
        run_startup_checks(s, STARTUP_CHECKS)
    assert exc.value.code == 2
    failed = caplog_json.events("startup.check_failed")
    assert [e["setting"] for e in failed] == [setting]
    assert failed[0]["level"] == "error"
    for v in ("changeme", "short", "password", API_KEY, STRONG_SECRET):
        assert v not in caplog_json.text


def test_good_uat_settings_pass(caplog_json: JsonLogCapture) -> None:
    """PAY-605: strong secrets, debug and docs off, ENV=uat → no failure."""
    run_startup_checks(make_settings(env="uat"), STARTUP_CHECKS)
    assert caplog_json.events("startup.check_failed") == []


def test_dev_failures_only_warn(caplog_json: JsonLogCapture) -> None:
    """C29: in dev the same failures are warnings and start-up continues."""
    run_startup_checks(make_settings(debug=True), STARTUP_CHECKS)
    [event] = caplog_json.events("startup.check_failed")
    assert event["level"] == "warning"


async def test_app_refuses_prod_before_connecting() -> None:
    """ADR 0001, PAY-605: the app's lifespan exits 2 for ENV=prod before any connection."""
    app = build_app(make_settings(env="prod"), instrument=False)
    with pytest.raises(SystemExit) as exc:
        async with app.router.lifespan_context(app):
            pass  # pragma: no cover
    assert exc.value.code == 2
    assert app.state.startup_exit_code == 2


def test_build_app_needs_bank_stub_settings() -> None:
    """Design §1: build_app takes bank-stub Settings only."""
    from pycommon.settings import BaseServiceSettings  # noqa: PLC0415

    with pytest.raises(TypeError):
        build_app(BaseServiceSettings.model_construct(), instrument=False)


def test_lazy_app_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Design §1: bank_stub.main:app is built from the environment on first access."""
    from bank_stub import main  # noqa: PLC0415

    for k, v in {
        "COMPANY_ID": "0193a000-0000-7000-8000-000000000001",
        "DB_PASSWORD": STRONG_SECRET,
        "BANK_STUB_API_KEY": API_KEY,
    }.items():
        monkeypatch.setenv(k, v)
    original = main.build_app
    monkeypatch.setattr(main, "build_app", lambda s: original(s, instrument=False))
    monkeypatch.delitem(main.__dict__, "app", raising=False)
    app = main.app
    assert app.title == "bank-stub"
    assert main.app is app
    monkeypatch.delitem(main.__dict__, "app")
    with pytest.raises(AttributeError):
        _ = main.nope


def test_runtime_missing_before_startup() -> None:
    """Design §1: routes refuse to run before the lifespan has built the runtime."""
    from bank_stub.runtime import get_runtime  # noqa: PLC0415
    from starlette.requests import Request  # noqa: PLC0415

    app = build_app(make_settings(), instrument=False)
    with pytest.raises(RuntimeError):
        get_runtime(Request({"type": "http", "app": app}))
