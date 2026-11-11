"""The worker JWT (design §3.6; PAY-600, R9, C30)."""

from __future__ import annotations

import json
from uuid import UUID

import jwt
import pytest
from payout_worker.jwt_signer import WorkerJwtSigner
from pycommon.auth import Keyset, TokenRejected, TokenVerifier
from pycommon.testing import FakeClock
from pydantic import SecretStr

from tests.helpers import COMPANY_ID, TOKENS

OTHER_CID = UUID("0193a000-0000-7000-8000-000000000002")


def _signer(clock: FakeClock) -> WorkerJwtSigner:
    return WorkerJwtSigner(
        SecretStr(TOKENS.private_pems["payout-worker"]), TOKENS.kids["payout-worker"], clock=clock
    )


def test_header_and_claims() -> None:
    """PAY-600, R9: EdDSA with the kid; iss/sub/aud fixed; cid from the event; roles []; jti;
    exp - iat = 60; no sid, no name."""
    clock = FakeClock()
    token = _signer(clock).mint(OTHER_CID)
    header = jwt.get_unverified_header(token)
    assert (header["alg"], header["kid"]) == ("EdDSA", TOKENS.kids["payout-worker"])
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["iss"] == "payout-worker"
    assert claims["sub"] == "service:payout-worker"
    assert claims["aud"] == "payroll"
    assert claims["cid"] == str(OTHER_CID)
    assert claims["roles"] == []
    assert claims["exp"] - claims["iat"] == 60
    assert "sid" not in claims
    assert "name" not in claims
    assert claims["jti"]


def test_unique_jti_and_verifies() -> None:
    """PAY-600: every token has its own jti and verifies with the issuer-bound keyset."""
    clock = FakeClock()
    signer = _signer(clock)
    a, b = signer.mint(COMPANY_ID), signer.mint(COMPANY_ID)
    ja = jwt.decode(a, options={"verify_signature": False})["jti"]
    jb = jwt.decode(b, options={"verify_signature": False})["jti"]
    assert ja != jb
    p = TokenVerifier(TOKENS.keyset, audience="payroll", clock=clock).verify(a)
    assert (p.iss, p.sub, p.cid) == ("payout-worker", "service:payout-worker", COMPANY_ID)


def test_rejected_when_kid_bound_to_web() -> None:
    """PAY-600, C30: a keyset binding the worker's kid to iss=web → 401 issuer_mismatch."""
    doc = json.loads(TOKENS.keyset_json)
    doc[TOKENS.kids["payout-worker"]]["iss"] = "web"
    keyset = Keyset.from_json(json.dumps(doc))
    clock = FakeClock()
    with pytest.raises(TokenRejected) as e:
        TokenVerifier(keyset, audience="payroll", clock=clock).verify(
            _signer(clock).mint(COMPANY_ID)
        )
    assert e.value.reason == "issuer_mismatch"
