"""scripts/sync-k8s-assets.sh keeps the Kustomize copies of shared assets exact (R32, C36).

Each case runs in a temporary copy of the files the script reads and writes, so the repository is
never touched: ``--check`` passes after a sync, and fails (exit 1, naming the file) after a source
edit, a copy edit, a deleted source or a missing copy.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "sync-k8s-assets.sh"
SOURCES = ("deploy/compose/init", "deploy/compose/redis", "deploy/observability")
COPIES = (
    "deploy/k8s/base/bootstrap/init",
    "deploy/k8s/components/in-cluster-data/redis",
    "deploy/k8s/base/otel-collector",
    "deploy/k8s/overlays/prod",
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "scripts").mkdir()
    shutil.copy2(SCRIPT, tmp_path / "scripts" / SCRIPT.name)
    for rel in SOURCES + COPIES:
        shutil.copytree(ROOT / rel, tmp_path / rel, ignore=shutil.ignore_patterns("__pycache__"))
    return tmp_path


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(repo / "scripts" / SCRIPT.name), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_committed_copies_match_their_sources() -> None:
    """R32: the copies in the repository are current (manifests.sh also diffs them in git)."""
    result = subprocess.run(
        ["bash", str(SCRIPT), "--check"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_check_passes_after_sync(repo: Path) -> None:
    assert _run(repo).returncode == 0
    result = _run(repo, "--check")
    assert result.returncode == 0, result.stderr
    assert "all copies match" in result.stdout


def test_source_edit_is_detected_and_synced(repo: Path) -> None:
    src = repo / "deploy/compose/init/10-identity.sql"
    src.write_text(src.read_text() + "-- changed\n")
    result = _run(repo, "--check")
    assert result.returncode == 1
    assert "deploy/k8s/base/bootstrap/init/10-identity.sql (differs" in result.stderr
    assert _run(repo).returncode == 0
    copy = repo / "deploy/k8s/base/bootstrap/init/10-identity.sql"
    assert copy.read_bytes() == src.read_bytes()


def test_copy_edit_is_detected(repo: Path) -> None:
    copy = repo / "deploy/k8s/components/in-cluster-data/redis/redis.conf"
    copy.write_text(copy.read_text() + "# hand edit\n")
    result = _run(repo, "--check")
    assert result.returncode == 1
    assert "in-cluster-data/redis/redis.conf (differs" in result.stderr


def test_collector_copies_are_checked(repo: Path) -> None:
    (repo / "deploy/k8s/overlays/prod/collector.prod.yaml").unlink()
    result = _run(repo, "--check")
    assert result.returncode == 1
    assert "deploy/k8s/overlays/prod/collector.prod.yaml (missing)" in result.stderr


def test_deleted_source_removes_the_copy(repo: Path) -> None:
    (repo / "deploy/compose/init/90-monitoring.sql").unlink()
    result = _run(repo, "--check")
    assert result.returncode == 1
    assert "deploy/k8s/base/bootstrap/init/90-monitoring.sql (no source)" in result.stderr
    assert _run(repo).returncode == 0
    assert not (repo / "deploy/k8s/base/bootstrap/init/90-monitoring.sql").exists()
    assert _run(repo, "--check").returncode == 0


def test_unknown_argument_exits_2(repo: Path) -> None:
    assert _run(repo, "--bogus").returncode == 2
