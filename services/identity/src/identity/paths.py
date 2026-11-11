"""Filesystem locations shared by the app and the CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Final

__all__ = ["MIGRATIONS_DIR"]

# services/identity/migrations (src/identity/paths.py → parents[2]).
MIGRATIONS_DIR: Final = Path(__file__).resolve().parents[2] / "migrations"
