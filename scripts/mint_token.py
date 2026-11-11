"""Mint a short-lived internal JWT for dev and CI tests (platform LLD §3.15; PAY-609, C35, R9, R30).

CLI::

    uv run python scripts/mint_token.py --aud payroll \
        --sub <uuid|service:web|service:payout-worker> \
        [--roles admin,approver] [--issuer web|payout-worker] [--cid UUID] [--name NAME]

prints the token and a newline on stdout and nothing else. Importable::

    from scripts.mint_token import mint      # or `from mint_token import mint` inside scripts/

Keys come from the process environment, else from ``./.env`` (written by ``scripts/dev-env.sh``):
``issuer="web"`` signs with ``INTERNAL_JWT_PRIVATE_KEY``/``INTERNAL_JWT_KID``, ``"payout-worker"``
with ``PAYOUT_WORKER_JWT_PRIVATE_KEY``/``PAYOUT_WORKER_JWT_KID``. Refuses unless ``ENV == "dev"``.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, Literal, NoReturn
from uuid import UUID

from pycommon.auth import TokenSigner
from pycommon.settings import normalise_pem

__all__ = ["DEFAULT_COMPANY_ID", "EnvError", "load_env", "main", "mint", "parse_env_file"]

DEFAULT_COMPANY_ID: Final = "0193a000-0000-7000-8000-000000000001"  # C28
REPO_ROOT: Final = Path(__file__).resolve().parents[1]
TTL_S: Final = 60  # PAY-600

Issuer = Literal["web", "payout-worker"]

_KEYS: Final[dict[str, tuple[str, str]]] = {
    "web": ("INTERNAL_JWT_PRIVATE_KEY", "INTERNAL_JWT_KID"),
    "payout-worker": ("PAYOUT_WORKER_JWT_PRIVATE_KEY", "PAYOUT_WORKER_JWT_KID"),
}


class EnvError(Exception):
    """A required key is missing, or ENV is not dev. The message names keys, never values."""


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a dev-env.sh ``.env``.

    Handles ``K=v``, ``K="...\\n..."`` (escapes kept) and ``K='literal'``.
    """
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        out[key] = value
    return out


def load_env(env_file: Path | None = None) -> dict[str, str]:
    """``.env`` values overlaid by the process environment (the process wins)."""
    merged = parse_env_file(env_file or REPO_ROOT / ".env")
    merged.update(os.environ)
    return merged


def _require_dev(env: Mapping[str, str]) -> None:
    if env.get("ENV", "dev") != "dev":
        raise EnvError("ENV must be dev")


def mint(
    aud: str,
    sub: str,
    roles: Sequence[str],
    *,
    issuer: Issuer = "web",
    cid: UUID | str | None = None,
    name: str | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """A 60 s EdDSA token with a fresh ``jti`` (PAY-600, R9).

    - user ``sub`` (a UUID): ``roles``, ``name`` and a random ``sid``;
    - ``sub="service:web"`` (issuer web): ``roles == []``, no ``name`` or ``sid``;
    - ``issuer="payout-worker"``: ``sub`` must be ``service:payout-worker`` and ``roles == []``.

    Raises ``ValueError`` for any other combination and :class:`EnvError` outside dev.
    """
    e = load_env() if env is None else env
    _require_dev(e)
    if issuer not in _KEYS:
        raise ValueError("issuer must be web or payout-worker")
    role_list = list(roles)
    sid: str | None = None
    if issuer == "payout-worker":
        if sub != "service:payout-worker" or role_list or name is not None:
            raise ValueError(
                "payout-worker tokens need sub=service:payout-worker, no roles, no name"
            )
    elif sub == "service:web":
        if role_list or name is not None:
            raise ValueError("service:web tokens carry no roles and no name")
    else:
        if sub.startswith("service:"):
            raise ValueError("issuer web signs service:web or a user UUID")
        try:
            UUID(sub)
        except ValueError:
            raise ValueError("a user sub must be a UUID") from None
        sid = str(uuid.uuid4())
    key_name, kid_name = _KEYS[issuer]
    missing = [k for k in (key_name, kid_name) if not e.get(k)]
    if missing:
        raise EnvError(f"missing {', '.join(missing)} (run scripts/dev-env.sh)")
    company = UUID(str(cid if cid is not None else e.get("COMPANY_ID") or DEFAULT_COMPANY_ID))
    signer = TokenSigner(normalise_pem(e[key_name]), kid=e[kid_name], issuer=issuer)
    return signer.mint(
        aud=aud, sub=sub, cid=company, roles=role_list, name=name, sid=sid, ttl_s=TTL_S
    )


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        sys.stderr.write(f"mint_token: {message}\n")
        raise SystemExit(2)


def main(argv: Sequence[str] | None = None) -> int:
    p = _Parser(description="Mint a 60 s internal JWT (dev and CI only).")
    p.add_argument("--aud", required=True, help="identity | payroll")
    p.add_argument("--sub", required=True, help="a user UUID, service:web or service:payout-worker")
    p.add_argument("--roles", default="", help="comma-separated roles (user tokens only)")
    p.add_argument("--issuer", default="web", choices=["web", "payout-worker"])
    p.add_argument("--cid", default=None, help="company id (default COMPANY_ID)")
    p.add_argument("--name", default=None, help="display name (user tokens only)")
    a = p.parse_args(argv)
    roles = [r.strip() for r in a.roles.split(",") if r.strip()]
    try:
        token = mint(a.aud, a.sub, roles, issuer=a.issuer, cid=a.cid, name=a.name)
    except (ValueError, EnvError) as exc:
        sys.stderr.write(f"mint_token: {exc}\n")
        return 2
    sys.stdout.write(token + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
