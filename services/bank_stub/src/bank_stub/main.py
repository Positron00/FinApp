"""The bank-stub app (design §1, §3.5).

``build_app(settings)`` assembles pycommon's app with no Redis and no JWT: OTel, request logging,
the 256 KB body limit, error handlers, the transfers router (``X-Api-Key`` per route), and
``/livez`` and ``/readyz`` (DB and migrations only; R6). The startup checks, including
``check_not_prod``, run before the engine is created. ``app`` is built lazily from the
environment, for ``PYCOMMON_APP=bank_stub.main:app``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from pycommon.app import AppContext, create_app
from pycommon.settings import BaseServiceSettings, load_settings

from bank_stub.api.transfers import router as transfers_router
from bank_stub.db.repo import TransferRepo
from bank_stub.metrics import Metrics
from bank_stub.runtime import Runtime
from bank_stub.settings import STARTUP_CHECKS, Settings

__all__ = ["build_app", "migrations_dir"]


def migrations_dir() -> str:
    """``services/bank_stub/migrations`` in the repo, ``/app/migrations`` in the image."""
    for base in (*Path(__file__).resolve().parents, Path.cwd()):
        if (base / "alembic.ini").is_file() and (base / "migrations" / "env.py").is_file():
            return str(base / "migrations")
    raise FileNotFoundError("bank-stub migrations directory not found")  # pragma: no cover


def build_app(settings: BaseServiceSettings, *, instrument: bool = True) -> FastAPI:
    if not isinstance(settings, Settings):
        raise TypeError("build_app needs bank-stub Settings")
    s = settings
    holder: dict[str, FastAPI] = {}

    async def on_startup(ctx: AppContext) -> None:
        if ctx.sessions is None:  # pragma: no cover  (use_db is on)
            raise RuntimeError("bank-stub needs Postgres")
        holder["app"].state.bank_stub = Runtime(
            settings=s, repo=TransferRepo(ctx.sessions), metrics=Metrics.create()
        )

    app = create_app(
        s,
        public_routers=[transfers_router],
        use_redis=False,
        use_jwt=False,
        migrations_script_location=migrations_dir(),
        startup_checks=STARTUP_CHECKS,
        on_startup=on_startup,
        instrument=instrument,
    )
    holder["app"] = app
    return app


def __getattr__(name: str) -> Any:
    """``bank_stub.main:app``: built on first access from the environment (``load_settings``)."""
    if name == "app":
        built = build_app(load_settings(Settings))
        globals()["app"] = built
        return built
    raise AttributeError(name)
