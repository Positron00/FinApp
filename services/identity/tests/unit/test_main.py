"""App assembly (identity LLD §1; PAY-605)."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from fastapi import FastAPI
from identity import main
from pycommon.testing import OtelMemory, TokenFactory

from tests.helpers import AUDIT_KEY, COMPANY_ID, STRONG_SECRET, web_only_keyset


def test_app_is_built_from_the_environment_on_first_access(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    token_factory: TokenFactory,
    otel_memory: OtelMemory,
) -> None:
    """PAY-605: identity.main:app (the serve path) loads settings from env once; importing
    build_app needs no env."""
    monkeypatch.chdir(tmp_path)
    for k, v in {
        "COMPANY_ID": str(COMPANY_ID),
        "DB_USER": "identity_app",
        "DB_PASSWORD": STRONG_SECRET,
        "REDIS_PASSWORD": STRONG_SECRET,
        "AUDIT_IP_HASH_KEY": AUDIT_KEY,
        "IDENTITY_PUBLIC_BASE_URL": "http://127.0.0.1:4100/",
        "INTERNAL_JWT_PUBLIC_KEYS": web_only_keyset(token_factory),
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(main, "_built", {})
    mod = importlib.import_module("identity.main")
    app = mod.app
    assert isinstance(app, FastAPI)
    assert mod.app is app
    assert app.title == "identity"
    with pytest.raises(AttributeError):
        _ = mod.not_there


def test_build_app_needs_identity_settings() -> None:
    """PAY-605: build_app refuses another service's settings."""
    from pycommon.settings import BaseServiceSettings  # noqa: PLC0415

    with pytest.raises(TypeError):
        main.build_app(BaseServiceSettings.model_construct())
