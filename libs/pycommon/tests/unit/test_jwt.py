"""Internal JWT verify and sign (platform LLD §3.5; PAY-600, C10, C30)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from datetime import timedelta
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pycommon.auth import Keyset, Principal, TokenRejected, TokenSigner, TokenVerifier
from pycommon.settings import SettingsError
from pycommon.testing import FakeClock, JsonLogCapture, TokenFactory

from tests.helpers import COMPANY_ID

AUD = "payroll"
USER = "0193a000-0000-7000-8000-0000000000aa"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def tf(clock: FakeClock) -> TokenFactory:
    return TokenFactory.create(clock=clock)


@pytest.fixture
def verifier(tf: TokenFactory, clock: FakeClock) -> TokenVerifier:
    return TokenVerifier(tf.keyset, audience=AUD, clock=clock)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _claims(clock: FakeClock, **over: Any) -> dict[str, Any]:
    iat = int(clock.now().timestamp())
    c: dict[str, Any] = {
        "iss": "web",
        "aud": AUD,
        "sub": USER,
        "cid": str(COMPANY_ID),
        "roles": ["approver"],
        "iat": iat,
        "exp": iat + 60,
        "jti": str(uuid.uuid4()),
    }
    c.update(over)
    return {k: v for k, v in c.items() if v is not None}


def _sign(tf: TokenFactory, claims: dict[str, Any], *, issuer: str = "web") -> str:
    key = serialization.load_pem_private_key(tf.private_pems[issuer].encode(), None)
    return jwt.encode(claims, key, algorithm="EdDSA", headers={"kid": tf.kids[issuer]})  # type: ignore[arg-type]


def _reason(verifier: TokenVerifier, token: str) -> str:
    with pytest.raises(TokenRejected) as info:
        verifier.verify(token)
    assert info.value.status == 401
    assert info.value.error_code == "invalid_token"
    return info.value.reason


def test_valid_web_user_token(tf: TokenFactory, verifier: TokenVerifier) -> None:
    """PAY-600: a valid web user token verifies; name round-trips to Principal.name (C10)."""
    token = tf.mint(aud=AUD, sub=USER, cid=COMPANY_ID, roles=["approver"], name="Ada L", sid="s1")
    p = verifier.verify(token)
    assert (p.iss, p.sub, p.cid, p.name, p.sid) == ("web", USER, COMPANY_ID, "Ada L", "s1")
    assert p.roles == frozenset({"approver"})
    assert p.user_id == uuid.UUID(USER)
    assert not p.is_service
    assert p.caller_key == f"web:{USER}"
    assert p.subject_kind == "user"
    assert "Ada" not in repr(p)
    assert "s1" not in repr(p)


def test_valid_worker_token_and_missing_name(tf: TokenFactory, verifier: TokenVerifier) -> None:
    """PAY-600: a worker token (roles [], no sid) verifies; name is None when absent (C10)."""
    token = tf.mint(issuer="payout-worker", aud=AUD, sub="service:payout-worker", cid=COMPANY_ID)
    p = verifier.verify(token)
    assert (p.iss, p.sub, p.name, p.sid) == ("payout-worker", "service:payout-worker", None, None)
    assert p.roles == frozenset()
    assert p.is_service
    assert p.user_id is None
    assert p.subject_kind == "service:payout-worker"


def test_missing_and_malformed(verifier: TokenVerifier) -> None:
    """PAY-600: empty → missing; garbage → malformed."""
    assert _reason(verifier, "") == "missing"
    assert _reason(verifier, "not-a-jwt") == "malformed"


def test_unknown_kid(tf: TokenFactory, clock: FakeClock, verifier: TokenVerifier) -> None:
    """PAY-600: a kid outside the keyset → 401 unknown_kid."""
    key = serialization.load_pem_private_key(tf.private_pems["web"].encode(), None)
    token = jwt.encode(_claims(clock), key, algorithm="EdDSA", headers={"kid": "other"})  # type: ignore[arg-type]
    assert _reason(verifier, token) == "unknown_kid"
    no_kid = jwt.encode(_claims(clock), key, algorithm="EdDSA")  # type: ignore[arg-type]
    assert _reason(verifier, no_kid) == "unknown_kid"


def test_alg_none_and_hs256_with_public_key_are_rejected(
    tf: TokenFactory, clock: FakeClock, verifier: TokenVerifier
) -> None:
    """PAY-600: alg must be EdDSA: ``none`` and HS256 keyed with the public PEM fail (bad_alg)."""
    payload = _b64(json.dumps(_claims(clock)).encode())
    none_header = _b64(json.dumps({"alg": "none", "kid": tf.kids["web"]}).encode())
    assert _reason(verifier, f"{none_header}.{payload}.") == "bad_alg"
    hs_header = _b64(json.dumps({"alg": "HS256", "kid": tf.kids["web"], "typ": "JWT"}).encode())
    public_pem = json.loads(tf.keyset_json)[tf.kids["web"]]["pem"].encode()
    sig = hmac.new(public_pem, f"{hs_header}.{payload}".encode(), hashlib.sha256).digest()
    assert _reason(verifier, f"{hs_header}.{payload}.{_b64(sig)}") == "bad_alg"


def test_tampered_signature(tf: TokenFactory, verifier: TokenVerifier) -> None:
    """PAY-600: a changed payload with the old signature → bad_signature."""
    token = tf.mint(aud=AUD, sub=USER, cid=COMPANY_ID, roles=["approver"])
    header, payload, sig = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    claims["roles"] = ["admin"]
    forged = f"{header}.{_b64(json.dumps(claims).encode())}.{sig}"
    assert _reason(verifier, forged) == "bad_signature"


def test_leeway_five_seconds_accepted_six_rejected(
    tf: TokenFactory, clock: FakeClock, verifier: TokenVerifier
) -> None:
    """PAY-600: 5 s leeway on exp: 5 s late passes, 6 s late is expired."""
    token = tf.mint(aud=AUD, sub=USER, cid=COMPANY_ID)
    clock.advance(65)
    assert verifier.verify(token).sub == USER
    clock.advance(1)
    assert _reason(verifier, token) == "expired"


def test_iat_in_the_future(tf: TokenFactory, clock: FakeClock, verifier: TokenVerifier) -> None:
    """PAY-600: iat more than the leeway in the future → not_yet_valid; nbf likewise."""
    future = clock.now() + timedelta(seconds=6)
    claims = _claims(clock, iat=int(future.timestamp()), exp=int(future.timestamp()) + 30)
    assert _reason(verifier, _sign(tf, claims)) == "not_yet_valid"
    ok = _claims(clock, iat=int(clock.now().timestamp()) + 5)
    assert verifier.verify(_sign(tf, ok)).sub == USER
    nbf = _claims(clock, nbf=int(clock.now().timestamp()) + 30)
    assert _reason(verifier, _sign(tf, nbf)) == "not_yet_valid"


def test_wrong_audience(tf: TokenFactory, verifier: TokenVerifier) -> None:
    """PAY-600: aud for another service → wrong_aud."""
    token = tf.mint(aud="identity", sub=USER, cid=COMPANY_ID)
    assert _reason(verifier, token) == "wrong_aud"


@pytest.mark.parametrize("claim", ["exp", "iat", "iss", "aud", "sub", "jti"])
def test_missing_required_claim(
    tf: TokenFactory, clock: FakeClock, verifier: TokenVerifier, claim: str
) -> None:
    """PAY-600: each required claim missing → missing_claim (iss/aud are required too)."""
    claims = _claims(clock)
    del claims[claim]
    assert _reason(verifier, _sign(tf, claims)) == "missing_claim"


def test_cid_and_name_rules(tf: TokenFactory, clock: FakeClock, verifier: TokenVerifier) -> None:
    """PAY-600 / C10: cid must be a UUID; name, when present, is a 1-200 character string."""
    assert _reason(verifier, _sign(tf, _claims(clock, cid=None))) == "missing_claim"
    assert _reason(verifier, _sign(tf, _claims(clock, cid="not-a-uuid"))) == "missing_claim"
    assert _reason(verifier, _sign(tf, _claims(clock, name="x" * 201))) == "malformed"
    assert _reason(verifier, _sign(tf, _claims(clock, name=""))) == "malformed"
    assert verifier.verify(_sign(tf, _claims(clock, name="x" * 200))).name == "x" * 200
    assert _reason(verifier, _sign(tf, _claims(clock, roles="admin"))) == "malformed"
    assert _reason(verifier, _sign(tf, _claims(clock, sid=5))) == "malformed"
    assert _reason(verifier, _sign(tf, _claims(clock, exp="soon"))) == "malformed"
    assert verifier.verify(_sign(tf, _claims(clock, roles=None))).roles == frozenset()


def test_lifetime_over_sixty_seconds(tf: TokenFactory, verifier: TokenVerifier) -> None:
    """PAY-600: exp - iat > 60 s → lifetime_too_long."""
    assert verifier.verify(tf.mint(aud=AUD, sub=USER, cid=COMPANY_ID, ttl_s=60)).sub == USER
    token = tf.mint(aud=AUD, sub=USER, cid=COMPANY_ID, ttl_s=61)
    assert _reason(verifier, token) == "lifetime_too_long"


def test_worker_key_cannot_mint_web_tokens(
    tf: TokenFactory, clock: FakeClock, verifier: TokenVerifier
) -> None:
    """PAY-600: a token signed with the worker key but claiming iss=web → issuer_mismatch."""
    forged = _sign(tf, _claims(clock, iss="web"), issuer="payout-worker")
    assert _reason(verifier, forged) == "issuer_mismatch"


def test_token_never_appears_in_logs(
    tf: TokenFactory, verifier: TokenVerifier, caplog_json: JsonLogCapture
) -> None:
    """PAY-600 / PAY-NFR-07: auth.token.rejected logs reason, kid, iss; never the token."""
    token = tf.mint(aud="identity", sub=USER, cid=COMPANY_ID, name="Grace", sid="sess-9")
    _reason(verifier, token)
    (event,) = caplog_json.events("auth.token.rejected")
    assert (event["reason"], event["kid"], event["iss"]) == ("wrong_aud", "test-web-1", "web")
    assert token not in caplog_json.text
    assert token.split(".")[2] not in caplog_json.text
    assert "Grace" not in caplog_json.text
    assert "sess-9" not in caplog_json.text


def test_keyset_from_json_rejections(tf: TokenFactory) -> None:
    """PAY-600: Keyset.from_json rejects bad JSON, non-objects, unknown iss, non-Ed25519, dups."""
    pem = json.loads(tf.keyset_json)["test-web-1"]["pem"]
    ec_pem = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    bad_inputs = [
        "{not json",
        "[]",
        "{}",
        json.dumps({"k": "pem-string"}),
        json.dumps({"k": {"iss": "admin", "pem": pem}}),
        json.dumps({"k": {"iss": "web"}}),
        json.dumps({"k": {"iss": "web", "pem": "garbage"}}),
        json.dumps({"k": {"iss": "web", "pem": ec_pem}}),
        f'{{"k": {{"iss": "web", "pem": {json.dumps(pem)}}}, '
        f'"k": {{"iss": "web", "pem": {json.dumps(pem)}}}}}',
    ]
    for raw in bad_inputs:
        with pytest.raises(SettingsError) as info:
            Keyset.from_json(raw)
        assert "BEGIN" not in str(info.value)
    ks = Keyset.from_json(json.dumps({"a": {"iss": "web", "pem": pem.replace("\n", "\\n")}}))
    assert ks.kids == {"a"}
    entry = ks.get("a")
    assert entry is not None
    assert entry.iss == "web"
    assert ks.get("zzz") is None
    assert "a" in repr(ks)
    with pytest.raises(SettingsError):
        Keyset([entry, entry])


def test_token_signer_mint_claims(tf: TokenFactory, clock: FakeClock) -> None:
    """PAY-600: mint sets iss, aud, sub, cid, roles, iat, exp, jti; name and sid only when given."""
    signer = tf.signers["web"]
    assert (signer.kid, signer.issuer) == ("test-web-1", "web")
    token = signer.mint(aud=AUD, sub=USER, cid=COMPANY_ID, roles=("a", "b"), ttl_s=30)
    header = jwt.get_unverified_header(token)
    assert (header["alg"], header["kid"]) == ("EdDSA", "test-web-1")
    claims = jwt.decode(token, options={"verify_signature": False})
    assert set(claims) == {"iss", "aud", "sub", "cid", "roles", "iat", "exp", "jti"}
    assert claims["exp"] - claims["iat"] == 30
    assert claims["iat"] == int(clock.now().timestamp())
    assert uuid.UUID(claims["jti"]).version == 7  # time-ordered (payout-worker LLD §3.6)
    assert claims["roles"] == ["a", "b"]
    full = jwt.decode(
        signer.mint(aud=AUD, sub=USER, cid=COMPANY_ID, name="N", sid="S"),
        options={"verify_signature": False},
    )
    assert (full["name"], full["sid"]) == ("N", "S")


def test_token_signer_rejects_bad_keys(tf: TokenFactory) -> None:
    """PAY-600: the signer needs an Ed25519 key and a known issuer."""
    ec_private = (
        ec.generate_private_key(ec.SECP256R1())
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )
    with pytest.raises(ValueError, match="Ed25519"):
        TokenSigner(ec_private, kid="k", issuer="web")
    with pytest.raises(ValueError, match="issuer"):
        TokenSigner(tf.private_pems["web"], kid="k", issuer="admin")  # type: ignore[arg-type]


def test_principal_subject_kinds() -> None:
    """PAY-601: subject kind is user (UUID sub), service:web, service:payout-worker or none."""
    base: dict[str, Any] = {
        "iss": "web",
        "roles": frozenset(),
        "cid": COMPANY_ID,
        "name": None,
        "sid": None,
        "jti": "j",
        "exp": FakeClock().now(),
    }
    assert Principal(sub="service:web", **base).subject_kind == "service:web"
    odd = Principal(sub="robot", **base)
    assert odd.subject_kind is None
    assert odd.user_id is None
