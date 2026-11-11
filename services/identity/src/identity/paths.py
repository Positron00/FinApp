"""Filesystem locations shared by the app and the CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Final

__all__ = ["MIGRATIONS_DIR", "migrations_dir"]


def migrations_dir() -> Path:
    """``services/identity/migrations`` in the repo, ``/app/migrations`` in the image.

    The nearest ancestor of this module (then the working directory) that holds ``alembic.ini``
    and ``migrations/env.py``, as payroll, payout-worker and bank-stub do. A fixed ``parents[2]``
    pointed into the venv's ``lib/python3.X`` in a non-editable install (S8 finding).
    """
    for base in (*Path(__file__).resolve().parents, Path.cwd()):
        if (base / "alembic.ini").is_file() and (base / "migrations" / "env.py").is_file():
            return base / "migrations"
    raise FileNotFoundError("identity migrations directory not found")


MIGRATIONS_DIR: Final = migrations_dir()
