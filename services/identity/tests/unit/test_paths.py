"""identity.paths.migrations_dir finds the migrations in the repo and in the image (S8 finding)."""

from __future__ import annotations

from pathlib import Path

import pytest
from identity import paths


def test_repo_layout() -> None:
    """PAY-605: in the repo, the migrations dir (readiness's head check) is found."""
    found = paths.migrations_dir()
    assert found == paths.MIGRATIONS_DIR
    assert (found / "env.py").is_file()
    assert found.parent.name == "identity"


def _fake_module(root: Path) -> Path:
    module = root / ".venv" / "lib" / "python3.14" / "site-packages" / "identity" / "paths.py"
    module.parent.mkdir(parents=True)
    module.touch()
    return module


def test_image_layout_non_editable_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PAY-605: /app/.venv/…/site-packages/identity/paths.py → /app/migrations (not the
    venv's lib dir)."""
    app = tmp_path / "app"
    (app / "migrations").mkdir(parents=True)
    (app / "migrations" / "env.py").touch()
    (app / "alembic.ini").touch()
    monkeypatch.setattr(paths, "__file__", str(_fake_module(app)))
    assert paths.migrations_dir() == app / "migrations"


def test_not_found_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PAY-605: no migrations dir anywhere → FileNotFoundError, never a wrong path."""
    monkeypatch.setattr(paths, "__file__", str(_fake_module(tmp_path / "elsewhere")))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError, match="identity migrations"):
        paths.migrations_dir()
