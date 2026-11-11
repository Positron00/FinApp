"""``python -m pycommon.serve`` (platform LLD §3.14; PAY-605, C29)."""

from typing import Any

import pytest
import uvicorn
from pycommon import serve

from tests.unit import serve_target

TARGET = "tests.unit.serve_target:app"


def test_build_config_from_env() -> None:
    """PAY-605: PYCOMMON_APP and PORT build the uvicorn config; access log, server header off."""
    config = serve.build_config({"PYCOMMON_APP": TARGET, "PORT": "8123"})
    assert config.app is serve_target.app
    assert (config.host, config.port) == ("0.0.0.0", 8123)  # noqa: S104
    assert config.access_log is False
    assert config.server_header is False
    assert config.proxy_headers is False
    assert config.lifespan == "on"
    assert config.timeout_graceful_shutdown == 12
    assert config.log_config is None
    assert serve.build_config({"PYCOMMON_APP": TARGET}).port == 8000


def test_missing_app_variable_exits() -> None:
    """PAY-605: without PYCOMMON_APP the process refuses to start."""
    with pytest.raises(SystemExit):
        serve.build_config({})


def test_failed_startup_exits_with_the_check_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """C29: a lifespan that failed a startup check (exit code 2 on app.state) exits 2."""
    ran: list[Any] = []

    def fake_run(self: uvicorn.Server, sockets: Any = None) -> None:
        ran.append(self.config.app)
        self.started = False

    monkeypatch.setattr(uvicorn.Server, "run", fake_run)
    serve_target.app.state.startup_exit_code = 2
    try:
        with pytest.raises(SystemExit) as info:
            serve.main({"PYCOMMON_APP": TARGET})
        assert info.value.code == 2
        del serve_target.app.state.startup_exit_code
        with pytest.raises(SystemExit) as other:
            serve.main({"PYCOMMON_APP": TARGET})
        assert other.value.code == 3
    finally:
        if hasattr(serve_target.app.state, "startup_exit_code"):
            del serve_target.app.state.startup_exit_code
    assert ran == [serve_target.app, serve_target.app]


def test_clean_run_returns(monkeypatch: pytest.MonkeyPatch) -> None:
    """PAY-605: a server that started and stopped returns normally."""

    def fake_run(self: uvicorn.Server, sockets: Any = None) -> None:
        self.started = True

    monkeypatch.setattr(uvicorn.Server, "run", fake_run)
    serve.main({"PYCOMMON_APP": TARGET})
