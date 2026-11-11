"""alert-sink: store, list and clear Alertmanager payloads (platform LLD §6.2, §8.5; D5)."""

from __future__ import annotations

import importlib.util
import json
import threading
import urllib.request
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from types import ModuleType

import pytest
from pycommon.testing import find_repo_root

PATH = find_repo_root() / "deploy" / "observability" / "alert-sink" / "alert_sink.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("alert_sink", PATH)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def sink() -> Iterator[str]:
    mod = _load()
    server = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


def _req(url: str, method: str = "GET", body: object | None = None) -> tuple[int, object]:
    data = None if body is None else json.dumps(body).encode()
    r = urllib.request.Request(url, data=data, method=method)  # noqa: S310  (local test server)
    with urllib.request.urlopen(r, timeout=5) as resp:  # noqa: S310
        raw = resp.read()
        return resp.status, json.loads(raw) if raw else None


def _alert(name: str) -> dict[str, object]:
    return {"alerts": [{"status": "firing", "labels": {"alertname": name}}]}


@pytest.mark.allow_hosts(["127.0.0.1"])
def test_store_list_and_clear(sink: str) -> None:
    assert _req(f"{sink}/healthz")[0] == 200
    assert _req(f"{sink}/page", "POST", _alert("PayoutDLQ"))[0] == 200
    assert _req(f"{sink}/ticket", "POST", _alert("OutboxStuck"))[0] == 200
    status, page = _req(f"{sink}/page")
    assert status == 200
    assert isinstance(page, list)
    assert len(page) == 1
    assert page[0]["payload"]["alerts"][0]["labels"]["alertname"] == "PayoutDLQ"
    assert "received_at" in page[0]
    assert _req(f"{sink}/all", "DELETE")[0] == 204
    assert _req(f"{sink}/page")[1] == []
    assert _req(f"{sink}/ticket")[1] == []


@pytest.mark.allow_hosts(["127.0.0.1"])
def test_bounded_at_500_per_route_oldest_first(sink: str) -> None:
    for i in range(510):
        _req(f"{sink}/page", "POST", {"n": i})
    _, page = _req(f"{sink}/page")
    assert isinstance(page, list)
    assert len(page) == 500
    assert page[0]["payload"] == {"n": 10}
    assert page[-1]["payload"] == {"n": 509}
