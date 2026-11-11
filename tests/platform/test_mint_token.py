"""scripts/mint_token.py (platform LLD §3.15, §6.2; PAY-600, PAY-609, C35, R9)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pycommon.testing import find_repo_root

ROOT = find_repo_root()
sys.path.insert(0, str(ROOT))
from scripts.mint_token import EnvError, mint  # noqa: E402  (repo root added just above)

CID = "0193a000-0000-7000-8000-000000000001"
USER = "01a1c95c-bbad-76db-aea0-1f15be6e6022"


def _pem(key: Ed25519PrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


@pytest.fixture
def keys() -> tuple[Ed25519PrivateKey, Ed25519PrivateKey, dict[str, str]]:
    web, worker = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    env = {
        "ENV": "dev",
        "COMPANY_ID": CID,
        "INTERNAL_JWT_PRIVATE_KEY": _pem(web).replace("\n", "\\n"),  # .env encoding
        "INTERNAL_JWT_KID": "dev-web-test",
        "PAYOUT_WORKER_JWT_PRIVATE_KEY": _pem(worker),
        "PAYOUT_WORKER_JWT_KID": "dev-worker-test",
    }
    return web, worker, env


def _claims(token: str, key: Ed25519PrivateKey, aud: str) -> dict[str, object]:
    out: dict[str, object] = jwt.decode(token, key.public_key(), algorithms=["EdDSA"], audience=aud)
    return out


def test_user_token(keys: tuple[Ed25519PrivateKey, Ed25519PrivateKey, dict[str, str]]) -> None:
    web, _, env = keys
    token = mint("payroll", USER, ["approver"], name="Owner", env=env)
    assert jwt.get_unverified_header(token)["kid"] == "dev-web-test"
    c = _claims(token, web, "payroll")
    assert c["iss"] == "web"
    assert c["sub"] == USER
    assert c["cid"] == CID
    assert c["roles"] == ["approver"]
    assert c["name"] == "Owner"
    assert c["sid"]
    assert isinstance(c["exp"], int)
    assert isinstance(c["iat"], int)
    assert c["exp"] - c["iat"] == 60
    assert c["jti"] != _claims(mint("payroll", USER, [], env=env), web, "payroll")["jti"]


def test_service_tokens(keys: tuple[Ed25519PrivateKey, Ed25519PrivateKey, dict[str, str]]) -> None:
    web, worker, env = keys
    c = _claims(mint("identity", "service:web", [], env=env), web, "identity")
    assert c["roles"] == []
    assert "name" not in c
    assert "sid" not in c
    t = mint("payroll", "service:payout-worker", [], issuer="payout-worker", env=env)
    c = _claims(t, worker, "payroll")
    assert c["iss"] == "payout-worker"
    assert c["roles"] == []
    assert "sid" not in c


@pytest.mark.parametrize(
    ("sub", "roles", "issuer"),
    [
        ("service:web", ["admin"], "web"),
        ("service:payout-worker", [], "web"),
        ("not-a-uuid", [], "web"),
        (USER, [], "payout-worker"),
        ("service:payout-worker", ["admin"], "payout-worker"),
    ],
)
def test_bad_pairs_raise(
    keys: tuple[Ed25519PrivateKey, Ed25519PrivateKey, dict[str, str]],
    sub: str,
    roles: list[str],
    issuer: str,
) -> None:
    with pytest.raises(ValueError, match=r"(tokens|sub|issuer)"):
        mint("payroll", sub, roles, issuer=issuer, env=keys[2])  # type: ignore[arg-type]


def test_refuses_outside_dev(
    keys: tuple[Ed25519PrivateKey, Ed25519PrivateKey, dict[str, str]],
) -> None:
    with pytest.raises(EnvError):
        mint("payroll", "service:web", [], env={**keys[2], "ENV": "uat"})


def test_cli_prints_only_the_token_and_exits_2_outside_dev(
    keys: tuple[Ed25519PrivateKey, Ed25519PrivateKey, dict[str, str]], tmp_path: Path
) -> None:
    script = ROOT / "scripts" / "mint_token.py"
    env = {**os.environ, **keys[2]}
    ok = subprocess.run(
        [sys.executable, str(script), "--aud", "identity", "--sub", "service:web"],
        env=env, capture_output=True, text=True, check=False, cwd=tmp_path,
    )  # fmt: skip
    assert ok.returncode == 0
    assert ok.stderr == ""
    assert ok.stdout.count("\n") == 1
    assert ok.stdout.startswith("eyJ")
    bad = subprocess.run(
        [sys.executable, str(script), "--aud", "identity", "--sub", "service:web"],
        env={**env, "ENV": "uat"}, capture_output=True, text=True, check=False, cwd=tmp_path,
    )  # fmt: skip
    assert bad.returncode == 2
    assert bad.stdout == ""
