"""Unit-suite isolation: no inherited service env, no repo ``.env``."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_PREFIXES = ("DB_", "REDIS_", "PAYOUT_", "BANK_", "OTEL_TRACES_SAMPLER", "PORT")
_KEYS = {"ENV", "LOG_LEVEL", "DEBUG", "DOCS_ENABLED", "COMPANY_ID", "APP_VERSION"}


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in list(os.environ):
        if key.startswith(_PREFIXES) or key in _KEYS:
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
