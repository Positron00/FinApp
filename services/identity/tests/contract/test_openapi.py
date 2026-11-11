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
    ("post", "/v1/auth/password-reset/request"): (
        "auth_password_reset_request",
        "ResetRequestIn",
        None,
        "202",
    ),
    ("post", "/v1/auth/password-reset/confirm"): (
        "auth_password_reset_confirm",
        "ResetConfirmIn",
        "ResetConfirmOut",
        "200",
    ),
    ("post", "/v1/invites/{id}/resend"): ("invites_resend", None, "PendingInviteOut", "200"),
    ("get", "/v1/users"): ("users_list", None, "UserListOut", "200"),
    ("patch", "/v1/users/{id}"): ("users_update", "PatchUserIn", "UserOut", "200"),
    ("get", "/v1/audit-log"): ("audit_log_list", None, "AuditListOut", "200"),
}
# Route-specific documented errors (§3.4), beyond the common ones.
OWN_ERRORS = {
    ("post", "/v1/invites/{id}/resend"): ("404", "409"),
    ("patch", "/v1/users/{id}"): ("404", "409"),
    ("post", "/v1/users/invite"): ("409",),
    ("get", "/v1/users/me"): ("404",),
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
    """PAY-NFR-12, PAY-102, PAY-105, PAY-108, PAY-109: every §3.4 route is present with its
    operationId, models and error responses, and no other route is exposed."""
    paths = spec["paths"]
    found = {(m, p) for p, ops in paths.items() for m in ops}
    assert found == set(EXPECTED)
    for (method, path), (op_id, body, out, ok) in EXPECTED.items():
        op = paths[path][method]
        assert op["operationId"] == op_id
        if body is not None:
            ref = op["requestBody"]["content"]["application/json"]["schema"]["$ref"]
            assert ref.endswith(f"/{body}")
        if out is None:
            assert "content" not in op["responses"][ok]  # 202 with an empty body
        else:
            ok_ref = op["responses"][ok]["content"]["application/json"]["schema"]["$ref"]
            assert ok_ref.endswith(f"/{out}")
        for code in ("401", "403", "422", "429", "503", *OWN_ERRORS.get((method, path), ())):
            err = op["responses"][code]["content"]["application/json"]["schema"]["$ref"]
            assert err.endswith("/ErrorBody")


def test_no_token_in_query_parameters(spec: dict[str, Any]) -> None:
    """PAY-103, PAY-NFR-07: no route takes a token (or anything secret) as a query parameter."""
    for ops in spec["paths"].values():
        for op in ops.values():
            for param in op.get("parameters", []):
                assert param["in"] != "query" or "token" not in param["name"].lower()


def test_list_query_parameters(spec: dict[str, Any]) -> None:
    """PAY-108, PAY-109, R4: the lists take limit (1-100, default 50) and an opaque cursor."""
    for path in ("/v1/users", "/v1/audit-log"):
        params = {p["name"]: p for p in spec["paths"][path]["get"]["parameters"]}
        assert set(params) == {"limit", "cursor"}
        limit = params["limit"]["schema"]
        assert (limit["minimum"], limit["maximum"], limit["default"]) == (1, 100, 50)


def test_patch_status_accepts_only_inactive(spec: dict[str, Any]) -> None:
    """PAY-108, R37, C25: PatchUserIn.status is "inactive" or absent (no reactivation)."""
    status = spec["components"]["schemas"]["PatchUserIn"]["properties"]["status"]
    assert {"const": "inactive", "type": "string"} in status["anyOf"]
