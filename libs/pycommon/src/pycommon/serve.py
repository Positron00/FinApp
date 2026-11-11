"""``python -m pycommon.serve``: run the ASGI app named by ``$PYCOMMON_APP`` under uvicorn.

Access log off (the request middleware logs ``http.request``), no proxy headers, no server header.
A failed startup check exits 2 (C29), like invalid settings.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Final

import uvicorn
from uvicorn.importer import import_from_string

__all__ = ["build_config", "main"]

DEFAULT_PORT: Final = 8000
DEFAULT_GRACEFUL_SHUTDOWN_S: Final = 60.0
STARTUP_FAILURE_EXIT: Final = 3  # uvicorn's own code when the lifespan fails for another reason


def build_config(env: Mapping[str, str] | None = None) -> uvicorn.Config:
    environ = os.environ if env is None else env
    target = environ.get("PYCOMMON_APP", "").strip()
    if not target:
        raise SystemExit("PYCOMMON_APP is not set (e.g. identity.main:app)")
    app: Any = import_from_string(target)
    state = getattr(app, "state", None)
    graceful = float(getattr(state, "shutdown_timeout_s", DEFAULT_GRACEFUL_SHUTDOWN_S))
    return uvicorn.Config(
        app,
        host="0.0.0.0",  # noqa: S104  (inside a container; NetworkPolicies restrict ingress)
        port=int(environ.get("PORT", str(DEFAULT_PORT))),
        access_log=False,
        proxy_headers=False,
        server_header=False,
        timeout_graceful_shutdown=int(graceful),
        lifespan="on",
        log_config=None,
    )


def main(env: Mapping[str, str] | None = None) -> None:
    config = build_config(env)
    server = uvicorn.Server(config)
    server.run()
    if not server.started:
        state = getattr(config.app, "state", None)
        raise SystemExit(getattr(state, "startup_exit_code", None) or STARTUP_FAILURE_EXIT)


if __name__ == "__main__":  # pragma: no cover
    main()
