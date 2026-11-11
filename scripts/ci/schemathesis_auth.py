"""Schemathesis auth hook: a fresh internal JWT per request (platform LLD §3.15; R30, C35).

Loaded with ``SCHEMATHESIS_HOOKS=scripts.ci.schemathesis_auth`` from the repo root. Settings:

- ``PAYRUN_SERVICE``: the service under test (``identity``, ``payroll`` or ``bank_stub``);
- ``SEED_JSON``: the ``scripts/seed.py`` summary (``users.owner``), default ``ci-logs/seed.json``.

Routes get the token the real caller would send:

- identity's public-flow routes: ``sub="service:web"``, no roles; every identity route also
  gets ``X-Client-IP: 127.0.0.1``, as the BFF sends it;
- ``/v1/internal/*``: ``issuer="payout-worker"``, ``sub="service:payout-worker"``;
- every other route: the owner with ``[admin, approver, payroll_admin]`` and ``name="Owner"``;
- bank-stub: ``X-Api-Key: $BANK_STUB_API_KEY`` and no JWT.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Final

import schemathesis

from scripts.mint_token import load_env, mint

PUBLIC_FLOW: Final = frozenset(
    {
        "/v1/auth/login",
        "/v1/invites/accept",
        "/v1/auth/password-reset/request",
        "/v1/auth/password-reset/confirm",
    }
)
OWNER_ROLES: Final = ("admin", "approver", "payroll_admin")
REPO_ROOT: Final = Path(__file__).resolve().parents[2]

_env = load_env()
_service = os.environ.get("PAYRUN_SERVICE", "").replace("-", "_")
_aud = _service.replace("_", "-")


def _owner_id() -> str:
    path = Path(os.environ.get("SEED_JSON", str(REPO_ROOT / "ci-logs/seed.json")))
    return str(json.loads(path.read_text(encoding="utf-8"))["users"]["owner"])


_owner = _owner_id() if _service in {"identity", "payroll"} else ""


def _headers_for(path: str) -> dict[str, str]:
    if _service == "bank_stub":
        return {"X-Api-Key": _env["BANK_STUB_API_KEY"]}
    # The BFF always sends a valid X-Client-IP to identity when it knows the IP (C16).
    ip = {"X-Client-IP": "127.0.0.1"} if _service == "identity" else {}
    if _service == "identity" and path in PUBLIC_FLOW:
        token = mint(_aud, "service:web", [], env=_env)
        return {"Authorization": f"Bearer {token}", **ip}
    if path.startswith("/v1/internal/"):
        token = mint(_aud, "service:payout-worker", [], issuer="payout-worker", env=_env)
        return {"Authorization": f"Bearer {token}"}
    token = mint(_aud, _owner, OWNER_ROLES, name="Owner", env=_env)
    return {"Authorization": f"Bearer {token}", **ip}


@schemathesis.auth()
class PayRunAuth:
    """Mints in ``set``, so every request carries its own 60 s token (``get`` is a stub)."""

    def get(self, case: Any, ctx: Any) -> str:
        return "per-request"

    def set(self, case: Any, data: str, ctx: Any) -> None:
        if not _service:
            raise RuntimeError("PAYRUN_SERVICE is not set")
        case.headers = case.headers or {}
        # A generated header of the same name (e.g. a fuzzed X-Api-Key) is replaced.
        case.headers.update(_headers_for(case.operation.path))
