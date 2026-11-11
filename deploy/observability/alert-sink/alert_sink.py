"""alert-sink: a tiny Alertmanager webhook receiver for dev and CI (platform LLD §8.5, D5).

Python stdlib only. POST /page and /ticket store the payload (at most 256 KB) with a receive
timestamp in a per-route ring buffer of 500 and log one JSON line per alert; GET returns the stored
payloads oldest first; DELETE /all clears both buffers; GET /healthz answers 200.
Run: ``python alert_sink.py`` (listens on ``ALERT_SINK_PORT`` inside the container, default 8080).
"""

from __future__ import annotations

import json
import os
import sys
import threading
from collections import deque
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MAX_BODY = 256 * 1024
RING = 500
ROUTES = ("page", "ticket")

_lock = threading.Lock()
_store: dict[str, deque[dict[str, Any]]] = {r: deque(maxlen=RING) for r in ROUTES}


def _log(event: str, **fields: Any) -> None:
    line = {"ts": datetime.now(UTC).isoformat(), "level": "info", "event": event, **fields}
    sys.stdout.write(json.dumps(line) + "\n")
    sys.stdout.flush()


class Handler(BaseHTTPRequestHandler):
    server_version = "alert-sink"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:
        return  # one JSON line per alert instead of the default access log

    def _send(self, status: int, body: object | None = None) -> None:
        data = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _route(self) -> str | None:
        name = self.path.split("?", 1)[0].strip("/")
        return name if name in ROUTES else None

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] == "/healthz":
            self._send(200, {"status": "ok"})
            return
        route = self._route()
        if route is None:
            self._send(404, {"error": "not_found"})
            return
        with _lock:
            items = list(_store[route])
        self._send(200, items)

    def do_POST(self) -> None:
        route = self._route()
        if route is None:
            self._send(404, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send(400, {"error": "bad_length"})
            return
        if length > MAX_BODY:
            self._send(413, {"error": "payload_too_large"})
            return
        try:
            payload = json.loads(self.rfile.read(length) or b"null")
        except json.JSONDecodeError:
            self._send(400, {"error": "bad_json"})
            return
        item = {"received_at": datetime.now(UTC).isoformat(), "payload": payload}
        with _lock:
            _store[route].append(item)
        alerts = payload.get("alerts", []) if isinstance(payload, dict) else []
        for alert in alerts if isinstance(alerts, list) else []:
            labels = alert.get("labels", {}) if isinstance(alert, dict) else {}
            _log(
                "alert_sink.received",
                route=route,
                alertname=labels.get("alertname"),
                status=alert.get("status") if isinstance(alert, dict) else None,
            )
        self._send(200, {"stored": True})

    def do_DELETE(self) -> None:
        if self.path.split("?", 1)[0] != "/all":
            self._send(404, {"error": "not_found"})
            return
        with _lock:
            for buf in _store.values():
                buf.clear()
        self._send(204)


def main() -> None:
    port = int(os.environ.get("ALERT_SINK_LISTEN_PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)  # noqa: S104  (container only)
    _log("alert_sink.started", port=port)
    server.serve_forever()


if __name__ == "__main__":
    main()
