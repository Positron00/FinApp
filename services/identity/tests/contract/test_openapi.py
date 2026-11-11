"""The committed OpenAPI contract (identity LLD §7.3; PAY-NFR-12, PAY-103)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from identity.main import build_app
from identity.settings import Settings

OPENAPI = Path(__file__).resolve().parents[2] / "openapi.json"
EXPECTED = {
    ("post", "/v1/auth/login"): ("auth_login", "LoginIn", "SessionUser", "200"),
    ("post", "/v1/invites/accept"): ("invites_accept", "InviteAcceptIn", "SessionUser", "200"),
    ("get", "/v1/users/me"): ("users_me", None, "MeOut", "200"),
    ("post", "/v1/users/invite"): ("users_invite", "InviteUserIn", "UserOut", "201"),
}


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    app = build_app(Settings.model_construct(), instrument=False)
    return app.openapi()


def test_committed_spec_is_current(spec: dict[str, Any]) -> None:
    """PAY-NFR-12: openapi.json equals what scripts/export_openapi.py would write."""
    rendered = json.dumps(spec, sort_keys=True, indent=2) + "\n"
    assert OPENAPI.read_text() == rendered


def test_routes_operation_ids_and_models(spec: dict[str, Any]) -> None:
    """PAY-NFR-12: every S4 route is present with its operationId, models and error responses;
    no unfinished route is exposed."""
    paths = spec["paths"]
    found = {(m, p) for p, ops in paths.items() for m in ops}
    assert found == set(EXPECTED)
    for (method, path), (op_id, body, out, ok) in EXPECTED.items():
        op = paths[path][method]
        assert op["operationId"] == op_id
        if body is not None:
            ref = op["requestBody"]["content"]["application/json"]["schema"]["$ref"]
            assert ref.endswith(f"/{body}")
        ok_ref = op["responses"][ok]["content"]["application/json"]["schema"]["$ref"]
        assert ok_ref.endswith(f"/{out}")
        for code in ("401", "403", "422", "429", "503"):
            err = op["responses"][code]["content"]["application/json"]["schema"]["$ref"]
            assert err.endswith("/ErrorBody")


def test_no_token_in_query_parameters(spec: dict[str, Any]) -> None:
    """PAY-103, PAY-NFR-07: no route takes a token (or anything secret) as a query parameter."""
    for ops in spec["paths"].values():
        for op in ops.values():
            for param in op.get("parameters", []):
                assert param["in"] != "query" or "token" not in param["name"].lower()
